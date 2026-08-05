"""Centralized Claude API client for Climate Advisor."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .const import (
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MAX_RETRIES,
    AI_MODELS,
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

try:
    # Issue #563 — distinct error type for an invalid/deprecated model ID, so the
    # retry loop can skip pointless same-model backoff and instead try a same-tier
    # replacement once. Import-guarded like the group above: if this SDK version
    # doesn't expose it, NotFoundError just never matches and behavior falls
    # through to today's generic APIError handling unchanged.
    from anthropic import NotFoundError
except ImportError:
    NotFoundError = None  # type: ignore[assignment,misc]

# Issue #563 — capability-tier keywords used for substring-matching a model ID to its
# tier (e.g. "claude-sonnet-4-6" -> "sonnet"). Intentionally not a maintained list of
# specific model names — Anthropic's product-line naming is the stable part, specific
# model IDs are not.
_MODEL_TIER_KEYWORDS: tuple[str, ...] = ("opus", "sonnet", "haiku")

# TTL for the live model-list cache (claude_api.py-local, not the coordinator's cache —
# this client has no coordinator reference). Matches the precedent set by
# ai_skills_context.py's GitHub-issue cache (_GITHUB_OPEN_TTL).
_MODEL_LIST_CACHE_TTL_SECONDS = 86_400  # 24 hours

# Issue #563 follow-on — reactive per-model parameter capability detection. Anthropic's
# Models API exposes id/display_name/created_at, not a per-model sampling-parameter
# schema, so there's no way to know ahead of time that e.g. a given model no longer
# accepts `temperature`. This regex matches the one confirmed error shape
# ("`temperature` is deprecated for this model."); a future differently-worded
# deprecation message would not be caught by this pattern — known limitation, not
# hidden.
_DEPRECATED_PARAM_RE = re.compile(r"`(\w+)` is deprecated for this model")


def _detect_deprecated_param(error_message: str) -> str | None:
    """Return the parameter name Anthropic's API just rejected as deprecated, if any."""
    match = _DEPRECATED_PARAM_RE.search(error_message)
    return match.group(1) if match else None


# Issue #565 — some newer models (confirmed: claude-sonnet-5) reject the legacy
# `thinking: {"type": "enabled", "budget_tokens": N}` shape outright with a 400 whose
# message names the replacement directly: '"thinking.type.enabled" is not supported for
# this model. Use "thinking.type.adaptive" and "output_config.effort" to control thinking
# behavior.' Matched on the literal replacement-parameter name Anthropic's own error text
# points at, not on the full sentence, so minor message wording changes don't break this.
_ADAPTIVE_THINKING_RE = re.compile(r"thinking\.type\.adaptive")


def _detect_adaptive_thinking_required(error_message: str) -> bool:
    """Return True if Anthropic's API just rejected the legacy thinking shape for this model."""
    return _ADAPTIVE_THINKING_RE.search(error_message) is not None


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
    stop_reason: str | None = None
    truncated: bool = False
    truncated_empty: bool = False  # truncated AND zero visible output — the whole
    # max_tokens budget was consumed without producing an answer (Issue #563 follow-on).
    # A distinct condition from ordinary truncation: there is no partial content to
    # salvage, and raising max_tokens further may not even be the right lever.
    resolved_model: str = ""  # actual model used; differs from the requested model only
    # when a deprecated/invalid model triggered a same-tier fallback (Issue #563)


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


def detect_model_tier(model_id: str) -> str | None:
    """Return the capability tier ("opus"/"sonnet"/"haiku") a model ID belongs to.

    Substring match on Anthropic's stable product-line naming — not a maintained
    list of specific model names, which would itself go stale (Issue #563).
    Returns None if no known tier keyword appears in the ID.
    """
    model_lower = model_id.lower()
    for tier in _MODEL_TIER_KEYWORDS:
        if tier in model_lower:
            return tier
    return None


async def fetch_available_models(api_key: str) -> list[str]:
    """Fetch the live list of available Claude model IDs from Anthropic.

    This function must never raise — on any failure (no API key, network error,
    an SDK version that lacks `.models`, an API error) it falls back to the
    static AI_MODELS default list. Used both by ClaudeAPIClient.async_list_models()
    (cached) and directly by config_flow.py (uncached — a config-flow render is a
    one-off interaction, not a hot path) so there is exactly one implementation of
    "how to ask Anthropic what models exist and what to do if that fails" (Issue #563).
    """
    if not api_key or not ANTHROPIC_AVAILABLE:
        return list(AI_MODELS)
    try:
        client = AsyncAnthropic(api_key=api_key)
        page = await client.models.list()
        models = [m.id for m in page.data]
        return models if models else list(AI_MODELS)
    except Exception:
        _LOGGER.debug("fetch_available_models: live fetch failed, using static fallback list", exc_info=True)
        return list(AI_MODELS)


class ClaudeAPIClient:
    """Centralized Anthropic Claude API client with rate limiting, circuit breaking, and budget tracking.

    Networking: uses the official Anthropic Python SDK (AsyncAnthropic) declared as
    ``anthropic>=0.49.0`` in manifest.json requirements — no raw HTTP client is used.
    """

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
        self._models_cache: list[str] | None = None
        self._models_cache_ts: float = 0.0
        # Issue #563 follow-on: model_id -> set of request-parameter names Anthropic has
        # rejected as deprecated for that model. In-memory only, per client instance —
        # same acceptable cost as _models_cache (resets on integration reload).
        self._unsupported_params: dict[str, set[str]] = {}
        # Issue #565: model_ids confirmed (either reactively, via a 400 naming
        # "thinking.type.adaptive", or after observing a zero-output full-budget
        # truncation) to need the newer adaptive thinking shape instead of the legacy
        # enabled/budget_tokens one. In-memory only, same lifecycle as _unsupported_params.
        self._adaptive_thinking_models: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_list_models(self) -> list[str]:
        """Return available Claude model IDs, cached for _MODEL_LIST_CACHE_TTL_SECONDS.

        Thin cached wrapper around the module-level fetch_available_models() — see
        that function for fallback behavior on failure (Issue #563).
        """
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cache_ts) < _MODEL_LIST_CACHE_TTL_SECONDS:
            return self._models_cache
        api_key = self._config.get(CONF_AI_API_KEY, "")
        models = await fetch_available_models(api_key)
        self._models_cache = models
        self._models_cache_ts = now
        return models

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

    async def async_request_streaming(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        triggered_by: str = "manual",
    ) -> AsyncIterator[dict[str, str]]:
        """Stream a Claude API response as typed event dicts.

        This is an async generator that yields dicts as they arrive:
        - ``{"type": "text", "text": str}`` — visible response text
        - ``{"type": "thinking", "text": str}`` — extended-thinking content (if enabled)

        Pre-flight guards (circuit breaker, rate limit, budget, client) raise
        RuntimeError on failure — callers should wrap in try/except.

        After all events are yielded the method records the request in history,
        updates budget/rate counters, and resets the circuit breaker on success.
        Streaming does not retry on error, with one narrow exception (Issue #563
        follow-on): if the API rejects a request parameter as deprecated for the
        configured model before any content has been yielded, the request is retried
        exactly once without that parameter. Any other failure — or a second failure,
        or one that occurs after content has already streamed to the caller —
        increments the circuit-breaker counter and re-raises, as before.

        Args:
            system_prompt: System-level instructions for the model.
            user_message: The user turn content.
            max_tokens: Override the configured max_tokens for this request.
            temperature: Override the configured temperature for this request.
            model: Override the configured model for this request.
            reasoning_effort: Override the configured reasoning effort for this request.
            triggered_by: "manual" or "auto" — determines rate limit counter.

        Yields:
            ``{"type": "text"|"thinking", "text": str}`` dicts from the Claude API stream.

        Raises:
            RuntimeError: When any pre-flight guard blocks the request.

        """
        self._reset_daily_counters_if_needed()

        if not self._check_circuit_breaker():
            raise RuntimeError("Circuit breaker open")

        if not self._check_rate_limit(triggered_by):
            raise RuntimeError("Rate limit exceeded")

        if not self._check_budget():
            raise RuntimeError("Monthly budget exceeded")

        if self._client is None:
            raise RuntimeError("Anthropic client not initialized (missing package or API key)")

        resolved_max_tokens = (
            max_tokens if max_tokens is not None else self._config.get(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS)
        )
        resolved_temperature = (
            temperature if temperature is not None else self._config.get(CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE)
        )
        resolved_model = model if model is not None else self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL)
        resolved_reasoning = (
            reasoning_effort
            if reasoning_effort is not None
            else self._config.get(CONF_AI_REASONING_EFFORT, DEFAULT_AI_REASONING_EFFORT)
        )

        start_time = time.monotonic()
        any_content_yielded = False  # any delta at all (text or thinking) — used only to
        # decide whether a fresh retry is safe (Issue #563 follow-on's param-fallback path)
        any_text_yielded = False  # specifically visible answer text — used to detect the
        # "burned the whole budget, produced no answer" case, since thinking output alone
        # doesn't give the caller an answer either (Issue #563 follow-on)
        final_msg = None
        kwargs: dict[str, Any] = {}

        for attempt in (1, 2):
            kwargs = self._build_request_kwargs(
                model=resolved_model,
                max_tokens=resolved_max_tokens,
                temperature=resolved_temperature,
                reasoning_effort=resolved_reasoning,
                system_prompt=system_prompt,
                user_message=user_message,
            )
            self._log_request_kwargs_decision(resolved_model, resolved_reasoning, kwargs)
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if getattr(event, "type", None) != "content_block_delta":
                            continue
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        delta_type = getattr(delta, "type", None)
                        if delta_type == "thinking_delta":
                            any_content_yielded = True
                            yield {"type": "thinking", "text": getattr(delta, "thinking", "")}
                        elif delta_type == "text_delta":
                            any_content_yielded = True
                            any_text_yielded = True
                            yield {"type": "text", "text": getattr(delta, "text", "")}
                    final_msg = await stream.get_final_message()
                break  # success

            except (RateLimitError, APITimeoutError, APIError, Exception) as exc:
                # Both checks are gated on "no content streamed yet" — a retry can only be
                # safe before anything has been yielded to the caller, since a streaming
                # generator can't un-yield partial content already shown in the UI.
                bad_param = None if any_content_yielded else _detect_deprecated_param(str(exc))
                needs_adaptive = (
                    not any_content_yielded
                    and resolved_model not in self._adaptive_thinking_models
                    and _detect_adaptive_thinking_required(str(exc))
                )
                if attempt == 1 and bad_param is not None:
                    self._unsupported_params.setdefault(resolved_model, set()).add(bad_param)
                    _LOGGER.warning(
                        "Model '%s' rejected parameter '%s' as deprecated — retrying stream without it",
                        resolved_model,
                        bad_param,
                    )
                    continue  # next loop iteration rebuilds kwargs without bad_param
                if attempt == 1 and needs_adaptive:
                    # Issue #565: model rejected the legacy thinking shape outright — learn
                    # it and retry with thinking.type.adaptive + output_config.effort.
                    self._adaptive_thinking_models.add(resolved_model)
                    _LOGGER.warning(
                        "Model '%s' rejected the legacy thinking shape — retrying stream with adaptive thinking",
                        resolved_model,
                    )
                    continue  # next loop iteration rebuilds kwargs with the adaptive shape

                if isinstance(exc, APIError) and bad_param is None and not needs_adaptive:
                    # Issue #568: a 400-class error that matches neither known
                    # capability-detection pattern — e.g. a genuinely new failure shape
                    # (a context-length-exceeded error is a documented risk for newer
                    # models with a different tokenizer). Mark it distinctly so it's
                    # immediately greppable and never mistaken for one of the two known,
                    # already-handled shapes.
                    _LOGGER.warning(
                        "Unrecognized API error shape for model '%s' (streaming) — full message: %s",
                        resolved_model,
                        exc,
                    )

                self._circuit_breaker.consecutive_failures += 1
                self._error_count += 1
                if self._circuit_breaker.consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD:
                    self._circuit_breaker.state = _CB_OPEN
                    self._circuit_breaker.opened_at = time.monotonic()
                    _LOGGER.error(
                        "Circuit breaker opened after %d consecutive failures",
                        self._circuit_breaker.consecutive_failures,
                    )
                raise

        latency_ms = (time.monotonic() - start_time) * 1000.0
        input_tokens: int = getattr(final_msg.usage, "input_tokens", 0)
        output_tokens: int = getattr(final_msg.usage, "output_tokens", 0)
        estimated_cost = self._estimate_cost(resolved_model, input_tokens, output_tokens)

        stop_reason = getattr(final_msg, "stop_reason", None)
        truncated = stop_reason == "max_tokens"
        truncated_empty = truncated and not any_text_yielded
        skill_name = self._extract_skill_name(system_prompt)
        _LOGGER.debug(
            "Claude streaming response finished: skill=%s stop_reason=%s input_tokens=%d output_tokens=%d",
            skill_name,
            stop_reason,
            input_tokens,
            output_tokens,
        )
        if truncated_empty:
            # Issue #563 follow-on: the full max_tokens budget was billed and consumed,
            # but no visible answer text was ever yielded (thinking deltas may still have
            # streamed) — a different, more severe problem than ordinary truncation
            # (there's no partial content to salvage).
            _LOGGER.warning(
                "Streaming response consumed the full max_tokens budget with zero visible "
                "output: skill=%s model=%s output_tokens=%d max_tokens=%d "
                "reasoning_effort=%s — model may need a larger max_tokens ceiling or a "
                "different reasoning_effort",
                skill_name,
                resolved_model,
                output_tokens,
                kwargs["max_tokens"],
                resolved_reasoning,
            )
            # Issue #565: this exact symptom (full budget, zero visible text) is what
            # uncapped implicit thinking on a newer model looks like at reasoning tiers
            # that never requested thinking control at all — arm the adaptive-thinking
            # capability for this model so the NEXT call (streaming or non-streaming)
            # applies bounded thinking from the start instead of repeating this failure.
            # This call already streamed thinking deltas to the caller and can't be
            # retried in place.
            if resolved_model not in self._adaptive_thinking_models:
                self._adaptive_thinking_models.add(resolved_model)
                _LOGGER.warning(
                    "Model '%s' will use adaptive thinking control on future requests "
                    "to recover from zero-output truncation",
                    resolved_model,
                )
        elif truncated:
            _LOGGER.warning(
                "Response truncated: skill=%s stop_reason=max_tokens output_tokens=%d max_tokens=%d",
                skill_name,
                output_tokens,
                kwargs["max_tokens"],
            )
        yield {"type": "stop", "stop_reason": stop_reason, "truncated_empty": truncated_empty}

        # Update counters on success
        self._circuit_breaker.consecutive_failures = 0
        if self._circuit_breaker.state != _CB_CLOSED:
            _LOGGER.info("Circuit breaker reset to closed after successful streaming request")
        self._circuit_breaker.state = _CB_CLOSED
        self._budget.monthly_cost += estimated_cost
        if triggered_by == "auto":
            self._rate_counters.auto_requests_today += 1
        else:
            self._rate_counters.manual_requests_today += 1

        self._total_requests += 1
        self._last_request_time = time.time()

        self.request_history.append(
            {
                "timestamp": self._last_request_time,
                "skill_name": self._extract_skill_name(system_prompt),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost": estimated_cost,
                "latency_ms": latency_ms,
                "success": True,
                "error": None,
            }
        )

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
            # Issue #568: without this, learned per-model capability (Issue #563's
            # _unsupported_params, Issue #565's _adaptive_thinking_models) was pure
            # in-memory state, wiped by any config reload (e.g. an options-flow save —
            # Issue #557 made these save+reload immediately) or HA restart, forcing the
            # first-call-fails-once discovery cost to repeat indefinitely instead of
            # self-healing once and staying healed.
            "unsupported_params": {model: sorted(params) for model, params in self._unsupported_params.items()},
            "adaptive_thinking_models": sorted(self._adaptive_thinking_models),
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
        # Issue #568: type-validated per the project's JSON-from-disk convention — any
        # malformed shape (hand-edited state file, downgrade from a future version)
        # degrades to "nothing learned yet" rather than raising.
        raw_unsupported = data.get("unsupported_params", {})
        self._unsupported_params = (
            {model: set(params) for model, params in raw_unsupported.items() if isinstance(params, list)}
            if isinstance(raw_unsupported, dict)
            else {}
        )
        raw_adaptive = data.get("adaptive_thinking_models", [])
        self._adaptive_thinking_models = set(raw_adaptive) if isinstance(raw_adaptive, list) else set()
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

    def _build_request_kwargs(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        system_prompt: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Build the kwargs dict for a single Anthropic messages API call.

        Single source of truth for both the non-streaming (_single_api_call) and
        streaming (async_request_streaming) request paths — previously each built its
        own copy, which is why a parameter-compatibility bug could be fixed in one
        path and silently left broken in the other (Issue #563 follow-on).
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
            "temperature": temperature,
        }

        if model in self._adaptive_thinking_models:
            # Issue #565: this model rejects the legacy enabled/budget_tokens shape and
            # performs its own uncapped internal reasoning whenever no thinking control is
            # sent at all — confirmed to silently consume the entire max_tokens budget on
            # real-sized prompts, at every reasoning_effort tier, not just "high" (unlike
            # older models, where only "high" ever requested extended thinking). Apply the
            # newer thinking.type.adaptive + output_config.effort shape at every tier so
            # thinking is always bounded and some max_tokens headroom is left for the
            # visible answer. `effort` maps directly from the configured reasoning_effort.
            kwargs["temperature"] = 1  # same API requirement as the legacy shape below
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": reasoning_effort}
        elif reasoning_effort == AI_REASONING_HIGH:
            # Extended thinking for high reasoning effort (legacy shape).
            # Claude API requirements when thinking is enabled:
            #   1. temperature must be exactly 1
            #   2. max_tokens must exceed budget_tokens
            budget = AI_REASONING_BUDGET_TOKENS.get(AI_REASONING_HIGH, 16384)
            kwargs["temperature"] = 1  # required by API — overrides configured value
            if kwargs["max_tokens"] <= budget:
                kwargs["max_tokens"] = budget + 4096  # reserve room for output tokens
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

        # Reactive per-model parameter capability detection (Issue #563 follow-on):
        # drop any parameter already learned to be unsupported for this model. Must
        # run LAST, after the thinking-budget block above, so a model that doesn't
        # accept `temperature` never gets it re-added even when high reasoning effort
        # would otherwise force temperature=1.
        for bad_param in self._unsupported_params.get(model, ()):
            kwargs.pop(bad_param, None)

        return kwargs

    def _log_request_kwargs_decision(self, model: str, reasoning_effort: str, kwargs: dict[str, Any]) -> None:
        """Log the resolved request-shape decision right before an API call (Issue #568).

        Answers "did the adaptive-thinking branch actually fire, and was the learned
        unsupported-parameter strip applied" directly from `ha_logs.py`, for every future
        request — no special live test or diagnostic script needed to see this.
        """
        _LOGGER.debug(
            "Claude request kwargs decision: model=%s reasoning_effort=%s adaptive_thinking=%s "
            "unsupported_params=%s max_tokens=%d has_thinking=%s has_output_config=%s",
            model,
            reasoning_effort,
            model in self._adaptive_thinking_models,
            sorted(self._unsupported_params.get(model, ())),
            kwargs.get("max_tokens", 0),
            "thinking" in kwargs,
            "output_config" in kwargs,
        )

    async def _single_api_call(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        start_time: float,
    ) -> ClaudeResponse:
        """Make exactly one call to the Anthropic messages API and parse the response.

        Raises on failure (RateLimitError/APITimeoutError/APIError/Exception) — this
        method has no retry/fallback policy of its own; callers (_async_call_with_retry's
        backoff loop, and the tier-fallback single attempt) own that decision. Factored
        out so the fallback attempt doesn't duplicate the kwargs-building/response-
        parsing logic (Issue #563).
        """
        kwargs = self._build_request_kwargs(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            user_message=user_message,
        )
        self._log_request_kwargs_decision(model, reasoning_effort, kwargs)

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

        stop_reason = getattr(api_response, "stop_reason", None)
        truncated = stop_reason == "max_tokens"
        truncated_empty = truncated and not content_text
        skill_name = self._extract_skill_name(system_prompt)
        _LOGGER.debug(
            "Claude response finished: skill=%s stop_reason=%s input_tokens=%d output_tokens=%d",
            skill_name,
            stop_reason,
            input_tokens,
            output_tokens,
        )
        if truncated_empty:
            # Issue #563 follow-on: the full max_tokens budget was billed and consumed,
            # but zero visible answer text came back — a different, more severe problem
            # than ordinary truncation (there's no partial content to salvage).
            _LOGGER.warning(
                "Response consumed the full max_tokens budget with zero visible output: "
                "skill=%s model=%s output_tokens=%d max_tokens=%d reasoning_effort=%s — "
                "model may need a larger max_tokens ceiling or a different reasoning_effort",
                skill_name,
                model,
                output_tokens,
                kwargs["max_tokens"],
                reasoning_effort,
            )
        elif truncated:
            _LOGGER.warning(
                "Response truncated: skill=%s stop_reason=max_tokens output_tokens=%d max_tokens=%d",
                skill_name,
                output_tokens,
                kwargs["max_tokens"],
            )

        return ClaudeResponse(
            success=True,
            content=content_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimated_cost,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
            truncated=truncated,
            truncated_empty=truncated_empty,
            resolved_model=model,
        )

    async def _try_tier_fallback(
        self,
        *,
        original_model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        start_time: float,
    ) -> ClaudeResponse | None:
        """Attempt one same-capability-tier replacement after a not-found-model error.

        Returns the successful ClaudeResponse if a same-tier replacement worked, or
        None if no replacement could be determined/found or the replacement attempt
        also failed. Callers must treat None as "fall through to normal error
        handling for the original model" — not as a new failure mode (Issue #563).
        """
        tier = detect_model_tier(original_model)
        if tier is None:
            _LOGGER.warning(
                "Model '%s' rejected by API but its capability tier could not be "
                "determined — no automatic fallback attempted",
                original_model,
            )
            return None

        live_models = await self.async_list_models()
        candidates = [m for m in live_models if m != original_model and detect_model_tier(m) == tier]
        if not candidates:
            _LOGGER.warning(
                "Model '%s' rejected by API — no live '%s'-tier replacement found",
                original_model,
                tier,
            )
            return None

        new_model = candidates[0]  # live list is newest-first per Anthropic's API convention
        _LOGGER.warning(
            "Model '%s' rejected by API (deprecated or invalid) — falling back to '%s' (%s tier)",
            original_model,
            new_model,
            tier,
        )
        try:
            return await self._single_api_call(
                system_prompt=system_prompt,
                user_message=user_message,
                model=new_model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                start_time=start_time,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Fallback model '%s' also failed: %s", new_model, exc)
            return None

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
                response = await self._single_api_call(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    start_time=start_time,
                )
                if response.truncated_empty and model not in self._adaptive_thinking_models:
                    # Issue #565: full max_tokens budget consumed with zero visible
                    # answer text — a newer model's uncapped implicit thinking left no
                    # room for the answer. Unlike the streaming path, nothing has been
                    # shown to the caller yet (this is a single atomic response), so a
                    # same-attempt retry with bounded thinking is safe.
                    self._adaptive_thinking_models.add(model)
                    _LOGGER.warning(
                        "Model '%s' consumed the full budget with zero output — retrying "
                        "with adaptive thinking control",
                        model,
                    )
                    return await self._single_api_call(
                        system_prompt=system_prompt,
                        user_message=user_message,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        reasoning_effort=reasoning_effort,
                        start_time=start_time,
                    )
                return response

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
                # Issue #563 follow-on: a request parameter (e.g. temperature) the
                # configured model no longer accepts won't succeed on retry either —
                # learn it, drop it, and retry immediately with the same model. This
                # and the NotFoundError branch below are mutually exclusive error
                # shapes (a 400 param-deprecation vs a 404 model-not-found), so only
                # one generic "last_error" tail is needed for whichever wasn't hit.
                bad_param = _detect_deprecated_param(str(exc))
                needs_adaptive = (
                    bad_param is None
                    and model not in self._adaptive_thinking_models
                    and _detect_adaptive_thinking_required(str(exc))
                )
                if bad_param is not None:
                    self._unsupported_params.setdefault(model, set()).add(bad_param)
                    _LOGGER.warning(
                        "Model '%s' rejected parameter '%s' as deprecated — retrying without it",
                        model,
                        bad_param,
                    )
                    try:
                        return await self._single_api_call(
                            system_prompt=system_prompt,
                            user_message=user_message,
                            model=model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            reasoning_effort=reasoning_effort,
                            start_time=start_time,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        last_error = f"API error even after dropping '{bad_param}': {retry_exc}"
                        _LOGGER.warning("Retry without '%s' also failed: %s", bad_param, retry_exc)
                elif needs_adaptive:
                    # Issue #565: model rejected the legacy thinking shape outright —
                    # learn it and retry with thinking.type.adaptive + output_config.effort.
                    self._adaptive_thinking_models.add(model)
                    _LOGGER.warning(
                        "Model '%s' rejected the legacy thinking shape — retrying with adaptive thinking",
                        model,
                    )
                    try:
                        return await self._single_api_call(
                            system_prompt=system_prompt,
                            user_message=user_message,
                            model=model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            reasoning_effort=reasoning_effort,
                            start_time=start_time,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        last_error = f"API error even after switching to adaptive thinking: {retry_exc}"
                        _LOGGER.warning("Retry with adaptive thinking also failed: %s", retry_exc)
                else:
                    # Issue #563: an invalid/deprecated model won't succeed on retry —
                    # skip the backoff loop and try one same-tier replacement instead.
                    # isinstance() against NotFoundError only when it's actually
                    # importable (import-guarded at module load) — never a bare
                    # `except NotFoundError` clause, which would raise TypeError if
                    # NotFoundError were None.
                    if NotFoundError is not None and isinstance(exc, NotFoundError):
                        fallback = await self._try_tier_fallback(
                            original_model=model,
                            system_prompt=system_prompt,
                            user_message=user_message,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            reasoning_effort=reasoning_effort,
                            start_time=start_time,
                        )
                        if fallback is not None:
                            return fallback
                        # No usable same-tier replacement — fall through to the
                        # normal error handling/backoff below.
                    else:
                        # Issue #568: a 400-class error that matches neither known
                        # capability-detection pattern and isn't a NotFoundError — e.g. a
                        # genuinely new failure shape (a context-length-exceeded error is
                        # a documented risk for newer models with a different tokenizer).
                        # Mark it distinctly so it's immediately greppable and never
                        # mistaken for one of the two known, already-handled shapes.
                        _LOGGER.warning(
                            "Unrecognized API error shape for model '%s' — full message: %s",
                            model,
                            exc,
                        )
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
