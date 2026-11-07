"""ChatGPT account manager against the real Store, issue registry, timers, aiohttp mock and PyJWT.

The stubbed tier (``tests/test_chatgpt_account.py``) covers the logic. These tests prove what stubs
cannot: that a real ``Store`` swallows write errors and the read-back catches it, that the file
is private, that a restart really reloads, and that the real PyJWT rejects bad ID tokens during
sign-in. All tokens are fake, the keys are generated here and all HTTP is mocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.climate_advisor import chatgpt_account
from custom_components.climate_advisor.ai_backend import AIBackendError
from custom_components.climate_advisor.chatgpt_account import (
    CHATGPT_ACCOUNT_HASS_KEY,
    REAUTH_ISSUE_ID,
    REVOCATION_CONFIRMED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_REAUTH_REQUIRED,
    STORAGE_KEY,
    AccountLoginError,
    AccountReauthRequired,
    async_get_account_manager,
    async_remove_account,
)
from custom_components.climate_advisor.const import DOMAIN

CLIENT_ID = "oaiapp_FAKECLIENT0001"
SUBJECT = "user-FAKESUBJECT0001"
EMAIL = "owner@example.test"
ACCESS = "at_FAKEACCESS00000"
REFRESH = "rt_FAKEREFRESH0000"
CODE = "fake-auth-code-0001"
OWNER = "ha-user-1"
ISSUER = "https://auth.openai.com"
DIRECT = "chatgpt.tokens.use.direct"
REVOKE_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
JWKS_URI = "https://auth.openai.com/.well-known/jwks.json"
HOUR = 3600

_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_RSA_KEY.public_key(), as_dict=True)
_JWKS = {"keys": [{**_PUBLIC_JWK, "kid": "k1", "use": "sig", "alg": "RS256"}]}


def _id_token(login_nonce: str, key=_RSA_KEY, alg: str = "RS256", kid: str = "k1", **overrides) -> str:
    now = time.time()
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "email": EMAIL,
        "iat": now,
        "exp": now + HOUR,
        "nonce": login_nonce,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _exchange_body(id_token: str, **overrides) -> dict:
    body = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": HOUR,
        "scope": f"openid offline_access {DIRECT}",
    }
    body.update(overrides)
    return body


def _token_ok(n: int = 1) -> dict:
    return {
        "access_token": f"at_NEWACCESS{n:05d}",
        "refresh_token": f"rt_NEWREFRESH{n:05d}",
        "token_type": "Bearer",
        "expires_in": HOUR,
        "scope": f"openid offline_access {DIRECT}",
    }


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _callback(url: str, **overrides) -> str:
    params = {"code": CODE, "state": _query(url)["state"], "client_id": CLIENT_ID}
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return f"{chatgpt_account.LOGIN_REDIRECT_URI}?{urllib.parse.urlencode(params)}"


def _serve_auth(aioclient_mock, token_body: dict | None = None, jwks: dict | None = None) -> None:
    aioclient_mock.clear_requests()
    doc = {"issuer": ISSUER, "jwks_uri": JWKS_URI, "revocation_endpoint": REVOKE_URL}
    aioclient_mock.get(chatgpt_account.DISCOVERY_URL, json=doc)
    aioclient_mock.get(JWKS_URI, json=jwks if jwks is not None else _JWKS)
    if token_body is not None:
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=token_body)


def _restart(hass) -> None:
    """Forget the manager; only the file remains."""
    manager = hass.data.pop(CHATGPT_ACCOUNT_HASS_KEY, None)
    if manager is not None:
        manager.async_cancel_runtime_tasks()


@pytest.fixture
def hass_storage():
    """Override the plugin's fixture, which ``hass`` requests: it replaces Store's reads and writes
    with an in-memory dict, and those real reads and writes are what this file exists to test."""
    return {}


@pytest.fixture
def config_dir(hass, tmp_path) -> Path:
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def _store_file(config_dir: Path) -> Path:
    return config_dir / ".storage" / STORAGE_KEY


def _on_disk(config_dir: Path) -> dict:
    return json.loads(_store_file(config_dir).read_text(encoding="utf-8"))


async def _connected(hass, aioclient_mock):
    """A complete sign-in with real PyJWT verification; request logs are cleared afterwards."""
    manager = await async_get_account_manager(hass)
    url = await manager.async_begin_login()
    _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
    await manager.async_complete_login(_callback(url), OWNER)
    aioclient_mock.clear_requests()
    return manager


def _revokes(aioclient_mock) -> list:
    return [call for call in aioclient_mock.mock_calls if str(call[1]) == REVOKE_URL]


async def _sign_in_landed_but_unproved(hass, aioclient_mock):
    """A sign-in whose write really reached the file but whose read-back failed."""
    manager = await async_get_account_manager(hass)
    url = await manager.async_begin_login()
    _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
    aioclient_mock.post(REVOKE_URL, text="")
    real_load = manager._store.async_load
    real_save = manager._store.async_save

    async def save_then_break_reads(data):
        await real_save(data)
        manager._store.async_load = AsyncMock(side_effect=OSError("read failed"))

    manager._store.async_save = save_then_break_reads
    with pytest.raises(AccountLoginError) as exc:
        await asyncio.wait_for(manager.async_complete_login(_callback(url), OWNER), 5)
    manager._store.async_save = real_save
    manager._store.async_load = real_load
    assert exc.value.reason == "save_failed"
    return manager


class TestRealStore:
    async def test_file_is_private_and_holds_the_stored_shape(self, hass, config_dir, aioclient_mock):
        manager = await _connected(hass, aioclient_mock)
        path = _store_file(config_dir)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        on_disk = _on_disk(config_dir)
        assert on_disk["version"] == 1
        assert on_disk["data"]["credentials"]["refresh_token"] == REFRESH
        assert "id_token" not in on_disk["data"]["credentials"]
        manager.async_cancel_runtime_tasks()

    async def test_pending_login_never_reaches_the_file(self, hass, config_dir):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        raw = _store_file(config_dir).read_text(encoding="utf-8")
        for secret in (_query(url)["state"], _query(url)["nonce"], manager._pending_login.verifier):
            assert secret not in raw

    async def test_restart_reloads_tokens_owner_and_host_id(self, hass, config_dir, aioclient_mock):
        first = await _connected(hass, aioclient_mock)
        host_id = first.host_id
        _restart(hass)
        second = await async_get_account_manager(hass)
        assert second is not first
        assert second.state == STATE_CONNECTED
        assert second.owner_user_id == OWNER
        assert second.host_id == host_id
        assert await second.async_get_access_token() == ACCESS
        second.async_cancel_runtime_tasks()

    async def test_store_really_swallows_write_errors(self, hass, config_dir):
        """The premise of the read-back: if this ever starts raising, simplify _persist."""
        store = Store(hass, 1, "climate_advisor.chatgpt_account_probe", private=True, atomic_writes=True)
        with patch("homeassistant.helpers.storage.write_utf8_file_atomic", side_effect=WriteError("disk full")):
            await store.async_save({"written": True})
        assert await store.async_load() is None

    async def test_swallowed_write_withholds_rotated_token_until_saved(self, hass, config_dir, aioclient_mock):
        manager = await _connected(hass, aioclient_mock)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())
        with (
            patch.object(chatgpt_account, "_now", return_value=time.time() + 2 * HOUR),
            patch("homeassistant.helpers.storage.write_utf8_file_atomic", side_effect=WriteError("disk full")),
        ):
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"
            assert manager.status()["persist_pending"] is True

        assert _on_disk(config_dir)["data"]["credentials"]["refresh_token"] == REFRESH
        assert await manager.async_get_access_token() == "at_NEWACCESS00001"
        assert aioclient_mock.call_count == 1  # the rotated token was never replayed
        assert _on_disk(config_dir)["data"]["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"
        manager.async_cancel_runtime_tasks()

    async def test_swallowed_write_at_sign_in_reports_save_failed(self, hass, config_dir, aioclient_mock):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
        aioclient_mock.post(REVOKE_URL, text="")
        with (
            patch("homeassistant.helpers.storage.write_utf8_file_atomic", side_effect=WriteError("disk full")),
            pytest.raises(AccountLoginError) as exc,
        ):
            await manager.async_complete_login(_callback(url), OWNER)
        assert exc.value.reason == "save_failed"
        assert manager.state == STATE_DISCONNECTED
        assert REFRESH not in _store_file(config_dir).read_text(encoding="utf-8")
        # The real read-back proved the tokens are not in the store, so they are revoked.
        revokes = [call for call in aioclient_mock.mock_calls if str(call[1]) == REVOKE_URL]
        assert [call[2] for call in revokes] == [
            {"token": REFRESH, "token_type_hint": "refresh_token", "client_id": CLIENT_ID}
        ]

    async def test_write_that_landed_with_failed_readback_is_not_revoked(self, hass, config_dir, aioclient_mock):
        manager = await _sign_in_landed_but_unproved(hass, aioclient_mock)
        assert REFRESH in _store_file(config_dir).read_text(encoding="utf-8")  # it did land
        assert _revokes(aioclient_mock) == []
        assert manager.status()["state"] == chatgpt_account.STATE_SAVE_UNCERTAIN

    async def test_retry_after_a_landed_write_adopts_it_and_overwrites_nothing(self, hass, config_dir, aioclient_mock):
        manager = await _sign_in_landed_but_unproved(hass, aioclient_mock)
        on_disk = _store_file(config_dir).read_text(encoding="utf-8")
        with pytest.raises(AccountLoginError) as exc:
            await asyncio.wait_for(manager.async_begin_login(), 5)
        assert exc.value.reason == "already_connected"
        assert manager.status()["state"] == STATE_CONNECTED
        assert manager.owner_user_id == OWNER
        assert await asyncio.wait_for(manager.async_get_access_token(), 5) == ACCESS
        assert _store_file(config_dir).read_text(encoding="utf-8") == on_disk
        assert _revokes(aioclient_mock) == []
        manager.async_cancel_runtime_tasks()

    async def test_retry_with_an_unreadable_store_is_refused(self, hass, config_dir, aioclient_mock):
        manager = await _sign_in_landed_but_unproved(hass, aioclient_mock)
        on_disk = _store_file(config_dir).read_text(encoding="utf-8")
        with (
            patch("homeassistant.helpers.storage.json_util.load_json", side_effect=HomeAssistantError("read failed")),
            pytest.raises(AccountLoginError) as exc,
        ):
            await asyncio.wait_for(manager.async_begin_login(), 5)
        assert exc.value.reason == "save_failed"
        assert manager.has_pending_login is False
        assert _store_file(config_dir).read_text(encoding="utf-8") == on_disk
        assert _revokes(aioclient_mock) == []

    async def test_restart_after_a_landed_write_is_connected_with_the_stored_owner(
        self, hass, config_dir, aioclient_mock
    ):
        first = await _sign_in_landed_but_unproved(hass, aioclient_mock)
        _restart(hass)
        second = await asyncio.wait_for(async_get_account_manager(hass), 5)
        assert second is not first
        assert second.status()["state"] == STATE_CONNECTED
        assert second.owner_user_id == OWNER
        assert await asyncio.wait_for(second.async_get_access_token(), 5) == ACCESS
        assert _revokes(aioclient_mock) == []
        second.async_cancel_runtime_tasks()

    async def test_disconnect_after_a_landed_write_revokes_and_clears_the_file(self, hass, config_dir, aioclient_mock):
        manager = await _sign_in_landed_but_unproved(hass, aioclient_mock)
        assert await asyncio.wait_for(manager.async_disconnect(), 5) == REVOCATION_CONFIRMED
        assert [call[2]["token"] for call in _revokes(aioclient_mock)] == [REFRESH]
        assert REFRESH not in _store_file(config_dir).read_text(encoding="utf-8")
        assert manager.status()["state"] == STATE_DISCONNECTED

    async def test_write_that_did_not_land_is_revoked_on_retry(self, hass, config_dir, aioclient_mock):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
        aioclient_mock.post(REVOKE_URL, text="")
        real_load = manager._store.async_load
        manager._store.async_load = AsyncMock(side_effect=OSError("read failed"))
        with (
            patch("homeassistant.helpers.storage.write_utf8_file_atomic", side_effect=WriteError("disk full")),
            pytest.raises(AccountLoginError) as exc,
        ):
            await asyncio.wait_for(manager.async_complete_login(_callback(url), OWNER), 5)
        manager._store.async_load = real_load
        assert exc.value.reason == "save_failed"
        assert manager.status()["state"] == chatgpt_account.STATE_SAVE_UNCERTAIN
        assert await asyncio.wait_for(manager.async_begin_login(), 5)
        assert manager.status()["state"] == STATE_DISCONNECTED
        assert [call[2]["token"] for call in _revokes(aioclient_mock)] == [REFRESH]

    async def test_unreadable_store_is_not_overwritten(self, hass, config_dir):
        path = _store_file(config_dir)
        path.parent.mkdir(parents=True)
        original = json.dumps({"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": {"host_id": "urn:uuid:k"}})
        path.write_text(original, encoding="utf-8")
        with patch("homeassistant.helpers.storage.json_util.load_json", side_effect=HomeAssistantError("read failed")):
            manager = await async_get_account_manager(hass)
            assert manager.state == STATE_DISCONNECTED
        assert path.read_text(encoding="utf-8") == original
        assert manager.host_id is None
        await manager.async_load()
        assert manager.host_id == "urn:uuid:k"
        manager.async_cancel_runtime_tasks()

    async def test_final_removal_deletes_file_and_recreates(self, hass, config_dir, aioclient_mock):
        await _connected(hass, aioclient_mock)
        _serve_auth(aioclient_mock)
        aioclient_mock.post(REVOKE_URL, text="")
        assert await async_remove_account(hass) == REVOCATION_CONFIRMED
        assert not _store_file(config_dir).exists()
        assert CHATGPT_ACCOUNT_HASS_KEY not in hass.data
        posted = aioclient_mock.mock_calls[-1][2]
        assert posted == {"token": REFRESH, "token_type_hint": "refresh_token", "client_id": CLIENT_ID}

        again = await async_get_account_manager(hass)
        assert again.state == STATE_DISCONNECTED
        again.async_cancel_runtime_tasks()


class TestRealRemovalThatCannotDelete:
    async def test_failed_delete_blanks_the_file_and_a_new_setup_is_disconnected(
        self, hass, config_dir, aioclient_mock
    ):
        manager = await _connected(hass, aioclient_mock)
        _serve_auth(aioclient_mock)
        aioclient_mock.post(REVOKE_URL, status=500)
        manager._store.async_remove = AsyncMock(side_effect=OSError("delete failed"))
        with patch.object(chatgpt_account, "_REVOKE_BACKOFF_SECONDS", (0, 0)):
            await asyncio.wait_for(async_remove_account(hass), 5)
        raw = _store_file(config_dir).read_text(encoding="utf-8")
        assert REFRESH not in raw and ACCESS not in raw
        assert stat.S_IMODE(os.stat(_store_file(config_dir)).st_mode) == 0o600

        new = await asyncio.wait_for(async_get_account_manager(hass), 5)
        assert new is not manager
        assert new.status()["state"] == STATE_DISCONNECTED
        assert new.host_id is None  # a new one on the next sign-in, never the removed one
        new.async_cancel_runtime_tasks()

    async def test_delete_and_overwrite_both_failing_leaves_the_record_and_says_so(
        self, hass, config_dir, aioclient_mock, caplog
    ):
        """No durable way to record anything: the next setup reads the old account."""
        manager = await _connected(hass, aioclient_mock)
        _serve_auth(aioclient_mock)
        aioclient_mock.post(REVOKE_URL, text="")
        manager._store.async_remove = AsyncMock(side_effect=OSError("delete failed"))
        with patch("homeassistant.helpers.storage.write_utf8_file_atomic", side_effect=WriteError("disk full")):
            assert await asyncio.wait_for(async_remove_account(hass), 5) == REVOCATION_CONFIRMED
        assert CHATGPT_ACCOUNT_HASS_KEY not in hass.data
        assert REFRESH in _store_file(config_dir).read_text(encoding="utf-8")
        assert STORAGE_KEY in caplog.text and "by hand" in caplog.text
        assert REFRESH not in caplog.text and ACCESS not in caplog.text


class TestRealIssueRegistry:
    async def test_terminal_refresh_raises_issue_and_sign_in_clears_it(self, hass, config_dir, aioclient_mock):
        manager = await _connected(hass, aioclient_mock)
        host_id = manager.host_id
        aioclient_mock.post(chatgpt_account.TOKEN_URL, status=400, json={"error": "refresh_token_reused"})
        with (
            patch.object(chatgpt_account, "_now", return_value=time.time() + 2 * HOUR),
            pytest.raises(AccountReauthRequired) as exc,
        ):
            await manager.async_get_access_token()
        assert exc.value.terminal is True
        assert manager.state == STATE_REAUTH_REQUIRED

        issue = ir.async_get(hass).async_get_issue(DOMAIN, REAUTH_ISSUE_ID)
        assert issue is not None
        assert issue.translation_key == REAUTH_ISSUE_ID
        assert issue.is_fixable is False

        url = await manager.async_begin_login()
        assert _query(url)["client_id"] == CLIENT_ID
        _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
        status = await manager.async_complete_login(_callback(url, client_id=None), OWNER)
        assert status["state"] == STATE_CONNECTED
        assert manager.host_id == host_id
        assert ir.async_get(hass).async_get_issue(DOMAIN, REAUTH_ISSUE_ID) is None
        manager.async_cancel_runtime_tasks()


class TestRealTimer:
    async def test_tick_refreshes_only_after_a_week_and_cancel_stops_it(self, hass, config_dir, aioclient_mock):
        manager = await _connected(hass, aioclient_mock)
        aioclient_mock.post(chatgpt_account.TOKEN_URL, json=_token_ok())
        now = datetime.now(UTC)

        async_fire_time_changed(hass, now + chatgpt_account.KEEPALIVE_TICK + timedelta(seconds=1))
        await hass.async_block_till_done()
        assert aioclient_mock.call_count == 0

        with patch.object(
            chatgpt_account, "_now", return_value=time.time() + chatgpt_account.KEEPALIVE_AFTER_SECONDS + 1
        ):
            async_fire_time_changed(hass, now + 2 * chatgpt_account.KEEPALIVE_TICK + timedelta(seconds=2))
            await hass.async_block_till_done(wait_background_tasks=True)
        assert aioclient_mock.call_count == 1

        manager.async_cancel_runtime_tasks()
        with patch.object(
            chatgpt_account, "_now", return_value=time.time() + 3 * chatgpt_account.KEEPALIVE_AFTER_SECONDS
        ):
            async_fire_time_changed(hass, now + 3 * chatgpt_account.KEEPALIVE_TICK + timedelta(seconds=3))
            await hass.async_block_till_done(wait_background_tasks=True)
        assert aioclient_mock.call_count == 1


async def _attempt(hass, aioclient_mock, make_token, *, jwks=None, config_dir=None, **body_overrides):
    """One sign-in whose ID token comes from ``make_token(nonce)``. Returns the manager and error."""
    manager = await async_get_account_manager(hass)
    url = await manager.async_begin_login()
    _serve_auth(
        aioclient_mock,
        _exchange_body(make_token(_query(url)["nonce"]), **body_overrides),
        jwks=jwks,
    )
    with pytest.raises(AccountLoginError) as exc:
        await manager.async_complete_login(_callback(url), OWNER)
    return manager, exc.value


class TestRealIdTokenVerification:
    async def test_valid_token_connects_with_verified_identity(self, hass, config_dir, aioclient_mock):
        manager = await _connected(hass, aioclient_mock)
        assert manager.status()["email"] == EMAIL
        assert _on_disk(config_dir)["data"]["credentials"]["subject"] == SUBJECT
        manager.async_cancel_runtime_tasks()

    async def test_requests_go_through_the_real_session_with_a_form(self, hass, config_dir, aioclient_mock):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"])))
        verifier = manager._pending_login.verifier
        await manager.async_complete_login(_callback(url), OWNER)
        methods_urls = [(c[0].lower(), str(c[1])) for c in aioclient_mock.mock_calls]
        assert methods_urls == [
            ("post", chatgpt_account.TOKEN_URL),
            ("get", chatgpt_account.DISCOVERY_URL),
            ("get", JWKS_URI),
        ]
        assert aioclient_mock.mock_calls[0][2] == {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": CODE,
            "code_verifier": verifier,
            "redirect_uri": chatgpt_account.LOGIN_REDIRECT_URI,
            "resource": "https://api.openai.com/v1",
        }
        manager.async_cancel_runtime_tasks()

    @pytest.mark.parametrize(
        ("make_token", "reason"),
        [
            pytest.param(lambda n: _id_token(n, key=_OTHER_RSA_KEY), "login_checks_failed", id="wrong_key"),
            pytest.param(lambda n: _id_token(n, kid="unknown"), "login_checks_failed", id="unknown_kid"),
            pytest.param(lambda n: _id_token(n, iss="https://evil.test"), "login_checks_failed", id="issuer"),
            pytest.param(lambda n: _id_token(n, aud="oaiapp_OTHER"), "login_checks_failed", id="audience"),
            pytest.param(lambda n: _id_token(n, exp=time.time() - HOUR), "login_checks_failed", id="expired"),
            pytest.param(lambda n: _id_token(n, exp=None), "login_checks_failed", id="no_exp"),
            pytest.param(lambda n: _id_token(n, iat=None), "login_checks_failed", id="no_iat"),
            pytest.param(lambda n: _id_token(n, aud=None), "login_checks_failed", id="no_aud"),
            pytest.param(lambda n: _id_token("forged"), "login_checks_failed", id="forged_nonce"),
            pytest.param(lambda n: _id_token(n, nonce=None), "login_checks_failed", id="no_nonce"),
            pytest.param(lambda n: _id_token(n, sub=None), "bad_identity", id="no_sub"),
            pytest.param(lambda n: _id_token(n, sub=""), "bad_identity", id="empty_sub"),
            pytest.param(lambda n: "a.!!!.c", "login_checks_failed", id="garbage_jwt"),
        ],
    )
    async def test_bad_id_tokens_are_refused(self, hass, config_dir, aioclient_mock, make_token, reason):
        manager, err = await _attempt(hass, aioclient_mock, make_token)
        assert err.reason == reason
        assert manager.state == STATE_DISCONNECTED
        assert manager.has_pending_login is False
        assert REFRESH not in _store_file(config_dir).read_text(encoding="utf-8")

    async def test_tampered_payload_fails_signature(self, hass, config_dir, aioclient_mock):
        def tamper(nonce):
            header, _payload, signature = _id_token(nonce).split(".")
            other_payload = _id_token(nonce, sub="attacker").split(".")[1]
            return f"{header}.{other_payload}.{signature}"

        _manager, err = await _attempt(hass, aioclient_mock, tamper)
        assert err.reason == "login_checks_failed"

    async def test_unsigned_token_is_refused(self, hass, config_dir, aioclient_mock):
        def unsigned(nonce):
            claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": SUBJECT, "nonce": nonce, "iat": 1, "exp": 9999999999}
            return jwt.encode(claims, None, algorithm="none", headers={"kid": "k1"})

        _manager, err = await _attempt(hass, aioclient_mock, unsigned)
        assert err.reason == "login_checks_failed"

    async def test_hmac_token_is_refused(self, hass, config_dir, aioclient_mock):
        secret = "a-shared-secret-long-enough-for-hmac-sha256"
        _manager, err = await _attempt(hass, aioclient_mock, lambda n: _id_token(n, key=secret, alg="HS256"))
        assert err.reason == "login_checks_failed"

    async def test_algorithm_that_does_not_fit_the_key_is_refused(self, hass, config_dir, aioclient_mock):
        """PyJWT raises TypeError here, not PyJWTError."""
        ec_key = ec.generate_private_key(ec.SECP256R1())
        _manager, err = await _attempt(hass, aioclient_mock, lambda n: _id_token(n, key=ec_key, alg="ES256"))
        assert err.reason == "login_checks_failed"

    async def test_empty_key_set_is_refused(self, hass, config_dir, aioclient_mock):
        _manager, err = await _attempt(hass, aioclient_mock, _id_token, jwks={"keys": []})
        assert err.reason == "login_checks_failed"

    def test_algorithm_allowlist_has_no_symmetric_or_none(self):
        algs = [a.lower() for a in chatgpt_account._ID_TOKEN_ALGS]
        assert "none" not in algs
        assert not any(a.startswith("hs") for a in algs)

    async def test_missing_scope_is_refused_despite_a_valid_id_token(self, hass, config_dir, aioclient_mock):
        manager, err = await _attempt(hass, aioclient_mock, _id_token, scope="openid profile")
        assert err.reason == "missing_scope"
        assert manager.state == STATE_DISCONNECTED

    async def test_jwt_access_token_for_another_client_is_refused(self, hass, config_dir, aioclient_mock):
        access = jwt.encode({"client_id": "oaiapp_OTHER"}, _RSA_KEY, algorithm="RS256")
        _manager, err = await _attempt(hass, aioclient_mock, _id_token, access_token=access)
        assert err.reason == "login_checks_failed"

    async def test_malformed_response_under_the_voluptuous_ha_ships(self, hass, config_dir, aioclient_mock):
        _manager, err = await _attempt(hass, aioclient_mock, _id_token, refresh_token="")
        assert err.reason == "login_checks_failed"
        assert err.__suppress_context__ is True

    async def test_opaque_access_token_is_fine(self, hass, config_dir, aioclient_mock):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        _serve_auth(aioclient_mock, _exchange_body(_id_token(_query(url)["nonce"]), access_token="opaque-token"))
        await manager.async_complete_login(_callback(url), OWNER)
        assert await manager.async_get_access_token() == "opaque-token"
        manager.async_cancel_runtime_tasks()

    async def test_clock_tolerance_is_small(self, hass, config_dir, aioclient_mock):
        manager = await async_get_account_manager(hass)
        url = await manager.async_begin_login()
        token = _id_token(_query(url)["nonce"], exp=time.time() - 2)
        _serve_auth(aioclient_mock, _exchange_body(token))
        await manager.async_complete_login(_callback(url), OWNER)
        assert manager.state == STATE_CONNECTED
        manager.async_cancel_runtime_tasks()

    async def test_no_code_or_token_reaches_logs_or_errors(self, hass, config_dir, aioclient_mock, caplog):
        caplog.set_level(logging.DEBUG, logger="custom_components.climate_advisor")
        caplog.set_level(logging.DEBUG, logger="homeassistant.helpers.storage")
        tokens: list[str] = []

        def bad(nonce):
            tokens.append(_id_token(nonce, aud="oaiapp_OTHER"))
            return tokens[-1]

        manager, err = await _attempt(hass, aioclient_mock, bad)
        pasted = f"http://evil.test/?code={CODE}"
        url = await manager.async_begin_login()
        with pytest.raises(AccountLoginError) as bad_url:
            await manager.async_complete_login(pasted, OWNER)
        with pytest.raises(AccountLoginError) as mismatch:
            await manager.async_complete_login(_callback(url, state="forged-state"), OWNER)

        shown = f"{caplog.text} {err} {err!r} {bad_url.value} {mismatch.value} {manager!r} {manager.status()}"
        for secret in (CODE, ACCESS, REFRESH, tokens[0], "forged-state", _query(url)["state"]):
            assert secret not in shown
        for raised in (err, bad_url.value, mismatch.value):
            assert raised.__cause__ is None
