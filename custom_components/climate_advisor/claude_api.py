"""Centralized AI client for Climate Advisor.

Two transports share one set of resilience guards (circuit breaker, daily limits, request
history): Home Assistant AI Task (Issue #1336, the main path: any AI integration the
occupant set up in Home Assistant) and the deprecated built-in Claude client (Anthropic SDK).
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .ai_settings_sync import provider_of
from .const import (
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_ERROR_CONTEXT_LIMIT,
    AI_ERROR_NO_ENTITY,
    AI_ERROR_OUTPUT_LIMIT,
    AI_ERROR_PROVIDER,
    AI_MAX_RETRIES,
    AI_MODEL_CAPABILITIES,
    AI_MODEL_CAPABILITIES_DEFAULT,
    AI_MODELS,
    AI_PROVIDER_HA_AI_TASK,
    AI_REQUEST_HISTORY_CAP,
    AI_RETRY_BASE_DELAY_SECONDS,
    AI_TASK_NAME,
    AI_TASK_TIMEOUT_SECONDS,
    AI_THINKING_SHAPE_ADAPTIVE,
    CONF_AI_API_KEY,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_RPD,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_MAX_TOKENS,
    CONF_AI_MODEL,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TASK_ENTITY,
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

# Per-model cost rates (USD per million tokens)
_MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-sonnet": {"input": 3.0, "output": 15.0},
    "claude-opus": {"input": 15.0, "output": 75.0},
    "claude-haiku": {"input": 0.80, "output": 4.0},
}

# Issue #1341 (D9): the occupant-facing sections of an AI report are written in the Home Assistant language. The
# prompt itself and every context section stay English (render_en); only this instruction names the output language.
# `## SECTION` headers stay English: parse_investigation_response() splits on them.
# Issue #1657: any language HA supports, not only the shipped ones. The instruction always carries the language
# code; these names are added for the shipped languages, as a plain-English hint for the model.
OUTPUT_LANGUAGE_NAMES: dict[str, str] = {
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "nl": "Dutch",
    "ja": "Japanese",
    "pt": "Portuguese",
    "it": "Italian",
    "pl": "Polish",
    "uk": "Ukrainian",
    "ko": "Korean",
    "hi": "Hindi",
    "ar": "Arabic",
    "ru": "Russian",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese (Taiwan)",
    "tr": "Turkish",
}


def output_language_instruction(language: str | None) -> str:
    """The "answer in <language>" instruction for a language code, or "" for English (any region) / no code.

    English adds nothing, so the English prompt (and every English-asserting test) is byte-identical.
    """
    code = (language or "").strip()
    if not code or code.split("-")[0].lower() == "en":
        return ""
    name = OUTPUT_LANGUAGE_NAMES.get(code)
    target = f"{name} (language code {code})" if name else f"the language with code {code}"
    return (
        f"=== OUTPUT LANGUAGE ===\n"
        f"  Write the occupant-facing text of every section of your report in {target}.\n"
        f"  Keep the section headers (## prefix) exactly as listed in English: they are parsed by exact match.\n"
        f"  Keep [source: ..., value: ...] citations, entity ids, field names, numbers and quoted log records"
        f" verbatim.\n"
    )


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
    error_code: str | None = None  # Issue #1336: a stable AI_ERROR_* code the occupant is told about


class AIProviderError(RuntimeError):
    """A failed AI request that carries a stable ``AI_ERROR_*`` code (Issue #1336).

    Raised by ``async_request_streaming()`` on the AI Task path so the streaming caller can put the code on its
    SSE error event. ``str(exc)`` is the plain-English error.
    """

    def __init__(self, message: str, error_code: str | None) -> None:
        super().__init__(message)
        self.error_code = error_code


# Issue #1336: how HA's AI integrations report a reply that hit a limit (HA 2026.10.4 source).
# Anthropic raises HomeAssistantError(translation_key=...); OpenAI raises plain text.
_OUTPUT_LIMIT_KEYS = frozenset({"response_max_tokens"})
_CONTEXT_LIMIT_KEYS = frozenset({"response_context_window_exceeded"})
_OUTPUT_LIMIT_TEXT = ("max output tokens", "response_max_tokens")
_CONTEXT_LIMIT_TEXT = ("response_context_window_exceeded",)
_NO_ENTITY_KEYS = frozenset({"no_entity"})
_NO_ENTITY_TEXT = "no entity_id provided and no preferred entity set"


def classify_ai_task_error(exc: BaseException) -> str:
    """Map an AI Task failure to the ``AI_ERROR_*`` code the occupant is shown (Issue #1336).

    Reads the HomeAssistantError translation key first, then the message. ``ai_task`` not set up at all
    (a ``KeyError`` on its hass.data, or no ``ai_task`` package) means there is no AI Task entity to use.
    Anything else, a timeout included, is a provider error.
    """
    key = str(getattr(exc, "translation_key", None) or "")
    text = str(exc).lower()
    if key in _OUTPUT_LIMIT_KEYS or any(t in text for t in _OUTPUT_LIMIT_TEXT):
        return AI_ERROR_OUTPUT_LIMIT
    if key in _CONTEXT_LIMIT_KEYS or any(t in text for t in _CONTEXT_LIMIT_TEXT):
        return AI_ERROR_CONTEXT_LIMIT
    if key in _NO_ENTITY_KEYS or _NO_ENTITY_TEXT in text or isinstance(exc, (KeyError, ImportError)):
        return AI_ERROR_NO_ENTITY
    return AI_ERROR_PROVIDER


async def _async_ai_task_generate(hass: Any, *, entity_id: str | None, instructions: str) -> Any:
    """Call ``ai_task.async_generate_data`` (the approved HA Boundary Rule exception, Issue #1336).

    Imported here, not at module level, so an install without ``ai_task`` still loads Climate Advisor.
    """
    from homeassistant.components import ai_task  # noqa: PLC0415

    return await ai_task.async_generate_data(
        hass,
        task_name=AI_TASK_NAME,
        entity_id=entity_id,
        instructions=instructions,
    )


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


async def fetch_available_models(api_key: str, hass: Any | None = None) -> list[str]:
    """Fetch the live list of available Claude model IDs from Anthropic.

    This function must never raise — on any failure (no API key, network error,
    an SDK version that lacks `.models`, an API error) it falls back to the
    static AI_MODELS default list. Used both by ClaudeAPIClient.async_list_models()
    (cached) and directly by config_flow.py (uncached — a config-flow render is a
    one-off interaction, not a hot path) so there is exactly one implementation of
    "how to ask Anthropic what models exist and what to do if that fails" (Issue #563).

    Args:
        api_key: the Anthropic API key to use.
        hass: Optional HomeAssistant instance. When given, the (blocking)
            AsyncAnthropic construction is offloaded to the executor. Optional
            and defaults to None so existing callers/tests without a hass
            reference are unaffected.
    """
    if not api_key or not ANTHROPIC_AVAILABLE:
        return list(AI_MODELS)
    try:
        if hass is not None:
            client = await hass.async_add_executor_job(functools.partial(AsyncAnthropic, api_key=api_key))
        else:
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
        # Issue #1336: the AI Task transport needs Home Assistant; set by the coordinator that attaches the
        # (install-wide, shared) client. Built-in Claude does not use it.
        self.hass: Any = None
        # Issue #1336: the client is shared by every zone, and every zone's state file carries its stats; only the
        # first zone to restore them applies them (they are the same install-wide counters).
        self.stats_restored: bool = False

        if self.uses_ai_task:
            _LOGGER.debug("AI client uses Home Assistant AI Task — no API key needed")
        elif self._client is None and ANTHROPIC_AVAILABLE:
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def provider(self) -> str:
        """The configured provider (``ha_ai_task`` or ``builtin_claude``), Issue #1336."""
        return provider_of(self._config)

    @property
    def uses_ai_task(self) -> bool:
        """True when requests go through Home Assistant AI Task (Issue #1336)."""
        return self.provider == AI_PROVIDER_HA_AI_TASK

    @property
    def ai_task_entity(self) -> str:
        """The chosen AI Task entity, or "" for Home Assistant's preferred one."""
        return str(self._config.get(CONF_AI_TASK_ENTITY) or "")

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

        if self.uses_ai_task:
            # Issue #1336: no budget (cost is the provider's, reported as 0) and no retry (a provider that fails
            # at its output limit fails the same way again; ai_task has no timeout of its own, so each attempt
            # can take up to AI_TASK_TIMEOUT_SECONDS).
            response = await self._async_ai_task_call(system_prompt, user_message)
            self._record_request_outcome(response, triggered_by, system_prompt)
            return response

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
        self._record_request_outcome(response, triggered_by, system_prompt)
        return response

    def _record_request_outcome(self, response: ClaudeResponse, triggered_by: str, system_prompt: str) -> None:
        """Circuit breaker, budget, daily counters and request history after one request (both transports)."""
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

    async def _async_ai_task_call(self, system_prompt: str, user_message: str) -> ClaudeResponse:
        """One Home Assistant AI Task request (Issue #1336). Never raises.

        The instructions are the system prompt, a blank line, then the context (AI Task has no separate system
        prompt for callers). Tokens and cost are unknown to callers and reported as 0.
        """
        start_time = time.monotonic()
        entity_id = self.ai_task_entity or None
        skill_name = self._extract_skill_name(system_prompt)

        def _failure(error: str, code: str) -> ClaudeResponse:
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=(time.monotonic() - start_time) * 1000.0,
                error=error,
                error_code=code,
            )

        if self.hass is None:
            _LOGGER.warning("AI Task request skipped: client not attached to Home Assistant skill=%s", skill_name)
            return _failure("AI Task client is not attached to Home Assistant", AI_ERROR_PROVIDER)

        _LOGGER.debug(
            "AI Task request: skill=%s entity=%s instructions_chars=%d",
            skill_name,
            entity_id or "preferred",
            len(system_prompt) + len(user_message),
        )
        try:
            async with asyncio.timeout(AI_TASK_TIMEOUT_SECONDS):
                result = await _async_ai_task_generate(
                    self.hass,
                    entity_id=entity_id,
                    instructions=system_prompt + "\n\n" + user_message,
                )
        except Exception as exc:  # noqa: BLE001 - every provider failure goes to the breaker and the fallback
            code = classify_ai_task_error(exc)
            if isinstance(exc, TimeoutError):
                error = f"AI Task timed out after {AI_TASK_TIMEOUT_SECONDS} s"
            else:
                error = f"AI Task error: {exc}"
            _LOGGER.warning(
                "AI Task request failed: skill=%s entity=%s error_code=%s error=%s",
                skill_name,
                entity_id or "preferred",
                code,
                error,
            )
            return _failure(error, code)

        data = getattr(result, "data", None)
        text = data if isinstance(data, str) else ("" if data is None else str(data))
        latency_ms = (time.monotonic() - start_time) * 1000.0
        if not text.strip():
            _LOGGER.warning(
                "AI Task request failed: empty reply skill=%s entity=%s",
                skill_name,
                entity_id or "preferred",
            )
            return _failure("AI Task returned an empty reply", AI_ERROR_PROVIDER)

        _LOGGER.debug(
            "AI Task response finished: skill=%s chars=%d latency_ms=%.0f",
            skill_name,
            len(text),
            latency_ms,
        )
        return ClaudeResponse(
            success=True,
            content=text,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
            latency_ms=latency_ms,
            stop_reason="end_turn",
        )

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
        Streaming never retries on error (Issue #572) — the request shape is built
        from the verified AI_MODEL_CAPABILITIES table up front, so any API error
        increments the circuit-breaker counter and re-raises immediately.

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

        if self.uses_ai_task:
            # Issue #1336: AI Task does not stream to callers — the whole reply arrives at once, as one text event.
            response = await self._async_ai_task_call(system_prompt, user_message)
            if not response.success:
                self._record_streaming_failure()
                raise AIProviderError(response.error or "AI Task request failed", response.error_code)
            yield {"type": "text", "text": response.content}
            yield {"type": "stop", "stop_reason": "end_turn", "truncated_empty": False}
            self._record_streaming_success(system_prompt, triggered_by, 0, 0, 0.0, response.latency_ms)
            return

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
        any_text_yielded = False  # visible answer text — used to detect the "burned
        # the whole budget, produced no answer" case (Issue #563 follow-on); thinking
        # output alone doesn't give the caller an answer either
        final_msg = None

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
                        yield {"type": "thinking", "text": getattr(delta, "thinking", "")}
                    elif delta_type == "text_delta":
                        any_text_yielded = True
                        yield {"type": "text", "text": getattr(delta, "text", "")}
                final_msg = await stream.get_final_message()

        except (RateLimitError, APITimeoutError, APIError, Exception) as exc:
            # Issue #572: capability-appropriate kwargs are built from
            # AI_MODEL_CAPABILITIES up front, so an APIError here means a genuinely
            # new/unexpected failure for a model this table claims to have verified —
            # not a known, retryable capability mismatch. Log it distinctly and
            # re-raise; no in-place retry.
            if isinstance(exc, APIError):
                _LOGGER.warning(
                    "Unexpected API error for model '%s' (streaming): %s",
                    resolved_model,
                    exc,
                )

            self._record_streaming_failure()
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
            # Issue #572: kwargs were built from AI_MODEL_CAPABILITIES, so this model's
            # request shape is supposed to already be correct — this symptom (full
            # budget billed, zero visible answer text) means Anthropic changed this
            # model's behavior out from under a known-good table entry. Log-only: no
            # automatic learning/retry. Verify AI_MODEL_CAPABILITIES in const.py.
            _LOGGER.warning(
                "Streaming response consumed the full max_tokens budget with zero visible "
                "output despite a verified request shape: skill=%s model=%s "
                "output_tokens=%d max_tokens=%d reasoning_effort=%s — Anthropic may have "
                "changed this model's behavior; check AI_MODEL_CAPABILITIES in const.py",
                skill_name,
                resolved_model,
                output_tokens,
                kwargs["max_tokens"],
                resolved_reasoning,
            )
        elif truncated:
            _LOGGER.warning(
                "Response truncated: skill=%s stop_reason=max_tokens output_tokens=%d max_tokens=%d",
                skill_name,
                output_tokens,
                kwargs["max_tokens"],
            )
        yield {"type": "stop", "stop_reason": stop_reason, "truncated_empty": truncated_empty}
        self._record_streaming_success(
            system_prompt, triggered_by, input_tokens, output_tokens, estimated_cost, latency_ms
        )

    def _record_streaming_failure(self) -> None:
        """Count a failed streaming request on the circuit breaker (both transports)."""
        self._circuit_breaker.consecutive_failures += 1
        self._error_count += 1
        if self._circuit_breaker.consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_breaker.state = _CB_OPEN
            self._circuit_breaker.opened_at = time.monotonic()
            _LOGGER.error(
                "Circuit breaker opened after %d consecutive failures",
                self._circuit_breaker.consecutive_failures,
            )

    def _record_streaming_success(
        self,
        system_prompt: str,
        triggered_by: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        latency_ms: float,
    ) -> None:
        """Reset the breaker and record counters/history after a completed streaming request (both transports)."""
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

    async def async_test_connection(self, hass: Any | None = None) -> tuple[bool, str]:
        """Validate the configured API key with a minimal API call.

        Args:
            hass: Optional HomeAssistant instance. When given, the (blocking —
                reads local config, loads the TLS cert bundle) AsyncAnthropic
                construction is offloaded to the executor instead of running
                inline on the event loop. Optional and defaults to None so
                existing callers/tests that construct this client without a
                hass reference are unaffected.

        Returns:
            (True, "Connected successfully") on success, or (False, error_message).

        """
        if not ANTHROPIC_AVAILABLE:
            return False, "anthropic package is not installed"

        api_key = self._config.get(CONF_AI_API_KEY, "")
        if not api_key:
            return False, "No API key configured"

        if hass is not None:
            test_client = await hass.async_add_executor_job(functools.partial(AsyncAnthropic, api_key=api_key))
        else:
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
        elif not self.uses_ai_task and not self._check_budget():
            status = "budget_exceeded"
        elif (self.hass is None) if self.uses_ai_task else (self._client is None):
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
            # Issue #1336: on AI Task the model is chosen in the AI integration, not here ("" = not known).
            "model": "" if self.uses_ai_task else self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL),
            "provider": self.provider,
            "ai_task_entity": self.ai_task_entity,
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
        except KeyError, ValueError:
            self._rate_counters.counter_date = date.today()
        self._investigator_requests_today = int(data.get("investigator_requests_today", 0))
        self._investigator_requests_date = data.get("investigator_requests_date", "")
        # Apply daily reset if rebooted after midnight
        self._reset_daily_counters_if_needed()
        self.stats_restored = True
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

        if self.uses_ai_task:
            # Issue #1336: AI Task needs no Anthropic client; one already built stays unused until a switch back.
            return
        if new_key != old_key or (self._client is None and new_key):
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
        streaming (async_request_streaming) request paths. Issue #572: the request
        shape is a pure lookup against the hardcoded AI_MODEL_CAPABILITIES table — no
        live discovery, no per-model learned state. A model not in the table falls
        back to AI_MODEL_CAPABILITIES_DEFAULT (the legacy shape, proven safe for years
        prior to claude-sonnet-5) and logs a WARNING naming it.
        """
        caps = AI_MODEL_CAPABILITIES.get(model)
        if caps is None:
            _LOGGER.warning(
                "Model '%s' is not in AI_MODEL_CAPABILITIES — using the safe legacy "
                "default request shape; add a verified entry to const.py for this model",
                model,
            )
            caps = AI_MODEL_CAPABILITIES_DEFAULT

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        if caps["supports_temperature"]:
            kwargs["temperature"] = temperature

        if caps["thinking_shape"] == AI_THINKING_SHAPE_ADAPTIVE:
            # Newer models (confirmed: claude-sonnet-5, claude-opus-5) reject the
            # legacy enabled/budget_tokens shape outright and perform their own
            # uncapped internal reasoning whenever no thinking control is sent at all,
            # at every reasoning_effort tier — not just "high" like older models.
            # `effort` maps directly from the configured reasoning_effort.
            if caps["supports_temperature"]:
                kwargs["temperature"] = 1  # required by the API when thinking is active
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": reasoning_effort}
        # Issue #1336: the legacy extended-thinking shape was sent only at "high" reasoning, which is no longer
        # offered (low/medium only; a stored "high" is migrated to "medium"). Legacy-shape models send no thinking.

        return kwargs

    def _log_request_kwargs_decision(self, model: str, reasoning_effort: str, kwargs: dict[str, Any]) -> None:
        """Log the resolved request-shape decision right before an API call.

        Answers "what shape did this request actually use" directly from
        `ha_logs.py`, for every request — no special live test or diagnostic script
        needed to see this.
        """
        _LOGGER.debug(
            "Claude request kwargs decision: model=%s reasoning_effort=%s "
            "thinking_type=%s max_tokens=%d has_temperature=%s has_output_config=%s",
            model,
            reasoning_effort,
            kwargs.get("thinking", {}).get("type", "none"),
            kwargs.get("max_tokens", 0),
            "temperature" in kwargs,
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
                if response.truncated_empty:
                    # Issue #572: kwargs were built from AI_MODEL_CAPABILITIES, so this
                    # model's request shape is supposed to already be correct — this
                    # symptom (full budget billed, zero visible answer text) means
                    # Anthropic changed this model's behavior out from under a
                    # known-good table entry. Log-only: no automatic retry.
                    _LOGGER.warning(
                        "Model '%s' consumed the full budget with zero output despite a "
                        "verified request shape — Anthropic may have changed this "
                        "model's behavior; check AI_MODEL_CAPABILITIES in const.py",
                        model,
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
                # Issue #572: request shape is built from the verified
                # AI_MODEL_CAPABILITIES table up front, so an APIError here is not a
                # known, retryable capability mismatch — except a deprecated/invalid
                # model ID (404), which won't succeed on retry either and gets one
                # same-tier replacement attempt instead.
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
                    # A 400-class error for a model this table claims to have
                    # verified — a genuinely new/unexpected failure shape (e.g. a
                    # context-length-exceeded error is a documented risk for newer
                    # models with a different tokenizer). Log it distinctly so it's
                    # immediately greppable, and check AI_MODEL_CAPABILITIES.
                    _LOGGER.warning(
                        "Unexpected API error for model '%s': %s",
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
