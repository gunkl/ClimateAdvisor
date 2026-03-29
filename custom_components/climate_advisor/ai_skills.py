"""AI Skills framework for Climate Advisor."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

        # Call Claude
        response = await claude_client.async_request(
            system_prompt=skill.system_prompt,
            user_message=context,
            triggered_by=skill.triggered_by,
        )

        if response.success:
            try:
                parsed = skill.response_parser(response.content)
                return {
                    "success": True,
                    "source": "ai",
                    "data": parsed,
                    "error": None,
                    "input_context": context,
                    "raw_response": response.content,
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
            return _run_fallback(skill, coordinator, context=context, **kwargs)

        return _error_result(
            response.error or "AI request failed and no fallback available",
            input_context=context,
        )


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
