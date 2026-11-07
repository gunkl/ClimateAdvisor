"""Tests for the shared ChatGPT account manager, including pasted-URL sign-in.

All tokens are fake and all HTTP is mocked. ``FakeStore`` mirrors the real
``homeassistant.helpers.storage.Store``, including the behaviour that matters most
here: a failed write is logged and swallowed, so ``async_save`` returning proves nothing.

PyJWT is not in this tier's environment and a skip for a missing dependency counts as a
failure here, so ``_read_claims`` and ``_verify_id_token`` are replaced and everything that
needs the real PyJWT, Store or issue registry is in ``tests_ha/test_chatgpt_account_real_ha.py``.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import sys
import threading
import types
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor import chatgpt_account  # noqa: E402
from custom_components.climate_advisor.ai_backend import AIBackendError  # noqa: E402
from custom_components.climate_advisor.chatgpt_account import (  # noqa: E402
    CHATGPT_ACCOUNT_HASS_KEY,
    REVOCATION_CONFIRMED,
    REVOCATION_NOT_NEEDED,
    REVOCATION_UNCONFIRMED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_REAUTH_REQUIRED,
    AccountLoginError,
    ChatGPTAccountManager,
    async_get_account_manager,
    async_remove_account,
    get_account_manager,
)
from custom_components.climate_advisor.const import DOMAIN  # noqa: E402

T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
CLIENT_ID = "oaiapp_FAKECLIENT0001"
SUBJECT = "user-FAKESUBJECT0001"
EMAIL = "owner@example.test"
ACCESS = "at_FAKEACCESS00000"
REFRESH = "rt_FAKEREFRESH0000"
OWNER = "ha-user-1"
ISSUER = "https://auth.openai.com"
DIRECT = "chatgpt.tokens.use.direct"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
REVOKE_URL = "https://auth.openai.com/api/accounts/oauth/revoke"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
HOUR = 3600
DAY = 86400
_DROP = object()


def _jwt(claims: dict) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.ZmFrZXNpZw"


def _id_token(**overrides) -> str:
    claims = {"iss": ISSUER, "aud": [CLIENT_ID], "sub": SUBJECT, "email": EMAIL}
    claims.update(overrides)
    return _jwt(claims)


def _exchange_ok(**overrides) -> tuple[int, dict]:
    """The token endpoint's answer to a sign-in."""
    body = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "id_token": _id_token(),
        "token_type": "Bearer",
        "expires_in": HOUR,
        "scope": f"openid offline_access {DIRECT}",
        "earliest_refresh_at": None,
    }
    for key, value in overrides.items():
        if value is _DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return 200, body


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _callback(authorize_url: str, **overrides) -> str:
    """The dead 127.0.0.1 address the browser ends on, built from what was authorized."""
    params = {"code": "fake-auth-code", "state": _query(authorize_url)["state"], "client_id": CLIENT_ID}
    for key, value in overrides.items():
        if value is _DROP:
            params.pop(key, None)
        else:
            params[key] = value
    return f"{chatgpt_account.LOGIN_REDIRECT_URI}?{urllib.parse.urlencode(params)}"


class Disk:
    """What is really on disk, plus failure switches."""

    def __init__(self) -> None:
        self.data = None
        self.events: list[str] = []
        self.save_raises = False
        self.write_swallowed = False
        self.load_raises = False
        self.remove_raises = False
        self.load_count = 0
        self.instances: list[FakeStore] = []


class FakeStore:
    """Shaped like HA's Store: write failures are logged and swallowed, not raised."""

    disk: Disk

    def __init__(self, hass, version, key, private=False, *, atomic_writes=False):
        self.args = {"version": version, "key": key, "private": private, "atomic_writes": atomic_writes}
        self.disk.instances.append(self)

    async def async_load(self):
        self.disk.load_count += 1
        await asyncio.sleep(0)
        if self.disk.load_raises:
            raise OSError("disk read failed")
        return copy.deepcopy(self.disk.data)

    async def async_save(self, data):
        await asyncio.sleep(0)
        if self.disk.save_raises:
            raise OSError("disk full")
        if self.disk.write_swallowed:
            return
        self.disk.data = json.loads(json.dumps(data))
        self.disk.events.append("save")

    def async_delay_save(self, *_args, **_kwargs):
        raise AssertionError("credentials must never use a delayed save")

    async def async_remove(self):
        if self.disk.remove_raises:
            raise OSError("disk remove failed")
        self.disk.data = None
        self.disk.events.append("remove")


class FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self, **_kwargs):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class FakeClientError(Exception):
    """Stands in for aiohttp.ClientError, a MagicMock in the stub environment."""


class FakeSession:
    """Scripted auth server. A POST pops from ``token_responses`` (or revoke list)."""

    def __init__(self, disk: Disk) -> None:
        self.disk = disk
        self.token_responses: list = []
        self.revoke_responses: list = [(200, "")]
        self.discovery_doc: dict = {"issuer": ISSUER, "revocation_endpoint": REVOKE_URL, "jwks_uri": JWKS_URL}
        self.jwks_response: tuple = (200, {"keys": [{"kid": "k1"}]})
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.gate: asyncio.Event | None = None
        self.kwargs_seen: list[dict] = []

    def _answer(self, item):
        if isinstance(item, Exception):
            raise item
        return FakeResponse(*item)

    def get(self, url, **kwargs):
        self.gets.append(url)
        self.kwargs_seen.append(kwargs)
        if url == JWKS_URL:
            return _Deferred(lambda: self._answer(self.jwks_response), None)
        return _Deferred(lambda: FakeResponse(200, self.discovery_doc), None)

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, dict(data or {})))
        self.kwargs_seen.append(kwargs)
        if url == TOKEN_URL:
            self.disk.events.append("refresh_post")
            return _Deferred(lambda: self._answer(self.token_responses.pop(0)), self.gate)
        self.disk.events.append("revoke_post")
        return _Deferred(lambda: self._answer(self.revoke_responses.pop(0)), None)

    def refreshes(self) -> list[dict]:
        return [form for url, form in self.posts if url == TOKEN_URL]

    def revokes(self) -> list[dict]:
        return [form for url, form in self.posts if url == REVOKE_URL]


class _Deferred:
    """Async context manager that can hold the response behind a gate."""

    def __init__(self, make, gate):
        self._make = make
        self._gate = gate
        self._response = None

    async def __aenter__(self):
        await asyncio.sleep(0)
        if self._gate is not None:
            await self._gate.wait()
        self._response = self._make()
        return self._response

    async def __aexit__(self, *_args):
        return False


class FakeHass:
    def __init__(self, config_dir: Path) -> None:
        self.data: dict = {}
        self.config = types.SimpleNamespace(path=lambda *parts: str(Path(config_dir, *parts)))
        self.executor_threads: list[int] = []
        self.tasks: list[asyncio.Task] = []

    def async_add_executor_job(self, target, *args):
        def run():
            self.executor_threads.append(threading.get_ident())
            return target(*args)

        return asyncio.get_running_loop().run_in_executor(None, run)

    def async_create_task(self, coro, name=None, eager_start=True):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    def async_create_background_task(self, coro, name, eager_start=True):
        return self.async_create_task(coro, name)


class FakeTimers:
    """Stands in for async_track_time_interval; ``fire`` runs one tick."""

    def __init__(self) -> None:
        self.active: list[dict] = []

    def __call__(self, hass, action, interval, *, name=None, cancel_on_shutdown=None):
        timer = {"action": action, "interval": interval, "cancel_on_shutdown": cancel_on_shutdown}
        self.active.append(timer)

        def unsub():
            if timer in self.active:
                self.active.remove(timer)

        return unsub

    def only(self) -> dict:
        assert len(self.active) == 1, f"expected one timer, have {len(self.active)}"
        return self.active[0]

    def fire(self) -> None:
        self.only()["action"](T0)


def _read_claims_without_pyjwt(token: str) -> dict | None:
    """Unit-tier stand-in for chatgpt_account._read_claims (real PyJWT: see the real-HA tier)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    return claims if isinstance(claims, dict) else None


async def _settle() -> None:
    await asyncio.sleep(0.01)


class Env:
    def __init__(self, tmp_path, monkeypatch, frozen_now) -> None:
        self.disk = Disk()
        self.session = FakeSession(self.disk)
        self.timers = FakeTimers()
        self.issues = MagicMock()
        self.tmp_path = tmp_path
        self.hass = FakeHass(tmp_path)
        self._frozen_now = frozen_now
        self.clock(0)
        store_class = type("BoundFakeStore", (FakeStore,), {"disk": self.disk})
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", types.SimpleNamespace(Store=store_class))
        monkeypatch.setitem(
            sys.modules,
            "homeassistant.helpers.aiohttp_client",
            types.SimpleNamespace(async_get_clientsession=lambda _hass: self.session),
        )
        monkeypatch.setattr(chatgpt_account, "async_track_time_interval", self.timers)
        monkeypatch.setattr(chatgpt_account, "_read_claims", _read_claims_without_pyjwt)
        self.verified: list[tuple] = []
        self.verify_claims: dict | Exception = {"sub": SUBJECT, "email": EMAIL}
        monkeypatch.setattr(chatgpt_account, "_verify_id_token", self._verify)
        monkeypatch.setattr(chatgpt_account, "ir", self.issues)
        monkeypatch.setattr(chatgpt_account, "ClientError", FakeClientError)
        monkeypatch.setattr(chatgpt_account, "ClientTimeout", lambda **kw: kw)
        monkeypatch.setattr(chatgpt_account, "_REVOKE_BACKOFF_SECONDS", (0, 0))

    def clock(self, offset_seconds: float) -> float:
        when = T0 + timedelta(seconds=offset_seconds)
        self._frozen_now(when)
        return when.timestamp()

    def restart(self) -> FakeHass:
        """A new HA process: new hass and no memory, same disk."""
        self.hass = FakeHass(self.tmp_path)
        self.timers.active.clear()
        return self.hass

    def _verify(self, id_token, jwks, audience, nonce):
        """Real signature, audience, issuer and nonce checks: tests_ha/test_chatgpt_account_real_ha.py."""
        self.verified.append((id_token, jwks, audience, nonce, threading.get_ident()))
        if isinstance(self.verify_claims, Exception):
            raise self.verify_claims
        return self.verify_claims

    def forget_network(self) -> None:
        self.session.posts.clear()
        self.session.gets.clear()
        self.session.kwargs_seen.clear()
        self.session.token_responses.clear()
        self.disk.events.clear()

    async def login(self, manager: ChatGPTAccountManager, owner: str = OWNER, **token_overrides) -> dict:
        """A complete, successful sign-in. Leaves the fake network's logs empty for the test."""
        url = await manager.async_begin_login()
        self.session.token_responses = [_exchange_ok(**token_overrides)]
        status = await manager.async_complete_login(_callback(url), owner)
        self.forget_network()
        return status

    async def connected(self) -> ChatGPTAccountManager:
        manager = await async_get_account_manager(self.hass)
        await self.login(manager)
        return manager


@pytest.fixture
def env(tmp_path, monkeypatch, frozen_now) -> Env:
    return Env(tmp_path, monkeypatch, frozen_now)


def _token_ok(n: int = 1, **overrides) -> tuple[int, dict]:
    body = {
        "access_token": f"at_NEWACCESS{n:05d}",
        "refresh_token": f"rt_NEWREFRESH{n:05d}",
        "token_type": "Bearer",
        "expires_in": HOUR,
        "scope": f"openid offline_access {DIRECT}",
        "earliest_refresh_at": None,
    }
    for key, value in overrides.items():
        if value is _DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return 200, body


class TestPlacementAndStore:
    def test_get_or_create_returns_one_instance_and_loads_once(self, env):
        env.disk.data = {"host_id": "urn:uuid:existing"}  # no first-run save, whose read-back also loads

        async def run():
            return await asyncio.gather(*(async_get_account_manager(env.hass) for _ in range(5)))

        managers = asyncio.run(run())
        assert all(m is managers[0] for m in managers)
        assert env.disk.load_count == 1
        assert len(env.disk.instances) == 1

    def test_load_failure_never_raises_and_never_overwrites(self, env):
        env.disk.data = {"host_id": "urn:uuid:keep", "state": STATE_DISCONNECTED}
        env.disk.load_raises = True

        async def run():
            manager = await async_get_account_manager(env.hass)
            assert manager.state == STATE_DISCONNECTED
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"
            with pytest.raises(AccountLoginError) as login:
                await manager.async_begin_login()
            assert login.value.reason == "save_failed"

        asyncio.run(run())
        assert env.disk.data == {"host_id": "urn:uuid:keep", "state": STATE_DISCONNECTED}

    def test_load_recovers_when_storage_comes_back(self, env):
        env.disk.load_raises = True

        async def run():
            manager = await async_get_account_manager(env.hass)
            env.disk.load_raises = False
            await env.login(manager)
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())

    def test_only_documented_hosts_are_contacted(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token()
            await manager.async_disconnect()

        asyncio.run(run())
        assert {url for url, _ in env.session.posts} == {TOKEN_URL, REVOKE_URL}
        assert env.session.gets == [DISCOVERY_URL]
        assert all(kw["allow_redirects"] is False for kw in env.session.kwargs_seen)

    def test_never_targets_backend_api(self):
        assert "backend-api" not in Path(chatgpt_account.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "url",
        [
            "https://chatgpt.com/backend-api/codex",
            "http://auth.openai.com/token",
            "https://auth.openai.com.evil.test/token",
            "https://api.openai.com/v1/models",
        ],
    )
    def test_url_guard_refuses(self, url):
        with pytest.raises(AIBackendError):
            chatgpt_account._check_auth_url(url)


class TestAccessToken:
    def test_not_connected_is_auth_error_without_network(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "auth"

        asyncio.run(run())
        assert env.session.posts == []

    def test_valid_token_needs_no_network(self, env):
        async def run():
            manager = await env.connected()
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())
        assert env.session.posts == []

    def test_refresh_form_matches_docs(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token()

        asyncio.run(run())
        assert env.session.refreshes() == [
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": REFRESH,
                "resource": "https://api.openai.com/v1",
            }
        ]

    def test_concurrent_zones_produce_one_network_refresh(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            env.session.gate = asyncio.Event()
            callers = [asyncio.create_task(manager.async_get_access_token()) for _ in range(6)]
            await asyncio.sleep(0.01)
            env.session.gate.set()
            return await asyncio.gather(*callers)

        tokens = asyncio.run(run())
        assert tokens == ["at_NEWACCESS00001"] * 6
        assert len(env.session.refreshes()) == 1

    def test_force_refresh_from_many_zones_refreshes_once(self, env):
        """Several zones hitting 401 on the same token must not burn a rotation each."""

        async def run():
            manager = await env.connected()
            env.session.token_responses = [_token_ok()]
            return await asyncio.gather(*(manager.async_get_access_token(force_refresh=True) for _ in range(4)))

        assert asyncio.run(run()) == ["at_NEWACCESS00001"] * 4
        assert len(env.session.refreshes()) == 1

    def test_later_force_refresh_is_a_real_refresh(self, env):
        async def run():
            manager = await env.connected()
            env.session.token_responses = [_token_ok(1), _token_ok(2)]
            await manager.async_get_access_token(force_refresh=True)
            return await manager.async_get_access_token(force_refresh=True)

        assert asyncio.run(run()) == "at_NEWACCESS00002"
        assert len(env.session.refreshes()) == 2

    def test_force_refresh_honours_the_floor(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await env.login(manager, earliest_refresh_at=int(T0.timestamp()) + 600)
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token(force_refresh=True)
            assert exc.value.category == "transient"
            assert getattr(exc.value, "terminal", False) is False

        asyncio.run(run())
        assert env.session.posts == []

    def test_caller_cancelled_mid_refresh_still_persists_rotation(self, env):
        """A dropped investigate request must not lose a token the server already rotated."""

        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            env.session.gate = asyncio.Event()
            caller = asyncio.create_task(manager.async_get_access_token())
            await asyncio.sleep(0.01)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # A new caller queues behind the in-flight refresh instead of replaying the old token.
            second = asyncio.create_task(manager.async_get_access_token())
            await asyncio.sleep(0.01)
            env.session.gate.set()
            return await second

        assert asyncio.run(run()) == "at_NEWACCESS00001"
        assert len(env.session.refreshes()) == 1
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"

    def test_token_response_without_replacement_keeps_old_refresh_token(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok(refresh_token=_DROP)]
            return await manager.async_get_access_token()

        assert asyncio.run(run()) == "at_NEWACCESS00001"
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH

    def test_replacement_without_access_token_is_still_persisted(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok(access_token=_DROP)]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"

    def test_lost_direct_scope_requires_reauth_and_releases_nothing(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok(scope="openid")]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "auth"
            assert manager.state == STATE_REAUTH_REQUIRED
            # Tokens stay so a disconnect can still revoke them.
            assert await manager.async_disconnect() == REVOCATION_CONFIRMED

        asyncio.run(run())


class TestOutcomeLogging:
    def _msgs(self, caplog, level):
        return [r.getMessage() for r in caplog.records if r.levelno == level]

    def test_sign_in_disconnect_refresh_lines_without_secrets(self, env, caplog):
        caplog.set_level(logging.DEBUG)

        async def run():
            manager = ChatGPTAccountManager(env.hass)
            await manager.async_load()
            await env.login(manager)
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token(force_refresh=True)
            return await manager.async_disconnect()

        outcome = asyncio.run(run())
        info = self._msgs(caplog, logging.INFO)
        assert any("ChatGPT sign-in completed" in m for m in info)
        assert any(f"ChatGPT account disconnected (revocation: {outcome})" == m for m in info)
        assert any("token refresh" in m for m in self._msgs(caplog, logging.DEBUG))
        for planted in (ACCESS, REFRESH, OWNER, "fake-auth-code", "@"):
            assert planted not in caplog.text

    def test_final_removal_logs_outcome(self, env, caplog):
        caplog.set_level(logging.INFO)

        async def run():
            manager = await env.connected()
            return await manager.async_remove()

        outcome = asyncio.run(run())
        assert any(f"ChatGPT account removed (revocation: {outcome})" == r.getMessage() for r in caplog.records)


class TestEarliestRefreshAt:
    @pytest.mark.parametrize(
        ("raw", "expected_offset"),
        [
            (int(T0.timestamp()) + 600, 600),
            (T0.timestamp() + 600.5, 600.5),
            (str(int(T0.timestamp()) + 600), 600),
            ((int(T0.timestamp()) + 600) * 1000, 600),
            ((T0 + timedelta(seconds=600)).isoformat(), 600),
        ],
    )
    def test_absolute_forms(self, raw, expected_offset):
        assert chatgpt_account._parse_earliest(raw) == pytest.approx(T0.timestamp() + expected_offset)

    @pytest.mark.parametrize("raw", [None, 600, "soon", "2026-11-07T12:00:00"])
    def test_ambiguous_forms_impose_no_floor(self, raw):
        assert chatgpt_account._parse_earliest(raw) is None

    def test_still_valid_token_is_served_before_the_floor(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await env.login(manager, earliest_refresh_at=int(T0.timestamp()) + HOUR + 600)
            env.clock(HOUR - 60)  # inside the expiry skew, still valid
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())
        assert env.session.posts == []

    def test_expired_token_before_the_floor_is_transient_and_keeps_credentials(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await env.login(manager, earliest_refresh_at=int(T0.timestamp()) + 2 * HOUR)
            env.clock(HOUR + 60)
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"
            assert manager.state == STATE_CONNECTED

            env.clock(2 * HOUR + 1)
            env.session.token_responses = [_token_ok()]
            assert await manager.async_get_access_token() == "at_NEWACCESS00001"

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1

    def test_floor_from_refresh_response_is_honoured_after_restart(self, env):
        floor = int(T0.timestamp()) + 5 * HOUR

        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok(earliest_refresh_at=floor)]
            await manager.async_get_access_token()

            env.restart()
            env.clock(3 * HOUR + 60)  # new token expired, floor not reached
            restarted = await async_get_account_manager(env.hass)
            with pytest.raises(AIBackendError) as exc:
                await restarted.async_get_access_token()
            assert exc.value.category == "transient"

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1


class TestRestart:
    def test_reload_restores_state_without_refresh(self, env):
        async def run():
            await env.connected()
            env.restart()
            manager = await async_get_account_manager(env.hass)
            assert manager.state == STATE_CONNECTED
            assert manager.owner_user_id == OWNER
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())
        assert env.session.posts == []

    def test_rotated_token_is_the_one_used_after_restart(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok(1)]
            await manager.async_get_access_token()

            env.restart()
            env.clock(4 * HOUR)
            restarted = await async_get_account_manager(env.hass)
            env.session.token_responses = [_token_ok(2)]
            assert await restarted.async_get_access_token() == "at_NEWACCESS00002"

        asyncio.run(run())
        assert env.session.refreshes()[1]["refresh_token"] == "rt_NEWREFRESH00001"

    def test_reauth_state_survives_restart_and_reraises_issue(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [(400, {"error": "invalid_grant"})]
            with pytest.raises(AIBackendError):
                await manager.async_get_access_token()

            env.issues.reset_mock()
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.state == STATE_REAUTH_REQUIRED

        asyncio.run(run())
        env.issues.async_create_issue.assert_called_once()

    def test_connected_on_disk_without_refresh_token_loads_as_reauth(self, env):
        env.disk.data = {"host_id": "urn:uuid:x", "state": STATE_CONNECTED, "credentials": {"client_id": "c"}}
        manager = asyncio.run(async_get_account_manager(env.hass))
        assert manager.state == STATE_REAUTH_REQUIRED


class TestRefreshErrors:
    @pytest.mark.parametrize(
        ("code", "shape"),
        [
            ("invalid_grant", "string"),
            ("invalid_refresh_token", "string"),
            ("token_expired", "string"),
            ("refresh_token_expired", "string"),
            ("refresh_token_invalidated", "string"),
            ("refresh_token_reused", "string"),
            ("invalid_grant", "object"),
            ("invalid_grant", "top_level"),
        ],
    )
    def test_terminal_codes_clear_tokens_and_raise_repair(self, env, code, shape):
        body = {
            "string": {"error": code},
            "object": {"error": {"code": code}},
            "top_level": {"code": code},
        }[shape]

        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [(400, body)]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "auth"
            assert exc.value.terminal is True
            assert manager.state == STATE_REAUTH_REQUIRED
            # No more network and no token now.
            with pytest.raises(AIBackendError) as again:
                await manager.async_get_access_token()
            assert again.value.terminal is True

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1
        stored = env.disk.data
        assert stored["state"] == STATE_REAUTH_REQUIRED
        assert "access_token" not in json.dumps(stored).replace('"access_token": null', "")
        assert REFRESH not in json.dumps(stored)
        assert stored["host_id"]
        env.issues.async_create_issue.assert_called_once()
        args, kwargs = env.issues.async_create_issue.call_args
        assert args[1] == DOMAIN
        assert kwargs["translation_key"] == chatgpt_account.REAUTH_ISSUE_ID
        assert kwargs["is_fixable"] is False

    @pytest.mark.parametrize(
        ("failure", "timeout"),
        [(FakeClientError("connection reset"), False), (TimeoutError(), True), (OSError("unreachable"), False)],
    )
    def test_network_errors_keep_credentials(self, env, failure, timeout):
        async def run():
            manager = await env.connected()
            before = copy.deepcopy(env.disk.data)
            env.clock(2 * HOUR)
            env.session.token_responses = [failure]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"
            assert exc.value.timeout is timeout
            assert exc.value.no_retry is True  # the server may have rotated: never sent again in this request
            assert getattr(exc.value, "terminal", False) is False
            assert manager.state == STATE_CONNECTED
            assert env.disk.data == before

        asyncio.run(run())
        env.issues.async_create_issue.assert_not_called()

    def test_error_while_reading_the_body_keeps_credentials(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [(200, FakeClientError("payload"))]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH

    @pytest.mark.parametrize(
        ("status", "body", "category"),
        [
            (500, "bad gateway", "transient"),
            (503, {"detail": "busy"}, "transient"),
            (429, {"error": "slow_down"}, "rate_limit"),
            (400, {"error": "invalid_client"}, "auth"),
            (401, {"detail": "nope"}, "auth"),
            (400, {"error": "invalid_request"}, "permanent"),
            (200, "not json", "transient"),
            (200, {"unexpected": True}, "transient"),
        ],
    )
    def test_non_terminal_failures_keep_credentials(self, env, status, body, category):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [(status, body)]
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == category
            assert exc.value.no_retry is True
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH
        env.issues.async_create_issue.assert_not_called()


class TestRotationPersistedBeforeRelease:
    def test_saved_before_caller_receives_token(self, env):
        async def run():
            manager = await env.connected()
            env.disk.events.clear()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            token = await manager.async_get_access_token()
            assert env.disk.data["credentials"]["access_token"] == token

        asyncio.run(run())
        assert env.disk.events == ["refresh_post", "save"]

    @pytest.mark.parametrize("failure", ["save_raises", "write_swallowed"])
    def test_failed_save_withholds_token_keeps_rotation_and_retries(self, env, failure):
        """HA's Store swallows write errors, so the swallowed case needs a read-back to see."""

        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            setattr(env.disk, failure, True)

            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "transient"
            assert manager.status()["persist_pending"] is True
            assert env.disk.data["credentials"]["refresh_token"] == REFRESH

            # Still failing: nothing released, and the rotated token is never replayed.
            with pytest.raises(AIBackendError):
                await manager.async_get_access_token()
            assert len(env.session.refreshes()) == 1

            setattr(env.disk, failure, False)
            assert await manager.async_get_access_token() == "at_NEWACCESS00001"
            assert manager.status()["persist_pending"] is False

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"


class TestAccountIdentity:
    def test_none_until_signed_in_then_owner_and_subject_through_rotation(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            assert manager.account_identity is None
            await env.login(manager)
            identity = manager.account_identity
            assert identity[0] == OWNER and identity[1]
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token()
            assert manager.account_identity == identity
            await manager.async_disconnect()
            assert manager.account_identity is None

        asyncio.run(run())


class TestHostId:
    def test_loading_creates_nothing(self, env):
        manager = asyncio.run(async_get_account_manager(env.hass))
        assert manager.host_id is None
        assert env.disk.data is None

    def test_created_and_persisted_when_sign_in_begins(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            return manager, url

        manager, url = asyncio.run(run())
        assert manager.host_id.startswith("urn:uuid:")
        assert env.disk.data["host_id"] == manager.host_id
        assert _query(url)["ext_agent_host_id"] == manager.host_id

    def test_stable_across_restart_connect_rotation_and_disconnect(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            host = manager.host_id
            assert _query(url)["ext_agent_host_id"] == host
            env.session.token_responses = [_exchange_ok(ext_agent_host_id="urn:uuid:other")]
            await manager.async_complete_login(_callback(url, ext_agent_host_id="urn:uuid:other"), OWNER)
            assert manager.host_id == host

            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token()
            assert manager.host_id == host

            await manager.async_disconnect()
            assert manager.host_id == host

            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.host_id == host
            await env.login(restarted)
            return host

        host = asyncio.run(run())
        assert env.disk.data["host_id"] == host


class TestQuotaCooldown:
    def test_no_cooldown_allows_every_request(self, env):
        async def run():
            manager = await env.connected()
            assert [manager.quota_cooldown_active() for _ in range(3)] == [False] * 3

        asyncio.run(run())

    def test_cooldown_stops_every_zone_at_once(self, env):
        async def run():
            manager = await env.connected()
            zone_a, zone_b = manager, get_account_manager(env.hass)
            assert zone_a is zone_b
            zone_a.set_quota_cooldown("subscription_sharing_usage_limit_exceeded")
            # Synchronous, before the save has even run.
            assert zone_b.quota_cooldown_active() is True
            await _settle()
            assert env.disk.data["quota_cooldown_until"] is not None

        asyncio.run(run())

    def test_exactly_one_probe_after_cooldown(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False  # the probe
            assert manager.quota_cooldown_active() is True

            manager.note_request_succeeded()
            assert manager.quota_cooldown_active() is False
            assert manager.quota_cooldown_active() is False
            assert manager.status()["quota_cooldown_active"] is False
            await _settle()
            assert env.disk.data["quota_cooldown_until"] is None

        asyncio.run(run())

    def test_failed_probe_restarts_the_cooldown(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            env.clock(offset)
            assert manager.quota_cooldown_active() is False
            manager.set_quota_cooldown("limit")
            assert manager.quota_cooldown_active() is True
            env.clock(offset + chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            await _settle()

        asyncio.run(run())

    def test_lease_expiry_keeps_the_cooldown_and_grants_one_new_probe(self, env):
        """A probe that failed for a non-quota reason, or is still running, proves nothing."""

        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            for _window in range(3):
                env.clock(offset)
                assert manager.quota_cooldown_active() is False  # this window's only probe
                assert manager.quota_cooldown_active() is True
                offset += chatgpt_account.QUOTA_PROBE_LEASE_SECONDS - 1
                env.clock(offset)
                assert manager.quota_cooldown_active() is True  # still inside the lease
                offset += 2
            await _settle()
            assert env.disk.data["quota_cooldown_until"] is not None

            manager.note_request_succeeded()  # only a success opens the gate
            assert manager.quota_cooldown_active() is False
            assert manager.quota_cooldown_active() is False
            await _settle()
            assert env.disk.data["quota_cooldown_until"] is None

        asyncio.run(run())

    def test_429_on_a_later_probe_restarts_the_cooldown(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            env.clock(offset)
            assert manager.quota_cooldown_active() is False
            offset += chatgpt_account.QUOTA_PROBE_LEASE_SECONDS + 1
            env.clock(offset)
            assert manager.quota_cooldown_active() is False
            manager.set_quota_cooldown("limit")
            env.clock(offset + chatgpt_account.QUOTA_COOLDOWN_SECONDS - 1)
            assert manager.quota_cooldown_active() is True
            env.clock(offset + chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            await _settle()

        asyncio.run(run())

    def test_status_reports_the_gate_without_touching_it(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            when = env.clock(offset)
            assert manager.quota_cooldown_active() is False
            status = manager.status()
            assert status["quota_cooldown_active"] is True
            lease_end = when + chatgpt_account.QUOTA_PROBE_LEASE_SECONDS
            assert status["quota_next_probe_at"] == chatgpt_account._iso(lease_end)

            env.clock(offset + chatgpt_account.QUOTA_PROBE_LEASE_SECONDS + 1)
            assert manager.status()["quota_cooldown_active"] is False  # a request would be let through
            assert manager.status()["quota_next_probe_at"] == chatgpt_account._iso(lease_end)
            assert manager.quota_cooldown_active() is False  # status did not use up the new probe
            assert manager.quota_cooldown_active() is True
            await _settle()

        asyncio.run(run())

    def test_success_of_a_request_older_than_the_cooldown_does_not_clear_it_after_a_probe(self, env):
        """The probe is out, so only the request's own start can tell the two apart."""

        async def run():
            manager = await env.connected()
            before = manager.quota_epoch
            manager.set_quota_cooldown("limit")
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            manager.note_request_succeeded(before)
            assert manager.status()["quota_next_probe_at"] is not None
            assert manager.quota_cooldown_active() is True

            manager.note_request_succeeded(manager.quota_epoch)
            assert manager.quota_cooldown_active() is False
            await _settle()

        asyncio.run(run())

    def test_probe_older_than_a_new_cooldown_does_not_clear_it(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            env.clock(offset)
            assert manager.quota_cooldown_active() is False
            first_probe = manager.quota_epoch
            manager.set_quota_cooldown("limit")
            env.clock(offset + chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            manager.note_request_succeeded(first_probe)
            assert manager.quota_cooldown_active() is True
            await _settle()

        asyncio.run(run())

    def test_restart_after_the_cooldown_grants_one_probe_and_no_free_pass(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            await _settle()
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.quota_cooldown_active() is False
            assert restarted.quota_cooldown_active() is True
            await _settle()
            assert env.disk.data["quota_cooldown_until"] is not None

        asyncio.run(run())

    def test_restart_during_a_lease_does_not_grant_a_second_probe(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            await _settle()
            offset = chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1
            env.clock(offset)
            assert manager.quota_cooldown_active() is False
            await _settle()
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.quota_cooldown_active() is True
            env.clock(offset + chatgpt_account.QUOTA_PROBE_LEASE_SECONDS + 1)
            assert restarted.quota_cooldown_active() is False
            assert restarted.quota_cooldown_active() is True
            await _settle()

        asyncio.run(run())

    def test_store_written_before_leases_were_saved_still_loads(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            await _settle()
            env.disk.data.pop("quota_probe_until")
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.quota_cooldown_active() is False
            assert restarted.quota_cooldown_active() is True
            await _settle()

        asyncio.run(run())

    def test_status_never_consumes_the_probe(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            manager.status()
            assert manager.quota_cooldown_active() is False
            await _settle()

        asyncio.run(run())

    def test_cooldown_survives_restart(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            await _settle()
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.quota_cooldown_active() is True

        asyncio.run(run())

    def test_success_of_an_older_request_does_not_lift_the_cooldown(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            manager.note_request_succeeded()
            assert manager.quota_cooldown_active() is True
            await _settle()

        asyncio.run(run())

    def test_failed_cooldown_save_does_not_raise_and_is_retried(self, env):
        async def run():
            manager = await env.connected()
            env.disk.write_swallowed = True
            manager.set_quota_cooldown("limit")
            await _settle()
            assert manager.status()["persist_pending"] is True
            env.disk.write_swallowed = False
            await manager.async_get_access_token()
            assert env.disk.data["quota_cooldown_until"] is not None

        asyncio.run(run())

    def test_quota_does_not_block_refresh_or_change_state(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            assert await manager.async_get_access_token() == "at_NEWACCESS00001"
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())


class TestKeepalive:
    def test_no_timer_while_disconnected(self, env):
        asyncio.run(async_get_account_manager(env.hass))
        assert env.timers.active == []

    def test_connect_arms_one_cancellable_timer(self, env):
        asyncio.run(env.connected())
        timer = env.timers.only()
        assert timer["interval"] == chatgpt_account.KEEPALIVE_TICK
        assert timer["cancel_on_shutdown"] is True
        assert chatgpt_account.KEEPALIVE_AFTER_SECONDS == 7 * DAY

    def test_tick_before_a_week_does_nothing(self, env):
        async def run():
            await env.connected()
            env.clock(7 * DAY - 1)
            env.timers.fire()
            await _settle()

        asyncio.run(run())
        assert env.session.posts == []

    def test_tick_after_a_week_refreshes_once(self, env):
        async def run():
            manager = await env.connected()
            env.clock(7 * DAY + 1)
            env.session.token_responses = [_token_ok()]
            env.timers.fire()
            await _settle()
            env.timers.fire()
            await _settle()
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"
        assert len(env.timers.active) == 1

    def test_restart_does_not_reset_the_week(self, env):
        """HA restarting more often than weekly must not starve the keepalive."""

        async def run():
            await env.connected()
            for day in range(1, 7):
                env.restart()
                env.clock(day * DAY)
                await async_get_account_manager(env.hass)
            env.restart()
            env.clock(7 * DAY + 1)
            await async_get_account_manager(env.hass)
            env.session.token_responses = [_token_ok()]
            env.timers.fire()
            await _settle()

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1

    def test_floor_defers_the_keepalive(self, env):
        floor = int(T0.timestamp()) + 8 * DAY

        async def run():
            manager = await async_get_account_manager(env.hass)
            await env.login(manager, earliest_refresh_at=floor)
            env.clock(7 * DAY + 1)
            env.timers.fire()
            await _settle()
            assert env.session.posts == []
            env.clock(8 * DAY + 1)
            env.session.token_responses = [_token_ok()]
            env.timers.fire()
            await _settle()

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1

    def test_network_failure_keeps_credentials_and_retries_next_tick(self, env):
        async def run():
            manager = await env.connected()
            env.clock(7 * DAY + 1)
            env.session.token_responses = [FakeClientError("down")]
            env.timers.fire()
            await _settle()
            assert manager.state == STATE_CONNECTED
            env.session.token_responses = [_token_ok()]
            env.timers.fire()
            await _settle()

        asyncio.run(run())
        assert len(env.session.refreshes()) == 2
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"

    def test_terminal_failure_disarms_and_raises_repair(self, env):
        async def run():
            manager = await env.connected()
            env.clock(7 * DAY + 1)
            env.session.token_responses = [(400, {"error": "refresh_token_expired"})]
            env.timers.fire()
            await _settle()
            assert manager.state == STATE_REAUTH_REQUIRED

        asyncio.run(run())
        assert env.timers.active == []
        env.issues.async_create_issue.assert_called_once()

    def test_failed_persist_is_retried_by_the_next_tick(self, env):
        async def run():
            manager = await env.connected()
            env.clock(7 * DAY + 1)
            env.session.token_responses = [_token_ok()]
            env.disk.write_swallowed = True
            env.timers.fire()
            await _settle()
            assert manager.status()["persist_pending"] is True

            env.disk.write_swallowed = False
            env.timers.fire()
            await _settle()
            assert manager.status()["persist_pending"] is False

        asyncio.run(run())
        assert len(env.session.refreshes()) == 1
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"

    def test_disconnect_disarms(self, env):
        async def run():
            manager = await env.connected()
            await manager.async_disconnect()

        asyncio.run(run())
        assert env.timers.active == []


class TestUnloadVersusRemoval:
    def test_cancel_runtime_touches_no_credentials(self, env):
        async def run():
            manager = await env.connected()
            before = copy.deepcopy(env.disk.data)
            env.disk.events.clear()
            manager.async_cancel_runtime_tasks()
            assert env.timers.active == []
            assert env.disk.events == []
            assert env.disk.data == before
            assert manager.state == STATE_CONNECTED
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())
        assert env.session.posts == []

    def test_setup_after_unload_keeps_the_manager_and_rearms(self, env):
        async def run():
            manager = await env.connected()
            manager.async_cancel_runtime_tasks()
            again = await async_get_account_manager(env.hass)
            assert again is manager

        asyncio.run(run())
        assert len(env.timers.active) == 1

    def test_refresh_after_unload_does_not_rearm(self, env):
        async def run():
            manager = await env.connected()
            manager.async_cancel_runtime_tasks()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            await manager.async_get_access_token()

        asyncio.run(run())
        assert env.timers.active == []

    def test_unload_during_keepalive_refresh_still_saves_rotation(self, env):
        async def run():
            manager = await env.connected()
            env.clock(7 * DAY + 1)
            env.session.token_responses = [_token_ok()]
            env.session.gate = asyncio.Event()
            env.timers.fire()
            await _settle()
            manager.async_cancel_runtime_tasks()
            await _settle()
            env.session.gate.set()
            await _settle()

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"
        assert env.timers.active == []

    def test_removal_revokes_then_deletes_store(self, env):
        async def run():
            await env.connected()
            env.disk.events.clear()
            return await async_remove_account(env.hass)

        assert asyncio.run(run()) == REVOCATION_CONFIRMED
        assert env.disk.events == ["revoke_post", "remove"]
        assert env.disk.data is None
        assert CHATGPT_ACCOUNT_HASS_KEY not in env.hass.data
        assert env.timers.active == []
        assert env.session.revokes() == [{"token": REFRESH, "token_type_hint": "refresh_token", "client_id": CLIENT_ID}]
        env.issues.async_delete_issue.assert_called()

    def test_removal_still_deletes_when_revocation_is_unconfirmed(self, env):
        async def run():
            await env.connected()
            env.session.revoke_responses = [FakeClientError("down")] * 3
            return await async_remove_account(env.hass)

        assert asyncio.run(run()) == REVOCATION_UNCONFIRMED
        assert env.disk.data is None

    def test_removal_loads_first_when_entry_never_set_up(self, env):
        async def run():
            await env.connected()
            env.restart()
            return await async_remove_account(env.hass)

        assert asyncio.run(run()) == REVOCATION_CONFIRMED
        assert env.disk.data is None

    def test_stale_reference_cannot_use_tokens_after_removal(self, env):
        async def run():
            stale = await env.connected()
            await async_remove_account(env.hass)
            with pytest.raises(AIBackendError) as exc:
                await stale.async_get_access_token()
            assert exc.value.category == "auth"
            assert env.disk.data is None

        asyncio.run(run())

    def test_later_setup_recreates_manager_and_timer(self, env):
        async def run():
            old = await env.connected()
            await async_remove_account(env.hass)
            new = await async_get_account_manager(env.hass)
            assert new is not old
            assert new.state == STATE_DISCONNECTED
            assert env.timers.active == []
            await env.login(new)
            assert len(env.timers.active) == 1

        asyncio.run(run())


class TestBeginLogin:
    def _begin(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            return manager, await manager.async_begin_login()

        return asyncio.run(run())

    def test_authorize_url_contents(self, env):
        manager, url = self._begin(env)
        parts = urllib.parse.urlsplit(url)
        query = _query(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://auth.openai.com/api/accounts/authorize"
        assert query["client_id"] == "dynamic_agent_client"
        assert query["agent_name_hint"] == "Climate Advisor"
        assert query["ext_agent_host_id"] == manager.host_id
        assert query["response_type"] == "code"
        assert query["redirect_uri"] == f"http://127.0.0.1:{chatgpt_account.LOGIN_PORT}/auth/callback"
        assert query["resource"] == "https://api.openai.com/v1"
        assert set(query["scope"].split()) == {
            "openid",
            "profile",
            "email",
            "offline_access",
            "resource.invoke",
            DIRECT,
        }
        assert query["code_challenge_method"] == "S256"
        assert "prompt" not in query
        assert "code_verifier" not in query

    def test_challenge_is_s256_of_the_verifier(self, env):
        manager, url = self._begin(env)
        expected = base64.urlsafe_b64encode(hashlib.sha256(manager._pending_login.verifier.encode()).digest()).rstrip(
            b"="
        )
        assert _query(url)["code_challenge"] == expected.decode()

    def test_begin_login_leaves_a_pending_login(self, env):
        assert self._begin(env)[0].has_pending_login is True

    def test_makes_no_network_call_and_status_shows_pending(self, env):
        manager, _url = self._begin(env)
        assert env.session.posts == []
        assert env.session.gets == []
        assert manager.status()["login_pending"] is True

    def test_each_attempt_has_fresh_secrets_and_replaces_the_last(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            first = await manager.async_begin_login()
            second = await manager.async_begin_login()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(first), OWNER)
            assert exc.value.reason == "state_mismatch"
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(second), OWNER)
            return first, second, manager

        first, second, manager = asyncio.run(run())
        for key in ("state", "nonce", "code_challenge"):
            assert _query(first)[key] != _query(second)[key]
        assert manager.state == STATE_CONNECTED

    def test_refused_when_connected(self, env):
        async def run():
            manager = await env.connected()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_begin_login()
            assert exc.value.reason == "already_connected"
            assert manager.has_pending_login is False

        asyncio.run(run())

    def test_cancel_forgets_the_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            assert manager.async_cancel_login() is None
            assert manager.has_pending_login is False
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "no_pending_login"

        asyncio.run(run())

    def test_each_attempt_has_its_own_id_that_is_not_the_state(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            assert manager.pending_login_id is None
            first = await manager.async_begin_login()
            first_id = manager.pending_login_id
            second = await manager.async_begin_login()
            return first, first_id, second, manager.pending_login_id

        first, first_id, second, second_id = asyncio.run(run())
        assert first_id and second_id and first_id != second_id
        assert first_id not in (_query(first)["state"], _query(second)["state"])
        assert second_id not in (_query(first)["state"], _query(second)["state"])

    def test_cancel_with_a_stale_id_keeps_the_newer_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            first = await manager.async_begin_login()
            stale_id = manager.pending_login_id
            second = await manager.async_begin_login()
            manager.async_cancel_login(stale_id)
            assert manager.has_pending_login is True
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(second), OWNER)
            return first, manager

        _first, manager = asyncio.run(run())
        assert manager.state == STATE_CONNECTED

    def test_cancel_with_the_current_id_cancels_and_without_id_cancels_any(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await manager.async_begin_login()
            manager.async_cancel_login(manager.pending_login_id)
            assert manager.has_pending_login is False
            assert manager.pending_login_id is None
            await manager.async_begin_login()
            manager.async_cancel_login()
            assert manager.has_pending_login is False

        asyncio.run(run())

    def test_stale_paste_is_state_mismatch_and_keeps_the_newer_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            first = await manager.async_begin_login()
            second = await manager.async_begin_login()
            current_id = manager.pending_login_id
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(first), OWNER)
            assert exc.value.reason == "state_mismatch"
            assert manager.pending_login_id == current_id
            assert manager.has_pending_login is True
            return second

        asyncio.run(run())

    def test_pending_is_not_persisted_and_does_not_survive_restart(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.restart()
            restarted = await async_get_account_manager(env.hass)
            assert restarted.has_pending_login is False
            with pytest.raises(AccountLoginError) as exc:
                await restarted.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "no_pending_login"

        asyncio.run(run())


class TestCompleteLogin:
    def test_success_stores_owner_tokens_and_status(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            verifier, nonce = manager._pending_login.verifier, manager._pending_login.nonce
            env.session.token_responses = [_exchange_ok()]
            status = await manager.async_complete_login(_callback(url), OWNER)
            return manager, status, verifier, nonce, threading.get_ident()

        manager, status, verifier, nonce, loop_thread = asyncio.run(run())
        assert status == manager.status()
        assert status["state"] == STATE_CONNECTED
        assert status["owner_user_id"] == OWNER
        assert status["email"] == EMAIL
        assert status["login_pending"] is False
        assert manager.owner_user_id == OWNER
        stored = env.disk.data
        assert stored["owner_user_id"] == OWNER
        assert stored["credentials"]["refresh_token"] == REFRESH
        assert stored["credentials"]["client_id"] == CLIENT_ID
        assert "id_token" not in stored["credentials"]
        assert env.session.posts == [
            (
                TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": "fake-auth-code",
                    "code_verifier": verifier,
                    "redirect_uri": chatgpt_account.LOGIN_REDIRECT_URI,
                    "resource": "https://api.openai.com/v1",
                },
            )
        ]
        assert env.session.gets == [DISCOVERY_URL, JWKS_URL]
        assert all(kw["allow_redirects"] is False for kw in env.session.kwargs_seen)
        token, jwks, audience, seen_nonce, thread = env.verified[0]
        assert token == _id_token()
        assert jwks == {"keys": [{"kid": "k1"}]}
        assert audience == CLIENT_ID
        assert seen_nonce == nonce
        assert thread != loop_thread

    def test_token_from_a_new_login_is_usable_without_refresh(self, env):
        async def run():
            manager = await env.connected()
            return await manager.async_get_access_token()

        assert asyncio.run(run()) == ACCESS
        assert env.session.posts == []

    @pytest.mark.parametrize("wrapped", [" {} ", "\n{}\n"])
    def test_surrounding_whitespace_is_forgiven(self, env, wrapped):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(wrapped.format(_callback(url)), OWNER)
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())

    def test_already_connected(self, env):
        async def run():
            manager = await env.connected()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(f"{chatgpt_account.LOGIN_REDIRECT_URI}?code=c&state=s", "other")
            assert exc.value.reason == "already_connected"
            assert manager.owner_user_id == OWNER

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH

    def test_owner_required_leaves_the_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), "")
            assert exc.value.reason == "owner_required"
            assert manager.has_pending_login is True

        asyncio.run(run())
        assert env.session.posts == []

    def test_absent_pending(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(f"{chatgpt_account.LOGIN_REDIRECT_URI}?code=c&state=s", OWNER)
            assert exc.value.reason == "no_pending_login"

        asyncio.run(run())
        assert env.session.posts == []

    def test_expired_pending(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.clock(chatgpt_account.LOGIN_TTL_SECONDS - 1)
            assert manager.has_pending_login is True
            env.clock(chatgpt_account.LOGIN_TTL_SECONDS)
            assert manager.has_pending_login is False
            assert manager.status()["login_pending"] is False
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "login_expired"
            with pytest.raises(AccountLoginError) as again:
                await manager.async_complete_login(_callback(url), OWNER)
            assert again.value.reason == "no_pending_login"

        asyncio.run(run())
        assert env.session.posts == []

    @pytest.mark.parametrize(
        "pasted",
        [
            "not a url",
            "http://localhost:54321/auth/callback?code=c&state=s",
            "http://127.0.0.1.evil.test:54321/auth/callback?code=c&state=s",
            "http://user@127.0.0.1:54321/auth/callback?code=c&state=s",
            "https://127.0.0.1:54321/auth/callback?code=c&state=s",
            "http://127.0.0.1:1455/auth/callback?code=c&state=s",
            "http://127.0.0.1/auth/callback?code=c&state=s",
            "http://127.0.0.1:notaport/auth/callback?code=c&state=s",
            "http://127.0.0.1:54321/callback?code=c&state=s",
            "http://127.0.0.1:54321/auth/callback/x?code=c&state=s",
            "http://127.0.0.1:54321/auth/callback?code=c",
            "http://127.0.0.1:54321/auth/callback?state=s",
            "http://127.0.0.1:54321/auth/callback?code=a&code=b&state=s",
            "http://[::1/auth/callback?code=c&state=s",
            "http://127.0.0.1:54321/auth/callback?code=c&state=s&pad=" + "x" * 9000,
            None,
        ],
    )
    def test_bad_paste_is_refused_unfetched_and_keeps_the_attempt(self, env, pasted):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(pasted, OWNER)
            assert exc.value.reason == "bad_url"
            assert manager.has_pending_login is True
            assert env.session.posts == []
            assert env.session.gets == []
            # The attempt is still good.
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(url), OWNER)
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())

    def test_state_mismatch_keeps_the_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url, state="forged"), OWNER)
            assert exc.value.reason == "state_mismatch"
            assert manager.has_pending_login is True
            assert env.session.posts == []

        asyncio.run(run())

    def _failed_once(self, env, reason, *, callback_overrides=None, exchange=None):
        """One attempt that passes state and fails with ``reason``. The attempt must be used up."""

        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [exchange if exchange is not None else _exchange_ok()]
            pasted = _callback(url, **(callback_overrides or {}))
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(pasted, OWNER)
            assert exc.value.reason == reason
            assert manager.state == STATE_DISCONNECTED
            assert manager.has_pending_login is False
            with pytest.raises(AccountLoginError) as again:
                await manager.async_complete_login(pasted, OWNER)
            assert again.value.reason == "no_pending_login"
            return manager

        manager = asyncio.run(run())
        stored = env.disk.data or {}
        assert not stored.get("credentials")
        return manager

    def test_declined_consent_makes_no_exchange(self, env):
        self._failed_once(env, "consent_denied", callback_overrides={"error": "access_denied", "code": _DROP})
        assert env.session.posts == []

    def test_other_oauth_error_makes_no_exchange(self, env):
        self._failed_once(env, "exchange_failed", callback_overrides={"error": "server_error", "code": _DROP})
        assert env.session.posts == []

    @pytest.mark.parametrize(
        "client",
        [_DROP, "someone_else", "dynamic_agent_client"],
    )
    def test_new_registration_needs_an_issued_client_id(self, env, client):
        self._failed_once(env, "login_checks_failed", callback_overrides={"client_id": client})
        assert env.session.posts == []

    @pytest.mark.parametrize(
        "exchange",
        [
            FakeClientError("connection reset"),
            (400, {"error": "invalid_grant"}),
            (500, "bad gateway"),
            (200, "not json"),
        ],
    )
    def test_exchange_failure_is_never_retried(self, env, exchange):
        expected = "login_checks_failed" if isinstance(exchange, tuple) and exchange[0] == 200 else "exchange_failed"
        self._failed_once(env, expected, exchange=exchange)
        assert len(env.session.posts) == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"id_token": _DROP},
            {"refresh_token": ""},
            {"access_token": 5},
            {"access_token": "x" * 70000},
            {"token_type": "mac"},
            {"access_token": "same-token", "refresh_token": "same-token"},
            {"access_token": _jwt({"client_id": "oaiapp_OTHER"})},
        ],
    )
    def test_malformed_or_unbound_tokens_are_refused(self, env, overrides):
        self._failed_once(env, "login_checks_failed", exchange=_exchange_ok(**overrides))

    def test_jwt_access_token_for_this_client_is_accepted(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await env.login(manager, access_token=_jwt({"client_id": CLIENT_ID}))
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())

    def test_failed_id_token_verification_keeps_nothing(self, env):
        env.verify_claims = AccountLoginError("login_checks_failed")
        self._failed_once(env, "login_checks_failed")

    def test_missing_identity(self, env):
        env.verify_claims = {"sub": ""}
        self._failed_once(env, "bad_identity")

    @pytest.mark.parametrize("scope", [_DROP, "", "openid profile", 5])
    def test_missing_scope_then_next_attempt_asks_for_consent_again(self, env, scope):
        env.session.revoke_responses = [(200, "")] * 2  # the refused tokens, then the disconnect
        manager = self._failed_once(env, "missing_scope", exchange=_exchange_ok(scope=scope))

        async def run():
            url = await manager.async_begin_login()
            assert _query(url)["prompt"] == "consent"
            assert _query(url)["client_id"] == CLIENT_ID
            assert "agent_name_hint" not in _query(url)
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(url), OWNER)
            manager.async_cancel_login()
            await manager.async_disconnect()
            return await manager.async_begin_login()

        assert "prompt" not in _query(asyncio.run(run()))

    def test_discovery_off_the_auth_host_is_refused(self, env):
        env.session.discovery_doc = {"issuer": ISSUER, "jwks_uri": "https://evil.test/jwks"}
        self._failed_once(env, "login_checks_failed")
        assert "https://evil.test/jwks" not in env.session.gets

    def test_wrong_discovery_issuer_is_refused(self, env):
        env.session.discovery_doc = {"issuer": "https://evil.test", "jwks_uri": JWKS_URL}
        self._failed_once(env, "login_checks_failed")

    @pytest.mark.parametrize("response", [FakeClientError("down"), (503, ""), (200, "not json")])
    def test_unreachable_signing_keys_are_an_exchange_failure(self, env, response):
        env.session.jwks_response = response
        self._failed_once(env, "exchange_failed")
        assert env.verified == []

    def test_issued_client_is_saved_before_the_exchange_and_reused_on_retry(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [FakeClientError("down")]
            with pytest.raises(AccountLoginError):
                await manager.async_complete_login(_callback(url), OWNER)
            assert env.disk.data["issued_client_id"] == CLIENT_ID

            env.restart()
            restarted = await async_get_account_manager(env.hass)
            retry = await restarted.async_begin_login()
            assert _query(retry)["client_id"] == CLIENT_ID
            assert "agent_name_hint" not in _query(retry)
            env.session.token_responses = [_exchange_ok()]
            await restarted.async_complete_login(_callback(retry, client_id=_DROP), OWNER)
            assert restarted.state == STATE_CONNECTED

        asyncio.run(run())

    def test_failed_save_is_reported_and_rolled_back(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.disk.write_swallowed = True
            env.session.token_responses = [_exchange_ok()]
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "save_failed"
            assert exc.value.category == "transient"
            assert manager.state == STATE_DISCONNECTED
            assert manager.owner_user_id is None
            assert env.timers.active == []
            with pytest.raises(AIBackendError):
                await manager.async_get_access_token()

        asyncio.run(run())

    def test_tokens_saved_before_login_reports_success(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(url), OWNER)
            assert env.disk.data["credentials"]["access_token"] == ACCESS
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())

    def test_login_token_rotates_through_the_save_path(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            assert await manager.async_get_access_token() == "at_NEWACCESS00001"
            assert env.session.refreshes()[0]["refresh_token"] == REFRESH
            assert env.session.refreshes()[0]["client_id"] == CLIENT_ID

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == "rt_NEWREFRESH00001"


class TestRefusedLoginRevokesNewTokens:
    """A sign-in refused after a good code exchange must not leave a live refresh token at OpenAI."""

    async def _attempt(self, env, manager=None, exchange=None):
        manager = manager or await async_get_account_manager(env.hass)
        url = await manager.async_begin_login()
        env.session.token_responses = [exchange if exchange is not None else _exchange_ok()]
        with pytest.raises(AccountLoginError) as exc:
            await manager.async_complete_login(_callback(url), OWNER)
        return manager, exc.value

    def _revoked_form(self, token=REFRESH):
        return {"token": token, "token_type_hint": "refresh_token", "client_id": CLIENT_ID}

    @pytest.mark.parametrize(
        ("reason", "setup"),
        [
            (
                "login_checks_failed",
                lambda env: setattr(env, "verify_claims", AccountLoginError("login_checks_failed")),
            ),
            ("bad_identity", lambda env: setattr(env, "verify_claims", {"sub": ""})),
            ("exchange_failed", lambda env: setattr(env.session, "jwks_response", (503, ""))),
        ],
    )
    def test_refused_after_exchange_revokes_the_new_refresh_token(self, env, reason, setup):
        setup(env)

        async def run():
            _, err = await self._attempt(env)
            assert err.reason == reason

        asyncio.run(run())
        assert env.session.revokes() == [self._revoked_form()]

    def test_missing_scope_revokes_the_new_refresh_token(self, env):
        async def run():
            _, err = await self._attempt(env, exchange=_exchange_ok(scope="openid"))
            assert err.reason == "missing_scope"

        asyncio.run(run())
        assert env.session.revokes() == [self._revoked_form()]

    def test_malformed_response_still_revokes_a_usable_refresh_token(self, env):
        async def run():
            _, err = await self._attempt(env, exchange=_exchange_ok(token_type="mac"))
            assert err.reason == "login_checks_failed"

        asyncio.run(run())
        assert env.session.revokes() == [self._revoked_form()]

    @pytest.mark.parametrize("exchange", [_exchange_ok(refresh_token=_DROP), _exchange_ok(refresh_token=""), (200, [])])
    def test_no_refresh_token_means_no_revoke_call(self, env, exchange):
        env.verify_claims = {"sub": ""}

        async def run():
            _, err = await self._attempt(env, exchange=exchange)
            assert err.reason in ("login_checks_failed", "bad_identity")

        asyncio.run(run())
        assert env.session.revokes() == []
        assert env.session.gets in ([], [DISCOVERY_URL, JWKS_URL])

    def test_rejected_exchange_never_revokes(self, env):
        async def run():
            _, err = await self._attempt(env, exchange=(400, {"error": "invalid_grant"}))
            assert err.reason == "exchange_failed"

        asyncio.run(run())
        assert env.session.revokes() == []

    def test_successful_login_never_revokes(self, env):
        asyncio.run(env.connected())
        assert env.session.revokes() == []

    @pytest.mark.parametrize(
        "failure",
        [
            [(400, "")],
            [(500, "")] * 3,
            [FakeClientError("down")] * 3,
            [RuntimeError("boom")],
        ],
    )
    def test_revoke_failure_keeps_the_original_reason(self, env, failure, caplog):
        env.session.revoke_responses = list(failure)
        env.verify_claims = {"sub": ""}
        caplog.set_level(logging.DEBUG)

        async def run():
            manager, err = await self._attempt(env)
            assert err.reason == "bad_identity"
            assert manager.state == STATE_DISCONNECTED
            assert manager._lock.locked() is False

        asyncio.run(run())
        assert REFRESH not in caplog.text
        assert ACCESS not in caplog.text

    def test_revoke_with_no_endpoint_keeps_the_original_reason(self, env):
        env.session.discovery_doc = {"issuer": ISSUER, "jwks_uri": JWKS_URL}
        env.verify_claims = {"sub": ""}

        async def run():
            _, err = await self._attempt(env)
            assert err.reason == "bad_identity"

        asyncio.run(run())
        assert env.session.revokes() == []

    def test_hung_revoke_is_bounded(self, env, monkeypatch):
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr(ChatGPTAccountManager, "_async_revoke_token", hang)
        monkeypatch.setattr(chatgpt_account, "_LOGIN_REVOKE_TIMEOUT_SECONDS", 0.05)
        env.verify_claims = {"sub": ""}

        async def run():
            manager, err = await asyncio.wait_for(self._attempt(env), 5)
            assert err.reason == "bad_identity"
            assert manager._lock.locked() is False

        asyncio.run(run())

    def test_cancelled_step_leaves_nothing_inconsistent(self, env):
        env.verify_claims = {"sub": ""}

        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            started, release = asyncio.Event(), asyncio.Event()
            real = ChatGPTAccountManager._async_revoke_token

            async def slow(self, *args, **kwargs):
                started.set()
                await release.wait()
                return await real(self, *args, **kwargs)

            ChatGPTAccountManager._async_revoke_token = slow
            try:
                step = asyncio.ensure_future(manager.async_complete_login(_callback(url), OWNER))
                await asyncio.wait_for(started.wait(), 5)  # a missing revoke fails here, never hangs
                step.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(step, 5)
                release.set()
                await _settle()
            finally:
                ChatGPTAccountManager._async_revoke_token = real
            assert env.session.revokes() == [self._revoked_form()]
            assert manager._lock.locked() is False
            assert manager.state == STATE_DISCONNECTED
            assert manager.has_pending_login is False
            assert (env.disk.data or {}).get("credentials") is None

        asyncio.run(run())

    def test_reauth_account_is_untouched_and_its_tokens_are_not_revoked(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [(400, {"error": "invalid_grant"})]
            with pytest.raises(AIBackendError):
                await manager.async_get_access_token()
            env.forget_network()
            before = copy.deepcopy(env.disk.data)
            old_refresh = manager._credentials.refresh_token
            env.verify_claims = {"sub": "user-SOMEONEELSE"}
            _, err = await self._attempt(env, manager, exchange=_exchange_ok(refresh_token="rt_FRESH_OTHER"))
            assert err.reason == "different_account"
            assert manager.state == STATE_REAUTH_REQUIRED
            assert manager.owner_user_id == OWNER
            assert env.disk.data == before
            assert all(form["token"] != old_refresh for form in env.session.revokes())
            assert env.session.revokes() == [self._revoked_form("rt_FRESH_OTHER")]

        asyncio.run(run())

    def test_save_failed_with_proof_it_was_not_saved_revokes(self, env):
        env.disk.write_swallowed = True

        async def run():
            manager, err = await self._attempt(env)
            assert err.reason == "save_failed"
            assert manager.state == STATE_DISCONNECTED

        asyncio.run(run())
        assert env.session.revokes() == [self._revoked_form()]

    def test_save_failed_with_failed_readback_is_ambiguous_and_never_revokes(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            real_save = manager._store.async_save

            async def save_then_break_reads(data):
                await real_save(data)  # the write lands...
                env.disk.load_raises = True  # ...but the read-back fails

            manager._store.async_save = save_then_break_reads
            _, err = await self._attempt(env, manager)
            assert err.reason == "save_failed"

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH
        assert env.session.revokes() == []

    def test_save_that_raised_is_ambiguous_and_never_revokes(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            real_save = manager._store.async_save

            async def save_then_raise(data):
                await real_save(data)
                raise OSError("disk full after the write")

            manager._store.async_save = save_then_raise
            _, err = await self._attempt(env, manager)
            assert err.reason == "save_failed"

        asyncio.run(run())
        assert env.session.revokes() == []


class TestLoginConcurrency:
    def test_concurrent_completions_exchange_the_code_once(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            env.session.gate = asyncio.Event()
            attempts = [
                asyncio.create_task(manager.async_complete_login(_callback(url), owner)) for owner in ("a", "b", "c")
            ]
            await _settle()
            env.session.gate.set()
            return await asyncio.gather(*attempts, return_exceptions=True), manager

        results, manager = asyncio.run(run())
        assert len(env.session.posts) == 1
        errors = [r for r in results if isinstance(r, AccountLoginError)]
        assert len(errors) == 2
        assert all(e.reason == "already_connected" for e in errors)
        assert manager.owner_user_id == "a"

    def test_caller_cancelled_mid_exchange_still_saves(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            env.session.gate = asyncio.Event()
            attempt = asyncio.create_task(manager.async_complete_login(_callback(url), OWNER))
            await _settle()
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
            env.session.gate.set()
            await _settle()
            assert manager.state == STATE_CONNECTED

        asyncio.run(run())
        assert env.disk.data["credentials"]["refresh_token"] == REFRESH

    def test_token_request_waits_for_a_login_in_progress(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            env.session.token_responses = [_exchange_ok()]
            env.session.gate = asyncio.Event()
            attempt = asyncio.create_task(manager.async_complete_login(_callback(url), OWNER))
            await _settle()
            waiting = asyncio.create_task(manager.async_get_access_token())
            await _settle()
            assert not waiting.done()
            env.session.gate.set()
            await attempt
            return await waiting

        assert asyncio.run(run()) == ACCESS

    def test_disconnect_forgets_a_pending_attempt(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            await manager.async_begin_login()
            await manager.async_disconnect()
            assert manager.has_pending_login is False

        asyncio.run(run())


class TestReauthRecovery:
    async def _into_reauth(self, env):
        manager = await env.connected()
        env.clock(2 * HOUR)
        env.session.token_responses = [(400, {"error": "invalid_grant"})]
        with pytest.raises(AIBackendError):
            await manager.async_get_access_token()
        env.forget_network()
        assert manager.state == STATE_REAUTH_REQUIRED
        env.issues.async_delete_issue.reset_mock()
        return manager

    def test_login_clears_reauth_and_repair_and_keeps_host_id(self, env):
        async def run():
            manager = await self._into_reauth(env)
            host = manager.host_id
            url = await manager.async_begin_login()
            assert _query(url)["client_id"] == CLIENT_ID
            assert "agent_name_hint" not in _query(url)
            env.session.token_responses = [_exchange_ok()]
            # Reauthorization may omit client_id.
            status = await manager.async_complete_login(_callback(url, client_id=_DROP), OWNER)
            assert status["state"] == STATE_CONNECTED
            assert manager.host_id == host
            assert len(env.timers.active) == 1
            assert await manager.async_get_access_token() == ACCESS

        asyncio.run(run())
        env.issues.async_delete_issue.assert_called_once()

    def test_different_account_is_refused_and_reauth_stays(self, env):
        async def run():
            manager = await self._into_reauth(env)
            url = await manager.async_begin_login()
            env.verify_claims = {"sub": "user-SOMEONEELSE"}
            env.session.token_responses = [_exchange_ok()]
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "different_account"
            assert manager.state == STATE_REAUTH_REQUIRED

        asyncio.run(run())
        env.issues.async_delete_issue.assert_not_called()
        assert [form["token"] for form in env.session.revokes()] == [REFRESH]

    def test_a_different_account_message_says_to_disconnect_first(self):
        assert "disconnect first" in str(AccountLoginError("different_account"))
        assert "disconnect" not in str(AccountLoginError("bad_identity"))

    def test_sign_in_without_a_subject_is_still_bad_identity(self, env):
        async def run():
            manager = await self._into_reauth(env)
            url = await manager.async_begin_login()
            env.verify_claims = {"sub": ""}
            env.session.token_responses = [_exchange_ok()]
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            assert exc.value.reason == "bad_identity"

        asyncio.run(run())

    def test_different_client_is_refused_before_any_exchange(self, env):
        async def run():
            manager = await self._into_reauth(env)
            url = await manager.async_begin_login()
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url, client_id="oaiapp_OTHER"), OWNER)
            assert exc.value.reason == "login_checks_failed"

        asyncio.run(run())
        assert env.session.posts == []

    def test_disconnect_lets_a_different_account_register(self, env):
        async def run():
            manager = await self._into_reauth(env)
            await manager.async_disconnect()
            url = await manager.async_begin_login()
            assert _query(url)["client_id"] == "dynamic_agent_client"
            env.verify_claims = {"sub": "user-SOMEONEELSE"}
            env.session.token_responses = [_exchange_ok()]
            await manager.async_complete_login(_callback(url, client_id="oaiapp_NEWCLIENT"), "new-owner")
            assert manager.state == STATE_CONNECTED
            assert manager.owner_user_id == "new-owner"

        asyncio.run(run())
        assert env.disk.data["issued_client_id"] == "oaiapp_NEWCLIENT"


class TestDisconnect:
    def test_revokes_then_clears(self, env):
        async def run():
            manager = await env.connected()
            env.disk.events.clear()
            outcome = await manager.async_disconnect()
            assert manager.state == STATE_DISCONNECTED
            assert manager.owner_user_id is None
            with pytest.raises(AIBackendError) as exc:
                await manager.async_get_access_token()
            assert exc.value.category == "auth"
            return outcome

        assert asyncio.run(run()) == REVOCATION_CONFIRMED
        assert env.disk.events == ["revoke_post", "save"]
        assert env.disk.data["credentials"] is None
        assert env.disk.data["owner_user_id"] is None

    def test_any_failure_response_on_5xx_is_retried(self, env):
        async def run():
            manager = await env.connected()
            env.session.revoke_responses = [(503, ""), FakeClientError("x"), (200, "")]
            return await manager.async_disconnect()

        assert asyncio.run(run()) == REVOCATION_CONFIRMED
        assert len(env.session.revokes()) == 3

    def test_4xx_is_not_retried_and_clears_unconfirmed(self, env):
        async def run():
            manager = await env.connected()
            env.session.revoke_responses = [(400, {"error": "invalid_client"})]
            outcome = await manager.async_disconnect()
            assert manager.state == STATE_DISCONNECTED
            return outcome

        assert asyncio.run(run()) == REVOCATION_UNCONFIRMED
        assert len(env.session.revokes()) == 1
        assert REFRESH not in json.dumps(env.disk.data)

    def test_off_allowlist_revocation_endpoint_is_never_called(self, env):
        async def run():
            manager = await env.connected()
            env.session.discovery_doc = {"revocation_endpoint": "https://evil.test/revoke"}
            return await manager.async_disconnect()

        assert asyncio.run(run()) == REVOCATION_UNCONFIRMED
        assert env.session.posts == []

    def test_no_tokens_means_not_needed(self, env):
        async def run():
            manager = await async_get_account_manager(env.hass)
            return await manager.async_disconnect()

        assert asyncio.run(run()) == REVOCATION_NOT_NEEDED
        assert env.session.posts == []

    def test_clears_repair_and_cooldown(self, env):
        async def run():
            manager = await env.connected()
            manager.set_quota_cooldown("limit")
            await manager.async_disconnect()
            assert manager.quota_cooldown_active() is False
            await _settle()

        asyncio.run(run())
        env.issues.async_delete_issue.assert_called()

    def test_disconnect_waits_for_inflight_refresh_and_revokes_the_new_token(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            env.session.gate = asyncio.Event()
            refresh = asyncio.create_task(manager.async_get_access_token())
            await asyncio.sleep(0.01)
            leaving = asyncio.create_task(manager.async_disconnect())
            await asyncio.sleep(0.01)
            env.session.gate.set()
            await refresh
            await leaving

        asyncio.run(run())
        assert env.session.revokes()[0]["token"] == "rt_NEWREFRESH00001"

    def test_disconnect_still_works_while_persist_is_pending(self, env):
        async def run():
            manager = await env.connected()
            env.clock(2 * HOUR)
            env.session.token_responses = [_token_ok()]
            env.disk.write_swallowed = True
            with pytest.raises(AIBackendError):
                await manager.async_get_access_token()
            env.disk.write_swallowed = False
            assert await manager.async_disconnect() == REVOCATION_CONFIRMED

        asyncio.run(run())
        assert env.session.revokes()[0]["token"] == "rt_NEWREFRESH00001"
        assert env.disk.data["credentials"] is None


class TestRedaction:
    SECRETS = (ACCESS, REFRESH, "at_NEWACCESS00001", "rt_NEWREFRESH00001", SUBJECT, "fake-auth-code")

    def _assert_clean(self, text: str) -> None:
        for secret in self.SECRETS:
            assert secret not in text

    def test_repr_status_and_credentials_repr(self, env):
        async def run():
            return await env.connected()

        manager = asyncio.run(run())
        for text in (repr(manager), str(manager), json.dumps(manager.status()), repr(manager._credentials)):
            self._assert_clean(text)
            assert manager.host_id not in text

    def test_status_has_only_non_secret_fields(self, env):
        status = asyncio.run(env.connected()).status()
        assert set(status) == {
            "state",
            "owner_user_id",
            "email",
            "plan_usage_enabled",
            "connected_at",
            "last_refreshed_at",
            "quota_cooldown_active",
            "quota_next_probe_at",
            "persist_pending",
            "login_pending",
        }

    def test_nothing_secret_in_logs_or_exceptions(self, env, caplog):
        caplog.set_level(logging.DEBUG)
        raised: list[BaseException] = []
        echoing_body = {"error": {"code": ACCESS, "message": REFRESH}, "detail": REFRESH}

        async def run():
            manager = await env.connected()
            for offset, response in enumerate(
                [
                    (500, echoing_body),
                    (400, echoing_body),
                    (200, f"garbage {REFRESH}"),
                    FakeClientError(f"failed sending {REFRESH}"),
                    (400, {"error": "invalid_grant", "error_description": REFRESH}),
                ],
                start=2,
            ):
                env.clock(offset * HOUR)
                env.session.token_responses = [response]
                with pytest.raises(AIBackendError) as exc:
                    await manager.async_get_access_token()
                raised.append(exc.value)

            url = await manager.async_begin_login()
            manager_state = manager.state
            assert manager_state == STATE_REAUTH_REQUIRED
            for pasted in (
                f"http://evil.test/auth/callback?code=fake-auth-code&state={REFRESH}",
                _callback(url, state="forged"),
            ):
                with pytest.raises(AccountLoginError) as exc:
                    await manager.async_complete_login(pasted, OWNER)
                raised.append(exc.value)
            env.session.token_responses = [(400, {"error": "invalid_grant", "error_description": REFRESH})]
            with pytest.raises(AccountLoginError) as exc:
                await manager.async_complete_login(_callback(url), OWNER)
            raised.append(exc.value)

        asyncio.run(run())
        self._assert_clean(caplog.text)
        assert caplog.records, "expected the failures above to be logged"
        for err in raised:
            self._assert_clean(str(err))
            self._assert_clean(repr(err))
            assert err.__cause__ is None
            assert err.__suppress_context__ is True or err.__context__ is None

    def test_pending_login_never_shows(self, env, caplog):
        caplog.set_level(logging.DEBUG)

        async def run():
            manager = await async_get_account_manager(env.hass)
            url = await manager.async_begin_login()
            return manager, url

        manager, url = asyncio.run(run())
        query = _query(url)
        pending = manager._pending_login
        shown = f"{manager!r} {pending!r} {json.dumps(manager.status())} {caplog.text}"
        for secret in (pending.verifier, pending.state, pending.nonce, query["code_challenge"], url):
            assert secret not in shown
        assert pending.verifier not in url
        assert "pending" not in json.dumps(env.disk.data)
        assert pending.verifier not in json.dumps(env.disk.data)


async def _soon(awaitable):
    """A regression here must fail, not hang the suite."""
    return await asyncio.wait_for(awaitable, 5)


async def _sign_in_save_unproved(env, manager, *, landed: bool, refresh: str = REFRESH, owner: str = OWNER) -> None:
    """A sign-in whose read-back fails: ``landed`` is what really happened, which the manager cannot see."""
    url = await _soon(manager.async_begin_login())
    env.session.token_responses = [_exchange_ok(refresh_token=refresh)]
    real_save = manager._store.async_save

    async def save(data):
        ours = _stored_refresh(data) == refresh
        if landed or not ours:
            await real_save(data)
        env.disk.load_raises = ours

    manager._store.async_save = save
    with pytest.raises(AccountLoginError) as exc:
        await _soon(manager.async_complete_login(_callback(url), owner))
    manager._store.async_save = real_save
    env.disk.load_raises = False
    assert exc.value.reason == "save_failed"
    assert exc.value.tokens_may_be_saved is True
    assert env.session.revokes() == []
    env.forget_network()


def _stored_refresh(data) -> str | None:
    return ((data or {}).get("credentials") or {}).get("refresh_token")


async def _in_reauth_with_saved_tokens(env) -> ChatGPTAccountManager:
    """Reauth that keeps its tokens: the store holds a live ``rt_NEWREFRESH00001``."""
    manager = await env.connected()
    env.clock(2 * HOUR)
    env.session.token_responses = [_token_ok(scope="openid")]
    with pytest.raises(AIBackendError):
        await _soon(manager.async_get_access_token())
    assert manager.state == STATE_REAUTH_REQUIRED
    assert _stored_refresh(env.disk.data) == "rt_NEWREFRESH00001"
    env.forget_network()
    return manager


class TestUnprovedSignInSave:
    """The save returned but the read-back failed, so the tokens may or may not be in the store."""

    def test_status_says_uncertain_not_disconnected_with_no_io_and_no_token(self, env, caplog):
        caplog.set_level(logging.DEBUG)

        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            loads = env.disk.load_count
            status = manager.status()
            assert status["state"] == chatgpt_account.STATE_SAVE_UNCERTAIN
            assert env.disk.load_count == loads
            assert status["plan_usage_enabled"] is False
            shown = f"{json.dumps(status)} {manager!r} {manager._uncertain_login!r} {caplog.text}"
            assert REFRESH not in shown and ACCESS not in shown
            assert chatgpt_account.STATE_SAVE_UNCERTAIN not in json.dumps(env.disk.data)

        asyncio.run(run())

    def test_proof_it_was_not_saved_is_not_uncertain(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            url = await _soon(manager.async_begin_login())
            env.session.token_responses = [_exchange_ok()]
            env.disk.write_swallowed = True
            with pytest.raises(AccountLoginError):
                await _soon(manager.async_complete_login(_callback(url), OWNER))
            assert manager.status()["state"] == STATE_DISCONNECTED
            assert manager._uncertain_login is None

        asyncio.run(run())

    @pytest.mark.parametrize("landed", [True, False])
    def test_unreadable_store_refuses_begin_and_complete_and_stays_uncertain(self, env, landed):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=landed)
            on_disk = copy.deepcopy(env.disk.data)
            env.disk.load_raises = True
            for attempt in (manager.async_begin_login(), manager.async_complete_login("http://x/", OWNER)):
                with pytest.raises(AccountLoginError) as exc:
                    await _soon(attempt)
                assert exc.value.reason == "save_failed"
            assert manager.has_pending_login is False
            assert manager.status()["state"] == chatgpt_account.STATE_SAVE_UNCERTAIN
            assert env.disk.data == on_disk
            assert env.session.posts == []

        asyncio.run(run())

    def test_begin_adopts_tokens_that_landed_and_refuses_as_connected(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            on_disk = copy.deepcopy(env.disk.data)
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_begin_login())
            assert exc.value.reason == "already_connected"
            status = manager.status()
            assert status["state"] == STATE_CONNECTED
            assert status["owner_user_id"] == OWNER
            assert status["persist_pending"] is False
            assert manager._uncertain_login is None
            assert await _soon(manager.async_get_access_token()) == ACCESS
            assert env.disk.data == on_disk
            assert env.session.posts == []
            assert len(env.timers.active) == 1

        asyncio.run(run())

    def test_complete_adopts_tokens_that_landed_and_refuses_as_connected(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_complete_login("http://x/", OWNER))
            assert exc.value.reason == "already_connected"
            assert manager.state == STATE_CONNECTED
            assert env.session.revokes() == []

        asyncio.run(run())

    def test_begin_revokes_tokens_that_did_not_land_and_carries_on(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            url = await _soon(manager.async_begin_login())
            assert manager._uncertain_login is None
            assert manager.status()["state"] == STATE_DISCONNECTED
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]
            assert env.session.revokes()[0]["client_id"] == CLIENT_ID
            assert _stored_refresh(env.disk.data) is None

            env.forget_network()
            env.session.token_responses = [_exchange_ok(refresh_token="rt_SECOND")]
            status = await _soon(manager.async_complete_login(_callback(url), OWNER))
            assert status["state"] == STATE_CONNECTED
            assert _stored_refresh(env.disk.data) == "rt_SECOND"
            assert env.session.revokes() == []

        asyncio.run(run())

    def test_complete_after_a_save_that_did_not_land_has_nothing_pending(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_complete_login("http://x/", OWNER))
            assert exc.value.reason == "no_pending_login"
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]

        asyncio.run(run())

    def test_failed_revoke_does_not_stop_the_new_sign_in_or_leak(self, env, caplog):
        caplog.set_level(logging.DEBUG)

        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            env.session.revoke_responses = [(400, {"error": REFRESH})]
            assert await _soon(manager.async_begin_login())
            assert manager._uncertain_login is None

        asyncio.run(run())
        assert REFRESH not in caplog.text

    def test_earlier_saved_tokens_are_never_revoked_when_the_new_ones_did_not_land(self, env):
        async def run():
            manager = await _in_reauth_with_saved_tokens(env)
            await _sign_in_save_unproved(env, manager, landed=False, refresh="rt_SECOND")
            assert await _soon(manager.async_begin_login())
            assert [form["token"] for form in env.session.revokes()] == ["rt_SECOND"]
            assert _stored_refresh(env.disk.data) == "rt_NEWREFRESH00001"
            assert manager.state == STATE_REAUTH_REQUIRED

        asyncio.run(run())

    def test_landed_over_a_reauth_adopts_the_stored_owner_and_clears_the_repair(self, env):
        async def run():
            manager = await _in_reauth_with_saved_tokens(env)
            env.issues.async_delete_issue.reset_mock()
            await _sign_in_save_unproved(env, manager, landed=True, refresh="rt_SECOND", owner="ha-user-2")
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_begin_login())
            assert exc.value.reason == "already_connected"
            assert manager.owner_user_id == "ha-user-2"
            assert env.session.revokes() == []
            env.issues.async_delete_issue.assert_called()

        asyncio.run(run())

    @pytest.mark.parametrize("landed", [True, False])
    def test_restart_trusts_only_the_store(self, env, landed):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=landed)
            env.restart()
            restarted = await _soon(async_get_account_manager(env.hass))
            assert restarted is not manager
            assert restarted.status()["state"] == (STATE_CONNECTED if landed else STATE_DISCONNECTED)
            assert restarted.owner_user_id == (OWNER if landed else None)

        asyncio.run(run())

    def test_token_request_cannot_flush_the_old_state_over_tokens_that_landed(self, env):
        """The failed save left the old state marked dirty, and a token request flushes it."""

        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            assert await _soon(manager.async_get_access_token()) == ACCESS
            assert manager.state == STATE_CONNECTED
            assert _stored_refresh(env.disk.data) == REFRESH

        asyncio.run(run())

    def test_token_request_with_unreadable_store_overwrites_nothing(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            env.disk.load_raises = True
            with pytest.raises(AIBackendError) as exc:
                await _soon(manager.async_get_access_token())
            assert exc.value.category == "transient"
            assert _stored_refresh(env.disk.data) == REFRESH
            assert manager.status()["state"] == chatgpt_account.STATE_SAVE_UNCERTAIN

        asyncio.run(run())

    def test_token_request_when_nothing_landed_revokes_and_reports_not_connected(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            with pytest.raises(AIBackendError) as exc:
                await _soon(manager.async_get_access_token())
            assert exc.value.category == "auth"
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]

        asyncio.run(run())

    def test_quota_save_while_uncertain_waits_and_overwrites_nothing(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            on_disk = copy.deepcopy(env.disk.data)
            manager.set_quota_cooldown("limit")
            await _settle()
            assert env.disk.data == on_disk

        asyncio.run(run())

    @pytest.mark.parametrize("landed", [True, False])
    @pytest.mark.parametrize("readable", [True, False])
    def test_disconnect_revokes_the_token_and_clears_the_store(self, env, landed, readable):
        """Needs no read: the kept token is revoked whichever way the save went, and the store is replaced."""

        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=landed)
            real_save = manager._store.async_save

            async def save(data):
                await real_save(data)
                env.disk.load_raises = not readable

            manager._store.async_save = save
            env.disk.load_raises = not readable
            if readable:
                assert await _soon(manager.async_disconnect()) == REVOCATION_CONFIRMED
            else:
                with pytest.raises(AIBackendError):  # the clearing save cannot be proved either
                    await _soon(manager.async_disconnect())
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]
            assert _stored_refresh(env.disk.data) is None
            status = manager.status()
            assert status["state"] == STATE_DISCONNECTED
            assert manager._uncertain_login is None
            env.disk.load_raises = False
            manager._store.async_save = real_save
            env.restart()
            restarted = await _soon(async_get_account_manager(env.hass))
            assert restarted.state == STATE_DISCONNECTED

        asyncio.run(run())

    @pytest.mark.parametrize("answers", [[(200, ""), (400, "")], [(400, ""), (200, "")]])
    def test_disconnect_revokes_earlier_tokens_too_and_reports_the_worst_outcome(self, env, answers):
        async def run():
            manager = await _in_reauth_with_saved_tokens(env)
            await _sign_in_save_unproved(env, manager, landed=True, refresh="rt_SECOND")
            env.session.revoke_responses = answers
            assert await _soon(manager.async_disconnect()) == REVOCATION_UNCONFIRMED
            assert {form["token"] for form in env.session.revokes()} == {"rt_NEWREFRESH00001", "rt_SECOND"}
            assert _stored_refresh(env.disk.data) is None

        asyncio.run(run())

    def test_failed_sign_in_keeps_the_probe_lease(self, env):
        async def run():
            manager = await _in_reauth_with_saved_tokens(env)
            manager.set_quota_cooldown("limit")
            env.clock(2 * HOUR + chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            await _settle()
            await _sign_in_save_unproved(env, manager, landed=False, refresh="rt_SECOND")
            assert manager.quota_cooldown_active() is True

        asyncio.run(run())

    def test_final_removal_revokes_the_kept_token(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_CONFIRMED
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]
            assert env.disk.data is None

        asyncio.run(run())

    def test_concurrent_callers_reconcile_once_without_deadlock(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            urls = await _soon(asyncio.gather(manager.async_begin_login(), manager.async_begin_login()))
            assert all(urls)
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]

        asyncio.run(run())

    def test_cancelled_caller_does_not_lose_the_reconcile(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=False)
            begin = asyncio.create_task(manager.async_begin_login())
            await asyncio.sleep(0)
            begin.cancel()
            with pytest.raises(asyncio.CancelledError):
                await begin
            await _soon(asyncio.gather(*manager._critical_tasks, return_exceptions=True))
            assert manager._uncertain_login is None
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]

        asyncio.run(run())

    def test_429_while_uncertain_survives_adoption_and_is_saved(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            manager.set_quota_cooldown("limit")
            until, epoch = manager._quota_until, manager.quota_epoch
            await _settle()
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_begin_login())
            assert exc.value.reason == "already_connected"
            assert manager.state == STATE_CONNECTED
            assert manager.quota_cooldown_active() is True
            assert manager.quota_epoch == epoch
            assert manager.status()["persist_pending"] is False
            assert env.disk.data["quota_cooldown_until"] == until
            assert _stored_refresh(env.disk.data) == REFRESH

        asyncio.run(run())

    def test_probe_lease_taken_while_uncertain_survives_adoption(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            manager.set_quota_cooldown("limit")
            env.clock(chatgpt_account.QUOTA_COOLDOWN_SECONDS + 1)
            assert manager.quota_cooldown_active() is False
            lease = manager._quota_probe_until
            await _settle()
            with pytest.raises(AccountLoginError):
                await _soon(manager.async_begin_login())
            assert manager.quota_cooldown_active() is True
            assert env.disk.data["quota_probe_until"] == lease

        asyncio.run(run())

    def test_failed_merge_save_stays_dirty_and_keeps_the_cooldown(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            manager.set_quota_cooldown("limit")
            await _settle()
            env.disk.write_swallowed = True
            with pytest.raises(AccountLoginError) as exc:
                await _soon(manager.async_begin_login())
            assert exc.value.reason == "already_connected"
            assert manager.state == STATE_CONNECTED
            assert manager.status()["persist_pending"] is True
            assert manager.quota_cooldown_active() is True
            assert env.disk.data["quota_cooldown_until"] is None

            env.disk.write_swallowed = False
            assert await _soon(manager.async_get_access_token()) == ACCESS
            assert manager.status()["persist_pending"] is False
            assert env.disk.data["quota_cooldown_until"] is not None

        asyncio.run(run())

    def test_cooldown_from_before_the_sign_in_is_still_cleared_by_adoption(self, env):
        """Same result as a sign-in whose save was proved, or a restart: the new record has none."""

        async def run():
            manager = await _in_reauth_with_saved_tokens(env)
            manager.set_quota_cooldown("limit")
            await _settle()
            await _sign_in_save_unproved(env, manager, landed=True, refresh="rt_SECOND")
            on_disk = copy.deepcopy(env.disk.data)
            with pytest.raises(AccountLoginError):
                await _soon(manager.async_begin_login())
            assert manager.quota_cooldown_active() is False
            assert env.disk.data == on_disk

        asyncio.run(run())

    def test_429_during_the_failed_save_itself_is_kept(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            url = await _soon(manager.async_begin_login())
            env.session.token_responses = [_exchange_ok()]
            real_save = manager._store.async_save

            async def save(data):
                await real_save(data)
                if _stored_refresh(data) == REFRESH:
                    manager.set_quota_cooldown("limit")
                    env.disk.load_raises = True

            manager._store.async_save = save
            with pytest.raises(AccountLoginError):
                await _soon(manager.async_complete_login(_callback(url), OWNER))
            manager._store.async_save = real_save
            env.disk.load_raises = False
            assert manager.quota_cooldown_active() is True
            with pytest.raises(AccountLoginError):
                await _soon(manager.async_begin_login())
            assert manager.state == STATE_CONNECTED
            assert manager.quota_cooldown_active() is True
            assert env.disk.data["quota_cooldown_until"] is not None

        asyncio.run(run())


class TestRemovalThatCannotDelete:
    def test_failed_delete_leaves_a_blank_record_and_a_new_setup_starts_disconnected(self, env):
        async def run():
            manager = await env.connected()
            old_host = manager.host_id
            env.session.revoke_responses = [FakeClientError("down")] * 3
            env.disk.remove_raises = True
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_UNCONFIRMED
            assert env.disk.data == {}
            assert CHATGPT_ACCOUNT_HASS_KEY not in env.hass.data

            env.restart()
            new = await _soon(async_get_account_manager(env.hass))
            assert new is not manager
            assert new.status()["state"] == STATE_DISCONNECTED
            assert new.owner_user_id is None
            # Same as a removal that deleted: a new host ID on the next sign-in, saved and then stable.
            assert new.host_id is None
            url = await _soon(new.async_begin_login())
            assert _query(url)["client_id"] == chatgpt_account._BOOTSTRAP_CLIENT_ID
            assert new.host_id and new.host_id != old_host
            assert env.disk.data["host_id"] == new.host_id
            env.restart()
            assert (await _soon(async_get_account_manager(env.hass))).host_id == new.host_id

        asyncio.run(run())

    @pytest.mark.parametrize("failure", ["save_raises", "write_swallowed"])
    def test_delete_and_overwrite_both_failing_is_logged_without_a_token(self, env, caplog, failure):
        caplog.set_level(logging.DEBUG)

        async def run():
            manager = await env.connected()
            env.disk.remove_raises = True
            setattr(env.disk, failure, True)
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_CONFIRMED
            assert CHATGPT_ACCOUNT_HASS_KEY not in env.hass.data
            assert manager._closed is True
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]

        asyncio.run(run())
        assert chatgpt_account.STORAGE_KEY in caplog.text
        assert "by hand" in caplog.text
        assert REFRESH not in caplog.text and ACCESS not in caplog.text

    def test_loaded_account_is_revoked_even_when_the_store_is_unreadable(self, env):
        async def run():
            manager = await _soon(async_get_account_manager(env.hass))
            await _sign_in_save_unproved(env, manager, landed=True)
            env.disk.load_raises = True
            env.disk.remove_raises = True
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_CONFIRMED
            assert [form["token"] for form in env.session.revokes()] == [REFRESH]
            assert env.disk.data == {}  # written, though the read-back could not prove it

        asyncio.run(run())

    def test_unreadable_store_reports_unconfirmed_not_nothing_to_revoke(self, env):
        """The tokens in a file that cannot be read cannot be revoked, so nobody may be told otherwise."""

        async def run():
            await env.connected()
            env.restart()
            env.forget_network()
            env.disk.load_raises = True
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_UNCONFIRMED
            assert env.session.posts == []
            assert env.disk.data is None

        asyncio.run(run())

    def test_unreadable_store_with_failed_delete_is_blanked(self, env):
        async def run():
            await env.connected()
            env.restart()
            env.disk.load_raises = True
            env.disk.remove_raises = True
            assert await _soon(async_remove_account(env.hass)) == REVOCATION_UNCONFIRMED
            assert env.disk.data == {}
            env.disk.load_raises = False
            env.restart()
            new = await _soon(async_get_account_manager(env.hass))
            assert new.state == STATE_DISCONNECTED

        asyncio.run(run())
