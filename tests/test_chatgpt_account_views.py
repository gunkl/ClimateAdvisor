"""ChatGPT account REST views, owner-only investigate gating and AI status for plan providers."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# claude_api binds its SDK exception names at first import; see test_ai_backend.py.
if "custom_components.climate_advisor.claude_api" not in sys.modules:
    _mock_anthropic = MagicMock()
    _mock_anthropic.__name__ = "anthropic"
    _mock_anthropic.__path__ = []
    _mock_anthropic.__file__ = None
    _mock_anthropic.__spec__ = None
    _mock_anthropic.__loader__ = None
    _mock_anthropic.__package__ = "anthropic"
    _mock_anthropic.APIError = type("APIError", (Exception,), {})
    _mock_anthropic.APITimeoutError = type("APITimeoutError", (Exception,), {})
    _mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
    _mock_anthropic.NotFoundError = type("NotFoundError", (_mock_anthropic.APIError,), {})
    _mock_anthropic.AsyncAnthropic = MagicMock()
    sys.modules["anthropic"] = _mock_anthropic

import pytest  # noqa: E402

from custom_components.climate_advisor import api  # noqa: E402
from custom_components.climate_advisor.ai_backend import AIUserNotAllowedError  # noqa: E402
from custom_components.climate_advisor.ai_providers import ensure_providers_registered  # noqa: E402
from custom_components.climate_advisor.claude_api import ClaudeAPIClient  # noqa: E402
from custom_components.climate_advisor.const import CHATGPT_ACCOUNT_HASS_KEY, DOMAIN  # noqa: E402

ensure_providers_registered()

OWNER = "user-owner"
OTHER = "user-other"
SECRET = "rt_SUPERSECRETTOKEN"


class FakeManager:
    """Stands in for ChatGPTAccountManager: same read API, recorded calls."""

    def __init__(self, state="connected", owner=OWNER):
        self.state = state
        self.owner_user_id = owner
        self.hass = MagicMock()
        self.async_disconnect = AsyncMock(return_value="confirmed")

    def status(self):
        return {
            "state": self.state,
            "owner_user_id": self.owner_user_id,
            "email": "owner@example.test",
            "plan_usage_enabled": self.state == "connected",
            "connected_at": None,
            "last_refreshed_at": None,
            "quota_cooldown_active": False,
            "quota_next_probe_at": None,
            "persist_pending": False,
        }


def _user(user_id=OWNER, admin=True):
    return SimpleNamespace(id=user_id, is_admin=admin)


def _request(hass, user=None, body=None, accept="", query=None):
    req = MagicMock()
    req.app = {"hass": hass}
    req.query = query or {}
    req.headers = {"Accept": accept}
    req.json = AsyncMock(return_value=body or {})
    values = {"hass_user": user} if user is not None else {}
    req.get = lambda key, default=None: values.get(key, default)
    return req


def _hass(manager=None, coordinators=None):
    hass = MagicMock()
    hass.data = {DOMAIN: dict(coordinators or {})}
    if manager is not None:
        hass.data[CHATGPT_ACCOUNT_HASS_KEY] = manager
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.async_add_executor_job = AsyncMock()
    return hass


def _chatgpt_client(manager, **config):
    cfg = {"ai_enabled": True, "ai_provider": "chatgpt", "ai_investigator_enabled": True, "ai_chatgpt_model": "gpt-5"}
    cfg.update(config)
    return ClaudeAPIClient(cfg, manager)


def _anthropic_client(**config):
    cfg = {"ai_enabled": True, "ai_provider": "anthropic", "ai_api_key": "sk-test", "ai_investigator_enabled": True}
    cfg.update(config)
    return ClaudeAPIClient(cfg, MagicMock())


# --------------------------------------------------------------------------- client


class TestCheckUserAllowed:
    def test_owner_allowed_under_plan_restriction(self):
        assert _chatgpt_client(FakeManager()).check_user_allowed(OWNER) is True

    def test_other_user_refused(self):
        assert _chatgpt_client(FakeManager()).check_user_allowed(OTHER) is False

    def test_missing_user_refused(self):
        assert _chatgpt_client(FakeManager()).check_user_allowed(None) is False

    @pytest.mark.parametrize("state", ["disconnected", "reauth_required"])
    def test_not_connected_refuses_everyone(self, state):
        assert _chatgpt_client(FakeManager(state=state)).check_user_allowed(OWNER) is False

    def test_no_manager_refuses(self):
        assert _chatgpt_client(None).check_user_allowed(OWNER) is False

    def test_anthropic_has_no_owner_restriction(self):
        client = _anthropic_client()
        assert client.check_user_allowed(OTHER) is True
        assert client.check_user_allowed(None) is True


class TestNonManualRefused:
    def test_request_with_auto_trigger_refused_before_backend(self):
        client = _chatgpt_client(FakeManager())
        client._backend.async_request = AsyncMock()
        resp = asyncio.run(client.async_request("sys", "msg", triggered_by="auto"))
        assert resp.success is False
        client._backend.async_request.assert_not_called()
        assert client._rate_counters.auto_requests_today == 0

    def test_stream_with_auto_trigger_refused(self):
        client = _chatgpt_client(FakeManager())

        async def run():
            async for _ in client.async_request_streaming("sys", "msg", triggered_by="auto"):
                pass

        with pytest.raises(RuntimeError, match="manually"):
            asyncio.run(run())

    def test_anthropic_auto_trigger_not_refused_by_restriction(self):
        client = _anthropic_client()
        assert client._plan_restricted_trigger_refused("auto") is False


class TestGetStatus:
    def test_unconnected_chatgpt_reports_not_connected(self):
        for manager in (None, FakeManager(state="disconnected", owner=None)):
            status = _chatgpt_client(manager).get_status()
            assert status["status"] == "not_connected"
            assert status["account_state"] == "disconnected"
            assert status["provider"] == "chatgpt"
            assert status["provider_label"] == "ChatGPT subscription"
            assert status["usage_url"]
            assert status["tracks_cost"] is False

    def test_connected_chatgpt_is_active(self):
        status = _chatgpt_client(FakeManager()).get_status()
        assert status["status"] == "active"
        assert status["account_state"] == "connected"

    def test_reauth_required_state_is_reported(self):
        assert _chatgpt_client(FakeManager(state="reauth_required")).get_status()["account_state"] == "reauth_required"

    def test_disabled_stays_disabled(self):
        assert _chatgpt_client(None, ai_enabled=False).get_status()["status"] == "disabled"

    def test_anthropic_status_unchanged_plus_provider_fields(self):
        status = _anthropic_client().get_status()
        assert status["status"] == "active"
        assert status["provider"] == "anthropic"
        assert status["account_state"] is None
        assert status["tracks_cost"] is True
        assert status["usage_url"] is None or isinstance(status["usage_url"], str)
        assert "api_key" not in status

    def test_status_has_no_token_values(self):
        manager = FakeManager()
        manager._credentials = SimpleNamespace(access_token=SECRET, refresh_token=SECRET)
        assert SECRET not in json.dumps(_chatgpt_client(manager).get_status(), default=str)


# --------------------------------------------------------------------------- investigate


def _coordinator(client):
    coord = MagicMock()
    coord.config = {"ai_enabled": True, "ai_investigator_enabled": True}
    coord.claude_client = client
    coord.zone_label = "z"
    coord.ai_skills = MagicMock()
    coord.ai_skills.async_execute = AsyncMock(return_value={"success": True, "source": "ai", "data": {}})

    async def stream(*a, **k):
        yield {"type": "done", "success": True, "source": "ai", "data": {}}

    coord.ai_skills.async_execute_streaming = MagicMock(side_effect=stream)
    coord.async_store_investigation_report = AsyncMock()
    return coord


def _investigate(coord, user, accept=""):
    hass = _hass(coordinators={"e1": coord})
    return asyncio.run(api.ClimateAdvisorInvestigateView().post(_request(hass, user, accept=accept)))


class TestInvestigateLogLine:
    @pytest.mark.parametrize("accept", ["", "text/event-stream"], ids=["non_streaming", "streaming"])
    def test_line_names_provider_model_and_zone(self, accept, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        coord = _coordinator(_chatgpt_client(FakeManager()))
        coord.config["ai_provider"] = "chatgpt"
        coord._entry_id = "entry-xyz"
        with patch.object(api.web, "StreamResponse", create=True) as stream_cls:
            stream_cls.return_value.prepare = AsyncMock()
            stream_cls.return_value.write = AsyncMock()
            stream_cls.return_value.drain = AsyncMock()
            stream_cls.return_value.write_eof = AsyncMock()
            _investigate(coord, _user(OWNER), accept)
        lines = [r for r in caplog.records if r.levelno == logging.INFO and "Investigation requested" in r.getMessage()]
        assert len(lines) == 1
        msg = lines[0].getMessage()
        assert "provider=chatgpt" in msg and "model=gpt-5" in msg and "entry_id=entry-xyz" in msg
        assert "focus_len=0 hours=24" in msg


class TestInvestigateOwnerOnly:
    @pytest.mark.parametrize("accept", ["", "text/event-stream"], ids=["non_streaming", "streaming"])
    def test_non_owner_refused_before_any_work(self, accept):
        client = _chatgpt_client(FakeManager())
        client.check_investigator_rate_limit = MagicMock(return_value=(True, ""))
        client.increment_investigator_counter = MagicMock()
        coord = _coordinator(client)

        resp = _investigate(coord, _user(OTHER), accept)

        assert resp.status == 403
        coord.ai_skills.async_execute.assert_not_called()
        coord.ai_skills.async_execute_streaming.assert_not_called()
        client.check_investigator_rate_limit.assert_not_called()
        client.increment_investigator_counter.assert_not_called()
        coord.async_store_investigation_report.assert_not_called()

    def test_requester_reaches_both_dispatch_paths(self):
        coord = _coordinator(_chatgpt_client(FakeManager()))
        _investigate(coord, _user(OWNER))
        assert coord.ai_skills.async_execute.call_args.kwargs["requester_id"] == OWNER
        with patch.object(api.web, "StreamResponse", create=True) as stream_cls:
            stream_cls.return_value.prepare = AsyncMock()
            stream_cls.return_value.write = AsyncMock()
            stream_cls.return_value.drain = AsyncMock()
            stream_cls.return_value.write_eof = AsyncMock()
            _investigate(coord, _user(OWNER), "text/event-stream")
        assert coord.ai_skills.async_execute_streaming.call_args.kwargs["requester_id"] == OWNER

    def test_refusal_at_dispatch_is_the_same_403_and_nothing_is_stored(self):
        client = _anthropic_client()  # passes the early check, as when the switch comes later
        client.increment_investigator_counter = MagicMock()
        coord = _coordinator(client)
        coord.ai_skills.async_execute = AsyncMock(side_effect=AIUserNotAllowedError())
        resp = _investigate(coord, _user(OTHER))
        assert resp.status == 403
        assert "Only the user who connected" in str(resp.json_data)
        client.increment_investigator_counter.assert_not_called()
        coord.async_store_investigation_report.assert_not_called()

    def test_not_connected_refused(self):
        coord = _coordinator(_chatgpt_client(FakeManager(state="disconnected", owner=None)))
        assert _investigate(coord, _user(OWNER)).status == 403
        coord.ai_skills.async_execute.assert_not_called()

    def test_anthropic_any_user_allowed(self):
        coord = _coordinator(_anthropic_client())
        assert _investigate(coord, _user(OTHER, admin=False)).status == 200

    def test_multi_zone_every_zone_checks_the_shared_owner(self):
        manager = FakeManager()
        coords = {"a": _coordinator(_chatgpt_client(manager)), "b": _coordinator(_chatgpt_client(manager))}
        for entry_id, coord in coords.items():
            hass = _hass(coordinators=coords)
            req = _request(hass, _user(OTHER), query={"entry_id": entry_id})
            resp = asyncio.run(api.ClimateAdvisorInvestigateView().post(req))
            assert resp.status == 403
            coord.ai_skills.async_execute.assert_not_called()
        hass = _hass(coordinators=coords)
        req = _request(hass, _user(OWNER), query={"entry_id": "b"})
        assert asyncio.run(api.ClimateAdvisorInvestigateView().post(req)).status == 200


# --------------------------------------------------------------------------- disconnect and status


def _disconnect(hass, user):
    return asyncio.run(api.ClimateAdvisorChatGPTAccountDisconnectView().post(_request(hass, user)))


class TestDisconnect:
    def test_non_admin_refused(self):
        manager = FakeManager()
        assert _disconnect(_hass(manager), _user(OWNER, admin=False)).status == 403
        manager.async_disconnect.assert_not_called()

    def test_second_admin_can_disconnect(self):
        manager = FakeManager(owner=OWNER)
        resp = _disconnect(_hass(manager), _user("another-admin"))
        assert resp.status == 200
        manager.async_disconnect.assert_awaited_once()
        assert resp.json_data["state"] == "disconnected"
        assert resp.json_data["revocation"] == "confirmed"
        assert resp.json_data["revocation_unconfirmed"] is False

    def test_unconfirmed_revocation_tells_user_to_use_chatgpt_settings(self):
        manager = FakeManager()
        manager.async_disconnect = AsyncMock(return_value="unconfirmed")
        resp = _disconnect(_hass(manager), _user("admin"))
        assert resp.json_data["revocation_unconfirmed"] is True
        assert "ChatGPT Settings" in resp.json_data["message"]

    def test_no_manager_is_a_noop(self):
        resp = _disconnect(_hass(None), _user("admin"))
        assert resp.status == 200
        assert resp.json_data["state"] == "disconnected"


def _status(hass, user):
    return asyncio.run(api.ClimateAdvisorChatGPTAccountStatusView().get(_request(hass, user)))


class TestAccountStatus:
    def test_admin_and_owner_see_identity(self):
        for user in (_user("admin"), _user(OWNER, admin=False)):
            body = _status(_hass(FakeManager()), user).json_data
            assert body["email"] == "owner@example.test"
            assert body["owner_user_id"] == OWNER

    def test_other_user_gets_no_identity(self):
        body = _status(_hass(FakeManager()), _user(OTHER, admin=False)).json_data
        assert "email" not in body
        assert "owner_user_id" not in body
        assert body["state"] == "connected"
        assert body["is_admin"] is False
        assert body["is_owner"] is False

    def test_flags_reflect_requester(self):
        body = _status(_hass(FakeManager()), _user(OWNER)).json_data
        assert body["is_admin"] is True and body["is_owner"] is True

    def test_no_manager_reports_disconnected(self):
        body = _status(_hass(None), _user("admin")).json_data
        assert body["state"] == "disconnected"

    def test_no_secret_values(self):
        manager = FakeManager()
        manager._credentials = SimpleNamespace(access_token=SECRET, refresh_token=SECRET)
        assert SECRET not in json.dumps(_status(_hass(manager), _user("admin")).json_data, default=str)

    def test_views_registered(self):
        names = {v.__name__ for v in api.API_VIEWS}
        assert {
            "ClimateAdvisorChatGPTAccountStatusView",
            "ClimateAdvisorChatGPTAccountDisconnectView",
        } <= names
        for view in (
            api.ClimateAdvisorChatGPTAccountStatusView,
            api.ClimateAdvisorChatGPTAccountDisconnectView,
        ):
            assert view.requires_auth is True
            assert "entry_id" not in view.url
