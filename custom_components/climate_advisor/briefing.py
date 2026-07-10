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
from datetime import datetime, time, timedelta

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
from .temperature import FAHRENHEIT, format_temp, format_temp_delta

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
    tldr_lines = _generate_tldr_table(
        c,
        config,
        temp_unit=temp_unit,
        bedtime_setback_heat=bedtime_setback_heat,
        bedtime_setback_cool=bedtime_setback_cool,
        occupancy_mode=occupancy_mode,
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
            _hot_day_plan(c, comfort_cool, setback_cool, wake_time, sleep_time, fan_mode=fan_mode, temp_unit=temp_unit)
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
                predicted_indoor_future=predicted_indoor_future,
                predicted_outdoor_future=predicted_outdoor_future,
                pre_cool_target=bedtime_setback_cool,
            )
        )
    elif c.day_type == DAY_TYPE_MILD:
        lines.extend(_mild_day_plan(c, comfort_heat, wake_time, sleep_time, temp_unit=temp_unit))
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
) -> list[str]:
    """Generate a plain-text aligned TLDR summary table.

    Args:
        c: Today's day classification.
        config: Dict with comfort_heat, comfort_cool, setback_heat, setback_cool,
            sleep_time, wake_time keys.
        temp_unit: Display unit — "fahrenheit" or "celsius".
        bedtime_setback_heat: Adaptive bedtime heat setback temperature, if learned.
        bedtime_setback_cool: Adaptive bedtime cool setback temperature, if learned.

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
            hvac_val = f"Cool at {format_temp(setback_cool, temp_unit)} (setback — {occupancy_mode})"
        elif c.hvac_mode == "heat":
            hvac_val = f"Heat at {format_temp(setback_heat, temp_unit)} (setback — {occupancy_mode})"
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
        close_t = c.window_close_time.strftime(_FMT_HOUR)
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

    label_w = 17
    rows = [
        f"  {'Day Type:':<{label_w}} {day_type_val}",
        f"  {'HVAC Mode:':<{label_w}} {hvac_val}",
    ]
    # Issue #85: show occupancy status when not home
    if occupancy_mode == "away":
        rows.append(f"  {'Occupancy:':<{label_w}} Away — setback active")
    elif occupancy_mode == "vacation":
        rows.append(f"  {'Occupancy:':<{label_w}} Vacation — deep setback active")
    elif occupancy_mode == "guest":
        rows.append(f"  {'Occupancy:':<{label_w}} Guest — comfort maintained")
    rows.extend(
        [
            f"  {'Windows:':<{label_w}} {windows_val}",
            f"  {'Bedtime Setback:':<{label_w}} {bedtime_val}",
            f"  {'Tomorrow:':<{label_w}} {tomorrow_val}",
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
) -> list[str]:
    """Conversational plan for hot days (85\u00b0F+)."""
    threshold = comfort_cool + ECONOMIZER_TEMP_DELTA

    lines = [
        f"I pre-cooled to {format_temp(comfort_cool - 2, temp_unit)} this morning while outdoor air"
        f" was still cool \u2014 that banking strategy cuts energy use significantly over"
        f" the course of the day.",
    ]

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
    """Architecture-reset (Issue #429 consolidation): the shared predicate both
    _derive_warm_day_events() and _derive_natural_vent_events() independently
    hand-rolled as `outdoor >= indoor - 1.0` — now a single shared definition."""
    return outdoor_temp >= indoor_temp - _NAT_VENT_CUTOFF_MARGIN_F


def _derive_warm_day_events(
    predicted_indoor: list[dict] | None,
    predicted_outdoor: list[dict] | None,
    comfort_cool: float,
    k_active_cool: float | None = None,
) -> dict:
    """Derive warm-day timing events from ODE predicted curves.

    Returns a dict with keys:
      nat_vent_cutoff: datetime | None — first hour outdoor >= indoor - 1 F
      ceiling_breach_time: datetime | None — first hour indoor > comfort_cool
      precool_start_time: datetime | None — ceiling_breach_time minus computed lead
      any_nat_vent_window: bool — True if outdoor < indoor at any point
      nat_vent_recovers: bool — True if outdoor drops back below indoor after cutoff
    """
    result: dict = {
        "nat_vent_cutoff": None,
        "ceiling_breach_time": None,
        "precool_start_time": None,
        "any_nat_vent_window": False,
        "nat_vent_recovers": False,
    }

    if not predicted_indoor or not predicted_outdoor:
        return result

    # Build aligned (ts, indoor_temp, outdoor_temp) triples by matching timestamps
    pairs = []
    for i_entry, o_entry in zip(predicted_indoor, predicted_outdoor, strict=False):
        i_temp = i_entry.get("temp")
        o_temp = o_entry.get("temp")
        ts_str = i_entry.get("ts")
        if i_temp is None or o_temp is None or ts_str is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        pairs.append((ts, float(i_temp), float(o_temp)))

    if not pairs:
        return result

    # Any nat-vent window (outdoor < indoor at any point)
    result["any_nat_vent_window"] = any(o < i for _, i, o in pairs)

    # nat_vent_cutoff: first entry where outdoor >= indoor - 1 F
    for ts, i_temp, o_temp in pairs:
        if _nat_vent_cutoff_reached(o_temp, i_temp):
            result["nat_vent_cutoff"] = ts
            break

    # ceiling_breach_time: first entry where indoor > comfort_cool
    for ts, i_temp, _o_temp in pairs:
        if i_temp > comfort_cool:
            result["ceiling_breach_time"] = ts
            break

    # precool_start_time = ceiling_breach_time - lead_time
    if result["ceiling_breach_time"] is not None:
        t_in_now = pairs[0][1] if pairs else comfort_cool - 2.0
        if k_active_cool is not None and abs(k_active_cool) > 0:
            lead_min = ((comfort_cool - t_in_now) / abs(k_active_cool)) * 60 * 1.3
        else:
            lead_min = float(_CEILING_PRECOOL_FALLBACK_MIN)
        lead_min = max(30.0, min(240.0, lead_min))
        result["precool_start_time"] = result["ceiling_breach_time"] - timedelta(minutes=lead_min)

    # nat_vent_recovers: outdoor drops back below indoor AFTER the cutoff
    if result["nat_vent_cutoff"] is not None:
        cutoff_ts = result["nat_vent_cutoff"]
        for ts, i_temp, o_temp in pairs:
            if ts > cutoff_ts and o_temp < i_temp:
                result["nat_vent_recovers"] = True
                break

    _LOGGER.debug(
        "WarmDayEvents: nat_vent_cutoff=%s, ceiling_breach=%s, precool_start=%s, recovers=%s",
        result["nat_vent_cutoff"],
        result["ceiling_breach_time"],
        result["precool_start_time"],
        result["nat_vent_recovers"],
    )

    return result


def _derive_natural_vent_events(
    predicted_indoor_future: list,
    predicted_outdoor_future: list,
    comfort_cool: float,
    k_active_cool,
) -> dict:
    """Derive natural-ventilation timing events from ODE predicted curves (hour-indexed lists).

    Simpler companion to _derive_warm_day_events for use with list[float] hour-indexed
    arrays (as produced by _build_predicted_indoor_future / MILD day ODE calls).

    Args:
        predicted_indoor_future: list[float] where index = local hour (0–23).
        predicted_outdoor_future: list[float] where index = local hour (0–23).
        comfort_cool: Upper comfort bound (°F); used for ceiling breach detection.
        k_active_cool: Cooling rate (°F/hr) for precool lead time, or None.

    Returns a dict with keys:
        nat_vent_cutoff: int | None — first hour h where predicted_outdoor_future[h] >= predicted_indoor_future[h] - 1.0
        ceiling_breach_hour: int | None — first hour where indoor > comfort_cool
        any_nat_vent_window: bool — True if outdoor < indoor at any hour
    """
    result: dict = {
        "nat_vent_cutoff": None,
        "ceiling_breach_hour": None,
        "any_nat_vent_window": False,
    }

    if not predicted_indoor_future or not predicted_outdoor_future:
        return result

    n = min(len(predicted_indoor_future), len(predicted_outdoor_future))
    if n == 0:
        return result

    # Any nat-vent window (outdoor < indoor at any hour)
    result["any_nat_vent_window"] = any(predicted_outdoor_future[h] < predicted_indoor_future[h] for h in range(n))

    # nat_vent_cutoff: first hour where outdoor >= indoor - 1.0 °F
    for h in range(n):
        if _nat_vent_cutoff_reached(predicted_outdoor_future[h], predicted_indoor_future[h]):
            result["nat_vent_cutoff"] = h
            break

    # ceiling_breach_hour: first hour where indoor > comfort_cool
    for h in range(n):
        if predicted_indoor_future[h] > comfort_cool:
            result["ceiling_breach_hour"] = h
            break

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
) -> list[str]:
    """Conversational plan for warm days (75-85\u00b0F)."""
    lines = []

    # Derive ODE timing events if prediction data is available
    _events = (
        _derive_warm_day_events(
            predicted_indoor=predicted_indoor_future,
            predicted_outdoor=predicted_outdoor_future,
            comfort_cool=comfort_cool,
        )
        if predicted_indoor_future and predicted_outdoor_future
        else None
    )
    _nat_vent_cutoff = _events["nat_vent_cutoff"] if _events else None
    _ceiling_breach = _events["ceiling_breach_time"] if _events else None
    _precool_start = _events["precool_start_time"] if _events else None
    _nat_vent_recovers = _events["nat_vent_recovers"] if _events else False

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        if _nat_vent_cutoff is not None:
            close_t = _nat_vent_cutoff.strftime(_FMT_HOUR)
            lines.append(
                f"Open windows around {open_t} to catch the cool morning air."
                f" Close up at {close_t} \u2014 after that the outdoor air will be"
                f" warmer than inside. I'll take over with AC as needed."
            )
        else:
            lines.append(
                f"Open windows around {open_t} to catch the cool morning air"
                f" \u2014 cross-ventilation keeps things comfortable without the AC."
            )
    else:
        lines.append("HVAC is off this morning \u2014 no action needed.")

    if fan_mode != FAN_MODE_DISABLED:
        lines.append("I'll use the fan to boost cross-ventilation when windows are open.")

    lines.append("")

    if _ceiling_breach is not None:
        breach_t = _ceiling_breach.strftime(_FMT_HOUR)
        if _precool_start is not None:
            precool_t = _precool_start.strftime(_FMT_HOUR)
            lines.append(
                f"Indoor temps are forecast to reach"
                f" {format_temp(comfort_cool, temp_unit)} around {breach_t}."
                f" I'll run the AC starting around {precool_t} \u2014 no action needed from you."
            )
        else:
            lines.append(
                f"Indoor temps are forecast to reach"
                f" {format_temp(comfort_cool, temp_unit)} around {breach_t}."
                f" I'll run the AC as needed to keep things comfortable."
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
        # Find recovery time from the curves
        _recovery_ts = None
        cutoff = _events["nat_vent_cutoff"]
        if cutoff is not None and predicted_indoor_future and predicted_outdoor_future:
            for i_e, o_e in zip(predicted_indoor_future, predicted_outdoor_future, strict=False):
                try:
                    ts = datetime.fromisoformat(i_e["ts"])
                except (KeyError, ValueError, TypeError):
                    continue
                if ts > cutoff and o_e.get("temp", 99) < i_e.get("temp", 0):
                    _recovery_ts = ts
                    break
        if _recovery_ts is not None:
            rec_t = _recovery_ts.strftime(_FMT_HOUR)
            lines.append(
                f"Reopen windows around {rec_t} when the evening air cools back down \u2014 I'll turn off the AC."
            )

    return lines


def _mild_day_plan(c, comfort_heat, wake_time, sleep_time, temp_unit: str = FAHRENHEIT) -> list[str]:
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

    if c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
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
    if adaptive_thermal_active:
        lines.append("Bedtime setback and pre-heat timing are tuned to your home's actual heating performance.")
    return lines
