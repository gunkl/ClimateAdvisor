"""Centralized Claude API client for Climate Advisor."""

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

from .ai_backend import (
    CREDENTIAL_ACCOUNT,
    CREDENTIAL_API_KEY,
    PROVIDERS,
    USER_NOT_CHECKED,
    AIBackendError,
    AIUserNotAllowedError,
    ProviderSpec,
    get_provider,
)
from .const import (
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    AI_CIRCUIT_BREAKER_THRESHOLD,
    AI_MAX_RETRIES,
    AI_MODEL_CAPABILITIES,
    AI_MODEL_CAPABILITIES_DEFAULT,
    AI_MODELS,
    AI_REASONING_BUDGET_TOKENS,
    AI_REASONING_HIGH,
    AI_REQUEST_HISTORY_CAP,
    AI_RETRY_BASE_DELAY_SECONDS,
    AI_THINKING_SHAPE_ADAPTIVE,
    CONF_AI_API_KEY,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_RPD,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_MAX_TOKENS,
    CONF_AI_MODEL,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_PROVIDER,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TEMPERATURE,
    DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
    DEFAULT_AI_INVESTIGATOR_ENABLED,
    DEFAULT_AI_INVESTIGATOR_RPD,
    DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_PROVIDER,
    DEFAULT_AI_REASONING_EFFORT,
    DEFAULT_AI_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

try:
    from anthropic import APIError, APITimeoutError, AsyncAnthropic, RateLimitError

    ANTHROPIC_AVAILABLE = True
except Exception:  # noqa: BLE001 - a broken SDK raises more than ImportError; AI must never fail setup
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
except Exception:  # noqa: BLE001 - as above
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
    no_answer: bool = False  # the model refused or returned nothing: shown as an error, no fallback report
    no_retry: bool = False  # failure the backend flagged AIBackendError.no_retry: not retried,
    # not counted toward the circuit breaker
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


def _api_error_category(exc: Exception) -> str:
    """Map an Anthropic SDK error to a neutral AIBackendError category."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return "permanent"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota"
    if status == 429:
        return "rate_limit"
    return "transient" if status >= 500 else "permanent"


class AnthropicBackend:
    """AIBackend for the Anthropic Messages API: SDK construction, request shape and calls.

    Raises ``AIBackendError`` for SDK failures. Retry, circuit breaking, rate limits
    and budget live in ``ClaudeAPIClient``, not here.

    Networking: uses the official Anthropic Python SDK (AsyncAnthropic) declared as
    ``anthropic>=0.49.0`` in manifest.json requirements — no raw HTTP client is used.
    """

    def __init__(self, config: dict[str, Any], client: Any | None = None) -> None:
        """Initialize the backend.

        Args:
            config: HA config entry data dict containing CONF_AI_* values.
            client: Optional AsyncAnthropic instance for dependency injection (tests).

        """
        self._config = config
        self.client: Any = client

        if self.client is None and ANTHROPIC_AVAILABLE:
            api_key = config.get(CONF_AI_API_KEY, "")
            if api_key:
                self.client = AsyncAnthropic(api_key=api_key)
                _LOGGER.debug("API client initialized — key configured")
            else:
                _LOGGER.warning("No AI API key configured; Claude API client will not be active")

    @property
    def inactive_reason(self) -> str | None:
        """Why requests cannot be made, or None when the SDK client exists."""
        if self.client is None:
            return "Anthropic client not initialized (missing package or API key)"
        return None

    def for_request(self) -> AnthropicBackend:
        """The key cannot change hands mid-request in a way that matters: no binding needed."""
        return self

    def update_config(self, config: dict[str, Any]) -> None:
        """Apply updated config; the SDK client is recreated if the API key changed."""
        old_key = self._config.get(CONF_AI_API_KEY, "")
        new_key = config.get(CONF_AI_API_KEY, "")

        self._config = config

        if new_key != old_key:
            if new_key and ANTHROPIC_AVAILABLE:
                self.client = AsyncAnthropic(api_key=new_key)
                _LOGGER.debug("API client re-initialized — key updated")
            else:
                self.client = None
                _LOGGER.warning("AI API key removed; Claude API client deactivated")

    async def async_list_models(self) -> list[str]:
        """Return live Claude model IDs (static fallback on failure)."""
        return await fetch_available_models(self._config.get(CONF_AI_API_KEY, ""))

    async def async_fetch_models(self) -> list[str]:
        """Live Claude model IDs; the lookup falls back to the static list, so it does not raise."""
        return await self.async_list_models()

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

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
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

    def build_request_kwargs(
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

        Single source of truth for both the non-streaming (async_request) and
        streaming (async_stream) request paths. Issue #572: the request
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
        elif reasoning_effort == AI_REASONING_HIGH:
            # Extended thinking for high reasoning effort (legacy shape).
            # Claude API requirements when thinking is enabled:
            #   1. temperature must be exactly 1
            #   2. max_tokens must exceed budget_tokens
            budget = AI_REASONING_BUDGET_TOKENS.get(AI_REASONING_HIGH, 16384)
            if caps["supports_temperature"]:
                kwargs["temperature"] = 1  # required by the API — overrides configured value
            if kwargs["max_tokens"] <= budget:
                kwargs["max_tokens"] = budget + 4096  # reserve room for output tokens
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

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

    async def async_request(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
    ) -> ClaudeResponse:
        """Make exactly one call to the Anthropic messages API and parse the response.

        Raises AIBackendError for SDK failures (and the raw exception for anything
        else) — no retry/fallback policy here; ClaudeAPIClient owns that decision.
        ``latency_ms`` is measured from this call; the wrapper overrides it with the
        time since its retry loop began.
        """
        start_time = time.monotonic()
        kwargs = self.build_request_kwargs(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            user_message=user_message,
        )
        self._log_request_kwargs_decision(model, reasoning_effort, kwargs)

        try:
            api_response = await self.client.messages.create(**kwargs)
        except RateLimitError as exc:
            raise AIBackendError(str(exc), "rate_limit") from exc
        except APITimeoutError as exc:
            raise AIBackendError(str(exc), "transient", timeout=True) from exc
        except APIError as exc:
            raise self._wrap_api_error(exc) from exc

        latency_ms = (time.monotonic() - start_time) * 1000.0
        input_tokens: int = getattr(api_response.usage, "input_tokens", 0)
        output_tokens: int = getattr(api_response.usage, "output_tokens", 0)
        estimated_cost = self.estimate_cost(model, input_tokens, output_tokens)

        # Extract text content from response blocks
        content_text = ""
        for block in api_response.content:
            if hasattr(block, "text"):
                content_text += block.text

        stop_reason = getattr(api_response, "stop_reason", None)
        truncated = stop_reason == "max_tokens"
        truncated_empty = truncated and not content_text
        skill_name = ClaudeAPIClient._extract_skill_name(system_prompt)
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

    @staticmethod
    def _wrap_api_error(exc: Exception) -> AIBackendError:
        """Wrap a generic Anthropic APIError; flags a 404 not-found-model (Issue #563).

        isinstance() against NotFoundError only when it's actually importable
        (import-guarded at module load) — never a bare `except NotFoundError` clause,
        which would raise TypeError if NotFoundError were None.
        """
        return AIBackendError(
            str(exc),
            _api_error_category(exc),
            model_not_found=NotFoundError is not None and isinstance(exc, NotFoundError),
        )

    async def async_stream(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a response as ``text``/``thinking`` events, then a ``stop`` event with usage.

        Streaming never retries on error (Issue #572) — the request shape is built
        from the verified AI_MODEL_CAPABILITIES table up front. SDK failures raise
        AIBackendError; anything else propagates unchanged.
        """
        any_text_yielded = False  # visible answer text — used to detect the "burned
        # the whole budget, produced no answer" case (Issue #563 follow-on); thinking
        # output alone doesn't give the caller an answer either
        final_msg = None

        kwargs = self.build_request_kwargs(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            system_prompt=system_prompt,
            user_message=user_message,
        )
        self._log_request_kwargs_decision(model, reasoning_effort, kwargs)
        try:
            async with self.client.messages.stream(**kwargs) as stream:
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
        except RateLimitError as exc:
            raise AIBackendError(str(exc), "rate_limit") from exc
        except APITimeoutError as exc:
            raise AIBackendError(str(exc), "transient", timeout=True) from exc
        except APIError as exc:
            raise self._wrap_api_error(exc) from exc

        input_tokens: int = getattr(final_msg.usage, "input_tokens", 0)
        output_tokens: int = getattr(final_msg.usage, "output_tokens", 0)
        estimated_cost = self.estimate_cost(model, input_tokens, output_tokens)

        stop_reason = getattr(final_msg, "stop_reason", None)
        truncated = stop_reason == "max_tokens"
        truncated_empty = truncated and not any_text_yielded
        skill_name = ClaudeAPIClient._extract_skill_name(system_prompt)
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
        yield {
            "type": "stop",
            "stop_reason": stop_reason,
            "truncated_empty": truncated_empty,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost": estimated_cost,
        }


def _anthropic_resolve_request_options(config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Per-request override, else the configured Claude setting, else the default."""

    def _pick(name: str, conf_key: str, default: Any) -> Any:
        value = overrides.get(name)
        return value if value is not None else config.get(conf_key, default)

    return {
        "max_tokens": _pick("max_tokens", CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS),
        "temperature": _pick("temperature", CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE),
        "model": _pick("model", CONF_AI_MODEL, DEFAULT_AI_MODEL),
        "reasoning_effort": _pick("reasoning_effort", CONF_AI_REASONING_EFFORT, DEFAULT_AI_REASONING_EFFORT),
    }


async def _anthropic_list_models(config: dict[str, Any], hass: Any | None = None) -> list[str]:
    """Live model list for the config flow (uncached; never raises)."""
    return await fetch_available_models(config.get(CONF_AI_API_KEY, ""), hass=hass)


async def _anthropic_validate_credentials(config: dict[str, Any], hass: Any | None = None) -> tuple[bool, str]:
    """Probe the configured API key. The backend constructor does blocking I/O, so build it in the executor."""
    if hass is not None:
        backend = await hass.async_add_executor_job(functools.partial(AnthropicBackend, config))
    else:
        backend = AnthropicBackend(config)
    return await backend.async_test_connection(hass=hass)


ANTHROPIC_SPEC = ProviderSpec(
    id="anthropic",
    label="Anthropic Claude",
    display_name="Claude AI",
    api_name="Claude API",
    credential_kind=CREDENTIAL_API_KEY,
    build_backend=AnthropicBackend,
    usage_url="https://console.anthropic.com/settings/usage",
    tracks_cost=True,
    supports_tuning=True,
    supports_tier_fallback=True,
    plan_restricted=False,
    resolve_request_options=_anthropic_resolve_request_options,
    list_models=_anthropic_list_models,
    validate_credentials=_anthropic_validate_credentials,
    credential_setting=CONF_AI_API_KEY,
    uses_claude_wording=True,
    model_setting=CONF_AI_MODEL,
    default_model=DEFAULT_AI_MODEL,
    fallback_models=tuple(AI_MODELS),
)
PROVIDERS.setdefault(ANTHROPIC_SPEC.id, ANTHROPIC_SPEC)


class ClaudeAPIClient:
    """Shared AI client wrapper: circuit breaker, rate limits, status, budget and history.

    Provider transport lives behind ``AIBackend`` (selected via ``CONF_AI_PROVIDER``
    and ``PROVIDERS``); this class has no per-provider branches — behaviour differences
    come from the ``ProviderSpec`` capability flags. The name is historical.
    """

    def __init__(
        self,
        config: dict[str, Any],
        client: Any | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            config: HA config entry data dict containing CONF_AI_* values.
            client: Optional transport handed to the provider's backend: an SDK client for
                Claude, or the shared account manager for an account provider.

        """
        self._config = config
        self._provider_id: str = config.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER)
        self._spec: ProviderSpec | None = get_provider(self._provider_id)
        self._backend: Any = None
        self._stop_reason: str | None = None
        if self._spec is not None:
            self._backend = self._spec.build_backend(config, client)
        else:
            # Never a setup failure: status reports "error" and no AI is available.
            _LOGGER.error("Unknown AI provider '%s'; AI is unavailable for this entry", self._provider_id)

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

    async def async_list_models(self) -> list[str]:
        """Return the provider's available model IDs, cached for _MODEL_LIST_CACHE_TTL_SECONDS.

        Thin cached wrapper around the backend's model list; for Claude see
        fetch_available_models() for fallback behavior on failure (Issue #563).
        """
        now = time.monotonic()
        if self._models_cache is not None and (now - self._models_cache_ts) < _MODEL_LIST_CACHE_TTL_SECONDS:
            return self._models_cache
        models = await self._backend.async_list_models() if self._backend is not None else list(AI_MODELS)
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
        requester_id: Any = USER_NOT_CHECKED,
    ) -> ClaudeResponse:
        """Send a request to the zone's AI provider with resilience guards.

        Args:
            system_prompt: System-level instructions for the model.
            user_message: The user turn content.
            max_tokens: Override the configured max_tokens for this request.
            temperature: Override the configured temperature for this request.
            model: Override the configured model for this request.
            reasoning_effort: Override the configured reasoning effort for this request.
            triggered_by: "manual" (user-initiated) or "auto" (scheduled/automated).
            requester_id: The HA user behind this request, checked against the provider
                the zone is on right now. There is no await between this check and the
                backend call, and the request then finishes on that provider's backend and
                accounting even if the zone switches meanwhile.

        Returns:
            ClaudeResponse with result or failure metadata.

        Raises:
            AIUserNotAllowedError: The requester may not use the current provider. Nothing was sent.

        """
        if requester_id is not USER_NOT_CHECKED and not self.check_user_allowed(requester_id):
            raise AIUserNotAllowedError

        self._reset_daily_counters_if_needed()

        # Guard: a plan-restricted provider only runs manual starts
        if self._plan_restricted_trigger_refused(triggered_by):
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error="This AI provider only runs requests started manually by its connected owner",
            )

        # Guard: circuit breaker
        if not self._check_circuit_breaker():
            _LOGGER.warning("%s circuit breaker is open; skipping request", self._api_name)
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
                "Daily %s request limit reached; skipping %s call",
                triggered_by,
                self._api_name,
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
            _LOGGER.warning("Monthly AI budget exceeded; skipping %s call", self._api_name)
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

        inactive_reason = self._inactive_reason()
        if inactive_reason is not None:
            return ClaudeResponse(
                success=False,
                content="",
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                latency_ms=0.0,
                error=inactive_reason,
            )

        spec, backend = self._spec, self._backend.for_request()
        options = self._resolve_options(max_tokens, temperature, model, reasoning_effort)
        response = await self._async_call_with_retry(
            spec=spec,
            backend=backend,
            system_prompt=system_prompt,
            user_message=user_message,
            **options,
        )

        # Update counters on success or failure
        if response.success:
            self._circuit_breaker.consecutive_failures = 0
            if self._circuit_breaker.state != _CB_CLOSED:
                _LOGGER.info("Circuit breaker reset to closed after successful request")
            self._circuit_breaker.state = _CB_CLOSED
            if spec.tracks_cost:
                self._budget.monthly_cost += response.estimated_cost
            if triggered_by == "auto":
                self._rate_counters.auto_requests_today += 1
            else:
                self._rate_counters.manual_requests_today += 1
        elif response.no_retry:
            self._error_count += 1  # reported as a failure, but outside the breaker
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
        requester_id: Any = USER_NOT_CHECKED,
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
            requester_id: As in ``async_request``.

        Yields:
            ``{"type": "text"|"thinking", "text": str}`` dicts from the Claude API stream.

        Raises:
            RuntimeError: When any pre-flight guard blocks the request.
            AIUserNotAllowedError: The requester may not use the current provider. Nothing was sent.

        """
        if requester_id is not USER_NOT_CHECKED and not self.check_user_allowed(requester_id):
            raise AIUserNotAllowedError

        self._reset_daily_counters_if_needed()

        if self._plan_restricted_trigger_refused(triggered_by):
            raise RuntimeError("This AI provider only runs requests started manually by its connected owner")

        if not self._check_circuit_breaker():
            raise RuntimeError("Circuit breaker open")

        if not self._check_rate_limit(triggered_by):
            raise RuntimeError("Rate limit exceeded")

        if not self._check_budget():
            raise RuntimeError("Monthly budget exceeded")

        inactive_reason = self._inactive_reason()
        if inactive_reason is not None:
            raise RuntimeError(inactive_reason)

        spec, backend = self._spec, self._backend.for_request()
        options = self._resolve_options(max_tokens, temperature, model, reasoning_effort)
        resolved_model = options["model"]

        start_time = time.monotonic()
        stop_event: dict[str, Any] = {}
        try:
            async for event in backend.async_stream(
                system_prompt=system_prompt,
                user_message=user_message,
                **options,
            ):
                if event.get("type") == "stop":
                    stop_event = event
                    continue
                yield event

        except Exception as exc:
            # Issue #572: capability-appropriate kwargs are built from
            # AI_MODEL_CAPABILITIES up front, so an APIError here means a genuinely
            # new/unexpected failure for a model this table claims to have verified —
            # not a known, retryable capability mismatch. Log it distinctly and
            # re-raise; no in-place retry.
            if isinstance(exc, AIBackendError):
                _LOGGER.warning(
                    "Unexpected API error for model '%s' (streaming): %s",
                    resolved_model,
                    exc,
                )

            self._error_count += 1
            if getattr(exc, "no_retry", False):
                raise  # reported as a failure, but outside the breaker
            self._circuit_breaker.consecutive_failures += 1
            if self._circuit_breaker.consecutive_failures >= AI_CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_breaker.state = _CB_OPEN
                self._circuit_breaker.opened_at = time.monotonic()
                _LOGGER.error(
                    "Circuit breaker opened after %d consecutive failures",
                    self._circuit_breaker.consecutive_failures,
                )
            raise

        latency_ms = (time.monotonic() - start_time) * 1000.0
        input_tokens: int = stop_event.get("input_tokens", 0)
        output_tokens: int = stop_event.get("output_tokens", 0)
        estimated_cost: float = stop_event.get("estimated_cost", 0.0)
        yield {
            "type": "stop",
            "stop_reason": stop_event.get("stop_reason"),
            "truncated_empty": stop_event.get("truncated_empty", False),
        }

        # Update counters on success
        self._circuit_breaker.consecutive_failures = 0
        if self._circuit_breaker.state != _CB_CLOSED:
            _LOGGER.info("Circuit breaker reset to closed after successful streaming request")
        self._circuit_breaker.state = _CB_CLOSED
        if spec.tracks_cost:
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
        """Validate the configured credentials via the provider backend.

        Args:
            hass: Optional HomeAssistant instance, forwarded so the backend can offload
                blocking client construction to the executor.

        Returns:
            (True, message) on success, or (False, error_message).

        """
        if self._backend is None:
            return False, f"Unknown AI provider '{self._provider_id}'"
        return await self._backend.async_test_connection(hass)

    def _account_manager(self) -> Any:
        """The shared account manager for an account-kind provider, else None."""
        if self._spec is None or self._spec.credential_kind != CREDENTIAL_ACCOUNT or self._backend is None:
            return None
        return getattr(self._backend, "client", None)

    def _plan_restricted_trigger_refused(self, triggered_by: str) -> bool:
        """Plan-restricted providers run manual requests only (never scheduled/automatic ones)."""
        return bool(self._spec is not None and self._spec.plan_restricted and triggered_by != "manual")

    def check_user_allowed(self, user_id: str | None) -> bool:
        """Whether this HA user may start a request. Driven by ``plan_restricted``, not the provider id.

        Plan-restricted providers allow only the connected account's owner. Others allow everyone.
        """
        if self._spec is None or not self._spec.plan_restricted:
            return True
        manager = self._account_manager()
        if manager is None or getattr(manager, "state", None) != "connected":
            return False
        owner = getattr(manager, "owner_user_id", None)
        return bool(user_id and owner and user_id == owner)

    def get_status(self) -> dict[str, Any]:
        """Return current client status metadata.

        The returned dict NEVER includes the API key.

        Returns:
            Dict with status summary suitable for sensor attributes or API responses.

        """
        self._reset_daily_counters_if_needed()

        account_state: str | None = None
        if self._spec is not None and self._spec.credential_kind == CREDENTIAL_ACCOUNT:
            manager = self._account_manager()
            account_state = manager.state if manager is not None else "disconnected"

        # Determine top-level status string
        if not self._config.get("ai_enabled", False):
            status = "disabled"
        elif self._spec is None:
            status = "error"  # unknown provider id: no AI, but never a setup failure
        elif self._stop_reason is not None:
            status = "inactive"
        elif account_state == "disconnected":
            status = "not_connected"
        elif account_state == "reauth_required":
            status = "reauth_required"
        elif self._circuit_breaker.state == _CB_OPEN:
            status = "circuit_open"
        elif not self._check_budget():
            status = "budget_exceeded"
        elif self._inactive_reason() is not None:
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
            "model": self.configured_model(),
            "circuit_breaker_state": self._circuit_breaker.state,
            "monthly_cost_estimate": round(self._budget.monthly_cost, 4),
            "auto_requests_today": self._rate_counters.auto_requests_today,
            "manual_requests_today": self._rate_counters.manual_requests_today,
            "investigator_requests_today": self._investigator_requests_today,
            "investigator_requests_limit": int(self._config.get(CONF_AI_INVESTIGATOR_RPD, DEFAULT_AI_INVESTIGATOR_RPD)),
            "investigator_enabled": self._config.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED),
            "provider": self._provider_id,
            "provider_label": self._spec.label if self._spec is not None else self._provider_id,
            "usage_url": self._spec.usage_url if self._spec is not None else None,
            "tracks_cost": self._tracks_cost,
            "plan_restricted": bool(self._spec is not None and self._spec.plan_restricted),
            "claude_wording": bool(self._spec is not None and self._spec.uses_claude_wording),
            "account_state": account_state,
            "stop_reason": self._stop_reason,
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

    def update_config(self, config: dict[str, Any], client: Any | None = None, *, backend: Any = None) -> None:
        """Apply updated config entry data.

        The backend recreates its transport when credentials change; a changed
        provider id rebuilds the backend. Lifts ``stop_dispatch``.

        Args:
            config: New config entry data dict.
            client: What a rebuilt backend is given, as in ``__init__`` (an account
                provider's shared manager). Unused when the provider id is unchanged.
            backend: A backend the caller already built off the event loop, which replaces
                the current one and is pointed at ``config``. Call this on the loop then:
                provider, spec and backend change together, with no dispatch able to see
                them half switched.

        """
        new_provider_id = config.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER)
        if backend is not None:
            backend.update_config(config)
        if backend is not None or new_provider_id != self._provider_id:
            new_spec = get_provider(new_provider_id)
            if backend is None and new_spec is not None:
                backend = new_spec.build_backend(config, client)
            self._provider_id, self._spec, self._backend = new_provider_id, new_spec, backend
            self._models_cache = None
        elif self._backend is not None:
            self._backend.update_config(config)
        self._config = config
        self._stop_reason = None

    def stop_dispatch(self, reason: str) -> None:
        """Refuse every new request with ``reason`` until ``update_config`` succeeds.

        For a zone whose saved settings could not be applied: the provider it would otherwise
        keep using is no longer the one the user chose. A request already running finishes.
        """
        self._stop_reason = reason

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @property
    def _api_name(self) -> str:
        """The name logs use for this zone's AI service."""
        return self._spec.api_name if self._spec is not None else "AI API"

    @property
    def _client(self) -> Any:
        """The backend's underlying SDK client (None when inactive or unknown provider)."""
        return self._backend.client if self._backend is not None else None

    @property
    def provider_id(self) -> str:
        """The provider id this client is on now."""
        return self._provider_id

    @property
    def _tracks_cost(self) -> bool:
        return self._spec is None or self._spec.tracks_cost

    def _inactive_reason(self) -> str | None:
        """Why no request can be made (dispatch stopped / unknown provider / backend not ready), else None."""
        if self._stop_reason is not None:
            return self._stop_reason
        if self._spec is None or self._backend is None:
            return f"Unknown AI provider '{self._provider_id}'"
        return self._backend.inactive_reason

    def configured_model(self) -> str:
        """The model a request would use right now, via the provider's resolver ("" = none chosen)."""
        if self._spec is None:
            return str(self._config.get(CONF_AI_MODEL, DEFAULT_AI_MODEL))
        return str(self._spec.resolve_request_options(self._config, {}).get("model") or "")

    def ready_model(self) -> str:
        """``configured_model()``, or "" while no request could be made (inactive or unknown provider)."""
        return "" if self._inactive_reason() is not None else self.configured_model()

    def _resolve_options(
        self,
        max_tokens: int | None,
        temperature: float | None,
        model: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        """Resolve per-request options through the provider spec (overrides over config)."""
        assert self._spec is not None  # guarded by _inactive_reason()
        # Every provider returns all four options (ProviderSpec.resolve_request_options).
        return self._spec.resolve_request_options(
            self._config,
            {
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )

    def _build_request_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        """Delegate to the backend's request builder (kept for callers/tests that read it here)."""
        return self._backend.build_request_kwargs(**kwargs)

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
        if not self._tracks_cost:
            return True

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

    async def _single_api_call(
        self,
        *,
        backend: Any,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        start_time: float,
    ) -> ClaudeResponse:
        """Make exactly one backend call; latency is measured from the retry loop start.

        Raises on failure — this method has no retry/fallback policy of its own;
        callers (_async_call_with_retry's backoff loop, and the tier-fallback single
        attempt) own that decision (Issue #563).
        """
        response = await backend.async_request(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        response.latency_ms = (time.monotonic() - start_time) * 1000.0
        return response

    async def _try_tier_fallback(
        self,
        *,
        backend: Any,
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

        # The client's cached list unless the zone has switched provider since this request started.
        live_models = await (self.async_list_models() if backend is self._backend else backend.async_list_models())
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
                backend=backend,
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
        spec: ProviderSpec,
        backend: Any,
        system_prompt: str,
        user_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
    ) -> ClaudeResponse:
        """Call the provider's backend with exponential-backoff retry.

        Args:
            spec: The provider spec the request runs on.
            backend: The backend taken for this request, kept through its retries.
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
                    backend=backend,
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

            except AIBackendError as exc:
                if exc.no_retry:
                    _LOGGER.warning("%s request not retried: %s", spec.api_name, exc)
                    return ClaudeResponse(
                        success=False,
                        content="",
                        input_tokens=0,
                        output_tokens=0,
                        estimated_cost=0.0,
                        latency_ms=(time.monotonic() - start_time) * 1000.0,
                        error=f"API error: {exc}",
                        no_retry=True,
                        no_answer=exc.no_answer,
                    )
                if exc.category == "rate_limit":
                    last_error = f"Rate limit error: {exc}"
                    _LOGGER.warning(
                        "%s rate limit on attempt %d/%d: %s",
                        spec.api_name,
                        attempt,
                        AI_MAX_RETRIES,
                        exc,
                    )
                elif exc.timeout:
                    last_error = f"Timeout error: {exc}"
                    _LOGGER.warning(
                        "%s timeout on attempt %d/%d: %s",
                        spec.api_name,
                        attempt,
                        AI_MAX_RETRIES,
                        exc,
                    )
                else:
                    # Issue #572: request shape is built from the verified
                    # AI_MODEL_CAPABILITIES table up front, so an API error here is not a
                    # known, retryable capability mismatch — except a deprecated/invalid
                    # model ID (404), which won't succeed on retry either and gets one
                    # same-tier replacement attempt instead (providers that support it).
                    if spec.supports_tier_fallback and exc.model_not_found:
                        fallback = await self._try_tier_fallback(
                            backend=backend,
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
                        "%s error on attempt %d/%d: %s",
                        spec.api_name,
                        attempt,
                        AI_MAX_RETRIES,
                        exc,
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = f"Unexpected error: {exc}"
                _LOGGER.warning(
                    "Unexpected error calling %s on attempt %d/%d: %s",
                    spec.api_name,
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
            "%s request failed after %d attempts; last error: %s",
            spec.api_name,
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
