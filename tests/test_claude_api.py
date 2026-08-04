"""Tests for the Claude API client (ClaudeAPIClient)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Inject a mock anthropic package before claude_api.py is imported so that
# ANTHROPIC_AVAILABLE=True and we can control AsyncAnthropic behaviour.
_mock_anthropic = MagicMock()
_mock_anthropic.__name__ = "anthropic"
_mock_anthropic.__path__ = []
_mock_anthropic.__file__ = None
_mock_anthropic.__spec__ = None
_mock_anthropic.__loader__ = None
_mock_anthropic.__package__ = "anthropic"

# Minimal exception stubs so claude_api.py can do 'from anthropic import APIError …'
_mock_anthropic.APIError = type("APIError", (Exception,), {})
_mock_anthropic.APITimeoutError = type("APITimeoutError", (Exception,), {})
_mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
_mock_anthropic.NotFoundError = type("NotFoundError", (_mock_anthropic.APIError,), {})
_mock_anthropic.AsyncAnthropic = MagicMock()

sys.modules["anthropic"] = _mock_anthropic

# Now it is safe to import the module under test.
from custom_components.climate_advisor.claude_api import (  # noqa: E402
    APIError as _ClaudeApiAPIError,
)
from custom_components.climate_advisor.claude_api import (  # noqa: E402
    ClaudeAPIClient,
    detect_model_tier,
    fetch_available_models,
)
from custom_components.climate_advisor.claude_api import (  # noqa: E402
    NotFoundError as _ClaudeApiNotFoundError,
)

# NOTE: claude_api.py's module-level `NotFoundError` name is bound to whichever test
# file's mocked `anthropic` module happened to be in sys.modules the FIRST time
# claude_api.py was imported in this test session — not necessarily this file's own
# _mock_anthropic (see CLAUDE.md's "Module-Level sys.modules Mocking" testing note).
# Tests that need to trigger the isinstance(exc, NotFoundError) branch must raise
# _ClaudeApiNotFoundError (the identity claude_api.py actually holds), not
# _mock_anthropic.NotFoundError.
from custom_components.climate_advisor.const import (  # noqa: E402
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MODELS,
    AI_REQUEST_HISTORY_CAP,
    DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
    DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_KEY = "sk-ant-test-key-12345"


def _make_config(**overrides) -> dict:
    """Return a minimal config dict suitable for ClaudeAPIClient."""
    config = {
        "ai_enabled": True,
        "ai_api_key": _TEST_KEY,
        "ai_model": "claude-sonnet-4-6",
        "ai_reasoning_effort": "medium",
        "ai_max_tokens": 4096,
        "ai_temperature": 0.3,
        "ai_monthly_budget": 0,
        "ai_auto_requests_per_day": DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
        "ai_manual_requests_per_day": DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
    }
    config.update(overrides)
    return config


def _mock_message(
    content_text: str = "test response",
    input_tokens: int = 10,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Build a mock anthropic Message response."""
    msg = MagicMock()
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = content_text
    msg.content = [content_block]
    msg.usage = MagicMock()
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
    msg.stop_reason = stop_reason
    return msg


def _make_client(mock_api_client: MagicMock, **config_overrides) -> ClaudeAPIClient:
    """Create a ClaudeAPIClient with a pre-injected mock API client."""
    return ClaudeAPIClient(config=_make_config(**config_overrides), client=mock_api_client)


def _make_mock_api_client() -> MagicMock:
    """Return a MagicMock that looks like an AsyncAnthropic instance."""
    mock = MagicMock()
    mock.messages = MagicMock()
    mock.messages.create = AsyncMock()
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSuccessfulRequest:
    """Basic happy-path: one successful API call."""

    def test_successful_request(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message("hello world", 100, 200)

        client = _make_client(mock_api)
        response = asyncio.run(client.async_request("System prompt.", "User message."))

        assert response.success is True
        assert response.content == "hello world"
        assert response.input_tokens == 100
        assert response.output_tokens == 200
        assert response.estimated_cost > 0
        assert response.error is None
        assert response.rate_limited is False
        assert response.circuit_open is False
        assert response.budget_exceeded is False


class TestRetryBehaviour:
    """Retry logic: partial failures and total exhaustion."""

    def test_retry_on_failure_then_success(self):
        """Fails on first two attempts, succeeds on the third."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = [
            Exception("API error attempt 1"),
            Exception("API error attempt 2"),
            _mock_message("recovered"),
        ]

        client = _make_client(mock_api)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert response.content == "recovered"
        assert mock_api.messages.create.call_count == 3

    def test_retry_exhaustion(self):
        """All three attempts fail — response should indicate failure."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = Exception("always fails")

        client = _make_client(mock_api)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is False
        assert response.error is not None
        assert "always fails" in response.error
        assert mock_api.messages.create.call_count == 3


class TestCircuitBreaker:
    """Circuit breaker trips after threshold failures and blocks subsequent calls."""

    def _exhaust_circuit_breaker(self, client: ClaudeAPIClient, mock_api: MagicMock) -> None:
        """Make AI_CIRCUIT_BREAKER_THRESHOLD failed requests to trip the breaker."""
        mock_api.messages.create.side_effect = Exception("forced failure")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            for _ in range(AI_CIRCUIT_BREAKER_THRESHOLD):
                asyncio.run(client.async_request("S.", "U."))

    def test_circuit_breaker_trips(self):
        """After threshold failures the next request returns circuit_open=True without an API call."""
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        self._exhaust_circuit_breaker(client, mock_api)

        # Reset the side_effect so any call would succeed — but it shouldn't be called.
        mock_api.messages.create.side_effect = None
        mock_api.messages.create.return_value = _mock_message()
        call_count_before = mock_api.messages.create.call_count

        response = asyncio.run(client.async_request("S.", "U."))

        assert response.circuit_open is True
        assert response.success is False
        # No new API calls should have been made.
        assert mock_api.messages.create.call_count == call_count_before

    def test_circuit_breaker_resets_on_success(self):
        """After cooldown elapses a probe request succeeds and the breaker closes."""
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        self._exhaust_circuit_breaker(client, mock_api)

        # Simulate cooldown elapsed by backdating the opened_at timestamp.
        from custom_components.climate_advisor.const import AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS

        client._circuit_breaker.opened_at = time.monotonic() - AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS - 1

        mock_api.messages.create.side_effect = None
        mock_api.messages.create.return_value = _mock_message("probe success")

        response = asyncio.run(client.async_request("S.", "U."))

        assert response.success is True
        assert response.circuit_open is False
        # Breaker should have returned to closed state.
        assert client._circuit_breaker.state == "closed"


class TestRateLimiter:
    """Daily request counters prevent over-use."""

    def test_rate_limiter_manual(self):
        """Exceeding DEFAULT_AI_MANUAL_REQUESTS_PER_DAY returns rate_limited=True."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message()
        client = _make_client(mock_api)

        # Consume the full daily manual allowance.
        for _ in range(DEFAULT_AI_MANUAL_REQUESTS_PER_DAY):
            asyncio.run(client.async_request("S.", "U.", triggered_by="manual"))

        response = asyncio.run(client.async_request("S.", "U.", triggered_by="manual"))

        assert response.rate_limited is True
        assert response.success is False

    def test_rate_limiter_auto(self):
        """Exceeding DEFAULT_AI_AUTO_REQUESTS_PER_DAY with triggered_by=auto returns rate_limited=True."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message()
        client = _make_client(mock_api)

        for _ in range(DEFAULT_AI_AUTO_REQUESTS_PER_DAY):
            asyncio.run(client.async_request("S.", "U.", triggered_by="auto"))

        response = asyncio.run(client.async_request("S.", "U.", triggered_by="auto"))

        assert response.rate_limited is True
        assert response.success is False

    def test_rate_limiter_daily_reset(self):
        """After the date advances the counters reset and requests are permitted again."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message()
        client = _make_client(mock_api)

        # Exhaust the manual limit.
        for _ in range(DEFAULT_AI_MANUAL_REQUESTS_PER_DAY):
            asyncio.run(client.async_request("S.", "U.", triggered_by="manual"))

        # Confirm rate-limited before date change.
        response_before = asyncio.run(client.async_request("S.", "U.", triggered_by="manual"))
        assert response_before.rate_limited is True

        # Advance the date by one day.
        from datetime import timedelta

        tomorrow = date.today() + timedelta(days=1)
        with patch("custom_components.climate_advisor.claude_api.date") as mock_date:
            mock_date.today.return_value = tomorrow

            response_after = asyncio.run(client.async_request("S.", "U.", triggered_by="manual"))

        assert response_after.rate_limited is False
        assert response_after.success is True


class TestBudgetTracking:
    """Monthly budget cap blocks requests once exceeded."""

    def test_budget_tracking(self):
        """Requests accumulate cost; once monthly_budget is exceeded, budget_exceeded=True."""
        mock_api = _make_mock_api_client()
        # Each call returns 1 000 000 input tokens — at $3/M that's $3 per call.
        mock_api.messages.create.return_value = _mock_message(input_tokens=1_000_000, output_tokens=0)

        # Set a tiny budget of $2 so the second request tips it over.
        client = _make_client(mock_api, ai_monthly_budget=2)

        # First request should succeed and accumulate ~$3.
        r1 = asyncio.run(client.async_request("S.", "U."))
        assert r1.success is True

        # Second request — budget should now be exceeded.
        r2 = asyncio.run(client.async_request("S.", "U."))
        assert r2.budget_exceeded is True
        assert r2.success is False


class TestRequestHistoryCap:
    """request_history deque never exceeds AI_REQUEST_HISTORY_CAP entries."""

    def test_request_history_cap(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message()
        # Set a manual limit large enough that the rate limiter never fires
        # before we hit the history cap.
        extra = 10
        total = AI_REQUEST_HISTORY_CAP + extra
        client = _make_client(mock_api, ai_manual_requests_per_day=total + 1)

        for _ in range(total):
            asyncio.run(client.async_request("S.", "U."))

        assert len(client.request_history) == AI_REQUEST_HISTORY_CAP


class TestSecurityDataExposure:
    """API key must never appear in status or history output."""

    def test_api_key_never_in_status(self):
        """get_status() must not contain the API key in any value."""
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        status = client.get_status()

        def _all_string_values(d) -> list[str]:
            values = []
            if isinstance(d, dict):
                for v in d.values():
                    values.extend(_all_string_values(v))
            elif isinstance(d, (list, tuple)):
                for item in d:
                    values.extend(_all_string_values(item))
            elif isinstance(d, str):
                values.append(d)
            return values

        for val in _all_string_values(status):
            assert _TEST_KEY not in val, f"API key found in status value: {val!r}"

    def test_api_key_never_in_history(self):
        """Serialised request history must not contain the API key."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message()
        client = _make_client(mock_api)

        asyncio.run(client.async_request("System with sk-ant-test-key-12345 leak?", "User."))

        history = client.get_request_history()
        serialised = json.dumps(history)
        assert _TEST_KEY not in serialised


class TestConfigUpdate:
    """update_config() recreates the internal API client when the key changes."""

    def test_config_update_recreates_client(self):
        """Changing the API key in update_config must recreate the underlying client."""
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        original_client = client._client

        new_key = "sk-ant-new-key-67890"
        new_config = _make_config(ai_api_key=new_key)

        with (
            patch("custom_components.climate_advisor.claude_api.ANTHROPIC_AVAILABLE", True),
            patch("custom_components.climate_advisor.claude_api.AsyncAnthropic") as mock_ctor,
        ):
            mock_ctor.return_value = MagicMock()
            client.update_config(new_config)

        # The underlying API client object should have been replaced.
        assert client._client is not original_client
        mock_ctor.assert_called_once_with(api_key=new_key)


class TestConnectionTest:
    """async_test_connection validates the key with a minimal probe call."""

    def test_test_connection_success(self):
        """A successful probe returns (True, message)."""
        with (
            patch("custom_components.climate_advisor.claude_api.ANTHROPIC_AVAILABLE", True),
            patch("custom_components.climate_advisor.claude_api.AsyncAnthropic") as mock_ctor,
        ):
            mock_instance = MagicMock()
            mock_instance.messages.create = AsyncMock(return_value=_mock_message())
            mock_ctor.return_value = mock_instance

            client = ClaudeAPIClient(config=_make_config())
            ok, msg = asyncio.run(client.async_test_connection())

        assert ok is True
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_test_connection_failure(self):
        """A failed probe returns (False, error_message)."""
        import custom_components.climate_advisor.claude_api as _mod

        mock_inner = MagicMock()
        mock_inner.messages.create = AsyncMock(side_effect=Exception("auth_error: invalid API key"))

        orig = _mod.AsyncAnthropic
        orig_avail = _mod.ANTHROPIC_AVAILABLE
        _mod.AsyncAnthropic = MagicMock(return_value=mock_inner)
        _mod.ANTHROPIC_AVAILABLE = True

        # Also patch the exception types so the except clauses work
        # when anthropic was never truly imported.
        orig_rate = _mod.RateLimitError
        orig_api = _mod.APIError
        _mod.RateLimitError = type("RateLimitError", (Exception,), {})
        _mod.APIError = type("APIError", (Exception,), {})
        try:
            client = ClaudeAPIClient(config=_make_config())
            ok, msg = asyncio.run(client.async_test_connection())
        finally:
            _mod.AsyncAnthropic = orig
            _mod.ANTHROPIC_AVAILABLE = orig_avail
            _mod.RateLimitError = orig_rate
            _mod.APIError = orig_api

        assert ok is False
        assert "auth_error" in msg


class TestDisabledWhenNoKey:
    """Client with an empty API key reports 'inactive' in status."""

    def test_disabled_when_no_key(self):
        """An empty API key results in no internal client and status='inactive'."""
        client = ClaudeAPIClient(config=_make_config(ai_api_key=""), client=None)

        # The client object should not have been created.
        assert client._client is None

        status = client.get_status()
        assert status["status"] == "inactive"


class TestPersistentStats:
    """Tests for get_persistent_stats / restore_persistent_stats (Issue #81)."""

    def test_roundtrip_preserves_all_fields(self):
        """Stats saved then restored should match exactly."""
        client = ClaudeAPIClient(_make_config())
        client._total_requests = 42
        client._error_count = 3
        client._budget.monthly_cost = 1.23
        client._budget.budget_month = 3
        client._rate_counters.auto_requests_today = 4
        client._rate_counters.manual_requests_today = 7
        today = date.today()
        client._rate_counters.counter_date = today

        stats = client.get_persistent_stats()

        client2 = ClaudeAPIClient(_make_config())
        client2.restore_persistent_stats(stats)

        assert client2._total_requests == 42
        assert client2._error_count == 3
        assert client2._budget.monthly_cost == 1.23
        assert client2._budget.budget_month == 3
        assert client2._rate_counters.auto_requests_today == 4
        assert client2._rate_counters.manual_requests_today == 7
        assert client2._rate_counters.counter_date == today

    def test_empty_dict_restores_to_defaults(self):
        """Restoring from empty dict should not crash and should use defaults."""
        client = ClaudeAPIClient(_make_config())
        client.restore_persistent_stats({})
        assert client._total_requests == 0
        assert client._budget.monthly_cost == 0.0

    def test_cross_day_reboot_resets_daily_counters(self):
        """Daily counters are cleared when restored from a previous calendar day."""
        client = ClaudeAPIClient(_make_config())
        today = date.today()
        if today.day > 1:
            yesterday = today.replace(day=today.day - 1)
        else:
            # First of month: use a fixed past date
            yesterday = date(today.year, today.month, 1).replace(day=1)
            yesterday = date(2026, 3, 30)
        data = {
            "auto_requests_today": 10,
            "manual_requests_today": 5,
            "counter_date": yesterday.isoformat(),
            "monthly_cost": 0.5,
            "budget_month": today.month,
            "total_requests": 20,
            "error_count": 1,
        }
        client.restore_persistent_stats(data)
        assert client._rate_counters.auto_requests_today == 0
        assert client._rate_counters.manual_requests_today == 0
        assert client._rate_counters.counter_date == today
        # Cumulative counters are preserved across day boundary
        assert client._total_requests == 20
        assert client._budget.monthly_cost == 0.5

    def test_same_day_reboot_preserves_daily_counters(self):
        """Daily counters are kept when restored from the same calendar day."""
        client = ClaudeAPIClient(_make_config())
        today = date.today()
        data = {
            "auto_requests_today": 3,
            "manual_requests_today": 2,
            "counter_date": today.isoformat(),
            "monthly_cost": 0.0,
            "budget_month": today.month,
            "total_requests": 5,
            "error_count": 0,
        }
        client.restore_persistent_stats(data)
        assert client._rate_counters.auto_requests_today == 3
        assert client._rate_counters.manual_requests_today == 2

    def test_invalid_counter_date_falls_back_to_today(self):
        """Corrupted counter_date should not crash — falls back to today."""
        client = ClaudeAPIClient(_make_config())
        client.restore_persistent_stats({"counter_date": "not-a-date"})
        assert client._rate_counters.counter_date == date.today()


# ---------------------------------------------------------------------------
# Truncation detection (Issue #420)
# ---------------------------------------------------------------------------


class _FakeStreamCM:
    """Fake async context manager mimicking anthropic's `messages.stream()` return value."""

    def __init__(self, events: list, final_message: MagicMock) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


def _mock_text_delta_event(text: str) -> MagicMock:
    event = MagicMock()
    event.type = "content_block_delta"
    event.delta = MagicMock()
    event.delta.type = "text_delta"
    event.delta.text = text
    return event


class TestTruncationDetection:
    """stop_reason must be inspected on every request so a max_tokens cutoff is never silent."""

    def test_non_streaming_max_tokens_marks_truncated_and_warns(self, caplog):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message(
            "partial report...", output_tokens=8192, stop_reason="max_tokens"
        )
        client = _make_client(mock_api)

        with caplog.at_level("WARNING"):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert response.stop_reason == "max_tokens"
        assert response.truncated is True
        assert any("truncated" in rec.message.lower() for rec in caplog.records)

    def test_non_streaming_end_turn_is_not_truncated(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message("complete report", stop_reason="end_turn")
        client = _make_client(mock_api)

        response = asyncio.run(client.async_request("System.", "User."))

        assert response.stop_reason == "end_turn"
        assert response.truncated is False

    def test_streaming_max_tokens_yields_stop_event_and_warns(self, caplog):
        mock_api = _make_mock_api_client()
        final_msg = MagicMock()
        final_msg.usage = MagicMock(input_tokens=50, output_tokens=4096)
        final_msg.stop_reason = "max_tokens"
        mock_api.messages.stream = MagicMock(
            return_value=_FakeStreamCM([_mock_text_delta_event("partial...")], final_msg)
        )
        client = _make_client(mock_api)

        async def _collect():
            events = []
            async for event in client.async_request_streaming("System.", "User."):
                events.append(event)
            return events

        with caplog.at_level("WARNING"):
            events = asyncio.run(_collect())

        stop_events = [e for e in events if e.get("type") == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0]["stop_reason"] == "max_tokens"
        assert any("truncated" in rec.message.lower() for rec in caplog.records)

    def test_streaming_end_turn_yields_stop_event_without_warning(self, caplog):
        mock_api = _make_mock_api_client()
        final_msg = MagicMock()
        final_msg.usage = MagicMock(input_tokens=50, output_tokens=100)
        final_msg.stop_reason = "end_turn"
        mock_api.messages.stream = MagicMock(return_value=_FakeStreamCM([_mock_text_delta_event("done.")], final_msg))
        client = _make_client(mock_api)

        async def _collect():
            events = []
            async for event in client.async_request_streaming("System.", "User."):
                events.append(event)
            return events

        with caplog.at_level("WARNING"):
            events = asyncio.run(_collect())

        stop_events = [e for e in events if e.get("type") == "stop"]
        assert stop_events == [{"type": "stop", "stop_reason": "end_turn"}]
        assert not any("truncated" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Issue #563 — dynamic model discovery + capability-tier deprecation fallback
# ---------------------------------------------------------------------------


class TestModelTierDetection:
    """detect_model_tier() must recognize every currently-offered model plus future ones."""

    def test_detects_all_current_models(self):
        for model_id in AI_MODELS:
            assert detect_model_tier(model_id) in ("opus", "sonnet", "haiku"), model_id

    def test_detects_sonnet_variants(self):
        assert detect_model_tier("claude-sonnet-4-6") == "sonnet"
        assert detect_model_tier("claude-sonnet-5") == "sonnet"  # synthetic future model

    def test_detects_opus_and_haiku(self):
        assert detect_model_tier("claude-opus-5") == "opus"
        assert detect_model_tier("claude-haiku-4-5-20251001") == "haiku"

    def test_unknown_model_returns_none(self):
        assert detect_model_tier("some-other-vendor-model") is None

    def test_case_insensitive(self):
        assert detect_model_tier("Claude-SONNET-4-6") == "sonnet"


class TestFetchAvailableModels:
    """fetch_available_models() must never raise and always fall back to AI_MODELS."""

    def test_no_api_key_returns_static_list(self):
        result = asyncio.run(fetch_available_models(""))
        assert result == list(AI_MODELS)

    def test_live_fetch_success_returns_live_list(self):
        fake_model = MagicMock()
        fake_model.id = "claude-sonnet-5"
        fake_page = MagicMock()
        fake_page.data = [fake_model]
        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=fake_page)

        with patch("custom_components.climate_advisor.claude_api.AsyncAnthropic", return_value=fake_client):
            result = asyncio.run(fetch_available_models(_TEST_KEY))

        assert result == ["claude-sonnet-5"]

    def test_network_error_falls_back_to_static_list(self):
        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(side_effect=Exception("network down"))

        with patch("custom_components.climate_advisor.claude_api.AsyncAnthropic", return_value=fake_client):
            result = asyncio.run(fetch_available_models(_TEST_KEY))

        assert result == list(AI_MODELS)

    def test_missing_models_attribute_falls_back_to_static_list(self):
        fake_client = MagicMock(spec=["messages"])  # no .models attribute at all

        with patch("custom_components.climate_advisor.claude_api.AsyncAnthropic", return_value=fake_client):
            result = asyncio.run(fetch_available_models(_TEST_KEY))

        assert result == list(AI_MODELS)

    def test_empty_live_list_falls_back_to_static_list(self):
        fake_page = MagicMock()
        fake_page.data = []
        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=fake_page)

        with patch("custom_components.climate_advisor.claude_api.AsyncAnthropic", return_value=fake_client):
            result = asyncio.run(fetch_available_models(_TEST_KEY))

        assert result == list(AI_MODELS)


class TestAsyncListModelsCache:
    """ClaudeAPIClient.async_list_models() caches the live fetch for the TTL window."""

    def test_second_call_within_ttl_uses_cache(self):
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        with patch(
            "custom_components.climate_advisor.claude_api.fetch_available_models",
            new_callable=AsyncMock,
            return_value=["claude-sonnet-5"],
        ) as mock_fetch:
            first = asyncio.run(client.async_list_models())
            second = asyncio.run(client.async_list_models())

        assert first == ["claude-sonnet-5"]
        assert second == ["claude-sonnet-5"]
        mock_fetch.assert_called_once()

    def test_cache_expires_after_ttl(self):
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api)

        with patch(
            "custom_components.climate_advisor.claude_api.fetch_available_models",
            new_callable=AsyncMock,
            return_value=["claude-sonnet-5"],
        ) as mock_fetch:
            asyncio.run(client.async_list_models())
            # Force the cache timestamp far enough in the past to expire it.
            client._models_cache_ts = time.monotonic() - 90_000
            asyncio.run(client.async_list_models())

        assert mock_fetch.call_count == 2


class TestDeprecatedModelFallback:
    """A NotFoundError on the configured model should trigger one same-tier retry, not backoff."""

    def test_deprecated_model_falls_back_to_same_tier_replacement(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = [
            _ClaudeApiNotFoundError("model not found"),
            _mock_message("recovered on new model"),
        ]
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch.object(client, "async_list_models", new_callable=AsyncMock, return_value=["claude-sonnet-5"]),
        ):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert response.resolved_model == "claude-sonnet-5"
        assert mock_api.messages.create.call_count == 2
        # No backoff sleep for a not-found error — it's not worth retrying the same model.
        mock_sleep.assert_not_called()

    def test_no_same_tier_candidate_falls_through_to_normal_retry(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = _ClaudeApiNotFoundError("model not found")
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(client, "async_list_models", new_callable=AsyncMock, return_value=[]),
        ):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is False
        # Falls through to the normal APIError retry path (still bounded by AI_MAX_RETRIES).
        assert mock_api.messages.create.call_count == 3

    def test_successful_request_without_fallback_sets_resolved_model_to_requested(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.return_value = _mock_message("normal response")
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert response.resolved_model == "claude-sonnet-4-6"

    def test_notfounderror_unavailable_degrades_to_normal_retry_behavior(self):
        """If the SDK doesn't expose NotFoundError, the special-case must be skipped, not crash."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = _ClaudeApiNotFoundError("model not found")
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with (
            patch("custom_components.climate_advisor.claude_api.NotFoundError", None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is False
        assert mock_api.messages.create.call_count == 3


class _FakeFailingStreamCM:
    """Fake async context manager that raises on entry — mimics Anthropic rejecting
    a stream request outright (e.g. a 400 invalid_request_error), before any content
    is produced."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info):
        return False


class TestBuildRequestKwargsOrdering:
    """The unsupported-params strip must run AFTER the thinking-budget special case,
    so a model known not to support temperature never gets it re-added even under
    high reasoning effort."""

    def test_strip_runs_after_thinking_budget_forces_temperature(self):
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")
        client._unsupported_params["claude-sonnet-4-6"] = {"temperature"}

        kwargs = client._build_request_kwargs(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.3,
            reasoning_effort="high",
            system_prompt="System.",
            user_message="User.",
        )

        assert "temperature" not in kwargs
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 16384}

    def test_no_unsupported_params_leaves_temperature_in_place(self):
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        kwargs = client._build_request_kwargs(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.3,
            reasoning_effort="medium",
            system_prompt="System.",
            user_message="User.",
        )

        assert kwargs["temperature"] == 0.3

    def test_strip_only_applies_to_the_matching_model(self):
        mock_api = _make_mock_api_client()
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")
        client._unsupported_params["claude-opus-4-6"] = {"temperature"}

        kwargs = client._build_request_kwargs(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.3,
            reasoning_effort="medium",
            system_prompt="System.",
            user_message="User.",
        )

        assert kwargs["temperature"] == 0.3


class TestDeprecatedParameterFallback:
    """A 'parameter deprecated for this model' 400 should be learned and stripped,
    not blindly retried with backoff (Issue #563 follow-on)."""

    def test_non_streaming_retries_without_param_and_learns_it(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = [
            _ClaudeApiAPIError("`temperature` is deprecated for this model."),
            _mock_message("recovered without temperature"),
        ]
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert mock_api.messages.create.call_count == 2
        assert "temperature" not in mock_api.messages.create.call_args.kwargs
        assert client._unsupported_params["claude-sonnet-4-6"] == {"temperature"}
        mock_sleep.assert_not_called()

    def test_second_request_to_same_model_proactively_skips_param(self):
        """Once learned, the param must never be sent again for that model — not
        just reactively stripped after another failure."""
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = [
            _ClaudeApiAPIError("`temperature` is deprecated for this model."),
            _mock_message("first request recovered"),
        ]
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            asyncio.run(client.async_request("System.", "User."))

        mock_api.messages.create.side_effect = None
        mock_api.messages.create.return_value = _mock_message("second request")
        response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert "temperature" not in mock_api.messages.create.call_args.kwargs
        # Only one extra call from the first request's retry — this one didn't need it.
        assert mock_api.messages.create.call_count == 3

    def test_retry_failure_after_dropping_param_falls_through_to_normal_backoff(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = [
            _ClaudeApiAPIError("`temperature` is deprecated for this model."),
            Exception("still broken"),
            _mock_message("recovered on attempt 3"),
        ]
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is True
        assert mock_api.messages.create.call_count == 3
        # The second (fallback) call failed too — normal backoff before attempt 3.
        mock_sleep.assert_called()

    def test_unrelated_400_error_is_not_treated_as_capability_issue(self):
        mock_api = _make_mock_api_client()
        mock_api.messages.create.side_effect = _ClaudeApiAPIError("invalid_request_error: bad JSON")
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = asyncio.run(client.async_request("System.", "User."))

        assert response.success is False
        assert client._unsupported_params == {}
        assert mock_api.messages.create.call_count == 3

    def test_streaming_retries_without_param_before_any_content_yielded(self):
        mock_api = _make_mock_api_client()
        final_msg = MagicMock()
        final_msg.usage = MagicMock(input_tokens=10, output_tokens=20)
        final_msg.stop_reason = "end_turn"
        good_stream = _FakeStreamCM([_mock_text_delta_event("recovered")], final_msg)
        bad_stream = _FakeFailingStreamCM(Exception("`temperature` is deprecated for this model."))

        mock_api.messages.stream = MagicMock(side_effect=[bad_stream, good_stream])
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        async def _collect():
            events = []
            async for event in client.async_request_streaming("System.", "User."):
                events.append(event)
            return events

        events = asyncio.run(_collect())

        text_events = [e for e in events if e.get("type") == "text"]
        assert text_events == [{"type": "text", "text": "recovered"}]
        assert mock_api.messages.stream.call_count == 2
        assert client._unsupported_params["claude-sonnet-4-6"] == {"temperature"}

    def test_streaming_second_failure_reraises_normally(self):
        mock_api = _make_mock_api_client()
        bad_stream_1 = _FakeFailingStreamCM(Exception("`temperature` is deprecated for this model."))
        bad_stream_2 = _FakeFailingStreamCM(Exception("still broken"))
        mock_api.messages.stream = MagicMock(side_effect=[bad_stream_1, bad_stream_2])
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        async def _collect():
            events = []
            async for event in client.async_request_streaming("System.", "User."):
                events.append(event)
            return events

        try:
            asyncio.run(_collect())
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            assert "still broken" in str(exc)

        assert raised is True
        assert mock_api.messages.stream.call_count == 2

    def test_streaming_unrelated_error_never_retries(self):
        mock_api = _make_mock_api_client()
        bad_stream = _FakeFailingStreamCM(Exception("connection reset"))
        mock_api.messages.stream = MagicMock(return_value=bad_stream)
        client = _make_client(mock_api, ai_model="claude-sonnet-4-6")

        async def _collect():
            events = []
            async for event in client.async_request_streaming("System.", "User."):
                events.append(event)
            return events

        try:
            asyncio.run(_collect())
            raised = False
        except Exception:  # noqa: BLE001
            raised = True

        assert raised is True
        assert mock_api.messages.stream.call_count == 1
