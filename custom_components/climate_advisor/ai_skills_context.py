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

import bisect
import contextlib
import datetime
import functools
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    pass

from . import i18n, log_capture, zone_registry
from .ai_settings_sync import provider_of
from .code_words import (
    action_w,
    day_w,
    family_w,
    mode_w,
    occupancy_w,
    outcome_w,
    requested_by_w,
    source_w,
    trend_w,
)
from .const import (
    AI_PROVIDER_BUILTIN_CLAUDE,
    AI_SETTINGS_BACKUP_KEY,
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
    COMFORT_DEVIATION_FLOOR_F,
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    MAX_WEATHER_BIAS_APPLY_F,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENT_FAN_DECAY,
    OBS_TYPE_VENT_WINDOW_DECAY,
)
from .coordinator import _REJECTION_LOG_CAP
from .fan_status import is_ca_fan_running
from .hvac_action import is_hvac_action_active
from .record_text import record_en
from .remote_offset import remote_offset_lines, remote_offset_view_for
from .setpoint_combinations import vacation_edges
from .swing import swing_tolerance, thermostat_holds_direction, within_thermostat_hysteresis
from .temperature import (
    UNIT_SYMBOL,
    convert_delta,
    display_temp,
    format_temp,
    format_temp_delta,
    read_attribute_temp_f,
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

    def select(self, focus: str = "", narration: bool = False, deep: bool = False) -> list[ContextProvider]:
        """Return providers relevant to the given focus string, sorted by priority.

        If focus is empty or contains no recognised keywords, and `deep` is False,
        providers are capped to priority <= 1 (current-state + recent-activity) — the
        same narrowing narration already used, extended to on-demand investigations
        (Issue #920) to cut default noise. Pass `deep=True` for the old
        "run everything" default-investigation behavior (audit-depth and network-bound
        providers: daily summaries, operational design, known fixes, version, GitHub).

        narration=True is for the silent/scheduled narration path (never combined with
        a non-empty focus or deep=True in practice) — see Issue #563.

        Priority-0 providers are always included regardless of tag match —
        they provide the essential current-state context every investigation needs.
        """
        sorted_providers = sorted(self._providers, key=lambda p: p.priority)
        if narration:
            return [p for p in sorted_providers if p.priority <= 1]
        if not focus:
            return sorted_providers if deep else [p for p in sorted_providers if p.priority <= 1]
        focus_lower = focus.lower()
        tag_set: frozenset[str] = frozenset()
        for keyword, tags in FOCUS_TAG_MAP.items():
            if keyword in focus_lower:
                tag_set = tag_set | tags
        if not tag_set:
            # No recognised keyword — fall back to the same default-depth behavior as
            # an empty focus, rather than always running everything (Issue #920).
            return sorted_providers if deep else [p for p in sorted_providers if p.priority <= 1]
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
        "nat_vent_outdoor_rise_exit",
        "nat_vent_away_ceiling_exit",
        "ceiling_guard_fired",
        "warm_day_state_confirmed",
        "warm_day_setback_applied",
        "warm_day_comfort_gap",
        "occupancy_setback",
        "occupancy_comfort_restored",
        "occupancy_comfort_restore_held",
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


def _build_known_override_false_positives(events: list, hours: float, now: datetime.datetime) -> str:
    """Detect the Issue #205 false-override pattern deterministically.

    An `override_detected` event within 60 seconds of an automation-initiated
    event (`nat_vent_*`, `ceiling_guard_fired`, `classification_applied`,
    `grace_started` with source=automation) is a known code-path false positive —
    automation actions must never trigger override detection. Previously this was
    encoded as ~15 lines of prompt text the model had to re-derive from raw
    timestamps every run; this computes the match once, deterministically, so the
    model only has to cite the result instead of re-doing the arithmetic (and
    risking getting the 60-second window wrong).

    Issue #840: `events` is first filtered to `hours` of `now` via
    `filter_events_by_window()` (no size `limit` — only the time window is
    applied, so no in-window match can be silently dropped), then the nested
    O(n*m) scan is replaced with a sort-once + `bisect` lookup per override
    event, since this previously scanned the full unbounded event log against
    every automation event on every investigator context build.

    Returns a formatted string starting with '=== KNOWN OVERRIDE FALSE POSITIVES
    (Issue #205) ==='.
    """
    import datetime as _dt

    start = time.perf_counter()

    lines: list[str] = ["=== KNOWN OVERRIDE FALSE POSITIVES (Issue #205) ==="]

    filtered_events, _limited = filter_events_by_window(events, hours, now)

    resolved: list[tuple[_dt.datetime | None, dict]] = []
    for entry in filtered_events:
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

    automation_events_sorted = sorted(automation_events, key=lambda pair: pair[0])
    automation_times_sorted = [dt for dt, _e in automation_events_sorted]
    window_delta = _dt.timedelta(seconds=_OVERRIDE_FALSE_POSITIVE_WINDOW_S)

    matches: list[str] = []
    for evt_dt, _evt in override_events:
        lo = bisect.bisect_left(automation_times_sorted, evt_dt - window_delta)
        hi = bisect.bisect_right(automation_times_sorted, evt_dt + window_delta)
        for auto_dt, auto_evt in automation_events_sorted[lo:hi]:
            delta_s = abs((evt_dt - auto_dt).total_seconds())
            if delta_s <= _OVERRIDE_FALSE_POSITIVE_WINDOW_S:
                matches.append(
                    f"  override_detected at {fmt_day_stamp(evt_dt, seconds=True)} is {delta_s:.0f}s from"
                    f" {auto_evt.get('type')} at {fmt_clock_near(auto_dt, evt_dt, seconds=True)} — known false override"
                    " detection (Issue #205), not a genuine incongruity"
                )
                break

    lines.extend(matches if matches else ["  None detected in this window."])

    duration_ms = (time.perf_counter() - start) * 1000
    _LOGGER.debug(
        "Override false-positive scan complete override_count=%d automation_count=%d matches=%d hours=%s"
        " duration_ms=%.2f",
        len(override_events),
        len(automation_events),
        len(matches),
        hours,
        duration_ms,
    )
    return "\n".join(lines)


def _build_restart_summary(events: list) -> str:
    """Summarize `system_restarted` events by cause (Issue #563).

    `coordinator.py`'s restart-cause classification (Issue #403/#413) already
    distinguishes `user_restart`/`version_changed`/`reload` (benign — a normal
    restart, a deploy, or a zone reload, Issue #1110) from `unknown` (crash-like, worth flagging) and stamps it as the
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
        " user_restart (a normal restart), version_changed (a deploy) and reload (a Climate"
        " Advisor zone reload: settings applied or a repair) are expected,"
        " benign events — do not narrate them as problems or cite the raw restart count"
        " as if every restart were equally concerning."
    )
    if unknown_times:
        lines.append(f"  cause=unknown restart timestamps: {', '.join(unknown_times)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper functions (moved from ai_skills_investigator.py)
# ---------------------------------------------------------------------------


def _build_timing_correlations(events: list, hours: float, now: datetime.datetime) -> str:
    """Build a TIMING CORRELATIONS section for the investigator context.

    Scans the event log for manual events that occur within ±2 minutes of a
    known automation interval after an automation event. These coincidences
    suggest the "manual" event may actually be automation-caused.

    Issue #840: `events` is first filtered to `hours` of `now` via
    `filter_events_by_window()` (no size `limit` — only the time window is
    applied, so no in-window candidate can be silently dropped), then the
    per-manual-event `prior_auto = [...]` list-comprehension + `max()` scan
    (O(n*m) against the full unbounded log) is replaced with a sort-once +
    `bisect` lookup for the nearest prior automation event.

    Returns a formatted string starting with '=== TIMING CORRELATIONS ==='.
    """
    import datetime as _dt

    start = time.perf_counter()

    lines: list[str] = ["=== TIMING CORRELATIONS ==="]
    if not events:
        lines.append("  (no events to correlate)")
        duration_ms = (time.perf_counter() - start) * 1000
        _LOGGER.debug(
            "Timing correlation scan complete manual_count=%d automation_count=%d matches=%d hours=%s duration_ms=%.2f",
            0,
            0,
            0,
            hours,
            duration_ms,
        )
        return "\n".join(lines)

    filtered_events, _limited = filter_events_by_window(events, hours, now)

    # Resolve timestamps to UTC datetime objects
    resolved: list[tuple] = []  # (dt | None, event_dict)
    for entry in filtered_events:
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

    # Collect automation events (with parseable timestamps), sorted once so the
    # nearest-prior lookup below can bisect instead of rescanning per manual event.
    auto_events_sorted = sorted(
        (
            (dt, e)
            for dt, e in resolved
            if dt is not None
            and (e.get("source") in ("automation",) or str(e.get("type", "")) in _TIMING_AUTO_EVENT_TYPES)
        ),
        key=lambda pair: pair[0],
    )
    auto_times_sorted = [dt for dt, _e in auto_events_sorted]

    # Check each manual event against the nearest prior automation event
    found_any = False
    manual_count = 0
    match_count = 0
    for evt_dt, evt in resolved:
        etype = str(evt.get("type", ""))
        is_manual = evt.get("source") == "manual" or etype in _TIMING_MANUAL_EVENT_TYPES
        if not is_manual or evt_dt is None:
            continue
        manual_count += 1

        # bisect_left(evt_dt) gives the index of the first automation event
        # that is NOT strictly before evt_dt — everything before that index
        # satisfies adt < evt_dt, and the entry immediately before it is the
        # nearest prior automation event (mirrors the old max(prior_auto)).
        idx = bisect.bisect_left(auto_times_sorted, evt_dt)
        if idx == 0:
            lines.append(f"  [OK] {fmt_day_stamp(evt_dt)} — {etype}: no prior automation event in window")
            found_any = True
            continue

        nearest_adt, nearest_ae = auto_events_sorted[idx - 1]
        delta_s = (evt_dt - nearest_adt).total_seconds()
        # Issue #1674: the window spans days, so each line names its day (and the prior event its own day when it
        # falls before midnight); both in local time (the stored zone was printed before).
        time_str = fmt_day_stamp(evt_dt)
        prior_type = str(nearest_ae.get("type", "?"))
        prior_time_str = fmt_clock_near(nearest_adt, evt_dt)

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
            match_count += 1
        else:
            lines.append(
                f"  [OK] {time_str} — {etype}: no matching automation interval (delta={delta_s:.0f}s from {prior_type})"
            )
        found_any = True

    if not found_any:
        lines.append("  (no manual events in window)")

    duration_ms = (time.perf_counter() - start) * 1000
    _LOGGER.debug(
        "Timing correlation scan complete manual_count=%d automation_count=%d matches=%d hours=%s duration_ms=%.2f",
        manual_count,
        len(auto_events_sorted),
        match_count,
        hours,
        duration_ms,
    )
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
    lines.append(_engine_line("k_vent_fan", "k_vent_fan", " hr^-1"))

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
                             typical after HA restart, or a short blower run-on transient
  running (thermostat)      — the thermostat's own schedule/circulation blower (hvac_action=fan, fan_mode
                             attr auto) that CA did not command and does not own; shown instead of
                             'running (untracked)' (see fan_observed_started/stopped events)
  off (manual override)     — _fan_override_active=True AND _fan_active=False; user turned
                             the fan on at the thermostat (setting _fan_override_active=True),
                             then turned it off before the grace period expired. The override
                             is still in effect (grace period not yet cleared), physical fan is off.
  disabled                  — fan control feature is turned off in configuration

Heating/cooling deadband (thermostat behavior — not a CA fault):
  Thermostats have a built-in deadband. Heating fires when indoor drops ~1-2°F
  below the setpoint (the learned heat swing: Climate Advisor measures it at each real
  heat start) and runs until near or slightly past the setpoint. If CA commanded heat mode
  at comfort_heat=68°F and indoor=67°F, the thermostat reporting hvac_action=idle
  or hvac_action=fan is expected deadband behavior — not a CA failure.

Comfort band on warm and mild days (Issues #249 / #1208 / #1269):
  A warm or mild classification carries hvac_mode "off". That is a day-plan label (no heating or
  cooling is expected to be needed), NOT a command: CA does not turn the thermostat off for it.
  While the home is occupied and awake, CA arms the comfort band every cycle via
  select_comfort_band (event: comfort_band_applied, with the mode and setpoint it sent and its
  outcome). The thermostat runs single-setpoint, so the nearest-edge selector picks heat (at the
  floor) or cool (at the ceiling) depending on which edge indoor is closer to -- a heat or cool
  start inside the comfort band on a warm or mild day is expected, not an anomaly.
  Away/vacation arm the away/vacation setback band and the sleep window arms the sleep band, the
  same way. The thermostat is Off only when something else holds it there: an open window
  (door/window pause, Rule 3), a whole-house-fan session, blower ventilation with a window open,
  or the occupant's own change.
  warm_day_comfort_gap, warm_day_state_confirmed and warm_day_setback_applied are legacy events
  (before #249): they appear only in old logs and nothing emits them today.

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


def _moisture_lines(coordinator: Any) -> list[str]:
    """CURRENT STATE lines for moisture (Issue #1696): the indoor and outdoor decision dew point with its relative
    humidity, the humid-air guard, and today's decisions it changed (window advice withdrawn, notices sent, fan starts
    held). Temperatures in the user's unit. Empty when the coordinator has no humidity facts (older stubs)."""
    display = getattr(coordinator, "humidity_display", None)
    if not callable(display):
        return []
    try:
        shown = display()
        counts_fn = getattr(coordinator, "humid_decisions_today", None)
        counts = counts_fn() if callable(counts_fn) else {}
        unit = (getattr(coordinator, "config", None) or {}).get("temp_unit", "fahrenheit")
    except Exception:  # noqa: BLE001 - the context goes out without these lines rather than fail
        return []
    if not isinstance(shown, dict):
        return []
    counts = counts if isinstance(counts, dict) else {}

    def _reading(sig: dict[str, Any]) -> str:
        dp = sig.get("dp_f")
        if not isinstance(dp, (int, float)):
            return "unknown"
        rh = sig.get("rh")
        rh_text = f" (RH {rh:.0f}%)" if isinstance(rh, (int, float)) else ""
        return f"{format_temp(dp, unit, 1)}{rh_text}"

    guard = shown.get("guard") or {}
    ceiling = guard.get("ceiling_f")
    if not isinstance(ceiling, (int, float)):
        guard_text = "off"
    elif guard.get("blocks"):
        guard_text = (
            f"blocking ({guard.get('reason')}; ceiling {format_temp(ceiling, unit, 1)}): open-window advice withdrawn,"
            " planned window period ended, no new fan / nat-vent starts"
        )
    else:
        guard_text = f"clear (ceiling {format_temp(ceiling, unit, 1)})"
    return [
        f"  indoor_dew_point:    {_reading(shown.get('indoor') or {})}",
        f"  outdoor_dew_point:   {_reading(shown.get('outdoor') or {})}",
        f"  humid_air_guard:     {guard_text}",
        f"  humid_decisions_today: close-window notices={int(counts.get('close_notices', 0))}"
        f" fan starts held={int(counts.get('fan_starts_held', 0))}",
    ]


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
        moisture = _moisture_lines(coordinator)

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
            *moisture,
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
        ]
        # Issue #1194 (d): the remote-sensor outer loop, so an investigation can see WHY the thermostat sits at
        # target + offset (loop state and reason, comfort target vs commanded setpoint, the remote reading's age).
        _ro_view = remote_offset_view_for(
            getattr(coordinator, "automation_engine", None), (coordinator.config or {}).get("temp_unit", "fahrenheit")
        )
        if _ro_view is not None:
            lines.append("  remote_sensor_offset:")
            lines.extend(remote_offset_lines(_ro_view))
        lines.append("")
        return "\n".join(lines)
    except Exception:
        _LOGGER.warning("investigator: failed to read HVAC entity state — skipping")
        return "=== HVAC ENTITY ===\n  unavailable\n"


def _swing_context_lines(thermal: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Swing lines for the investigator: what it means, how it was learned, what was thrown away (Issue #1233)."""
    unit = config.get("temp_unit", "fahrenheit") if isinstance(config, dict) else "fahrenheit"
    dbg = thermal.get("swing_debug") or {}
    # Issue #1477: the swing is a half degree F; one decimal in Celsius would print 1.5 F as 0.8 C (= 1.44 F)
    dec = 2 if unit == "celsius" else 1
    lines = ["", f"  Thermostat swing — {thermal.get('swing_definition') or dbg.get('definition') or ''}"]
    for mode, side in (("heat", "below"), ("cool", "above")):
        value = thermal.get(f"swing_{mode}_f")
        count = thermal.get(f"observation_count_swing_{mode}", 0)
        if value is None:
            lines.append(f"  {mode} swing: not measured yet ({count} accepted cycles; the default is only an estimate)")
        else:
            res = thermal.get(f"swing_{mode}_resolution_f") or 0.0
            res_txt = f", readings quantised to {format_temp_delta(res, unit, 1)}" if res else ""
            raw = thermal.get(f"swing_{mode}_raw_f")
            raw_txt = f"median {format_temp_delta(raw, unit, 2)} rounded up to the next half degree F, " if raw else ""
            lines.append(
                f"  {mode} swing: {format_temp_delta(value, unit, dec)} {side} setpoint"
                f" ({raw_txt}from the finest readings of the last accepted cycles; {count} accepted in total{res_txt})"
            )
        block = dbg.get(mode) or {}
        counts = block.get("reject_counts") or {}
        if counts:
            lines.append(
                "    rejected cycles (not hysteresis): "
                + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
            )
        recent = block.get("recent") or []
        if recent:
            lines.append(
                "    recent cycles: "
                + "; ".join(
                    (
                        f"accepted {format_temp_delta(r['value_f'], unit, 1)}"
                        if r.get("accepted")
                        else f"rejected {r.get('reason')}"
                    )
                    for r in recent
                )
            )
        coast = thermal.get(f"coast_{mode}_f")
        if coast is not None:
            lines.append(
                f"    {mode} coast (kept apart from the swing): the room goes {format_temp_delta(coast, unit, 1)} "
                f"further after the equipment stops ({thermal.get(f'observation_count_coast_{mode}', 0)} windows)"
            )
    return lines


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
                "  avg_daily_thermostat_fan_only_runtime_minutes: "
                f"{compliance.get('avg_daily_thermostat_fan_only_runtime_minutes', 'unknown')}",
                f"  comfort_score:                  {compliance.get('comfort_score', 'unknown')}",
                "  NOTE — comfort_score: share of time indoor stayed inside the target range IN EFFECT",
                "  at that moment (the chart's target band: sleep/away setbacks included), not the static",
                "  awake comfort range. A planned setback is not a violation (Issue #1107, 0.8.1+;",
                "  older days in the 14-day window were scored against the static range).",
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
                "  NOTE — these are the evidence behind the heating/cooling rates (the confidence-",
                "  grading input), not cumulative counters: the pre-upgrade count, which gives way",
                "  only as newer informative cycles replace or contradict it (never with time alone),",
                "  plus the cycle model's weighted informative cycles (Issue #1512). They can fall",
                "  when new cycles arrive and stay unchanged through a quiet season. They are not the",
                "  live committed/rejected counts shown in THERMAL OBSERVATION PIPELINE below, which count",
                "  the observations still kept (capped per type, none leaves with time); a gap between them",
                "  is expected and does not indicate lost data (Issues #586, #1523).",
                *_swing_context_lines(thermal, getattr(coordinator, "config", None) or {}),
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
                        fan_only_runtime = rec.get("thermostat_fan_only_runtime_minutes", "?")
                        overrides = rec.get("manual_overrides", "?")
                        section_lines.append(
                            f"  {date_val}: opened={opened} window_rec={compliance_val}"
                            f" runtime={runtime}min fan_only_runtime={fan_only_runtime}min overrides={overrides}"
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
        OBS_TYPE_VENT_WINDOW_DECAY,
        OBS_TYPE_VENT_FAN_DECAY,
        OBS_TYPE_SOLAR_GAIN,
    ]

    # Reason codes that indicate the observation was interrupted by normal system operation.
    # Issues #1512/#1516: a new session, fan start or mode switch ends a run early by design.
    _OPERATIONAL_CODES = {
        "abandoned",
        "indoor_unavailable",
        "new_session_started",
        "fan_activated",
        "hvac_mode_switch",
        "hvac_unreported",  # Issue #1536: a heating/cooling mode with no run reported, excluded on purpose
    }

    # Reason codes that indicate a signal quality problem worth flagging.
    # Issues #1512/#1516: the cycle model's codes say why a heating or cooling cycle taught the rate model
    # nothing (too short, room moved less than the sensor can show, setpoint changed, a restart or an unavailable
    # thermostat cut it, a window or fan was involved). The two ``hvac_*`` run codes are HISTORICAL: the per-run
    # estimator that wrote them is retired, but old entries stay in the stored history until they age out.
    _QUALITY_FAILURE_CODES = {
        "too_few_samples",
        "too_few_blocks",
        "small_delta",
        "ols_bad_fit",
        "ols_wrong_sign",
        "ols_bounds",
        "window_too_short",
        "no_interior_peak",
        "cycle_too_short",
        "cycle_signal_below_resolution",
        "cycle_excluded_window_fan",
        "cycle_setpoint_change",
        "cycle_gap_restart",
        "cycle_thermostat_unavailable",
        "cycle_no_off_edge",
        "cycle_sensor_swap",
        "hvac_run_too_short",  # historical (retired per-run estimator)
        "hvac_signal_below_resolution",  # historical (retired per-run estimator)
        "no_k_passive",  # historical
    }

    lines.append("Per-type rejection summary:")
    lines.append(
        "  (committed = still kept, capped per type, none leaves with time (#1523); rejected is capped at"
        f" {_REJECTION_LOG_CAP} per type — compare against observation_count_heat/cool in LEARNING —"
        " THERMAL MODEL above only as a floor, not an exact match; Issue #586)"
    )
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

        rejected_display = (
            f"{_REJECTION_LOG_CAP}+ (capped)" if total_rejected >= _REJECTION_LOG_CAP else str(total_rejected)
        )
        lines.append(f"  {obs_type}: {committed} committed, {rejected_display} rejected{suffix}")
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

    # Issues #1512/#1516: say how heating/cooling rates are learned so the AI does not assume a per-run or
    # post-run fit, and does not over-trust a rate learned from whole-degree short cycles.
    lines.append(
        "How heat/cool rates are learned (the cycle model): every heating or cooling cycle is recorded with the"
        " room readings around it, and all recent cycles are fitted together (the rate, the leak and the coast"
        " after the equipment stops) so the room's drift after a run is modelled, not mistaken for the rate."
        " It uses the home's typical-house default or its stored rate as a starting guess, pulled by how much"
        " evidence there is, so it is never worse than the default and improves with each informative cycle."
        " A cycle only counts when the room moved by at least three sensor steps; on a whole-degree sensor"
        " short cycles cannot identify the rate, so the model says so (grade low, never high) instead of"
        " guessing. Old cycles give way only to newer ones, weighed by what they tell: a cycle's weight halves"
        " once the newer cycles carry 20 newer cycles' worth of its own information (faster when they contradict"
        " it), never with time alone, so a long accurate run outlives short pulses, a quiet season changes"
        " nothing and there is no reset. The sensor's delay is fitted out, the grade is held down when the rate"
        " rests on a guessed leak rate, and a blower run before or after the heating/cooling is never a run of"
        " its own (the coast is tracked through it)."
        " The closed-house leak rate comes only from closed-up stretches (passive windows); the open-window"
        " rate never stands in for it. On a v4 zone decisions use v4's own control rate; the cycle model's"
        " measurement is shown beside it."
    )
    try:
        crow = [
            r
            for r in (
                coordinator.decision_rates_summary()
                if callable(getattr(coordinator, "decision_rates_summary", None))
                else []
            )
            if isinstance(r, dict) and r.get("source") in ("cycle", "notice")
        ]
        # rows carry q / sd_q in the display unit per hour and coast_area_f as a degF delta (_cycle_model_rows)
        _unit = (getattr(coordinator, "config", None) or {}).get("temp_unit", "fahrenheit")
        _sym = UNIT_SYMBOL.get(_unit, "°F")
        for r in crow:
            if r.get("source") == "notice":  # owner decision 2026-11-13: no heating/cooling reports at all
                lines.append(f"  thermostat: {r.get('text')} (rate in use: {r.get('rate_in_use')})")
                continue
            val = r.get("value") if isinstance(r.get("value"), dict) else r
            mode = r.get("mode") or ("cool" if "cool" in str(r.get("key", "")) else "heat")
            q, grade = val.get("q"), val.get("grade")
            if not isinstance(q, (int, float)) or isinstance(q, bool):
                continue
            sd, area = val.get("sd_q"), val.get("coast_area_f")
            parts = [f"{mode} rate {q:.2f}"]
            if isinstance(sd, (int, float)) and not isinstance(sd, bool):
                parts[0] += f" +/- {sd:.2f}"
            parts[0] += f" {_sym}/h"
            parts.append(f"grade {grade or 'none'}")
            if isinstance(area, (int, float)) and not isinstance(area, bool):
                parts.append(f"coast after a run {convert_delta(area, _unit):+.2f} {_sym}")
            if val.get("ne") is not None:
                parts.append(f"{val.get('ne')} informative cycles")
            if isinstance(val.get("lag_min"), (int, float)) and not isinstance(val.get("lag_min"), bool):
                parts.append(
                    f"sensor lag {val['lag_min']:.1f} min" + ("" if val.get("lag_identified") else " (not identified)")
                )
            if val.get("grade_caps"):
                parts.append("grade held down by " + "/".join(str(x) for x in val["grade_caps"]))
            lines.append("  cycle model: " + ", ".join(parts))
            notice = val.get("coarse_sensor")
            if isinstance(notice, dict) and notice.get("text"):
                # Issue #1512 reopened: a whole/half-degree sensor cannot teach the rate from normal cycles
                lines.append(
                    f"  cycle model {mode} sensor: {notice['text']} (rate in use: {notice.get('rate_in_use')})"
                )
    except Exception:  # noqa: BLE001 - context building never fails the investigator
        pass
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
        # Issue #1340: this used to recommend a developer-only repo script (tools/thermal_replay.py) that a HACS install
        # does not have. The fit is automatic, so the note says what to expect and that there is nothing to run.
        lines.append(
            "  NOTE: 0 chart_log observations. Climate Advisor fits these on its own from its chart log: once over"
            " the last 30 days at first start-up, then each time a night-time period with the house closed and the"
            " heat, AC and fan off ends (daytime periods are skipped because of the sun). 0 is expected on a new"
            " install, or while the HVAC runs constantly or windows stay open, until such periods are recorded."
            " There is no manual step for the user: do not recommend running a script or a backfill."
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

    # Issue #1529: the primary prediction engine's refits came back unusable while its learned model stays in use
    try:
        fn = getattr(coordinator, "prediction_refit_note", None)
        note = fn() if callable(fn) else ""
        if note:
            lines.append(f"Prediction model: {note}")
    except Exception:  # noqa: BLE001 - context building never fails the investigator
        pass

    # Issue #1505: on a v4 zone the timeline and decisions read v4's own rates (window and fan included), not the v3
    # learner's above; say which value is in use and where it comes from.
    try:
        rows = (
            coordinator.decision_rates_summary()
            if callable(getattr(coordinator, "decision_rates_summary", None))
            else []
        )
        vent = [r for r in rows if r.get("key") in ("k_passive", "k_vent_window", "k_vent_fan")]
        if vent:
            lines.append("Rates in use (decisions and timeline):")
            for r in vent:
                v = r.get("value")
                v_str = f"{v:.4f} hr^-1" if isinstance(v, (int, float)) and not isinstance(v, bool) else "not learned"
                lines.append(f"  {r.get('key')}: {v_str} (source {r.get('source')})")
    except Exception:  # noqa: BLE001 - context building never fails the investigator
        pass

    lines.append("")
    return "\n".join(lines)


def filter_events_by_window(
    event_log: list[Any], hours: float, now: datetime.datetime, *, limit: int | None = None
) -> tuple[list[dict], bool]:
    """Filter `event_log` entries to those within `hours` of `now`, in original order.

    Filtering happens BEFORE any size limit is applied — this is the fix for
    Issue #432, where three call sites previously sliced the raw log to its last
    200 entries and only then filtered by time window. When recent event volume
    exceeds the slice size, that ordering silently drops older-but-still-in-window
    events before the time filter ever sees them, so a requested N-hour window can
    return a small, misleading fraction of what actually happened.

    Non-dict entries are skipped. An entry with no parseable "time" field is kept
    (fail-open, matching prior behavior at all three call sites) unless dropped by
    a subsequent `limit` trim.

    If `limit` is given and the filtered result exceeds it, the returned list is
    trimmed to the most recent `limit` entries (chronological order preserved) and
    `limited` is True — this signals the result was budget-trimmed by `limit`,
    distinct from the underlying log simply not holding that much history in the
    requested window.

    Returns:
        (filtered_entries, limited) — filtered_entries is always in the original
        (chronological) order of `event_log`; limited is True iff `limit` was
        given and the time-filtered result exceeded it.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    cutoff = now - datetime.timedelta(hours=hours)

    filtered: list[dict] = []
    for entry in event_log:
        if not isinstance(entry, dict):
            continue
        raw_time = entry.get("time")
        if raw_time is None:
            filtered.append(entry)
            continue
        # Accept datetime objects or ISO strings
        if isinstance(raw_time, datetime.datetime):
            event_dt: datetime.datetime | None = raw_time
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=datetime.UTC)
        else:
            try:
                event_dt = datetime.datetime.fromisoformat(str(raw_time))
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=datetime.UTC)
            except ValueError, TypeError:
                event_dt = None
                filtered.append(entry)
                continue
        if event_dt >= cutoff:
            filtered.append(entry)

    limited = False
    if limit is not None and len(filtered) > limit:
        filtered = filtered[-limit:]
        limited = True

    return filtered, limited


async def build_event_log_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build EVENT LOG, TIMING CORRELATIONS, and KNOWN OVERRIDE FALSE POSITIVES sections."""
    hours: int = min(max(int(kwargs.get("hours", 168)), 1), 720)
    now_utc = datetime.datetime.now(datetime.UTC)

    event_section_lines: list[str] = []
    timing_section: str = ""
    false_positives_section: str = ""
    restart_section: str = ""

    # --- Event log ---
    try:
        event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        recent_events, limited = filter_events_by_window(event_log, hours, now_utc, limit=200)

        # Count by type
        type_counts: dict[str, int] = {}
        for entry in recent_events:
            etype = str(entry.get("type", "unknown"))
            type_counts[etype] = type_counts.get(etype, 0) + 1

        event_section_lines += [
            f"=== EVENT LOG (last {hours}h, {len(recent_events)} events) ===",
            f"  event_type_counts: {type_counts}",
        ]
        if limited:
            event_section_lines.append("  NOTE: window contains more than 200 events — showing the most recent 200")
        event_section_lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to read event log — skipping")
        event_section_lines += ["=== EVENT LOG ===", "  unavailable", ""]

    # --- Real captured log records (Issue #578 — see log_capture.py) ---
    # Issue #812: the ring buffer is shared across every zone (config entry) —
    # each record carries a "zone" field (the coordinator's zone_label, or
    # None if captured outside any log_capture.zone_scope()). Filter to the
    # zone actually being investigated so zone B's warnings never show up in
    # zone A's report; a record with no zone tag is shown explicitly labeled
    # as unknown rather than silently included or excluded, since it cannot
    # be confidently attributed to either zone.
    try:
        handler = log_capture.get_handler(hass) if hass is not None else None
        records = handler.get_records() if handler is not None else []
        cutoff_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
        this_zone_label = getattr(coordinator, "zone_label", None)
        recent_records: list[dict[str, Any]] = []
        for r in records:
            try:
                record_dt = datetime.datetime.fromisoformat(str(r.get("time"))).astimezone(datetime.UTC)
            except ValueError:
                recent_records.append(r)
                continue
            if record_dt >= cutoff_dt:
                recent_records.append(r)
        # Zone-scope the window: keep records tagged for this zone plus any
        # untagged ("unknown zone") record — drop records confidently
        # attributed to a *different* zone.
        zone_filtered_records = [r for r in recent_records if r.get("zone") in (this_zone_label, None)]
        # Issue #976: this_zone_label/record["zone"] stay entry_id-keyed for the filtering
        # above (identity, must be unique) — resolve_zone_display_name() is used only for
        # what a human actually reads, never for the comparison above it.
        display_zone_label = zone_registry.resolve_zone_display_name(hass, this_zone_label)
        event_section_lines += [
            f"=== SYSTEM LOG RECORDS (WARNING+, last {hours}h, {len(zone_filtered_records)} records, "
            f"zone={display_zone_label or 'unknown'}) ===",
        ]
        if zone_filtered_records:
            for r in zone_filtered_records:
                local_time = _fmt_time(r.get("time"))
                record_zone = r.get("zone")
                zone_tag = (
                    zone_registry.resolve_zone_display_name(hass, record_zone)
                    if record_zone is not None
                    else "unknown zone"
                )
                event_section_lines.append(
                    f"  {local_time} {r['level']} [{r['logger_name']}] (zone={zone_tag}) {r['message']}"
                )
        else:
            event_section_lines.append("  (none captured in window)")
        event_section_lines.append("")
    except Exception:
        _LOGGER.warning("investigator: failed to read captured log records — skipping")
        event_section_lines += ["=== SYSTEM LOG RECORDS ===", "  unavailable", ""]

    # --- Timing correlations ---
    try:
        raw_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        timing_section = _build_timing_correlations(raw_log, hours, now_utc)
    except Exception:
        _LOGGER.warning("investigator: failed to build timing correlations -- skipping")
        timing_section = "=== TIMING CORRELATIONS ===\n  unavailable"

    # --- Known override false positives (Issue #205) ---
    try:
        raw_log_fp: list[Any] = getattr(coordinator, "_event_log", []) or []
        false_positives_section = _build_known_override_false_positives(raw_log_fp, hours, now_utc)
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
        cfg.pop(AI_SETTINGS_BACKUP_KEY, None)  # Issue #1336: the backup can hold a key too
        _provider = provider_of(cfg)

        # Issue #1048: config temperatures are internal degF; show them in the user's unit.
        _cfg_unit = cfg.get("temp_unit", "fahrenheit")

        def _cfg_temp(key: str) -> Any:
            v = cfg.get(key, "unknown")
            return f"{display_temp(v, _cfg_unit):g}" if isinstance(v, (int, float)) else v

        _comfort_heat = _cfg_temp("comfort_heat")
        _comfort_cool = _cfg_temp("comfort_cool")
        _vac_heat_f, _vac_cool_f = vacation_edges(cfg)
        _vac_heat = round(display_temp(_vac_heat_f, _cfg_unit), 1)
        _vac_cool = round(display_temp(_vac_cool_f, _cfg_unit), 1)
        _vac_configured = cfg.get("vacation_heat") is not None or cfg.get("vacation_cool") is not None
        lines = [
            "=== CONFIGURATION ===",
            f"  comfort_heat (lower bound): {_comfort_heat} — indoor must be >= this to be in comfort band",
            f"  comfort_cool (upper bound): {_comfort_cool} — indoor must be <= this to be in comfort band",
            f"  comfort_band: [{_comfort_heat}, {_comfort_cool}]{UNIT_SYMBOL.get(_cfg_unit, '°F')}"
            " — temperature T is in-band only if comfort_heat <= T <= comfort_cool",
            f"  setback_heat:    {_cfg_temp('setback_heat')}",
            f"  setback_cool:    {_cfg_temp('setback_cool')}",
            # Issue #1347: the vacation band the engine arms (optional setpoints, else away -/+ 3 °F).
            f"  vacation_band:   [{_vac_heat:g}, {_vac_cool:g}]"
            f" ({'configured' if _vac_configured else 'away -/+ 3°F'})",
            f"  wake_time:       {cfg.get('wake_time', 'unknown')}",
            f"  sleep_time:      {cfg.get('sleep_time', 'unknown')}",
            f"  briefing_time:   {cfg.get('briefing_time', 'unknown')}",
            f"  ai_enabled:      {cfg.get('ai_enabled', 'unknown')}",
            # Issue #1336: the provider, and the model only where Climate Advisor chooses it (built-in Claude)
            f"  ai_provider:     {_provider}",
            *(
                [f"  ai_model:        {cfg.get('ai_model', 'unknown')}"]
                if _provider == AI_PROVIDER_BUILTIN_CLAUDE
                else [f"  ai_task_entity:  {cfg.get('ai_task_entity') or 'preferred'}"]
            ),
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


_KNOWN_FIXES_RECENT_COUNT = 15  # bound by count, same pattern as GITHUB_ISSUES_LIMIT


async def build_known_fixes_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build KNOWN-FIXED ISSUES section, bounded to a relevant-and-recent subset.

    Since Issue #702, this reads `fix_history.py` (a streaming search over
    `fix_history.jsonl`) instead of the old `KNOWN_FIXES`/`RELEASE_NOTES` dicts that
    used to live in const.py. Not-yet-deployed fixes always surface; the rest are
    ranked by relevance to the investigation's `focus` when one is given, falling back
    to recency otherwise — a strict improvement over the old recency-only selection,
    which could miss the actually-relevant older fix for the user's specific symptom.

    Each entry renders its `user_summary` (occupant-outcome language) when present,
    falling back to `title` (internal engineering detail) for records that never had
    a user-facing summary — not every historical change was user-visible.
    """
    from .const import VERSION  # noqa: PLC0415
    from .fix_history import async_search_fix_history, parse_version  # noqa: PLC0415

    focus = kwargs.get("focus", "")
    current_tuple = parse_version(VERSION)
    relevant = await async_search_fix_history(
        hass, query=focus, limit=_KNOWN_FIXES_RECENT_COUNT, current_version=current_tuple
    )

    if not relevant:
        return ""

    lines = [
        f"## KNOWN-FIXED ISSUES ({len(relevant)} relevant/recent entries) (scope-bounded — use for cross-check, step 8)"
    ]
    for fix in sorted(relevant, key=lambda r: r.get("issue", 0), reverse=True):
        issue_num = fix.get("issue")
        version_fixed = fix.get("version_fixed", "")
        summary = fix.get("user_summary") or fix.get("title", "")
        lines.append(f"\nIssue #{issue_num} — fixed in v{version_fixed}: {summary}")
    lines.append("")
    return "\n".join(lines)


async def build_version_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build version/release notes section for investigator context."""
    from .const import VERSION  # noqa: PLC0415
    from .fix_history import async_recent_release_notes  # noqa: PLC0415

    release_notes = await async_recent_release_notes(hass)

    lines = [f"## RUNNING VERSION\n{VERSION}\n"]
    lines.append("## RECENT RELEASE NOTES")
    for ver, notes in release_notes.items():
        lines.append(f"\n### v{ver}")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


def _client_session(hass: Any) -> Any:
    """Home Assistant's shared aiohttp session (the same accessor api.py uses).

    Issue #1650: this used ``hass.helpers.aiohttp_client.async_get_clientsession()``. Home Assistant removed
    ``hass.helpers`` before 2026.3, Climate Advisor's minimum, so the call raised. The except below swallowed it,
    and the AI Investigator never saw the project's open or closed issues.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return async_get_clientsession(hass)


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
        session = _client_session(hass)
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
    except TypeError, ValueError:
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
        _ds_unit = (getattr(coordinator, "config", None) or {}).get("temp_unit", "fahrenheit")
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
        col_hdr = "  Date       | DayType | HVAC(min) | FanOnly(min) | Overrides | Viol(min) | AvgIndoor | ObsHigh/Low"
        sep = "  -----------|---------|-----------|--------------|-----------|-----------|-----------|------------"
        rows = []
        for r in sorted(past, key=lambda x: x.get("date", "")):
            date = r.get("date", "?")
            day_type = str(r.get("day_type", "?"))[:7]
            hvac_min = int(r.get("hvac_runtime_minutes", 0) or 0)
            fan_only_min = int(r.get("thermostat_fan_only_runtime_minutes", 0) or 0)
            overrides = int(r.get("manual_overrides", 0) or 0)
            viol_min = int(r.get("comfort_violations_minutes", 0) or 0)
            avg_in = r.get("avg_indoor_temp")
            avg_in_str = format_temp(avg_in, _ds_unit, 1) if isinstance(avg_in, (int, float)) else "n/a"
            obs_high = r.get("observed_high_f")
            obs_low = r.get("observed_low_f")
            hl_str = (
                f"{format_temp(obs_high, _ds_unit)}/{format_temp(obs_low, _ds_unit)}"
                if isinstance(obs_high, (int, float)) and isinstance(obs_low, (int, float))
                else "n/a"
            )
            row = (
                f"  {date} | {day_type:<7} | {hvac_min:<9} | {fan_only_min:<12}"
                f" | {overrides:<9} | {viol_min:<9} | {avg_in_str:<9} | {hl_str}"
            )
            rows.append(row)

        note = "  Note: event log ring buffer covers ~50-60h; use daily summaries for context beyond that."
        viol_note = (
            "  Note: Viol(min) = minutes indoor was outside the target range in effect at the time"
            " (sleep/away setbacks included; days before 0.8.1 used the static comfort range)."
        )
        return ["", header, col_hdr, sep, *rows, note, viol_note]
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
        "occupancy_comfort_restore_held",
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

_SYSTEM_EVENT_TYPES: frozenset[str] = frozenset({"system_restarted", "entity_unavailable", "entity_recovered"})


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


def _parse_event_time(raw_time: Any) -> datetime.datetime | None:
    """Parse a raw event-log timestamp into a local, timezone-aware datetime.

    Single source of truth for timestamp parsing (Issue #925) -- shared by
    `_fmt_time()` (display string) and `_render_timeline_events()` (real datetime,
    needed for gap-based session grouping in `_group_timeline_sessions()`). Returns
    None if `raw_time` is missing or unparseable.
    """
    if raw_time is None:
        return None
    if isinstance(raw_time, datetime.datetime):
        dt = raw_time
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(raw_time))
        except ValueError, TypeError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt_util.as_local(dt)


# Issue #1674: every multi-day section of the investigator context stamps a time with its weekday and date
# ("Sat 11-21 10:31"). A bare "10:31" in a 7-day window names no day, the AI copied it, and the occupant could not
# tell which morning "6:15 AM" was. English names on purpose (not strftime's locale-dependent %a): the context is
# English text and its byte form must not change with the host's locale.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _as_local_aware(dt: datetime.datetime) -> datetime.datetime:
    """The instant in Home Assistant local time; a naive datetime is read as UTC, like ``_parse_event_time``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt_util.as_local(dt)


def fmt_day_stamp(dt: datetime.datetime, *, seconds: bool = False) -> str:
    """``Sat 11-21 10:31`` (or ``Sat 11-21 10:31:05``): weekday, month-day and 24-hour local time (Issue #1674)."""
    local = _as_local_aware(dt)
    clock = local.strftime("%H:%M:%S" if seconds else "%H:%M")
    return f"{_WEEKDAYS[local.weekday()]} {local.month:02d}-{local.day:02d} {clock}"


def fmt_clock_near(dt: datetime.datetime, ref: datetime.datetime, *, seconds: bool = False) -> str:
    """A time printed right after ``ref``'s stamp: the bare clock when both fall on the same local day, else the full
    stamp, so a second time across midnight still names its own day (Issue #1674)."""
    local, local_ref = _as_local_aware(dt), _as_local_aware(ref)
    if local.date() == local_ref.date():
        return local.strftime("%H:%M:%S" if seconds else "%H:%M")
    return fmt_day_stamp(local, seconds=seconds)


def fmt_day_span(start: datetime.datetime, end: datetime.datetime) -> str:
    """``Sat 11-21 10:46-11:21``; a single stamp when both are the same minute; ``Fri 11-20 23:50-Sat 11-21 00:10``
    across midnight (Issue #1674)."""
    first = fmt_day_stamp(start)
    second = fmt_clock_near(end, start)
    return first if first.endswith(" " + second) else f"{first}-{second}"


def _fmt_time(raw_time: Any) -> str:
    """Format a raw event-log / log-record timestamp as ``Sat 11-21 10:31`` (local; Issue #1674 added the day)."""
    if raw_time is None:
        return "??:??"
    parsed = _parse_event_time(raw_time)
    if parsed is None:
        return str(raw_time)
    return fmt_day_stamp(parsed)


def _humanize_type(event_type: str) -> str:
    """Convert snake_case event type to a human-readable label."""
    return event_type.replace("_", " ").capitalize()


# ---------------------------------------------------------------------------
# Issue #1341: the Activity record as messages.
#
# Each renderer builds ``i18n.Msg`` cells (catalog area ``activity``); the AI input and every legacy caller get the
# English text (``record_en``), the dashboard gets the ``Msg`` wire form. A cell may still be a plain ``str`` (an
# entity id, a mode code, an old event's stored text); ``render``/``render_en`` pass those through unchanged.
# ---------------------------------------------------------------------------

Cell = i18n.Msg | str


def _m(key: str, **params: Any) -> i18n.Msg:
    """A catalog message of the ``activity`` area."""
    return i18n.Msg(f"activity.{key}", **params)


def _tp(value_f: Any, unit: str, decimals: int = 0) -> i18n.Temp:
    """An absolute temperature param (internal °F, shown in ``unit``)."""
    return i18n.Temp(value_f, unit, decimals)


def _tdp(delta_f: Any, unit: str, decimals: int = 0) -> i18n.TempDelta:
    """A temperature difference param (scale-only conversion)."""
    return i18n.TempDelta(delta_f, unit, decimals)


def _yn(flag: Any) -> str:
    """``select`` argument for an optional clause."""
    return "yes" if flag else "no"


def _num(value: Any) -> Any:
    """A payload number as a message param, keeping the exact text a Python f-string would print (2.0 stays "2.0")."""
    return value if isinstance(value, int) and not isinstance(value, bool) else str(value)


def _field(p: dict, name: str, default: Any = "", *, strip: bool = False) -> Cell:
    """A payload text field: a ``Msg`` when the event stored ``<name>_key``/``<name>_params``, else its text."""
    key = p.get(f"{name}_key")
    if key:
        try:
            return i18n.Msg.from_wire({"key": key, "params": p.get(f"{name}_params") or {}})
        except Exception:  # noqa: BLE001 - a malformed stored wire form falls back to the stored English
            pass
    value = p.get(name, default)
    if value is None:
        return default
    return str(value).strip() if strip else value


def _join(parts: list[Cell], sep: str = "comma") -> Cell:
    """Join non-empty cells with the language's separator (``", "`` or `` — `` in English)."""
    parts = [x for x in parts if x != ""]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    limit = 6 if sep == "comma" else 4
    if len(parts) > limit:
        return _m(f"join_{sep}_2", a=_join(parts[:-1], sep), b=parts[-1])
    return _m(f"join_{sep}_{len(parts)}", **{chr(97 + i): part for i, part in enumerate(parts)})


def _cell_en(cell: Any) -> str:
    """The English text of a renderer cell (the AI input / record form)."""
    return str(record_en(cell))


def _cell_wire(cell: Any) -> dict | None:
    """The wire form of a renderer cell for the API's ``*_msg`` sibling (``None`` when the cell is plain text)."""
    return cell.to_wire() if isinstance(cell, i18n.Msg) else None


def _msg_renderer(fn: Callable[[dict, str], tuple[Any, Any]]) -> Callable[[dict, str], tuple[str, str]]:
    """Register ``fn`` (returns ``Msg``/``str`` cells) as an English-string renderer; the Msg form is on ``.msg``."""

    @functools.wraps(fn)
    def wrapper(p: dict, unit: str) -> tuple[str, str]:
        label, settings = fn(p, unit)
        return _cell_en(label), _cell_en(settings)

    wrapper.msg = fn  # type: ignore[attr-defined]
    return wrapper


def _precondition_action(p: dict) -> str:
    """``select`` argument of the TOU pre-conditioning sentence: cool / heat / other."""
    mode = p.get("mode", "")
    return "cool" if mode == "cool" else "heat" if mode == "heat" else "other"


def _band_setpoint_msg(floor: Any, ceiling: Any, active: Any, unit: str) -> i18n.Msg | str:
    """Render a ComfortBand as a single-setpoint Settings cell (a ``Msg``; ``""`` when the band is unreadable).

    active == "ceiling" -> the cool setpoint is the guarded edge.
    active == "floor"   -> the heat setpoint is the guarded edge.
    """
    try:
        floor_f = float(floor)
        ceiling_f = float(ceiling)
    except TypeError, ValueError:
        return ""
    edges = {"floor": _tp(floor_f, unit), "ceiling": _tp(ceiling_f, unit)}
    if active == "ceiling":
        return _m("setpoint_band_cool_guarded", **edges)
    if active == "floor":
        return _m("setpoint_band_heat_guarded", **edges)
    # active unknown -- show both
    return _m("setpoint_band_both", **edges)


def _format_band_setpoint(floor: Any, ceiling: Any, active: Any, unit: str) -> str:
    """English text of ``_band_setpoint_msg`` (the AI input and the legacy string callers)."""
    return record_en(_band_setpoint_msg(floor, ceiling, active, unit))


# ---------------------------------------------------------------------------
# EVENT_RENDERERS: (payload, unit) -> (event_text, settings_text)
# All renderers read structured payload fields -- never parse prose strings.
# Issue #1341: each renderer is written once as returning ``Msg`` (or ``""``) cells; ``@_msg_renderer`` registers
# an English-string wrapper (the AI input and the legacy callers) and keeps the Msg form on ``.msg`` for the
# dashboard (the API sends ``event_msg``/``settings_msg`` per row).
# ---------------------------------------------------------------------------

_WRITE_OUTCOME_KEYS = {
    "blocked": "write_outcome_blocked",
    "dry_run": "write_outcome_dry_run",
    "failed": "write_outcome_failed",
    "superseded": "write_outcome_superseded",
    "not_sent": "write_outcome_not_sent",
}


def _write_not_sent_suffix(p: dict) -> i18n.Msg | str | None:
    """Issue #1049 (change 5): reason text when an ``*_applied`` event's write was NOT sent.

    ``None`` for ``outcome == "sent"`` and for historical events with no ``outcome`` field
    (those predate the field and keep their original wording).
    """
    outcome = p.get("outcome")
    if not outcome or outcome == "sent":
        return None
    key = _WRITE_OUTCOME_KEYS.get(outcome)
    return _m(key) if key else str(outcome).replace("_", " ")


def _held_why(reason: Any) -> Cell:
    """Catalog text for a ``held_reason`` code (``ComfortModeWrite``); an unknown code is shown as given."""
    key = _RESTORE_HELD_KEYS.get(reason or "")
    return _m(key) if key else (str(reason) if reason else _m("held_default"))


def _mode_write_fact(d: Any, before: Any = None) -> Cell:
    """Issues #1268 / #1597: the "mode: ..." fact of a nat-vent row from its structured write record.

    ``d`` is the row's ``hvac_restore`` / ``hvac_mode_set`` (``mode`` / ``outcome`` / ``held_reason``); ``before`` the
    mode the thermostat held before (None: the exit rows, which always leave a fan session's Off).
    """
    if not isinstance(d, dict):
        return ""
    held = d.get("held_reason")
    if held:
        return _m("fact_mode_unchanged", why=_held_why(held))
    mode = d.get("mode")
    if not mode:
        return ""
    fact = (
        _m("fact_mode_change", old=mode_w(str(before)), new=mode_w(str(mode)))
        if before
        else _m("fact_mode_off_to", mode=mode_w(str(mode)))
    )
    not_sent = _write_not_sent_suffix(d)
    return _m("fact_mode_not_sent", fact=fact, why=not_sent) if not_sent else fact


def _band_write_not_sent(label: Cell, p: dict) -> Cell:
    """Issues #1267 / #1596: a setback / bedtime / wake-up row whose band was NOT sent says so, and why.

    Rows from versions before the fix carry no ``outcome`` and keep their wording.
    """
    held = p.get("held_reason")
    why = _held_why(held) if held else _write_not_sent_suffix(p)
    return _m("band_write_not_sent", label=label, why=why) if why else label


@_msg_renderer
def _render_comfort_band_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    reason = _field(p, "reason")
    not_sent = _write_not_sent_suffix(p)
    if not_sent:
        label = (
            _m("comfort_band_not_applied_mode", mode=mode_w(mode), why=not_sent)
            if mode
            else _m("comfort_band_not_applied", why=not_sent)
        )
    else:
        label = _m("comfort_band_applied_mode", mode=mode_w(mode)) if mode else _m("comfort_band_applied")
    if reason:
        label = _m("label_reason", label=label, reason=reason)
    settings = _band_setpoint_msg(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return label, settings


@_msg_renderer
def _render_bedtime_setback(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    label = _m("bedtime_setback_mode", mode=mode_w(mode)) if mode else _m("bedtime_setback")
    settings = _band_setpoint_msg(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return _band_write_not_sent(label, p), settings


@_msg_renderer
def _render_tou_precondition_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    schedule_id = p.get("schedule_id", "")
    label: Cell = _m(
        "tou_precondition", action=_precondition_action(p), has_schedule=_yn(schedule_id), schedule_id=schedule_id
    )
    not_sent = _write_not_sent_suffix(p)
    if not_sent:
        label = _m("tou_precondition_not_applied", label=label, why=not_sent)
    target = p.get("target")
    settings: Cell = _m("fact_target", target=_tp(target, unit)) if target is not None else ""
    return label, settings


@_msg_renderer
def _render_tou_schedule_window_active(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Phase 3d / Investigation D: a configured cost_tag="high" TOU schedule window
    became active. Occupant-first framing per CLAUDE.md: distinguishes "this window
    coincided with real heating/cooling" from "this window applied to a day with no
    planned HVAC operation, so nothing else happened" -- the exact live-instance gap
    (a schedule silently doing nothing with zero visibility) this event closes.
    """
    del unit  # no temperature value on this event
    preconditioned = bool(p.get("preconditioned"))
    hvac_mode = p.get("hvac_mode")
    skipped = p.get("skipped")
    if isinstance(skipped, list) and skipped:
        # Issues #1558/#1565: every schedule covering the window was skipped -- say so, and why (one wording).
        from .scheduler import schedule_skip_list  # noqa: PLC0415

        pairs = [(str(i.get("reason")), i.get("entity")) for i in skipped if isinstance(i, dict)]
        return _m("tou_window_skipped"), _m("tou_window_skip_reasons", reasons=schedule_skip_list(pairs))
    label = _m("tou_window_active")
    settings = (
        _m("tou_window_coincided", mode=mode_w(str(hvac_mode))) if preconditioned else _m("tou_window_none_needed")
    )
    return label, settings


@_msg_renderer
def _render_tou_av_precondition_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #899: away/vacation counterpart to tou_precondition_applied — the split-
    delta precool/preheat leg, driven directly by apply_tou_away_vacation_precondition()
    rather than the Home/Guest comfort-band path."""
    schedule_id = p.get("schedule_id", "")
    label = _m(
        "tou_av_precondition",
        action=_precondition_action(p),
        occupancy=occupancy_w(p.get("occupancy", "")),
        has_schedule=_yn(schedule_id),
        schedule_id=schedule_id,
    )
    target = p.get("target")
    settings: Cell = _m("fact_target", target=_tp(target, unit)) if target is not None else ""
    return label, settings


@_msg_renderer
def _render_tou_precondition_cycle_summary(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #899: end-of-window observability for away/vacation TOU pre-conditioning —
    did the split-delta prediction hold up, or did the AC/heat still have to run during
    the (supposedly banked-through) high-cost window?"""
    del unit  # no temperature value rendered here
    hvac_ran = p.get("hvac_ran_during_window")
    label = _m("tou_window_ended")
    settings = _m("tou_window_hvac_ran") if hvac_ran else _m("tou_window_drifted")
    return label, settings


@_msg_renderer
def _render_morning_wakeup(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    label = _m("morning_wakeup_mode", mode=mode_w(mode)) if mode else _m("morning_wakeup")
    settings = _band_setpoint_msg(p.get("floor"), p.get("ceiling"), p.get("active"), unit)
    return _band_write_not_sent(label, p), settings


@_msg_renderer
def _render_occupancy_setback(p: dict, unit: str) -> tuple[Cell, Cell]:
    occ = p.get("occupancy") or p.get("mode", "")
    label = _m("occupancy_setback_occ", occupancy=occupancy_w(occ)) if occ else _m("occupancy_setback")
    settings: Cell = _band_setpoint_msg(p.get("floor"), p.get("ceiling"), None, unit)
    if p.get("tou_bank") and p.get("target_f") is not None:
        # Issue #1409: the change wrote the time-of-use bank, not the band: say what the thermostat was told.
        with contextlib.suppress(TypeError, ValueError):
            settings = _m(
                "occupancy_setback_tou_bank",
                hvac_mode=mode_w(str(p.get("hvac_mode", ""))),
                target=_tp(float(p["target_f"]), unit),
                band=settings,
            )
    return _band_write_not_sent(label, p), settings


@_msg_renderer
def _render_occupancy_comfort_restored(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    target = p.get("target_f")
    label: Cell = _m("occupancy_restored_mode", mode=mode_w(mode)) if mode else _m("occupancy_restored")
    not_sent = _write_not_sent_suffix(p)  # Issue #1245: a failed / dry-run / superseded restore write
    if not_sent:
        label = _m("occupancy_restore_not_sent", mode=mode_w(mode), has_mode=_yn(mode), why=not_sent)
    settings: Cell = ""
    if target is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m("fact_setpoint", target=_tp(float(target), unit))
    if p.get("tou_bank") and settings:
        settings = _m("occupancy_restored_tou_bank", setting=settings)  # Issue #1409
    return label, settings


_RESTORE_HELD_KEYS = {
    "whf_session": "held_whf_session",
    "blower_hold": "held_blower_hold",
    "occupancy_redirect": "held_occupancy_redirect",
    "no_family": "held_no_family",
    # Issues #1267 / #1596 / #1268 / #1597 (``ComfortModeWrite`` held reasons of the band and nat-vent rows)
    "window_open": "held_window_open",
    "no_capable_mode": "held_no_capable_mode",
    "occupant_change": "held_occupant_change",
    "fan_off_pending": "held_fan_off_pending",
    "no_window_settled": "held_no_window_settled",
    "fan_not_started": "held_fan_not_started",
}


@_msg_renderer
def _render_occupancy_comfort_restore_held(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1245: arriving home, the restore wrote nothing because a ventilation session owns the thermostat.
    why = _held_why(p.get("reason"))
    mode = p.get("mode", "")
    settings = _m("thermostat_unchanged_mode", mode=mode_w(mode)) if mode else _m("thermostat_unchanged")
    return _m("occupancy_restore_held", why=why), settings


@_msg_renderer
def _render_pre_cool_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    target = p.get("target")
    not_sent = _write_not_sent_suffix(p)
    label = _m("pre_cool_not_applied", why=not_sent) if not_sent else _m("pre_cool_applied")
    settings: Cell = ""
    if target is not None:
        try:
            floor = p.get("floor")
            if floor is not None:
                settings = _band_setpoint_msg(floor, float(target), "ceiling", unit)
            else:
                settings = _m("setpoint_cool", target=_tp(float(target), unit))
        except TypeError, ValueError:
            pass
    return label, settings


def _mode_change_fact(old_m: Any, new_m: Any) -> i18n.Msg:
    return _m("fact_mode_change", old=mode_w(str(old_m)), new=mode_w(str(new_m)))


def _setpoint_change_fact(old_t: Any, new_t: Any, unit: str) -> i18n.Msg:
    return _m("fact_setpoint_change", old=_tp(float(old_t), unit), new=_tp(float(new_t), unit))


@_msg_renderer
def _render_override_detected(p: dict, unit: str) -> tuple[Cell, Cell]:
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    old_m = p.get("old_mode") or p.get("old_hvac_mode")
    new_m = p.get("new_mode") or p.get("new_hvac_mode")
    source = p.get("source", "")
    label = _m("override_detected_source", source=source_w(source)) if source else _m("override_detected")
    parts: list[Cell] = []
    if old_m and new_m and old_m != new_m:
        parts.append(_mode_change_fact(old_m, new_m))
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_setpoint_change_fact(old_t, new_t, unit))
    return label, _join(parts)


@_msg_renderer
def _render_ceiling_guard_fired(p: dict, unit: str) -> tuple[Cell, Cell]:
    breach = p.get("breach_time", "")
    lead = p.get("lead_time_min")
    label = _m("ceiling_guard_fired_lead", breach=str(breach), lead=_num(lead)) if lead else _m("ceiling_guard_fired")
    old_m = p.get("old_hvac_mode")
    new_m = p.get("new_hvac_mode", "cool")
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    parts: list[Cell] = []
    if old_m and new_m and old_m != new_m:
        parts.append(_mode_change_fact(old_m, new_m))
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_setpoint_change_fact(old_t, new_t, unit))
    elif new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_setpoint", target=_tp(float(new_t), unit)))
    return label, _join(parts)


@_msg_renderer
def _render_classification_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    day_type = p.get("day_type", "")
    trend = p.get("trend", "")
    hvac = p.get("hvac_mode", "")
    old_m = p.get("old_hvac_mode")
    label: Cell = (
        _m("classification_applied_day", day_type=day_w(day_type)) if day_type else _m("classification_applied")
    )
    if trend:
        label = _m("label_trend", label=label, trend=trend_w(trend))
    settings_parts: list[Cell] = []
    if old_m and hvac and old_m != hvac:
        settings_parts.append(_mode_change_fact(old_m, hvac))
    today_high = p.get("today_high")
    threshold = p.get("applied_threshold_f")
    margin = p.get("threshold_margin_f")
    if today_high is not None and threshold is not None and margin is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                _m(
                    "classification_high_vs_threshold",
                    high=_tp(today_high, unit),
                    day_type=day_w(day_type),
                    threshold=_tp(threshold, unit),
                    sign="+" if margin >= 0 else "-",
                    margin=_tdp(abs(margin), unit, 1),
                )
            )
    trend_mag = p.get("trend_magnitude")
    if trend_mag is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(_m("classification_trend_magnitude", magnitude=_tdp(trend_mag, unit)))
    return label, _join(settings_parts)


@_msg_renderer
def _render_hvac_mode_not_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1049: the thermostat is not in the HVAC mode Climate Advisor requested."""
    requested = p.get("requested_mode") or "?"
    actual = p.get("actual_mode")
    kind = p.get("kind")
    if kind == "rejected":
        label = _m("hvac_mode_rejected", requested=mode_w(requested))
    elif kind == "open_window_retry":
        # Issue #1302: the Off for an open window failed or went unconfirmed; Climate Advisor is re-sending it.
        label = _m("hvac_mode_open_window_retry", actual=mode_w(actual or "?"), requested=mode_w(requested))
    elif kind == "restore_retry":
        # Issue #1308: the restore after the window closed (or a whole-house fan release) did not land; re-sending it.
        label = _m("hvac_mode_restore_retry", actual=mode_w(actual or "?"), requested=mode_w(requested))
    else:
        label = _m("hvac_mode_not_switched", requested=mode_w(requested))
    settings = (
        _m("hvac_mode_requested_reports", requested=mode_w(requested), actual=mode_w(str(actual)))
        if actual
        else _m("hvac_mode_requested", requested=mode_w(requested))
    )
    return label, settings


@_msg_renderer
def _render_hvac_mode_applied_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1049: the thermostat is now in the requested HVAC mode."""
    requested = p.get("requested_mode") or "?"
    minutes = p.get("minutes_down")
    return (
        _m("hvac_mode_now", requested=mode_w(requested)),
        (_m("fact_not_following_min", minutes=_num(minutes)) if minutes is not None else ""),
    )


@_msg_renderer
def _render_hvac_command_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1049: the thermostat accepted a climate command again after failures."""
    minutes = p.get("minutes_down")
    return _m("hvac_command_recovered"), (_m("fact_down_min", minutes=_num(minutes)) if minutes is not None else "")


_RESTORE_DEFERRED_KEYS = {
    "manual_override": "deferred_manual_override",
    "override_confirm_pending": "deferred_override_confirm_pending",
    "paused_by_door": "deferred_door_window_open",
    "open_sensor": "deferred_door_window_open",
}


def _lockout_facts(p: dict, unit: str) -> Cell:
    """Issue #1699: the Settings cell of a lockout row -- the outdoor reading and limit, or the depth past the edge."""
    if p.get("safety") and isinstance(p.get("depth_f"), (int, float)):
        return _m("lockout_fact_depth", depth=_tdp(p["depth_f"], unit, 1))
    outdoor, threshold = p.get("outdoor_f"), p.get("threshold_f")
    if isinstance(outdoor, (int, float)) and isinstance(threshold, (int, float)):
        return _m("lockout_fact_outdoor", outdoor=_tp(outdoor, unit, 1), threshold=_tp(threshold, unit, 1))
    if isinstance(outdoor, (int, float)):
        return _m("lockout_fact_outdoor_only", outdoor=_tp(outdoor, unit, 1))
    return ""


def _lockout_why(p: dict) -> str:
    """``select`` argument for the lockout's reason (``season`` / anything else = the outdoor lockout)."""
    return "season" if p.get("reason") == "season" else "outdoor"


@_msg_renderer
def _render_hvac_lockout_engaged(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1699: an outdoor lockout or the season setting took a side away (or locked it again once the house was
    # back inside the tolerance after a safety release).
    key = "lockout_safety_relocked" if p.get("safety") else "lockout_engaged"
    return _m(key, mode=mode_w(str(p.get("family") or "")), why=_lockout_why(p)), _lockout_facts(p, unit)


@_msg_renderer
def _render_hvac_lockout_released(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1699: the lock ended, or stepped aside because the house went deeper past the edge than the tolerance.
    key = "lockout_safety_released" if p.get("safety") else "lockout_released"
    return _m(key, mode=mode_w(str(p.get("family") or "")), why=_lockout_why(p)), _lockout_facts(p, unit)


@_msg_renderer
def _render_occupancy_restore_deferred(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1050 (#1048 change 18): arriving home while a hold is active -- the comfort restore waits.
    key = _RESTORE_DEFERRED_KEYS.get(p.get("reason", ""))
    why: Cell = _m(key) if key else (p.get("reason") or _m("held_default"))
    mode = p.get("mode", "")
    settings = _m("resumes_once_clears_mode", mode=mode_w(mode)) if mode else _m("resumes_once_clears")
    return _m("occupancy_restore_deferred", why=why), settings


@_msg_renderer
def _render_hvac_output_stall(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1048 change 9: off-target for hours and no equipment ran in the needed direction."""
    heating = p.get("direction") != "cool"
    hours = p.get("hours")
    label = _m(
        "hvac_output_stall",
        direction="heating" if heating else "cooling",
        has_hours=_yn(hours is not None),
        hours=_num(hours) if hours is not None else "",
    )
    parts: list[Cell] = []
    with contextlib.suppress(TypeError, ValueError):
        if p.get("indoor_f") is not None:
            parts.append(_m("fact_indoor", indoor=_tp(float(p["indoor_f"]), unit)))
        if p.get("edge_f") is not None:
            parts.append(_m("fact_band_edge", edge=_tp(float(p["edge_f"]), unit)))
    if p.get("thermostat_mode"):
        parts.append(_m("fact_thermostat_mode", mode=mode_w(str(p["thermostat_mode"]))))
    if p.get("thermostat_action"):
        parts.append(_m("fact_action", action=action_w(str(p["thermostat_action"]))))
    return label, _join(parts)


@_msg_renderer
def _render_hvac_output_stall_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1048 change 9: the off-target stall ended (back in band, or equipment ran)."""
    minutes = p.get("minutes_down")
    detail = _m(
        "stall_recovered_detail",
        how="equipment_ran" if p.get("reason") == "equipment_ran" else "in_band",
        has_minutes=_yn(minutes is not None),
        minutes=_num(minutes) if minutes is not None else "",
    )
    return _m("stall_recovered"), detail


def _reject_streak_fact(streak: Any) -> i18n.Msg:
    return _m("fact_reject_streak", streak=_num(streak))


@_msg_renderer
def _render_setpoint_rejected(p: dict, unit: str) -> tuple[Cell, Cell]:
    commanded = p.get("commanded")
    reported = p.get("reported")
    streak = p.get("reject_streak")
    label = _m("setpoint_validation_failed")
    settings: Cell = ""
    if commanded is not None and reported is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m(
                "setpoint_commanded_reports",
                commanded=_tp(float(commanded), unit),
                reported=_tp(float(reported), unit),
            )
    if streak is not None:
        settings = _join([settings, _reject_streak_fact(streak)]) if settings else _reject_streak_fact(streak)
    return label, settings


@_msg_renderer
def _render_setpoint_nudge(p: dict, unit: str) -> tuple[Cell, Cell]:
    nudge_value = p.get("nudge_value")
    real_target = p.get("real_target")
    mode = p.get("mode", "")
    streak = p.get("reject_streak")
    label = _m("setpoint_nudge")
    settings: Cell = ""
    if nudge_value is not None and real_target is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m(
                "setpoint_nudge_detail",
                nudge=_tp(float(nudge_value), unit),
                mode=mode_w(mode),
                target=_tp(float(real_target), unit),
            )
    if streak is not None:
        settings = _join([settings, _reject_streak_fact(streak)]) if settings else _reject_streak_fact(streak)
    return label, settings


@_msg_renderer
def _render_override_cleared(p: dict, unit: str) -> tuple[Cell, Cell]:
    was_mode = p.get("was_mode", "")
    old_t = p.get("old_setpoint_f")
    reason = _field(p, "reason")
    label = _m("override_cleared_was", mode=mode_w(was_mode)) if was_mode else _m("override_cleared")
    settings: Cell = ""
    if old_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m("override_cleared_was_setpoint", setpoint=_tp(float(old_t), unit))
    elif reason:
        settings = _m("override_cleared_fan_reason", reason=reason)
    return label, settings


@_msg_renderer
def _render_override_confirmed(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    cls_mode = p.get("cls_mode", "")
    source = p.get("source", "")
    label: Cell = _m("override_confirmed_mode", mode=mode_w(mode)) if mode else _m("override_confirmed")
    if cls_mode and mode and cls_mode != mode:
        label = _m("override_confirmed_cls", label=label, cls_mode=mode_w(cls_mode))
    return label, _m("fact_trigger", trigger=source_w(str(source))) if source else ""


@_msg_renderer
def _render_override_self_resolved(p: dict, unit: str) -> tuple[Cell, Cell]:
    detected = p.get("detected_mode", "")
    current = p.get("current_mode", "")
    if detected and current:
        return _m("override_self_resolved_modes", detected=mode_w(detected), current=mode_w(current)), ""
    return _m("override_self_resolved"), ""


@_msg_renderer
def _render_override_adopted(p: dict, unit: str) -> tuple[Cell, Cell]:
    mode = p.get("mode", "")
    src = p.get("source", "")
    pre_expiry = p.get("pre_expiry", False)
    label: Cell = _m("override_adopted_mode", mode=mode_w(mode)) if mode else _m("override_adopted")
    label = _m("override_adopted_agrees", label=label, early=_yn(pre_expiry))
    settings: Cell = _m("fact_trigger", trigger=source_w(str(src))) if src else ""
    return label, settings


_GRACE_TRIGGER_KEYS: dict[str, str] = {
    "fan_manual_override": "grace_trigger_fan_manual_override",
    "override_confirmed": "grace_trigger_override_confirmed",
    "dashboard_resume": "grace_trigger_dashboard_resume",
    "sensor_closed_resume": "grace_trigger_sensor_closed_resume",
    "nat_vent_exit_resume": "grace_trigger_nat_vent_exit_resume",
}


@_msg_renderer
def _render_grace_started(p: dict, unit: str) -> tuple[Cell, Cell]:
    trigger = p.get("trigger", "")
    source = p.get("source", "")
    duration = p.get("duration_seconds")
    label: Cell = _m("grace_started_min", minutes=duration // 60) if isinstance(duration, int) else _m("grace_started")
    if source:
        label = _m("label_source", label=label, source=source_w(str(source)))
    # Settings cell: human-readable trigger label for known triggers; empty otherwise.
    key = _GRACE_TRIGGER_KEYS.get(trigger)
    return label, _m(key) if key else ""


_CANCEL_REASON_KEYS: dict[str, str] = {
    "bedtime": "cancel_reason_bedtime",
    "morning_wakeup": "cancel_reason_morning_wakeup",
    "user_cancel_override": "cancel_reason_user_cancel_override",
    "user_cancel_fan_override": "cancel_reason_user_cancel_fan_override",
    "adopted_matching_decision": "cancel_reason_adopted_matching_decision",
    "superseded": "cancel_reason_superseded",
}


def _cancel_reason_label(reason: Any) -> i18n.Msg | str:
    """Human label for a cancel/clear reason; never raises, never blank."""
    if not reason or not isinstance(reason, str):
        return _m("cancel_reason_unspecified")
    key = _CANCEL_REASON_KEYS.get(reason)
    return _m(key) if key else reason.replace("_", " ")


@_msg_renderer
def _render_grace_cancelled(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1029: grace period ended early on purpose (bedtime, wake-up, cancel, superseded)."""
    label = _m("grace_cancelled", reason=_cancel_reason_label(p.get("reason")))
    parts: list[Cell] = []
    trigger = p.get("trigger")
    remaining = p.get("remaining_minutes")
    if trigger:
        parts.append(str(trigger))
    if remaining is not None:
        parts.append(_m("fact_min_left", minutes=_num(remaining)))
    settings: Cell = _join(parts)
    rf = p.get("remote_timer_hours")
    if rf is not None:
        rf_fact = _m("fact_rf_timer", hours=_num(rf))
        settings = _join([settings, rf_fact]) if settings else rf_fact
    return label, settings


@_msg_renderer
def _render_fan_override_cleared(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1029: fan override flag cleared by an intentional (non-expiry) path."""
    label = _m("fan_override_cleared", reason=_cancel_reason_label(p.get("reason")))
    rf = p.get("remote_timer_hours")
    return label, (_m("fact_rf_timer", hours=_num(rf)) if rf is not None else "")


_SENSOR_REASON_CODES = (
    "not_configured",
    "entity_missing",
    "unavailable",
    "no_reading",
    "non_numeric",
    "non_finite",
    "implausible",
    "stale",
)
SENSOR_REASON_LABELS: dict[str, str] = {
    "not_configured": "no sensor selected",
    "entity_missing": "sensor entity not found",
    "unavailable": "sensor unavailable",
    "no_reading": "no reading",
    "non_numeric": "sensor reading not a number",
    "non_finite": "sensor reading invalid",
    "implausible": "sensor reading out of range",
    "stale": "sensor stopped updating",
}
# Issue #1033: the outdoor name stays importable (api.py, tests); both roles share one table. Issue #1341: these
# English values are the record text; the catalog keys ``activity.sensor_reason_<code>`` carry the same words for the
# dashboard (a parity test pins them equal).
OUTDOOR_SENSOR_REASON_LABELS = SENSOR_REASON_LABELS

_OUTDOOR_SOURCE_IN_USE_CODES = ("weather_interpolation", "weather_attribute", "kept_forecast")
# weather_interpolation: weather service (hourly forecast); weather_attribute: weather service (current conditions);
# Issue #1337 kept_forecast: the weather service is down too; the value comes from the last forecast it sent.


def _sensor_reason_msg(reason: Any) -> Cell:
    """The occupant label for a sensor-failure reason code (``"unspecified"`` when there is none)."""
    if not reason:
        return _m("sensor_reason_unspecified")
    if reason in _SENSOR_REASON_CODES:
        return _m(f"sensor_reason_{reason}")
    return str(reason).replace("_", " ")


@_msg_renderer
def _render_outdoor_sensor_fallback(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1032: the dedicated outdoor sensor failed, so the weather service value is in use."""
    label = _m("outdoor_sensor_fallback", reason=_sensor_reason_msg(p.get("reason")))
    parts: list[Cell] = []
    in_use = p.get("source_in_use")
    if in_use:
        what: Cell = (
            _m(f"outdoor_source_{in_use}") if in_use in _OUTDOOR_SOURCE_IN_USE_CODES else str(in_use).replace("_", " ")
        )
        parts.append(_m("fact_using", what=what))
    value_f = p.get("value_f")
    if isinstance(value_f, (int, float)):
        parts.append(_m("fact_temp", temp=_tp(value_f, unit)))
    return label, _join(parts)


@_msg_renderer
def _render_outdoor_sensor_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1032: the dedicated outdoor sensor is being read again."""
    parts: list[Cell] = []
    value_f = p.get("value_f")
    if isinstance(value_f, (int, float)):
        parts.append(_m("fact_temp", temp=_tp(value_f, unit)))
    previous = p.get("previous_reason")
    if previous:
        parts.append(_m("fact_was_reason", reason=_sensor_reason_msg(previous)))
    minutes = p.get("minutes_in_fallback")
    if minutes is not None:
        parts.append(_m("fact_min_on_weather", minutes=_num(minutes)))
    return _m("outdoor_sensor_recovered"), _join(parts)


def _local_hhmm(iso: Any) -> str:
    """``Sat 11-21 10:31`` in the occupant's local time for an ISO instant, or "" when it cannot be read.

    Issue #1674: a "since" time can be days before the row that prints it, so it names its own day.
    """
    try:
        t = dt_util.parse_datetime(iso) if isinstance(iso, str) and iso else None
        return fmt_day_stamp(t) if isinstance(t, datetime.datetime) else ""
    except TypeError, ValueError, AttributeError:
        return ""


@_msg_renderer
def _render_entity_unavailable(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1322: a configured entity (thermostat, weather source, sensor, fan) stopped responding."""
    name = _field(p, "friendly_name") or p.get("entity_id") or _m("entity_generic")
    label = _m("entity_not_found" if p.get("status") == "missing" else "entity_unavailable", name=name)
    parts: list[Cell] = []
    since = _local_hhmm(p.get("down_since"))
    if since:
        parts.append(_m("fact_since", since=since))
    detail = _field(p, "detail")
    if detail:
        parts.append(detail if isinstance(detail, i18n.Msg) else str(detail))
    return label, _join(parts)


@_msg_renderer
def _render_entity_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1322: a configured entity is responding again after an announced outage."""
    name = _field(p, "friendly_name") or p.get("entity_id") or _m("entity_generic")
    parts: list[Cell] = []
    minutes = p.get("outage_minutes")
    if isinstance(minutes, int):
        parts.append(_m("fact_out_min", minutes=minutes))
    since = _local_hhmm(p.get("down_since"))
    if since:
        parts.append(_m("fact_since", since=since))
    return _m("entity_available_again", name=name), _join(parts)


@_msg_renderer
def _render_indoor_sensor_unavailable(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1033: an indoor sensor failed (primary: decisions paused; sleep: primary used instead)."""
    entity = p.get("entity") or _m("none_selected")
    reason = _sensor_reason_msg(p.get("reason"))
    if p.get("role") == "sleep":
        label = _m("sleep_sensor_unavailable", entity=entity, reason=reason)
    else:
        label = _m("indoor_sensor_unavailable", entity=entity, reason=reason)
    return label, ""


@_msg_renderer
def _render_indoor_source_changed(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1230 R1c: the sensor Climate Advisor reads the room temperature from changed."""
    to_name = p.get("to_name") or p.get("to_entity") or _m("unknown_paren")
    from_name = p.get("from_name") or p.get("from_entity") or _m("unknown_paren")
    to_role = p.get("to_role")
    label = _m(
        "indoor_source_changed",
        to_name=to_name,
        has_role=_yn(to_role),
        to_role=str(to_role or ""),
        from_name=from_name,
    )
    parts: list[Cell] = []
    reason = _field(p, "reason")
    if reason:
        parts.append(reason if isinstance(reason, i18n.Msg) else str(reason))
    for key, fact in (("to_value_f", "fact_now_temp"), ("from_value_f", "fact_was_temp")):
        value_f = p.get(key)
        if isinstance(value_f, (int, float)):
            parts.append(_m(fact, temp=_tp(value_f, unit, 1)))
    return label, _join(parts)


@_msg_renderer
def _render_thermostat_recalibration_started(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1676: the thermostat jumped several degrees high when the heating stopped (a furnace high-limit trip) and
    reads falsely until it recalibrates; the room is read from another sensor meanwhile when one is configured."""
    name = p.get("thermostat_name") or p.get("thermostat") or _m("unknown_paren")
    alternate = p.get("alternate_name") or p.get("alternate_entity")
    label = _m("thermostat_recal_started", name=name, has_alt=_yn(alternate), alt=str(alternate or ""))
    parts: list[Cell] = []
    for key, fact in (("reading_f", "fact_now_temp"), ("baseline_f", "fact_was_temp")):
        value_f = p.get(key)
        if isinstance(value_f, (int, float)) and not isinstance(value_f, bool):
            parts.append(_m(fact, temp=_tp(value_f, unit, 1)))
    return label, _join(parts)


@_msg_renderer
def _render_thermostat_recalibration_ended(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1676: the thermostat's reading is back (``settled``) or the watch ran out (``timed_out``)."""
    name = p.get("thermostat_name") or p.get("thermostat") or _m("unknown_paren")
    label = _m("thermostat_recal_ended", name=name, how="timed_out" if p.get("how") == "timed_out" else "settled")
    parts: list[Cell] = []
    minutes = p.get("minutes")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
        parts.append(_m("fact_recal_min", minutes=_num(minutes)))
    peak = p.get("peak_f")
    if isinstance(peak, (int, float)) and not isinstance(peak, bool):
        parts.append(_m("fact_peak_temp", temp=_tp(peak, unit, 1)))
    return label, _join(parts)


@_msg_renderer
def _render_humid_windows_close_notice(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1692: humid outdoor air with the windows open; the occupant was asked to close them (advice only)."""
    dp = p.get("outdoor_dp_f")
    shown: Any = _tp(dp, unit, 1) if isinstance(dp, (int, float)) else _m("unknown_paren")
    label = _m("humid_windows_close_notice", dew_point=shown)
    parts: list[Cell] = []
    if p.get("reason") == "ceiling" and isinstance(p.get("ceiling_f"), (int, float)):
        parts.append(_m("fact_humid_ceiling", limit=_tp(p.get("ceiling_f"), unit, 1)))
    elif p.get("reason") == "wetter_than_house" and isinstance(p.get("indoor_dp_f"), (int, float)):
        parts.append(_m("fact_humid_wetter", indoor=_tp(p.get("indoor_dp_f"), unit, 1)))
    return label, _join(parts)


@_msg_renderer
def _render_humidity_source_changed(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1455: the source a chart humidity line (room, bedroom, outdoor) is read from changed. Chart only."""
    subject = str(p.get("subject") or "humidity")
    subject = subject[:1].upper() + subject[1:]
    from_name = p.get("from_name") or p.get("from_entity") or _m("no_humidity_source")
    if p.get("to_entity"):
        to_name = p.get("to_name") or p.get("to_entity")
        to_kind = p.get("to_kind")
        label: Cell = _m(
            "humidity_source_changed",
            subject=subject,
            to_name=to_name,
            has_kind=_yn(to_kind),
            to_kind=str(to_kind or ""),
            from_name=from_name,
        )
    else:
        label = _m("humidity_source_lost", subject=subject, from_name=from_name)
    parts: list[Cell] = []
    reason = _field(p, "reason")
    if reason:
        parts.append(reason if isinstance(reason, i18n.Msg) else str(reason))
    for key, fact in (("to_rh", "fact_now_rh"), ("from_rh", "fact_was_rh")):
        value = p.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(_m(fact, rh=value))
    return label, _join(parts)


@_msg_renderer
def _render_forecast_model_changed(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1432: the model the prediction engine's decisions read changed (stitched CA reading <-> each room).

    A new event carries ``headline``/``detail`` as messages (``*_key``/``*_params``; the temperature differences are
    typed ``TempDelta`` params in degrees F with the display unit). An older event carries ``detail_template`` with
    ``{tN}`` slots and ``temps_f`` (differences in degrees F), filled here in the report's unit."""
    headline = _field(p, "headline")
    label: Cell = headline if headline else _m("forecast_model_changed")
    if p.get("detail_key"):
        return label, _field(p, "detail")
    tmpl = p.get("detail_template")
    if not isinstance(tmpl, str):
        return label, ""
    temps = p.get("temps_f") if isinstance(p.get("temps_f"), dict) else {}
    try:
        return label, tmpl.format(
            **{str(k): format_temp_delta(float(v), unit, 2) for k, v in temps.items() if isinstance(v, (int, float))}
        )
    except KeyError, IndexError, ValueError:
        return label, ""


@_msg_renderer
def _render_indoor_sensor_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1033: an indoor sensor is being read again."""
    entity = p.get("entity")
    label = _m(
        "indoor_sensor_recovered",
        role="sleep" if p.get("role") == "sleep" else "indoor",
        has_entity=_yn(entity),
        entity=str(entity or ""),
    )
    parts: list[Cell] = []
    value_f = p.get("value_f")
    if isinstance(value_f, (int, float)):
        parts.append(_m("fact_temp", temp=_tp(value_f, unit)))
    previous = p.get("previous_reason")
    if previous:
        parts.append(_m("fact_was_reason", reason=_sensor_reason_msg(previous)))
    minutes = p.get("minutes_down")
    if minutes is not None:
        parts.append(_m("fact_down_min", minutes=_num(minutes)))
    return label, _join(parts)


_BLIND_SESSION_CODES = ("nat_vent", "override", "economizer")


@_msg_renderer
def _render_fan_blind_started(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1037: a fan session is running while the indoor temperature is unreadable."""
    session = p.get("session")
    label = _m("fan_blind_started", kind=session if session in _BLIND_SESSION_CODES else "other")
    stop_at = p.get("stop_at")
    if p.get("will_stop") and stop_at:
        return label, _m("fan_blind_auto_stop", time=_fmt_time(stop_at))
    return label, _m("fan_blind_no_auto_stop")


@_msg_renderer
def _render_fan_blind_stopped(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1037: the blind-fan timeout ended a nat-vent session (always ends the fan)."""
    minutes = p.get("minutes_blind")
    fan_change = _fan_change(p, activating=False)
    label = _m(
        "fan_blind_stopped",
        has_minutes=_yn(minutes is not None),
        minutes=_num(minutes) if minutes is not None else "",
        fan_change=fan_change,
    )
    return label, ""


@_msg_renderer
def _render_fan_blind_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1037: indoor temperature reads again after a blind-fan episode."""
    label = _m("fan_blind_recovered")
    parts: list[Cell] = []
    value_f = p.get("indoor_f")
    if isinstance(value_f, (int, float)):
        parts.append(_m("fact_temp", temp=_tp(value_f, unit)))
    minutes = p.get("minutes_blind")
    if minutes is not None:
        # lower_bound: the outage is measured to the last blind sighting (a stopped or
        # unevaluated episode), so the real duration may be longer.
        parts.append(_m("fact_blind_min", at_least=_yn(p.get("lower_bound")), minutes=_num(minutes)))
    if p.get("stopped"):
        parts.append(_m("fact_fan_had_stopped"))
    elif p.get("fan_running", True) is False:
        parts.append(_m("fact_fan_no_longer_running"))
    else:
        parts.append(_m("fact_fan_kept_running"))
    return label, _join(parts)


@_msg_renderer
def _render_grace_expired(p: dict, unit: str) -> tuple[Cell, Cell]:
    source = p.get("source", "")
    re_paused = p.get("re_paused", False)
    label: Cell = _m("grace_expired_source", source=source_w(str(source))) if source else _m("grace_expired")
    if re_paused:
        label = _m("grace_expired_repaused", label=label)
    return label, ""


def _fan_transition_msg(fan_device: str, *, activating: bool) -> i18n.Msg:
    """Fallback fan-transition text for a payload that lacks `fan_mode_change`.

    Issue #957: mirrors `automation.py::_fan_transition_text()` -- a whole-house fan has
    no "auto" state, only a binary on/off relay; "auto" is only correct for the HVAC
    thermostat's own fan mode. Kept as a small, `fan_device`-string-only helper (rather
    than threading full config into every renderer) since `fan_device` (from
    `automation.py::_fan_device_label()`) is already present on nearly every payload.
    Issue #1341: the same message keys as `_fan_transition_text()` so both sides render identically.
    """
    whf = _m("fan_whf_on" if activating else "fan_whf_off")
    hvac_fan = _m("fan_hvac_on" if activating else "fan_hvac_off")
    if fan_device == "whf":
        return whf
    if fan_device == "hvac_fan":
        return hvac_fan
    if fan_device == "both":
        return _m("join_comma_2", a=whf, b=hvac_fan)
    return _m("fan_device_on" if activating else "fan_device_off", device=fan_device)


def _fan_transition_fallback(fan_device: str, *, activating: bool) -> str:
    """English text of ``_fan_transition_msg`` (the legacy string form)."""
    return _cell_en(_fan_transition_msg(fan_device, activating=activating))


def _fan_change(p: dict, *, activating: bool) -> Cell:
    """The payload's own ``fan_mode_change`` (a deferred-toggle note or the transition), else the fallback."""
    given = _field(p, "fan_mode_change")
    if given:
        return given
    return _fan_transition_msg(p.get("fan_device", "fan"), activating=activating)


@_msg_renderer
def _render_nat_vent_fan_on(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor_temp")
    outdoor = p.get("outdoor_temp")
    on_thr = p.get("on_threshold")
    label = _m("nat_vent_fan_on_cycling")
    if indoor is not None and on_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_fan_on_indoor", indoor=_tp(float(indoor), unit), threshold=_tp(float(on_thr), unit))
    # Issue #936: fan_mode_change, when present, overrides the fallback claim -- set by
    # the nat-vent cycling call site to a "deferred (...)" description when the Issue
    # #641 rate limiter blocked the toggle, mirroring _render_fan_activated()'s same
    # override pattern. Issue #957: fallback corrected to not claim an "auto" state a
    # whole-house fan doesn't have.
    settings: Cell = _fan_change(p, activating=True)
    if outdoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _join([settings, _m("fact_outdoor", outdoor=_tp(float(outdoor), unit))])
    return label, settings


@_msg_renderer
def _render_nat_vent_fan_off(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor_temp")
    off_thr = p.get("off_threshold")
    label = _m("nat_vent_fan_off_cycling")
    if indoor is not None and off_thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_fan_off_indoor", indoor=_tp(float(indoor), unit), threshold=_tp(float(off_thr), unit))
    # Issue #936: fan_mode_change, when present, overrides the fallback claim -- mirrors
    # _render_nat_vent_fan_on()/_render_fan_deactivated()'s same override pattern for a
    # deferred (rate-limited) fan-stop command. Issue #957: fallback corrected to not
    # claim an "auto" state a whole-house fan doesn't have.
    return label, _fan_change(p, activating=False)


@_msg_renderer
def _render_fan_activated(p: dict, unit: str) -> tuple[Cell, Cell]:
    reason = _field(p, "reason", strip=True)
    label = _m("fan_activated_reason", reason=reason) if reason else _m("fan_activated")
    # Issue #649: fan_mode_change, when present, overrides the fallback claim -- set by
    # _exit_nat_vent() to a "deferred (...)" description when the Issue #641 rate limiter
    # blocked the toggle, so this row doesn't claim a state transition that hasn't
    # happened yet. Issue #957: fallback corrected to not claim an "auto" state a
    # whole-house fan doesn't have.
    return label, _fan_change(p, activating=True)


@_msg_renderer
def _render_fan_deactivated(p: dict, unit: str) -> tuple[Cell, Cell]:
    reason = _field(p, "reason", strip=True)
    label = _m("fan_deactivated_reason", reason=reason) if reason else _m("fan_deactivated")
    return label, _fan_change(p, activating=False)


@_msg_renderer
def _render_hvac_write_blocked_whf_active(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #392 Fix 1b: choke-point guard intercepted an HVAC write while WHF owns the thermostat.

    Makes the structural WHF/AC mutual-exclusion guarantee visible in the Activity Log
    instead of silently dropping the blocked write.
    """
    attempted_mode = str(p.get("attempted_mode", "")).strip()
    reason = _field(p, "reason", strip=True)
    label = _m("ac_heat_blocked_whf_reason", reason=reason) if reason else _m("ac_heat_blocked")
    settings = _m("fact_hvac_blocked", mode=mode_w(attempted_mode)) if attempted_mode else ""
    return label, settings


@_msg_renderer
def _render_whf_hvac_suppressed(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #495: HVAC suppressed for a whole-house-fan session — CA-initiated OR a
    manual/remote fan-on detection (both now route through the same suppress helper).
    """
    prior_mode = str(p.get("prior_mode", "")).strip()
    reason = _field(p, "reason", strip=True)
    label = _m("ac_heat_paused_whf_reason", reason=reason) if reason else _m("ac_heat_paused_whf")
    settings = _m("fact_hvac_to_off", mode=mode_w(prior_mode)) if prior_mode else _m("fact_hvac_to_off_unknown")
    return label, settings


@_msg_renderer
def _render_whf_sealed_observe_notified(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1144: automation is off and the whole-house fan runs with every door/window closed.

    Climate Advisor sends no command in observe-only mode; it told the occupant instead (one notice per episode).
    """
    return _m("whf_sealed_observe_notified"), ""


@_msg_renderer
def _render_whf_rule2_backstop(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1288: the whole-house fan and the heating/cooling were both running (a press Climate Advisor missed);
    the rule-2 state backstop stopped one of them."""
    mode = str(p.get("thermostat_mode") or "").strip()
    if p.get("stopped") == "fan":
        return _m("whf_rule2_fan_stopped"), ""
    return (
        _m("whf_rule2_hvac_off"),
        _m("fact_hvac_to_off", mode=mode_w(mode)) if mode else _m("fact_hvac_to_off_unknown"),
    )


@_msg_renderer
def _render_whf_rule2_observe_notified(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1288: observe-only, the fan and the heating/cooling run together; the occupant was told, nothing sent."""
    return _m("whf_rule2_observe_notified"), ""


@_msg_renderer
def _render_whf_hvac_released(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #495: a manual/remote WHF session ended — HVAC suppression released and CA's
    current classification reasserted (not a blind restore of the mode captured at activation,
    since a remote-timer session can span hours).
    """
    reason = _field(p, "reason", strip=True)
    label = _m("ac_heat_resumed_reason", reason=reason) if reason else _m("ac_heat_resumed")
    return label, _m("fact_hvac_reclassifying")


@_msg_renderer
def _render_stranded_hvac_suppression_restored(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #618: the fan was already physically off, but a whole-house-fan suppression
    snapshot from an earlier session was still stranded — this restores the HVAC mode that
    snapshot captured. Previously invisible in the Activity Record (only a DEBUG log line);
    this is the specific corrective action that could silently cancel active/just-started
    cooling or heating (the 2026-08-10 incident), so it gets its own labeled entry.
    """
    reason = _field(p, "reason", strip=True)
    restore_mode = str(p.get("restore_mode", "")).strip()
    label = _m("ac_heat_restored_reason", reason=reason) if reason else _m("ac_heat_restored")
    settings = _m("fact_hvac_to_mode", mode=mode_w(restore_mode)) if restore_mode else _m("fact_hvac_restored")
    return label, settings


@_msg_renderer
def _render_fan_manual_override(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #524: append remote speed/timer context when the override was armed by an RF
    remote press (`automation.py::handle_fan_manual_override`'s `remote_speed`/
    `remote_timer_hours` kwargs) -- without it, this row looked identical whether a specific
    speed/timer choice drove the override or a thermostat-detected toggle did. A plain
    (non-remote) override has neither field set and renders exactly as before."""
    fan_before = str(p.get("fan_before", "")).strip()
    fan_after = str(p.get("fan_after", "")).strip()
    fan_device = p.get("fan_device", "fan")
    parts: list[Cell] = []
    if fan_before and fan_after:
        parts.append(_m("fact_fan_change", device=str(fan_device), change=f"{fan_before}->{fan_after}"))
    remote_speed = p.get("remote_speed")
    if remote_speed:
        parts.append(_m("fact_remote_speed", speed=str(remote_speed)))
    remote_timer_hours = p.get("remote_timer_hours")
    if remote_timer_hours is not None:
        parts.append(_m("fact_remote_timer", hours=_num(remote_timer_hours)))
    # Issue #1045: an override re-armed by the startup reconcile (not a fresh press).
    if p.get("restored_from"):
        parts.append(_m("fact_restored_after_restart"))
    return _m("fan_manual_override"), _join(parts)


@_msg_renderer
def _render_fan_speed_observed(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #519: a comfort-only remote speed change — NOT a manual override (the fan was
    already running; the user just adjusted speed, so no grace/HVAC-suppression armed)."""
    speed = str(p.get("speed", "")).strip()
    fan_device = p.get("fan_device", "fan")
    settings = _m("fact_fan_speed", device=str(fan_device), speed=speed) if speed else ""
    return _m("fan_speed_observed"), settings


@_msg_renderer
def _render_fan_running_untracked(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1664: a row with ``source_code`` names the source in the viewer's language; an older row carries only the
    # English ``source`` text and shows it as written (never parsed; those rows age out).
    code = p.get("source_code")
    source: Cell
    if code == "blower_during_cycle":
        source = _m("fan_source_blower_during_cycle", mode=mode_w(str(p.get("thermostat_mode", ""))))
    elif code == "fan_schedule":
        source = _m("fan_source_schedule")
    else:
        source = str(p.get("source", "")).strip() or _m("source_thermostat_initiated")
    action = str(p.get("hvac_action", "")).strip()
    label = _m("fan_running_external", source=source)
    settings = _m("fact_fan_on_external_action", action=action_w(action)) if action else _m("fact_fan_on_external")
    return label, settings


@_msg_renderer
def _render_fan_untracked_cleared(p: dict, unit: str) -> tuple[Cell, Cell]:
    fan_device = p.get("fan_device")
    settings = _m("fact_fan_device_off", device=str(fan_device)) if fan_device else _m("fact_fan_off")
    return _m("fan_stopped_external"), settings


@_msg_renderer
def _render_fan_observed_started(p: dict, unit: str) -> tuple[Cell, Cell]:
    origin = str(p.get("origin", "unknown")).replace("_", " ")
    action = str(p.get("hvac_action", "")).strip()
    mode = str(p.get("thermostat_fan_mode", "")).strip()
    cfg = str(p.get("fan_mode_config", "")).strip()
    settings = _m(
        "thermostat_blower_on_observed",
        origin=origin,
        mode=mode or "n/a",
        action=action_w(action or "n/a"),
        cfg=cfg or "n/a",
    )
    return _m("thermostat_fan_running_observed"), settings


@_msg_renderer
def _render_fan_observed_stopped(p: dict, unit: str) -> tuple[Cell, Cell]:
    minutes = p.get("duration_minutes")
    cfg = str(p.get("fan_mode_config", "")).strip()
    settings = _m(
        "thermostat_blower_off",
        has_minutes=_yn(minutes is not None),
        minutes=_num(minutes) if minutes is not None else "",
        cfg=cfg or "n/a",
    )
    return _m("thermostat_fan_stopped_observed"), settings


@_msg_renderer
def _render_fan_ownership_self_healed(p: dict, unit: str) -> tuple[Cell, Cell]:
    since = p.get("was_on_since")
    settings = _m("fan_released_believed_running", has_since=_yn(since), since=str(since or ""))
    return _m("fan_ownership_self_heal"), settings


@_msg_renderer
def _render_fan_orphan_released(p: dict, unit: str) -> tuple[Cell, Cell]:
    old = p.get("old_fan_mode")
    sent = p.get("command_sent")
    if p.get("dry_run") is True:
        # Issue #1108: automation was off -- nothing was sent and the pending release was dropped.
        return _m("fan_left_as_is"), _m("fan_left_as_is_detail", old=str(old))
    return _m("fan_released_after_config"), _m("fan_released_after_config_detail", old=str(old), sent=str(sent))


@_msg_renderer
def _render_fan_off_hold_ended(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1205: the occupant ended the fan-off pause while no fan-off grace was running (a thermostat override's
    grace had replaced it). With the grace running, ``grace_cancelled`` already says so, so this row only exists for the
    case that would otherwise leave no trace."""
    return _m("fan_off_hold_ended"), ""


@_msg_renderer
def _render_fan_cancel(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #567: branch on `trigger` — not every fan_cancel event is a user action.

    `physical_drift_correction` is CA noticing its own _fan_active bookkeeping was stale
    (the fan had already stopped) and correcting it — no user touched anything. Rendering
    that identically to a genuine user-detected fan-off misled the Activity Report into
    implying a manual action that never happened.
    """
    fan_before = str(p.get("fan_before", "?")).strip()
    fan_after = str(p.get("fan_after", "?")).strip()
    fan_device = p.get("fan_device", "fan")
    settings: Cell = (
        _m("fact_fan_change", device=str(fan_device), change=f"{fan_before}->{fan_after}")
        if fan_before and fan_after
        else ""
    )
    trigger = p.get("trigger")
    if trigger == "physical_drift_correction" and p.get("readback"):
        # session_kept False = the windows had closed so the ventilation session ended and the AC
        # suppression was released; otherwise the session (and the AC suppression) stays.
        released = p.get("session_kept") is False
        if p.get("retry"):
            if released:
                return _m("fan_cancel_ignored_ended"), settings
            return _m("fan_cancel_ignored_retry"), settings
        if released:
            return _m("fan_cancel_ignored_again_released"), settings
        return _m("fan_cancel_ignored_again_stays"), settings
    if trigger == "physical_drift_correction":
        return _m("fan_cancel_stale_flag"), settings
    if trigger == "timer_boundary_settle":
        return _m("fan_cancel_rf_timer"), settings
    if trigger == "manual_origin_carryforward":
        # Issue #829 / #1205: a PERSON turned off a fan Climate Advisor had taken over or restarted after an earlier
        # manual fan-on. The marker only selects this wording; the fan-off hold starts exactly as for any
        # occupant off, so the label says the fan is now being held off.
        started = ""
        raw_started = p.get("manual_origin_started")
        if raw_started:
            with contextlib.suppress(TypeError, ValueError):
                # Issue #1674: the earlier manual fan-on can be on a previous day; name it.
                started = fmt_day_stamp(datetime.datetime.fromisoformat(str(raw_started)))
        kind = p.get("manual_origin_kind")
        return (
            _m(
                "fan_cancel_manual_origin",
                kind=kind if kind in ("restart", "takeover") else "other",
                has_started=_yn(started),
                started=started,
            ),
            settings,
        )
    return _m("fan_cancel_user"), settings


def _exit_label_with_fan(label: Cell, p: dict) -> i18n.Msg:
    """Issue #957: fold the fan transition into the label itself (not only settings_text) so it survives into the
    session-grouped Activity Summary input, which reads only the label. This event always ends the fan; a deferred
    toggle's ``fan_mode_change`` (Issue #649) says so instead."""
    return _m("label_fan_change", label=label, fan_change=_fan_change(p, activating=False))


@_msg_renderer
def _render_nat_vent_outdoor_rise_exit(p: dict, unit: str) -> tuple[Cell, Cell]:
    outdoor = p.get("outdoor")
    indoor = p.get("indoor")
    label = _m("nat_vent_exit_outdoor_warmer")
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m(
                "nat_vent_exit_outdoor_gt_indoor", outdoor=_tp(float(outdoor), unit), indoor=_tp(float(indoor), unit)
            )
    return _exit_label_with_fan(label, p), ""


@_msg_renderer
def _render_nat_vent_comfort_floor_exit(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor_temp")
    heat = p.get("comfort_heat")
    label = _m("nat_vent_exit_floor_reached")
    if indoor is not None and heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_exit_indoor_le_floor", indoor=_tp(float(indoor), unit), floor=_tp(float(heat), unit))
    label = _exit_label_with_fan(label, p)
    if "hvac_restore" in p:  # Issue #1268: what the exit actually wrote (or why it wrote nothing)
        return label, _mode_write_fact(p["hvac_restore"])
    # Rows from versions before #1268 carry the day label only (not necessarily what was written); kept as they were.
    hvac_restored = p.get("hvac_mode_restored", "")
    settings = (
        _m("fact_mode_off_to", mode=mode_w(str(hvac_restored)))
        if hvac_restored and hvac_restored not in ("unknown", "")
        else ""
    )
    return label, settings


@_msg_renderer
def _render_nat_vent_manual_override_exit(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor_temp")
    mode = p.get("override_mode", "")
    label = _m("nat_vent_exit_manual_conflict")
    if mode:
        label = _m("nat_vent_exit_manual_override_mode", mode=mode_w(mode))
        if indoor is not None:
            with contextlib.suppress(TypeError, ValueError):
                label = _m(
                    "nat_vent_exit_manual_override_mode_indoor", mode=mode_w(mode), indoor=_tp(float(indoor), unit)
                )
    return _exit_label_with_fan(label, p), ""


@_msg_renderer
def _render_nat_vent_reconcile_exit(p: dict, unit: str) -> tuple[Cell, Cell]:
    reason = _field(p, "reason")
    return _m("nat_vent_exit_reconcile"), reason if isinstance(reason, i18n.Msg) else str(reason)


@_msg_renderer
def _render_nat_vent_away_ceiling_exit(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor")
    cool = p.get("comfort_cool")
    label = _m("nat_vent_exit_away_ceiling")
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_exit_away_indoor_ge", indoor=_tp(float(indoor), unit), ceiling=_tp(float(cool), unit))
    return _exit_label_with_fan(label, p), ""


@_msg_renderer
def _render_nat_vent_soft_start_entered(p: dict, unit: str) -> tuple[Cell, Cell]:
    outdoor = p.get("outdoor")
    indoor = p.get("indoor")
    label = _m("nat_vent_soft_start_parity")
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_soft_start_temps", outdoor=_tp(float(outdoor), unit), indoor=_tp(float(indoor), unit))
    parts: list[Cell] = []
    peak = p.get("outdoor_today_peak")
    if peak is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_todays_peak", peak=_tp(float(peak), unit)))
    heat = p.get("comfort_heat")
    margin = p.get("decline_margin_f")
    if heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_floor", floor=_tp(float(heat), unit)))
    if margin is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_decline_margin", margin=f"{float(margin):.1f}"))
    return label, _join(parts)


@_msg_renderer
def _render_nat_vent_reactivated_while_paused(p: dict, unit: str) -> tuple[Cell, Cell]:
    outdoor = p.get("outdoor")
    indoor = p.get("indoor")
    threshold = p.get("threshold")
    label = _m("nat_vent_reactivated_paused")
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_reactivated_temps", outdoor=_tp(float(outdoor), unit), indoor=_tp(float(indoor), unit))
    settings: Cell = ""
    if threshold is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m("fact_threshold", threshold=_tp(float(threshold), unit))
    return label, settings


@_msg_renderer
def _render_nat_vent_ceiling_escalation(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor")
    # Issue #1093: "ceiling" is the guard ceiling; older persisted events only carry "comfort_cool".
    cool = p.get("ceiling", p.get("comfort_cool"))
    label = _m("nat_vent_escalated")
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_escalated_temps", indoor=_tp(float(indoor), unit), ceiling=_tp(float(cool), unit))
    parts: list[Cell] = [_m("fact_mode_off_to_cool")]
    hours = p.get("hours_to_breach")
    lead = p.get("lead_min")
    k_cool = p.get("k_active_cool")
    if hours is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_breach_in_h", hours=f"{float(hours):.1f}"))
    if lead is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_lead_min", lead=int(lead)))
    if k_cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_k_cool", k=f"{float(k_cool):.3f}"))
    # Issue #936: fan_mode_change, when present, notes a deferred (rate-limited)
    # fan-stop command instead of silently implying the "mode: off->cool" switch
    # happened cleanly -- mirrors _render_nat_vent_away_ceiling_exit()'s
    # same fan_mode_change read.
    fan_change = _field(p, "fan_mode_change")
    if fan_change:
        parts.append(_m("fact_fan", fan=fan_change))
    return label, _join(parts)


@_msg_renderer
def _render_nat_vent_ac_assist_armed(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Legacy (Issue #1142 D7): AC assist was removed; kept so persisted logs from earlier versions still render.
    return _m("nat_vent_ac_assist_armed"), ""


@_msg_renderer
def _render_close_windows_advised(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1142 D7: blower ventilation never cools into an open window; the occupant was asked to close it.
    indoor = p.get("indoor")
    ceiling = p.get("comfort_cool")
    detail: Cell = (
        _m("fact_indoor_comfort_limit", indoor=_tp(indoor, unit), limit=_tp(ceiling, unit))
        if indoor and ceiling
        else ""
    )
    return _m("close_windows_advised"), detail


# Legacy event, no current emitter (Issue #593 audit) -- kept only to render
# historically-persisted event logs from a removed nat-vent code path.
@_msg_renderer
def _render_nat_vent_sleep_ceiling_reached(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor_temp")
    cool = p.get("sleep_cool")
    label = _m("nat_vent_exit_sleep_ceiling")
    if indoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_exit_sleep_temps", indoor=_tp(float(indoor), unit), ceiling=_tp(float(cool), unit))
    return label, ""


@_msg_renderer
def _render_nat_vent_bedtime_continue(p: dict, unit: str) -> tuple[Cell, Cell]:
    outdoor = p.get("outdoor_temp")
    cool = p.get("sleep_cool")
    label = _m("nat_vent_bedtime_continue")
    if outdoor is not None and cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m(
                "nat_vent_bedtime_continue_temps", outdoor=_tp(float(outdoor), unit), ceiling=_tp(float(cool), unit)
            )
    return label, ""


@_msg_renderer
def _render_sensor_opened(p: dict, unit: str) -> tuple[Cell, Cell]:
    entity = p.get("entity", "")
    result = p.get("result", "")
    trigger = p.get("trigger", "")
    label = _m("sensor_opened_result", result=str(result)) if result else _m("sensor_opened")
    if entity == "natural_vent_reeval" and trigger == "open_door_reeval":
        label = _m("sensor_open_recheck")
    elif entity and entity not in ("re-check", "natural_vent_reeval"):
        label = (
            _m("sensor_opened_entity_result", entity=str(entity), result=str(result))
            if result
            else _m("sensor_opened_entity", entity=str(entity))
        )
    elif trigger:
        label = _m("sensor_opened_trigger", trigger=str(trigger))
    hvac_change = p.get("hvac_mode_change", "")
    fan_change = _field(p, "fan_mode_change")
    parts: list[Cell] = []
    if "hvac_mode_set" in p:  # Issue #1597: what the nat-vent start did to the thermostat
        hold = _mode_write_fact(p["hvac_mode_set"], before=p.get("hvac_mode_before") or "unknown")
        if hold:
            parts.append(hold)
    elif hvac_change:  # the pause rows, and nat-vent rows from before #1597 (their "->band-armed" kept as written)
        parts.append(_m("fact_mode", mode=str(hvac_change)))
    if fan_change:
        parts.append(_m("fact_fan", fan=fan_change))
    outdoor = p.get("outdoor_temp")
    indoor = p.get("indoor_temp")
    threshold = p.get("threshold")
    if outdoor is not None and indoor is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(
                _m("fact_outdoor_lt_indoor", outdoor=_tp(float(outdoor), unit), indoor=_tp(float(indoor), unit))
            )
    if threshold is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_threshold_plain", threshold=_tp(float(threshold), unit)))
    return label, _join(parts)


@_msg_renderer
def _render_sensor_all_closed(p: dict, unit: str) -> tuple[Cell, Cell]:
    was_paused = p.get("was_paused", False)
    was_nat_vent = p.get("was_nat_vent", False)
    if was_nat_vent:
        # Issue #504 / #957: the fan really did turn off here (via _exit_nat_vent(),
        # whose own fan_deactivated event is intentionally suppressed per Issue #411) —
        # fold the transition into the label so it's visible even in the session-grouped
        # summary input, not just settings_text.
        return _m("sensors_closed_ending_nat_vent", fan_change=_fan_change(p, activating=False)), ""
    if was_paused:
        return _m("sensors_closed_resuming"), ""
    return _m("sensors_closed"), ""


@_msg_renderer
def _render_nat_vent_forecast_skip(p: dict, unit: str) -> tuple[Cell, Cell]:
    peak = p.get("forecast_peak")
    thr = p.get("threshold")
    label = _m("nat_vent_skip_forecast_warm")
    if peak is not None and thr is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_skip_forecast_peak", peak=_tp(float(peak), unit), threshold=_tp(float(thr), unit))
    return label, ""


@_msg_renderer
def _render_nat_vent_humid_skip(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #1691 (#1507 H1): a fan start refused because the outdoor air is humid (one row per humid spell)."""
    label = _m("nat_vent_skip_humid")
    out = p.get("outdoor_dew_point")
    with contextlib.suppress(TypeError, ValueError):
        if p.get("reason") == "ceiling" and out is not None and p.get("ceiling") is not None:
            label = _m(
                "nat_vent_skip_humid_ceiling",
                outdoor=_tp(float(out), unit, 1),
                ceiling=_tp(float(p["ceiling"]), unit, 1),
            )
        elif p.get("reason") == "wetter_than_house" and out is not None and p.get("indoor_dew_point") is not None:
            label = _m(
                "nat_vent_skip_humid_wetter",
                outdoor=_tp(float(out), unit, 1),
                indoor=_tp(float(p["indoor_dew_point"]), unit, 1),
            )
    return label, ""


@_msg_renderer
def _render_nat_vent_floor_imminent_skip(p: dict, unit: str) -> tuple[Cell, Cell]:
    ttf = p.get("time_to_floor_hr")
    label = _m("nat_vent_skip_floor_imminent")
    if ttf is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("nat_vent_skip_floor_in_hr", hours=f"{float(ttf):.2f}")
    parts: list[Cell] = []
    indoor = p.get("indoor_temp")
    heat = p.get("comfort_heat")
    if indoor is not None and heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_indoor_to_floor", indoor=_tp(float(indoor), unit), floor=_tp(float(heat), unit)))
    k_passive = p.get("k_passive")
    if k_passive is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_m("fact_k_passive", k=f"{float(k_passive):.4f}"))
    return label, _join(parts)


@_msg_renderer
def _render_bedtime_setback_skipped(p: dict, unit: str) -> tuple[Cell, Cell]:
    reason = _field(p, "reason")
    occ = p.get("occupancy", "")
    if reason == "occupancy" and occ:
        return _m("bedtime_setback_skipped_occ", occupancy=occupancy_w(str(occ))), ""
    if reason:
        return _m("bedtime_setback_skipped_reason", reason=reason), ""
    return _m("bedtime_setback_skipped"), ""


@_msg_renderer
def _render_morning_wakeup_skipped(p: dict, unit: str) -> tuple[Cell, Cell]:
    reason = _field(p, "reason")
    occ = p.get("occupancy", "")
    if reason == "occupancy" and occ:
        return _m("morning_wakeup_skipped_occ", occupancy=occupancy_w(str(occ))), ""
    return (_m("morning_wakeup_skipped_reason", reason=reason) if reason else _m("morning_wakeup_skipped")), ""


@_msg_renderer
def _render_pre_cool_suppressed_nat_vent(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor")
    target = p.get("target")
    reason = p.get("reason")
    label = _m("pre_cool_deferred_session") if reason == "active_session" else _m("pre_cool_suppressed_nat_vent")
    settings: Cell = ""
    if indoor is not None and target is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _m(
                "fact_indoor_vs_target",
                indoor=_tp(float(indoor), unit),
                mode="chasing" if reason == "active_session" else "le",
                target=_tp(float(target), unit),
            )
    return label, settings


@_msg_renderer
def _render_pre_cool_overshoot(p: dict, unit: str) -> tuple[Cell, Cell]:
    indoor = p.get("indoor")
    heat = p.get("comfort_heat")
    label = _m("pre_cool_overshoot")
    if indoor is not None and heat is not None:
        with contextlib.suppress(TypeError, ValueError):
            label = _m("pre_cool_overshoot_temps", indoor=_tp(float(indoor), unit), floor=_tp(float(heat), unit))
    return label, ""


@_msg_renderer
def _render_system_restarted(p: dict, unit: str) -> tuple[Cell, Cell]:
    # cause == "reload" (Issue #1110) is a config-entry reload, not an HA restart; not "settings saved" -- a
    # weather auto-resolve reload involves no user action.
    recovered = p.get("recovered_events", 0)
    cause = p.get("cause", "unknown")
    return (
        _m(
            "system_restarted",
            cause=cause if cause in ("version_changed", "user_restart", "reload") else "other",
            old=str(p.get("old_version")),
            new=str(p.get("new_version")),
            recovered=_num(recovered),
        ),
        "",
    )


@_msg_renderer
def _render_version_changed(p: dict, unit: str) -> tuple[Cell, Cell]:
    return _m("version_changed", old=str(p.get("old_version")), new=str(p.get("new_version"))), ""


@_msg_renderer
def _render_startup_coalesced(p: dict, unit: str) -> tuple[Cell, Cell]:
    nv = p.get("nat_vent_activated", False)
    hvac = p.get("hvac_commanded", False)
    sensors = p.get("sensors_open_count", 0)
    notes: list[Cell] = []
    if nv:
        notes.append(_m("note_nat_vent_activated"))
    if hvac:
        notes.append(_m("note_hvac_commanded"))
    if sensors:
        notes.append(_m("note_sensors_open", count=_num(sensors)))
    label = _m("startup_coalesced", has_notes=_yn(notes), notes=_join(notes))
    settings_parts: list[Cell] = []
    indoor_f = p.get("indoor_f")
    outdoor_f = p.get("outdoor_f")
    if indoor_f is not None and outdoor_f is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                _m("fact_indoor_outdoor", indoor=_tp(float(indoor_f), unit), outdoor=_tp(float(outdoor_f), unit))
            )
    archetype = p.get("fan_archetype")
    if archetype:
        settings_parts.append(_m("fact_fan", fan=str(archetype)))
    return label, _join(settings_parts)


@_msg_renderer
def _render_stuck_grace_recovered(p: dict, unit: str) -> tuple[Cell, Cell]:
    grace_end = p.get("grace_end_time", "")
    stale_mode = p.get("stale_mode", "")
    stale_since = p.get("stale_since", "")
    settings: Cell = _m("fact_was_since", mode=mode_w(str(stale_mode)), since=str(stale_since)) if stale_mode else ""
    if p.get("reason") == "grace_without_override":
        # Issue #508's watchdog mirror: grace_end_time is typically still in the future here
        # (the timer would have fired correctly on its own) — "expired" would be misleading.
        return _m("grace_recovered_no_override"), settings
    # Issue #1199: grace end instants are stored as UTC; show the occupant's local time.
    try:
        _end = dt_util.parse_datetime(grace_end) if grace_end else None
        if isinstance(_end, datetime.datetime):
            grace_end = dt_util.as_local(_end).isoformat()
    except TypeError, ValueError, AttributeError:
        pass
    return _m("grace_recovered_expired", end=str(grace_end)), settings


@_msg_renderer
def _render_state_contradiction_warning(p: dict, unit: str) -> tuple[Cell, Cell]:
    return _m(
        "mode_action_mismatch", mode=mode_w(str(p.get("hvac_mode", ""))), action=action_w(str(p.get("hvac_action", "")))
    ), ""


@_msg_renderer
def _render_invariant_violation(p: dict, unit: str) -> tuple[Cell, Cell]:
    """Issue #749: a hard system invariant was violated (e.g. AC and the whole-house
    fan both physically running at once).

    Issue #913: lead with the already-plain-English `detail` field (same text already
    shown on the dashboard Status card) as the label; the raw `invariant` enum-name code
    moves to the settings column instead of being the label.
    """
    invariant = p.get("invariant", "")
    detail = _field(p, "detail")
    label: Cell = detail or (
        _m("invariant_violated_named", invariant=str(invariant)) if invariant else _m("invariant_violated")
    )
    # Issue #1128: household promises reuse this event with extra keys. A log-level hit was noted
    # but never alerted, and the label says so (the AI Investigator must not call it an alert).
    if p.get("severity") == "log":
        label = _m("invariant_noted_not_alerted", label=label)
    promise = p.get("promise")
    if promise is not None and invariant:
        severity = p.get("severity")
        return label, _m(
            "fact_promise",
            promise=str(promise),
            invariant=str(invariant),
            has_severity=_yn(severity),
            severity=str(severity or ""),
        )
    return label, (_m("fact_invariant", invariant=str(invariant)) if invariant else "")


@_msg_renderer
def _render_thermal_learning_no_observations(p: dict, unit: str) -> tuple[Cell, Cell]:
    runtime = p.get("hvac_runtime_minutes", "")
    fan_only_runtime = p.get("thermostat_fan_only_runtime_minutes", "")
    label = _m("thermal_no_observations_runtime", minutes=_num(runtime)) if runtime else _m("thermal_no_observations")
    settings_parts: list[Cell] = []
    if fan_only_runtime:
        settings_parts.append(_m("fact_fan_only_runtime", minutes=_num(fan_only_runtime)))
    session_count = p.get("thermal_session_count")
    if session_count is not None:
        settings_parts.append(_m("fact_sessions_today", count=_num(session_count)))
    return label, _join(settings_parts)


@_msg_renderer
def _render_incident_detected(p: dict, unit: str) -> tuple[Cell, Cell]:
    cls = p.get("incident_class", "")
    incident_id = p.get("incident_id")
    label: Cell = _m("incident_detected_class", incident_class=str(cls)) if cls else _m("incident_detected")
    if incident_id:
        label = _m("label_id", label=label, id=str(incident_id))
    settings_parts: list[Cell] = []
    indoor_f = p.get("indoor_f")
    comfort_heat = p.get("comfort_heat")
    comfort_cool = p.get("comfort_cool")
    if indoor_f is not None and comfort_heat is not None and comfort_cool is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings_parts.append(
                _m(
                    "fact_indoor_vs_band",
                    indoor=_tp(float(indoor_f), unit, 1),  # Issue #1678: 64.4, not 64
                    floor=_tp(float(comfort_heat), unit),
                    ceiling=_tp(float(comfort_cool), unit),
                )
            )
    # Issue #1678: which sensor was judged (the bedroom at night) and the tolerance that judged it.
    source_name = p.get("indoor_source_name")
    if source_name:
        settings_parts.append(
            _m("fact_indoor_sensor", name=str(source_name), role=str(p.get("indoor_source_role") or "other"))
        )
    tolerance_f = p.get("tolerance_f")
    if isinstance(tolerance_f, (int, float)) and not isinstance(tolerance_f, bool):
        settings_parts.append(
            _m(
                "fact_tolerance",
                tolerance=_tdp(float(tolerance_f), unit, 1),
                kind=str(p.get("tolerance_kind") or "other"),
            )
        )
    occupancy = p.get("occupancy_mode")
    if occupancy:
        settings_parts.append(_m("fact_occupancy", occupancy=occupancy_w(str(occupancy))))
    # Issue #1049 (change 8): what the thermostat was doing and what CA last tried.
    t_mode = p.get("thermostat_mode")
    if t_mode:
        t_action = p.get("thermostat_action")
        settings_parts.append(
            _m(
                "fact_thermostat",
                mode=mode_w(str(t_mode)),
                has_action=_yn(t_action),
                action=action_w(str(t_action or "")),
            )
        )
    last_mode = p.get("last_commanded_mode")
    if last_mode:
        last_sp = p.get("last_commanded_setpoint_f")
        sp_temp: Any = ""
        if last_sp is not None:
            with contextlib.suppress(TypeError, ValueError):
                sp_temp = _tp(float(last_sp), unit)
        result = p.get("last_command_result")
        settings_parts.append(
            _m(
                "fact_ca_last_tried",
                mode=mode_w(str(last_mode)),
                has_setpoint=_yn(sp_temp != ""),
                setpoint=sp_temp,
                has_result=_yn(result),
                result=str(result or ""),
            )
        )
        # Issue #1678: the remote-sensor loop sets the thermostat above (heat) / below (cool) the comfort target.
        target_f = p.get("comfort_target_f")
        numbers = (
            isinstance(target_f, (int, float)) and not isinstance(target_f, bool) and isinstance(last_sp, (int, float))
        )
        if numbers and abs(float(target_f) - float(last_sp)) > 0.05:
            settings_parts.append(
                _m("fact_target_commanded", target=_tp(float(target_f), unit), commanded=_tp(float(last_sp), unit))
            )
    if cls == "grace_churn":
        last_fan = p.get("last_fan_event")
        settings_parts.append(
            _m(
                "fact_grace_churn",
                trigger=str(p.get("trigger")),
                count=str(p.get("count")),
                window=str(p.get("window_minutes")),
                first=str(p.get("first_time")),
                last=str(p.get("last_time")),
                cfg=str(p.get("fan_mode_config")),
                has_fan_event=_yn(isinstance(last_fan, dict)),
                fan_event=str(last_fan.get("type")) if isinstance(last_fan, dict) else "",
            )
        )
    return label, _join(settings_parts)


# Legacy warm_day events (pre-P3, may appear in persisted event logs). Confirmed
# zero current emitters (Issue #593 audit) -- kept renderer-only for history.
@_msg_renderer
def _render_warm_day_setback_applied(p: dict, unit: str) -> tuple[Cell, Cell]:
    old_t = p.get("old_setpoint_f")
    new_t = p.get("new_setpoint_f")
    label = _m("warm_day_setback_applied")
    settings: Cell = ""
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            settings = _setpoint_change_fact(old_t, new_t, unit)
    return label, settings


@_msg_renderer
def _render_warm_day_state_confirmed(p: dict, unit: str) -> tuple[Cell, Cell]:
    return _m("warm_day_state_confirmed"), ""


@_msg_renderer
def _render_warm_day_comfort_gap(p: dict, unit: str) -> tuple[Cell, Cell]:
    return _m("warm_day_comfort_gap"), ""


def _render_paused_entity_settings(p: dict) -> Cell:
    """Shared settings-cell rendering for the door/window pause lifecycle (Issue #592).

    ``paused_entity``/``paused_minutes`` are threaded from the same ``_paused_entity``/
    ``_paused_since`` state on the engine at every pause-suppression emit site, so the
    Settings cell shows which sensor and for how long consistently across all of them.
    """
    entity = p.get("paused_entity")
    minutes = p.get("paused_minutes")
    if not entity:
        return ""
    if isinstance(minutes, (int, float)):
        return _m("fact_paused_entity_open", entity=str(entity), minutes=int(minutes))
    return str(entity)


@_msg_renderer
def _render_classification_suppressed_paused(p: dict, unit: str) -> tuple[Cell, Cell]:
    day_type = p.get("day_type", "")
    hvac_mode = p.get("hvac_mode", "")
    label = _m(
        "classification_suppressed_paused",
        has_detail=_yn(day_type or hvac_mode),
        day_type=day_w(str(day_type)),
        mode=mode_w(str(hvac_mode)),
    )
    return label, _render_paused_entity_settings(p)


@_msg_renderer
def _render_occupancy_setback_suppressed_paused(p: dict, unit: str) -> tuple[Cell, Cell]:
    occupancy = p.get("occupancy", "away")
    return _m(
        "occupancy_setback_suppressed_paused", occupancy=occupancy_w(str(occupancy))
    ), _render_paused_entity_settings(p)


@_msg_renderer
def _render_comfort_family_switch_locked_out(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #821: the family-switch lockout (comfort_mode_switch_min_interval_s)
    # blocked a candidate heating/cooling family switch — surfaced so a persistently
    # blocked breach is observable, not silent (per that issue's own design note).
    candidate = p.get("candidate_family", "")
    label = _m("switch_delayed_candidate", candidate=family_w(str(candidate))) if candidate else _m("switch_delayed")
    return label, _field(p, "reason")


@_msg_renderer
def _render_comfort_family_switch(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1066 (nearest-edge arming): the thermostat's guarded edge flipped. The detail shows where
    # indoor sat against the comfort range at that moment so "why did it switch now?" is answerable
    # without raw logs. "breach" means indoor was already outside the range (switched immediately).
    # Events persisted by v0.7.96 and earlier carry the retired deadband/recency keys; they still render.
    resolved_family = p.get("resolved_family", "")
    label: Cell = (
        _m("switched_to_family", family=family_w(str(resolved_family))) if resolved_family else _m("family_switched")
    )
    # Issue #1073: also emitted when the thermostat's previous mode could not be read ("unknown").
    if p.get("from_family") == "unknown":
        label = _m("label_previous_unknown", label=label)
    detail_parts: list[Cell] = [_field(p, "reason")]
    indoor, floor, ceiling = p.get("indoor_f"), p.get("floor_f"), p.get("ceiling_f")
    if all(isinstance(v, (int, float)) for v in (indoor, floor, ceiling)):
        detail_parts.append(
            _m(
                "fact_indoor_comfort_range",
                indoor=_tp(indoor, unit),
                floor=_tp(floor, unit),
                ceiling=_tp(ceiling, unit),
            )
        )
    margin = p.get("margin_applied_f")
    if isinstance(margin, (int, float)):
        detail_parts.append(_m("fact_arming_margin", margin=_tdp(margin, unit, 1)))
    if p.get("breach"):
        detail_parts.append(_m("fact_outside_comfort_range"))
    legacy_deadband = p.get("deadband_applied_f")
    if legacy_deadband is not None:
        detail_parts.append(_m("fact_deadband_applied", deadband=_tdp(legacy_deadband, unit, 1)))
    return label, _join([part for part in detail_parts if part], sep="dash")


@_msg_renderer
def _render_thermostat_mode_set(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1073: one row per ACTUAL thermostat mode write (re-affirms are never emitted). The detail
    # answers "who asked and why" so a heat/cool flip can be traced without raw logs.
    to_mode = p.get("to_mode") or ""
    from_mode = p.get("from_mode")
    label: Cell = _m("thermostat_set_to", mode=mode_w(str(to_mode))) if to_mode else _m("thermostat_mode_set")
    if from_mode and from_mode != to_mode:
        label = _m("label_was", label=label, was=mode_w(str(from_mode)))
    detail_parts: list[Cell] = [_field(p, "reason")]
    source = p.get("source")
    # The row is written when the change is REQUESTED; confirmation/failure are separate rows (#1049).
    detail_parts.append(_m("fact_requested_by", source=requested_by_w(str(source))) if source else _m("fact_requested"))
    indoor, floor, ceiling = p.get("indoor_f"), p.get("floor_f"), p.get("ceiling_f")
    if isinstance(indoor, (int, float)):
        if isinstance(floor, (int, float)) and isinstance(ceiling, (int, float)):
            detail_parts.append(
                _m(
                    "fact_indoor_comfort_range",
                    indoor=_tp(indoor, unit),
                    floor=_tp(floor, unit),
                    ceiling=_tp(ceiling, unit),
                )
            )
        else:
            detail_parts.append(_m("fact_indoor", indoor=_tp(indoor, unit)))
    outcome = p.get("outcome")
    if outcome and outcome not in ("sent", "requested"):
        detail_parts.append(_m("fact_not_applied_outcome", outcome=outcome_w(str(outcome))))
    return label, _join([part for part in detail_parts if part], sep="dash")


_OPPOSITE_SIDE_CODES = {"heating": "cooling", "cooling": "heating", "heat": "cooling", "cool": "heating"}


@_msg_renderer
def _render_comfort_switch_held(p: dict, unit: str) -> tuple[Cell, Cell]:
    # Issue #1073: the settling guard deliberately did not flip the mode (shallow breach shortly after the
    # opposite side ran or the user acted). Surfaced so a "why isn't it heating yet?" question is answerable.
    candidate = p.get("candidate_family") or ""
    label = _m("switch_held_candidate", candidate=family_w(str(candidate))) if candidate else _m("switch_held")
    detail_parts: list[Cell] = []
    indoor = p.get("indoor_f")
    if isinstance(indoor, (int, float)):
        detail_parts.append(_m("fact_indoor", indoor=_tp(indoor, unit)))
    depth, tolerance = p.get("depth_f"), p.get("tolerance_f")
    if p.get("zone") == "margin":
        # Inside the comfort range, near the edge: the opposite side ran recently so the mode was not flipped.
        has_tol = isinstance(tolerance, (int, float))
        detail_parts.append(
            _m(
                "held_margin_near_edge",
                has_tolerance=_yn(has_tol),
                tolerance=_tdp(tolerance, unit, 1) if has_tol else "",
            )
        )
    elif isinstance(depth, (int, float)):
        has_tol = isinstance(tolerance, (int, float))
        detail_parts.append(
            _m(
                "held_past_edge",
                depth=_tdp(depth, unit, 1),
                has_tolerance=_yn(has_tol),
                tolerance=_tdp(tolerance, unit, 1) if has_tol else "",
            )
        )
    ran = p.get("opposite_ran_min_ago")
    if isinstance(ran, (int, float)):
        window = p.get("window_min")
        has_window = isinstance(window, (int, float))
        detail_parts.append(
            _m(
                "held_opposite_ran",
                side=_OPPOSITE_SIDE_CODES.get(str(candidate), "other"),
                minutes=int(round(ran)),
                has_window=_yn(has_window),
                window=int(round(window)) if has_window else 0,
            )
        )
    return label, _join(detail_parts, sep="dash")


# Registry: event_type -> renderer
EVENT_RENDERERS: dict[str, Callable[[dict, str], tuple[str, str]]] = {
    "comfort_band_applied": _render_comfort_band_applied,
    "tou_precondition_applied": _render_tou_precondition_applied,
    "tou_schedule_window_active": _render_tou_schedule_window_active,
    "tou_av_precondition_applied": _render_tou_av_precondition_applied,
    "tou_precondition_cycle_summary": _render_tou_precondition_cycle_summary,
    "bedtime_setback": _render_bedtime_setback,
    "morning_wakeup": _render_morning_wakeup,
    "occupancy_setback": _render_occupancy_setback,
    "occupancy_comfort_restored": _render_occupancy_comfort_restored,
    "occupancy_comfort_restore_held": _render_occupancy_comfort_restore_held,
    "pre_cool_applied": _render_pre_cool_applied,
    "override_detected": _render_override_detected,
    "ceiling_guard_fired": _render_ceiling_guard_fired,
    "classification_applied": _render_classification_applied,
    "classification_suppressed_paused": _render_classification_suppressed_paused,
    "occupancy_setback_suppressed_paused": _render_occupancy_setback_suppressed_paused,
    "comfort_family_switch_locked_out": _render_comfort_family_switch_locked_out,
    "comfort_family_switch": _render_comfort_family_switch,
    "thermostat_mode_set": _render_thermostat_mode_set,
    "comfort_switch_held": _render_comfort_switch_held,
    "hvac_lockout_engaged": _render_hvac_lockout_engaged,
    "hvac_lockout_released": _render_hvac_lockout_released,
    "setpoint_rejected": _render_setpoint_rejected,
    "hvac_mode_not_applied": _render_hvac_mode_not_applied,
    "hvac_mode_applied_recovered": _render_hvac_mode_applied_recovered,
    "hvac_command_recovered": _render_hvac_command_recovered,
    "hvac_output_stall": _render_hvac_output_stall,
    "occupancy_restore_deferred": _render_occupancy_restore_deferred,
    "hvac_output_stall_recovered": _render_hvac_output_stall_recovered,
    "setpoint_nudge": _render_setpoint_nudge,
    "override_cleared": _render_override_cleared,
    "override_confirmed": _render_override_confirmed,
    "override_self_resolved": _render_override_self_resolved,
    "override_adopted": _render_override_adopted,
    "grace_started": _render_grace_started,
    "grace_cancelled": _render_grace_cancelled,
    "fan_off_hold_ended": _render_fan_off_hold_ended,
    "fan_override_cleared": _render_fan_override_cleared,
    "outdoor_sensor_fallback": _render_outdoor_sensor_fallback,
    "outdoor_sensor_recovered": _render_outdoor_sensor_recovered,
    "entity_unavailable": _render_entity_unavailable,
    "entity_recovered": _render_entity_recovered,
    "indoor_sensor_unavailable": _render_indoor_sensor_unavailable,
    "indoor_sensor_recovered": _render_indoor_sensor_recovered,
    "indoor_source_changed": _render_indoor_source_changed,
    "thermostat_recalibration_started": _render_thermostat_recalibration_started,
    "thermostat_recalibration_ended": _render_thermostat_recalibration_ended,
    "humidity_source_changed": _render_humidity_source_changed,
    "humid_windows_close_notice": _render_humid_windows_close_notice,
    "forecast_model_changed": _render_forecast_model_changed,
    "fan_blind_started": _render_fan_blind_started,
    "fan_blind_stopped": _render_fan_blind_stopped,
    "fan_blind_recovered": _render_fan_blind_recovered,
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
    "whf_sealed_observe_notified": _render_whf_sealed_observe_notified,
    "whf_rule2_backstop": _render_whf_rule2_backstop,
    "whf_rule2_observe_notified": _render_whf_rule2_observe_notified,
    "stranded_hvac_suppression_restored": _render_stranded_hvac_suppression_restored,
    "fan_running_untracked": _render_fan_running_untracked,
    "fan_untracked_cleared": _render_fan_untracked_cleared,
    "fan_observed_started": _render_fan_observed_started,
    "fan_observed_stopped": _render_fan_observed_stopped,
    "fan_ownership_self_healed": _render_fan_ownership_self_healed,
    "fan_orphan_released": _render_fan_orphan_released,
    "fan_cancel": _render_fan_cancel,
    "nat_vent_outdoor_rise_exit": _render_nat_vent_outdoor_rise_exit,
    "nat_vent_reconcile_exit": _render_nat_vent_reconcile_exit,
    "nat_vent_comfort_floor_exit": _render_nat_vent_comfort_floor_exit,
    "nat_vent_manual_override_exit": _render_nat_vent_manual_override_exit,
    "nat_vent_away_ceiling_exit": _render_nat_vent_away_ceiling_exit,
    "nat_vent_soft_start_entered": _render_nat_vent_soft_start_entered,
    "nat_vent_reactivated_while_paused": _render_nat_vent_reactivated_while_paused,
    "nat_vent_ceiling_escalation": _render_nat_vent_ceiling_escalation,
    "nat_vent_ac_assist_armed": _render_nat_vent_ac_assist_armed,
    "close_windows_advised": _render_close_windows_advised,
    "nat_vent_sleep_ceiling_reached": _render_nat_vent_sleep_ceiling_reached,
    "nat_vent_bedtime_continue": _render_nat_vent_bedtime_continue,
    "sensor_opened": _render_sensor_opened,
    "sensor_all_closed": _render_sensor_all_closed,
    "nat_vent_forecast_skip": _render_nat_vent_forecast_skip,
    "nat_vent_floor_imminent_skip": _render_nat_vent_floor_imminent_skip,
    "nat_vent_humid_skip": _render_nat_vent_humid_skip,
    "bedtime_setback_skipped": _render_bedtime_setback_skipped,
    "morning_wakeup_skipped": _render_morning_wakeup_skipped,
    "pre_cool_suppressed_nat_vent": _render_pre_cool_suppressed_nat_vent,
    "pre_cool_overshoot": _render_pre_cool_overshoot,
    "system_restarted": _render_system_restarted,
    "version_changed": _render_version_changed,
    "startup_coalesced": _render_startup_coalesced,
    "stuck_grace_recovered": _render_stuck_grace_recovered,
    "state_contradiction_warning": _render_state_contradiction_warning,
    "invariant_violation": _render_invariant_violation,
    "thermal_learning_no_observations": _render_thermal_learning_no_observations,
    "incident_detected": _render_incident_detected,
    # Legacy warm_day events (pre-P3 persisted logs)
    "warm_day_setback_applied": _render_warm_day_setback_applied,
    "warm_day_state_confirmed": _render_warm_day_state_confirmed,
    "warm_day_comfort_gap": _render_warm_day_comfort_gap,
}


def _default_renderer_msg(event_type: str, payload: dict, unit: str) -> tuple[Cell, Cell]:
    """Surprise-safe fallback for unregistered event types.

    Event cell: humanized type + reason if present.
    Settings cell: generic extraction of recognized fields -- never blank-broken, never raises.
    """
    label: Cell = _humanize_type(event_type)
    reason = _field(payload, "reason")
    if reason:
        label = _m("label_reason", label=label, reason=reason)

    # Generic settings extraction
    parts: list[Cell] = []
    old_m = payload.get("old_hvac_mode") or payload.get("old_mode")
    new_m = payload.get("new_hvac_mode") or payload.get("new_mode")
    if old_m and new_m and old_m != new_m:
        parts.append(_mode_change_fact(old_m, new_m))
    old_t = payload.get("old_setpoint_f")
    new_t = payload.get("new_setpoint_f")
    if old_t is not None and new_t is not None:
        with contextlib.suppress(TypeError, ValueError):
            parts.append(_setpoint_change_fact(old_t, new_t, unit))
    floor = payload.get("floor")
    ceiling = payload.get("ceiling")
    active = payload.get("active")
    if floor is not None and ceiling is not None:
        band = _band_setpoint_msg(floor, ceiling, active, unit)
        if band:
            parts.append(band)
    fan = payload.get("fan") or payload.get("fan_mode_change")
    if fan:
        parts.append(_m("fact_fan", fan=_field(payload, "fan_mode_change") if not payload.get("fan") else str(fan)))
    trigger = payload.get("trigger")
    # "trigger" never appears in the parts built above (they are mode/setpoint/band/fan facts), so it is always added.
    if trigger:
        parts.append(_m("fact_trigger", trigger=str(trigger)))

    return label, _join(parts)


def _default_renderer(event_type: str, payload: dict, unit: str) -> tuple[str, str]:
    """English text of ``_default_renderer_msg`` (the legacy string form)."""
    label, settings = _default_renderer_msg(event_type, payload, unit)
    return _cell_en(label), _cell_en(settings)


# Types that should NOT be deduplicated (each has meaningful individual payload)
_NO_DEDUP: frozenset[str] = frozenset(
    {
        "system_restarted",
        "version_changed",
        "override_detected",
        "override_confirmed",
        "override_cleared",
        "override_adopted",
        "grace_cancelled",
        "fan_override_cleared",
        "outdoor_sensor_fallback",
        "outdoor_sensor_recovered",
        "indoor_sensor_unavailable",
        "indoor_sensor_recovered",
        "indoor_source_changed",
        "thermostat_recalibration_started",
        "thermostat_recalibration_ended",
        "humidity_source_changed",
        "forecast_model_changed",
        "entity_unavailable",
        "entity_recovered",
        "fan_blind_started",
        "fan_blind_stopped",
        "fan_blind_recovered",
        "hvac_mode_not_applied",
        "hvac_mode_applied_recovered",
        "hvac_command_recovered",
        "hvac_output_stall",
        "hvac_output_stall_recovered",
        "occupancy_restore_deferred",
        "ceiling_guard_fired",
        "incident_detected",
        "setpoint_rejected",
        "comfort_band_applied",
        "bedtime_setback",
        "morning_wakeup",
        "occupancy_comfort_restored",
        "occupancy_comfort_restore_held",
        "pre_cool_applied",
        "classification_applied",
    }
)


# Issue #913: event types that fire internally (coordinator's diagnostic override/grace
# FSM tracker still reads raw_event_log for these) but whose Activity Report row is
# intentionally suppressed -- purely internal diagnostic signals that read as confusing
# duplicates of an adjacent, already-rendered row (e.g. unprotected_grace_started duplicates
# the preceding grace_started row for the same action). Only the rendered row disappears;
# raw_event_log itself is untouched.
_REPORT_HIDDEN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "unprotected_grace_started",
    }
)


def _whf_warning_applies(config: dict[str, Any]) -> bool:
    """True when a whole-house fan is configured without state feedback (command-only mode)."""
    _fsf = config.get("fan_state_feedback", False)
    _fmode = config.get("fan_mode", "disabled")
    _fentity = config.get("fan_entity", "")
    return _fmode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and bool(_fentity) and not _fsf


def _maybe_prepend_whf_warning(table: str, config: dict[str, Any]) -> str:
    """Prepend a WHF command-only warning banner when fan_state_feedback is disabled."""
    if _whf_warning_applies(config):
        return _cell_en(_m("whf_command_only_banner")) + "\n\n" + table
    return table


class _RenderedEvent(NamedTuple):
    """One rendered (but not yet grouped) event-log entry (Issue #925).

    `dt` is the real parsed timestamp (may be None if unparseable) -- the field
    `_fmt_time()`-only rows never carried, needed for gap-based session boundaries.
    """

    dt: datetime.datetime | None
    time_str: str
    event_type: str
    ev_text: str
    settings_text: str
    source: str
    indoor: str
    outdoor: str
    ev_cell: Cell = ""  # Issue #1341: the same cells as ``Msg`` (or plain text), for the dashboard
    settings_cell: Cell = ""


def _render_timeline_events(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    limit: int = 200,
) -> tuple[list[_RenderedEvent], bool]:
    """Filter and render each in-window event log entry individually (Issue #925).

    One `_RenderedEvent` per raw entry, in chronological order, with NO grouping
    applied yet. Shared by `_build_timeline_rows()` (which applies consecutive-
    same-type collapsing on top, for the markdown table) and
    `_group_timeline_sessions()` (which applies gap-based grouping across any
    event type, for the Activity Summary) — this keeps the renderer dispatch
    (`EVENT_RENDERERS`/`_default_renderer`) and per-event field extraction in one
    place regardless of which grouping strategy consumes it.

    Events are filtered to the requested `hours` window FIRST, then capped to the
    most recent `limit` (Issue #432) — capping the raw log to its last N entries
    before filtering would silently drop older-but-still-in-window events whenever
    recent event volume exceeds the limit.
    """
    unit: str = config.get("temp_unit", "fahrenheit")
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)

    filtered, limited = filter_events_by_window(raw_event_log, hours, now, limit=limit)
    if not filtered:
        return [], limited

    rendered: list[_RenderedEvent] = []

    # Fan ownership tracker: updated per-event to detect when nat_vent_fan_off fires
    # while the user is still running the fan manually (misleading if shown as CA fan-off).
    _fan_user_owns = False

    for entry in filtered:
        event_type = str(entry.get("type", "unknown"))
        if event_type in _REPORT_HIDDEN_EVENT_TYPES:
            # Issue #913: intentionally hidden from the report (still present in
            # raw_event_log, untouched, for coordinator-side diagnostic consumers).
            continue
        payload = {k: v for k, v in entry.items() if k not in ("time", "type")}
        time_str = _fmt_time(entry.get("time"))
        event_dt = _parse_event_time(entry.get("time"))

        # Update fan ownership state before rendering
        if event_type in ("nat_vent_fan_on", "fan_activated"):
            _fan_user_owns = False
        elif event_type == "fan_manual_override":
            # The occupant's own fan-off ends their ownership too; otherwise a later CA fan-off would be
            # annotated "fan may still be running -- user-controlled" for a fan the occupant already stopped.
            _fan_user_owns = str(payload.get("fan_after", "")).strip() == "on"
        elif event_type == "fan_cancel":
            _fan_user_owns = False

        renderer = EVENT_RENDERERS.get(event_type)
        try:
            if renderer is not None:
                ev_cell, settings_cell = getattr(renderer, "msg", renderer)(payload, unit)
            else:
                ev_cell, settings_cell = _default_renderer_msg(event_type, payload, unit)
            # When nat_vent_fan_off fires while the user owns the fan, annotate the label
            # so the developer knows the physical fan may still be running under user control.
            if event_type == "nat_vent_fan_off" and _fan_user_owns:
                ev_cell = _m("label_fan_user_controlled", label=ev_cell)
            ev_text, settings_text = _cell_en(ev_cell), _cell_en(settings_cell)
        except Exception:
            _LOGGER.warning("activity_report: renderer raised for event type %r -- using fallback", event_type)
            ev_text = ev_cell = _humanize_type(event_type)
            settings_text = settings_cell = ""

        source = _event_source_label(event_type, payload) or "sensor"
        indoor_cell = _fmt_temp_cell(_first_temp(entry, "indoor_f", "indoor_temp", "indoor"), unit)
        outdoor_cell = _fmt_temp_cell(_first_temp(entry, "outdoor_f", "outdoor_temp", "outdoor"), unit)

        rendered.append(
            _RenderedEvent(
                event_dt,
                time_str,
                event_type,
                ev_text,
                settings_text,
                source,
                indoor_cell,
                outdoor_cell,
                ev_cell,
                settings_cell,
            )
        )

    return rendered, limited


class _TimelineRow(NamedTuple):
    """One output row of the collapsed timeline: the English strings plus the same cells as messages (Issue #1341)."""

    time_str: str
    event_text: str
    settings_text: str
    source: str
    indoor: str
    outdoor: str
    event_cell: Cell
    settings_cell: Cell
    dt: datetime.datetime | None = None  # Issue #1674: the row's first event, for the dashboard's localized date


def _run_clock_range(first: _RenderedEvent, last: _RenderedEvent) -> str:
    """The ``(16:48-17:36)`` range of a collapsed run, printed after the row's own day stamp (Issue #1674).

    Bare clocks while the run stays on the row's day; the end names its own day when the run crosses midnight
    (``23:50-Sun 11-22 00:10``). Falls back to the rendered stamps when a timestamp cannot be read.
    """
    if first.dt is None or last.dt is None:
        return f"{first.time_str}-{last.time_str}" if first.time_str != last.time_str else first.time_str
    start_clock = _as_local_aware(first.dt).strftime("%H:%M")
    end_clock = fmt_clock_near(last.dt, first.dt)
    return start_clock if start_clock == end_clock else f"{start_clock}-{end_clock}"


def _collapse_timeline_rows(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    limit: int = 200,
) -> tuple[list[_TimelineRow], bool]:
    """Render and deduplicate the event log into timeline rows (English strings and ``Msg`` cells together).

    Consecutive same-type events (excluding types in ``_NO_DEDUP``) collapse into one row with a xN count and time
    range; the Settings cell of a collapsed row is the LAST event's. Returns (rows, limited) in chronological order;
    ``limited`` is True if the window contained more than ``limit`` events.
    """
    rendered, limited = _render_timeline_events(raw_event_log, config, hours, now, limit=limit)
    if not rendered:
        return [], limited

    rows: list[_TimelineRow] = []
    run: list[_RenderedEvent] = []  # the open run: [first, ..., last]
    run_settings: tuple[str, Cell] = ("", "")

    def _flush_run() -> None:
        nonlocal run, run_settings
        if not run:
            return
        first, last = run[0], run[-1]
        if len(run) == 1:
            # A run of exactly one event was never actually deduplicated with anything —
            # this is the common case, not a collapsed group. Use the renderer's real
            # event text (which carries the descriptive reason) instead of the bare
            # _humanize_type(run_type) fallback, which silently discarded it for every
            # event type not on the small _NO_DEDUP allowlist.
            rows.append(
                _TimelineRow(
                    first.time_str,
                    first.ev_text,
                    run_settings[0],
                    first.source,
                    first.indoor,
                    first.outdoor,
                    first.ev_cell,
                    run_settings[1],
                    first.dt,
                )
            )
        else:
            time_range = _run_clock_range(first, last)
            collapsed = _m(
                "event_run_collapsed", type=_humanize_type(first.event_type), count=len(run), range=time_range
            )
            rows.append(
                _TimelineRow(
                    first.time_str,
                    _cell_en(collapsed),
                    run_settings[0],
                    first.source,
                    first.indoor,
                    first.outdoor,
                    collapsed,
                    run_settings[1],
                    first.dt,
                )
            )
        run = []

    for ev in rendered:
        event_type = ev.event_type
        # Flush run when type changes or type is not deduplicated
        if event_type in _NO_DEDUP or not run or event_type != run[0].event_type:
            _flush_run()
            if event_type in _NO_DEDUP:
                rows.append(
                    _TimelineRow(
                        ev.time_str,
                        ev.ev_text,
                        ev.settings_text,
                        ev.source,
                        ev.indoor,
                        ev.outdoor,
                        ev.ev_cell,
                        ev.settings_cell,
                        ev.dt,
                    )
                )
            else:
                # Start a new run; temps are from the first event in the run
                run = [ev]
                run_settings = (ev.settings_text, ev.settings_cell)
        else:
            # Continue run -- update last time and settings (last setpoint wins); temps stay from first event
            run.append(ev)
            if ev.settings_text:
                run_settings = (ev.settings_text, ev.settings_cell)

    _flush_run()
    return rows, limited


def _build_timeline_rows(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    limit: int = 200,
) -> tuple[list[tuple[str, str, str, str, str, str]], bool]:
    """Render and deduplicate the event log into timeline rows.

    Shared row-building core for `build_event_timeline_table()` (markdown table,
    Activity Record + LLM context) — a thin consecutive-same-type collapse pass
    over `_render_timeline_events()`'s output.

    Returns (rows, limited) where each row is
    (time_str, event_text, settings_text, source, indoor, outdoor), in chronological
    order, and `limited` is True if the window contained more than `limit` events.
    """
    rows, limited = _collapse_timeline_rows(raw_event_log, config, hours, now, limit=limit)
    return [r[:6] for r in rows], limited


def build_event_timeline_wire(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    newest_first: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """The Activity record as structured rows for the dashboard (Issue #1341).

    Each row carries the English text the table shows (``event``/``settings``) and, where the cell is a message, its
    wire form (``event_msg``/``settings_msg``, ``Msg.to_wire()``; ``None`` for a plain-text cell) so the dashboard
    renders it in the viewer's language. ``limited_msg``/``whf_warning_msg`` are the two table-level notices.
    """
    rows, limited = _collapse_timeline_rows(raw_event_log, config, hours, now, limit=limit)
    ordered = list(reversed(rows)) if newest_first else rows
    return {
        "rows": [
            {
                "time": r.time_str,
                # Issue #1674: the instant itself, so the dashboard shows the row's date in the viewer's language
                # (``time`` is the English ``Sat 11-21 10:31`` stamp the markdown table and the AI read).
                "ts": r.dt.isoformat() if r.dt is not None else None,
                "event": r.event_text,
                "settings": r.settings_text,
                "source": r.source,
                "indoor": r.indoor,
                "outdoor": r.outdoor,
                "event_msg": _cell_wire(r.event_cell),
                "settings_msg": _cell_wire(r.settings_cell),
            }
            for r in ordered
        ],
        "limited": limited,
        "limited_msg": _m("timeline_limited", limit=limit).to_wire() if limited else None,
        "no_events_msg": _m("no_events_in_window").to_wire() if not rows else None,
        "whf_warning_msg": _m("whf_command_only_banner").to_wire() if _whf_warning_applies(config) else None,
    }


_SESSION_GAP_MINUTES = 25  # gap-bounded session boundary threshold (Issue #925)

# Issue #925 follow-up: settings_text carries genuinely jargon-dense fields
# ("setpoint: 72°F Cool (64°F Heat)", "mode: heat->cool") that event_lines must
# never surface -- but it also carries a small number of plain, human-meaningful
# facts (a remote timer's duration, a remote's speed setting) that got thrown out
# with that jargon. This is a narrow whitelist, not a settings_text passthrough,
# so it can only ever add these specific plain phrases -- never any other
# settings_text content.
_SETTINGS_TEXT_FACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"remote timer:\s*(\d+(?:\.\d+)?)h"), "{0}-hour timer"),
    (re.compile(r"remote:\s*(\w+)\s*speed"), "{0} speed"),
]


def _extract_plain_facts(settings_text: str) -> list[str]:
    """Pull whitelisted, jargon-free facts out of a settings_text cell."""
    facts: list[str] = []
    for pattern, template in _SETTINGS_TEXT_FACT_PATTERNS:
        match = pattern.search(settings_text)
        if match:
            facts.append(template.format(match.group(1)))
    return facts


class _TimelineSession(NamedTuple):
    """One gap-bounded group of activity, the deterministic input for the
    LLM-authored Activity Summary (Issue #925)."""

    start_time_str: str
    end_time_str: str
    indoor_start: str
    indoor_end: str
    outdoor_start: str
    outdoor_end: str
    event_lines: list[str]  # humanized event text, in chronological order
    event_count: int
    time_range: str = ""  # Issue #1674: "Sat 11-21 10:46-11:21" (one stamp for a single moment)


def _group_timeline_sessions(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    limit: int = 200,
    gap_minutes: int = _SESSION_GAP_MINUTES,
) -> tuple[list[_TimelineSession], bool]:
    """Group rendered timeline events into gap-bounded sessions (Issue #925).

    A new session starts whenever the gap since the previous event exceeds
    `gap_minutes` of quiet — this absorbs a tightly-clustered burst of different
    event types (e.g. a comfort-band change, a fan cycle, and a reclassification
    all within one minute) into a single unit, unlike `_build_timeline_rows()`'s
    consecutive-SAME-TYPE-only collapsing. Each session carries only `event_lines`
    (the renderer's already-fairly-plain `ev_text` per event — never `settings_text`,
    which is the most jargon-dense field, e.g. "setpoint: 72°F Cool (64°F Heat)") plus
    session-level start/end time and temps, as the bounded, fact-only input an LLM
    prompt can be constrained to summarize without inventing new information.

    An event with no parseable timestamp never starts a new session on its own (it
    can't be gap-compared) — it's folded into whatever session is currently open, or
    starts the first session if none is open yet.
    """
    rendered, limited = _render_timeline_events(raw_event_log, config, hours, now, limit=limit)
    if not rendered:
        return [], limited

    sessions: list[_TimelineSession] = []
    current: list[_RenderedEvent] = []
    gap = datetime.timedelta(minutes=gap_minutes)

    def _flush_session() -> None:
        if not current:
            return
        indoor_vals = [e.indoor for e in current if e.indoor and e.indoor != "—"]
        outdoor_vals = [e.outdoor for e in current if e.outdoor and e.outdoor != "—"]

        def _line_for(ev: _RenderedEvent) -> str:
            facts = _extract_plain_facts(ev.settings_text) if ev.settings_text else []
            return f"{ev.ev_text} ({', '.join(facts)})" if facts else ev.ev_text

        dated = [e.dt for e in current if e.dt is not None]
        if dated:
            time_range = fmt_day_span(dated[0], dated[-1])
        else:
            first_s, last_s = current[0].time_str, current[-1].time_str
            time_range = f"{first_s}-{last_s}" if first_s != last_s else first_s
        sessions.append(
            _TimelineSession(
                start_time_str=current[0].time_str,
                end_time_str=current[-1].time_str,
                indoor_start=indoor_vals[0] if indoor_vals else "—",
                indoor_end=indoor_vals[-1] if indoor_vals else "—",
                outdoor_start=outdoor_vals[0] if outdoor_vals else "—",
                outdoor_end=outdoor_vals[-1] if outdoor_vals else "—",
                event_lines=[_line_for(e) for e in current],
                event_count=len(current),
                time_range=time_range,
            )
        )

    last_dt: datetime.datetime | None = None
    for ev in rendered:
        if current and ev.dt is not None and last_dt is not None and (ev.dt - last_dt) > gap:
            _flush_session()
            current = []
        current.append(ev)
        if ev.dt is not None:
            last_dt = ev.dt
    _flush_session()

    return sessions, limited


async def build_activity_sessions_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build the ACTIVITY SESSIONS section: the LLM's sole input for ACTIVITY SUMMARY
    (Issue #925). Deliberately separate from ACTIVITY TIMELINE (the raw, technical
    table used for the rest of the report's investigative sections) — this section
    contains only pre-grouped, fact-bounded session data, so the model has nothing
    jargon-dense to copy from when writing the summary.
    """
    hours = float(kwargs.get("hours", 24))
    hours = max(1.0, min(hours, 168.0))
    raw_event_log = list(getattr(coordinator, "_event_log", []) or [])
    config = getattr(coordinator, "config", {}) or {}
    sessions, _limited = _group_timeline_sessions(raw_event_log, config, hours, dt_util.now())

    if not sessions:
        return "=== ACTIVITY SESSIONS (for Activity Summary only) ===\n  No activity in this window.\n"

    lines = [f"=== ACTIVITY SESSIONS (for Activity Summary only, last {hours:g}h) ==="]
    for i, session in enumerate(sessions, start=1):
        lines.append(
            f"SESSION {i}: {session.time_range} (indoor {session.indoor_start}->{session.indoor_end}, "
            f"outdoor {session.outdoor_start}->{session.outdoor_end})"
        )
        for ev_text in session.event_lines:
            lines.append(f"  - {ev_text}")
    return "\n".join(lines) + "\n"


def build_event_timeline_table(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
    newest_first: bool = False,
    limit: int = 200,
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

    `limit` (default 200) bounds how many in-window events are rendered — callers
    that feed an LLM should keep the default; the display-only Activity Record view
    passes a much higher limit since no LLM ever sees this output (Issue #920).
    """
    rows, limited = _build_timeline_rows(raw_event_log, config, hours, now, limit=limit)

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
    if limited:
        table = f"NOTE: window contains more than {limit} events — showing the most recent {limit}.\n\n" + table

    return _maybe_prepend_whf_warning(table, config)


def build_activity_summary_narrative(
    raw_event_log: list[Any],
    config: dict[str, Any],
    hours: float,
    now: datetime.datetime,
) -> str:
    """Build a small, deliberately plain deterministic Activity Summary — the
    no-AI fallback's "activity_summary" (Issue #925).

    Issue #920 originally made this the primary (non-LLM) Activity Summary
    implementation; Issue #925 moved the primary, polished version to an
    LLM-authored `## ACTIVITY SUMMARY` section (see `_SYSTEM_PROMPT` in
    `ai_skills_investigator.py`), fed by `_group_timeline_sessions()`'s structured
    facts. This function now exists only for `investigation_fallback()` (no Claude
    available) so `activity_summary` is always populated on both paths without
    calling Claude from the no-AI path or duplicating the LLM's phrasing logic.
    Terse by design — not meant to read as polished prose, just present and
    jargon-free — e.g. "Sat 11-21 10:53-10:58: 6 event(s)."
    """
    sessions, limited = _group_timeline_sessions(raw_event_log, config, hours, now)

    if not sessions:
        return "No activity recorded in the analyzed window."

    lines: list[str] = []
    for session in sessions:
        lines.append(f"- {session.time_range}: {session.event_count} event(s).")

    narrative = "\n".join(lines)
    if limited:
        narrative = "NOTE: window contains more than 200 events — showing the most recent 200.\n\n" + narrative
    return narrative


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
    # Issue #920: clamp to 168h (7 days), matching EVENT_LOG_MAX_AGE_HOURS — the event
    # log itself never retains more, so a 720h ceiling was dead and misleading.
    hours = float(kwargs.get("hours", 24))
    hours = max(1.0, min(hours, 168.0))
    raw_event_log = list(getattr(coordinator, "_event_log", []) or [])
    config = getattr(coordinator, "config", {}) or {}
    table = build_event_timeline_table(raw_event_log, config, hours, dt_util.now())
    return f"=== ACTIVITY TIMELINE (last {hours:g}h) ===\n{table}\n"


async def build_state_cross_validation_context(hass: Any, coordinator: Any, **kwargs: Any) -> str:
    """Build a STATE CROSS-VALIDATION section (moved from ai_skills_activity.py, Issue #563).

    Two deterministic checks, pre-computed so the model cites them rather than
    re-deriving: (1) hvac_mode=off but hvac_action reports active — flagged unless
    it's the expected CA-fan-only-mode case (is_ca_fan_running() is the single
    source of truth here, Issue #458); (2) indoor temp vs. comfort band, judged by the SAME rule as
    the comfort incident scan (Issue #1258): the room may sit up to the thermostat's own learned
    swing past the band edge (``swing.within_thermostat_hysteresis``; since Issue #1234 the swing is
    how far BELOW the setpoint the thermostat lets the room fall before heating, how far ABOVE before
    cooling) only while the thermostat mode holds that direction (heat/heat_cool/auto below,
    cool/heat_cool/auto above). Off, the opposite mode or an unknown mode keep only the
    ``COMFORT_DEVIATION_FLOOR_F`` (0.5 F) tolerance, so a cold room with the thermostat Off is flagged.
    """
    data: dict[str, Any] = coordinator.data or {}
    options: dict[str, Any] = coordinator.config or {}

    hvac_mode = data.get("hvac_mode") or "unknown"
    hvac_action = data.get(ATTR_HVAC_ACTION, "unknown")
    fan_status = data.get(ATTR_FAN_STATUS, "unknown")

    _temp_unit = options.get("temp_unit", "fahrenheit")
    climate_entity_id: str = options.get("climate_entity", "")
    # Issue #1048: read the thermostat's reading through the unit-aware helper so it is compared
    # with comfort_heat/comfort_cool (internal degF) in the same unit.
    current_temp_f: float | None = None
    if climate_entity_id:
        climate_state = hass.states.get(climate_entity_id) if hass is not None else None
        if climate_state is not None:
            current_temp_f = read_attribute_temp_f(climate_state, "current_temperature", _temp_unit)[0]

    state_flags: list[str] = []
    if hvac_mode == "off" and is_hvac_action_active(hvac_action):
        ca_fan_running = is_ca_fan_running(fan_status)
        if not (str(hvac_action).lower() == "fan" and ca_fan_running):
            state_flags.append(
                f"[WARNING] hvac_mode=off but hvac_action={hvac_action!r} -- "
                "possible stale coordinator data or thermostat reporting bug"
            )

    # One-sided thermostat hysteresis (swing_heat_f below / swing_cool_f above the setpoint), learned
    # from real on-transitions (Issue #1234); swing.swing_tolerance() applies THERMAL_SWING_DEFAULT_F
    # until a sample exists and never goes below COMFORT_DEVIATION_FLOOR_F.
    _swing_heat_f: Any = None
    _swing_cool_f: Any = None
    learning = getattr(coordinator, "learning", None)
    if learning is not None and callable(getattr(learning, "get_thermal_model", None)):
        try:
            _build_health = getattr(coordinator, "_build_learning_health", None)
            _health = _build_health() if callable(_build_health) else {}
            _thermal = learning.get_thermal_model(learning_health=_health)
            _swing_heat_f = _thermal.get("swing_heat_f", _thermal.get("swing_heat_f_display"))
            _swing_cool_f = _thermal.get("swing_cool_f", _thermal.get("swing_cool_f_display"))
        except Exception:
            pass

    try:
        ch = float(options.get("comfort_heat", "unknown"))
        cc = float(options.get("comfort_cool", "unknown"))
        if current_temp_f is None:
            raise ValueError("no indoor reading")
        ct = current_temp_f
        # All comparisons are in internal degF (the swing is a degF delta); only the rendered text is
        # in the user's unit.
        _ct_s = format_temp(ct, _temp_unit, 1)
        if ct < ch or ct > cc:
            _below = ct < ch
            _direction = "heat" if _below else "cool"
            _edge = ch if _below else cc
            _gap = (ch - ct) if _below else (ct - cc)
            _held = thermostat_holds_direction(_direction, hvac_mode)
            _swing_tol, _swing_src = swing_tolerance(_swing_heat_f if _below else _swing_cool_f)
            _tol = _swing_tol if _held else COMFORT_DEVIATION_FLOOR_F
            _swing_dec = 2 if _temp_unit == "celsius" else 1  # Issue #1477: 1.5 F = 0.83 C, not 0.8
            _tol_s = (
                f"thermostat {_direction} swing {format_temp_delta(_swing_tol, _temp_unit, _swing_dec)}, "
                f"{_swing_src}; "
                f"hvac_mode={hvac_mode}"
                if _held
                else f"{format_temp_delta(_tol, _temp_unit, 1)}; hvac_mode={hvac_mode} does not hold {_direction}, "
                "so the swing does not apply"
            )
            _cmp, _side, _edge_name = ("<", "below", "comfort_heat") if _below else (">", "above", "comfort_cool")
            # Same composition as the incident scan: beyond the 0.5 F floor AND not inside the swing.
            _flag = _gap > COMFORT_DEVIATION_FLOOR_F and not within_thermostat_hysteresis(
                ct, ch, cc, _swing_heat_f, _swing_cool_f, hvac_mode
            )
            state_flags.append(
                f"{'[FLAG]' if _flag else '[OK]'} Indoor {_ct_s} {_cmp} {_edge_name} "
                f"{format_temp(_edge, _temp_unit, 1)} -- {_side} by {format_temp_delta(_gap, _temp_unit, 1)} "
                f"({'beyond' if _flag else 'within'} tolerance: {_tol_s})"
            )
        else:
            state_flags.append(
                f"[OK] Indoor {_ct_s} is within comfort band "
                f"[{format_temp(ch, _temp_unit, 1)}-{format_temp(cc, _temp_unit, 1)}]"
            )
    except ValueError, TypeError:
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
            # Issue #1048: override_details are stored in internal degF; render in the user's unit.
            _ov_unit = (getattr(coordinator, "config", None) or {}).get("temp_unit", "fahrenheit")
            for i, d in enumerate(override_details, 1):
                t = d.get("time", "??:??")
                old_t = d.get("old_temp")
                new_t = d.get("new_temp")
                direction = d.get("direction", "?")
                magnitude = d.get("magnitude")
                sign = "+" if direction == "up" else "-"
                old_s = format_temp(old_t, _ov_unit, 1) if isinstance(old_t, (int, float)) else "?"
                new_s = format_temp(new_t, _ov_unit, 1) if isinstance(new_t, (int, float)) else "?"
                mag_s = format_temp_delta(magnitude, _ov_unit, 1) if isinstance(magnitude, (int, float)) else "?"
                lines.append(f"  #{i}  {t}  {old_s} -> {new_s}  ({sign}{mag_s}, {direction})")
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
                        f"  Current override:  active since {fmt_day_stamp(local_start)}, "
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
        now_utc = datetime.datetime.now(datetime.UTC)
        raw_event_log: list[Any] = getattr(coordinator, "_event_log", []) or []
        _own_ca = False
        _own_user = False
        fan_ownership_lines: list[str] = []
        _fan_override_count = 0
        # Issue #432: filter to the requested window FIRST, then cap to the most
        # recent 200 for the ownership scan below — capping the raw log before
        # filtering would silently drop older-but-still-in-window ownership
        # transitions whenever recent event volume exceeds 200.
        _filtered_events, _fan_limited = filter_events_by_window(raw_event_log, hours, now_utc, limit=200)
        for entry in _filtered_events:
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
            elif _etype == "fan_manual_override":
                # Issue #1226 mirror: the occupant's own fan-off ends their ownership too (same rule as
                # _render_timeline_events()); otherwise the history kept saying "User owns fan".
                if _own_user:
                    _own_user = False
                    fan_ownership_lines.append(f"  {_ts_str}: Fan ownership cleared (fan_manual_override, fan->off)")
            elif _etype == "fan_cancel":
                if _own_user:
                    _own_user = False
                    fan_ownership_lines.append(f"  {_ts_str}: Fan ownership cleared (fan_cancel)")
            elif (
                _etype
                in (
                    "nat_vent_fan_off",
                    "fan_deactivated",
                    # Issue #957 mirror-check: this loop duplicates
                    # _render_timeline_events()'s fan-ownership tracker (a separate
                    # "FAN OWNERSHIP HISTORY" context section fed directly to the AI
                    # investigator, not routed through EVENT_RENDERERS) and had the
                    # identical staleness gap — these four are also confirmed real
                    # fan-off exits.
                    "nat_vent_comfort_floor_exit",
                    "nat_vent_outdoor_rise_exit",
                    "nat_vent_away_ceiling_exit",
                    "nat_vent_manual_override_exit",
                    # Issue #1037: blind-fan timeout also releases the fan (mirror of the
                    # _render_timeline_events() tracker above).
                    "fan_blind_stopped",
                )
                and _own_ca
            ):
                _own_ca = False
                fan_ownership_lines.append(f"  {_ts_str}: CA released fan ({_etype})")

        lines += [
            "",
            "=== FAN OWNERSHIP HISTORY ===",
            f"  Fan override count (window): {_fan_override_count}",
            *(fan_ownership_lines if fan_ownership_lines else ["  (no fan ownership transitions in window)"]),
        ]
        if _fan_limited:
            lines.append("  NOTE: window contains more than 200 events — showing the most recent 200")
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
        name="activity_sessions",
        tags=frozenset({"events", "system"}),
        priority=1,
        builder=build_activity_sessions_context,
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
        # Issue #920: priority 1, not 2 — comfort_heat/comfort_cool (the values the
        # system prompt's NUMERIC VERIFICATION RULE checks against) are supplied only
        # by this provider. Dropping it from the default (non-deep) investigation would
        # silently break that rule for every default run.
        priority=1,
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
