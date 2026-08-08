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

import contextlib
import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    pass

from . import log_capture
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
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    MAX_WEATHER_BIAS_APPLY_F,
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENTILATED_DECAY,
    THERMAL_SWING_DEFAULT_F,
)
from .fan_status import is_ca_fan_running
from .temperature import format_temp, format_temp_delta

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

    def select(self, focus: str = "", narration: bool = False) -> list[ContextProvider]:
        """Return providers relevant to the given focus string, sorted by priority.

        If focus is empty or contains no recognised keywords, all providers are
        returned (backward-compatible with no-focus behaviour) — UNLESS narration=True.

        narration=True is for the silent/scheduled narration path (never combined with
        a non-empty focus in practice — narration call sites never set one, and the
        on-demand Investigate call site never sets narration). It caps providers to
        priority <= 1 (current-state + recent-activity), skipping the audit-depth and
        network-bound providers (priority 2-4: daily summaries, report history, config,
        operational design, known fixes, version, GitHub) that a "what happened
        recently" narration doesn't need — see Issue #563.

        Priority-0 providers are always included regardless of tag match —
        they provide the essential current-state context every investigation needs.
        """
        sorted_providers = sorted(self._providers, key=lambda p: p.priority)
        if narration:
            return [p for p in sorted_providers if p.priority <= 1]
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
    "briefing": frozenset({"briefing", "system"}),
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

# Issue #205: automation event types whose immediate proximity to an
# override_detected event indicates a known false-positive override detection,
# not a genuine user override. `grace_started` only counts when automation-sourced.
_OVERRIDE_FALSE_POSITIVE_WINDOW_S = 60
_ISSUE_205_AUTOMATION_TYPE_PREFIXES: tuple[str, ...] = ("nat_vent_",)
_ISSUE_205_AUTOMATION_TYPES: frozenset[str] = frozenset(
    {"ceiling_guard_fired", "classification_applied", "grace_started"}
)


def _is_issue_205_automation_event(entry: dict) -> bool:
    """Return True if `entry` is an automation event type relevant to Issue #205."""
    etype = str(entry.get("type", ""))
    if etype.startswith(_ISSUE_205_AUTOMATION_TYPE_PREFIXES):
        return True
    if etype in _ISSUE_205_AUTOMATION_TYPES:
        if etype == "grace_started":
            return entry.get("source") == "automation"
        return True
    return False


def _build_known_override_false_positives(events: list) -> str:
    """Detect the Issue #205 false-override pattern deterministically.

    An `override_detected` event within 60 seconds of an automation-initiated
    event (`nat_vent_*`, `ceiling_guard_fired`, `classification_applied`,
    `grace_started` with source=automation) is a known code-path false positive —
    automation actions must never trigger override detection. Previously this was
    encoded as ~15 lines of prompt text the model had to re-derive from raw
    timestamps every run; this computes the match once, deterministically, so the
    model only has to cite the result instead of re-doing the arithmetic (and
    risking getting the 60-second window wrong).

    Returns a formatted string starting with '=== KNOWN OVERRIDE FALSE POSITIVES
    (Issue #205) ==='.
    """
    import datetime as _dt

    lines: list[str] = ["=== KNOWN OVERRIDE FALSE POSITIVES (Issue #205) ==="]

    resolved: list[tuple[_dt.datetime | None, dict]] = []
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

    automation_events = [(dt, e) for dt, e in resolved if dt is not None and _is_issue_205_automation_event(e)]
    override_events = [
        (dt, e) for dt, e in resolved if dt is not None and str(e.get("type", "")) == "override_detected"
    ]

    matches: list[str] = []
    for evt_dt, _evt in override_events:
        for auto_dt, auto_evt in automation_events:
            delta_s = abs((evt_dt - auto_dt).total_seconds())
            if delta_s <= _OVERRIDE_FALSE_POSITIVE_WINDOW_S:
                matches.append(
                    f"  override_detected at {evt_dt.strftime('%H:%M:%S')} is {delta_s:.0f}s from"
                    f" {auto_evt.get('type')} at {auto_dt.strftime('%H:%M:%S')} — known false override"
                    " detection (Issue #205), not a genuine incongruity"
                )
                break

    lines.extend(matches if matches else ["  None detected in this window."])
    return "\n".join(lines)


def _build_restart_summary(events: list) -> str:
    """Summarize `system_restarted` events by cause (Issue #563).

    `coordinator.py`'s restart-cause classification (Issue #403/#413) already
    distinguishes `user_restart`/`version_changed` (benign — a deploy or a normal
    restart) from `unknown` (crash-like, worth flagging) and stamps it as the
    event's `cause` field. Previously nothing surfaced that breakdown to the
    investigator — only a raw `system_restarted` count was visible via
    `event_type_counts` — so a day of routine deploys could get narrated as
    an alarming number of restarts. This hands the model the breakdown directly.

    Returns a formatted string starting with '=== RESTART HISTORY ==='.
    """
    lines: list[str] = ["=== RESTART HISTORY ==="]
    restarts = [e for e in events if isinstance(e, dict) and str(e.get("type", "")) == "system_restarted"]
    if not restarts:
        lines.append("  No restarts in this window.")
        return "\n".join(lines)

    by_cause: dict[str, int] = {}
    unknown_times: list[str] = []
    for entry in restarts:
        cause = str(entry.get("cause", "unknown"))
        by_cause[cause] = by_cause.get(cause, 0) + 1
        if cause == "unknown":
            unknown_times.append(str(entry.get("time", "?")))

    breakdown = ", ".join(f"{cause}={count}" for cause, count in sorted(by_cause.items()))
    lines.append(f"  {len(restarts)} restart(s) in this window: {breakdown}")
    lines.append(
        "  Only cause=unknown restarts are potentially crash-like and worth mentioning;"
        " user_restart (a normal restart) and version_changed (a deploy) are expected,"
        " benign events — do not narrate them as problems or cite the raw restart count"
        " as if every restart were equally concerning."
    )
    if unknown_times:
        lines.append(f"  cause=unknown restart timestamps: {', '.join(unknown_times)}")
    return "\n".join(lines)


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


async def build_last_briefing_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build LAST BRIEFING section — the most recently rendered briefing text (Issue #518).

    Lets the investigator review the user-facing briefing itself for internal
    contradictions (e.g. header/body disagreeing on a time, or a sentence that
    doesn't match the day's actual hvac_mode) rather than only inspecting the
    structured coordinator state that produced it.
    """
    try:
        text = getattr(coordinator, "_last_briefing", "") or ""
        if not text:
            return "=== LAST BRIEFING ===\n  not yet generated today\n"
        return "\n".join(["=== LAST BRIEFING ===", text, ""])
    except Exception:
        _LOGGER.warning("investigator: failed to read coordinator._last_briefing — skipping")
        return "=== LAST BRIEFING ===\n  unavailable\n"


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
        _target_temp_high = data.get("target_temp_high")
        _off_note = " (expected — hvac_mode=off, no active setpoint)" if hvac_mode == "off" else ""
        target_temp_low = f"unknown{_off_note}" if _target_temp_low is None else _target_temp_low
        target_temp_high = f"unknown{_off_note}" if _target_temp_high is None else _target_temp_high
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
                f"  cap_f:              {MAX_WEATHER_BIAS_APPLY_F}",
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
    """Build EVENT LOG, TIMING CORRELATIONS, and KNOWN OVERRIDE FALSE POSITIVES sections."""
    hours: int = min(max(int(kwargs.get("hours", 168)), 1), 720)

    event_section_lines: list[str] = []
    timing_section: str = ""
    false_positives_section: str = ""
    restart_section: str = ""

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
        for entry in recent_events:
            etype = str(entry.get("type", "unknown"))
            type_counts[etype] = type_counts.get(etype, 0) + 1

        event_section_lines += [
            f"=== EVENT LOG (last {hours}h, {len(recent_events)} events) ===",
            f"  event_type_counts: {type_counts}",
            "",
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to read event log — skipping")
        event_section_lines += ["=== EVENT LOG ===", "  unavailable", ""]

    # --- Real captured log records (Issue #578 — see log_capture.py) ---
    try:
        handler = log_capture.get_handler(hass) if hass is not None else None
        records = handler.get_records() if handler is not None else []
        cutoff_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        recent_records: list[dict[str, Any]] = []
        for r in records:
            try:
                record_dt = datetime.datetime.fromisoformat(str(r.get("time"))).astimezone(datetime.UTC)
            except ValueError:
                recent_records.append(r)
                continue
            if record_dt >= cutoff_dt:
                recent_records.append(r)
        event_section_lines += [
            f"=== SYSTEM LOG RECORDS (WARNING+, last {hours}h, {len(recent_records)} records) ===",
        ]
        if recent_records:
            for r in recent_records:
                local_time = _fmt_time(r.get("time"))
                event_section_lines.append(f"  {local_time} {r['level']} [{r['logger_name']}] {r['message']}")
        else:
            event_section_lines.append("  (none captured in window)")
        event_section_lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to read captured log records — skipping")
        event_section_lines += ["=== SYSTEM LOG RECORDS ===", "  unavailable", ""]

    # --- Timing correlations ---
    try:
        raw_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        timing_section = _build_timing_correlations(raw_log)
    except Exception:
        _LOGGER.warning("investigator: failed to build timing correlations -- skipping")
        timing_section = "=== TIMING CORRELATIONS ===\n  unavailable"

    # --- Known override false positives (Issue #205) ---
    try:
        raw_log_fp: list[Any] = getattr(coordinator, "_event_log", []) or []
        false_positives_section = _build_known_override_false_positives(raw_log_fp)
    except Exception:
        _LOGGER.warning("investigator: failed to build override false-positive check -- skipping")
        false_positives_section = "=== KNOWN OVERRIDE FALSE POSITIVES (Issue #205) ===\n  unavailable"

    # --- Restart history by cause ---
    try:
        raw_log_restart: list[Any] = getattr(coordinator, "_event_log", []) or []
        restart_section = _build_restart_summary(raw_log_restart)
    except Exception:
        _LOGGER.warning("investigator: failed to build restart summary -- skipping")
        restart_section = "=== RESTART HISTORY ===\n  unavailable"

    return (
        "\n".join(event_section_lines)
        + "\n"
        + timing_section
        + "\n"
        + false_positives_section
        + "\n"
        + restart_section
        + "\n"
    )


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


_KNOWN_FIXES_RECENT_COUNT = 15  # bound by count, same pattern as GITHUB_ISSUES_LIMIT


def _select_relevant_fixes(known_fixes: dict, current_tuple: tuple[int, ...]) -> dict:
    """Return the KNOWN_FIXES entries relevant to the investigator's cross-check.

    Bounded by count, not by an exact version-equality threshold. An earlier version
    of this filter kept an entry only when `version_fixed >= current version` — but on
    a real running install `current version` is pinned to whatever's actually deployed,
    so that threshold only ever matches the single most-recent release's fixes
    (verified directly: one release after a fix ships, its entry already drops out of
    context). That's too narrow to usefully answer "was this already fixed" for a user
    who hasn't updated in a few releases — the entire purpose of this section.

    Instead: always include any not-yet-deployed entry (version_fixed > current — a
    known, still-open gap Claude should recognize rather than "discover" fresh), plus
    the `_KNOWN_FIXES_RECENT_COUNT` most recently fixed entries. This stays properly
    bounded regardless of how large KNOWN_FIXES grows or how often releases ship — the
    two things the prior `scope_not_covered`-based rule failed at (see Issue #563:
    that field was mandatory on every entry, so the rule matched all 169 of them).
    """
    not_yet_deployed = {
        num: fix for num, fix in known_fixes.items() if _parse_version(fix.get("version_fixed", "0")) > current_tuple
    }
    already_fixed_nums = sorted(
        (num for num in known_fixes if num not in not_yet_deployed),
        key=lambda num: _parse_version(known_fixes[num].get("version_fixed", "0")),
        reverse=True,
    )
    recent = {num: known_fixes[num] for num in already_fixed_nums[:_KNOWN_FIXES_RECENT_COUNT]}
    return {**not_yet_deployed, **recent}


def _release_note_bullet(release_notes: dict, version: str, issue_num: int) -> str:
    """Find the RELEASE_NOTES bullet for a given issue number within a version's notes.

    Matches bullets formatted "Fix #N: ..." or "Feat #N: ...". Returns "" if no match,
    so the caller can fall back to the KNOWN_FIXES title.
    """
    prefix_fix = f"Fix #{issue_num}:"
    prefix_feat = f"Feat #{issue_num}:"
    for note in release_notes.get(version, []):
        if note.startswith(prefix_fix) or note.startswith(prefix_feat):
            return note
    return ""


async def build_known_fixes_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build KNOWN-FIXED ISSUES section, bounded to a recent-and-relevant subset.

    Each entry is rendered as its RELEASE_NOTES bullet (short, occupant-outcome
    language, already mandatory for every release) rather than the KNOWN_FIXES
    `title`/`scope_covered` fields, which are internal engineering detail aimed at
    a PR reviewer, not the investigator's cross-check use case. Falls back to
    `title` only when no matching RELEASE_NOTES bullet is found.
    """
    from .const import KNOWN_FIXES, RELEASE_NOTES, VERSION  # noqa: PLC0415

    if not KNOWN_FIXES:
        return ""

    current_tuple = _parse_version(VERSION)
    relevant = _select_relevant_fixes(KNOWN_FIXES, current_tuple)

    if not relevant:
        return ""

    lines = [
        f"## KNOWN-FIXED ISSUES (most recent {len(relevant)} of {len(KNOWN_FIXES)} entries)"
        " (scope-bounded — use for cross-check, step 8)"
    ]
    for issue_num in sorted(relevant.keys(), reverse=True):
        fix = relevant[issue_num]
        version_fixed = fix.get("version_fixed", "")
        summary = _release_note_bullet(RELEASE_NOTES, version_fixed, issue_num) or fix.get("title", "")
        lines.append(f"\nIssue #{issue_num} — fixed in v{version_fixed}: {summary}")
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


def _trim_issue_fields(issues: list[dict]) -> list[dict]:
    """Keep only the GitHub issue fields the context renderer actually uses.

    A raw GitHub API issue object carries 30+ fields (body, assignees, milestone,
    reactions, timestamps, etc.). Only number/title/state/labels are ever rendered
    — caching the full response wastes coordinator memory on every live install for
    up to 30 days (the closed-issue cache TTL) for data nothing reads.
    """
    trimmed = []
    for issue in issues or []:
        trimmed.append(
            {
                "number": issue.get("number"),
                "title": issue.get("title", ""),
                "state": issue.get("state", "?"),
                "labels": [{"name": lbl.get("name", "")} for lbl in issue.get("labels", [])],
            }
        )
    return trimmed


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
                    open_issues = _trim_issue_fields(await resp.json())
                    if coordinator is not None:
                        coordinator._github_open_cache = open_issues
                        coordinator._github_open_cache_ts = now
                else:
                    open_issues = open_issues or []

        if closed_issues is None:
            url = f"{base}?state=closed&per_page={GITHUB_ISSUES_LIMIT}&sort=updated"
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    closed_issues = _trim_issue_fields(await resp.json())
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

# ---------------------------------------------------------------------------
# Activity timeline rendering (moved from ai_skills_activity.py, Issue #563)
# ---------------------------------------------------------------------------


def _first_temp(entry: dict, *keys: str) -> Any:
    """Return first non-None temp value from the given keys in an event dict."""
    for k in keys:
        v = entry.get(k)
        if v is not None:
            return v
    return None


def _fmt_temp_cell(val: Any, unit: str) -> str:
    """Format a temperature value for a timeline table cell; em-dash when unavailable."""
    try:
        return format_temp(float(val), unit)
    except (TypeError, ValueError):
        return "—"


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


_AUTO_EVENT_TYPES = frozenset(
    {
        "ceiling_guard_fired",
        "classification_applied",
        "classification_suppressed_paused",
        "warm_day_state_confirmed",
        "warm_day_setback_applied",
        "warm_day_comfort_gap",
        "nat_vent_ceiling_escalation",
        "nat_vent_away_ceiling_exit",
        "nat_vent_ac_assist_armed",
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
        "override_adopted",
        "fan_manual_override",
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
    settings_parts = []
    if old_m and hvac and old_m != hvac:
        settings_parts.append(f"mode: {old_m}->{hvac}")
    today_high = p.get("today_high")
    threshold = p.get("applied_threshold_f")
    margin = p.get("threshold_margin_f")
    if today_high is not None and threshold is not None and margin is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                f"today's high {format_temp(today_high, unit)} vs. {day_type} threshold "
                f"{format_temp(threshold, unit)} ({margin:+.1f}°F)"
            )
    trend_mag = p.get("trend_magnitude")
    if trend_mag is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(f"trend magnitude {format_temp_delta(trend_mag, unit)}")
    settings = ", ".join(settings_parts)
    return label, settings


def _render_setpoint_rejected(p: dict, unit: str) -> tuple[str, str]:
    commanded = p.get("commanded")
    reported = p.get("reported")
    streak = p.get("reject_streak")
    label = "Setpoint validation failed -- retry scheduled"
    settings = ""
    if commanded is not None and reported is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = (
                f"commanded {format_temp(float(commanded), unit)}, "
                f"thermostat reports {format_temp(float(reported), unit)}"
            )
    if streak is not None:
        settings = f"{settings}, reject streak={streak}" if settings else f"reject streak={streak}"
    return label, settings


def _render_setpoint_nudge(p: dict, unit: str) -> tuple[str, str]:
    nudge_value = p.get("nudge_value")
    real_target = p.get("real_target")
    mode = p.get("mode", "")
    streak = p.get("reject_streak")
    label = "Reconciling stuck setpoint -- nudging thermostat"
    settings = ""
    if nudge_value is not None and real_target is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = (
                f"nudge to {format_temp(float(nudge_value), unit)} ({mode}),"
                f" then {format_temp(float(real_target), unit)} in 30s"
            )
    if streak is not None:
        settings = f"{settings}, reject streak={streak}" if settings else f"reject streak={streak}"
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


def _render_override_adopted(p: dict, unit: str) -> tuple[str, str]:
    mode = p.get("mode", "")
    src = p.get("source", "")
    pre_expiry = p.get("pre_expiry", False)
    label = f"Override adopted ({mode} mode)" if mode else "Override adopted"
    label = f"{label} -- automation agrees" + (", ended grace early" if pre_expiry else ", grace ended cleanly")
    settings = f"trigger: {src}" if src else ""
    return label, settings


_GRACE_TRIGGER_LABELS: dict[str, str] = {
    "fan_manual_override": "fan override (manual fan change)",
    "override_confirmed": "HVAC mode override",
    "dashboard_resume": "user resumed from dashboard",
    "sensor_closed_resume": "all sensors closed",
    "nat_vent_exit_resume": "natural ventilation ended",
}


def _render_grace_started(p: dict, unit: str) -> tuple[str, str]:
    trigger = p.get("trigger", "")
    source = p.get("source", "")
    duration = p.get("duration_seconds")
    dur_str = f" ({duration // 60} min)" if isinstance(duration, int) else ""
    label = f"Grace period started{dur_str}"
    if source:
        label = f"{label} ({source})"
    # Settings cell: human-readable trigger label for known triggers; empty otherwise.
    trigger_label = _GRACE_TRIGGER_LABELS.get(trigger, "")
    return label, trigger_label


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
    fan_device = p.get("fan_device", "fan")
    label = "Nat-vent fan on (cycling)"
    if indoor is not None and on_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent fan on -- indoor {format_temp(float(indoor), unit)} >= {format_temp(float(on_thr), unit)}"
            )
    return label, f"{fan_device}: auto->on"


def _render_nat_vent_fan_off(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor_temp")
    off_thr = p.get("off_threshold")
    fan_device = p.get("fan_device", "fan")
    label = "Nat-vent fan off (cycling)"
    if indoor is not None and off_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent fan off -- indoor {format_temp(float(indoor), unit)} <= {format_temp(float(off_thr), unit)}"
            )
    return label, f"{fan_device}: on->auto"


def _render_fan_activated(p: dict, unit: str) -> tuple[str, str]:
    reason = str(p.get("reason", "")).strip()
    fan_device = p.get("fan_device", "fan")
    label = f"Fan activated -- {reason}" if reason else "Fan activated"
    return label, f"{fan_device}: off->on"


def _render_fan_deactivated(p: dict, unit: str) -> tuple[str, str]:
    reason = str(p.get("reason", "")).strip()
    fan_device = p.get("fan_device", "fan")
    label = f"Fan deactivated -- {reason}" if reason else "Fan deactivated"
    return label, f"{fan_device}: on->off"


def _render_hvac_write_blocked_whf_active(p: dict, unit: str) -> tuple[str, str]:
    """Issue #392 Fix 1b: choke-point guard intercepted an HVAC write while WHF owns the thermostat.

    Makes the structural WHF/AC mutual-exclusion guarantee visible in the Activity Log
    instead of silently dropping the blocked write.
    """
    attempted_mode = str(p.get("attempted_mode", "")).strip()
    reason = str(p.get("reason", "")).strip()
    label = f"HVAC write blocked (whole-house fan active) -- {reason}" if reason else "HVAC write blocked"
    settings = f"hvac: blocked ({attempted_mode})" if attempted_mode else ""
    return label, settings


def _render_whf_hvac_suppressed(p: dict, unit: str) -> tuple[str, str]:
    """Issue #495: HVAC suppressed for a whole-house-fan session — CA-initiated OR a
    manual/remote fan-on detection (both now route through the same suppress helper).
    """
    prior_mode = str(p.get("prior_mode", "")).strip()
    reason = str(p.get("reason", "")).strip()
    label = f"HVAC suppressed (whole-house fan) -- {reason}" if reason else "HVAC suppressed (whole-house fan)"
    settings = f"hvac: {prior_mode}->off" if prior_mode else "hvac: ->off"
    return label, settings


def _render_whf_hvac_released(p: dict, unit: str) -> tuple[str, str]:
    """Issue #495: a manual/remote WHF session ended — HVAC suppression released and CA's
    current classification reasserted (not a blind restore of the mode captured at activation,
    since a remote-timer session can span hours).
    """
    reason = str(p.get("reason", "")).strip()
    label = f"HVAC suppression released -- {reason}" if reason else "HVAC suppression released"
    return label, "hvac: reclassifying"


def _render_fan_manual_override(p: dict, unit: str) -> tuple[str, str]:
    """Issue #524: append remote speed/timer context when the override was armed by an RF
    remote press (`automation.py::handle_fan_manual_override`'s `remote_speed`/
    `remote_timer_hours` kwargs) -- without it, this row looked identical whether a specific
    speed/timer choice drove the override or a thermostat-detected toggle did. A plain
    (non-remote) override has neither field set and renders exactly as before."""
    fan_before = str(p.get("fan_before", "")).strip()
    fan_after = str(p.get("fan_after", "")).strip()
    fan_device = p.get("fan_device", "fan")
    change = f"{fan_before}->{fan_after}" if fan_before and fan_after else ""
    settings = f"{fan_device}: {change}" if change else ""
    remote_speed = p.get("remote_speed")
    if remote_speed:
        settings = f"{settings}, remote: {remote_speed} speed" if settings else f"remote: {remote_speed} speed"
    remote_timer_hours = p.get("remote_timer_hours")
    if remote_timer_hours is not None:
        settings = (
            f"{settings}, remote timer: {remote_timer_hours}h" if settings else f"remote timer: {remote_timer_hours}h"
        )
    return "Fan manual override", settings


def _render_fan_speed_observed(p: dict, unit: str) -> tuple[str, str]:
    """Issue #519: a comfort-only remote speed change — NOT a manual override (the fan was
    already running; the user just adjusted speed, so no grace/HVAC-suppression armed)."""
    speed = str(p.get("speed", "")).strip()
    fan_device = p.get("fan_device", "fan")
    settings = f"{fan_device}: speed->{speed}" if speed else ""
    return "Fan speed observed (comfort-only)", settings


def _render_fan_running_untracked(p: dict, unit: str) -> tuple[str, str]:
    source = str(p.get("source", "")).strip() or "thermostat-initiated"
    action = str(p.get("hvac_action", "")).strip()
    label = f"Fan running (untracked) -- {source}"
    settings = f"fan: on (untracked; hvac_action={action})" if action else "fan: on (untracked)"
    return label, settings


def _render_fan_untracked_cleared(p: dict, unit: str) -> tuple[str, str]:
    fan_device = p.get("fan_device")
    settings = f"fan: {fan_device} off" if fan_device else "fan: off"
    return "Fan stopped (untracked fan ended)", settings


def _render_fan_cancel(p: dict, unit: str) -> tuple[str, str]:
    """Issue #567: branch on `trigger` — not every fan_cancel event is a user action.

    `physical_drift_correction` is CA noticing its own _fan_active bookkeeping was stale
    (the fan had already stopped) and correcting it — no user touched anything. Rendering
    that identically to a genuine user-detected fan-off misled the Activity Report into
    implying a manual action that never happened.
    """
    fan_before = str(p.get("fan_before", "?")).strip()
    fan_after = str(p.get("fan_after", "?")).strip()
    fan_device = p.get("fan_device", "fan")
    settings = f"{fan_device}: {fan_before}->{fan_after}" if fan_before and fan_after else ""
    trigger = p.get("trigger")
    if trigger == "physical_drift_correction":
        return "Fan ownership corrected -- stale flag cleared (was already off)", settings
    if trigger == "timer_boundary_settle":
        return "Fan cancel -- RF timer session ended", settings
    return "Fan cancel (user turned off)", settings


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


def _render_nat_vent_reconcile_exit(p: dict, unit: str) -> tuple[str, str]:
    label = "Nat-vent exit -- fan found running without a CA-owned session"
    reason = p.get("reason", "")
    return label, reason


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


def _render_nat_vent_soft_start_entered(p: dict, unit: str) -> tuple[str, str]:
    outdoor = p.get("outdoor")
    indoor = p.get("indoor")
    label = "Nat-vent soft-start -- purge/comfort at parity"
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent soft-start -- outdoor {format_temp(float(outdoor), unit)}"
                f" <= indoor {format_temp(float(indoor), unit)}, past today's peak"
            )
    peak = p.get("outdoor_today_peak")
    detail = f"today's peak: {format_temp(float(peak), unit)}" if peak is not None else ""
    return label, detail


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


def _render_nat_vent_ac_assist_armed(p: dict, unit: str) -> tuple[str, str]:
    return "Nat-vent + AC assist armed (full band)", ""


# Legacy event, no current emitter (Issue #593 audit) -- kept only to render
# historically-persisted event logs from a removed nat-vent code path.
def _render_nat_vent_sleep_ceiling_reached(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor_temp")
    cool = p.get("sleep_cool")
    label = "Nat-vent exit -- sleep ceiling reached"
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent exit (sleep) -- indoor {format_temp(float(indoor), unit)}"
                f" <= sleep ceiling {format_temp(float(cool), unit)}"
            )
    return label, ""


def _render_nat_vent_bedtime_continue(p: dict, unit: str) -> tuple[str, str]:
    outdoor = p.get("outdoor_temp")
    cool = p.get("sleep_cool")
    label = "Nat-vent continues through bedtime"
    if outdoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = (
                f"Nat-vent continues at bedtime -- outdoor {format_temp(float(outdoor), unit)}"
                f" < sleep ceiling {format_temp(float(cool), unit)}"
            )
    return label, ""


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
    fan_device = p.get("fan_device", "fan")
    if was_nat_vent:
        # Issue #504: the fan really did turn off here (via _exit_nat_vent(), whose own
        # fan_deactivated event is intentionally suppressed per Issue #411) — show that
        # transition in Settings instead of leaving it blank.
        return "All sensors closed -- ending nat-vent", f"{fan_device}: on->off"
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
    occ = p.get("occupancy", "")
    if reason == "occupancy" and occ:
        return f"Morning wake-up skipped -- {occ} mode active", ""
    return (f"Morning wake-up skipped -- {reason}" if reason else "Morning wake-up skipped"), ""


def _render_pre_cool_suppressed_nat_vent(p: dict, unit: str) -> tuple[str, str]:
    indoor = p.get("indoor")
    target = p.get("target")
    reason = p.get("reason")
    if reason == "active_session":
        label = "Pre-cool deferred -- nat-vent/WHF session already active"
    else:
        label = "Pre-cool suppressed -- nat-vent already achieved target"
    settings = ""
    if indoor is not None and target is not None:
        with contextlib.suppress(TypeError, ValueError):
            comparator = "<=" if reason != "active_session" else "chasing"
            settings = (
                f"indoor {format_temp(float(indoor), unit)} {comparator} target {format_temp(float(target), unit)}"
            )
    return label, settings


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
    cause = p.get("cause", "unknown")
    if cause == "version_changed":
        old = p.get("old_version")
        new = p.get("new_version")
        return (
            f"--- HA restart boundary (version_changed {old}->{new}, {recovered} prior events recovered) ---",
            "",
        )
    if cause == "user_restart":
        return f"--- HA restart boundary (user_restart, {recovered} prior events recovered) ---", ""
    return f"--- HA restart boundary (unknown, {recovered} prior events recovered) ---", ""


def _render_version_changed(p: dict, unit: str) -> tuple[str, str]:
    old = p.get("old_version")
    new = p.get("new_version")
    return f"Version changed: {old} -> {new}", ""


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
    settings_parts = []
    indoor_f = p.get("indoor_f")
    outdoor_f = p.get("outdoor_f")
    if indoor_f is not None and outdoor_f is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                f"indoor {format_temp(float(indoor_f), unit)} / outdoor {format_temp(float(outdoor_f), unit)}"
            )
    archetype = p.get("fan_archetype")
    if archetype:
        settings_parts.append(f"fan: {archetype}")
    settings = ", ".join(settings_parts)
    return f"Startup coalescing complete{suffix}", settings


def _render_stuck_grace_recovered(p: dict, unit: str) -> tuple[str, str]:
    grace_end = p.get("grace_end_time", "")
    if p.get("reason") == "grace_without_override":
        # Issue #508's watchdog mirror: grace_end_time is typically still in the future here
        # (the timer would have fired correctly on its own) — "expired" would be misleading.
        return "Stuck grace recovered (no override was active to protect it)", ""
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
    session_count = p.get("thermal_session_count")
    settings = f"sessions today: {session_count}" if session_count is not None else ""
    return label, settings


def _render_incident_detected(p: dict, unit: str) -> tuple[str, str]:
    cls = p.get("incident_class", "")
    incident_id = p.get("incident_id")
    label = f"Incident detected: {cls}" if cls else "Incident detected"
    if incident_id:
        label = f"{label} ({incident_id})"
    settings_parts = []
    indoor_f = p.get("indoor_f")
    comfort_heat = p.get("comfort_heat")
    comfort_cool = p.get("comfort_cool")
    if indoor_f is not None and comfort_heat is not None and comfort_cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                f"indoor {format_temp(float(indoor_f), unit)} vs. band "
                f"[{format_temp(float(comfort_heat), unit)}/{format_temp(float(comfort_cool), unit)}]"
            )
    occupancy = p.get("occupancy_mode")
    if occupancy:
        settings_parts.append(f"occupancy: {occupancy}")
    settings = ", ".join(settings_parts)
    return label, settings


# Legacy warm_day events (pre-P3, may appear in persisted event logs). Confirmed
# zero current emitters (Issue #593 audit) -- kept renderer-only for history.
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


def _render_classification_suppressed_paused(p: dict, unit: str) -> tuple[str, str]:
    return "Classification suppressed (windows open)", ""


def _render_occupancy_setback_suppressed_paused(p: dict, unit: str) -> tuple[str, str]:
    occupancy = p.get("occupancy", "away")
    return f"Occupancy setback suppressed (windows open, {occupancy})", ""


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
    "classification_suppressed_paused": _render_classification_suppressed_paused,
    "occupancy_setback_suppressed_paused": _render_occupancy_setback_suppressed_paused,
    "setpoint_rejected": _render_setpoint_rejected,
    "setpoint_nudge": _render_setpoint_nudge,
    "override_cleared": _render_override_cleared,
    "override_confirmed": _render_override_confirmed,
    "override_self_resolved": _render_override_self_resolved,
    "override_adopted": _render_override_adopted,
    "grace_started": _render_grace_started,
    "grace_expired": _render_grace_expired,
    "nat_vent_fan_on": _render_nat_vent_fan_on,
    "nat_vent_fan_off": _render_nat_vent_fan_off,
    "fan_activated": _render_fan_activated,
    "fan_deactivated": _render_fan_deactivated,
    "fan_manual_override": _render_fan_manual_override,
    "fan_speed_observed": _render_fan_speed_observed,
    "hvac_write_blocked_whf_active": _render_hvac_write_blocked_whf_active,
    "whf_hvac_suppressed": _render_whf_hvac_suppressed,
    "whf_hvac_released": _render_whf_hvac_released,
    "fan_running_untracked": _render_fan_running_untracked,
    "fan_untracked_cleared": _render_fan_untracked_cleared,
    "fan_cancel": _render_fan_cancel,
    "nat_vent_outdoor_rise_exit": _render_nat_vent_outdoor_rise_exit,
    "nat_vent_reconcile_exit": _render_nat_vent_reconcile_exit,
    "nat_vent_comfort_floor_exit": _render_nat_vent_comfort_floor_exit,
    "nat_vent_away_ceiling_exit": _render_nat_vent_away_ceiling_exit,
    "nat_vent_soft_start_entered": _render_nat_vent_soft_start_entered,
    "nat_vent_predicted_floor_exit": _render_nat_vent_predicted_floor_exit,
    "nat_vent_ceiling_escalation": _render_nat_vent_ceiling_escalation,
    "nat_vent_ac_assist_armed": _render_nat_vent_ac_assist_armed,
    "nat_vent_sleep_ceiling_reached": _render_nat_vent_sleep_ceiling_reached,
    "nat_vent_bedtime_continue": _render_nat_vent_bedtime_continue,
    "sensor_opened": _render_sensor_opened,
    "sensor_all_closed": _render_sensor_all_closed,
    "nat_vent_forecast_skip": _render_nat_vent_forecast_skip,
    "nat_vent_floor_imminent_skip": _render_nat_vent_floor_imminent_skip,
    "bedtime_setback_skipped": _render_bedtime_setback_skipped,
    "morning_wakeup_skipped": _render_morning_wakeup_skipped,
    "pre_cool_suppressed_nat_vent": _render_pre_cool_suppressed_nat_vent,
    "pre_cool_overshoot": _render_pre_cool_overshoot,
    "system_restarted": _render_system_restarted,
    "version_changed": _render_version_changed,
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
        "version_changed",
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "override_adopted",
        "ceiling_guard_fired",
        "incident_detected",
        "setpoint_rejected",
        "comfort_band_applied",
        "bedtime_setback",
        "morning_wakeup",
        "occupancy_comfort_restored",
        "pre_cool_applied",
        "classification_applied",
    }
)


def _maybe_prepend_whf_warning(table: str, config: dict[str, Any]) -> str:
    """Prepend a WHF command-only warning banner when fan_state_feedback is disabled."""
    _fsf = config.get("fan_state_feedback", False)
    _fmode = config.get("fan_mode", "disabled")
    _fentity = config.get("fan_entity", "")
    if _fmode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and bool(_fentity) and not _fsf:
        return (
            "⚠ Whole house fan state feedback disabled (command-only mode) "
            "-- physical fan state is unverifiable; events below reflect CA commands.\n\n" + table
        )
    return table


def build_event_timeline_table(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    newest_first: bool = False,
) -> str:
    """Build a deterministic markdown timeline table from the event log.

    Returns a markdown table string:
      | Time | Event | Settings | Source |

    Consecutive same-type events (excluding types in _NO_DEDUP) are collapsed
    into a single row with a xN count and time range.  The Settings cell of the
    collapsed row is taken from the LAST event in the run (most recent setpoint wins).

    Rows are built in chronological order internally (dedup depends on forward
    iteration). When `newest_first` is True, the final row order is reversed for
    display — most recent event first, oldest last.
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
        table = (
            "| Time | Event | Settings | Source | Indoor | Outdoor |\n"
            "|---|---|---|---|---|---|\n"
            "| -- | (no events in window) | | | | |"
        )
        return _maybe_prepend_whf_warning(table, config)

    # ---- render & deduplicate ----
    rows: list[
        tuple[str, str, str, str, str, str]
    ] = []  # (time_str, event_text, settings_text, source, indoor, outdoor)

    # Dedup state
    run_type: str | None = None
    run_count = 0
    run_first_time: str = ""
    run_last_time: str = ""
    run_ev_text: str = ""
    run_settings: str = ""
    run_source: str = ""
    run_indoor: str = ""
    run_outdoor: str = ""

    def _flush_run() -> None:
        nonlocal \
            run_type, \
            run_count, \
            run_first_time, \
            run_last_time, \
            run_ev_text, \
            run_settings, \
            run_source, \
            run_indoor, \
            run_outdoor
        if run_type is None or run_count == 0:
            return
        if run_count == 1:
            # A run of exactly one event was never actually deduplicated with anything —
            # this is the common case, not a collapsed group. Use the renderer's real
            # event text (which carries the descriptive reason) instead of the bare
            # _humanize_type(run_type) fallback, which silently discarded it for every
            # event type not on the small _NO_DEDUP allowlist.
            rows.append((run_first_time, run_ev_text, run_settings, run_source, run_indoor, run_outdoor))
        else:
            time_range = f"{run_first_time}-{run_last_time}" if run_first_time != run_last_time else run_first_time
            event_text = f"{_humanize_type(run_type)} x{run_count} ({time_range})"
            rows.append((run_first_time, event_text, run_settings, run_source, run_indoor, run_outdoor))
        run_type = None
        run_count = 0

    # Fan ownership tracker: updated per-event to detect when nat_vent_fan_off fires
    # while the user is still running the fan manually (misleading if shown as CA fan-off).
    _fan_ca_owns = False
    _fan_user_owns = False

    for entry in filtered:
        event_type = str(entry.get("type", "unknown"))
        payload = {k: v for k, v in entry.items() if k not in ("time", "type")}
        time_str = _fmt_time(entry.get("time"))

        # Update fan ownership state before rendering
        if event_type in ("nat_vent_fan_on", "fan_activated"):
            _fan_ca_owns = True
            _fan_user_owns = False
        elif event_type == "fan_manual_override" and str(payload.get("fan_after", "")).strip() == "on":
            _fan_user_owns = True
            _fan_ca_owns = False
        elif event_type == "fan_cancel":
            _fan_user_owns = False
        elif event_type in ("nat_vent_fan_off", "fan_deactivated"):
            _fan_ca_owns = False

        renderer = EVENT_RENDERERS.get(event_type)
        try:
            if renderer is not None:
                ev_text, settings_text = renderer(payload, unit)
            else:
                ev_text, settings_text = _default_renderer(event_type, payload, unit)
            # When nat_vent_fan_off fires while the user owns the fan, annotate the label
            # so the developer knows the physical fan may still be running under user control.
            if event_type == "nat_vent_fan_off" and _fan_user_owns:
                ev_text = ev_text + " [NOTE: fan may still be running -- user-controlled]"
        except Exception:
            _LOGGER.warning("activity_report: renderer raised for event type %r -- using fallback", event_type)
            ev_text = _humanize_type(event_type)
            settings_text = ""

        source = _event_source_label(event_type, payload) or "sensor"
        indoor_cell = _fmt_temp_cell(_first_temp(entry, "indoor_f", "indoor_temp", "indoor"), unit)
        outdoor_cell = _fmt_temp_cell(_first_temp(entry, "outdoor_f", "outdoor_temp", "outdoor"), unit)

        # Flush run when type changes or type is not deduplicated
        if event_type in _NO_DEDUP or event_type != run_type:
            _flush_run()
            if event_type in _NO_DEDUP:
                rows.append((time_str, ev_text, settings_text, source, indoor_cell, outdoor_cell))
            else:
                # Start a new run; temps are from the first event in the run
                run_type = event_type
                run_count = 1
                run_first_time = time_str
                run_last_time = time_str
                run_ev_text = ev_text
                run_settings = settings_text
                run_source = source
                run_indoor = indoor_cell
                run_outdoor = outdoor_cell
        else:
            # Continue run -- update last time and settings (last setpoint wins); temps stay from first event
            run_count += 1
            run_last_time = time_str
            if settings_text:
                run_settings = settings_text

    _flush_run()

    if not rows:
        table = (
            "| Time | Event | Settings | Source | Indoor | Outdoor |\n"
            "|---|---|---|---|---|---|\n"
            "| -- | (no events in window) | | | | |"
        )
        return _maybe_prepend_whf_warning(table, config)

    # ---- format as markdown ----
    header = "| Time | Event | Settings | Source | Indoor | Outdoor |"
    sep = "|---|---|---|---|---|---|"
    ordered_rows = list(reversed(rows)) if newest_first else rows
    row_lines = [f"| {t} | {ev} | {st} | {src} | {ind} | {out} |" for t, ev, st, src, ind, out in ordered_rows]
    table = "\n".join([header, sep, *row_lines])

    return _maybe_prepend_whf_warning(table, config)


async def build_daily_summaries_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build a HISTORICAL DAILY SUMMARIES section for multi-day investigations
    (moved from ai_skills_activity.py, Issue #563).

    Wraps `_build_daily_summaries()`. Only meaningful once the requested window
    exceeds ~36 hours — for a same-day question the event log already covers it,
    so this stays empty below that threshold rather than adding noise.
    """
    hours = float(kwargs.get("hours", 24))
    if hours <= 36:
        return ""
    return "\n".join(_build_daily_summaries(coordinator, hours)) + "\n"


async def build_activity_timeline_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build a deterministic event timeline table for investigator context (Issue #563).

    Wraps `build_event_timeline_table()` — a markdown table of what happened,
    generated programmatically (never LLM-authored) — so both the silent/scheduled
    narration mode and the on-demand investigation mode of the merged skill can
    ground their narrative in an actual chronological record instead of re-deriving
    one from raw event-log counts.
    """
    hours = float(kwargs.get("hours", 24))
    hours = max(1.0, min(hours, 720.0))
    raw_event_log = list(getattr(coordinator, "_event_log", []) or [])
    config = getattr(coordinator, "config", {}) or {}
    table = build_event_timeline_table(raw_event_log, config, hours, dt_util.now())
    return f"=== ACTIVITY TIMELINE (last {hours:g}h) ===\n{table}\n"


async def build_state_cross_validation_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build a STATE CROSS-VALIDATION section (moved from ai_skills_activity.py, Issue #563).

    Two deterministic checks, pre-computed so the model cites them rather than
    re-deriving: (1) hvac_mode=off but hvac_action reports active — flagged unless
    it's the expected CA-fan-only-mode case (is_ca_fan_running() is the single
    source of truth here, Issue #458); (2) indoor temp vs. comfort band, using the
    thermostat's own swing/deadband so a within-deadband shortfall isn't flagged.
    """
    data: dict[str, Any] = coordinator.data or {}
    options: dict[str, Any] = coordinator.config or {}

    hvac_mode = data.get("hvac_mode") or "unknown"
    hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
    fan_status = data.get(ATTR_FAN_STATUS, "unknown")

    climate_entity_id: str = options.get("climate_entity", "")
    current_temp: Any = "unknown"
    if climate_entity_id:
        climate_state = hass.states.get(climate_entity_id) if hass is not None else None
        if climate_state is not None:
            current_temp = climate_state.attributes.get("current_temperature", "unknown")

    state_flags: list[str] = []
    active_actions = {"heating", "cooling", "fan"}
    if hvac_mode == "off" and str(hvac_action).lower() in active_actions:
        ca_fan_running = is_ca_fan_running(fan_status)
        if not (str(hvac_action).lower() == "fan" and ca_fan_running):
            state_flags.append(
                f"[WARNING] hvac_mode=off but hvac_action={hvac_action!r} -- "
                "possible stale coordinator data or thermostat reporting bug"
            )

    _swing_heat_f = THERMAL_SWING_DEFAULT_F
    _swing_cool_f = THERMAL_SWING_DEFAULT_F
    _temp_unit = options.get("temp_unit", "fahrenheit")
    learning = getattr(coordinator, "learning", None)
    if learning is not None and callable(getattr(learning, "get_thermal_model", None)):
        try:
            _build_health = getattr(coordinator, "_build_learning_health", None)
            _health = _build_health() if callable(_build_health) else {}
            _thermal = learning.get_thermal_model(learning_health=_health)
            _swing_heat_f = _thermal.get("swing_heat_f_display", THERMAL_SWING_DEFAULT_F)
            _swing_cool_f = _thermal.get("swing_cool_f_display", THERMAL_SWING_DEFAULT_F)
            if _temp_unit == "celsius":
                _swing_heat_f *= 5.0 / 9.0
                _swing_cool_f *= 5.0 / 9.0
        except Exception:
            pass

    try:
        ch = float(options.get("comfort_heat", "unknown"))
        cc = float(options.get("comfort_cool", "unknown"))
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

    lines = ["=== STATE CROSS-VALIDATION ===", *(state_flags if state_flags else ["  No contradictions detected."])]
    return "\n".join(lines) + "\n"


async def build_override_details_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build MANUAL OVERRIDES TODAY + FAN OWNERSHIP HISTORY (moved from
    ai_skills_activity.py, Issue #563).

    Includes the Issue #321 stuck-grace detection: manual_override_active=True but
    grace already expired without clearing — a critical system error the occupant
    experiences as "the HVAC won't return to automatic," pre-flagged as top priority
    rather than left for the model to notice from raw timestamps.
    """
    lines: list[str] = ["=== MANUAL OVERRIDES TODAY ==="]
    try:
        today_record = getattr(coordinator, "_today_record", None)
        override_count = 0
        override_details: list[dict] = []
        if today_record is not None:
            override_count = getattr(today_record, "manual_overrides", 0)
            override_details = list(getattr(today_record, "override_details", []) or [])

        lines.append(f"  Setpoint override count: {override_count}")
        if override_details:
            for i, d in enumerate(override_details, 1):
                t = d.get("time", "??:??")
                old_t = d.get("old_temp", "?")
                new_t = d.get("new_temp", "?")
                direction = d.get("direction", "?")
                magnitude = d.get("magnitude", "?")
                sign = "+" if direction == "up" else "-"
                lines.append(f"  #{i}  {t}  {old_t}F -> {new_t}F  ({sign}{magnitude}F, {direction})")
        else:
            lines.append("  (no setpoint overrides recorded today)")

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
                    lines.append(
                        f"  Current override:  active since {local_start.strftime('%H:%M')}, "
                        f"duration {duration_min} min (ongoing)"
                    )
                except Exception:
                    lines.append("  Current override:  active (duration unknown)")
            else:
                lines.append("  Current override:  active (start time unknown)")
        else:
            lines.append("  Current override:  none active")

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
                        lines.append(
                            "  WARNING STUCK GRACE DETECTED: manual_override_active=True but "
                            f"grace_end_time ({_ae_grace_end}) is in the past and no grace timer "
                            "is active. This is a critical system error -- the override should "
                            "have been cleared. Recommend flagging as top priority incongruity."
                        )
                except Exception:
                    pass
    except Exception:
        _LOGGER.warning("investigator: failed to build override detail section -- skipping")
        lines = ["=== MANUAL OVERRIDES TODAY ===", "  (unavailable)"]

    # --- Fan ownership history ---
    try:
        hours = float(kwargs.get("hours", 24))
        hours = max(1.0, min(hours, 720.0))
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        raw_event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        _own_ca = False
        _own_user = False
        fan_ownership_lines: list[str] = []
        _fan_override_count = 0
        for entry in raw_event_log[-200:]:
            if not isinstance(entry, dict):
                continue
            raw_time = entry.get("time")
            if raw_time is not None:
                if isinstance(raw_time, datetime.datetime):
                    _odt = raw_time
                    if _odt.tzinfo is None:
                        _odt = _odt.replace(tzinfo=datetime.UTC)
                else:
                    try:
                        _odt = datetime.datetime.fromisoformat(str(raw_time))
                        if _odt.tzinfo is None:
                            _odt = _odt.replace(tzinfo=datetime.UTC)
                    except ValueError:
                        _odt = None
                if _odt is not None and _odt < cutoff:
                    continue

            _etype = str(entry.get("type", "unknown"))
            _edata = {k: v for k, v in entry.items() if k not in ("time", "type")}
            _ts_str = _fmt_time(entry.get("time"))

            if _etype in ("nat_vent_fan_on", "fan_activated"):
                if not _own_ca:
                    _own_ca = True
                    _own_user = False
                    fan_ownership_lines.append(f"  {_ts_str}: CA owns fan ({_etype})")
            elif _etype == "fan_manual_override" and str(_edata.get("fan_after", "")).strip() == "on":
                if not _own_user:
                    _own_user = True
                    _own_ca = False
                    _fan_override_count += 1
                    fan_ownership_lines.append(f"  {_ts_str}: User owns fan (fan_manual_override, fan->on)")
            elif _etype == "fan_cancel":
                if _own_user:
                    _own_user = False
                    fan_ownership_lines.append(f"  {_ts_str}: Fan ownership cleared (fan_cancel)")
            elif _etype in ("nat_vent_fan_off", "fan_deactivated") and _own_ca:
                _own_ca = False
                fan_ownership_lines.append(f"  {_ts_str}: CA released fan ({_etype})")

        lines += [
            "",
            "=== FAN OWNERSHIP HISTORY ===",
            f"  Fan override count (window): {_fan_override_count}",
            *(fan_ownership_lines if fan_ownership_lines else ["  (no fan ownership transitions in window)"]),
        ]
    except Exception:
        _LOGGER.warning("investigator: failed to build fan ownership history -- skipping")

    return "\n".join(lines) + "\n"


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
        name="last_briefing",
        tags=frozenset({"system", "briefing"}),
        priority=1,
        builder=build_last_briefing_context,
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
        name="activity_timeline",
        tags=frozenset({"events", "system"}),
        priority=1,
        builder=build_activity_timeline_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="state_cross_validation",
        tags=frozenset({"system", "hvac"}),
        priority=0,
        builder=build_state_cross_validation_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="override_details",
        tags=frozenset({"events", "system"}),
        priority=1,
        builder=build_override_details_context,
    )
)
_PROVIDER_REGISTRY.register(
    ContextProvider(
        name="daily_summaries",
        tags=frozenset({"learning", "events"}),
        priority=2,
        builder=build_daily_summaries_context,
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
