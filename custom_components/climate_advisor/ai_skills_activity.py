"""Activity Report AI skill for Climate Advisor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .ai_skills import AISkillDefinition, AISkillRegistry
from .const import (
    ATTR_AUTOMATION_STATUS,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_TREND,
)

_LOGGER = logging.getLogger(__name__)

_SKILL_NAME = "activity_report"

_SYSTEM_PROMPT = """\
You are an HVAC automation diagnostic assistant for Climate Advisor, a Home Assistant integration.
Analyze the provided system state and sensor data.
Return your analysis with these exact section headers (use ## for headers):
## SUMMARY
2-3 sentence overview of the current situation.
## TIMELINE
Chronological summary of significant recent events and state changes.
## DECISIONS
Why each automation action was taken, with the logic explained.
## ANOMALIES
Anything unusual: long runtimes, frequent cycling, comfort violations, unexpected states.
## DIAGNOSTICS
System health observations: sensor connectivity, automation engine status, learning state.\
"""


async def async_build_activity_context(
    hass: HomeAssistant,
    coordinator: Any,
    **kwargs: Any,
) -> str:
    """Build context string for the activity report skill.

    Gathers current system state from coordinator and HA and formats it as a
    structured text block suitable for Claude analysis.
    """
    data: dict[str, Any] = coordinator.data or {}
    options: dict[str, Any] = coordinator.config or {}

    # --- Classification ---
    day_type = data.get(ATTR_DAY_TYPE, "unknown")
    trend = data.get(ATTR_TREND, "unknown")

    climate_entity_id: str = options.get("climate_entity", "")
    hvac_mode = "unknown"
    current_temp = "unknown"
    if climate_entity_id:
        climate_state = hass.states.get(climate_entity_id)
        if climate_state is not None:
            hvac_mode = climate_state.state
            current_temp = climate_state.attributes.get("current_temperature", "unknown")

    # --- Automation state ---
    automation_status = data.get(ATTR_AUTOMATION_STATUS, "unknown")
    last_action_time = data.get(ATTR_LAST_ACTION_TIME, "unknown")
    last_action_reason = data.get(ATTR_LAST_ACTION_REASON, "unknown")
    next_action = data.get(ATTR_NEXT_AUTOMATION_ACTION, "unknown")
    next_action_time = data.get(ATTR_NEXT_AUTOMATION_TIME, "unknown")

    # --- Occupancy ---
    occupancy_mode = data.get(ATTR_OCCUPANCY_MODE, "unknown")

    # --- Fan ---
    fan_status = data.get(ATTR_FAN_STATUS, "unknown")

    # --- Contact sensors ---
    contact_status = data.get(ATTR_CONTACT_STATUS, "unknown")

    # --- Learning suggestions ---
    raw_suggestions = data.get(ATTR_LEARNING_SUGGESTIONS, [])
    if isinstance(raw_suggestions, list):
        suggestion_count = len(raw_suggestions)
        suggestion_types = [s.get("suggestion_type", "unknown") for s in raw_suggestions if isinstance(s, dict)]
        if suggestion_types:
            suggestions_summary = f"{suggestion_count} pending ({', '.join(suggestion_types)})"
        else:
            suggestions_summary = f"{suggestion_count} pending"
    else:
        suggestions_summary = "unavailable"

    # --- Config: comfort/setback temps and schedule ---
    comfort_heat = options.get("comfort_heat", "unknown")
    comfort_cool = options.get("comfort_cool", "unknown")
    setback_heat = options.get("setback_heat", "unknown")
    setback_cool = options.get("setback_cool", "unknown")
    wake_time = options.get("wake_time", "unknown")
    sleep_time = options.get("sleep_time", "unknown")
    briefing_time = options.get("briefing_time", "unknown")

    # --- Active features ---
    learning_enabled = options.get("learning_enabled", False)
    adaptive_preheat = options.get("adaptive_preheat_enabled", False)
    adaptive_setback = options.get("adaptive_setback_enabled", False)
    weather_bias = options.get("weather_bias_enabled", False)
    fan_mode = options.get("fan_mode", "disabled")

    # --- Format context block ---
    lines = [
        "=== Climate Advisor Activity Report Context ===",
        "",
        "## CLASSIFICATION",
        f"  Day type:          {day_type}",
        f"  Trend direction:   {trend}",
        f"  HVAC mode:         {hvac_mode}",
        f"  Indoor temp:       {current_temp}",
        "",
        "## AUTOMATION STATE",
        f"  Status:            {automation_status}",
        f"  Last action time:  {last_action_time}",
        f"  Last action reason:{last_action_reason}",
        f"  Next action:       {next_action}",
        f"  Next action time:  {next_action_time}",
        "",
        "## OCCUPANCY",
        f"  Mode:              {occupancy_mode}",
        "",
        "## FAN",
        f"  Status:            {fan_status}",
        f"  Mode configured:   {fan_mode}",
        "",
        "## CONTACT SENSORS",
        f"  Status:            {contact_status}",
        "",
        "## LEARNING",
        f"  Enabled:           {learning_enabled}",
        f"  Suggestions:       {suggestions_summary}",
        "",
        "## CONFIGURATION",
        f"  Comfort heat:      {comfort_heat}",
        f"  Comfort cool:      {comfort_cool}",
        f"  Setback heat:      {setback_heat}",
        f"  Setback cool:      {setback_cool}",
        f"  Wake time:         {wake_time}",
        f"  Sleep time:        {sleep_time}",
        f"  Briefing time:     {briefing_time}",
        "",
        "## ACTIVE FEATURES",
        f"  Adaptive preheat:  {adaptive_preheat}",
        f"  Adaptive setback:  {adaptive_setback}",
        f"  Weather bias:      {weather_bias}",
    ]

    return "\n".join(lines)


def parse_activity_response(raw_response: str) -> dict[str, Any]:
    """Parse a Claude activity report response into section dict.

    Splits on ## SECTION_NAME headers. Missing sections default to empty string.
    Handles malformed or partial responses without raising.
    """
    sections: dict[str, str] = {
        "summary": "",
        "timeline": "",
        "decisions": "",
        "anomalies": "",
        "diagnostics": "",
    }

    _header_map = {
        "SUMMARY": "summary",
        "TIMELINE": "timeline",
        "DECISIONS": "decisions",
        "ANOMALIES": "anomalies",
        "DIAGNOSTICS": "diagnostics",
    }

    if not raw_response:
        return sections

    current_key: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_key is not None:
            sections[current_key] = "\n".join(current_lines).strip()

    for line in raw_response.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            _flush()
            current_lines = []
            header_name = stripped[3:].strip().upper()
            current_key = _header_map.get(header_name)
            # Unrecognised header — discard content until next known header
            if current_key is None:
                _LOGGER.debug(
                    "Activity response parser: unknown header '%s', skipping",
                    stripped,
                )
        else:
            if current_key is not None:
                current_lines.append(line)

    _flush()

    return sections


def activity_fallback(coordinator: Any, **kwargs: Any) -> dict[str, Any]:
    """Return a simplified activity dict from coordinator data when AI is unavailable.

    Keys match the parse_activity_response output format so callers can treat both
    sources uniformly.
    """
    data: dict[str, Any] = coordinator.data or {}

    automation_status = data.get(ATTR_AUTOMATION_STATUS, "unknown")
    last_action_time = data.get(ATTR_LAST_ACTION_TIME, "unknown")
    last_action_reason = data.get(ATTR_LAST_ACTION_REASON, "unknown")
    next_action = data.get(ATTR_NEXT_AUTOMATION_ACTION, "unknown")
    next_action_time = data.get(ATTR_NEXT_AUTOMATION_TIME, "unknown")
    occupancy_mode = data.get(ATTR_OCCUPANCY_MODE, "unknown")
    day_type = data.get(ATTR_DAY_TYPE, "unknown")
    trend = data.get(ATTR_TREND, "unknown")
    contact_status = data.get(ATTR_CONTACT_STATUS, "unknown")
    fan_status = data.get(ATTR_FAN_STATUS, "unknown")

    summary = f"Automation is {automation_status}. Occupancy: {occupancy_mode}. Day type: {day_type} ({trend} trend)."

    timeline_parts = []
    if last_action_time and last_action_time != "unknown":
        timeline_parts.append(f"{last_action_time} — {last_action_reason or 'action taken'}")
    if next_action and next_action != "unknown":
        timeline_parts.append(f"Next: {next_action} at {next_action_time or 'unscheduled'}")
    timeline = "\n".join(timeline_parts) if timeline_parts else "No recent events recorded."

    decisions = (
        f"Last action reason: {last_action_reason}"
        if last_action_reason and last_action_reason != "unknown"
        else "No automation decisions recorded."
    )

    anomalies_parts = []
    if contact_status and contact_status not in ("unknown", "all_closed", "closed"):
        anomalies_parts.append(f"Contact sensor state: {contact_status}")
    anomalies = "\n".join(anomalies_parts) if anomalies_parts else "No anomalies detected."

    diagnostics_parts = [
        f"Automation status: {automation_status}",
        f"Fan status: {fan_status}",
        f"Contact status: {contact_status}",
    ]
    diagnostics = "\n".join(diagnostics_parts)

    return {
        "summary": summary,
        "timeline": timeline,
        "decisions": decisions,
        "anomalies": anomalies,
        "diagnostics": diagnostics,
    }


def register_activity_skill(registry: AISkillRegistry) -> None:
    """Create and register the activity report skill with the given registry."""
    skill = AISkillDefinition(
        name=_SKILL_NAME,
        description=(
            "Analyzes current HVAC activity, automation decisions, and system health."
            " Returns a structured report with summary, timeline, decisions, anomalies,"
            " and diagnostics sections."
        ),
        system_prompt=_SYSTEM_PROMPT,
        context_builder=async_build_activity_context,
        response_parser=parse_activity_response,
        fallback=activity_fallback,
        triggered_by="manual",
    )
    registry.register(skill)
