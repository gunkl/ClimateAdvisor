"""Activity Report AI skill for Climate Advisor."""

# ruff: noqa: E501  # _SYSTEM_PROMPT contains intentionally long AI instruction lines that cannot be wrapped

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .ai_skills import AISkillDefinition, AISkillRegistry
from .const import (
    ATTR_AUTOMATION_STATUS,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_TREND,
    THERMAL_SWING_DEFAULT_F,
)

_LOGGER = logging.getLogger(__name__)

_SKILL_NAME = "activity_report"

_SYSTEM_PROMPT = """You are an HVAC automation diagnostic assistant for Climate Advisor, a Home Assistant integration.
Analyze the provided system state and sensor data.
Return your analysis with these exact section headers (use ## for headers):
## SUMMARY
2-3 sentence overview of the current situation.
## TIMELINE
Output a markdown table with four columns: Time | Event | Settings | Source

Column definitions:
- **Time**: HH:MM (24-hour format)
- **Event**: One-line description (max 80 chars). Compress consecutive same-type automation events into one row with count and time range, e.g. "Warm-day setback applied Ã—10" with time range in the Event column. Do NOT use sub-bullets or nested lists.
- **Settings**: Thermostat settings changed by this event. Use the following rules:
  - If event data has `old_hvac_mode` and `new_hvac_mode` fields that differ â†’ show "mode: Xâ†’Y"
  - If event data also has `new_setpoint_f` or `old_setpoint` â†’ append ", setpoint: Aâ†’BÂ°F" (round to 1 decimal if needed)
  - For `override_detected` events â†’ use `old_mode`â†’`new_mode` from event data for the mode change
  - warm_day_state_confirmed: heartbeat — thermostat already in correct warm-day state, no change. Leave Settings blank.
  - warm_day_setback_applied: actual setpoint or mode change was made (cool→setback_cool, heat→setback_heat, or hard off). If old_setpoint_f/new_setpoint_f present → "setpoint: A→B°F".
  - sensor_opened: if hvac_mode_change present â†’ render it; if fan_mode_change present â†’ append it. Example: "mode: coolâ†’off, fan: autoâ†’on"
  - nat_vent_comfort_floor_exit / nat_vent_predicted_floor_exit: if fan_mode_change present â†’ "fan: onâ†’auto"; if hvac_mode_restored present â†’ prepend "mode: offâ†’X, "
  - grace_started: use trigger field in the Event description, NOT in Settings. Settings column stays blank for grace_started.
  - override_cleared: if old_setpoint_f present → show "was X°F (manual setpoint)" in Settings.
  - Leave Settings blank (empty cell) if no settings fields are present in the event data
  - Events that do not change thermostat settings (grace_started, sensor_opened, sensor_all_closed, nat_vent_outdoor_rise_exit, etc.) -> empty Settings cell
  - nat_vent_ceiling_escalation: nat-vent escalated to HVAC cooling because indoor exceeded comfort_cool. Settings: mode: off->cool
- **Source**: Exactly one of: `automation`, `manual`, `sensor`, or `system`

Special event types:
- system_restarted: HA restart boundary marker. Events ABOVE are from the prior session (recovered_events field = count of pre-restart events preserved). Leave Settings blank.

Source mapping rules:
- Events with source_label=automation in the event log â†’ `automation`
- Events with source_label=manual â†’ `manual`
- override_detected, override_confirmed, manual_override records â†’ `manual`
- nat_vent_* events, ceiling_guard_fired, classification_applied, grace_started{source=automation} â†’ `automation`
- sensor_opened, sensor_all_closed (hardware events) â†’ `sensor`
- Events without source_label and not matching above â†’ `sensor`

Grace period triggers: grace_started events do not always follow an override_detected event. Five trigger paths exist (shown in the trigger field): override_confirmed, fan_manual_override, sensor_closed_resume, nat_vent_exit_resume, dashboard_resume. Show the trigger value in the Event description to explain the context.

Warm-day setback grouping rule:
- warm_day_state_confirmed: heartbeat — thermostat already in correct warm-day state, no change.
  Collapse ALL consecutive warm_day_state_confirmed into one row: "Warm-day state confirmed ×N (HH:MM–HH:MM)".
  Leave Settings blank.
- warm_day_setback_applied: actual setpoint or mode change was made (cool→setback_cool, heat→setback_heat, or hard off).
  Do NOT collapse. If old_setpoint_f/new_setpoint_f present → show "setpoint: A→B°F" in Settings.

Example output:
| Time | Event | Settings | Source |
|---|---|---|---|
| 05:32â€“10:02 | Warm-day setback applied Ã—10 | mode: heatâ†’off | automation |
| 06:13 | Setpoint raised 79Â°F â†’ 80Â°F (+1Â°F) | setpoint: 79.0â†’80.0Â°F | manual |
| 11:02 | Natural ventilation exit (outdoor 69Â°F = indoor 69Â°F) | | automation |

If a HISTORICAL DAILY SUMMARIES section is present in the context:
- Produce a two-part Timeline:
  Part 1 â€” Per-day summary table with columns: Date | Day Type | HVAC Runtime | Overrides | Notes
            Use one row per day from HISTORICAL DAILY SUMMARIES.
  Part 2 â€” The standard per-event table (Time | Event | Settings | Source) for the period
            covered by the EVENT LOG (most recent ~2 days).
- In SUMMARY: describe trends across the full period, not just the current state.
- In ANOMALIES: flag patterns that repeat across multiple days (e.g. daily overrides, recurring violations).
## DECISIONS
Why each automation action was taken, with the logic explained.
When fan_status is active while hvac_mode is off, explicitly trace the fan state to the logged automation action that caused it (e.g., “Fan activated — natural ventilation: outdoor X°F ≤ threshold”). Do not describe the state in isolation.
- grace_started with trigger=dashboard_resume: the user manually resumed automation from the dashboard; the grace period prevents door/window sensors from immediately re-pausing. In DECISIONS, describe this as expected behavior: “90-min buffer against sensor re-trigger after user resume.”
- When override_cleared and grace_started appear at the same timestamp: the user both cancelled an active override AND resumed from pause — show these as one coordinated action in DECISIONS.
## ANOMALIES
Anything unusual: long runtimes, frequent cycling, comfort violations, unexpected states.
IMPORTANT: The STATE CROSS-VALIDATION section in the context contains pre-computed flags. If it contains [WARNING] or [FLAG] entries, call each one out explicitly here â€” do NOT construct explanatory narratives around contradictions. Treat them as data quality issues or potential hardware bugs requiring investigation.
NUMERIC VERIFICATION RULE: A temperature T is within comfort band [L, H] only if L <= T <= H. Verify the arithmetic directly against supplied numeric values before making any comfort characterization statement. The cross-validation section already contains this check â€” reference it rather than re-deriving.
## DIAGNOSTICS
System health observations: sensor connectivity, automation engine status, learning state.

SECTION ROLES ARE EXCLUSIVE:
- SUMMARY: current state only. No analysis, no decisions, no explanations.
- TIMELINE: chronological events only. No "why" analysis.
- DECISIONS: explain WHY each automation action was taken. Do NOT re-describe what Timeline already covered.
- ANOMALIES: items that deviate from expected behavior ONLY. Do NOT re-explain decisions already in Decisions. Reference [FLAG] items briefly â€” do not construct a full explanatory narrative.
- DIAGNOSTICS: subsystem health only. Do NOT repeat anomalies or decisions.

DEDUPLICATION RULE: Do not repeat any fact or analysis already covered in a prior section. A one-line cross-reference ("see Decisions") is acceptable; re-stating the same analysis verbatim is not."""


def _fmt_hours(h: float) -> str:
    """Format a float hours value as a human-readable string."""
    if h < 24:
        return f"{int(h)}h"
    days = h / 24
    return f"{int(days)}d" if days == int(days) else f"{days:.1f}d"


def _build_daily_summaries(coordinator: Any, hours: float) -> list[str]:
    """Return context lines for historical daily records when hours > 36."""
    try:
        days_back = max(1, int(hours / 24))
        today_str = datetime.date.today().isoformat()
        cutoff_date = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        records: list[dict] = (
            getattr(coordinator, "learning", None)
            and getattr(coordinator.learning, "_state", None)
            and getattr(coordinator.learning._state, "records", [])
            or []
        )
        past = [
            r
            for r in records
            if isinstance(r, dict) and r.get("date", "") > cutoff_date and r.get("date", "") < today_str
        ]
        if not past:
            return ["", "## HISTORICAL DAILY SUMMARIES", "  (no past records available)"]

        header = f"## HISTORICAL DAILY SUMMARIES (last {days_back} days, excluding today)"
        col_hdr = "  Date       | DayType | HVAC(min) | Overrides | Viol(min) | AvgIndoor | ObsHigh/Low"
        sep = "  -----------|---------|-----------|-----------|-----------|-----------|------------"
        rows = []
        for r in sorted(past, key=lambda x: x.get("date", "")):
            date = r.get("date", "?")
            day_type = str(r.get("day_type", "?"))[:7]
            hvac_min = int(r.get("hvac_runtime_minutes", 0) or 0)
            overrides = int(r.get("manual_overrides", 0) or 0)
            viol_min = int(r.get("comfort_violations_minutes", 0) or 0)
            avg_in = r.get("avg_indoor_temp")
            avg_in_str = f"{avg_in:.1f}Ã‚Â°F" if isinstance(avg_in, (int, float)) else "n/a"
            obs_high = r.get("observed_high_f")
            obs_low = r.get("observed_low_f")
            hl_str = (
                f"{obs_high:.0f}Ã‚Â°F/{obs_low:.0f}Ã‚Â°F"
                if isinstance(obs_high, (int, float)) and isinstance(obs_low, (int, float))
                else "n/a"
            )
            row = (
                f"  {date} | {day_type:<7} | {hvac_min:<9} | {overrides:<9}"
                f" | {viol_min:<9} | {avg_in_str:<9} | {hl_str}"
            )
            rows.append(row)

        note = "  Note: event log ring buffer covers ~50-60h; use daily summaries for context beyond that."
        return ["", header, col_hdr, sep, *rows, note]
    except Exception:
        _LOGGER.warning("activity_report: failed to build daily summaries Ã¢â‚¬â€ skipping")
        return []


def _format_engine_status_for_ai(engine_status: dict) -> str:
    """Format get_engine_status() output as a plain-text table for AI context.

    Returns a multi-line string ready to embed in an AI context block.
    Each engine appears on one line with activation date, value, confidence,
    and obs count.  Inactive engines show "(not yet active)".
    The ODE version and physics_eligible flag appear on the last line.
    """
    lines: list[str] = []

    def _engine_line(key: str, label: str, unit: str = "") -> str:
        info = engine_status.get(key, {})
        if not isinstance(info, dict) or not info.get("active"):
            return f"  {label}: (not yet active)"
        value = info.get("value")
        conf = info.get("confidence", "")
        obs = info.get("obs_count", "")
        since = info.get("since", "")
        val_str = f"{value:.4f}{unit}" if isinstance(value, float) else str(value)
        parts = [val_str]
        if conf:
            parts.append(conf)
        if obs:
            parts.append(f"{obs} obs")
        if since:
            parts.append(f"since {since}")
        detail = ", ".join(str(p) for p in parts)
        return f"  {label}: ({detail}) [ACTIVE]"

    lines.append(_engine_line("k_passive", "k_passive", " hrÃ¢ÂÂ»Ã‚Â¹"))
    lines.append(_engine_line("k_solar", "k_solar", " Ã‚Â°F/hr"))
    lines.append(_engine_line("solar_phase_offset_h", "solar_phase_offset_h", "h"))
    lines.append(_engine_line("k_vent_window", "k_vent_window", " hrÃ¢ÂÂ»Ã‚Â¹"))

    # k_active_hvac has a different shape Ã¢â‚¬â€ values nested under "value": {"heat": ..., "cool": ...}
    hvac_info = engine_status.get("k_active_hvac", {})
    if isinstance(hvac_info, dict) and hvac_info.get("active"):
        _hvac_value = hvac_info.get("value") or {}
        heat = _hvac_value.get("heat")
        cool = _hvac_value.get("cool")
        since = hvac_info.get("since", "")
        heat_str = f"{heat:.4f}" if isinstance(heat, float) else str(heat)
        cool_str = f"{cool:.4f}" if isinstance(cool, float) else str(cool)
        since_str = f", since {since}" if since else ""
        lines.append(f"  k_active_hvac: heat={heat_str} cool={cool_str} Ã‚Â°F/hr{since_str} [ACTIVE]")
    else:
        lines.append("  k_active_hvac: (not yet active)")

    ode_ver = engine_status.get("ode_version", "unknown")
    eligible = "YES" if engine_status.get("physics_eligible") else "NO"
    eligible_reason = engine_status.get("physics_eligible_reason", "")
    reason_str = f" ({eligible_reason})" if eligible_reason else ""
    lines.append(f"  ODE: {ode_ver}, eligible: {eligible}{reason_str}")

    return "\n".join(lines)


_AUTO_EVENT_TYPES = frozenset(
    {
        "ceiling_guard_fired",
        "classification_applied",
        "warm_day_state_confirmed",
        "warm_day_setback_applied",
        "warm_day_comfort_gap",
        "nat_vent_ceiling_escalation",
    }
)

_MANUAL_EVENT_TYPES = frozenset(
    {
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "override_self_resolved",
    }
)

_UNKNOWN_EVENT_TYPES = frozenset(
    {
        "sensor_opened",
        "sensor_all_closed",
    }
)

_SYSTEM_EVENT_TYPES: frozenset[str] = frozenset({"system_restarted"})


def _event_source_label(event_type: str, data: dict) -> str | None:
    """Return source label for an event, or None if unknown/default.

    Returns one of 'automation', 'manual', 'system', or None (caller treats None as unknown).
    """
    if event_type in _SYSTEM_EVENT_TYPES:
        return "system"

    # Explicit source field takes precedence
    source = data.get("source")
    if source in ("automation", "manual"):
        return source

    # nat_vent_* prefix Ã¢â€ â€™ automation
    if event_type.startswith("nat_vent_"):
        return "automation"

    # grace_started / grace_expired with source field
    if event_type in ("grace_started", "grace_expired"):
        if source in ("automation", "manual"):
            return source
        return None

    if event_type in _AUTO_EVENT_TYPES:
        return "automation"

    if event_type in _MANUAL_EVENT_TYPES:
        return "manual"

    if event_type in _UNKNOWN_EVENT_TYPES:
        return "sensor"  # physical HA sensor state change (door/window open/close)

    return None


async def async_build_activity_context(
    hass: HomeAssistant,
    coordinator: Any,
    **kwargs: Any,
) -> str:
    """Build context string for the activity report skill.

    Gathers current system state from coordinator and HA and formats it as a
    structured text block suitable for Claude analysis.
    """
    hours: float = float(kwargs.get("hours", 24))
    hours = max(1.0, min(hours, 168.0))  # clamp to frontend range 1Ã¢â‚¬â€œ168h

    data: dict[str, Any] = coordinator.data or {}
    options: dict[str, Any] = coordinator.config or {}

    # --- Classification ---
    day_type = data.get(ATTR_DAY_TYPE, "unknown")
    trend = data.get(ATTR_TREND, "unknown")
    hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
    # Compute fresh runtime Ã¢â‚¬â€ coordinator.data may be up to 30 min stale
    _base_runtime = coordinator._today_record.hvac_runtime_minutes if coordinator._today_record is not None else 0.0
    _session_elapsed = (
        (dt_util.now() - coordinator._hvac_on_since).total_seconds() / 60.0
        if coordinator._hvac_on_since is not None
        else 0.0
    )
    hvac_runtime_today = round(_base_runtime + _session_elapsed, 1)

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

    # --- Prediction engines ---
    engine_status_block = ""
    if hasattr(coordinator, "learning") and hasattr(coordinator.learning, "get_engine_status"):
        try:
            engine_status_block = _format_engine_status_for_ai(coordinator.learning.get_engine_status())
        except Exception:
            engine_status_block = "  (unavailable)"

    # --- Active features ---
    learning_enabled = options.get("learning_enabled", False)
    adaptive_preheat = options.get("adaptive_preheat_enabled", False)
    adaptive_setback = options.get("adaptive_setback_enabled", False)
    weather_bias = options.get("weather_bias_enabled", False)
    fan_mode = options.get("fan_mode", "disabled")

    # --- State cross-validation ---
    state_flags: list[str] = []
    active_actions = {"heating", "cooling", "fan"}
    if hvac_mode == "off" and str(hvac_action).lower() in active_actions:
        # Suppress if CA intentionally has the fan running (e.g., natural ventilation).
        # hvac_mode=off + hvac_action=fan is expected when CA activated fan_mode=on.
        # Only warn when the thermostat reports activity CA cannot account for.
        ca_fan_running = fan_status in ("active", "running (manual override)", "running (untracked)")
        if str(hvac_action).lower() == "fan" and ca_fan_running:
            pass  # Expected: CA activated HVAC fan-only mode for natural ventilation
        else:
            state_flags.append(
                f"[WARNING] hvac_mode=off but hvac_action={hvac_action!r} Ã¢â‚¬â€ "
                "possible stale coordinator data or thermostat reporting bug"
            )
    # Acquire thermostat swing/deadband Ã¢â‚¬â€ suppress flags for within-swing shortfalls.
    _swing_heat_f = THERMAL_SWING_DEFAULT_F
    _swing_cool_f = THERMAL_SWING_DEFAULT_F
    _temp_unit = options.get("temp_unit", "fahrenheit")
    if hasattr(coordinator, "learning") and callable(getattr(coordinator.learning, "get_thermal_model", None)):
        try:
            _thermal = coordinator.learning.get_thermal_model()
            _swing_heat_f = _thermal.get("swing_heat_f_display", THERMAL_SWING_DEFAULT_F)
            _swing_cool_f = _thermal.get("swing_cool_f_display", THERMAL_SWING_DEFAULT_F)
            if _temp_unit == "celsius":
                _swing_heat_f *= 5.0 / 9.0
                _swing_cool_f *= 5.0 / 9.0
        except Exception:
            pass
    try:
        ch = float(comfort_heat)
        cc = float(comfort_cool)
        ct = float(current_temp)
        if (ch - ct) > _swing_heat_f:
            state_flags.append(
                f"[FLAG] Indoor {ct}Ã‚Â°F < comfort_heat {ch}Ã‚Â°F Ã¢â‚¬â€ "
                f"below by {ch - ct:.1f}Ã‚Â°F (deadband: {_swing_heat_f:.1f}Ã‚Â°F)"
            )
        elif (ct - cc) > _swing_cool_f:
            state_flags.append(
                f"[FLAG] Indoor {ct}Ã‚Â°F > comfort_cool {cc}Ã‚Â°F Ã¢â‚¬â€ "
                f"above by {ct - cc:.1f}Ã‚Â°F (deadband: {_swing_cool_f:.1f}Ã‚Â°F)"
            )
        else:
            state_flags.append(f"[OK] Indoor {ct}Ã‚Â°F is within comfort band [{ch}Ã¢â‚¬â€œ{cc}Ã‚Â°F]")
    except (ValueError, TypeError):
        pass

    # --- Manual overrides today ---
    override_detail_lines: list[str] = []
    try:
        today_record = getattr(coordinator, "_today_record", None)
        override_count = 0
        override_details: list[dict] = []
        if today_record is not None:
            override_count = getattr(today_record, "manual_overrides", 0)
            override_details = list(getattr(today_record, "override_details", []) or [])

        override_detail_lines.append(f"  Count:             {override_count}")
        if override_details:
            for i, d in enumerate(override_details, 1):
                t = d.get("time", "??:??")
                old_t = d.get("old_temp", "?")
                new_t = d.get("new_temp", "?")
                direction = d.get("direction", "?")
                magnitude = d.get("magnitude", "?")
                sign = "+" if direction == "up" else "-"
                override_detail_lines.append(
                    f"  #{i}  {t}  {old_t}Ã‚Â°F Ã¢â€ â€™ {new_t}Ã‚Â°F  ({sign}{magnitude}Ã‚Â°F, {direction})"
                )
        else:
            override_detail_lines.append("  (no setpoint overrides recorded today)")

        ae = getattr(coordinator, "automation_engine", None)
        if ae is not None and getattr(ae, "_manual_override_active", False):
            override_time_str = getattr(ae, "_manual_override_time", None)
            if override_time_str:
                try:
                    override_dt = datetime.datetime.fromisoformat(str(override_time_str))
                    now_local = dt_util.now()
                    duration_seconds = (now_local - override_dt).total_seconds()
                    duration_min = max(0, round(duration_seconds / 60))
                    local_start = dt_util.as_local(override_dt) if override_dt.tzinfo else override_dt
                    override_detail_lines.append(
                        f"  Current override:  active since {local_start.strftime('%H:%M')}, "
                        f"duration {duration_min} min (ongoing)"
                    )
                except Exception:
                    override_detail_lines.append("  Current override:  active (duration unknown)")
            else:
                override_detail_lines.append("  Current override:  active (start time unknown)")
        else:
            override_detail_lines.append("  Current override:  none active")
    except Exception:
        _LOGGER.warning("activity_report: failed to build override detail section Ã¢â‚¬â€ skipping")
        override_detail_lines = ["  (unavailable)"]

    # --- Format context block ---
    lines = [
        "=== Climate Advisor Activity Report Context ===",
        "",
        "## STATE CROSS-VALIDATION",
        *(state_flags if state_flags else ["  No contradictions detected."]),
        "",
        "## CLASSIFICATION",
        f"  Day type:          {day_type}",
        f"  Trend direction:   {trend}",
        f"  HVAC mode:         {hvac_mode}",
        f"  HVAC action:       {hvac_action}",
        f"  HVAC runtime today:{hvac_runtime_today} min",
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
        "",
        "## ACTIVE PREDICTION ENGINES",
        *(engine_status_block.splitlines() if engine_status_block else ["  (unavailable)"]),
        "",
        "## MANUAL OVERRIDES TODAY",
        *override_detail_lines,
    ]

    # --- Event log (hours-based window, one line per event with source_label) ---
    try:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        raw_event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        event_lines: list[str] = []

        for entry in raw_event_log[-200:]:
            if not isinstance(entry, dict):
                continue
            raw_time = entry.get("time")
            # Filter by cutoff when a parseable timestamp is present
            if raw_time is not None:
                if isinstance(raw_time, datetime.datetime):
                    event_dt = raw_time
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=datetime.UTC)
                else:
                    try:
                        event_dt = datetime.datetime.fromisoformat(str(raw_time))
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.replace(tzinfo=datetime.UTC)
                    except ValueError:
                        event_dt = None
                if event_dt is not None and event_dt < cutoff:
                    continue

            # Format: "HH:MM Ã¢â‚¬â€ event_type: key=value ... [source_label=X]"
            if isinstance(raw_time, datetime.datetime):
                time_str = raw_time.strftime("%H:%M")
            elif raw_time is not None:
                try:
                    _dt = datetime.datetime.fromisoformat(str(raw_time))
                    time_str = _dt.strftime("%H:%M")
                except ValueError:
                    time_str = str(raw_time)
            else:
                time_str = "??:??"

            event_type = str(entry.get("type", "unknown"))
            data_fields = {k: v for k, v in entry.items() if k not in ("time", "type")}
            fields_str = " ".join(f"{k}={v}" for k, v in data_fields.items())

            label = _event_source_label(event_type, data_fields)
            label_str = f" source_label={label}" if label is not None else ""

            line = f"  {time_str} Ã¢â‚¬â€ {event_type}: {fields_str}{label_str}".rstrip(": ")
            event_lines.append(line)

        lines += [
            "",
            f"## EVENT LOG (last {_fmt_hours(hours)}, {len(event_lines)} events)",
            *(event_lines if event_lines else [f"  (no events in last {_fmt_hours(hours)})"]),
        ]
    except Exception:
        _LOGGER.warning("activity_report: failed to read event log Ã¢â‚¬â€ skipping")
        lines += ["", "## EVENT LOG", "  (unavailable)"]

    if hours > 36:
        lines += _build_daily_summaries(coordinator, hours)

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
            # Unrecognised header Ã¢â‚¬â€ discard content until next known header
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
        timeline_parts.append(f"{last_action_time} Ã¢â‚¬â€ {last_action_reason or 'action taken'}")
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
