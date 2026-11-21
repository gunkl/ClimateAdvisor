"""Coverage for ClaudeAPIClient's guard, retry, circuit-breaker and status paths (claude_api.py).

Occupant view: when the built-in Claude provider is unavailable (no package, no key, rate limited, over budget,
repeatedly failing, timing out, retired model) the AI Investigator fails fast with a reason, falls back, or recovers
on its own, and the dashboard's AI status says why. No real network call is made: the anthropic SDK is not
installed for the unit tier, so its error classes and client are stood in for on the imported module (CLAUDE.md
"Module-Level sys.modules Mocking"). ``claude_api`` is imported lazily, at test time, never at collection.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor.const import (  # noqa: E402
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MAX_RETRIES,
    AI_MODELS,
)

_mod = None  # custom_components.climate_advisor.claude_api, bound by the ``sdk`` fixture


class _APIError(Exception):
    pass


class _NotFound(_APIError):
    pass


class _AuthError(_APIError):
    pass


class _RateLimit(Exception):
    pass


class _Timeout(Exception):
    pass


@pytest.fixture
def sdk(monkeypatch):
    global _mod
    _mod = importlib.import_module("custom_components.climate_advisor.claude_api")
    monkeypatch.setattr(_mod, "ANTHROPIC_AVAILABLE", True)
    monkeypatch.setattr(_mod, "APIError", _APIError)
    monkeypatch.setattr(_mod, "NotFoundError", _NotFound)
    monkeypatch.setattr(_mod, "AuthenticationError", _AuthError)
    monkeypatch.setattr(_mod, "RateLimitError", _RateLimit)
    monkeypatch.setattr(_mod, "APITimeoutError", _Timeout)
    monkeypatch.setattr(_mod, "AI_RETRY_BASE_DELAY_SECONDS", 0)
    factory = MagicMock(name="AsyncAnthropic")
    monkeypatch.setattr(_mod, "AsyncAnthropic", factory)
    return factory


_CFG = {
    "ai_enabled": True,
    "ai_provider": "builtin_claude",
    "ai_api_key": "k",
    "ai_model": "claude-sonnet-4-6",
    "ai_max_tokens": 4096,
}


def _client(api=None, **overrides):
    return _mod.ClaudeAPIClient({**_CFG, **overrides}, client=api if api is not None else MagicMock())


def _msg(text="answer", stop="end_turn", blocks=None):
    msg = MagicMock()
    msg.content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    msg.usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    msg.stop_reason = stop
    return msg


class _Stream:
    """Async context manager standing in for ``client.messages.stream(...)``."""

    def __init__(self, events, final=None):
        self._events = events
        self._final = final or _msg()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for e in self._events:
                yield e

        return _gen()

    async def get_final_message(self):
        return self._final


def _collect(client, **kwargs):
    async def _run():
        return [e async for e in client.async_request_streaming("System.", "User.", **kwargs)]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Import without the anthropic package
# ---------------------------------------------------------------------------


def test_module_loads_without_the_anthropic_package():
    """An install without ``anthropic`` still loads; built-in Claude is simply unavailable."""
    path = importlib.util.find_spec("custom_components.climate_advisor.claude_api").origin
    name = "custom_components.climate_advisor._claude_api_no_sdk"
    spec = importlib.util.spec_from_file_location(name, path)
    copy = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"anthropic": None, name: copy}):
        spec.loader.exec_module(copy)
    assert copy.ANTHROPIC_AVAILABLE is False
    assert copy.AsyncAnthropic is None
    assert copy.APIError is Exception and copy.RateLimitError is Exception and copy.APITimeoutError is Exception
    assert copy.NotFoundError is None and copy.AuthenticationError is None
    # No key and no SDK: no client is built, and a request fails with a reason instead of raising.
    client = copy.ClaudeAPIClient(dict(_CFG))
    response = asyncio.run(client.async_request("System.", "User."))
    assert response.success is False
    assert response.error == "Anthropic client not initialized (missing package or API key)"
    ok, message = asyncio.run(client.async_test_connection())
    assert (ok, message) == (False, "anthropic package is not installed")


# ---------------------------------------------------------------------------
# Model list
# ---------------------------------------------------------------------------


def test_fetch_models_builds_the_client_in_the_executor(sdk):
    sdk.return_value.models.list = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(id="claude-sonnet-x")]))
    hass = MagicMock()

    async def _executor(fn):
        return fn()

    hass.async_add_executor_job = _executor
    assert asyncio.run(_mod.fetch_available_models("k", hass)) == ["claude-sonnet-x"]
    sdk.assert_called_once_with(api_key="k")


# ---------------------------------------------------------------------------
# Non-streaming request
# ---------------------------------------------------------------------------


class TestRequest:
    def test_builtin_without_key_fails_with_reason(self, sdk):
        client = _mod.ClaudeAPIClient({**_CFG, "ai_api_key": ""})
        response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert response.error == "Anthropic client not initialized (missing package or API key)"

    def test_timeouts_are_retried_then_reported(self, sdk):
        api = MagicMock()
        api.messages.create = AsyncMock(side_effect=_Timeout("slow"))
        response = asyncio.run(_client(api).async_request("System.", "User."))
        assert response.success is False and response.error == "Timeout error: slow"
        assert api.messages.create.await_count == AI_MAX_RETRIES

    def test_blocks_without_text_are_skipped(self, sdk):
        api = MagicMock()
        api.messages.create = AsyncMock(
            return_value=_msg(blocks=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="hi")])
        )
        response = asyncio.run(_client(api).async_request("System.", "User."))
        assert response.success is True and response.content == "hi"

    def test_retired_model_without_a_tier_is_not_substituted(self, sdk, caplog):
        api = MagicMock()
        api.messages.create = AsyncMock(side_effect=_NotFound("404 model"))
        client = _client(api, ai_model="mystery-model")
        with caplog.at_level(logging.WARNING):
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False and response.error.startswith("API error")
        assert any("capability tier could not be determined" in r.message for r in caplog.records)
        assert api.messages.create.await_count == AI_MAX_RETRIES

    def test_failed_same_tier_replacement_falls_through(self, sdk, caplog):
        api = MagicMock()
        api.messages.create = AsyncMock(side_effect=_NotFound("404 model"))
        client = _client(api)
        client.async_list_models = AsyncMock(return_value=["claude-sonnet-new", "claude-sonnet-4-6"])
        with caplog.at_level(logging.WARNING):
            response = asyncio.run(client.async_request("System.", "User."))
        assert response.success is False
        assert any("Fallback model 'claude-sonnet-new' also failed" in r.message for r in caplog.records)
        # Each attempt: the original model, then the one replacement.
        assert api.messages.create.await_count == 2 * AI_MAX_RETRIES

    def test_auth_failure_without_hook_still_stops_retrying(self, sdk):
        api = MagicMock()
        api.messages.create = AsyncMock(side_effect=_AuthError("401"))
        client = _client(api)
        assert client.on_auth_failed is None
        response = asyncio.run(client.async_request("System.", "User."))
        assert response.error == "Authentication failed: 401"
        assert api.messages.create.await_count == 1


# ---------------------------------------------------------------------------
# Streaming request
# ---------------------------------------------------------------------------


class TestStreamingGuards:
    def test_open_breaker_blocks(self, sdk):
        client = _client()
        client._circuit_breaker.state = _mod._CB_OPEN
        client._circuit_breaker.opened_at = time.monotonic()
        with pytest.raises(RuntimeError, match="Circuit breaker open"):
            _collect(client)

    def test_rate_limit_blocks(self, sdk):
        client = _client(ai_manual_requests_per_day=0)
        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            _collect(client)

    def test_budget_blocks(self, sdk):
        client = _client(ai_monthly_budget=1)
        client._budget.monthly_cost = 5.0
        with pytest.raises(RuntimeError, match="Monthly budget exceeded"):
            _collect(client)

    def test_missing_client_blocks(self, sdk):
        client = _mod.ClaudeAPIClient({**_CFG, "ai_api_key": ""})
        with pytest.raises(RuntimeError, match="not initialized"):
            _collect(client)


class TestStreaming:
    def test_non_delta_and_unknown_delta_events_are_skipped(self, sdk):
        events = [
            SimpleNamespace(type="message_start"),
            SimpleNamespace(type="content_block_delta", delta=None),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="input_json_delta")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="t")),
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="hello")),
        ]
        api = MagicMock()
        api.messages.stream = MagicMock(return_value=_Stream(events))
        out = _collect(_client(api))
        assert out == [
            {"type": "thinking", "text": "t"},
            {"type": "text", "text": "hello"},
            {"type": "stop", "stop_reason": "end_turn", "truncated_empty": False},
        ]

    def test_repeated_failures_open_the_breaker(self, sdk):
        api = MagicMock()
        api.messages.stream = MagicMock(side_effect=_RateLimit("429"))
        client = _client(api)
        for _ in range(AI_CIRCUIT_BREAKER_THRESHOLD):
            with pytest.raises(_RateLimit):
                _collect(client)
        assert client._circuit_breaker.state == _mod._CB_OPEN
        assert client.get_status()["status"] == "circuit_open"

    def test_success_after_cooldown_closes_the_breaker_and_counts_auto(self, sdk):
        api = MagicMock()
        api.messages.stream = MagicMock(return_value=_Stream([]))
        client = _client(api)
        client._circuit_breaker.state = _mod._CB_OPEN
        client._circuit_breaker.opened_at = time.monotonic() - AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS - 1
        _collect(client, triggered_by="auto")
        assert client._circuit_breaker.state == _mod._CB_CLOSED
        assert client._rate_counters.auto_requests_today == 1
        assert client._rate_counters.manual_requests_today == 0

    def test_half_open_lets_the_probe_through(self, sdk):
        client = _client()
        client._circuit_breaker.state = _mod._CB_HALF_OPEN
        assert client._check_circuit_breaker() is True


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


class TestConnection:
    def test_no_key(self, sdk):
        client = _mod.ClaudeAPIClient({**_CFG, "ai_api_key": ""})
        assert asyncio.run(client.async_test_connection()) == (False, "No API key configured")

    def test_rate_limited_key_is_valid_and_client_built_in_executor(self, sdk):
        sdk.return_value.messages.create = AsyncMock(side_effect=_RateLimit("429"))
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())
        result = asyncio.run(_client().async_test_connection(hass))
        assert result == (True, "Connected successfully (rate limited)")
        hass.async_add_executor_job.assert_awaited_once()

    def test_api_error_is_reported(self, sdk):
        sdk.return_value.messages.create = AsyncMock(side_effect=_APIError("400 bad"))
        assert asyncio.run(_client().async_test_connection()) == (False, "400 bad")


# ---------------------------------------------------------------------------
# Status, config, budget, cost, skill name
# ---------------------------------------------------------------------------


class TestStatus:
    def test_disabled(self, sdk):
        assert _client(ai_enabled=False).get_status()["status"] == "disabled"

    def test_budget_exceeded(self, sdk):
        client = _client(ai_monthly_budget=1)
        client._budget.monthly_cost = 2.0
        assert client.get_status()["status"] == "budget_exceeded"

    def test_error_after_a_failure(self, sdk):
        client = _client()
        client._error_count = 1
        client._circuit_breaker.consecutive_failures = 1
        assert client.get_status()["status"] == "error"


def test_removing_the_key_deactivates_the_client(sdk):
    client = _client()
    client.update_config({**_CFG, "ai_api_key": ""})
    assert client._client is None
    assert client.get_status()["status"] == "inactive"


def test_new_month_resets_the_budget(sdk):
    client = _client(ai_monthly_budget=1)
    client._budget.monthly_cost = 5.0
    client._budget.budget_month = (date.today().month % 12) + 1
    assert client._check_budget() is True
    assert client._budget.monthly_cost == 0.0 and client._budget.budget_month == date.today().month


def test_unknown_model_is_costed_at_sonnet_rates(sdk):
    client = _client()
    assert client._estimate_cost("mystery", 1_000_000, 0) == client._estimate_cost("claude-sonnet-4-6", 1_000_000, 0)


def test_adaptive_model_with_temperature_sends_temperature_one(sdk, monkeypatch):
    monkeypatch.setitem(
        _mod.AI_MODEL_CAPABILITIES, "claude-test-adaptive", {"thinking_shape": "adaptive", "supports_temperature": True}
    )
    kwargs = _client()._build_request_kwargs(
        model="claude-test-adaptive",
        max_tokens=100,
        temperature=0.3,
        reasoning_effort="low",
        system_prompt="s",
        user_message="u",
    )
    assert kwargs["temperature"] == 1
    assert kwargs["thinking"] == {"type": "adaptive"} and kwargs["output_config"] == {"effort": "low"}


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [("skill: investigator\nrest", "investigator"), ("skill:   \nskill_name: other", "other"), ("none", "unknown")],
)
def test_skill_name_extraction(sdk, prompt, expected):
    assert _mod.ClaudeAPIClient._extract_skill_name(prompt) == expected


def test_static_models_when_no_key(sdk):
    assert asyncio.run(_mod.fetch_available_models("")) == list(AI_MODELS)
