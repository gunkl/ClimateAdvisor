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
_mock_anthropic.AsyncAnthropic = MagicMock()

sys.modules["anthropic"] = _mock_anthropic

# Now it is safe to import the module under test.
from custom_components.climate_advisor.claude_api import ClaudeAPIClient  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    AI_CIRCUIT_BREAKER_THRESHOLD,
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


def _mock_message(content_text: str = "test response", input_tokens: int = 10, output_tokens: int = 20) -> MagicMock:
    """Build a mock anthropic Message response."""
    msg = MagicMock()
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = content_text
    msg.content = [content_block]
    msg.usage = MagicMock()
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
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
