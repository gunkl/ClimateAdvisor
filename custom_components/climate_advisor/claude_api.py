"""Centralized Claude API client for Climate Advisor."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .const import (
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MAX_RETRIES,
    AI_REASONING_BUDGET_TOKENS,
    AI_REASONING_HIGH,
    AI_REQUEST_HISTORY_CAP,
    AI_RETRY_BASE_DELAY_SECONDS,
    CONF_AI_API_KEY,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_RPD,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_MAX_TOKENS,
    CONF_AI_MODEL,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TEMPERATURE,
    DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
    DEFAULT_AI_INVESTIGATOR_ENABLED,
    DEFAULT_AI_INVESTIGATOR_RPD,
    DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_REASONING_EFFORT,
    DEFAULT_AI_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

try:
    from anthropic import APIError, APITimeoutError, AsyncAnthropic, RateLimitError

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    AsyncAnthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    APITimeoutError = Exception  # type: ignore[assignment,misc]
    RateLimitError = Exception  # type: ignore[assignment,misc]

# Per-model cost rates (USD per million tokens)
_MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-sonnet": {"input": 3.0, "output": 15.0},
    "claude-opus": {"input": 15.0, "output": 75.0},
    "claude-haiku": {"input": 0.80, "output": 4.0},
}

# Circuit breaker states
_CB_CLOSED = "closed"
_CB_OPEN = "open"
_CB_HALF_OPEN = "half_open"


@dataclass
class ClaudeResponse:
    """Response from a Claude API request."""

    success: bool
    content: str  # response text (empty on failure)
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    latency_ms: float
    error: str | None = None
    rate_limited: bool = False
    circuit_open: bool = False
    budget_exceeded: bool = False


@dataclass
class _CircuitBreaker:
    """Simple circuit breaker for API resilience."""

    state: str = _CB_CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0


@dataclass
class _RateLimitCounters:
    """Daily request counters, one per trigger type."""

    auto_requests_today: int = 0
    manual_requests_today: int = 0
    counter_date: date = field(default_factory=date.today)


@dataclass
class _BudgetTracker:
    """Monthly spend tracker."""

    monthly_cost: float = 0.0
    budget_month: int = field(default_factory=lambda: date.today().month)


class ClaudeAPIClient:
    """Centralized Anthropic Claude API client with rate limiting, circuit breaking, and budget tracking."""

    def __init__(
        self,
        config: dict[str, Any],
        client: Any | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            config: HA config entry data dict containing CONF_AI_* values.
            client: Optional AsyncAnthropic instance for dependency injection (tests).

        """
        self._config = config
        self._client: Any = client

        if self._client is None and ANTHROPIC_AVAILABLE:
            api_key = config.get(CONF_AI_API_KEY, "")
            if api_key:
                self._client = AsyncAnthropic(api_key=api_key)
                _LOGGER.debug("API client initialized — key configured")
            else:
                _LOGGER.warning("No AI API key configured; Claude API client will not be active")

        self.request_history: deque[dict[str, Any]] = deque(maxlen=AI_REQUEST_HISTORY_CAP)
        self._circuit_breaker = _CircuitBreaker()
        self._rate_counters = _RateLimitCounters()
        self._budget = _BudgetTracker()
        self._total_requests: int = 0
        self._error_count: int = 0
        self._last_request_time: float | None = None
        self._investigator_requests_today: int = 0
        self._investigator_requests_date: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_request(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        triggered_by: str = "manual",
    ) -> ClaudeResponse:
        """Send a request to the Claude API with resilience guards.

        Args:
            system_prompt: System-level instructions for the model.
            user_message: The user turn content.
            max_tokens: Override the configured max_tokens for this request.
            temperature: Override the configured temperature for this request.
            model: Override the configured model for this request.
            reasoning_effort: Override the configured reasoning effort for this request.
            triggered_by: "manual" (user-initiated) or "auto" (scheduled/automated).

        Returns:
            ClaudeResponse with result or failure metadata.

        """
        self._reset_daily_counters_if_needed()

        # Guard: circuit breaker
        if not self._check_circuit_breaker():
            _LOGGER.warning("Claude API circuit breaker is open; skipping request")
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error="Circuit breaker open",
                circuit_open=True,
            )

        # Guard: rate limiter
        if not self._check_rate_limit(triggered_by):
            _LOGGER.warning(
                "Daily %s request limit reached; skipping Claude API call",
                triggered_by,
            )
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error="Rate limit exceeded",
                rate_limited=True,
            )

        # Guard: monthly budget
        if not self._check_budget():
            _LOGGER.warning("Monthly AI budget exceeded; skipping Claude API call")
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error="Monthly budget exceeded",
                budget_exceeded=True,
            )

        if self._client is None:
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error="Anthropic client not initialized (missing package or API key)",
            )

        resolved_max_tokens = (
            max_tokens if max_tokens is not None else self._config.get(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS)
        )
        resolved_temperature = (
            temperature if temperature is not None else self._config.get(CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE)
        )
        model = model if model is not None else self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL)
        reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else self._config.get(CONF_AI_REASONING_EFFORT, DEFAULT_AI_REASONING_EFFORT)
        )

        response = await self._async_call_with_retry(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model,
            max_tokens=resolved_max_tokens,
            temperature=resolved_temperature,
            reasoning_effort=reasoning_effort,
        )

        # Update counters on success or failure
        if response.success:
            self._circuit_breaker.consecutive_failures = 0
            if self._circuit_breaker.state != _CB_CLOSED:
                _LOGGER.info("Circuit breaker reset to closed after successful request")
            self._circuit_breaker.state = _CB_CLOSED
            self._budget.monthly_cost += response.estimated_cost
            if triggered_by == "auto":
                self._rate_counters.auto_requests_today += 1
            else:
                self._rate_counters.manual_requests_today += 1
        else:
            self._circuit_breaker.consecutive_failures += 1
            self._error_count += 1
            if self._circuit_breaker.consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_breaker.state = _CB_OPEN
                self._circuit_breaker.opened_at = time.monotonic()
                _LOGGER.error(
                    "Circuit breaker opened after %d consecutive failures",
                    self._circuit_breaker.consecutive_failures,
                )

        self._total_requests += 1
        self._last_request_time = time.time()

        # Record metadata (no content, no key)
        self.request_history.append(
            {
                "timestamp": self._last_request_time,
                "skill_name": self._extract_skill_name(system_prompt),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "estimated_cost": response.estimated_cost,
                "latency_ms": response.latency_ms,
                "success": response.success,
                "error": response.error,
            }
        )

        return response

    async def async_test_connection(self) -> tuple[bool, str]:
        """Validate the configured API key with a minimal API call.

        Returns:
            (True, "Connected successfully") on success, or (False, error_message).

        """
        if not ANTHROPIC_AVAILABLE:
            return False, "anthropic package is not installed"

        api_key = self._config.get(CONF_AI_API_KEY, "")
        if not api_key:
            return False, "No API key configured"

        test_client = AsyncAnthropic(api_key=api_key)
        try:
            await test_client.messages.create(
                model=self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL),
                max_tokens=1,
                messages=[{"role": "user", "content": "Hi"}],
            )
        except RateLimitError:
            # Rate limited but key is valid
            return True, "Connected successfully (rate limited)"
        except APIError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        else:
            return True, "Connected successfully"

    def get_status(self) -> dict[str, Any]:
        """Return current client status metadata.

        The returned dict NEVER includes the API key.

        Returns:
            Dict with status summary suitable for sensor attributes or API responses.

        """
        self._reset_daily_counters_if_needed()

        # Determine top-level status string
        if not self._config.get("ai_enabled", False):
            status = "disabled"
        elif self._circuit_breaker.state == _CB_OPEN:
            status = "circuit_open"
        elif not self._check_budget():
            status = "budget_exceeded"
        elif self._client is None:
            status = "inactive"
        elif self._error_count > 0 and self._circuit_breaker.consecutive_failures > 0:
            status = "error"
        else:
            status = "active"

        return {
            "status": status,
            "error_count": self._error_count,
            "total_requests": self._total_requests,
            "last_request_time": self._last_request_time,
            "model": self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL),
            "circuit_breaker_state": self._circuit_breaker.state,
            "monthly_cost_estimate": round(self._budget.monthly_cost, 4),
            "auto_requests_today": self._rate_counters.auto_requests_today,
            "manual_requests_today": self._rate_counters.manual_requests_today,
            "investigator_requests_today": self._investigator_requests_today,
            "investigator_requests_limit": int(self._config.get(CONF_AI_INVESTIGATOR_RPD, DEFAULT_AI_INVESTIGATOR_RPD)),
            "investigator_enabled": self._config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED),
        }

    def get_persistent_stats(self) -> dict[str, Any]:
        """Return stats that should survive HA reboot.

        Called by the coordinator's state persistence layer to include AI stats
        in the operational state file saved on every update cycle.

        Returns:
            Serializable dict suitable for JSON storage.

        """
        return {
            "total_requests": self._total_requests,
            "error_count": self._error_count,
            "monthly_cost": self._budget.monthly_cost,
            "budget_month": self._budget.budget_month,
            "auto_requests_today": self._rate_counters.auto_requests_today,
            "manual_requests_today": self._rate_counters.manual_requests_today,
            "counter_date": self._rate_counters.counter_date.isoformat(),
            "investigator_requests_today": self._investigator_requests_today,
            "investigator_requests_date": self._investigator_requests_date,
        }

    def restore_persistent_stats(self, data: dict[str, Any]) -> None:
        """Restore stats saved from a previous session.

        Called during coordinator startup after the state file is loaded.
        Missing keys default to zero so old state files are safe.
        Calls _reset_daily_counters_if_needed() to handle cross-day reboots.

        Args:
            data: Dict previously returned by get_persistent_stats().

        """
        self._total_requests = int(data.get("total_requests", 0))
        self._error_count = int(data.get("error_count", 0))
        self._budget.monthly_cost = float(data.get("monthly_cost", 0.0))
        self._budget.budget_month = int(data.get("budget_month", date.today().month))
        self._rate_counters.auto_requests_today = int(data.get("auto_requests_today", 0))
        self._rate_counters.manual_requests_today = int(data.get("manual_requests_today", 0))
        try:
            self._rate_counters.counter_date = date.fromisoformat(data["counter_date"])
        except (KeyError, ValueError):
            self._rate_counters.counter_date = date.today()
        self._investigator_requests_today = int(data.get("investigator_requests_today", 0))
        self._investigator_requests_date = data.get("investigator_requests_date", "")
        # Apply daily reset if rebooted after midnight
        self._reset_daily_counters_if_needed()
        _LOGGER.debug(
            "AI stats restored — total_requests=%d, monthly_cost=%.4f, budget_month=%d",
            self._total_requests,
            self._budget.monthly_cost,
            self._budget.budget_month,
        )

    def get_request_history(self) -> list[dict[str, Any]]:
        """Return metadata-only request history.

        Each entry contains: timestamp, skill_name, input_tokens, output_tokens,
        estimated_cost, latency_ms, success, error. NEVER includes API key or raw content.

        Returns:
            List of request metadata dicts (most recent last).

        """
        return list(self.request_history)

    def update_config(self, config: dict[str, Any]) -> None:
        """Apply updated config entry data.

        If the API key changed, the underlying AsyncAnthropic client is recreated.

        Args:
            config: New config entry data dict.

        """
        old_key = self._config.get(CONF_AI_API_KEY, "")
        new_key = config.get(CONF_AI_API_KEY, "")

        self._config = config

        if new_key != old_key:
            if new_key and ANTHROPIC_AVAILABLE:
                self._client = AsyncAnthropic(api_key=new_key)
                _LOGGER.debug("API client re-initialized — key updated")
            else:
                self._client = None
                _LOGGER.warning("AI API key removed; Claude API client deactivated")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_rate_limit(self, triggered_by: str) -> bool:
        """Return True if the request is within the daily rate limit.

        Args:
            triggered_by: "auto" or "manual".

        Returns:
            True if under limit, False if at or over limit.

        """
        if triggered_by == "auto":
            limit = self._config.get(CONF_AI_AUTO_REQUESTS_PER_DAY, DEFAULT_AI_AUTO_REQUESTS_PER_DAY)
            return self._rate_counters.auto_requests_today < limit
        else:
            limit = self._config.get(CONF_AI_MANUAL_REQUESTS_PER_DAY, DEFAULT_AI_MANUAL_REQUESTS_PER_DAY)
            return self._rate_counters.manual_requests_today < limit

    def check_investigator_rate_limit(self) -> tuple[bool, str]:
        """Check whether an investigator request is allowed under the daily limit.

        Returns (allowed: bool, reason: str).
        """
        if not self._config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED):
            return False, "Investigative agent is not enabled"
        self._reset_investigator_counter_if_needed()
        limit = int(self._config.get(CONF_AI_INVESTIGATOR_RPD, DEFAULT_AI_INVESTIGATOR_RPD))
        if limit > 0 and self._investigator_requests_today >= limit:
            return (
                False,
                f"Investigator daily limit reached ({self._investigator_requests_today}/{limit})",
            )
        return True, ""

    def increment_investigator_counter(self) -> None:
        """Increment the investigator daily request counter."""
        self._reset_investigator_counter_if_needed()
        self._investigator_requests_today += 1

    def _reset_investigator_counter_if_needed(self) -> None:
        """Reset investigator counter if the date has rolled over."""
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        if self._investigator_requests_date != today:
            self._investigator_requests_today = 0
            self._investigator_requests_date = today

    def _check_circuit_breaker(self) -> bool:
        """Return True if the circuit breaker permits a request.

        If the breaker is open and the cooldown has elapsed, transitions to half-open
        and allows one probe request through.

        Returns:
            True if closed or half-open (OK to proceed), False if open.

        """
        if self._circuit_breaker.state == _CB_CLOSED:
            return True

        if self._circuit_breaker.state == _CB_HALF_OPEN:
            return True

        # State is OPEN — check cooldown
        elapsed = time.monotonic() - self._circuit_breaker.opened_at
        if elapsed >= AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS:
            _LOGGER.info(
                "Circuit breaker cooldown elapsed (%.0fs); transitioning to half-open",
                elapsed,
            )
            self._circuit_breaker.state = _CB_HALF_OPEN
            return True

        return False

    def _check_budget(self) -> bool:
        """Return True if the monthly budget has not been exceeded.

        A budget of 0 means no cap (always returns True).
        Resets the accumulator when the calendar month rolls over.

        Returns:
            True if under budget or no cap, False if over budget.

        """
        today = date.today()
        if today.month != self._budget.budget_month:
            _LOGGER.debug(
                "Monthly budget counter reset (old month: %d, new month: %d)",
                self._budget.budget_month,
                today.month,
            )
            self._budget.monthly_cost = 0.0
            self._budget.budget_month = today.month

        monthly_budget = self._config.get(CONF_AI_MONTHLY_BUDGET, 0)
        if monthly_budget == 0:
            return True

        return self._budget.monthly_cost < monthly_budget

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for a request based on per-model published rates.

        Args:
            model: Model identifier string (e.g. "claude-sonnet-4-6").
            input_tokens: Number of input tokens consumed.
            output_tokens: Number of output tokens generated.

        Returns:
            Estimated cost in USD.

        """
        rates: dict[str, float] | None = None
        for prefix, model_rates in _MODEL_COSTS.items():
            if prefix in model:
                rates = model_rates
                break

        if rates is None:
            # Default to Sonnet rates if model is unrecognised
            rates = _MODEL_COSTS["claude-sonnet"]

        return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset daily request counters when the calendar date has changed."""
        today = date.today()
        if today != self._rate_counters.counter_date:
            _LOGGER.debug(
                "Resetting daily request counters (previous date: %s)",
                self._rate_counters.counter_date,
            )
            self._rate_counters.auto_requests_today = 0
            self._rate_counters.manual_requests_today = 0
            self._rate_counters.counter_date = today

    async def _async_call_with_retry(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
    ) -> ClaudeResponse:
        """Call the Anthropic messages API with exponential-backoff retry.

        Args:
            system_prompt: System instructions.
            user_message: User turn content.
            model: Model identifier.
            max_tokens: Maximum response tokens.
            temperature: Sampling temperature.
            reasoning_effort: One of "low", "medium", "high".

        Returns:
            ClaudeResponse with result or terminal failure.

        """
        last_error: str = "Unknown error"
        start_time = time.monotonic()

        for attempt in range(1, AI_MAX_RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                    "temperature": temperature,
                }

                # Extended thinking for high reasoning effort
                # Claude API requirements when thinking is enabled:
                #   1. temperature must be exactly 1
                #   2. max_tokens must exceed budget_tokens
                if reasoning_effort == AI_REASONING_HIGH:
                    budget = AI_REASONING_BUDGET_TOKENS.get(AI_REASONING_HIGH, 16384)
                    kwargs["temperature"] = 1  # required by API — overrides configured value
                    if kwargs["max_tokens"] <= budget:
                        kwargs["max_tokens"] = budget + 4096  # reserve room for output tokens
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

                api_response = await self._client.messages.create(**kwargs)

                latency_ms = (time.monotonic() - start_time) * 1000.0
                input_tokens: int = getattr(api_response.usage, "input_tokens", 0)
                output_tokens: int = getattr(api_response.usage, "output_tokens", 0)
                estimated_cost = self._estimate_cost(model, input_tokens, output_tokens)

                # Extract text content from response blocks
                content_text = ""
                for block in api_response.content:
                    if hasattr(block, "text"):
                        content_text += block.text

                return ClaudeResponse(
                    success=True,
                    content=content_text,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    latency_ms=latency_ms,
                )

            except RateLimitError as exc:
                last_error = f"Rate limit error: {exc}"
                _LOGGER.warning(
                    "Claude API rate limit on attempt %d/%d: %s",
                    attempt,
                    AI_MAX_RETRIES,
                    exc,
                )
            except APITimeoutError as exc:
                last_error = f"Timeout error: {exc}"
                _LOGGER.warning(
                    "Claude API timeout on attempt %d/%d: %s",
                    attempt,
                    AI_MAX_RETRIES,
                    exc,
                )
            except APIError as exc:
                last_error = f"API error: {exc}"
                _LOGGER.warning(
                    "Claude API error on attempt %d/%d: %s",
                    attempt,
                    AI_MAX_RETRIES,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"Unexpected error: {exc}"
                _LOGGER.warning(
                    "Unexpected error calling Claude API on attempt %d/%d: %s",
                    attempt,
                    AI_MAX_RETRIES,
                    exc,
                )

            if attempt < AI_MAX_RETRIES:
                delay = AI_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                _LOGGER.debug("Retrying in %.1f seconds (attempt %d)", delay, attempt + 1)
                await asyncio.sleep(delay)

        latency_ms = (time.monotonic() - start_time) * 1000.0
        _LOGGER.error(
            "Claude API request failed after %d attempts; last error: %s",
            AI_MAX_RETRIES,
            last_error,
        )
        return ClaudeResponse(
            success=False,
            content="",
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
            latency_ms=latency_ms,
            error=last_error,
        )

    @staticmethod
    def _extract_skill_name(system_prompt: str) -> str:
        """Derive a short skill identifier from the system prompt.

        Looks for a "skill:" or "skill_name:" line in the prompt, otherwise
        returns "unknown".

        Args:
            system_prompt: The full system prompt string.

        Returns:
            A short skill name string.

        """
        for line in system_prompt.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("skill:") or stripped.startswith("skill_name:"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    name = parts[1].strip()
                    if name:
                        return name
        return "unknown"
