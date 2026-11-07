"""Tests for the provider seam (ai_backend.py) and its use by ClaudeAPIClient.

A fake API-key provider shaped like OpenRouter is registered purely through
``PROVIDERS`` and must run settings resolution and inference with no change to
the shared ``ClaudeAPIClient`` wrapper.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import sys
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

# Same stub test_claude_api.py injects: claude_api.py binds its SDK exception names at
# first import, so a bare import here (collected first) would bind them to Exception
# and break that file. Skipped when claude_api is already imported.
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

from custom_components.climate_advisor import claude_api  # noqa: E402
from custom_components.climate_advisor.ai_backend import (  # noqa: E402
    PROVIDERS,
    AIBackendError,
    AIUserNotAllowedError,
    ProviderSpec,
    get_provider,
)
from custom_components.climate_advisor.claude_api import ClaudeAPIClient, ClaudeResponse  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MAX_RETRIES,
    CONF_AI_PROVIDER,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_REASONING_EFFORT,
    DEFAULT_AI_TEMPERATURE,
)

# ---------------------------------------------------------------------------
# A fake API-key provider shaped like OpenRouter
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal backend: no SDK, records what the wrapper hands it."""

    def for_request(self) -> FakeBackend:
        return self

    def __init__(self, config: dict[str, Any], client: Any | None = None) -> None:
        self._config = config
        self.client = client or object()
        self.calls: list[dict[str, Any]] = []
        self.errors: list[Exception] = []

    @property
    def inactive_reason(self) -> str | None:
        return None if self._config.get("fake_key") else "Fake key missing"

    async def async_request(self, **kwargs: Any) -> ClaudeResponse:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return ClaudeResponse(
            success=True,
            content="fake answer",
            input_tokens=10,
            output_tokens=20,
            estimated_cost=0.0,
            latency_ms=1.0,
            resolved_model=kwargs["model"],
        )

    async def async_stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        yield {"type": "text", "text": "fake "}
        yield {"type": "text", "text": "stream"}
        yield {
            "type": "stop",
            "stop_reason": "end_turn",
            "truncated_empty": False,
            "input_tokens": 5,
            "output_tokens": 6,
            "estimated_cost": 0.0,
        }

    async def async_list_models(self) -> list[str]:
        return ["fake/model-a", "fake/model-b"]

    async def async_test_connection(self, hass: Any | None = None) -> tuple[bool, str]:
        return True, "ok"

    def update_config(self, config: dict[str, Any]) -> None:
        self._config = config


def _fake_resolve(config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    # Drops Claude-only overrides and reads its own setting names.
    return {
        "model": config.get("fake_model", "fake/model-a"),
        "max_tokens": 0,
        "temperature": 0.0,
        "reasoning_effort": "",
    }


async def _fake_list_models(config: dict[str, Any], hass: Any | None = None) -> list[str]:
    return ["fake/model-a", "fake/model-b"]


async def _fake_validate(config: dict[str, Any], hass: Any | None = None) -> tuple[bool, str]:
    return (bool(config.get("fake_key")), "ok" if config.get("fake_key") else "no key")


FAKE_SPEC = ProviderSpec(
    id="fake",
    label="Fake Router",
    display_name="Fake Router",
    api_name="Fake Router",
    credential_kind="api_key",
    build_backend=lambda config, client=None: FakeBackend(config, client),
    usage_url="https://example.invalid/usage",
    tracks_cost=False,
    supports_tuning=False,
    supports_tier_fallback=False,
    plan_restricted=False,
    resolve_request_options=_fake_resolve,
    list_models=_fake_list_models,
    validate_credentials=_fake_validate,
    credential_setting="fake_key",
    model_setting="fake_model",
)


@pytest.fixture
def fake_provider(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "fake", FAKE_SPEC)
    return FAKE_SPEC


def _fake_client(**overrides: Any) -> ClaudeAPIClient:
    config = {
        "ai_enabled": True,
        CONF_AI_PROVIDER: "fake",
        "fake_key": "k",
        "fake_model": "fake/model-b",
        **overrides,
    }
    return ClaudeAPIClient(config)


# ---------------------------------------------------------------------------
# Registry and errors
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_unknown_provider_is_none(self):
        assert get_provider("nope") is None

    def test_error_carries_category(self):
        err = AIBackendError("boom", "quota")
        assert err.category == "quota"
        assert str(err) == "boom"
        with pytest.raises(ValueError):
            AIBackendError("boom", "bogus")


class TestAnthropicResolver:
    def test_defaults_come_from_config_defaults(self):
        spec = get_provider("anthropic")
        opts = spec.resolve_request_options({}, {})
        assert opts == {
            "max_tokens": DEFAULT_AI_MAX_TOKENS,
            "temperature": DEFAULT_AI_TEMPERATURE,
            "model": DEFAULT_AI_MODEL,
            "reasoning_effort": DEFAULT_AI_REASONING_EFFORT,
        }

    def test_overrides_win_over_config(self):
        spec = get_provider("anthropic")
        config = {"ai_model": "claude-a", "ai_max_tokens": 1000, "ai_temperature": 0.2, "ai_reasoning_effort": "low"}
        opts = spec.resolve_request_options(config, {"model": "claude-b", "max_tokens": 5, "reasoning_effort": "high"})
        assert opts == {"model": "claude-b", "max_tokens": 5, "temperature": 0.2, "reasoning_effort": "high"}

    def test_none_override_falls_back_to_config(self):
        spec = get_provider("anthropic")
        opts = spec.resolve_request_options({"ai_model": "claude-a"}, {"model": None, "max_tokens": None})
        assert opts["model"] == "claude-a"


# ---------------------------------------------------------------------------
# Fake provider: settings and inference with no wrapper change
# ---------------------------------------------------------------------------


class TestFakeProviderInference:
    def test_request_uses_spec_resolved_options(self, fake_provider):
        client = _fake_client()
        response = asyncio.run(client.async_request("System.", "User.", model="claude-ignored"))
        assert response.success is True
        assert response.content == "fake answer"
        backend = client._backend
        assert backend.calls[0]["model"] == "fake/model-b"
        assert backend.calls[0]["max_tokens"] == 0
        assert client.get_status()["status"] == "active"
        assert client.get_request_history()[0]["success"] is True

    def test_cost_not_tracked_and_budget_never_blocks(self, fake_provider):
        client = _fake_client(ai_monthly_budget=1)
        client._budget.monthly_cost = 999.0
        response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is True
        assert response.budget_exceeded is False
        assert client._budget.monthly_cost == 999.0

    def test_streaming_hides_usage_fields(self, fake_provider):
        client = _fake_client()

        async def _collect():
            return [e async for e in client.async_request_streaming("System.", "User.")]

        events = asyncio.run(_collect())
        assert [e["type"] for e in events] == ["text", "text", "stop"]
        assert events[-1] == {"type": "stop", "stop_reason": "end_turn", "truncated_empty": False}
        assert client.get_status()["total_requests"] == 1

    def test_model_not_found_never_triggers_tier_fallback(self, fake_provider):
        client = _fake_client()
        client._backend.errors = [
            AIBackendError("gone", "permanent", model_not_found=True) for _ in range(AI_MAX_RETRIES)
        ]
        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(client, "async_list_models", new_callable=AsyncMock) as list_models,
        ):
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert len(client._backend.calls) == AI_MAX_RETRIES
        list_models.assert_not_called()

    def test_retry_then_success(self, fake_provider):
        client = _fake_client()
        client._backend.errors = [AIBackendError("slow", "rate_limit")]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is True
        assert len(client._backend.calls) == 2


class TestNoRetryFlag:
    """AIBackendError(no_retry=True): never retried, never counted by the breaker."""

    @staticmethod
    def _breaker(client):
        cb = client._circuit_breaker
        return cb.consecutive_failures, cb.state

    def test_no_retry_not_retried_and_not_counted(self, fake_provider):
        client = _fake_client()
        before = self._breaker(client)
        client._backend.errors = [AIBackendError("limit", "quota", no_retry=True) for _ in range(AI_MAX_RETRIES)]
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert response.error
        assert len(client._backend.calls) == 1
        sleep.assert_not_called()
        assert self._breaker(client) == before
        assert client.get_status()["total_requests"] == 1

    def test_no_retry_repeated_never_opens_breaker(self, fake_provider):
        client = _fake_client()
        for _ in range(AI_CIRCUIT_BREAKER_THRESHOLD + 2):
            client._backend.errors = [AIBackendError("limit", "quota", no_retry=True)]
            asyncio.run(client.async_request("System.", "User."))
        assert client._circuit_breaker.consecutive_failures == 0
        assert client._circuit_breaker.state == "closed"

    def test_no_retry_stream_raises_and_not_counted(self, fake_provider):
        client = _fake_client()
        before = self._breaker(client)
        client._backend.errors = [AIBackendError("limit", "quota", no_retry=True)]

        async def _collect():
            return [e async for e in client.async_request_streaming("System.", "User.")]

        with pytest.raises(AIBackendError) as info:
            asyncio.run(_collect())
        assert info.value.category == "quota"
        assert len(client._backend.calls) == 1
        assert self._breaker(client) == before

    def test_transient_still_retries_and_counts(self, fake_provider):
        client = _fake_client()
        client._backend.errors = [AIBackendError("blip", "transient") for _ in range(AI_MAX_RETRIES)]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert len(client._backend.calls) == AI_MAX_RETRIES
        assert client._circuit_breaker.consecutive_failures == 1

    def test_transient_stream_still_counts(self, fake_provider):
        client = _fake_client()
        client._backend.errors = [AIBackendError("blip", "transient")]

        async def _collect():
            return [e async for e in client.async_request_streaming("System.", "User.")]

        with pytest.raises(AIBackendError):
            asyncio.run(_collect())
        assert client._circuit_breaker.consecutive_failures == 1

    def test_quota_without_flag_unchanged(self, fake_provider):
        """Anthropic's 402 maps to quota without the flag: still retried and counted."""
        client = _fake_client()
        client._backend.errors = [AIBackendError("pay", "quota") for _ in range(AI_MAX_RETRIES)]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(client.async_request("System.", "User."))
        assert len(client._backend.calls) == AI_MAX_RETRIES
        assert client._circuit_breaker.consecutive_failures == 1


class TestStopBetweenRetries:
    """stop_dispatch ends a request's further attempts; a call already in progress may finish."""

    @staticmethod
    def _failing_client(*, stop_when: str | None):
        client = _fake_client()
        backend = client._backend

        async def fail(**kwargs: Any) -> ClaudeResponse:
            backend.calls.append(kwargs)
            if stop_when == "attempt":
                client.stop_dispatch("stopped for this test")
            raise AIBackendError("down", "transient")

        backend.async_request = fail
        return client, backend

    def test_stop_during_an_attempt_ends_the_retries(self, fake_provider):
        client, backend = self._failing_client(stop_when="attempt")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))
        assert len(backend.calls) == 1
        assert response.success is False and response.error == "stopped for this test"
        assert client._circuit_breaker.consecutive_failures == 0

    def test_stop_during_the_backoff_ends_the_retries(self, fake_provider):
        client, backend = self._failing_client(stop_when=None)

        async def stop_while_waiting(_delay):
            client.stop_dispatch("stopped for this test")

        with patch("asyncio.sleep", stop_while_waiting):
            response = asyncio.run(client.async_request("System.", "User."))
        assert len(backend.calls) == 1 and response.error == "stopped for this test"

    def test_without_a_stop_every_attempt_runs(self, fake_provider):
        client, backend = self._failing_client(stop_when=None)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))
        assert len(backend.calls) == AI_MAX_RETRIES and "down" in response.error
        assert client._circuit_breaker.consecutive_failures == 1

    def test_stop_prevents_a_tier_fallback_call(self, monkeypatch):
        monkeypatch.setitem(PROVIDERS, "fake", dataclasses.replace(FAKE_SPEC, supports_tier_fallback=True))
        client = _fake_client(fake_model="claude-sonnet-old")
        backend = client._backend

        async def not_found(**kwargs: Any) -> ClaudeResponse:
            backend.calls.append(kwargs)
            client.stop_dispatch("stopped for this test")
            raise AIBackendError("gone", "permanent", model_not_found=True)

        async def live_models() -> list[str]:
            return ["claude-sonnet-new"]

        backend.async_request = not_found
        backend.async_list_models = live_models
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))
        assert [call["model"] for call in backend.calls] == ["claude-sonnet-old"]
        assert response.error == "stopped for this test"


class TestUnknownProvider:
    def test_unknown_provider_reports_error_and_never_raises(self):
        client = ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: "does-not-exist"})
        assert client.get_status()["status"] == "error"
        response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert "provider" in (response.error or "").lower()

        async def _stream():
            return [e async for e in client.async_request_streaming("System.", "User.")]

        with pytest.raises(RuntimeError):
            asyncio.run(_stream())

    def test_unknown_provider_connection_test_fails_cleanly(self):
        client = ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: "does-not-exist"})
        ok, _msg = asyncio.run(client.async_test_connection())
        assert ok is False

    def test_update_config_can_switch_provider(self, fake_provider):
        client = ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: "does-not-exist"})
        client.update_config({"ai_enabled": True, CONF_AI_PROVIDER: "fake", "fake_key": "k"})
        assert client.get_status()["status"] == "active"


# ---------------------------------------------------------------------------
# A provider whose key and model settings have their own names
# ---------------------------------------------------------------------------


@pytest.fixture
def own_fields_provider(monkeypatch):
    """Tier-fallback API-key provider using only its own key and model settings."""
    spec = dataclasses.replace(FAKE_SPEC, credential_setting="fake_key", supports_tier_fallback=True)
    monkeypatch.setitem(PROVIDERS, "fake", spec)
    return spec


class TestOwnCredentialAndModelFields:
    def test_builtin_credential_settings(self):
        from custom_components.climate_advisor.ai_providers import ensure_providers_registered

        ensure_providers_registered()
        assert get_provider("anthropic").credential_setting == "ai_api_key"
        assert get_provider("chatgpt").credential_setting is None  # credential_kind is account

    def test_coordinator_gate_reads_the_providers_own_key(self, own_fields_provider):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        gate = ClimateAdvisorCoordinator._ai_credentials_present
        assert gate({CONF_AI_PROVIDER: "fake", "fake_key": "k"})
        assert not gate({CONF_AI_PROVIDER: "fake"})
        assert not gate({CONF_AI_PROVIDER: "fake", "ai_api_key": "claude-key"})

    def test_coordinator_gate_unchanged_for_anthropic(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        gate = ClimateAdvisorCoordinator._ai_credentials_present
        assert gate({"ai_api_key": "k"}) and not gate({"fake_key": "k"})

    def test_fallback_detection_compares_the_providers_own_model(self, own_fields_provider):
        from custom_components.climate_advisor.ai_skills import _maybe_persist_model_fallback

        coordinator = MagicMock()
        coordinator.async_persist_model_fallback = AsyncMock()
        cfg = {CONF_AI_PROVIDER: "fake", "fake_model": "fake/model-b", "ai_model": "claude-old"}
        unchanged = MagicMock(resolved_model="fake/model-b")
        asyncio.run(_maybe_persist_model_fallback(coordinator, None, cfg, unchanged))
        coordinator.async_persist_model_fallback.assert_not_awaited()
        asyncio.run(_maybe_persist_model_fallback(coordinator, None, cfg, MagicMock(resolved_model="fake/model-c")))
        coordinator.async_persist_model_fallback.assert_awaited_once_with("fake/model-c")

    def test_fallback_is_persisted_to_the_providers_own_model(self, own_fields_provider):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        entry = MagicMock(data={CONF_AI_PROVIDER: "fake", "fake_model": "old", "ai_model": "claude-keep"})
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = dict(entry.data)
        coord._entry_id = "e1"
        coord.hass = MagicMock()
        coord.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        asyncio.run(coord.async_persist_model_fallback("new"))
        saved = coord.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert saved["fake_model"] == "new" and saved["ai_model"] == "claude-keep"
        assert coord.config["fake_model"] == "new" and coord.config["ai_model"] == "claude-keep"

    def test_investigator_config_context_shows_the_model_really_used_and_never_the_key(self, own_fields_provider):
        from custom_components.climate_advisor.ai_skills_context import build_config_context

        coordinator = MagicMock()
        coordinator.config = {
            CONF_AI_PROVIDER: "fake",
            "fake_model": "fake/model-b",
            "fake_key": "secret-value",
            "ai_model": "claude-old",
        }
        text = asyncio.run(build_config_context(MagicMock(), coordinator))
        assert "fake/model-b" in text and "claude-old" not in text and "secret-value" not in text

    def test_investigator_config_context_unchanged_for_anthropic(self):
        from custom_components.climate_advisor.ai_skills_context import build_config_context

        coordinator = MagicMock()
        coordinator.config = {"ai_model": "claude-sonnet-5"}
        assert "  ai_model:        claude-sonnet-5\n" in asyncio.run(build_config_context(MagicMock(), coordinator))

    def test_update_config_can_hand_an_account_client_to_the_new_backend(self, fake_provider):
        client = ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: "does-not-exist"})
        manager = object()
        client.update_config({"ai_enabled": True, CONF_AI_PROVIDER: "fake", "fake_key": "k"}, manager)
        assert client._backend.client is manager


# ---------------------------------------------------------------------------
# A provider switch while a request is pending or running
# ---------------------------------------------------------------------------

PLAN_OWNER = "owner-1"


class _PlanManager:
    state = "connected"
    owner_user_id = PLAN_OWNER


class _SwitchingBackend(FakeBackend):
    """A costed backend during whose request the zone switches to the plan provider."""

    on_call: Any = None

    async def async_request(self, **kwargs: Any) -> ClaudeResponse:
        self.calls.append(kwargs)
        self.on_call()
        if self.errors:
            raise self.errors.pop(0)
        return ClaudeResponse(
            success=True,
            content="answer",
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.5,
            latency_ms=1.0,
            resolved_model=kwargs["model"],
        )

    async def async_stream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(kwargs)
        self.on_call()
        yield {"type": "text", "text": "answer"}
        yield {
            "type": "stop",
            "stop_reason": "end_turn",
            "truncated_empty": False,
            "input_tokens": 1,
            "output_tokens": 1,
            "estimated_cost": 0.5,
        }


@pytest.fixture
def switching(monkeypatch):
    """(client, costed backend, plan backend): the client starts on ``costed``; ``switch()`` moves it to ``plan``."""
    plan_backend = FakeBackend({"fake_key": "k"}, _PlanManager())
    costed = dataclasses.replace(
        FAKE_SPEC,
        id="costed",
        tracks_cost=True,
        build_backend=lambda config, client=None: _SwitchingBackend(config, client),
    )
    plan = dataclasses.replace(
        FAKE_SPEC,
        id="plan",
        tracks_cost=False,
        plan_restricted=True,
        credential_kind="account",
        build_backend=lambda config, client=None: plan_backend,
    )
    monkeypatch.setitem(PROVIDERS, "costed", costed)
    monkeypatch.setitem(PROVIDERS, "plan", plan)
    client = ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: "costed", "fake_key": "k"})
    started_on = client._backend

    def switch():
        client.update_config({"ai_enabled": True, CONF_AI_PROVIDER: "plan", "fake_key": "k"})

    started_on.on_call = switch
    return client, started_on, plan_backend


class TestProviderPinnedPerRequest:
    def test_cost_of_a_request_finishing_after_the_switch_is_recorded(self, switching):
        client, _started_on, _plan = switching
        response = asyncio.run(client.async_request("S", "U"))
        assert response.success and client._spec.id == "plan"
        assert client._budget.monthly_cost == 0.5

    def test_cost_of_a_stream_finishing_after_the_switch_is_recorded(self, switching):
        client, _started_on, _plan = switching

        async def _stream():
            return [e async for e in client.async_request_streaming("S", "U")]

        asyncio.run(_stream())
        assert client._spec.id == "plan"
        assert client._budget.monthly_cost == 0.5

    def test_a_switch_during_an_attempt_ends_the_request_on_neither_provider(self, switching):
        client, started_on, plan = switching
        started_on.errors = [AIBackendError("busy", "transient")]
        with patch.object(claude_api.asyncio, "sleep", AsyncMock()):
            response = asyncio.run(client.async_request("S", "U"))
        assert not response.success and response.no_retry
        assert response.error == claude_api._BACKEND_REPLACED_MESSAGE
        assert len(started_on.calls) == 1 and plan.calls == []
        assert client._budget.monthly_cost == 0
        assert client._circuit_breaker.consecutive_failures == 0


class _BoundBackend(FakeBackend):
    """Hands each request a copy, as ChatGPTBackend does; the copy shares ``calls`` and ``errors``."""

    def for_request(self) -> _BoundBackend:
        return copy.copy(self)


class TestBackendReplacedDuringRetries:
    """A request stays on the backend it started on, and ends once that is no longer the zone's backend."""

    @pytest.fixture
    def providers(self, monkeypatch):
        """Two providers, one whose requests hold the backend itself and one that holds a copy of it."""
        built = {"direct": FakeBackend({"fake_key": "k"}), "bound": _BoundBackend({"fake_key": "k"})}
        for provider_id, backend in built.items():
            monkeypatch.setitem(
                PROVIDERS,
                provider_id,
                dataclasses.replace(
                    FAKE_SPEC,
                    id=provider_id,
                    supports_tier_fallback=True,
                    build_backend=lambda config, client=None, b=backend: b,
                ),
            )
        return built

    @staticmethod
    def _client(provider_id: str) -> ClaudeAPIClient:
        return ClaudeAPIClient({"ai_enabled": True, CONF_AI_PROVIDER: provider_id, "fake_key": "k"})

    @staticmethod
    def _config(provider_id: str) -> dict[str, Any]:
        return {"ai_enabled": True, CONF_AI_PROVIDER: provider_id, "fake_key": "k"}

    @pytest.mark.parametrize(("old_id", "new_id"), [("direct", "bound"), ("bound", "direct")])
    def test_a_switch_during_the_backoff_sends_nothing_further(self, providers, old_id, new_id):
        client = self._client(old_id)
        old, new = providers[old_id], providers[new_id]
        old.errors = [AIBackendError("busy", "transient") for _ in range(AI_MAX_RETRIES)]

        async def switch_while_waiting(_delay):
            client.update_config(self._config(new_id))

        with patch.object(claude_api.asyncio, "sleep", switch_while_waiting):
            response = asyncio.run(client.async_request("S", "U"))
        assert len(old.calls) == 1 and new.calls == []
        assert not response.success and response.no_retry
        assert response.error == claude_api._BACKEND_REPLACED_MESSAGE
        assert client._circuit_breaker.consecutive_failures == 0

    @pytest.mark.parametrize("provider_id", ["direct", "bound"])
    def test_with_no_switch_every_attempt_runs(self, providers, provider_id):
        client = self._client(provider_id)
        backend = providers[provider_id]
        backend.errors = [AIBackendError("busy", "transient") for _ in range(AI_MAX_RETRIES)]
        with patch.object(claude_api.asyncio, "sleep", AsyncMock()):
            response = asyncio.run(client.async_request("S", "U"))
        assert len(backend.calls) == AI_MAX_RETRIES and "busy" in response.error
        assert client._circuit_breaker.consecutive_failures == 1

    def test_a_config_update_keeping_the_backend_does_not_end_the_retries(self, providers):
        client = self._client("direct")
        backend = providers["direct"]
        backend.errors = [AIBackendError("busy", "transient")]

        async def update_while_waiting(_delay):
            client.update_config(self._config("direct"))

        with patch.object(claude_api.asyncio, "sleep", update_while_waiting):
            assert asyncio.run(client.async_request("S", "U")).success
        assert len(backend.calls) == 2

    def test_a_same_provider_rebuild_ends_the_retries(self, providers):
        client = self._client("direct")
        backend = providers["direct"]
        rebuilt = FakeBackend({"fake_key": "k"})
        backend.errors = [AIBackendError("busy", "transient")]

        async def rebuild_while_waiting(_delay):
            client.update_config(self._config("direct"), backend=rebuilt)

        with patch.object(claude_api.asyncio, "sleep", rebuild_while_waiting):
            response = asyncio.run(client.async_request("S", "U"))
        assert len(backend.calls) == 1 and rebuilt.calls == []
        assert response.no_retry and response.error == claude_api._BACKEND_REPLACED_MESSAGE

    @pytest.mark.parametrize("switch_during", ["attempt", "model_list"])
    def test_a_switch_prevents_a_tier_fallback_call(self, providers, switch_during):
        client = self._client("direct")
        client._config["fake_model"] = "claude-sonnet-old"
        old, new = providers["direct"], providers["bound"]

        def switch():
            client.update_config(self._config("bound"))

        async def not_found(**kwargs: Any) -> ClaudeResponse:
            old.calls.append(kwargs)
            if switch_during == "attempt":
                switch()
            raise AIBackendError("gone", "permanent", model_not_found=True)

        async def live_models() -> list[str]:
            if switch_during == "model_list":
                switch()
            return ["claude-sonnet-new"]

        old.async_request = not_found
        old.async_list_models = live_models
        with patch.object(claude_api.asyncio, "sleep", AsyncMock()):
            response = asyncio.run(client.async_request("S", "U"))
        assert [call["model"] for call in old.calls] == ["claude-sonnet-old"] and new.calls == []
        assert response.no_retry and response.error == claude_api._BACKEND_REPLACED_MESSAGE
        assert client._circuit_breaker.consecutive_failures == 0

    def test_requests_after_the_switch_use_the_new_provider(self, providers):
        client = self._client("direct")
        old, new = providers["direct"], providers["bound"]
        old.errors = [AIBackendError("busy", "transient")]

        async def switch_while_waiting(_delay):
            client.update_config(self._config("bound"))

        with patch.object(claude_api.asyncio, "sleep", switch_while_waiting):
            asyncio.run(client.async_request("S", "U"))
        assert asyncio.run(client.async_request("S", "U")).success
        assert len(old.calls) == 1 and len(new.calls) == 1


class TestRequesterCheckedAtDispatch:
    def _plan_client(self, switching):
        client, _started_on, plan = switching
        client.update_config({"ai_enabled": True, CONF_AI_PROVIDER: "plan", "fake_key": "k"})
        return client, plan

    def test_non_owner_is_refused_and_nothing_is_sent(self, switching):
        client, plan = self._plan_client(switching)
        with pytest.raises(AIUserNotAllowedError):
            asyncio.run(client.async_request("S", "U", requester_id="someone-else"))
        assert plan.calls == [] and client._total_requests == 0

    def test_non_owner_stream_is_refused_and_nothing_is_sent(self, switching):
        client, plan = self._plan_client(switching)

        async def _stream():
            return [e async for e in client.async_request_streaming("S", "U", requester_id="someone-else")]

        with pytest.raises(AIUserNotAllowedError):
            asyncio.run(_stream())
        assert plan.calls == [] and client._circuit_breaker.consecutive_failures == 0

    def test_no_user_at_all_is_refused(self, switching):
        client, plan = self._plan_client(switching)
        with pytest.raises(AIUserNotAllowedError):
            asyncio.run(client.async_request("S", "U", requester_id=None))
        assert plan.calls == []

    def test_owner_is_sent(self, switching):
        client, plan = self._plan_client(switching)
        assert asyncio.run(client.async_request("S", "U", requester_id=PLAN_OWNER)).success
        assert len(plan.calls) == 1

    def test_unrestricted_provider_allows_anyone(self, switching):
        client, _started_on, _plan = switching
        assert asyncio.run(client.async_request("S", "U", requester_id="someone-else")).success


def test_fallback_is_not_persisted_when_the_provider_changed_during_the_request(monkeypatch):
    """A Claude substitution must not be written over the provider the zone has since switched to."""
    from custom_components.climate_advisor.ai_skills import AISkillDefinition, AISkillRegistry

    monkeypatch.setitem(PROVIDERS, "fake", dataclasses.replace(FAKE_SPEC, supports_tier_fallback=True))
    monkeypatch.setitem(PROVIDERS, "plan", dataclasses.replace(FAKE_SPEC, id="plan", model_setting="plan_model"))
    coordinator = MagicMock()
    coordinator.config = {CONF_AI_PROVIDER: "fake", "fake_model": "m1"}
    coordinator.async_persist_model_fallback = AsyncMock()

    async def request(**_kwargs):
        coordinator.config[CONF_AI_PROVIDER] = "plan"
        return MagicMock(success=True, content="x", resolved_model="m2")

    registry = AISkillRegistry()
    registry.register(
        AISkillDefinition(
            name="s",
            description="d",
            system_prompt="p",
            context_builder=AsyncMock(return_value="c"),
            response_parser=lambda raw: {},
        )
    )
    client = MagicMock(async_request=AsyncMock(side_effect=request))
    asyncio.run(registry.async_execute("s", MagicMock(), coordinator, client))
    coordinator.async_persist_model_fallback.assert_not_awaited()


def test_a_provider_must_declare_its_own_key_and_model_settings():
    required = {f.name for f in dataclasses.fields(ProviderSpec) if f.default is dataclasses.MISSING}
    assert {"credential_setting", "model_setting"} <= required


def test_import_failure_of_the_sdk_of_any_kind_leaves_the_module_usable(monkeypatch):
    """A broken ``anthropic`` raises more than ImportError (a pydantic mismatch, for one)."""
    import importlib.util

    class Broken:
        def __getattr__(self, name):
            raise TypeError("broken SDK")

    monkeypatch.setitem(sys.modules, "anthropic", Broken())
    name = "custom_components.climate_advisor._claude_api_probe"
    spec = importlib.util.spec_from_file_location(name, claude_api.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert module.ANTHROPIC_AVAILABLE is False and module.NotFoundError is None


def test_no_answer_is_not_replaced_by_a_fallback_report():
    from custom_components.climate_advisor.ai_skills import AISkillDefinition, AISkillRegistry

    fallback = MagicMock(return_value={})
    registry = AISkillRegistry()
    registry.register(
        AISkillDefinition(
            name="s",
            description="d",
            system_prompt="p",
            context_builder=AsyncMock(return_value="c"),
            response_parser=lambda raw: {},
            fallback=fallback,
        )
    )
    declined = ClaudeResponse(
        success=False,
        content="",
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0.0,
        latency_ms=1.0,
        error="declined",
        no_answer=True,
    )
    client = MagicMock(async_request=AsyncMock(return_value=declined))
    result = asyncio.run(registry.async_execute("s", MagicMock(config={}), MagicMock(config={}), client))
    assert result["source"] == "error" and result["error"] == "declined"
    fallback.assert_not_called()


@pytest.mark.parametrize(
    ("config", "text"),
    [({}, "Sending context to Claude AI…"), ({CONF_AI_PROVIDER: "fake"}, "Sending context to Fake Router…")],
    ids=["claude", "other_provider"],
)
def test_sending_status_text(fake_provider, config, text):
    from custom_components.climate_advisor.ai_skills import AISkillDefinition, AISkillRegistry

    async def context(*_args, **_kwargs):
        return "c"

    async def stream(**_kwargs):
        yield {"type": "text", "text": "x"}

    registry = AISkillRegistry()
    registry.register(
        AISkillDefinition(
            name="s", description="d", system_prompt="p", context_builder=context, response_parser=lambda raw: {}
        )
    )
    client = MagicMock(async_request_streaming=stream)

    async def go():
        return [e async for e in registry.async_execute_streaming("s", MagicMock(), MagicMock(config=config), client)]

    assert any(e.get("text") == text for e in asyncio.run(go()))


def test_provider_imports_use_absolute_names(monkeypatch):
    """HA's blocking-import check only passes a loaded module when given its absolute name."""
    import importlib

    from custom_components.climate_advisor import ai_providers

    names: list[str] = []
    monkeypatch.setattr(importlib, "import_module", lambda name, package=None: names.append(name))
    ai_providers.ensure_providers_registered()
    assert names
    assert all(name.startswith("custom_components.climate_advisor.") for name in names)


def test_investigator_fallback_names_the_zones_provider():
    from types import SimpleNamespace

    from custom_components.climate_advisor.ai_providers import ensure_providers_registered
    from custom_components.climate_advisor.ai_skills_investigator import investigation_fallback

    ensure_providers_registered()
    expected = {
        "anthropic": "AI unavailable — hypotheses require cross-source analysis by Claude.",
        "chatgpt": "AI unavailable — hypotheses require cross-source analysis by the AI provider.",
    }
    for provider, text in expected.items():
        result = investigation_fallback(SimpleNamespace(config={"ai_provider": provider}))
        assert result["hypotheses"] == text
    assert investigation_fallback(SimpleNamespace(config={}))["hypotheses"] == expected["anthropic"]
