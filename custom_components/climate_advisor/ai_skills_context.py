"""Context provider registry for Climate Advisor AI skills (Issue #82).

This module owns the decomposed context providers extracted from the monolithic
async_build_investigator_context() function. Each provider is a standalone async
function with the signature::

    async def build_<name>_context(hass, coordinator, **kwargs) -> str

Providers are registered in a ContextProviderRegistry and selected by the
orchestrator in ai_skills_investigator.py.

Phase 2: providers are focus-filtered by semantic tags; KNOWN_FIXES is version-scoped; GitHub issues are TTL-cached.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from .const import (
    ATTR_AUTOMATION_STATUS,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_TREND,
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENTILATED_DECAY,
)

_LOGGER = logging.getLogger(__name__)

# GitHub issues cache TTLs (seconds)
_GITHUB_OPEN_TTL: int = 86_400  # 24 hours — open issues change daily at most
_GITHUB_CLOSED_TTL: int = 2_592_000  # 30 days — closed issues rarely change

# ---------------------------------------------------------------------------
# ContextProvider dataclass and registry
# ---------------------------------------------------------------------------


@dataclass
class ContextProvider:
    """A single named context provider for the investigator skill.

    Attributes:
        name:      Short identifier used in log messages and registry lookup.
        tags:      Semantic categories (for Phase 2 focus-filtering).
        priority:  Sort key — 0 = always essential; higher = more optional.
        builder:   Async callable (hass, coordinator, **kwargs) -> str.
        cache_ttl: Reserved for Phase 2; unused in Phase 1.
    """

    name: str
    tags: frozenset[str]
    priority: int
    builder: Callable
    cache_ttl: int | None = field(default=None)


class ContextProviderRegistry:
    """Registry of ContextProvider instances for the investigator skill."""

    def __init__(self) -> None:
        self._providers: list[ContextProvider] = []

    def register(self, provider: ContextProvider) -> None:
        """Append a provider to the registry."""
        self._providers.append(provider)

    def select(self, focus: str = "") -> list[ContextProvider]:
        """Return providers relevant to the given focus string, sorted by priority.

        If focus is empty or contains no recognised keywords, all providers are
        returned (backward-compatible with no-focus behaviour).

        Priority-0 providers are always included regardless of tag match —
        they provide the essential current-state context every investigation needs.
        """
        sorted_providers = sorted(self._providers, key=lambda p: p.priority)
        if not focus:
            return sorted_providers
        focus_lower = focus.lower()
        tag_set: frozenset[str] = frozenset()
        for keyword, tags in FOCUS_TAG_MAP.items():
            if keyword in focus_lower:
                tag_set = tag_set | tags
        if not tag_set:
            # No recognised keyword — run everything so we don't silently under-investigate
            return sorted_providers
        return [p for p in sorted_providers if p.priority == 0 or bool(p.tags & tag_set)]


# ---------------------------------------------------------------------------
# FOCUS_TAG_MAP (Phase 2 — defined now, not yet wired to select())
# ---------------------------------------------------------------------------

FOCUS_TAG_MAP: dict[str, frozenset[str]] = {
    "thermal": frozenset({"hvac", "thermal", "learning", "events", "system"}),
    "learning": frozenset({"learning", "thermal", "system"}),
    "nat-vent": frozenset({"hvac", "system", "events"}),
    "nat_vent": frozenset({"hvac", "system", "events"}),
    "fan": frozenset({"hvac", "system", "events"}),
    "override": frozenset({"learning", "events", "system"}),
    "config": frozenset({"config", "system"}),
    "window": frozenset({"learning", "events", "system"}),
}

# ---------------------------------------------------------------------------
# Helper constants for timing correlations
# ---------------------------------------------------------------------------

# Known automation cycle intervals (name → seconds).
_AUTOMATION_INTERVALS_SECONDS: dict[str, int] = {
    "coordinator_cycle": 30 * 60,  # 30 min — main coordinator update cycle
    "manual_grace": 90 * 60,  # 90 min — manual override grace period
    "sensor_grace": 5 * 60,  # 5 min  — door/window sensor grace period
    "override_confirmation": 10 * 60,  # 10 min — override confirmation window
}

# How close a delta must be to a known interval to be flagged (seconds).
_TIMING_TOLERANCE_S: int = 2 * 60  # ±2 minutes

# Event types treated as automation-sourced for timing correlation purposes.
_TIMING_AUTO_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "classification_applied",
        "comfort_band_applied",
        "grace_started",
        "grace_expired",
        "nat_vent_started",
        "nat_vent_ended",
        "nat_vent_ceiling_escalation",
        "nat_vent_comfort_floor_exit",
        "nat_vent_predicted_floor_exit",
        "nat_vent_outdoor_rise_exit",
        "nat_vent_away_ceiling_exit",
        "ceiling_guard_fired",
        "warm_day_state_confirmed",
        "warm_day_setback_applied",
        "warm_day_comfort_gap",
        "occupancy_setback",
        "occupancy_comfort_restored",
        "morning_wakeup",
    }
)

# Event types treated as manual-sourced for timing correlation purposes.
_TIMING_MANUAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "override_self_resolved",
        "manual_override_cleared",
        "handle_manual_override",
        "fan_manual_override",
    }
)

# ---------------------------------------------------------------------------
# Helper functions (moved from ai_skills_investigator.py)
# ---------------------------------------------------------------------------


def _build_timing_correlations(events: list) -> str:
    """Build a TIMING CORRELATIONS section for the investigator context.

    Scans the event log for manual events that occur within ±2 minutes of a
    known automation interval after an automation event. These coincidences
    suggest the "manual" event may actually be automation-caused.

    Returns a formatted string starting with '=== TIMING CORRELATIONS ==='.
    """
    import datetime as _dt

    lines: list[str] = ["=== TIMING CORRELATIONS ==="]
    if not events:
        lines.append("  (no events to correlate)")
        return "\n".join(lines)

    # Resolve timestamps to UTC datetime objects
    resolved: list[tuple] = []  # (dt | None, event_dict)
    for entry in events:
        if not isinstance(entry, dict):
            continue
        raw_time = entry.get("time")
        event_dt = None
        if isinstance(raw_time, _dt.datetime):
            event_dt = raw_time
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=_dt.UTC)
        elif raw_time is not None:
            try:
                event_dt = _dt.datetime.fromisoformat(str(raw_time))
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=_dt.UTC)
            except ValueError:
                pass
        resolved.append((event_dt, entry))

    # Collect automation events (with parseable timestamps)
    auto_events = [
        (dt, e)
        for dt, e in resolved
        if dt is not None and (e.get("source") in ("automation",) or str(e.get("type", "")) in _TIMING_AUTO_EVENT_TYPES)
    ]

    # Check each manual event against all prior automation events
    found_any = False
    for evt_dt, evt in resolved:
        etype = str(evt.get("type", ""))
        is_manual = evt.get("source") == "manual" or etype in _TIMING_MANUAL_EVENT_TYPES
        if not is_manual or evt_dt is None:
            continue

        # Find the nearest prior automation event
        prior_auto = [(adt, ae) for adt, ae in auto_events if adt < evt_dt]
        if not prior_auto:
            lines.append(f"  [OK] {evt_dt.strftime('%H:%M')} — {etype}: no prior automation event in window")
            found_any = True
            continue

        # Most recent prior automation event
        nearest_adt, nearest_ae = max(prior_auto, key=lambda x: x[0])
        delta_s = (evt_dt - nearest_adt).total_seconds()
        time_str = evt_dt.strftime("%H:%M")
        prior_type = str(nearest_ae.get("type", "?"))
        prior_time_str = nearest_adt.strftime("%H:%M")

        # Check against known intervals
        matched_interval: str | None = None
        for interval_name, interval_s in _AUTOMATION_INTERVALS_SECONDS.items():
            if abs(delta_s - interval_s) <= _TIMING_TOLERANCE_S:
                matched_interval = interval_name
                break

        if matched_interval is not None:
            delta_min = delta_s / 60
            lines.append(
                f"  [TIMING-COINCIDENT] {time_str} — {etype}: "
                f"{delta_min:.0f}m after {prior_type} at {prior_time_str} "
                f"(≈{matched_interval.replace('_', '-')}) — may be automation-caused"
            )
        else:
            lines.append(
                f"  [OK] {time_str} — {etype}: no matching automation interval (delta={delta_s:.0f}s from {prior_type})"
            )
        found_any = True

    if not found_any:
        lines.append("  (no manual events in window)")

    return "\n".join(lines)


def _fmt_window_compliance(compliance: dict) -> str:
    """Format window_compliance with its denominator for unambiguous AI interpretation.

    Produces e.g. "0.6667 (2 of 3 windows-recommended days)" so the AI cannot
    mistake the denominator for the total recording window.
    """
    val = compliance.get("window_compliance")
    denom = compliance.get("window_compliance_denominator", 0)
    if val is None:
        return "none (no windows-recommended days in window)"
    if denom == 0:
        return f"{val} (denominator=0)"
    numerator = round(val * denom)
    return f"{val:.4f} ({numerator} of {denom} windows-recommended days)"


def format_engine_status_for_ai(engine_status: dict) -> str:
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


# ---------------------------------------------------------------------------
# Static operational design text (Block 7)
# ---------------------------------------------------------------------------

_OPERATIONAL_DESIGN_TEXT = """\
=== CA OPERATIONAL DESIGN ===
CA has 100% programmatic control of the HVAC via HA service calls.
There is NO physical switch that can activate the fan independently.
If the fan is running, one of the following is true:
  - CA activated it (fan_status=active, natural vent or HVAC fan-only mode)
  - A user overrode it via the thermostat app (fan_status='running (manual override)')
  - It is a post-command thermostat transient (fan_status='running (untracked)')

fan_status values explained:
  inactive                  — fan is off; CA has no record of activating it
  active                    — CA commanded the fan on (natural vent or HVAC fan-only)
  running (manual override) — fan running; user overrode CA's command at the thermostat
  running (untracked)       — thermostat reports fan on but CA's _fan_active=False;
                             typical after HA restart, or post-heat blowdown transient
  off (manual override)     — _fan_override_active=True AND _fan_active=False; user turned
                             the fan on at the thermostat (setting _fan_override_active=True),
                             then turned it off before the grace period expired. The override
                             is still in effect (grace period not yet cleared), physical fan is off.
  disabled                  — fan control feature is turned off in configuration

Heating/cooling deadband (thermostat behavior — not a CA fault):
  Thermostats have a built-in deadband. Heating fires when indoor drops ~1-2°F
  below the setpoint and runs until slightly above. If CA commanded heat mode
  at comfort_heat=68°F and indoor=67°F, the thermostat reporting hvac_action=idle
  or hvac_action=fan is expected deadband behavior — not a CA failure.

Warm-day comfort floor guard:
  When day_type is warm/hot, CA sets hvac_mode=off — but ONLY after indoor reaches
  comfort_heat. If indoor < comfort_heat at automation time, CA heats first
  (event: warm_day_comfort_gap) then shuts off. A brief morning heating cycle on
  a warm day is intentional. This guard prevents comfort violations at shutoff.
The warm_day_state_confirmed event fires every 30 min when the thermostat is already off\
 (heartbeat) — no service call is made.
The warm_day_setback_applied event fires when an actual setpoint or mode change is needed\
 (cool→setback_cool, heat→setback_heat, or hard off).
High event counts for warm_day_state_confirmed on sustained warm days are expected normal\
 behavior — 60+ firings in 48 hours is typical.

Natural ventilation / economizer maintain phase:
  CA can set hvac_mode=off AND fan_mode=on simultaneously for fan-only air
  circulation. hvac_mode=off with fan running is NOT a contradiction when
  fan_status=active or natural_vent_active=True. This is the economizer phase.

State contradiction warning:
  Fires when hvac_mode=off and hvac_action is heating/cooling/fan AND
  the fan is not CA-controlled and not already classified as untracked.
  It does NOT fire for untracked fans (already acknowledged) or CA-activated fans.
"""

# ---------------------------------------------------------------------------
# Provider functions
# ---------------------------------------------------------------------------


async def build_current_state_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build CURRENT STATE section from coordinator.data."""
    try:
        data: dict[str, Any] = coordinator.data or {}
        day_type = data.get(ATTR_DAY_TYPE, "unknown")
        trend = data.get(ATTR_TREND, "unknown")
        hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
        # Compute fresh runtime — coordinator.data may be up to 30 min stale (Issue #464)
        hvac_runtime_today = coordinator.get_hvac_runtime_today()
        automation_status = data.get(ATTR_AUTOMATION_STATUS, "unknown")
        last_action_time = data.get(ATTR_LAST_ACTION_TIME, "unknown")
        last_action_reason = data.get(ATTR_LAST_ACTION_REASON, "unknown")
        next_action = data.get(ATTR_NEXT_AUTOMATION_ACTION, "unknown")
        next_action_time = data.get(ATTR_NEXT_AUTOMATION_TIME, "unknown")
        occupancy_mode = data.get(ATTR_OCCUPANCY_MODE, "unknown")
        fan_status = data.get(ATTR_FAN_STATUS, "unknown")
        contact_status = data.get(ATTR_CONTACT_STATUS, "unknown")

        lines = [
            "=== CURRENT STATE ===",
            f"  day_type:            {day_type}",
            f"  trend:               {trend}",
            f"  hvac_action:         {hvac_action}",
            f"  hvac_runtime_today:  {hvac_runtime_today} min",
            f"  automation_status:   {automation_status}",
            f"  last_action_time:    {last_action_time}",
            f"  last_action_reason:  {last_action_reason}",
            f"  next_action:         {next_action}",
            f"  next_action_time:    {next_action_time}",
            f"  occupancy_mode:      {occupancy_mode}",
            f"  fan_status:          {fan_status}",
            f"  contact_status:      {contact_status}",
            "",
        ]
        return "\n".join(lines)
    except Exception:
        _LOGGER.warning("investigator: failed to read coordinator.data — skipping current state")
        return "=== CURRENT STATE ===\n  unavailable\n"


async def build_hvac_entity_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build HVAC ENTITY section from HA state.

    Issue #466: hvac_mode/target_temp_low/target_temp_high read from
    coordinator.data (populated once per update cycle) instead of independently
    re-fetching hass.states.get() here — this investigator context doesn't need
    sub-cycle freshness. current_temp still needs a live read: it isn't one of
    the fields coordinator.data exposes.
    """
    try:
        data: dict[str, Any] = coordinator.data or {}
        climate_entity_id: str = (coordinator.config or {}).get("climate_entity", "")
        hvac_mode = data.get("hvac_mode") or "unknown"
        _target_temp_low = data.get("target_temp_low")
        target_temp_low = "unknown" if _target_temp_low is None else _target_temp_low
        _target_temp_high = data.get("target_temp_high")
        target_temp_high = "unknown" if _target_temp_high is None else _target_temp_high
        current_temp = "unknown"
        if climate_entity_id:
            climate_state = hass.states.get(climate_entity_id)
            if climate_state is not None:
                current_temp = climate_state.attributes.get("current_temperature", "unknown")

        lines = [
            "=== HVAC ENTITY ===",
            f"  entity_id:        {climate_entity_id or 'not configured'}",
            f"  hvac_mode:        {hvac_mode}",
            f"  current_temp:     {current_temp}",
            f"  target_temp_low:  {target_temp_low}",
            f"  target_temp_high: {target_temp_high}",
            "",
        ]
        return "\n".join(lines)
    except Exception:
        _LOGGER.warning("investigator: failed to read HVAC entity state — skipping")
        return "=== HVAC ENTITY ===\n  unavailable\n"


async def build_learning_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build learning engine sections from coordinator.learning."""
    hours: int = min(max(int(kwargs.get("hours", 168)), 1), 720)
    daily_records_days: int = min((hours + 23) // 24 + 1, 30)

    try:
        learning = coordinator.learning if hasattr(coordinator, "learning") else None
        if learning is None:
            return "=== LEARNING ===\n  learning engine not available\n"

        section_lines: list[str] = []

        # Compliance summary
        try:
            compliance: dict[str, Any] = learning.get_compliance_summary() or {}
            section_lines += [
                "=== LEARNING — COMPLIANCE SUMMARY ===",
                f"  window_compliance:              {_fmt_window_compliance(compliance)}",
                f"  avg_daily_hvac_runtime_minutes: {compliance.get('avg_daily_hvac_runtime_minutes', 'unknown')}",
                f"  comfort_score:                  {compliance.get('comfort_score', 'unknown')}",
                f"  total_manual_overrides:         {compliance.get('total_manual_overrides', 'unknown')}",
                f"  pending_suggestions:            {compliance.get('pending_suggestions', 'unknown')}",
                "  NOTE — window_compliance scope: the value above uses the last 14 days only",
                "  (get_compliance_summary() 14-day window). The suggestion engine uses full",
                "  historical records. A discrepancy between compliance summary and suggestion",
                "  engine values is expected when non-compliant days exist outside the 14-day",
                "  window — this is not a calculation bug.",
                "",
            ]
        except Exception:
            _LOGGER.warning("investigator: get_compliance_summary() failed")
            section_lines += ["=== LEARNING — COMPLIANCE SUMMARY ===", "  unavailable", ""]

        # Thermal model
        try:
            # Issue #468: pass learning_health so this call matches the canonical shape
            # used everywhere else (coordinator.py, sensor.py) — otherwise the returned
            # dict is structurally incomplete (learning_health always {}).
            _learning_health = (
                coordinator._build_learning_health()
                if callable(getattr(coordinator, "_build_learning_health", None))
                else {}
            )
            thermal: dict[str, Any] = learning.get_thermal_model(learning_health=_learning_health) or {}
            section_lines += [
                "=== LEARNING — THERMAL MODEL ===",
                f"  heating_rate_f_per_hour:   {thermal.get('heating_rate_f_per_hour', 'unknown')}",
                f"  cooling_rate_f_per_hour:   {thermal.get('cooling_rate_f_per_hour', 'unknown')}",
                f"  confidence:                {thermal.get('confidence', 'unknown')}",
                f"  observation_count_heat:    {thermal.get('observation_count_heat', 'unknown')}",
                f"  observation_count_cool:    {thermal.get('observation_count_cool', 'unknown')}",
                "",
            ]
        except Exception:
            _LOGGER.warning("investigator: get_thermal_model() failed")
            section_lines += ["=== LEARNING — THERMAL MODEL ===", "  unavailable", ""]

        # Weather bias
        try:
            bias: dict[str, Any] = learning.get_weather_bias() or {}
            section_lines += [
                "=== LEARNING — WEATHER BIAS ===",
                f"  high_bias:          {bias.get('high_bias', 'unknown')}",
                f"  low_bias:           {bias.get('low_bias', 'unknown')}",
                f"  confidence:         {bias.get('confidence', 'unknown')}",
                f"  observation_count:  {bias.get('observation_count', 'unknown')}",
                "",
            ]
        except Exception:
            _LOGGER.warning("investigator: get_weather_bias() failed")
            section_lines += ["=== LEARNING — WEATHER BIAS ===", "  unavailable", ""]

        # Active suggestions
        try:
            suggestions: list[Any] = learning.generate_suggestions() or []
            section_lines.append("=== LEARNING — ACTIVE SUGGESTIONS ===")
            if suggestions:
                for idx, sug in enumerate(suggestions, start=1):
                    if isinstance(sug, dict):
                        stype = sug.get("suggestion_type", "unknown")
                        text = sug.get("text", "")
                        evidence = sug.get("evidence", {})
                        section_lines.append(f"  [{idx}] type={stype}")
                        if text:
                            section_lines.append(f"      text: {text}")
                        if evidence:
                            section_lines.append(f"      evidence: {evidence}")
            else:
                section_lines.append("  (none)")
            section_lines.append("")
        except Exception:
            _LOGGER.warning("investigator: generate_suggestions() failed")
            section_lines += ["=== LEARNING — ACTIVE SUGGESTIONS ===", "  unavailable", ""]

        # Daily records — window determined by caller's hours parameter
        try:
            state_obj = getattr(learning, "_state", None)
            records: list[Any] = []
            if state_obj is not None:
                raw_records = getattr(state_obj, "records", None)
                if isinstance(raw_records, list):
                    records = raw_records[-daily_records_days:]

            section_lines.append(f"=== LEARNING — LAST {daily_records_days} DAILY RECORDS ===")
            if records:
                for rec in records:
                    if isinstance(rec, dict):
                        date_val = rec.get("date", "?")
                        recommended = rec.get("windows_recommended", False)
                        opened = rec.get("windows_physically_opened", rec.get("windows_opened", False))
                        compliance_val = ("opened" if opened else "not-opened") if recommended else "n/a"
                        runtime = rec.get("hvac_runtime_minutes", "?")
                        overrides = rec.get("manual_overrides", "?")
                        section_lines.append(
                            f"  {date_val}: opened={opened} window_rec={compliance_val}"
                            f" runtime={runtime}min overrides={overrides}"
                        )
            else:
                section_lines.append("  (no records)")
            section_lines.append("")
        except Exception:
            _LOGGER.warning("investigator: failed to read daily records")
            section_lines += [f"=== LEARNING — LAST {daily_records_days} DAILY RECORDS ===", "  unavailable", ""]

        return "\n".join(section_lines)
    except Exception:
        _LOGGER.warning("investigator: failed to access learning engine — skipping")
        return "=== LEARNING ===\n  unavailable\n"


async def build_thermal_pipeline_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build THERMAL OBSERVATION PIPELINE section for the investigator context.

    Calls coordinator._build_learning_health() and coordinator._build_thermal_pipeline_summary()
    to surface per-obs-type rejection counts, pending observation state, and engine status so the
    AI can distinguish 'k_active_cool=None because never learned' from 'pipeline failure'.
    """
    lines: list[str] = ["=== THERMAL OBSERVATION PIPELINE ==="]

    # --- Per-type health from _build_learning_health() ---
    try:
        health: dict = (
            coordinator._build_learning_health()
            if callable(getattr(coordinator, "_build_learning_health", None))
            else {}
        )
    except Exception:
        health = {}

    # Retrieve current thermal model so we can flag NEVER LEARNED parameters.
    # Issue #468: pass the `health` dict already computed above — this call previously
    # omitted it entirely (computing the exact same value twice for no reason, once for
    # display here and once discarded inside get_thermal_model()'s empty default).
    try:
        learning = getattr(coordinator, "learning", None)
        thermal: dict = (learning.get_thermal_model(learning_health=health) if learning is not None else {}) or {}
    except Exception:
        thermal = {}

    k_active_cool = thermal.get("k_active_cool")
    k_active_heat = thermal.get("k_active_heat")

    all_obs_types = [
        OBS_TYPE_HVAC_HEAT,
        OBS_TYPE_HVAC_COOL,
        OBS_TYPE_PASSIVE_DECAY,
        OBS_TYPE_FAN_ONLY_DECAY,
        OBS_TYPE_VENTILATED_DECAY,
        OBS_TYPE_SOLAR_GAIN,
    ]

    # Reason codes that indicate the observation was interrupted by normal system operation.
    _OPERATIONAL_CODES = {"abandoned"}

    # Reason codes that indicate a signal quality problem worth flagging.
    _QUALITY_FAILURE_CODES = {
        "too_few_samples",
        "too_few_blocks",
        "small_delta",
        "ols_bad_fit",
        "ols_wrong_sign",
        "ols_bounds",
        "window_too_short",
        "no_interior_peak",
    }

    lines.append("Per-type rejection summary:")
    hvac_heat_committed = 0
    hvac_cool_committed = 0
    hvac_heat_total_rejected = 0
    hvac_cool_total_rejected = 0

    for obs_type in all_obs_types:
        type_health = health.get(obs_type, {})
        committed = type_health.get("committed", 0)
        rejections_by_code: dict = type_health.get("rejections", {})
        total_rejected = sum(rejections_by_code.values())

        # Split rejections into operational interruptions vs quality failures
        operational_count = sum(rejections_by_code.get(rc, 0) for rc in _OPERATIONAL_CODES)
        quality_failures: dict[str, int] = {
            rc: cnt for rc, cnt in rejections_by_code.items() if rc in _QUALITY_FAILURE_CODES and cnt > 0
        }
        quality_count = sum(quality_failures.values())

        # Track HVAC totals for pipeline failure detection
        if obs_type == OBS_TYPE_HVAC_HEAT:
            hvac_heat_committed = committed
            hvac_heat_total_rejected = total_rejected
        elif obs_type == OBS_TYPE_HVAC_COOL:
            hvac_cool_committed = committed
            hvac_cool_total_rejected = total_rejected

        # Build suffix markers
        suffix_parts: list[str] = []
        if obs_type == OBS_TYPE_HVAC_COOL and k_active_cool is None:
            suffix_parts.append("NEVER LEARNED — k_active_cool is None")
        if obs_type == OBS_TYPE_HVAC_HEAT and k_active_heat is None:
            suffix_parts.append("NEVER LEARNED — k_active_heat is None")
        suffix = f"  [{', '.join(suffix_parts)}]" if suffix_parts else ""

        lines.append(f"  {obs_type}: {committed} committed, {total_rejected} rejected{suffix}")
        if total_rejected == 0:
            lines.append("    — no rejections")
        else:
            if operational_count > 0:
                lines.append(f"    — operational interruptions: {operational_count} [expected on active days]")
            if quality_count > 0:
                qf_parts = ", ".join(
                    f"{rc} x{cnt}" for rc, cnt in sorted(quality_failures.items(), key=lambda x: -x[1])
                )
                lines.append(f"    — quality failures: {quality_count} ({qf_parts})")
            elif total_rejected > 0:
                lines.append("    — no quality failures")

    # Pipeline failure detection
    hvac_total_committed = hvac_heat_committed + hvac_cool_committed
    hvac_total_rejected = hvac_heat_total_rejected + hvac_cool_total_rejected
    if hvac_total_committed == 0 and hvac_total_rejected > 0:
        lines.append(
            f"  *** PIPELINE FAILURE INDICATOR: 0 committed HVAC observations,"
            f" {hvac_total_rejected} rejections — pipeline is not learning from HVAC cycles ***"
        )

    # Source estimator counts
    endpoint_count = health.get("source_endpoint_count", 0)
    block_ols_count = health.get("source_block_ols_count", 0)
    lines.append(f"  chart_log endpoint observations: {endpoint_count}")
    lines.append(f"  block-OLS observations: {block_ols_count}")
    if endpoint_count == 0 and block_ols_count == 0:
        lines.append(
            "  NOTE: 0 chart_log observations — consider running"
            " python tools/thermal_replay.py --chart-log --write to backfill"
        )

    # --- Engine status ---
    lines.append("")
    lines.append("Engine status:")
    try:
        if learning is not None and hasattr(learning, "get_engine_status"):
            engine_status = learning.get_engine_status()
            engine_lines = format_engine_status_for_ai(engine_status)
            lines.append(engine_lines)
        else:
            lines.append("  unavailable")
    except Exception:
        lines.append("  unavailable")

    lines.append("")
    return "\n".join(lines)


async def build_event_log_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build EVENT LOG and TIMING CORRELATIONS sections."""
    hours: int = min(max(int(kwargs.get("hours", 168)), 1), 720)

    event_section_lines: list[str] = []
    timing_section: str = ""

    # --- Event log ---
    try:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        recent_events: list[Any] = []

        for entry in event_log[-200:]:
            if not isinstance(entry, dict):
                continue
            raw_time = entry.get("time")
            if raw_time is None:
                recent_events.append(entry)
                continue
            # Accept datetime objects or ISO strings
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
                    recent_events.append(entry)
                    continue
            if event_dt >= cutoff:
                recent_events.append(entry)

        # Count by type
        type_counts: dict[str, int] = {}
        errors_and_warnings: list[dict[str, Any]] = []
        for entry in recent_events:
            etype = str(entry.get("type", "unknown"))
            type_counts[etype] = type_counts.get(etype, 0) + 1
            if "error" in etype.lower() or "warning" in etype.lower():
                errors_and_warnings.append(entry)

        event_section_lines += [
            f"=== EVENT LOG (last {hours}h, {len(recent_events)} events) ===",
            f"  event_type_counts: {type_counts}",
            f"  errors_and_warnings_count: {len(errors_and_warnings)}",
        ]
        if errors_and_warnings:
            event_section_lines.append("  ERROR/WARNING ENTRIES:")
            for entry in errors_and_warnings:
                event_section_lines.append(f"    {entry}")
        event_section_lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to read event log — skipping")
        event_section_lines += ["=== EVENT LOG ===", "  unavailable", ""]

    # --- Timing correlations ---
    try:
        raw_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        timing_section = _build_timing_correlations(raw_log)
    except Exception:
        _LOGGER.warning("investigator: failed to build timing correlations -- skipping")
        timing_section = "=== TIMING CORRELATIONS ===\n  unavailable"

    return "\n".join(event_section_lines) + "\n" + timing_section + "\n"


async def build_ai_report_history_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build RECENT AI ACTIVITY REPORTS section."""
    try:
        report_history_fn = getattr(coordinator, "get_ai_report_history", None)
        if callable(report_history_fn):
            report_history: list[Any] = report_history_fn() or []
            last_reports = report_history[-3:]
            lines = ["=== RECENT AI ACTIVITY REPORTS (last 3) ==="]
            if last_reports:
                for rpt in last_reports:
                    if isinstance(rpt, dict):
                        ts = rpt.get("timestamp", "unknown")
                        result = rpt.get("result", {})
                        summary_text = result.get("data", {}).get("summary", "") if isinstance(result, dict) else ""
                        lines.append(f"  [{ts}] summary: {summary_text or '(no summary)'}")
            else:
                lines.append("  (no prior reports)")
            lines.append("")
            return "\n".join(lines)
        return "=== RECENT AI ACTIVITY REPORTS ===\n  get_ai_report_history not available\n"
    except Exception:
        _LOGGER.warning("investigator: failed to read AI report history — skipping")
        return "=== RECENT AI ACTIVITY REPORTS ===\n  unavailable\n"


async def build_config_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build CONFIGURATION section (sensitive keys stripped)."""
    try:
        cfg: dict[str, Any] = dict(coordinator.config or {})
        cfg.pop("ai_api_key", None)

        _comfort_heat = cfg.get("comfort_heat", "unknown")
        _comfort_cool = cfg.get("comfort_cool", "unknown")
        lines = [
            "=== CONFIGURATION ===",
            f"  comfort_heat (lower bound): {_comfort_heat} — indoor must be >= this to be in comfort band",
            f"  comfort_cool (upper bound): {_comfort_cool} — indoor must be <= this to be in comfort band",
            f"  comfort_band: [{_comfort_heat}, {_comfort_cool}]°F"
            " — temperature T is in-band only if comfort_heat <= T <= comfort_cool",
            f"  setback_heat:    {cfg.get('setback_heat', 'unknown')}",
            f"  setback_cool:    {cfg.get('setback_cool', 'unknown')}",
            f"  wake_time:       {cfg.get('wake_time', 'unknown')}",
            f"  sleep_time:      {cfg.get('sleep_time', 'unknown')}",
            f"  briefing_time:   {cfg.get('briefing_time', 'unknown')}",
            f"  ai_enabled:      {cfg.get('ai_enabled', 'unknown')}",
            f"  ai_model:        {cfg.get('ai_model', 'unknown')}",
            f"  learning_enabled:{cfg.get('learning_enabled', 'unknown')}",
            "",
        ]
        return "\n".join(lines)
    except Exception:
        _LOGGER.warning("investigator: failed to read config — skipping")
        return "=== CONFIGURATION ===\n  unavailable\n"


async def build_operational_design_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Return the static CA operational design prose."""
    return _OPERATIONAL_DESIGN_TEXT


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple.

    Examples: '0.4.47' -> (0, 4, 47);  '0.3.55' -> (0, 3, 55).
    Returns (0,) on any parse failure.
    """
    try:
        return tuple(int(x) for x in str(version_str).split("."))
    except (ValueError, AttributeError):
        return (0,)


def _fix_is_relevant(fix: dict, current_tuple: tuple[int, ...]) -> bool:
    """Return True if a KNOWN_FIXES entry should be included in investigator context.

    Inclusion rules (any one is sufficient):
    1. scope_not_covered is non-empty  — still partially unfixed, always relevant.
    2. version_fixed >= current version — just fixed in current release (tell Claude
       not to re-flag) OR fix not yet deployed (known open issue).

    Entries that are fully covered AND were fixed in a prior version are excluded —
    Claude has no actionable reason to see them and they add noise.
    """
    if fix.get("scope_not_covered"):
        return True
    fix_tuple = _parse_version(fix.get("version_fixed", "0"))
    return fix_tuple >= current_tuple


async def build_known_fixes_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build KNOWN-FIXED ISSUES section, version-scoped to relevant entries only."""
    from .const import KNOWN_FIXES, VERSION  # noqa: PLC0415

    if not KNOWN_FIXES:
        return ""

    current_tuple = _parse_version(VERSION)
    relevant = {issue_num: fix for issue_num, fix in KNOWN_FIXES.items() if _fix_is_relevant(fix, current_tuple)}

    if not relevant:
        return ""

    lines = [
        f"## KNOWN-FIXED ISSUES (version-scoped to v{VERSION} — {len(relevant)} of {len(KNOWN_FIXES)} entries)"
        " (scope-bounded — use for cross-check, step 8)"
    ]
    for issue_num in sorted(relevant.keys(), reverse=True):
        fix = relevant[issue_num]
        lines.append(f"\nIssue #{issue_num} — fixed in v{fix['version_fixed']}: {fix['title']}")
        for covered in fix.get("scope_covered", []):
            lines.append(f"  [COVERED] {covered}")
        for gap in fix.get("scope_not_covered", []):
            lines.append(f"  [NOT COVERED] {gap}")
    lines.append("")
    return "\n".join(lines)


async def build_version_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build version/release notes section for investigator context."""
    from .const import RELEASE_NOTES, VERSION  # noqa: PLC0415

    lines = [f"## RUNNING VERSION\n{VERSION}\n"]
    lines.append("## RECENT RELEASE NOTES")
    for ver, notes in list(RELEASE_NOTES.items())[:5]:
        lines.append(f"\n### v{ver}")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


async def _fetch_github_issues(hass: Any, coordinator: Any = None) -> str:
    """Fetch recent GitHub issues. Patchable sub-function for tests.

    Uses coordinator._github_open_cache / _github_closed_cache with independent
    TTLs: 24 h for open issues, 30 days for closed. Returns '' on network error.
    If coordinator is None or lacks cache fields, fetches unconditionally.
    """
    import time  # noqa: PLC0415

    import aiohttp  # noqa: PLC0415

    from .const import (  # noqa: PLC0415
        GITHUB_API_BASE,
        GITHUB_CONTEXT_TIMEOUT,
        GITHUB_ISSUES_LIMIT,
        GITHUB_REPO,
        GITHUB_REPO_URL,
    )

    now = time.monotonic()

    # --- Read from cache ---
    open_issues: list[dict] | None = None
    closed_issues: list[dict] | None = None
    # Retain any cached value (even if expired) as a fallback for network errors.
    stale_open: list[dict] | None = None
    stale_closed: list[dict] | None = None

    if coordinator is not None:
        open_cache = getattr(coordinator, "_github_open_cache", None)
        open_ts = getattr(coordinator, "_github_open_cache_ts", 0.0)
        stale_open = open_cache
        if open_cache is not None and now - open_ts < _GITHUB_OPEN_TTL:
            open_issues = open_cache

        closed_cache = getattr(coordinator, "_github_closed_cache", None)
        closed_ts = getattr(coordinator, "_github_closed_cache_ts", 0.0)
        stale_closed = closed_cache
        if closed_cache is not None and now - closed_ts < _GITHUB_CLOSED_TTL:
            closed_issues = closed_cache

    # --- Fetch what's missing ---
    try:
        session = hass.helpers.aiohttp_client.async_get_clientsession()
        timeout = aiohttp.ClientTimeout(total=GITHUB_CONTEXT_TIMEOUT)
        base = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/issues"

        if open_issues is None:
            url = f"{base}?state=open&per_page={GITHUB_ISSUES_LIMIT}&sort=updated"
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    open_issues = await resp.json()
                    if coordinator is not None:
                        coordinator._github_open_cache = open_issues
                        coordinator._github_open_cache_ts = now
                else:
                    open_issues = open_issues or []

        if closed_issues is None:
            url = f"{base}?state=closed&per_page={GITHUB_ISSUES_LIMIT}&sort=updated"
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    closed_issues = await resp.json()
                    if coordinator is not None:
                        coordinator._github_closed_cache = closed_issues
                        coordinator._github_closed_cache_ts = now
                else:
                    closed_issues = closed_issues or []

    except Exception:  # noqa: BLE001
        # On network error, use whatever we have from cache (may be stale but better than nothing)
        open_issues = open_issues or stale_open or []
        closed_issues = closed_issues or stale_closed or []

    all_issues = list(open_issues or []) + list(closed_issues or [])
    if not all_issues:
        return ""

    lines = [f"## GITHUB REPOSITORY\n{GITHUB_REPO_URL}\n", "## RECENT GITHUB ISSUES"]
    for issue in all_issues:
        state = issue.get("state", "?")
        number = issue.get("number", "?")
        title = issue.get("title", "")[:100]
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        lines.append(f"- #{number} ({state}){label_str}: {title}")
    return "\n".join(lines)


async def build_github_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Fetch recent GitHub issues for investigator context (cached)."""
    return await _fetch_github_issues(hass, coordinator)


# ---------------------------------------------------------------------------
# Global registry instance
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY = ContextProviderRegistry()

_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="current_state",
        tags=frozenset({"system", "hvac"}),
        priority=0,
        builder=build_current_state_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="hvac_entity",
        tags=frozenset({"hvac"}),
        priority=0,
        builder=build_hvac_entity_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="learning",
        tags=frozenset({"learning", "thermal"}),
        priority=1,
        builder=build_learning_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="thermal_pipeline",
        tags=frozenset({"thermal"}),
        priority=1,
        builder=build_thermal_pipeline_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="event_log",
        tags=frozenset({"events"}),
        priority=1,
        builder=build_event_log_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="ai_report_history",
        tags=frozenset({"context"}),
        priority=2,
        builder=build_ai_report_history_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="config",
        tags=frozenset({"config"}),
        priority=2,
        builder=build_config_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="operational_design",
        tags=frozenset({"system"}),
        priority=3,
        builder=build_operational_design_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="known_fixes",
        tags=frozenset({"system"}),
        priority=3,
        builder=build_known_fixes_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="version",
        tags=frozenset({"system"}),
        priority=3,
        builder=build_version_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="github",
        tags=frozenset({"external"}),
        priority=4,
        builder=build_github_context,
    )
)


def get_provider_registry() -> ContextProviderRegistry:
    """Return the global ContextProviderRegistry instance."""
    return _PROVIDER_REGISTRY
