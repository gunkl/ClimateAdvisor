"""AI Skills framework for Climate Advisor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import CONF_AI_MODEL, DEFAULT_AI_MODEL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .claude_api import ClaudeAPIClient

_LOGGER = logging.getLogger(__name__)

# Issue #1336: every streaming status event carries a catalog key (frontend/i18n/ai/<lang>.json) beside its
# English fallback text, so the dashboard renders it in the viewer's language.
STATUS_KEY_GATHERING = "ai.status_gathering"
STATUS_KEY_SENDING = "ai.status_sending"
STATUS_KEY_PROVIDER = "ai.status_provider"
STATUS_TEXT_GATHERING = "Gathering system data…"
STATUS_TEXT_SENDING = "Sending the data to the AI provider…"
STATUS_TEXT_PROVIDER = "Waiting for the AI provider to write the report…"


def status_event(text: str, key: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """A ``{"type": "status", "text", "key"[, "params"]}`` SSE event (Issue #1336)."""
    event: dict[str, Any] = {"type": "status", "text": text, "key": key}
    if params:
        event["params"] = dict(params)
    return event


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
    # Issue #1341: the parser also needs the output language (it is called as parser(raw, language=<code>)).
    parser_takes_language: bool = False


def _parse_response(skill: AISkillDefinition, text: str, hass: HomeAssistant) -> Any:
    """Run the skill's parser, giving it the output language when it asks for it."""
    if skill.parser_takes_language:
        from . import i18n  # noqa: PLC0415

        return skill.response_parser(text, language=i18n.hass_language(hass))
    return skill.response_parser(text)


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
        override_model = cfg.get(skill.config_key_model) if skill.config_key_model else None
        override_max_tokens_raw = cfg.get(skill.config_key_max_tokens) if skill.config_key_max_tokens else None
        override_max_tokens = int(override_max_tokens_raw) if override_max_tokens_raw is not None else None
        override_reasoning = cfg.get(skill.config_key_reasoning) if skill.config_key_reasoning else None

        # Call Claude
        response = await claude_client.async_request(
            system_prompt=skill.system_prompt,
            user_message=context,
            triggered_by=skill.triggered_by,
            model=override_model,
            max_tokens=override_max_tokens,
            reasoning_effort=override_reasoning,
        )

        error_code = getattr(response, "error_code", None)
        if response.success:
            await _maybe_persist_model_fallback(coordinator, override_model, cfg, response)
            try:
                parsed = _parse_response(skill, response.content, hass)
                return {
                    "success": True,
                    "source": "ai",
                    "data": parsed,
                    "error": None,
                    "input_context": context,
                    "raw_response": response.content,
                    "truncated": response.truncated,
                    "truncated_empty": response.truncated_empty,
                    "error_code": None,
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
        if skill.fallback:
            result = _run_fallback(skill, coordinator, context=context, **kwargs)
            # Issue #1336: why the AI part failed (the occupant is told, e.g. "the provider's output limit")
            result["error_code"] = error_code
            return result

        result = _error_result(
            response.error or "AI request failed and no fallback available",
            input_context=context,
        )
        result["error_code"] = error_code
        return result

    async def async_execute_streaming(
        self,
        name: str,
        hass: HomeAssistant,
        coordinator: Any,
        claude_client: ClaudeAPIClient,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a skill by name, streaming results as SSE-ready dicts.

        Yields dicts with ``type`` key:
        - ``{"type": "chunk", "text": str}`` — incremental text from the model
        - ``{"type": "status", "text": str, "key": str[, "params": dict]}`` — progress (Issue #1336: ``key``)
        - ``{"type": "error", "message": str, "error_code": str | None}`` — terminal failure
        - ``{"type": "done", ...}`` — final structured result (same shape as async_execute)

        Args:
            name: Registered skill name.
            hass: Home Assistant instance.
            coordinator: ClimateAdvisorCoordinator.
            claude_client: Initialized ClaudeAPIClient.
            **kwargs: Forwarded to the skill's context_builder.

        """
        skill = self._skills.get(name)
        if skill is None:
            _LOGGER.error("AI skill '%s' not found in registry", name)
            yield {"type": "error", "message": f"Unknown skill: {name}", "error_code": None}
            return

        # Run context builder concurrently, streaming status events as providers complete.
        status_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def _push_status(msg: str) -> None:
            # Per-context-section progress ("Loading <section>…"): English detail text, gathering key.
            await status_queue.put(status_event(msg, STATUS_KEY_GATHERING))

        context_task = asyncio.create_task(
            skill.context_builder(hass, coordinator, status_callback=_push_status, **kwargs)
        )
        yield status_event(STATUS_TEXT_GATHERING, STATUS_KEY_GATHERING)
        while not context_task.done():
            try:
                status = await asyncio.wait_for(status_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            yield status

        # Drain any remaining status messages before the AI call.
        while not status_queue.empty():
            yield status_queue.get_nowait()

        try:
            context = context_task.result()
        except Exception:
            _LOGGER.exception("Failed to build context for skill '%s'", name)
            yield {"type": "error", "message": "Context builder failed", "error_code": None}
            return

        yield status_event(STATUS_TEXT_SENDING, STATUS_KEY_SENDING)
        yield status_event(STATUS_TEXT_PROVIDER, STATUS_KEY_PROVIDER)

        cfg: dict[str, Any] = getattr(coordinator, "config", {}) or {}
        override_model = cfg.get(skill.config_key_model) if skill.config_key_model else None
        override_max_tokens_raw = cfg.get(skill.config_key_max_tokens) if skill.config_key_max_tokens else None
        override_max_tokens = int(override_max_tokens_raw) if override_max_tokens_raw is not None else None
        override_reasoning = cfg.get(skill.config_key_reasoning) if skill.config_key_reasoning else None

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
        except Exception as exc:
            error_code = getattr(exc, "error_code", None)
            _LOGGER.error("Streaming request failed for skill '%s': error_code=%s %s", name, error_code, exc)
            yield {"type": "error", "message": str(exc), "error_code": error_code}
            return

        try:
            parsed = _parse_response(skill, full_text, hass)
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
            "error_code": None,
        }


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
    override, else the configured CONF_AI_MODEL, else DEFAULT_AI_MODEL) against
    response.resolved_model — they differ only when a fallback occurred.
    """
    requested_model = override_model or cfg.get(CONF_AI_MODEL, DEFAULT_AI_MODEL)
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
