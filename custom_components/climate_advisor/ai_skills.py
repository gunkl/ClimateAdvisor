"""AI Skills framework for Climate Advisor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import claude_api as _claude_api  # noqa: F401  (registers the built-in "anthropic" provider)
from .ai_backend import USER_NOT_CHECKED, AIUserNotAllowedError, ProviderSpec, get_provider
from .const import CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .claude_api import ClaudeAPIClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class AISkillDefinition:
    """Definition of an AI-powered analysis skill."""

    name: str
    description: str
    system_prompt: str
    context_builder: Callable  # async (hass, coordinator, **kwargs) -> str
    response_parser: Callable  # (raw_response: str) -> dict[str, Any]
    fallback: Callable | None = None  # (coordinator, **kwargs) -> dict[str, Any]
    triggered_by: str = "manual"  # "manual" or "auto" — determines rate limit counter
    # Optional per-skill config overrides — config key names read from coordinator.config at call time
    config_key_model: str | None = None  # config key for model override (e.g. CONF_AI_INVESTIGATOR_MODEL)
    config_key_max_tokens: str | None = None  # config key for max_tokens override
    config_key_reasoning: str | None = None  # config key for reasoning_effort override


class AISkillRegistry:
    """Registry of AI-powered analysis skills."""

    def __init__(self) -> None:
        self._skills: dict[str, AISkillDefinition] = {}

    def register(self, skill: AISkillDefinition) -> None:
        """Register an AI skill."""
        if skill.name in self._skills:
            _LOGGER.warning("AI skill '%s' already registered, replacing", skill.name)
        self._skills[skill.name] = skill
        _LOGGER.debug("Registered AI skill: %s", skill.name)

    def get(self, name: str) -> AISkillDefinition | None:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[dict[str, str]]:
        """Return name + description for each registered skill."""
        return [{"name": s.name, "description": s.description} for s in self._skills.values()]

    async def async_execute(
        self,
        name: str,
        hass: HomeAssistant,
        coordinator: Any,  # ClimateAdvisorCoordinator (avoid circular import)
        claude_client: ClaudeAPIClient,
        *,
        requester_id: Any = USER_NOT_CHECKED,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a skill by name.

        Flow:
        1. Look up skill definition
        2. Build context via skill.context_builder(hass, coordinator, **kwargs)
        3. Call claude_client.async_request(skill.system_prompt, context)
        4. If failed and fallback exists, call fallback(coordinator, **kwargs)
        5. Parse response via skill.response_parser(raw_response)
        6. Return structured result dict with keys:
           - success: bool
           - source: "ai" or "fallback"
           - data: parsed result dict
           - error: str or None
           - input_context: the context string sent to Claude (for report persistence)
           - raw_response: the raw Claude response text (for report persistence)

        ``requester_id`` is handed to the client, which checks it at dispatch; a refusal
        raises ``AIUserNotAllowedError`` here, before any fallback.
        """
        skill = self._skills.get(name)
        if skill is None:
            _LOGGER.error("AI skill '%s' not found in registry", name)
            return {
                "success": False,
                "source": "error",
                "data": {},
                "error": f"Unknown skill: {name}",
                "input_context": "",
                "raw_response": "",
            }

        # Build context
        try:
            context = await skill.context_builder(hass, coordinator, **kwargs)
        except Exception:
            _LOGGER.exception("Failed to build context for skill '%s'", name)
            if skill.fallback:
                return _run_fallback(skill, coordinator, **kwargs)
            return _error_result(f"Context builder failed for {name}")

        # Resolve per-skill config overrides from coordinator.config (if configured)
        cfg: dict[str, Any] = getattr(coordinator, "config", {}) or {}
        spec, options = _resolve_skill_options(skill, cfg)
        override_model = options.get("model")
        override_max_tokens = options.get("max_tokens")
        override_reasoning = options.get("reasoning_effort")

        # Call Claude
        response = await claude_client.async_request(
            system_prompt=skill.system_prompt,
            user_message=context,
            triggered_by=skill.triggered_by,
            model=override_model,
            max_tokens=override_max_tokens,
            reasoning_effort=override_reasoning,
            requester_id=requester_id,
        )

        if response.success:
            # A substitution belongs to the provider it was made on: skip it if the zone has switched since.
            if (
                spec is not None
                and spec.supports_tier_fallback
                and cfg.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER) == spec.id
            ):
                await _maybe_persist_model_fallback(coordinator, override_model, cfg, response)
            try:
                parsed = skill.response_parser(response.content)
                return {
                    "success": True,
                    "source": "ai",
                    "data": parsed,
                    "error": None,
                    "input_context": context,
                    "raw_response": response.content,
                    "truncated": response.truncated,
                    "truncated_empty": response.truncated_empty,
                }
            except Exception:
                _LOGGER.exception("Failed to parse AI response for skill '%s'", name)
                # Fall through to fallback

        # AI failed — try fallback
        _LOGGER.warning(
            "AI request failed for skill '%s' — %s",
            name,
            response.error or "parse error",
        )
        if response.no_answer:  # the model was reached and declined: say so, no stand-in report
            return _error_result(response.error, input_context=context)
        if skill.fallback:
            return _run_fallback(skill, coordinator, context=context, **kwargs)

        return _error_result(
            response.error or "AI request failed and no fallback available",
            input_context=context,
        )

    async def async_execute_streaming(
        self,
        name: str,
        hass: HomeAssistant,
        coordinator: Any,
        claude_client: ClaudeAPIClient,
        *,
        requester_id: Any = USER_NOT_CHECKED,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a skill by name, streaming results as SSE-ready dicts.

        Yields dicts with ``type`` key:
        - ``{"type": "chunk", "text": str}`` — incremental text from the model
        - ``{"type": "error", "message": str}`` — terminal failure
        - ``{"type": "done", ...}`` — final structured result (same shape as async_execute)

        Args:
            name: Registered skill name.
            hass: Home Assistant instance.
            coordinator: ClimateAdvisorCoordinator.
            claude_client: Initialized ClaudeAPIClient.
            requester_id: As in ``async_execute``; a refusal is a terminal error event with ``status`` 403.
            **kwargs: Forwarded to the skill's context_builder.

        """
        skill = self._skills.get(name)
        if skill is None:
            _LOGGER.error("AI skill '%s' not found in registry", name)
            yield {"type": "error", "message": f"Unknown skill: {name}"}
            return

        # Run context builder concurrently, streaming status events as providers complete.
        status_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _push_status(msg: str) -> None:
            await status_queue.put(msg)

        context_task = asyncio.create_task(
            skill.context_builder(hass, coordinator, status_callback=_push_status, **kwargs)
        )
        yield {"type": "status", "text": "Gathering system data…"}
        while not context_task.done():
            try:
                msg = await asyncio.wait_for(status_queue.get(), timeout=1.0)
                yield {"type": "status", "text": msg}
            except TimeoutError:
                pass

        # Drain any remaining status messages before the Claude call.
        while not status_queue.empty():
            yield {"type": "status", "text": status_queue.get_nowait()}

        try:
            context = context_task.result()
        except Exception:
            _LOGGER.exception("Failed to build context for skill '%s'", name)
            yield {"type": "error", "message": "Context builder failed"}
            return

        cfg: dict[str, Any] = getattr(coordinator, "config", {}) or {}
        label_spec = get_provider(cfg.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER))
        provider_name = "the AI provider" if label_spec is None else label_spec.display_name
        yield {"type": "status", "text": f"Sending context to {provider_name}…"}

        _, options = _resolve_skill_options(skill, cfg)
        override_model = options.get("model")
        override_max_tokens = options.get("max_tokens")
        override_reasoning = options.get("reasoning_effort")

        full_text = ""
        stop_reason: str | None = None
        truncated_empty = False
        try:
            async for event in claude_client.async_request_streaming(
                system_prompt=skill.system_prompt,
                user_message=context,
                triggered_by=skill.triggered_by,
                model=override_model,
                max_tokens=override_max_tokens,
                reasoning_effort=override_reasoning,
                requester_id=requester_id,
            ):
                event_type = event.get("type")
                event_text = event.get("text", "")
                if event_type == "thinking":
                    yield {"type": "thinking", "text": event_text}
                elif event_type == "text":
                    full_text += event_text
                    yield {"type": "chunk", "text": event_text}
                elif event_type == "stop":
                    stop_reason = event.get("stop_reason")
                    truncated_empty = event.get("truncated_empty", False)
        except AIUserNotAllowedError as exc:
            yield {"type": "error", "message": str(exc), "status": 403}
            return
        except Exception as exc:
            _LOGGER.error("Streaming request failed for skill '%s': %s", name, exc)
            yield {"type": "error", "message": str(exc)}
            return

        try:
            parsed = skill.response_parser(full_text)
        except Exception:
            _LOGGER.exception("Failed to parse streaming response for skill '%s'", name)
            parsed = {}

        yield {
            "type": "done",
            "success": True,
            "source": "ai",
            "data": parsed,
            "error": None,
            "input_context": context,
            "raw_response": full_text,
            "truncated": stop_reason == "max_tokens",
            "truncated_empty": truncated_empty,
        }


def _resolve_skill_options(skill: AISkillDefinition, cfg: dict[str, Any]) -> tuple[ProviderSpec | None, dict[str, Any]]:
    """Resolve a skill's per-request options through the active provider spec.

    The skill's per-skill config keys supply the raw overrides; the provider decides
    which of them apply (e.g. a provider may ignore Claude model overrides). An unknown
    provider id yields the raw overrides and no spec (the client reports the error).
    """
    max_tokens_raw = cfg.get(skill.config_key_max_tokens) if skill.config_key_max_tokens else None
    overrides: dict[str, Any] = {
        "model": cfg.get(skill.config_key_model) if skill.config_key_model else None,
        "max_tokens": int(max_tokens_raw) if max_tokens_raw is not None else None,
        "reasoning_effort": cfg.get(skill.config_key_reasoning) if skill.config_key_reasoning else None,
    }
    spec = get_provider(cfg.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER))
    if spec is None:
        return None, overrides
    return spec, spec.resolve_request_options(cfg, overrides)


async def _maybe_persist_model_fallback(
    coordinator: Any,
    override_model: str | None,
    cfg: dict[str, Any],
    response: Any,
) -> None:
    """Persist a same-tier model substitution if claude_api.py made one (Issue #563).

    Only the non-streaming async_request()/_async_call_with_retry() path can produce
    a fallback (async_request_streaming() never retries — see its docstring), so this
    is only called from async_execute(), not async_execute_streaming(). Compares the
    model that was actually requested (mirroring claude_api.py's own resolution:
    override, else the provider's configured model, else its default) against
    response.resolved_model — they differ only when a fallback occurred.
    """
    spec = get_provider(cfg.get(CONF_AI_PROVIDER, DEFAULT_AI_PROVIDER))
    model_key = spec.model_setting
    default_model = spec.default_model
    requested_model = override_model or cfg.get(model_key, default_model)
    resolved_model = getattr(response, "resolved_model", "") or requested_model
    if resolved_model != requested_model:
        persist = getattr(coordinator, "async_persist_model_fallback", None)
        if persist is not None:
            await persist(resolved_model)


def _run_fallback(
    skill: AISkillDefinition,
    coordinator: Any,
    context: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a skill's fallback function."""
    try:
        data = skill.fallback(coordinator, **kwargs)
        _LOGGER.info("Using fallback for skill '%s'", skill.name)
        return {
            "success": True,
            "source": "fallback",
            "data": data,
            "error": None,
            "input_context": context,
            "raw_response": "",
        }
    except Exception:
        _LOGGER.exception("Fallback also failed for skill '%s'", skill.name)
        return _error_result(
            f"Both AI and fallback failed for {skill.name}",
            input_context=context,
        )


def _error_result(error: str, input_context: str = "") -> dict[str, Any]:
    """Build a standard error result dict."""
    return {
        "success": False,
        "source": "error",
        "data": {},
        "error": error,
        "input_context": input_context,
        "raw_response": "",
    }
