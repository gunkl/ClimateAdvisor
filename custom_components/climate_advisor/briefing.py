"""Daily briefing generator for Climate Advisor.

Voice rules for the conversational body:
- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- Cause-and-effect: explain *why* before asking the user to do something
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times
- No emoji in body text (emoji only in the structured header and learning section)
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable
from datetime import datetime, time, timedelta

from .automation import _in_sleep_window, compute_pre_cool_target, resolve_pre_cool_modifier
from .classifier import DayClassification
from .const import (
    COLD_DAY_SETBACK_DEPTH_F,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    DEFAULT_SETBACK_HEAT,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_DISABLED,
    OCCUPANCY_SETBACK_MINUTES,
)
from .nat_vent_gate import resolve_comfort_heat
from .temperature import FAHRENHEIT, find_temperature_crossing, format_temp, format_temp_delta

_LOGGER = logging.getLogger(__name__)

# strftime format for 12-hour without leading zero (platform-dependent)
_FMT_HOUR = "%#I:%M %p" if platform.system() == "Windows" else "%-I:%M %p"


def generate_briefing(
    classification: DayClassification,
    comfort_heat: float,
    comfort_cool: float,
    setback_heat: float,
    setback_cool: float,
    wake_time: time,
    sleep_time: time,
    learning_suggestions: list[str] | None = None,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
    verbosity: str = "normal",
    fan_mode: str = FAN_MODE_DISABLED,
    occupancy_mode: str = "home",
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    adaptive_thermal_active: bool = False,
    predicted_indoor_future: list[dict] | None = None,
    predicted_outdoor_future: list[dict] | None = None,
    runtime_config: dict | None = None,
) -> str:
    """Generate the daily climate briefing message.

    Args:
        classification: Today's day classification and recommendations.
        comfort_heat / comfort_cool: User's comfort setpoints.
        setback_heat / setback_cool: User's setback setpoints.
        wake_time / sleep_time: User's schedule.
        learning_suggestions: Any pending suggestions from the learning system.
        debounce_seconds: How long a door/window must be open before HVAC pauses.
        manual_grace_seconds: Grace period after a manual HVAC override.
        automation_grace_seconds: Grace period after Climate Advisor resumes HVAC.
        grace_active: Whether a grace period is currently active.
        grace_source: "manual" or "automation" if a grace period is active.
        verbosity: "tldr_only" (header + table only), "normal" (header + table + trimmed
            body), or "verbose" (header + table + full original body).
        fan_mode: Fan control mode — one of the FAN_MODE_* constants.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
        runtime_config: Full runtime config dict, used only to compute whether tonight's overnight
            pre-cool (``resolve_pre_cool_modifier()``) is expected to run, for the hot-day plan's
            narrative (Issue #558). If omitted, that narrative line is left out rather than
            guessed. Named distinctly from the local ``config`` dict built below (TLDR-table
            shape) to avoid shadowing it.

    Returns:
        Formatted briefing string suitable for email or notification.
    """
    c = classification
    lines: list[str] = []

    _LOGGER.debug(
        "Generating briefing — day_type=%s, trend=%s, comfort_heat=%.0f\u00b0F, comfort_cool=%.0f\u00b0F, verbosity=%s",
        c.day_type,
        c.trend_direction,
        comfort_heat,
        comfort_cool,
        verbosity,
    )

    # TLDR table — used standalone for push notifications, embedded in full briefing
    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": setback_heat,
        "setback_cool": setback_cool,
        "sleep_time": sleep_time,
        "wake_time": wake_time,
    }
    # Issue #535: comfort-floor awareness for the nat-vent cutoff prediction — the real
    # activation gate (decide_nat_vent_gate()) requires indoor > comfort_heat as one of
    # its four conditions, but this predictive curve scan historically only modeled the
    # outdoor-vs-indoor half. When runtime_config is available, resolve comfort_heat_raw/
    # sleep_heat from it and pass an in_sleep_window_fn so _derive_warm_day_events() can
    # also scan for a floor crossing. Omitted (None) when runtime_config isn't provided —
    # callers relying on that (e.g. tests that call generate_briefing() directly without
    # it) keep today's outdoor-only behavior rather than guessing at missing config.
    _comfort_heat_raw = float(runtime_config.get("comfort_heat", comfort_heat)) if runtime_config else None
    _sleep_heat = float(runtime_config.get("sleep_heat", comfort_heat)) if runtime_config else None
    _in_sleep_window_fn: Callable[[datetime], bool] | None = (
        (lambda ts: _in_sleep_window(ts, runtime_config)) if runtime_config else None
    )

    # Single source of truth for warm-day window/AC timing (Issue #518): derive once here
    # and hand the same result to both the header table and the conversational body, so
    # they can never disagree about when windows close or whether AC is expected.
    warm_events = (
        _derive_warm_day_events(
            predicted_indoor=predicted_indoor_future,
            predicted_outdoor=predicted_outdoor_future,
            comfort_cool=comfort_cool,
            comfort_heat_raw=_comfort_heat_raw,
            sleep_heat=_sleep_heat,
            in_sleep_window_fn=_in_sleep_window_fn,
        )
        if c.day_type == DAY_TYPE_WARM and predicted_indoor_future and predicted_outdoor_future
        else None
    )
    # Issue #534: MILD-day window close time was documented (docs/08-COMPUTATION-REFERENCE.md
    # §6d) as ODE-dynamic but never actually wired up — _mild_day_plan() always used the static
    # classifier hour. Uses _derive_warm_day_events() here too (the dead
    # `_derive_natural_vent_events()` sibling — built for a list[float] hour-indexed curve shape
    # that _build_predicted_indoor_future() has never actually produced — was removed in #535).
    mild_events = (
        _derive_warm_day_events(
            predicted_indoor=predicted_indoor_future,
            predicted_outdoor=predicted_outdoor_future,
            comfort_cool=comfort_cool,
            comfort_heat_raw=_comfort_heat_raw,
            sleep_heat=_sleep_heat,
            in_sleep_window_fn=_in_sleep_window_fn,
        )
        if c.day_type == DAY_TYPE_MILD and predicted_indoor_future and predicted_outdoor_future
        else None
    )

    tldr_lines = _generate_tldr_table(
        c,
        config,
        temp_unit=temp_unit,
        bedtime_setback_heat=bedtime_setback_heat,
        bedtime_setback_cool=bedtime_setback_cool,
        occupancy_mode=occupancy_mode,
        warm_events=warm_events,
        mild_events=mild_events,
    )

    if verbosity == "tldr_only":
        briefing_text = "\n".join(tldr_lines).rstrip()
        _LOGGER.debug(
            "Briefing generated (tldr_only) — %d chars",
            len(briefing_text),
        )
        return briefing_text

    # Structured header (kept for full briefing / email)
    # Note: Today/Tomorrow temps and Day Type are already in the TLDR table,
    # so we only include the title and separator to avoid duplication (Issue #52).
    lines.append("\U0001f3e0 Your Home Climate Plan for Today")
    lines.append(f"{'=' * 40}")
    lines.append("")
    lines.extend(tldr_lines)
    lines.append("")

    # Conversational body
    if c.day_type == DAY_TYPE_HOT:
        lines.extend(
            _hot_day_plan(
                c,
                comfort_cool,
                setback_cool,
                wake_time,
                sleep_time,
                fan_mode=fan_mode,
                temp_unit=temp_unit,
                runtime_config=runtime_config,
            )
        )
    elif c.day_type == DAY_TYPE_WARM:
        lines.extend(
            _warm_day_plan(
                c,
                comfort_cool,
                wake_time,
                sleep_time,
                fan_mode=fan_mode,
                temp_unit=temp_unit,
                warm_events=warm_events,
                pre_cool_target=bedtime_setback_cool,
            )
        )
    elif c.day_type == DAY_TYPE_MILD:
        lines.extend(
            _mild_day_plan(c, comfort_heat, wake_time, sleep_time, temp_unit=temp_unit, mild_events=mild_events)
        )
    elif c.day_type == DAY_TYPE_COOL:
        lines.extend(
            _cool_day_plan(
                c,
                comfort_heat,
                setback_heat,
                wake_time,
                sleep_time,
                temp_unit=temp_unit,
                bedtime_setback_heat=bedtime_setback_heat,
            )
        )
    elif c.day_type == DAY_TYPE_COLD:
        lines.extend(
            _cold_day_plan(
                c,
                comfort_heat,
                setback_heat,
                wake_time,
                sleep_time,
                temp_unit=temp_unit,
                bedtime_setback_heat=bedtime_setback_heat,
            )
        )

    _LOGGER.debug("Dispatched %s day plan", c.day_type)

    lines.append("")
    lines.extend(
        _leaving_home_section(c, setback_heat, setback_cool, occupancy_mode=occupancy_mode, temp_unit=temp_unit)
    )
    lines.append("")
    lines.extend(_fresh_air_section(c, comfort_heat, comfort_cool, debounce_seconds, temp_unit=temp_unit))

    # Grace period status — only shown when a grace period is currently active,
    # or when grace periods are configured to a non-default value worth explaining
    grace_lines = _grace_period_section(
        debounce_seconds=debounce_seconds,
        manual_grace_seconds=manual_grace_seconds,
        automation_grace_seconds=automation_grace_seconds,
        grace_active=grace_active,
        grace_source=grace_source,
    )
    if grace_lines:
        lines.append("")
        lines.extend(grace_lines)
        _LOGGER.debug("Grace section included — source=%s", grace_source)

    lines.append("")
    lines.extend(
        _tonight_preview(
            c,
            comfort_heat,
            comfort_cool,
            sleep_time,
            temp_unit=temp_unit,
            adaptive_thermal_active=adaptive_thermal_active,
        )
    )

    # Learning suggestions (kept structured for accept/dismiss clarity)
    if learning_suggestions:
        lines.append("")
        lines.append("\U0001f4a1 Suggestions Based on Recent Patterns")
        lines.append("-" * 40)
        for suggestion in learning_suggestions:
            lines.append(f"  \u2022 {suggestion}")
        lines.append("")
        lines.append("To respond to these suggestions, open the Climate Advisor dashboard and visit the AI tab.")

    briefing_text = "\n".join(lines)
    _LOGGER.debug(
        "Briefing generated — %d chars, %d learning suggestions",
        len(briefing_text),
        len(learning_suggestions) if learning_suggestions else 0,
    )
    if len(briefing_text) > 250:
        _LOGGER.debug("Briefing exceeds 250-char sensor state limit — full text available in sensor attribute")
    return briefing_text


def _generate_tldr_table(
    c: DayClassification,
    config: dict,
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
    bedtime_setback_cool: float | None = None,
    occupancy_mode: str = "home",
    warm_events: dict | None = None,
    mild_events: dict | None = None,
) -> list[str]:
    """Generate a plain-text aligned TLDR summary table.

    Args:
        c: Today's day classification.
        config: Dict with comfort_heat, comfort_cool, setback_heat, setback_cool,
            sleep_time, wake_time keys.
        temp_unit: Display unit — "fahrenheit" or "celsius".
        bedtime_setback_heat: Adaptive bedtime heat setback temperature, if learned.
        bedtime_setback_cool: Adaptive bedtime cool setback temperature, if learned.
        warm_events: Result of _derive_warm_day_events(), pre-computed once in
            generate_briefing() (Issue #518) so the header's window-close time always
            agrees with the conversational body — never re-derive it independently here.
        mild_events: Same, for MILD days (Issue #534) — only one of warm_events/mild_events
            is ever populated for a given classification's day_type.

    Returns:
        List of lines forming a plain-text aligned table.
    """
    comfort_heat = config["comfort_heat"]
    comfort_cool = config["comfort_cool"]
    sleep_time = config["sleep_time"]

    # --- Day Type row ---
    day_type_val = f"{c.day_type.title()} ({format_temp(c.today_high, temp_unit)})"

    # --- HVAC Mode row (Issue #85: show setback temps when away/vacation) ---
    setback_heat = config.get("setback_heat", DEFAULT_SETBACK_HEAT)
    setback_cool = config.get("setback_cool", DEFAULT_SETBACK_COOL)
    if occupancy_mode in ("away", "vacation"):
        if c.hvac_mode == "cool":
            hvac_val = f"Cool at {format_temp(setback_cool, temp_unit)}"
        elif c.hvac_mode == "heat":
            hvac_val = f"Heat at {format_temp(setback_heat, temp_unit)}"
        else:
            hvac_val = f"Off — {occupancy_mode}"
    elif c.hvac_mode == "cool":
        hvac_val = f"Cool at {format_temp(comfort_cool, temp_unit)}"
    elif c.hvac_mode == "heat":
        hvac_val = f"Heat at {format_temp(comfort_heat, temp_unit)}"
    else:
        hvac_val = "Off — windows day"

    # --- Windows row ---
    threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
    if c.windows_recommended and c.window_open_time and c.window_close_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        # Prefer the same ODE-derived cutoff the conversational body uses (Issue #518, extended
        # to MILD days in #534) \u2014 falls back to the classifier's static hour only when no
        # forecast curve exists. warm_events/mild_events are mutually exclusive (populated only
        # for their matching day_type).
        _events = warm_events or mild_events
        _cutoff = _events.get("nat_vent_cutoff") if _events else None
        close_t = _cutoff.strftime(_FMT_HOUR) if _cutoff is not None else c.window_close_time.strftime(_FMT_HOUR)
        windows_val = f"Open {open_t} \u2013 {close_t}"
    elif c.window_opportunity_morning and c.window_opportunity_evening:
        m_start = c.window_opportunity_morning_start.strftime(_FMT_HOUR).lstrip("0")
        m_end = c.window_opportunity_morning_end.strftime(_FMT_HOUR).lstrip("0")
        e_start = c.window_opportunity_evening_start.strftime(_FMT_HOUR).lstrip("0")
        windows_val = f"{m_start}\u2013{m_end} / {e_start}+ (<{format_temp(threshold, temp_unit)})"
    elif c.window_opportunity_morning:
        m_start = c.window_opportunity_morning_start.strftime(_FMT_HOUR).lstrip("0")
        m_end = c.window_opportunity_morning_end.strftime(_FMT_HOUR).lstrip("0")
        windows_val = f"{m_start}\u2013{m_end} (<{format_temp(threshold, temp_unit)})"
    elif c.window_opportunity_evening:
        e_start = c.window_opportunity_evening_start.strftime(_FMT_HOUR).lstrip("0")
        windows_val = f"{e_start} onward (<{format_temp(threshold, temp_unit)})"
    else:
        windows_val = "Closed all day"

    # --- Bedtime Setback row ---
    sleep_str = sleep_time.strftime(_FMT_HOUR)
    if c.hvac_mode == "cool":
        # setback for cool days goes up (warmer is fine when sleeping)
        bedtime_temp = (
            bedtime_setback_cool if bedtime_setback_cool is not None else comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F
        )
        bedtime_val = f"{format_temp(bedtime_temp, temp_unit)} at {sleep_str}"
    elif c.hvac_mode == "heat":
        bedtime_temp = (
            bedtime_setback_heat if bedtime_setback_heat is not None else comfort_heat - DEFAULT_SETBACK_DEPTH_F
        )
        bedtime_val = f"{format_temp(bedtime_temp, temp_unit)} at {sleep_str}"
    else:
        bedtime_val = "No setback"

    # --- Tomorrow row ---
    trend_desc = _trend_description(c, temp_unit=temp_unit)
    tomorrow_val = f"{trend_desc} ({format_temp(c.tomorrow_high, temp_unit)})"

    rows = [
        f"  Day Type: {day_type_val}",
        f"  HVAC Mode: {hvac_val}",
    ]
    # Issue #85: show occupancy status when not home
    if occupancy_mode == "away":
        rows.append("  Occupancy: Away — setback active")
    elif occupancy_mode == "vacation":
        rows.append("  Occupancy: Vacation — deep setback active")
    elif occupancy_mode == "guest":
        rows.append("  Occupancy: Guest — comfort maintained")
    rows.extend(
        [
            f"  Windows: {windows_val}",
            f"  Bedtime Setback: {bedtime_val}",
            f"  Tomorrow: {tomorrow_val}",
        ]
    )
    return rows


def _trend_description(c: DayClassification, temp_unit: str = FAHRENHEIT) -> str:
    """Human-readable trend description."""
    if c.trend_direction == "warming":
        if c.trend_magnitude >= 10:
            return f"Significantly warmer tomorrow (+{format_temp_delta(c.trend_magnitude, temp_unit)})"
        return f"Warming trend (+{format_temp_delta(c.trend_magnitude, temp_unit)})"
    elif c.trend_direction == "cooling":
        if c.trend_magnitude >= 10:
            return f"Significant cold front coming (-{format_temp_delta(c.trend_magnitude, temp_unit)})"
        return f"Cooling trend (-{format_temp_delta(c.trend_magnitude, temp_unit)})"
    return "Stable"


def _hot_day_plan(
    c,
    comfort_cool,
    setback_cool,
    wake_time,
    sleep_time,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
    runtime_config: dict | None = None,
) -> list[str]:
    """Conversational plan for hot days (85\u00b0F+)."""
    threshold = comfort_cool + ECONOMIZER_TEMP_DELTA

    # Issue #558: only claim overnight pre-cool banking when it's actually expected to run
    # tonight (resolve_pre_cool_modifier() \u2014 the same gate handle_pre_cool() uses), and phrase
    # it prospectively ("tonight") rather than asserting a past event that may not have happened
    # (e.g. the home was away overnight, or the trigger hasn't fired yet today).
    _modifier = resolve_pre_cool_modifier(c, runtime_config) if runtime_config is not None else None
    if _modifier is not None:
        _pre_cool_target = compute_pre_cool_target(runtime_config, _modifier)
        opener = (
            f"Tonight I'll pre-cool the house to about {format_temp(_pre_cool_target, temp_unit)} while"
            f" outdoor air is still cool \u2014 that banking strategy cuts energy use over the course"
            f" of the day."
        )
    else:
        opener = (
            f"Today's forecast calls for a high near {format_temp(c.today_high, temp_unit)} \u2014"
            f" I'll hold things at {format_temp(comfort_cool, temp_unit)} through the day."
        )
    lines = [opener]

    has_morning = c.window_opportunity_morning
    has_evening = c.window_opportunity_evening

    if has_morning and has_evening:
        m_start = c.window_opportunity_morning_start.strftime(_FMT_HOUR)
        m_end = c.window_opportunity_morning_end.strftime(_FMT_HOUR)
        e_start = c.window_opportunity_evening_start.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"This morning between {m_start} and {m_end}, if outdoor temps are"
            f" at or below {format_temp(threshold, temp_unit)}, open up for a cross-breeze \u2014"
            f" I'll handle the AC transition."
        )
        lines.append("")
        lines.append(
            f"After {m_end}, close up and keep blinds drawn on sun-facing windows"
            f" (especially west-facing after noon). I'll hold things at"
            f" {format_temp(comfort_cool, temp_unit)}."
        )
        lines.append("")
        lines.append(
            f"From {e_start} onward, if outdoor temps drop back below"
            f" {format_temp(threshold, temp_unit)}, open up again and I'll cut the AC to let"
            f" natural ventilation take over."
        )
    elif has_morning:
        m_start = c.window_opportunity_morning_start.strftime(_FMT_HOUR)
        m_end = c.window_opportunity_morning_end.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"This morning between {m_start} and {m_end}, if outdoor temps are"
            f" at or below {format_temp(threshold, temp_unit)}, open up for a cross-breeze \u2014"
            f" I'll handle the AC transition."
        )
        lines.append("")
        lines.append(
            f"After {m_end}, close up and keep blinds drawn on sun-facing windows"
            f" (especially west-facing after noon). I'll hold things at"
            f" {format_temp(comfort_cool, temp_unit)} for the rest of the day."
        )
    elif has_evening:
        e_start = c.window_opportunity_evening_start.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Today's a keep-it-sealed kind of day. Close the blinds on sun-facing"
            f" windows (especially west-facing ones after noon) and I'll hold things"
            f" at {format_temp(comfort_cool, temp_unit)}."
        )
        lines.append("")
        lines.append(
            f"From {e_start} onward, if outdoor temps drop below {format_temp(threshold, temp_unit)},"
            f" open up and I'll cut the AC to let natural ventilation take over."
        )
    else:
        lines.append("")
        lines.append(
            f"Today's a keep-it-sealed kind of day. Close the blinds on sun-facing"
            f" windows (especially west-facing ones after noon) and I'll handle"
            f" the rest at {format_temp(comfort_cool, temp_unit)}."
        )

    if fan_mode != FAN_MODE_DISABLED and (has_morning or has_evening):
        lines.append("")
        lines.append(
            "When ventilation windows open, I'll use the fan to help pull that cool outdoor air through the house."
        )
    return lines


_CEILING_PRECOOL_FALLBACK_MIN = 120  # default lead time when k_active_cool is unavailable

_NAT_VENT_CUTOFF_MARGIN_F = 1.0  # forecast-hour margin — distinct from the live-control gates'
# own boundary choices (nat_vent_gate.py's strict <, fan_thermostat_decision.py's non-strict >=);
# this is a PREDICTIVE identification of "the hour nat-vent stops being viable", not a live
# control decision, so a small conservative buffer is appropriate here specifically.


def _nat_vent_cutoff_reached(outdoor_temp: float, indoor_temp: float) -> bool:
    """Architecture-reset (Issue #429 consolidation): the shared outdoor-vs-indoor
    predicate _derive_warm_day_events() hand-rolled as `outdoor >= indoor - 1.0` —
    now a single shared definition. This is only half of the real activation gate's
    predicate — see the comfort-floor scan in _derive_warm_day_events() (Issue #535)."""
    return outdoor_temp >= indoor_temp - _NAT_VENT_CUTOFF_MARGIN_F


def _derive_warm_day_events(
    predicted_indoor: list[dict] | None,
    predicted_outdoor: list[dict] | None,
    comfort_cool: float,
    k_active_cool: float | None = None,
    comfort_heat_raw: float | None = None,
    sleep_heat: float | None = None,
    in_sleep_window_fn: Callable[[datetime], bool] | None = None,
) -> dict:
    """Derive warm-day timing events from ODE predicted curves.

    Args:
        comfort_heat_raw, sleep_heat, in_sleep_window_fn: optional (Issue #535) — when
            all three are provided, nat_vent_cutoff also scans for the comfort-floor
            crossing the real activation gate (decide_nat_vent_gate() in
            nat_vent_gate.py) requires (`indoor > comfort_heat`) but this predictive
            scan previously never modeled. When omitted, behavior is unchanged from
            before #535 (outdoor-crossing only).

    Returns a dict with keys:
      nat_vent_cutoff: datetime | None — earlier of the outdoor-crossing and (if the
          three optional params are given) comfort-floor crossing
      nat_vent_cutoff_reason: str | None — "outdoor_rise" or "comfort_floor", whichever
          produced nat_vent_cutoff; None if nat_vent_cutoff is None
      ceiling_breach_time: datetime | None — first hour indoor > comfort_cool
      precool_start_time: datetime | None — ceiling_breach_time minus computed lead
      any_nat_vent_window: bool — True if outdoor < indoor at any point
      nat_vent_recovers: bool — True if outdoor drops back below indoor after cutoff
      recovery_time: datetime | None — first timestamp after cutoff where outdoor < indoor again
    """
    result: dict = {
        "nat_vent_cutoff": None,
        "nat_vent_cutoff_reason": None,
        "ceiling_breach_time": None,
        "precool_start_time": None,
        "any_nat_vent_window": False,
        "nat_vent_recovers": False,
        "recovery_time": None,
    }

    if not predicted_indoor or not predicted_outdoor:
        return result

    # Issue #528: each crossing is found via find_temperature_crossing(), which aligns
    # the two curves by matching ISO timestamp — not list position — so a mismatch in
    # how/when the two curves were built (different "now" filter boundaries, one cached
    # from an earlier cycle vs. the other rebuilt fresh) can no longer silently shift
    # the pairing the way the previous zip()-by-index implementation did. See
    # docs/08-COMPUTATION-REFERENCE.md's warm-day-events note for the production
    # incident this replaced.
    result["any_nat_vent_window"] = (
        find_temperature_crossing(predicted_indoor, predicted_outdoor, lambda _ts, o, i: o < i) is not None
    )

    outdoor_crossing = find_temperature_crossing(
        predicted_indoor, predicted_outdoor, lambda _ts, o, i: _nat_vent_cutoff_reached(o, i)
    )

    # Issue #535: comfort-floor crossing — the real activation gate (decide_nat_vent_gate())
    # requires indoor > comfort_heat as one of its four conditions; this predictive scan
    # previously never modeled that term. Only reads the indoor curve (no outdoor pairing
    # needed — same shape as ceiling_breach_time below), but still requires a matching
    # entry in predicted_outdoor via find_temperature_crossing() so it can only fire at a
    # timestamp both curves actually cover.
    floor_crossing = None
    if comfort_heat_raw is not None and sleep_heat is not None and in_sleep_window_fn is not None:
        floor_crossing = find_temperature_crossing(
            predicted_indoor,
            predicted_outdoor,
            lambda ts, _o, i: i <= resolve_comfort_heat(comfort_heat_raw, sleep_heat, in_sleep_window_fn(ts)),
        )

    if outdoor_crossing is not None and (floor_crossing is None or outdoor_crossing <= floor_crossing):
        result["nat_vent_cutoff"] = outdoor_crossing
        result["nat_vent_cutoff_reason"] = "outdoor_rise"
    elif floor_crossing is not None:
        result["nat_vent_cutoff"] = floor_crossing
        result["nat_vent_cutoff_reason"] = "comfort_floor"

    # ceiling_breach_time only reads the indoor curve — no pairing needed.
    for entry in predicted_indoor:
        ts_str = entry.get("ts")
        i_temp = entry.get("temp")
        if ts_str is None or i_temp is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if float(i_temp) > comfort_cool:
            result["ceiling_breach_time"] = ts
            break

    # precool_start_time = ceiling_breach_time - lead_time
    if result["ceiling_breach_time"] is not None:
        t_in_now = predicted_indoor[0].get("temp", comfort_cool - 2.0)
        if k_active_cool is not None and abs(k_active_cool) > 0:
            lead_min = ((comfort_cool - t_in_now) / abs(k_active_cool)) * 60 * 1.3
        else:
            lead_min = float(_CEILING_PRECOOL_FALLBACK_MIN)
        lead_min = max(30.0, min(240.0, lead_min))
        result["precool_start_time"] = result["ceiling_breach_time"] - timedelta(minutes=lead_min)

    # nat_vent_recovers / recovery_time: outdoor drops back below indoor AFTER the cutoff
    if result["nat_vent_cutoff"] is not None:
        result["recovery_time"] = find_temperature_crossing(
            predicted_indoor, predicted_outdoor, lambda _ts, o, i: o < i, after=result["nat_vent_cutoff"]
        )
        result["nat_vent_recovers"] = result["recovery_time"] is not None

    _LOGGER.debug(
        "WarmDayEvents: nat_vent_cutoff=%s (%s), ceiling_breach=%s, precool_start=%s, recovers=%s, recovery_time=%s",
        result["nat_vent_cutoff"],
        result["nat_vent_cutoff_reason"],
        result["ceiling_breach_time"],
        result["precool_start_time"],
        result["nat_vent_recovers"],
        result["recovery_time"],
    )

    return result


def _warm_day_plan(
    c,
    comfort_cool,
    wake_time,
    sleep_time,
    fan_mode: str = FAN_MODE_DISABLED,
    temp_unit: str = FAHRENHEIT,
    predicted_indoor_future: list[dict] | None = None,
    predicted_outdoor_future: list[dict] | None = None,
    pre_cool_target: float | None = None,
    warm_events: dict | None = None,
) -> list[str]:
    """Conversational plan for warm days (75-85\u00b0F).

    Issue #518: window/AC timing is derived once in generate_briefing() and passed
    in as `warm_events` so this never disagrees with the header table. Falls back to
    deriving it locally only when called directly with raw prediction curves (tests).
    """
    lines = []

    _events = warm_events
    if _events is None and predicted_indoor_future and predicted_outdoor_future:
        _events = _derive_warm_day_events(
            predicted_indoor=predicted_indoor_future,
            predicted_outdoor=predicted_outdoor_future,
            comfort_cool=comfort_cool,
        )
    _nat_vent_cutoff = _events["nat_vent_cutoff"] if _events else None
    _nat_vent_cutoff_reason = _events.get("nat_vent_cutoff_reason") if _events else None
    _ceiling_breach = _events["ceiling_breach_time"] if _events else None
    _nat_vent_recovers = _events["nat_vent_recovers"] if _events else False

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        if _nat_vent_cutoff is not None:
            close_t = _nat_vent_cutoff.strftime(_FMT_HOUR)
            # Issue #535: two distinct reasons the cutoff can fire \u2014 outdoor air rising
            # above indoor (the original predicate), or indoor forecast to reach the
            # comfort floor first. Same close time either way; different sentence why.
            if _nat_vent_cutoff_reason == "comfort_floor":
                close_sentence = f"Close up at {close_t} to hold the heat in."
            else:
                close_sentence = f"Close up at {close_t} \u2014 after that the outdoor air will be warmer than inside."
            lines.append(f"Open windows around {open_t} to catch the cool morning air. {close_sentence}")
        else:
            lines.append(
                f"Open windows around {open_t} to catch the cool morning air"
                f" \u2014 cross-ventilation keeps things comfortable without the AC."
            )
    else:
        lines.append("HVAC is off this morning.")

    if fan_mode != FAN_MODE_DISABLED:
        lines.append("I'll use the fan to boost cross-ventilation when windows are open.")

    lines.append("")

    # Issue #518: this used to independently promise "I'll run the AC starting around
    # X \u2014 no action needed from you", ignoring window state entirely \u2014 contradicting
    # the real automation guard (automation.py apply_classification()'s DEFER_PAUSED
    # branch actually suppresses AC the whole time a window is open) and duplicating
    # _fresh_air_section()'s already-correct, debounce-aware version of this same fact.
    # This section now only states the forecast and ties the AC to windows being
    # closed; _fresh_air_section owns the debounce/pause mechanics, so it's said once.
    if _ceiling_breach is not None:
        breach_t = _ceiling_breach.strftime(_FMT_HOUR)
        lines.append(
            f"Indoor temps are forecast to reach"
            f" {format_temp(comfort_cool, temp_unit)} around {breach_t}."
            f" Once windows are closed, the AC will step in automatically if it's"
            f" needed to hold that ceiling."
        )
    elif c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        lines.append(
            f"Close up by {close_t} before outdoor temps climb \u2014 seal the cool"
            f" air inside so the AC can take over above {format_temp(comfort_cool, temp_unit)}."
        )
    else:
        lines.append(
            f"The AC will step in above {format_temp(comfort_cool, temp_unit)} as a safety net if"
            f" needed, but with good airflow you probably won't need it."
        )

    # Pre-cool night mention: warming trend + a lower sleep ceiling is planned
    if pre_cool_target is not None and getattr(c, "setback_modifier", 0.0) < 0:
        lines.append("")
        lines.append(
            f"Tonight I'll cool the home to {format_temp(pre_cool_target, temp_unit)} while"
            f" you sleep to build up cold thermal mass before tomorrow's heat — so the house"
            f" coasts longer before the AC needs to kick in."
        )

    if _nat_vent_recovers and _events is not None:
        _recovery_ts = _events["recovery_time"]
        if _recovery_ts is not None:
            rec_t = _recovery_ts.strftime(_FMT_HOUR)
            # Issue #518: only claim "I'll turn off the AC" when the AC could
            # plausibly have engaged first (breach predicted before recovery) \u2014
            # otherwise this contradicted itself by canceling an action that was
            # never actually started.
            if _ceiling_breach is not None and _ceiling_breach < _recovery_ts:
                lines.append(
                    f"Reopen windows around {rec_t} when the evening air cools back down \u2014 I'll turn off the AC."
                )
            else:
                lines.append(f"Reopen windows around {rec_t} when the evening air cools back down.")

    return lines


def _mild_day_plan(
    c, comfort_heat, wake_time, sleep_time, temp_unit: str = FAHRENHEIT, mild_events: dict | None = None
) -> list[str]:
    """Conversational plan for mild days (60-74\u00b0F)."""
    lines = [
        f"A day where the house practically takes care of itself. I warmed to"
        f" {format_temp(comfort_heat, temp_unit)} before sunrise \u2014 now HVAC is off and the weather"
        f" does the rest.",
    ]

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Open south and east windows around {open_t} for a natural"
            f" cross-breeze that freshens the air and warms the house for free."
        )

    # Issue #534: prefer the ODE-derived cutoff when available (same forecast already validated
    # accurate for warm days), matching docs/08-COMPUTATION-REFERENCE.md \u00a76d \u2014 falls back to the
    # classifier's static hour only when no forecast curve exists (fresh install, uncalibrated
    # model), same fallback pattern _generate_tldr_table() already uses for warm days.
    _mild_cutoff = mild_events.get("nat_vent_cutoff") if mild_events else None
    _close_time = _mild_cutoff if _mild_cutoff is not None else c.window_close_time
    if _close_time:
        close_t = _close_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Close up by {close_t} to trap the warmth. If it dips below"
            f" {format_temp(comfort_heat - 2, temp_unit)} tonight, I'll bring the heater back on"
            f" automatically."
        )

    return lines


def _cool_day_plan(
    c,
    comfort_heat,
    setback_heat,
    wake_time,
    sleep_time,
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
) -> list[str]:
    """Conversational plan for cool days (45-59\u00b0F)."""
    setback_display = (
        bedtime_setback_heat if bedtime_setback_heat is not None else comfort_heat - DEFAULT_SETBACK_DEPTH_F
    )
    return [
        f"Heater day \u2014 too cool outside for windows. I'll hold {format_temp(comfort_heat, temp_unit)}"
        f" through the morning, ease back a couple degrees midday to ride any solar"
        f" gain, then return to {format_temp(comfort_heat, temp_unit)} as the sun drops.",
        "",
        f"At bedtime I'll set back to {format_temp(setback_display, temp_unit)}"
        " \u2014 most people sleep better a little cooler.",
    ]


def _cold_day_plan(
    c,
    comfort_heat,
    setback_heat,
    wake_time,
    sleep_time,
    temp_unit: str = FAHRENHEIT,
    bedtime_setback_heat: float | None = None,
) -> list[str]:
    """Conversational plan for cold days (below 45\u00b0F)."""
    lines = [
        "Cold day \u2014 heater runs all day. Help it out: close north-side curtains,"
        " open south-facing ones for free solar heat, and minimize time holding"
        " exterior doors open.",
    ]

    if c.pre_condition and c.trend_direction == "cooling":
        target = comfort_heat + (c.pre_condition_target or 3)
        lines.append("")
        lines.append(
            f"Tomorrow's even colder, so I'm banking extra heat this evening \u2014"
            f" I'll bump to {format_temp(target, temp_unit)} around 7pm for a couple hours."
            f" If the house feels extra warm before bed, that's on purpose."
        )

    setback_display = (
        bedtime_setback_heat if bedtime_setback_heat is not None else comfort_heat - COLD_DAY_SETBACK_DEPTH_F
    )
    lines.append("")
    lines.append(
        f"Tonight I'm using a conservative setback \u2014 {format_temp(setback_display, temp_unit)}"
        f" instead of the usual {format_temp(setback_heat, temp_unit)}. When it's this cold, a"
        f" deeper setback takes too long to recover from in the morning."
    )

    return lines


def _leaving_home_section(
    c, setback_heat, setback_cool, occupancy_mode: str = "home", temp_unit: str = FAHRENHEIT
) -> list[str]:
    """Conversational section about what happens when they leave.

    Args:
        c: Today's day classification.
        setback_heat: Heating setback temperature.
        setback_cool: Cooling setback temperature.
        occupancy_mode: Current occupancy state — "home", "away", "guest", or "vacation".
        temp_unit: Display unit — "fahrenheit" or "celsius".
    """
    if occupancy_mode == "vacation":
        return [
            "While you're on vacation, I'm keeping the house at a deeper"
            " energy-saving setback to save energy. Comfort temperatures will be"
            " restored when you return.",
        ]
    elif occupancy_mode == "guest":
        return [
            "Guests are visiting \u2014 maintaining full comfort temperatures."
            " Away setbacks are disabled while guest mode is active.",
        ]
    elif occupancy_mode == "away":
        if c.hvac_mode == "cool":
            return [
                f"You're currently away. I've applied setback temperatures,"
                f" letting the house drift up to {format_temp(setback_cool, temp_unit)} to save"
                f" energy. Comfort will be restored when you return \u2014 give it"
                f" 20 to 30 minutes to feel normal again.",
            ]
        elif c.hvac_mode == "heat":
            return [
                f"You're currently away. I've dropped to {format_temp(setback_heat, temp_unit)}"
                f" to save energy. Comfort will be restored when you return \u2014"
                f" should take 20 to 30 minutes depending on how long you've been gone.",
            ]
        else:
            return [
                "You're currently away. The HVAC is off today, so not much"
                " changes. If it kicks on as a safety net, it'll set back on its own.",
            ]
    else:
        # occupancy_mode == "home" — default hypothetical text
        if c.hvac_mode == "cool":
            return [
                f"If you head out, no worries. After about {OCCUPANCY_SETBACK_MINUTES} minutes I'll let the"
                f" house drift up to {format_temp(setback_cool, temp_unit)} to save energy. When you're"
                f" back, I'll pull it right back down \u2014 give it 20 to 30 minutes to"
                f" feel normal again.",
            ]
        elif c.hvac_mode == "heat":
            return [
                f"If you head out, I'll drop to {format_temp(setback_heat, temp_unit)} after about"
                f" {OCCUPANCY_SETBACK_MINUTES} minutes. When you get back, I'll warm things right up \u2014 should"
                f" take 20 to 30 minutes depending on how long you were gone.",
            ]
        else:
            return [
                "If you head out, nothing really changes today \u2014 the HVAC is off."
                " If it was running as a safety net, it'll set back on its own.",
            ]


def _fresh_air_section(
    c,
    comfort_heat: float,
    comfort_cool: float,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    temp_unit: str = FAHRENHEIT,
    natural_vent_active: bool = False,
    current_outdoor_temp: float | None = None,
) -> list[str]:
    """User-centric section about opening windows/doors for fresh air.

    Affirms the user's choice first, then explains impact and recovery.
    Varies by HVAC mode since the impact differs significantly.
    Uses the configured debounce duration so the timing matches actual behavior.
    """
    debounce_minutes = max(1, debounce_seconds // 60)
    debounce_desc = f"{debounce_minutes} minute" if debounce_minutes == 1 else f"{debounce_minutes} minutes"

    if natural_vent_active and current_outdoor_temp is not None:
        return [
            f"Windows are open and outdoor air is {format_temp(current_outdoor_temp, temp_unit)} \u2014"
            f" I'll use the fan to hold your {format_temp(comfort_cool, temp_unit)} target"
            f" without running the AC. Once you close up, I'll resume normal cooling."
        ]

    if c.hvac_mode == "cool":
        return [
            f"If you want to crack a window for some fresh air, no problem \u2014"
            f" it's your house. I'll keep the AC running for a bit in"
            f" case it's just a quick thing, but if it stays open past {debounce_desc}"
            f" I'll shut the AC off so you're not cooling the outdoors. Once you"
            f" close up, I'll fire the AC back up right away. Just know that on a"
            f" day like today it may take a bit longer to pull back down to"
            f" {format_temp(comfort_cool, temp_unit)}, so if you want to minimize the impact, shorter"
            f" is better \u2014 and try to keep other windows and doors shut while"
            f" you've got one open.",
        ]
    elif c.hvac_mode == "heat":
        return [
            f"If you want to open a window for some fresh air, no problem \u2014"
            f" go for it. I'll keep the heat running for a bit in case"
            f" you're just airing things out, but if it stays open past {debounce_desc}"
            f" I'll turn the heat off so we're not heating the neighborhood. Once"
            f" you close up, the heat kicks right back on. It'll take a little"
            f" extra energy to warm back up, so if you want to minimize the"
            f" impact, a quick burst of fresh air works great \u2014 and closing doors"
            f" to the room with the open window helps keep the rest of the house"
            f" comfortable while you do it.",
        ]
    else:
        return [
            f"If you want to open a window for some fresh air, go for it \u2014"
            f" the HVAC is off today so there's no energy impact at all."
            f" Enjoy the breeze. If the system does need to kick on as a safety"
            f" net later and a window is still open, I'll give it {debounce_desc}"
            f" and then pause until you close up.",
        ]


def _grace_period_section(
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
) -> list[str]:
    """Explain active grace periods and configured timings.

    Only included in the briefing when a grace period is currently active,
    so users aren't surprised that door/window sensors aren't pausing HVAC.
    Returns an empty list when there is nothing noteworthy to report.
    """
    if not grace_active or not grace_source:
        return []

    if grace_source == "manual":
        grace_minutes = max(1, manual_grace_seconds // 60)
        grace_desc = f"{grace_minutes} minute" if grace_minutes == 1 else f"{grace_minutes} minutes"
        return [
            f"One heads-up for this morning: you manually turned the HVAC back on"
            f" earlier, so I'm in a {grace_desc} hands-off window right now. During"
            f" that window, opening a door or window won't trigger a pause \u2014 I'm"
            f" giving you space to settle in without the system jumping in. Once the"
            f" window closes, door/window sensing goes back to normal."
        ]
    else:
        # automation grace
        grace_minutes = max(1, automation_grace_seconds // 60)
        grace_desc = f"{grace_minutes} minute" if grace_minutes == 1 else f"{grace_minutes} minutes"
        return [
            f"One heads-up: I just resumed the HVAC after all the doors and windows"
            f" closed, so I'm in a {grace_desc} settling period. During that time,"
            f" opening a door or window briefly won't immediately pause things again \u2014"
            f" this prevents the system from cycling on and off if you're moving in"
            f" and out. After the settling period, normal door/window sensing resumes."
        ]


def _tonight_preview(
    c,
    comfort_heat,
    comfort_cool,
    sleep_time,
    temp_unit: str = FAHRENHEIT,
    adaptive_thermal_active: bool = False,
) -> list[str]:
    """Conversational preview of tonight and tomorrow based on trend."""
    _LOGGER.debug(
        "Tonight preview \u2014 trend=%s, magnitude=%.1f\u00b0F",
        c.trend_direction,
        c.trend_magnitude,
    )
    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        lines = [
            f"Looking ahead \u2014 tomorrow's warmer at {format_temp(c.tomorrow_high, temp_unit)}, so"
            f" I'm going to set back a bit more aggressively tonight. Less"
            f" heating needed means energy saved while you sleep.",
        ]
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        lines = [
            f"Looking ahead \u2014 tomorrow's cooler at {format_temp(c.tomorrow_high, temp_unit)}, so"
            f" I'll bank some extra warmth this evening and go easy on the"
            f" overnight setback. If the house feels a touch warmer than usual"
            f" before bed, that's intentional.",
        ]
    else:
        lines = [
            f"Tomorrow looks pretty similar to today \u2014 {format_temp(c.tomorrow_high, temp_unit)}"
            f" for a high. Nothing special planned overnight.",
        ]
    if adaptive_thermal_active and c.hvac_mode in ("heat", "cool"):
        lines.append("Bedtime setback and pre-heat timing are tuned to your home's actual heating performance.")
    return lines
