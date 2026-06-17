"""Activity Report AI skill for Climate Advisor."""

# ruff: noqa: E501  # _SYSTEM_PROMPT contains intentionally long AI instruction lines

from __future__ import annotations

import contextlib
import datetime
import logging
from collections.abc import Callable
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
from .temperature import format_temp

_LOGGER = logging.getLogger(__name__)

_SKILL_NAME = "activity_report"

_SYSTEM_PROMPT = """You are an HVAC automation diagnostic assistant for Climate Advisor, a Home Assistant integration.
Analyze the provided system state and sensor data.
Return your analysis with these exact section headers (use ## for headers):
## SUMMARY
2-3 sentence overview of the current situation.
If a HISTORICAL DAILY SUMMARIES section is present in the context, describe trends across the full period, not just the current state.
## TIMELINE
(The timeline table is generated programmatically -- do NOT output a Timeline section. If you include ## TIMELINE, it will be overwritten. Skip it entirely and proceed to ## DECISIONS.)
## DECISIONS
Why each automation action was taken, with the logic explained.
When fan_status is active while hvac_mode is off, explicitly trace the fan state to the logged automation action that caused it (e.g., "Fan activated -- natural ventilation: outdoor X°F <= threshold"). Do not describe the state in isolation.
- grace_started with trigger=dashboard_resume: the user manually resumed automation from the dashboard; the grace period prevents door/window sensors from immediately re-pausing. In DECISIONS, describe this as expected behavior: "90-min buffer against sensor re-trigger after user resume."
- When override_cleared and grace_started appear at the same timestamp: the user both cancelled an active override AND resumed from pause -- show these as one coordinated action in DECISIONS.
## ANOMALIES
Anything unusual: long runtimes, frequent cycling, comfort violations, unexpected states.
IMPORTANT: The STATE CROSS-VALIDATION section in the context contains pre-computed flags. If it contains [WARNING] or [FLAG] entries, call each one out explicitly here -- do NOT construct explanatory narratives around contradictions. Treat them as data quality issues or potential hardware bugs requiring investigation.
If a HISTORICAL DAILY SUMMARIES section is present in the context, flag patterns that repeat across multiple days (e.g. daily overrides, recurring violations).
NUMERIC VERIFICATION RULE: A temperature T is within comfort band [L, H] only if L <= T <= H. Verify the arithmetic directly against supplied numeric values before making any comfort characterization statement. The cross-validation section already contains this check -- reference it rather than re-deriving.
## DIAGNOSTICS
System health observations: sensor connectivity, automation engine status, learning state.

SECTION ROLES ARE EXCLUSIVE:
- SUMMARY: current state only. No analysis, no decisions, no explanations.
- DECISIONS: explain WHY each automation action was taken. Do NOT re-describe what Timeline already covered.
- ANOMALIES: items that deviate from expected behavior ONLY. Do NOT re-explain decisions already in Decisions. Reference [FLAG] items briefly -- do not construct a full explanatory narrative.
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
            avg_in_str = f"{avg_in:.1f}F" if isinstance(avg_in, (int, float)) else "n/a"
            obs_high = r.get("observed_high_f")
            obs_low = r.get("observed_low_f")
            hl_str = (
                f"{obs_high:.0f}F/{obs_low:.0f}F"
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
        _LOGGER.warning("activity_report: failed to build daily summaries -- skipping")
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

    lines.append(_engine_line("k_passive", "k_passive", " hr^-1"))
    lines.append(_engine_line("k_solar", "k_solar", " F/hr"))
    lines.append(_engine_line("solar_phase_offset_h", "solar_phase_offset_h", "h"))
    lines.append(_engine_line("k_vent_window", "k_vent_window", " hr^-1"))

    # k_active_hvac has a different shape -- values nested under "value": {"heat": ..., "cool": ...}
    hvac_info = engine_status.get("k_active_hvac", {})
    if isinstance(hvac_info, dict) and hvac_info.get("active"):
        _hvac_value = hvac_info.get("value") or {}
        heat = _hvac_value.get("heat")
        cool = _hvac_value.get("cool")
        since = hvac_info.get("since", "")
        heat_str = f"{heat:.4f}" if isinstance(heat, float) else str(heat)
        cool_str = f"{cool:.4f}" if isinstance(cool, float) else str(cool)
        since_str = f", since {since}" if since else ""
        lines.append(f"  k_active_hvac: heat={heat_str} cool={cool_str} F/hr{since_str} [ACTIVE]")
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
        "nat_vent_away_ceiling_exit",
        "occupancy_setback",
        "occupancy_comfort_restored",
        "morning_wakeup",
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

    # nat_vent_* prefix -> automation
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


# ---------------------------------------------------------------------------
# Deterministic per-event timeline table (Issue #330)
# ---------------------------------------------------------------------------

# Module-level context holder: async_build_activity_context populates this so
# parse_activity_response can override the LLM timeline section with the
# deterministic table without changing the response_parser(raw_str) call
# signature in ai_skills.py.
_activity_parse_context: dict[str, Any] = {}


def _fmt_time(raw_time: Any) -> str:
    """Format a raw timestamp from the event log as HH:MM (local)."""
    if raw_time is None:
        return "??:??"
    if isinstance(raw_time, datetime.datetime):
        dt = raw_time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt_util.as_local(dt).strftime("%H:%M")
    try:
        dt = datetime.datetime.fromisoformat(str(raw_time))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt_util.as_local(dt).strftime("%H:%M")
    except (ValueError, TypeError):
        return str(raw_time)


def _humanize_type(event_type: str) -> str:
    """Convert snake_case event type to a human-readable label."""
    return event_type.replace("_", " ").capitalize()


def _format_band_setpoint(floor: Any, ceiling: Any, active: Any, unit: str) -> str:
    """Render a ComfortBand as a single-setpoint Settings cell.

    active == "ceiling" -> the cool setpoint is the guarded edge.
    active == "floor"   -> the heat setpoint is the guarded edge.
    """
    try:
        floor_f = float(floor)
        ceiling_f = float(ceiling)
    except (TypeError, ValueError):
        return ""
    if active == "ceiling":
        return f"setpoint: {format_temp(ceiling_f, unit)} Cool ({format_temp(floor_f, unit)} Heat)"
    if active == "floor":
        return f"setpoint: {format_temp(floor_f, unit)} Heat ({format_temp(ceiling_f, unit)} Cool)"
    # active unknown -- show both
    return f"setpoint: {format_temp(floor_f, unit)} Heat / {format_temp(ceiling_f, unit)} Cool"


# ---------------------------------------------------------------------------
# EVENT_RENDERERS: (payload, unit) -> (event_text, settings_text)
# All renderers read structured payload fields -- never parse prose strings.
# ---------------------------------------------------------------------------


def _render_comfort_band_applied(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    reason = p.get("reason", "")
    label = f"Comfort band applied ({mode})" if mode else "Comfort band applied"
    if reason:
        label = f"{label} -- {reason}"
    settings = _format_band_setpoint(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return label, settings


def _render_bedtime_setback(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    label = f"Bedtime setback ({mode} mode)" if mode else "Bedtime setback"
    settings = _format_band_setpoint(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return label, settings


def _render_morning_wakeup(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    label = f"Morning wake-up -- comfort restored ({mode})" if mode else "Morning wake-up -- comfort restored"
    settings = _format_band_setpoint(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return label, settings


def _render_occupancy_setback(p: dict, unit: str) -> tuple[str, str]:
    occ = p.get("occupancy") or p.get("mode", "")
    label = f"Occupancy setback ({occ})" if occ else "Occupancy setback"
    settings = _format_band_setpoint(p.get("floor"), p.get("ceiling"), None, unit)
    return label, settings


def _render_occupancy_comfort_restored(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    target = p.get("target_f")
    label = f"Occupancy -- comfort restored ({mode})" if mode else "Occupancy -- comfort restored"
    settings = ""
    if target is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = f"setpoint: {format_temp(float(target), unit)}"
    return label, settings


def _render_pre_cool_applied(p: dict, unit: str) -> tuple[str, str]:
    target = p.get("target")
    label = "Pre-cool applied"
    settings = ""
    if target is not None:
        try:
            floor = p.get("floor")
            if floor is not None:
                settings = _format_band_setpoint(floor, float(target), "ceiling", unit)
            else:
                settings = f"setpoint: {format_temp(float(target), unit)} Cool"
        except (TypeError, ValueError):
            pass
    return label, settings


def _render_override_detected(p: dict, unit: str) -> tuple[str, str]:
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    old_m = p.get("old_mode") or p.get("old_hvac_mode")
    new_m = p.get("new_mode") or p.get("new_hvac_mode")
    source = p.get("source", "")
    label = f"Setpoint override detected ({source})" if source else "Setpoint override detected"
    parts = []
    if old_m and new_m and old_m != new_m:
        parts.append(f"mode: {old_m}->{new_m}")
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"setpoint: {format_temp(float(old_t), unit)}->{format_temp(float(new_t), unit)}")
    return label, ", ".join(parts)


def _render_ceiling_guard_fired(p: dict, unit: str) -> tuple[str, str]:
    breach = p.get("breach_time", "")
    lead = p.get("lead_time_min")
    label = f"ODE ceiling guard fired (breach {breach}, lead {lead} min)" if lead else "ODE ceiling guard fired"
    old_m = p.get("old_hvac_mode")
    new_m = p.get("new_hvac_mode", "cool")
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    parts = []
    if old_m and new_m and old_m != new_m:
        parts.append(f"mode: {old_m}->{new_m}")
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"setpoint: {format_temp(float(old_t), unit)}->{format_temp(float(new_t), unit)}")
    elif new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"setpoint: {format_temp(float(new_t), unit)}")
    return label, ", ".join(parts)


def _render_classification_applied(p: dict, unit: str) -> tuple[str, str]:
    day_type = p.get("day_type", "")
    trend = p.get("trend", "")
    hvac = p.get("hvac_mode", "")
    old_m = p.get("old_hvac_mode")
    label = f"Classification applied: {day_type}" if day_type else "Classification applied"
    if trend:
        label = f"{label} ({trend})"
    settings = ""
    if old_m and hvac and old_m != hvac:
        settings = f"mode: {old_m}->{hvac}"
    return label, settings


def _render_setpoint_rejected(p: dict, unit: str) -> tuple[str, str]:
    commanded = p.get("commanded")
    reported = p.get("reported")
    label = "Setpoint validation failed -- retry scheduled"
    settings = ""
    if commanded is not None and reported is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = (
                f"commanded {format_temp(float(commanded), unit)}, "
                f"thermostat reports {format_temp(float(reported), unit)}"
            )
    return label, settings


def _render_override_cleared(p: dict, unit: str) -> tuple[str, str]:
    was_mode = p.get("was_mode", "")
    old_t = p.get("old_setpoint_f")
    label = f"Override cleared (was {was_mode})" if was_mode else "Override cleared"
    settings = ""
    if old_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = f"was {format_temp(float(old_t), unit)} (manual setpoint)"
    return label, settings


def _render_override_confirmed(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    label = f"Override confirmed ({mode} mode)" if mode else "Override confirmed"
    return label, ""


def _render_override_self_resolved(p: dict, unit: str) -> tuple[str, str]:
    detected = p.get("detected_mode", "")
    current = p.get("current_mode", "")
    if detected and current:
        return f"Override self-resolved: {detected}->{current} (transient)", ""
    return "Override self-resolved (transient)", ""


def _render_grace_started(p: dict, unit: str) -> tuple[str, str]:
    trigger = p.get("trigger", "")
    source = p.get("source", "")
    duration = p.get("duration_seconds")
    dur_str = f" ({duration // 60} min)" if isinstance(duration, int) else ""
    label = f"Grace period started{dur_str}"
    if trigger:
        label = f"{label} -- trigger: {trigger}"
    elif source:
        label = f"{label} ({source})"
    return label, ""


def _render_grace_expired(p: dict, unit: str) -> tuple[str, str]:
    source = p.get("source", "")
    re_paused = p.get("re_paused", False)
    label = f"Grace period expired ({source})" if source else "Grace period expired"
    if re_paused:
        label = f"{label} -- sensor still open, re-paused"
    return label, ""


def _render_nat_vent_fan_on(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor_temp")
    on_thr = p.get("on_threshold")
    label = "Nat-vent fan on (cycling)"
    if indoor is not None and on_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent fan on -- indoor {format_temp(float(indoor), unit)} >= {format_temp(float(on_thr), unit)}"
            )
    return label, "fan: auto->on"


def _render_nat_vent_fan_off(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor_temp")
    off_thr = p.get("off_threshold")
    label = "Nat-vent fan off (cycling)"
    if indoor is not None and off_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent fan off -- indoor {format_temp(float(indoor), unit)} <= {format_temp(float(off_thr), unit)}"
            )
    return label, "fan: on->auto"


def _render_nat_vent_outdoor_rise_exit(p: dict, unit: str) -> tuple[str, str]:
    outdoor = p.get("outdoor")
    indoor = p.get("indoor")
    label = "Nat-vent exit -- outdoor warmer than indoor"
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent exit -- outdoor {format_temp(float(outdoor), unit)}"
                f" > indoor {format_temp(float(indoor), unit)}"
            )
    return label, ""


def _render_nat_vent_comfort_floor_exit(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor_temp")
    heat = p.get("comfort_heat")
    label = "Nat-vent exit -- comfort floor reached"
    if indoor is not None and heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent exit -- indoor {format_temp(float(indoor), unit)} <= floor {format_temp(float(heat), unit)}"
            )
    parts = []
    hvac_restored = p.get("hvac_mode_restored", "")
    fan_change = p.get("fan_mode_change", "")
    if hvac_restored and hvac_restored not in ("unknown", ""):
        parts.append(f"mode: off->{hvac_restored}")
    if fan_change:
        parts.append(f"fan: {fan_change}")
    return label, ", ".join(parts)


def _render_nat_vent_away_ceiling_exit(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor")
    cool = p.get("comfort_cool")
    label = "Nat-vent exit -- away-mode ceiling reached"
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent exit (away) -- indoor {format_temp(float(indoor), unit)}"
                f" >= ceiling {format_temp(float(cool), unit)}"
            )
    return label, ""


def _render_nat_vent_predicted_floor_exit(p: dict, unit: str) -> tuple[str, str]:
    ttf = p.get("time_to_floor_hr")
    label = "Nat-vent proactive exit -- floor predicted"
    if ttf is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = f"Nat-vent proactive exit -- floor in {float(ttf):.2f} hr"
    parts = []
    hvac_restored = p.get("hvac_mode_restored", "")
    fan_change = p.get("fan_mode_change", "")
    if hvac_restored and hvac_restored not in ("unknown", ""):
        parts.append(f"mode: off->{hvac_restored}")
    if fan_change:
        parts.append(f"fan: {fan_change}")
    return label, ", ".join(parts)


def _render_nat_vent_ceiling_escalation(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor")
    cool = p.get("comfort_cool")
    label = "Nat-vent escalated to AC cooling"
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent escalated to AC -- indoor {format_temp(float(indoor), unit)}"
                f" > ceiling {format_temp(float(cool), unit)}"
            )
    return label, "mode: off->cool"


def _render_sensor_opened(p: dict, unit: str) -> tuple[str, str]:
    entity = p.get("entity", "")
    result = p.get("result", "")
    trigger = p.get("trigger", "")
    label = f"Sensor opened -- {result}" if result else "Sensor opened"
    if entity and entity not in ("re-check", "natural_vent_reeval"):
        label = f"Sensor opened: {entity} ({result})" if result else f"Sensor opened: {entity}"
    elif trigger:
        label = f"Sensor opened -- {trigger}"
    hvac_change = p.get("hvac_mode_change", "")
    fan_change = p.get("fan_mode_change", "")
    parts = []
    if hvac_change:
        parts.append(f"mode: {hvac_change}")
    if fan_change:
        parts.append(f"fan: {fan_change}")
    return label, ", ".join(parts)


def _render_sensor_all_closed(p: dict, unit: str) -> tuple[str, str]:
    was_paused = p.get("was_paused", False)
    was_nat_vent = p.get("was_nat_vent", False)
    if was_nat_vent:
        return "All sensors closed -- ending nat-vent", ""
    if was_paused:
        return "All sensors closed -- resuming HVAC", ""
    return "All sensors closed", ""


def _render_nat_vent_forecast_skip(p: dict, unit: str) -> tuple[str, str]:
    peak = p.get("forecast_peak")
    thr = p.get("threshold")
    label = "Nat-vent skipped -- forecast too warm"
    if peak is not None and thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent skipped -- forecast peak {format_temp(float(peak), unit)}"
                f" > threshold {format_temp(float(thr), unit)}"
            )
    return label, ""


def _render_nat_vent_floor_imminent_skip(p: dict, unit: str) -> tuple[str, str]:
    ttf = p.get("time_to_floor_hr")
    label = "Nat-vent skipped -- floor imminent"
    if ttf is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = f"Nat-vent skipped -- floor in {float(ttf):.2f} hr (thermal model)"
    return label, ""


def _render_bedtime_setback_skipped(p: dict, unit: str) -> tuple[str, str]:
    reason = p.get("reason", "")
    occ = p.get("occupancy", "")
    if reason == "occupancy" and occ:
        return f"Bedtime setback skipped -- {occ} mode active", ""
    if reason:
        return f"Bedtime setback skipped -- {reason}", ""
    return "Bedtime setback skipped", ""


def _render_morning_wakeup_skipped(p: dict, unit: str) -> tuple[str, str]:
    reason = p.get("reason", "")
    return (f"Morning wake-up skipped -- {reason}" if reason else "Morning wake-up skipped"), ""


def _render_pre_cool_suppressed_nat_vent(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor")
    target = p.get("target")
    label = "Pre-cool suppressed -- nat-vent already achieved target"
    if indoor is not None and target is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Pre-cool suppressed -- nat-vent: indoor {format_temp(float(indoor), unit)}"
                f" <= target {format_temp(float(target), unit)}"
            )
    return label, ""


def _render_pre_cool_overshoot(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor")
    heat = p.get("comfort_heat")
    label = "Pre-cool overshoot -- indoor below comfort floor at wake-up"
    if indoor is not None and heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Pre-cool overshoot -- indoor {format_temp(float(indoor), unit)}"
                f" < floor {format_temp(float(heat), unit)}"
            )
    return label, ""


def _render_system_restarted(p: dict, unit: str) -> tuple[str, str]:
    recovered = p.get("recovered_events", 0)
    return f"--- HA restart boundary ({recovered} prior events recovered) ---", ""


def _render_startup_coalesced(p: dict, unit: str) -> tuple[str, str]:
    nv = p.get("nat_vent_activated", False)
    hvac = p.get("hvac_commanded", False)
    sensors = p.get("sensors_open_count", 0)
    notes = []
    if nv:
        notes.append("nat-vent activated")
    if hvac:
        notes.append("HVAC commanded")
    if sensors:
        notes.append(f"{sensors} sensor(s) open")
    suffix = " -- " + ", ".join(notes) if notes else ""
    return f"Startup coalescing complete{suffix}", ""


def _render_stuck_grace_recovered(p: dict, unit: str) -> tuple[str, str]:
    grace_end = p.get("grace_end_time", "")
    return f"Stuck grace recovered (expired {grace_end})", ""


def _render_state_contradiction_warning(p: dict, unit: str) -> tuple[str, str]:
    hvac_mode = p.get("hvac_mode", "")
    hvac_action = p.get("hvac_action", "")
    return f"State contradiction: mode={hvac_mode} but action={hvac_action}", ""


def _render_thermal_learning_no_observations(p: dict, unit: str) -> tuple[str, str]:
    runtime = p.get("hvac_runtime_minutes", "")
    if runtime:
        label = f"Thermal learning: no observations despite {runtime} min HVAC runtime"
    else:
        label = "Thermal learning: no observations recorded"
    return label, ""


def _render_incident_detected(p: dict, unit: str) -> tuple[str, str]:
    cls = p.get("incident_class", "")
    label = f"Incident detected: {cls}" if cls else "Incident detected"
    return label, ""


# Legacy warm_day events (pre-P3, may appear in persisted event logs)
def _render_warm_day_setback_applied(p: dict, unit: str) -> tuple[str, str]:
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    label = "Warm-day setback applied"
    settings = ""
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = f"setpoint: {format_temp(float(old_t), unit)}->{format_temp(float(new_t), unit)}"
    return label, settings


def _render_warm_day_state_confirmed(p: dict, unit: str) -> tuple[str, str]:
    return "Warm-day state confirmed (heartbeat)", ""


def _render_warm_day_comfort_gap(p: dict, unit: str) -> tuple[str, str]:
    return "Warm-day comfort gap -- heating before shutoff", ""


# Registry: event_type -> renderer
EVENT_RENDERERS: dict[str, Callable[[dict, str], tuple[str, str]]] = {
    "comfort_band_applied": _render_comfort_band_applied,
    "bedtime_setback": _render_bedtime_setback,
    "morning_wakeup": _render_morning_wakeup,
    "occupancy_setback": _render_occupancy_setback,
    "occupancy_comfort_restored": _render_occupancy_comfort_restored,
    "pre_cool_applied": _render_pre_cool_applied,
    "override_detected": _render_override_detected,
    "ceiling_guard_fired": _render_ceiling_guard_fired,
    "classification_applied": _render_classification_applied,
    "setpoint_rejected": _render_setpoint_rejected,
    "override_cleared": _render_override_cleared,
    "override_confirmed": _render_override_confirmed,
    "override_self_resolved": _render_override_self_resolved,
    "grace_started": _render_grace_started,
    "grace_expired": _render_grace_expired,
    "nat_vent_fan_on": _render_nat_vent_fan_on,
    "nat_vent_fan_off": _render_nat_vent_fan_off,
    "nat_vent_outdoor_rise_exit": _render_nat_vent_outdoor_rise_exit,
    "nat_vent_comfort_floor_exit": _render_nat_vent_comfort_floor_exit,
    "nat_vent_away_ceiling_exit": _render_nat_vent_away_ceiling_exit,
    "nat_vent_predicted_floor_exit": _render_nat_vent_predicted_floor_exit,
    "nat_vent_ceiling_escalation": _render_nat_vent_ceiling_escalation,
    "sensor_opened": _render_sensor_opened,
    "sensor_all_closed": _render_sensor_all_closed,
    "nat_vent_forecast_skip": _render_nat_vent_forecast_skip,
    "nat_vent_floor_imminent_skip": _render_nat_vent_floor_imminent_skip,
    "bedtime_setback_skipped": _render_bedtime_setback_skipped,
    "morning_wakeup_skipped": _render_morning_wakeup_skipped,
    "pre_cool_suppressed_nat_vent": _render_pre_cool_suppressed_nat_vent,
    "pre_cool_overshoot": _render_pre_cool_overshoot,
    "system_restarted": _render_system_restarted,
    "startup_coalesced": _render_startup_coalesced,
    "stuck_grace_recovered": _render_stuck_grace_recovered,
    "state_contradiction_warning": _render_state_contradiction_warning,
    "thermal_learning_no_observations": _render_thermal_learning_no_observations,
    "incident_detected": _render_incident_detected,
    # Legacy warm_day events (pre-P3 persisted logs)
    "warm_day_setback_applied": _render_warm_day_setback_applied,
    "warm_day_state_confirmed": _render_warm_day_state_confirmed,
    "warm_day_comfort_gap": _render_warm_day_comfort_gap,
}


def _default_renderer(event_type: str, payload: dict, unit: str) -> tuple[str, str]:
    """Surprise-safe fallback for unregistered event types.

    Event cell: humanized type + reason if present.
    Settings cell: generic extraction of recognized fields -- never blank-broken, never raises.
    """
    label = _humanize_type(event_type)
    reason = payload.get("reason")
    if reason:
        label = f"{label} -- {reason}"

    # Generic settings extraction
    parts: list[str] = []
    old_m = payload.get("old_hvac_mode") or payload.get("old_mode")
    new_m = payload.get("new_hvac_mode") or payload.get("new_mode")
    if old_m and new_m and old_m != new_m:
        parts.append(f"mode: {old_m}->{new_m}")
    old_t = payload.get("old_setpoint_f")
    new_t = payload.get("new_setpoint_f")
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(f"setpoint: {format_temp(float(old_t), unit)}->{format_temp(float(new_t), unit)}")
    floor = payload.get("floor")
    ceiling = payload.get("ceiling")
    active = payload.get("active")
    if floor is not None and ceiling is not None:
        s = _format_band_setpoint(floor, ceiling, active, unit)
        if s:
            parts.append(s)
    fan = payload.get("fan") or payload.get("fan_mode_change")
    if fan:
        parts.append(f"fan: {fan}")
    trigger = payload.get("trigger")
    if trigger and not any("trigger" in p for p in parts):
        parts.append(f"trigger: {trigger}")

    return label, ", ".join(parts)


# Types that should NOT be deduplicated (each has meaningful individual payload)
_NO_DEDUP: frozenset[str] = frozenset(
    {
        "system_restarted",
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "ceiling_guard_fired",
        "incident_detected",
        "setpoint_rejected",
        "comfort_band_applied",
        "bedtime_setback",
        "morning_wakeup",
        "occupancy_setback",
        "occupancy_comfort_restored",
        "pre_cool_applied",
        "classification_applied",
    }
)


def build_event_timeline_table(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
) -> str:
    """Build a deterministic markdown timeline table from the event log.

    Returns a markdown table string:
      | Time | Event | Settings | Source |

    Consecutive same-type events (excluding types in _NO_DEDUP) are collapsed
    into a single row with a xN count and time range.  The Settings cell of the
    collapsed row is taken from the LAST event in the run (most recent setpoint wins).
    """
    unit: str = config.get("temp_unit", "fahrenheit")
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    cutoff = now - datetime.timedelta(hours=hours)

    # ---- filter within window ----
    filtered: list[dict] = []
    for entry in raw_event_log[-200:]:
        if not isinstance(entry, dict):
            continue
        raw_time = entry.get("time")
        if raw_time is not None:
            if isinstance(raw_time, datetime.datetime):
                event_dt: datetime.datetime | None = raw_time
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=datetime.UTC)
            else:
                try:
                    event_dt = datetime.datetime.fromisoformat(str(raw_time))
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=datetime.UTC)
                except (ValueError, TypeError):
                    event_dt = None
            if event_dt is not None and event_dt < cutoff:
                continue
        filtered.append(entry)

    if not filtered:
        return "| Time | Event | Settings | Source |\n|---|---|---|---|\n| -- | (no events in window) | | |"

    # ---- render & deduplicate ----
    rows: list[tuple[str, str, str, str]] = []  # (time_str, event_text, settings_text, source)

    # Dedup state
    run_type: str | None = None
    run_count = 0
    run_first_time: str = ""
    run_last_time: str = ""
    run_settings: str = ""
    run_source: str = ""

    def _flush_run() -> None:
        nonlocal run_type, run_count, run_first_time, run_last_time, run_settings, run_source
        if run_type is None or run_count == 0:
            return
        if run_count == 1:
            rows.append((run_first_time, _humanize_type(run_type), run_settings, run_source))
        else:
            time_range = f"{run_first_time}-{run_last_time}" if run_first_time != run_last_time else run_first_time
            event_text = f"{_humanize_type(run_type)} x{run_count} ({time_range})"
            rows.append((run_first_time, event_text, run_settings, run_source))
        run_type = None
        run_count = 0

    for entry in filtered:
        event_type = str(entry.get("type", "unknown"))
        payload = {k: v for k, v in entry.items() if k not in ("time", "type")}
        time_str = _fmt_time(entry.get("time"))

        renderer = EVENT_RENDERERS.get(event_type)
        try:
            if renderer is not None:
                ev_text, settings_text = renderer(payload, unit)
            else:
                ev_text, settings_text = _default_renderer(event_type, payload, unit)
        except Exception:
            _LOGGER.warning("activity_report: renderer raised for event type %r -- using fallback", event_type)
            ev_text = _humanize_type(event_type)
            settings_text = ""

        source = _event_source_label(event_type, payload) or "sensor"

        # Flush run when type changes or type is not deduplicated
        if event_type in _NO_DEDUP or event_type != run_type:
            _flush_run()
            if event_type in _NO_DEDUP:
                rows.append((time_str, ev_text, settings_text, source))
            else:
                # Start a new run
                run_type = event_type
                run_count = 1
                run_first_time = time_str
                run_last_time = time_str
                run_settings = settings_text
                run_source = source
        else:
            # Continue run -- update last time and settings (last setpoint wins)
            run_count += 1
            run_last_time = time_str
            if settings_text:
                run_settings = settings_text

    _flush_run()

    if not rows:
        return "| Time | Event | Settings | Source |\n|---|---|---|---|\n| -- | (no events in window) | | |"

    # ---- format as markdown ----
    header = "| Time | Event | Settings | Source |"
    sep = "|---|---|---|---|"
    row_lines = [f"| {t} | {ev} | {st} | {src} |" for t, ev, st, src in rows]
    return "\n".join([header, sep, *row_lines])


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
    hours = max(1.0, min(hours, 168.0))  # clamp to frontend range 1-168h

    data: dict[str, Any] = coordinator.data or {}
    options: dict[str, Any] = coordinator.config or {}

    # --- Classification ---
    day_type = data.get(ATTR_DAY_TYPE, "unknown")
    trend = data.get(ATTR_TREND, "unknown")
    hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
    # Compute fresh runtime -- coordinator.data may be up to 30 min stale
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
    target_temp: float | None = None
    target_temp_low: float | None = None
    target_temp_high: float | None = None
    if climate_entity_id:
        climate_state = hass.states.get(climate_entity_id)
        if climate_state is not None:
            hvac_mode = climate_state.state
            current_temp = climate_state.attributes.get("current_temperature", "unknown")
            target_temp = climate_state.attributes.get("temperature")
            target_temp_low = climate_state.attributes.get("target_temp_low")
            target_temp_high = climate_state.attributes.get("target_temp_high")

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
        ca_fan_running = fan_status in (
            "active",
            "running (manual override)",
            "running (untracked)",
            "nat-vent (session active, fan idle)",
        )
        if str(hvac_action).lower() == "fan" and ca_fan_running:
            pass  # Expected: CA activated HVAC fan-only mode for natural ventilation
        else:
            state_flags.append(
                f"[WARNING] hvac_mode=off but hvac_action={hvac_action!r} -- "
                "possible stale coordinator data or thermostat reporting bug"
            )
    # Acquire thermostat swing/deadband -- suppress flags for within-swing shortfalls.
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
                f"[FLAG] Indoor {ct}F < comfort_heat {ch}F -- below by {ch - ct:.1f}F (deadband: {_swing_heat_f:.1f}F)"
            )
        elif (ct - cc) > _swing_cool_f:
            state_flags.append(
                f"[FLAG] Indoor {ct}F > comfort_cool {cc}F -- above by {ct - cc:.1f}F (deadband: {_swing_cool_f:.1f}F)"
            )
        else:
            state_flags.append(f"[OK] Indoor {ct}F is within comfort band [{ch}-{cc}F]")
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
                override_detail_lines.append(f"  #{i}  {t}  {old_t}F -> {new_t}F  ({sign}{magnitude}F, {direction})")
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

        # Bug 2 (Issue #321): flag stuck grace for AI investigator attention.
        # This condition fires when the override flag is still set but grace already
        # expired and was not properly cleared -- occupant sees HVAC stuck in override.
        if ae is not None:
            _ae_grace_end = getattr(ae, "_grace_end_time", None)
            _ae_override = getattr(ae, "_manual_override_active", False)
            _ae_grace = getattr(ae, "_grace_active", False)
            if _ae_override and not _ae_grace and _ae_grace_end is not None:
                try:
                    _grace_end_dt = datetime.datetime.fromisoformat(str(_ae_grace_end))
                    if _grace_end_dt.tzinfo is None:
                        _grace_end_dt = _grace_end_dt.replace(tzinfo=datetime.UTC)
                    if dt_util.now() > _grace_end_dt:
                        override_detail_lines.append(
                            "  WARNING STUCK GRACE DETECTED: manual_override_active=True but "
                            f"grace_end_time ({_ae_grace_end}) is in the past and no grace timer "
                            "is active. This is a critical system error -- the override should "
                            "have been cleared. Recommend flagging as top priority incongruity."
                        )
                except Exception:
                    pass
    except Exception:
        _LOGGER.warning("activity_report: failed to build override detail section -- skipping")
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
        f"  Setpoint (single): {target_temp if target_temp is not None else 'N/A'}",
        f"  Setpoint low/high: {target_temp_low if target_temp_low is not None else 'N/A'} / {target_temp_high if target_temp_high is not None else 'N/A'}",
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

            # Format: "HH:MM -- event_type: key=value ... [source_label=X]"
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

            # For override_detected events with old_setpoint_f/new_setpoint_f, annotate
            # [settings: setpoint: X->Y] so AI routes temp values to Settings.
            settings_str = ""
            if event_type == "override_detected":
                old_t = data_fields.get("old_setpoint_f")
                new_t = data_fields.get("new_setpoint_f")
                if old_t is not None and new_t is not None:
                    _unit_sym = "C" if _temp_unit == "celsius" else "F"
                    settings_str = f" [settings: setpoint: {old_t}{_unit_sym}->{new_t}{_unit_sym}]"

            line = f"  {time_str} -- {event_type}: {fields_str}{label_str}{settings_str}".rstrip(": ")
            event_lines.append(line)

        lines += [
            "",
            f"## EVENT LOG (last {_fmt_hours(hours)}, {len(event_lines)} events)",
            *(event_lines if event_lines else [f"  (no events in last {_fmt_hours(hours)})"]),
        ]
    except Exception:
        _LOGGER.warning("activity_report: failed to read event log -- skipping")
        lines += ["", "## EVENT LOG", "  (unavailable)"]

    if hours > 36:
        lines += _build_daily_summaries(coordinator, hours)

    # Populate module-level parse context so parse_activity_response can override
    # the LLM timeline section with the deterministic table without changing the
    # response_parser(raw_str) call signature in ai_skills.py.
    try:
        _activity_parse_context["raw_event_log"] = list(getattr(coordinator, "_event_log", []) or [])
        _activity_parse_context["config"] = dict(options)
        _activity_parse_context["hours"] = hours
        _activity_parse_context["now"] = dt_util.now()
    except Exception:
        _LOGGER.warning("activity_report: failed to capture parse context -- timeline will use LLM output")

    return "\n".join(lines)


def parse_activity_response(raw_response: str) -> dict[str, Any]:
    """Parse a Claude activity report response into section dict.

    Splits on ## SECTION_NAME headers. Missing sections default to empty string.
    Handles malformed or partial responses without raising.
    After parsing, overrides the timeline section with the deterministic table
    built from _activity_parse_context (populated by async_build_activity_context).
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
        # Still build deterministic timeline even for empty responses
        _override_timeline(sections)
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
            # Unrecognised header -- discard content until next known header
            if current_key is None:
                _LOGGER.debug(
                    "Activity response parser: unknown header '%s', skipping",
                    stripped,
                )
        else:
            if current_key is not None:
                current_lines.append(line)

    _flush()

    # Override the LLM timeline section with the deterministic table.
    _override_timeline(sections)

    return sections


def _override_timeline(sections: dict[str, str]) -> None:
    """Replace sections["timeline"] with the deterministic table from _activity_parse_context.

    Called from parse_activity_response and from activity_fallback path.
    No-op (logs warning) if context was not populated.
    """
    try:
        _ctx = _activity_parse_context
        if _ctx.get("raw_event_log") is not None:
            sections["timeline"] = build_event_timeline_table(
                raw_event_log=_ctx["raw_event_log"],
                config=_ctx.get("config", {}),
                hours=float(_ctx.get("hours", 24)),
                now=_ctx.get("now") or dt_util.now(),
            )
    except Exception:
        _LOGGER.warning("activity_report: failed to build deterministic timeline -- keeping LLM output")


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

    # Build deterministic timeline from event log (same table as AI path)
    hours: float = float(kwargs.get("hours", 24))
    hours = max(1.0, min(hours, 168.0))
    _fb_options: dict[str, Any] = getattr(coordinator, "config", {}) or {}
    try:
        _fb_event_log: list[Any] = list(getattr(coordinator, "_event_log", []) or [])
        timeline = build_event_timeline_table(
            raw_event_log=_fb_event_log,
            config=_fb_options,
            hours=hours,
            now=dt_util.now(),
        )
    except Exception:
        _LOGGER.warning("activity_fallback: failed to build timeline table -- using plain text")
        timeline_parts = []
        if last_action_time and last_action_time != "unknown":
            timeline_parts.append(f"{last_action_time} -- {last_action_reason or 'action taken'}")
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
