"""Operation timeline (Issue #1230 R3a): one ordered plan of what CA will do and what the occupant should do.

Occupant view: today the plan is worked out separately by Next Action, Next Automation, the briefing, Status and the
chart, and they can disagree (#1276: heat on at the floor while the card said "open windows"). This module builds ONE
list of the next 24-48 h from one snapshot: what Climate Advisor will do and when (wake/sleep band, setback, pre-cool,
fan session), what the occupant should do and when (open or close the windows, shown 30 min ahead), and the predicted
temperature path under that plan. R3a only builds it, in shadow: nothing reads it for a decision or a display.

Pure and headless (no Home Assistant, no clock, no I/O): every input is in ``TimelineSnapshot``, so the coordinator, the
replay tool and the tests all run the same code. The only collaborators are the existing pure rules it must agree with:
``temperature.outdoor_direction_ok_for_thermostat`` (the live Next Action direction rule) and the band function the
caller injects (``select_comfort_band`` in production), so band maths exists once.

Parts:

* ``StepKind`` - the closed set of things CA does or asks the occupant to do (each documents the code path CA uses).
* ``SENTENCES`` - the one sentence table (plan section 9): every reason text is built from structured facts through it;
  a sentence whose facts are missing is dropped, never guessed.
* ``build_timeline`` - snapshot -> ``DetailedTimeline`` (steps, predicted path, predicted runs, window candidates).
* ``TimelineLatch`` - the per-day latch and the regeneration rules (``material_changes``).

Spec: docs/operation-timeline-spec.md.
"""

from __future__ import annotations

import bisect
import logging
import math
import string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from .const import (
    OCCUPANCY_AWAY,
    OCCUPANCY_VACATION,
    THERMAL_SOLAR_DAYTIME_END_H,
    THERMAL_SOLAR_DAYTIME_START_H,
)
from .temperature import format_temp, outdoor_direction_ok_for_thermostat
from .thermal_confidence import grade_hvac_confidence, grade_solar_confidence

_LOGGER = logging.getLogger(__name__)

TIMELINE_SCHEMA_VERSION = 1

# --- Tunables (documented in docs/operation-timeline-spec.md; every number here is a decision, not a fit) ----------
TIMELINE_HORIZON_HOURS = 48
SIM_STEP_MINUTES = 15
YOU_STEP_LEAD_MIN = 30  # DP-4: a "you" step is shown this long before it is due (push is optional and off by default)
DISCOMFORT_FH_PER_HVAC_H = 1.0  # DP-2: 1 F-hour outside the band (beyond the swing) weighs the same as 1 h of HVAC
WINDOW_MIN_HVAC_SAVED_MIN = 10.0  # plan section 6: a window period must save this much HVAC...
WINDOW_MIN_DISCOMFORT_SAVED_FH = 0.5  # ...or this much discomfort beyond the swing (and never cost net score)
FREE_COOL_MIN_DELTA_F = 2.0  # candidate: outdoor at least this much cooler than the (clamped) house
FREE_HEAT_MIN_DELTA_F = 1.5  # candidate: outdoor at least this much warmer than the house
FREE_HEAT_START_H, FREE_HEAT_END_H = 9, 17  # candidate: daytime only
WINDOW_MIN_OBSERVATIONS = 50  # window effect validated = k_vent_window learned from >= 50 ventilated observations
WINDOW_MIN_RATIO = 1.5  # ...and at least this many times the envelope rate (|k_vent_window| / |k_passive|)
WINDOW_MIN_SPAN_MIN = 30
WINDOW_END_MARGIN_F = 0.5  # close before the house gets within this of the edge it is drifting toward
RUN_CLUSTER_GAP_MIN = 45  # plan section 9: heat/AC runs less than this far apart are one sentence
RUN_MIN_MINUTES = 5
SUMMARY_MAX_RUNS = 6  # run sentences in the summary (all runs stay in ``runs``)
MATERIAL_BAND_SHIFT_F = 0.5  # a band edge moving this much is a setpoint/band change
MATERIAL_FORECAST_MEAN_F = 2.0  # N: mean |change| of the next 12 forecast hours since the plan was made...
MATERIAL_FORECAST_PEAK_F = 3.0  # ...or the next-24-h high or low moved by this much
FORECAST_MAX_AGE_H = 6.0  # plan section 9: hide forecast-derived sentences when the forecast is older than this

# Engine events that can change the plan: they mark the shadow stale so the next 5-minute tick re-evaluates it.
TIMELINE_TRIGGER_EVENTS = frozenset(
    {
        "override_confirmed",
        "override_adopted",
        "override_cleared",
        "grace_started",
        "grace_expired",
        "sensor_closed_resume",
        "dashboard_resume",
        "re_paused",
    }
)

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


class Actor(StrEnum):
    CA = "ca"  # Climate Advisor performs it (a service call it actually makes)
    YOU = "you"  # the occupant must do it physically (windows only, today)


class StepKind(StrEnum):
    """Everything the timeline can say CA does or the occupant should do (contract: append-only, never renamed).

    Members R3a emits name the production path that performs them (``STEP_SOURCE``), so the enum cannot drift into
    planning an action CA never takes. Members in the frozen contract that R3a does NOT emit, and why:
    ``SETPOINT_CHANGE`` (every setpoint CA writes belongs to a named cause below: a causeless one would be invented),
    ``FAN_ON``/``FAN_OFF`` (the only planned fan runs are nat-vent sessions: ``NAT_VENT_*``), ``SETBACK_END`` and
    ``OCCUPANCY`` (arrivals and departures are unscheduled events: they regenerate the plan instead), and
    ``NIGHT_STRATEGY`` (R4). Appended by R3a: ``RESUME_SCHEDULE``. A predicted heat/AC run is not a step either: the
    thermostat starts it, CA only arms the band (see ``runs``).
    """

    MODE_CHANGE = "mode_change"  # thermostat hvac_mode change
    SETPOINT_CHANGE = "setpoint_change"
    FAN_ON = "fan_on"
    FAN_OFF = "fan_off"
    NAT_VENT_START = "nat_vent_start"
    NAT_VENT_STOP = "nat_vent_stop"
    PRECOOL_START = "precool_start"
    PREHEAT_START = "preheat_start"
    SETBACK_START = "setback_start"
    SETBACK_END = "setback_end"
    SLEEP_BAND = "sleep_band"
    WAKE_BAND = "wake_band"
    OCCUPANCY = "occupancy"
    WINDOWS_OPEN = "windows_open"
    WINDOWS_CLOSE = "windows_close"
    NIGHT_STRATEGY = "night_strategy"
    # --- appended by R3a ---
    RESUME_SCHEDULE = "resume_schedule"  # grace expiry: CA resumes the schedule after the occupant's change
    # --- appended by R3b (contract additive change): the two automation-executed actions Next Automation lists that
    # no earlier kind covers ---
    BRIEFING_SEND = "briefing_send"  # the scheduled daily briefing (a notify service call)
    CEILING_GUARD = "ceiling_guard"  # the ODE ceiling guard switches the AC on ahead of a predicted ceiling breach


STEP_SOURCE: dict[StepKind, str] = {
    StepKind.WAKE_BAND: "automation.handle_morning_wakeup",
    StepKind.SLEEP_BAND: "automation.handle_bedtime",
    StepKind.MODE_CHANGE: "automation.apply_classification",
    StepKind.SETBACK_START: "automation.handle_occupancy_away/vacation",
    StepKind.PRECOOL_START: "automation.handle_pre_cool/apply_tou_precondition",
    StepKind.PREHEAT_START: "automation.apply_tou_precondition",
    StepKind.NAT_VENT_START: "automation.check_natural_vent_conditions",
    StepKind.NAT_VENT_STOP: "automation.check_natural_vent_conditions",
    StepKind.RESUME_SCHEDULE: "automation._apply_current_scheduled_state",
    StepKind.SETPOINT_CHANGE: "automation.handle_occupancy_away/vacation (tou_expansion_f, Issue #899)",
    StepKind.BRIEFING_SEND: "coordinator._send_briefing",
    StepKind.CEILING_GUARD: "automation._apply_ode_ceiling_guard_decision",
    StepKind.WINDOWS_OPEN: "operation_timeline.evaluate_windows",
    StepKind.WINDOWS_CLOSE: "operation_timeline.evaluate_windows",
}
NOT_EMITTED_BY_R3A = frozenset(
    {
        StepKind.FAN_ON,
        StepKind.FAN_OFF,
        StepKind.SETBACK_END,
        StepKind.OCCUPANCY,
        StepKind.NIGHT_STRATEGY,
    }
)

_CA_KINDS = frozenset(k for k in StepKind if k not in (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE))


# ---------------------------------------------------------------------------
# Sentence table (plan section 9)
# ---------------------------------------------------------------------------


class MissingFact(KeyError):
    """A sentence was asked for without one of its facts: the caller drops the sentence instead of guessing."""


@dataclass(frozen=True)
class Sentence:
    """One generated sentence: its template, the facts it needs (name -> kind), and where it comes from."""

    key: str
    template: str
    facts: Mapping[str, str]  # name -> "time" | "temp" | "delta" | "text" | "int" | "minutes" | "hours"
    source: str  # which structured fact set feeds it
    rule: str  # the rule that decides it is said
    guard: str  # what stops it being wrong


def _s(key: str, template: str, facts: Mapping[str, str], source: str, rule: str, guard: str) -> tuple[str, Sentence]:
    return key, Sentence(key, template, facts, source, rule, guard)


SENTENCES: dict[str, Sentence] = dict(
    [
        _s(
            "wake_band.default",
            "{t}: CA switches to the daytime comfort band, {floor} to {ceiling}.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "band segment starting at wake_time (select_comfort_band, awake)",
            "one per wake_time in the horizon, occupancy home/guest",
            "band comes from the engine's own resolver, never recomputed here",
        ),
        _s(
            "wake_band.sensor_swap",
            "{t}: CA switches to the daytime comfort band, {floor} to {ceiling}, and reads the {sensor} again.",
            {"t": "time", "floor": "temp", "ceiling": "temp", "sensor": "text"},
            "band segment + sleep sensor configuration",
            "only when a sleep sensor is configured (the room being read changes)",
            "the sensor label comes from the same swap function as the Status card",
        ),
        _s(
            "sleep_band.default",
            "{t}: bedtime, CA switches to the sleep band, {floor} to {ceiling}.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "band segment starting at sleep_time (select_comfort_band, in_sleep_window)",
            "one per sleep_time in the horizon, occupancy home/guest",
            "raw configured sleep edges, exactly what handle_bedtime arms",
        ),
        _s(
            "sleep_band.sensor_swap",
            "{t}: bedtime, CA switches to the sleep band, {floor} to {ceiling}, and reads the {sensor} until {wake}.",
            {"t": "time", "floor": "temp", "ceiling": "temp", "sensor": "text", "wake": "time"},
            "band segment + sleep sensor configuration + next wake_time",
            "only when a sleep sensor is configured",
            "the sensor label comes from the same swap function as the Status card",
        ),
        _s(
            "mode_change",
            "{t}: the day plan changes from {old} to {new}.",
            {"t": "time", "old": "text", "new": "text"},
            "day modes (classification hvac_mode today, forecast high for later days)",
            "emitted where consecutive day modes differ",
            "today's mode is the live classification, later days are forecast-derived",
        ),
        _s(
            "occupancy_setback.away",
            "{t}: nobody is home, CA holds the away band, {floor} to {ceiling}, until someone is back.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "occupancy mode away + away band",
            "away applies to today only (as the chart's target band does)",
            "no return time is invented: arrival is an event, not a schedule",
        ),
        _s(
            "occupancy_setback.vacation",
            "{t}: vacation mode, CA holds the vacation band, {floor} to {ceiling}, until you return.",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "occupancy mode vacation + vacation band",
            "vacation applies to every day of the horizon",
            "no return time is invented",
        ),
        _s(
            "pre_cool_start.weather",
            "{t}: CA pre-cools the house to {target} so tomorrow's heat starts from a cooler house.",
            {"t": "time", "target": "temp"},
            "coordinator pre-cool trigger and target (compute_pre_cool_target)",
            "only when the pre-cool trigger and target exist",
            "trigger sits inside the sleep window (Issue #1371)",
        ),
        _s(
            "pre_cool_start.tou",
            "{t}: CA pre-cools to {target} ahead of the high-cost period at {start}.",
            {"t": "time", "target": "temp", "start": "time"},
            "TOU phase resolution (precondition_start, target, schedule_start)",
            "only for a cool-mode TOU precondition in the future",
            "lead time is the scheduler's own, not recomputed",
        ),
        _s(
            "pre_heat_start.tou",
            "{t}: CA pre-heats to {target} ahead of the high-cost period at {start}.",
            {"t": "time", "target": "temp", "start": "time"},
            "TOU phase resolution (precondition_start, target, schedule_start)",
            "only for a heat-mode TOU precondition in the future",
            "lead time is the scheduler's own, not recomputed",
        ),
        _s(
            "natvent_start",
            "{t}: once a window is open, CA runs the {fan} to bring the cooler air in.",
            {"t": "time", "fan": "text"},
            "accepted window period + fan mode",
            "only with a fan configured; follows the occupant's windows_open step",
            "the fan only starts with a window open (check_natural_vent_conditions precondition)",
        ),
        _s(
            "natvent_stop",
            "{t}: CA stops the {fan} as the window period ends.",
            {"t": "time", "fan": "text"},
            "accepted window period + fan mode",
            "only with a fan configured; at the window period's end",
            "the exit is the engine's, this only names the time it is expected",
        ),
        _s(
            "resume_schedule",
            "{t}: your change ends and CA resumes the schedule ({floor} to {ceiling}).",
            {"t": "time", "floor": "temp", "ceiling": "temp"},
            "manual override / grace end time + band then in force",
            "only while an override or grace is active with a known end",
            "the end time is the engine's own grace_end_time",
        ),
        _s(
            "setpoint_change.tou_expansion_start",
            "{t}: high-cost period, CA widens the {occupancy} band to {edge} until {end}.",
            {"t": "time", "occupancy": "text", "edge": "temp", "end": "time"},
            "away/vacation TOU window (scheduler resolution) + the day's direction",
            "only while away or on vacation, with a future window and a non-zero expansion",
            "the widening is select_comfort_band(tou_expansion_f), the same call the engine makes at the window start",
        ),
        _s(
            "setpoint_change.tou_expansion_end",
            "{t}: the high-cost period ends, CA returns to the {occupancy} band, {floor} to {ceiling}.",
            {"t": "time", "occupancy": "text", "floor": "temp", "ceiling": "temp"},
            "away/vacation TOU window end + the held band",
            "paired with the widening step",
            "the end is the schedule's own window end",
        ),
        _s(
            "briefing_send.default",
            "{t}: CA sends the daily briefing.",
            {"t": "time"},
            "configured briefing time",
            "one per briefing_time in the horizon",
            "the time is the scheduler's own (config briefing_time)",
        ),
        _s(
            "ceiling_guard.default",
            "{t}: CA switches the AC on to hold the {ceiling} ceiling.",
            {"t": "time", "ceiling": "temp"},
            "nat_vent_plan ceiling_breach_time + armed ceiling",
            "only on a windows day whose plan predicts a ceiling breach in the future",
            "the breach time is the plan's own; the switch is the ODE ceiling guard's mode write",
        ),
        _s(
            "natvent_start.predicted",
            "{t}: CA starts the {fan} (a window is open and outdoor air is cooler).",
            {"t": "time", "fan": "text"},
            "the engine's nat-vent start prediction (decide_nat_vent_gate on the forecast) + fan mode",
            "only with a fan configured and a window already open",
            "the start is the engine's own gate answer as of that hour, never promised without an open window",
        ),
        _s(
            "windows_open.free_cooling",
            "{t}: open the windows. Outdoor is {delta} cooler than the house{until}.",
            {"t": "time", "delta": "delta", "until": "text"},
            "window candidate (free cooling) with simulated benefit",
            "house predicted above the ceiling, outdoor at least FREE_COOL_MIN_DELTA_F cooler, saves >= 10 min "
            "HVAC or >= 0.5 F-h",
            "direction rule is the live Next Action rule; window effect must be validated",
        ),
        _s(
            "windows_open.free_heating",
            "{t}: open the windows. Outdoor is {delta} warmer than the house{until}.",
            {"t": "time", "delta": "delta", "until": "text"},
            "window candidate (free heating) with simulated benefit",
            "daytime, outdoor at least FREE_HEAT_MIN_DELTA_F warmer, house in the lower half of the band, same "
            "benefit gate",
            "direction rule is the live Next Action rule; window effect must be validated",
        ),
        _s(
            "windows_open.planned",
            "{t}: open the windows. Outdoor air helps the house until about {end}.",
            {"t": "time", "end": "time"},
            "classification window span, re-checked by simulation",
            "the planned window period survives the direction rule, the benefit gate and the validation gate",
            "a period that fails any gate is not offered (the rejected candidate keeps the reason)",
        ),
        _s(
            "windows_close.outdoor_rise",
            "{t}: close the windows. Outdoor stops helping (it is warming past the house).",
            {"t": "time"},
            "window candidate end (direction flips)",
            "direction rule fails at this hour",
            "same direction rule as the open step",
        ),
        _s(
            "windows_close.comfort_edge",
            "{t}: close the windows before the house drifts below {edge}.",
            {"t": "time", "edge": "temp"},
            "window candidate end (simulated drift to the band edge)",
            "simulated house within 0.5 F of the edge it is drifting toward",
            "simulated with the learned window rate",
        ),
        _s(
            "windows_close.planned_end",
            "{t}: close the windows, the planned window period ends.",
            {"t": "time"},
            "classification window span end",
            "end of the accepted period",
            "the close time can only move earlier within a day (latch)",
        ),
        _s(
            "windows_close.bedtime",
            "{t}: bedtime, close the windows before you turn in.",
            {"t": "time"},
            "window candidate reaching the sleep window",
            "a window period is never carried into the sleep window (nobody is awake to close it)",
            "sleep window comes from the configured wake/sleep times",
        ),
        _s(
            "windows_close.horizon",
            "{t}: close the windows when you go to bed or leave.",
            {"t": "time"},
            "window candidate reaching the end of the horizon",
            "period still open at the horizon's end",
            "never promises a time beyond the forecast",
        ),
        _s(
            "summary.windows_closed",
            "Keep windows closed for the next {hours}: no opening helps ({checked} checked).",
            {"hours": "text", "checked": "int"},
            "window candidates (all rejected or none found)",
            "no window period accepted",
            "numbers are the candidate count, not a prediction",
        ),
        _s(
            "summary.windows_open",
            "Windows help for {count} period(s) in the next {hours}.",
            {"count": "int", "hours": "text"},
            "accepted window periods",
            "at least one window period accepted",
            "only accepted periods are counted",
        ),
        _s(
            "run.heat",
            "Heat runs about {minutes} between {start} and {end} to hold {edge}.",
            {"minutes": "minutes", "start": "time", "end": "time", "edge": "temp"},
            "predicted runs (simulated under the plan)",
            "runs < 45 min apart are one run; rounded to 5 min; model confidence >= medium",
            "dropped when the HVAC rate is not graded medium or better",
        ),
        _s(
            "run.cool",
            "AC runs about {minutes} between {start} and {end} to hold {edge}.",
            {"minutes": "minutes", "start": "time", "end": "time", "edge": "temp"},
            "predicted runs (simulated under the plan)",
            "runs < 45 min apart are one run; rounded to 5 min; model confidence >= medium",
            "dropped when the HVAC rate is not graded medium or better",
        ),
        _s(
            "peak.warmest",
            "Warmest point: about {temp} at {t}.",
            {"temp": "temp", "t": "time"},
            "predicted path (v3; v4 stays in shadow until its backtest passes)",
            "daytime maximum of the predicted path, rounded to 0.5",
            "hidden when the forecast is older than 6 h",
        ),
        _s(
            "held.suffix",
            "{base} It applies when CA resumes the schedule at {until}.",
            {"base": "text", "until": "time"},
            "manual override / grace end time",
            "step falls inside an active override or grace",
            "the end time is the engine's own",
        ),
        _s(
            "held.direction_veto.heat",
            "Keep windows closed to hold heat — outdoor ({outdoor}) would cool the house below its {floor} floor.",
            {"outdoor": "temp", "floor": "temp"},
            "live outdoor/indoor readings + the band the thermostat is armed with (R3b surface layer)",
            "a window period is running, the windows are shut, and the live direction rule vetoes opening them "
            "with outdoor cooler than indoor",
            "applied by timeline_surfaces.apply_direction_veto on every read; never stored in the latched plan",
        ),
        _s(
            "held.direction_veto.warm",
            "Keep windows closed for now — outdoor ({outdoor}) isn't cooler than indoor ({indoor}) yet.",
            {"outdoor": "temp", "indoor": "temp"},
            "live outdoor/indoor readings (R3b surface layer)",
            "a window period is running, the windows are shut, and the live direction rule vetoes opening them "
            "with outdoor not cooler than indoor",
            "applied by timeline_surfaces.apply_direction_veto on every read; never stored in the latched plan",
        ),
        _s(
            "held.pause",
            "{base} It applies when the door or window closes.",
            {"base": "text"},
            "door/window pause state",
            "step falls while the engine is paused by a door or window",
            "pause clears when the sensor closes",
        ),
    ]
)

# Kinds -> sentence key prefixes that must exist (the coverage test walks this).
STEP_SENTENCE_PREFIX: dict[StepKind, str] = {
    **{k: k.value for k in StepKind if k not in NOT_EMITTED_BY_R3A},
    StepKind.SETBACK_START: "occupancy_setback",
    StepKind.PRECOOL_START: "pre_cool_start",
    StepKind.PREHEAT_START: "pre_heat_start",
    StepKind.NAT_VENT_START: "natvent_start",
    StepKind.NAT_VENT_STOP: "natvent_stop",
    StepKind.BRIEFING_SEND: "briefing_send",
    StepKind.CEILING_GUARD: "ceiling_guard",
    StepKind.SETPOINT_CHANGE: "setpoint_change",
}


def _fmt_time(dt: datetime, ref: datetime | None = None) -> str:
    """ "7:30 PM", with the weekday when it is not the reference day ("Thu 6:30 AM")."""
    text = dt.strftime("%I:%M %p").lstrip("0")
    if ref is not None and dt.date() != ref.date():
        return f"{dt.strftime('%a')} {text}"
    return text


def _fmt_minutes(m: float) -> str:
    rounded = max(RUN_MIN_MINUTES, int(round(m / 5.0) * 5))
    return f"{rounded} min"


def say(key: str, unit: str = "fahrenheit", *, ref: datetime | None = None, **facts: Any) -> str:
    """Render one table sentence. Raises ``MissingFact`` when a declared fact is absent or None."""
    sentence = SENTENCES[key]
    rendered: dict[str, str] = {}
    for name, kind in sentence.facts.items():
        value = facts.get(name)
        if value is None:
            raise MissingFact(f"{key}: {name}")
        if kind == "time":
            rendered[name] = _fmt_time(value, ref)
        elif kind == "temp":
            rendered[name] = format_temp(round(float(value) * 2) / 2 if name == "temp" else float(value), unit)
        elif kind == "delta":
            rendered[name] = format_temp_delta_text(float(value), unit)
        elif kind == "minutes":
            rendered[name] = _fmt_minutes(float(value))
        else:
            rendered[name] = str(value)
    return sentence.template.format(**rendered)


def format_temp_delta_text(delta_f: float, unit: str) -> str:
    """A temperature difference as text in the user's unit (scale only, no offset)."""
    scaled = delta_f * (5.0 / 9.0) if unit == "celsius" else delta_f
    return f"{scaled:.0f}°{'C' if unit == 'celsius' else 'F'}"


def template_fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


# ---------------------------------------------------------------------------
# Input: the snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandSpec:
    """The band the thermostat is armed with at an instant (a ``select_comfort_band`` result, flattened)."""

    floor: float
    ceiling: float
    active: str = "floor"  # "floor" | "ceiling": the day's dominant edge
    context: str = "comfort"  # comfort | prefer_savings | sleep | away | vacation | tou_expansion


BandFn = Callable[[datetime, str], BandSpec]  # (instant, occupancy_mode) -> band; the engine's own resolver


@dataclass
class TimelineSnapshot:
    """Everything the timeline needs, as plain data. Temperatures are internal degrees F."""

    now: datetime
    unit: str = "fahrenheit"
    zone: str = ""  # config entry id
    # configuration
    wake_time: time | None = None
    sleep_time: time | None = None
    fan_mode: str = "disabled"  # whole_house_fan | hvac_fan | disabled (never "both": Issue #1175)
    sleep_sensor_label: str | None = None  # e.g. "bedroom sensor"; None = no sleep sensor configured
    primary_sensor_label: str = "thermostat"
    can_heat: bool = True
    can_cool: bool = True
    # plan
    band_at: BandFn | None = None
    occupancy_mode: str = "home"
    day_modes: Mapping[date, str] = field(default_factory=dict)  # date -> heat | cool | off
    day_type: str = ""
    windows_recommended: bool = False
    window_open_time: time | None = None
    window_close_time: time | None = None  # classifier's static close (the plan's cutoff, if any, overrides it)
    nat_vent_cutoff: datetime | None = None  # nat_vent_plan: ODE-adjusted close for today
    evening_open_time: datetime | None = None  # R3b: nat_vent_plan's forecast-derived reopening (warm/mild/hot evening)
    opportunity_spans: Sequence[tuple[time, time]] = ()  # hot-day morning/evening window opportunities
    pre_cool_trigger: datetime | None = None
    pre_cool_target: float | None = None
    tou_precondition_start: datetime | None = None
    tou_schedule_start: datetime | None = None
    tou_target: float | None = None
    tou_mode: str | None = None
    briefing_time: time | None = None  # R3b: the scheduled daily briefing
    # R3b: the away/vacation TOU window (Issue #899): CA widens the held band by ``expansion`` on the day's side for the
    # window and returns to it after. The only setpoint change that has no step of its own kind: a named cause.
    tou_av_start: datetime | None = None
    tou_av_end: datetime | None = None
    tou_av_expansion_f: float | None = None
    ceiling_breach: datetime | None = (
        None  # R3b: nat_vent_plan ceiling_breach_time (AC switches on to hold the ceiling)
    )
    nat_vent_predicted_start: datetime | None = None  # R3b: the engine's own nat-vent start prediction (Issue #528)
    # R3b: the classification facts per local day (today, tomorrow), the one place the Analysis "Tomorrow (plan)" column
    # and the window plan for tomorrow come from. Each: date, day_type, hvac_mode, trend_direction, windows_recommended,
    # window_open_time, window_close_time, pre_condition, expected_high, expected_low (internal degrees F).
    plan_days: Sequence[Mapping[str, Any]] = ()
    # readings
    indoor: float | None = None  # what CA reads now (sleep sensor while asleep)
    indoor_primary: float | None = None  # the thermostat's own room; the simulation seed
    outdoor: float | None = None
    thermostat_mode: str | None = None
    windows_physically_open: bool = False
    # forecast and predictions
    hourly: Sequence[tuple[datetime, float]] = ()  # raw service forecast, local-aware
    forecast_age_h: float | None = None
    thermal_model: Mapping[str, Any] | None = None  # v3 thermal_model_cache
    v3_path: Sequence[tuple[datetime, float]] = ()  # the authoritative predicted path until R6
    selected_path: Sequence[
        tuple[datetime, float]
    ] = ()  # the user-selected display model's path (not v3); DISPLAY only
    selected_path_model: str = "v3"
    sensor_paths: Mapping[str, Sequence[tuple[datetime, float]]] = field(default_factory=dict)
    sensor_path_model: str = "v3"  # label the caller gives the engine behind ``sensor_paths``
    path_fn: Callable[[Sequence[tuple[datetime, datetime]]], Sequence[tuple[datetime, float]] | None] | None = None
    # engine armed state
    automation_enabled: bool = True
    manual_override_active: bool = False
    grace_active: bool = False
    grace_end: datetime | None = None
    paused_by_door: bool = False
    nat_vent_active: bool = False


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Facts hold datetimes and numbers; the debug payload wants plain JSON."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 2)
    return value


@dataclass(frozen=True)
class TimelineStep:
    """One step, exactly the frozen contract (docs/operation-timeline-spec.md); extras live in ``meta``."""

    ts: datetime  # aware, local tz of the zone
    actor: Actor
    kind: StepKind
    text: str  # one sentence from the table
    reason: str  # the sentence-table rule id, e.g. "sleep_band.default"
    target_f: float | None = None  # setpoint or temperature the step aims for (internal F)
    mode: str | None = None  # hvac mode after the step, when relevant
    confidence: str = "ok"  # "ok" | "low"
    lead_minutes: int = 0  # YOU steps: how far ahead to show them (DP-4: 30)
    source: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)  # id, grade (low|medium|high), status, after, facts

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", "planned"))

    @property
    def after(self) -> str | None:
        return self.meta.get("after")

    @property
    def facts(self) -> Mapping[str, Any]:
        return self.meta.get("facts", {})

    @property
    def grade(self) -> str:
        return str(self.meta.get("grade", "medium"))

    @property
    def show_from(self) -> datetime:
        """When the step becomes visible: YOU steps ``lead_minutes`` ahead, CA steps immediately (their own time)."""
        return self.ts - timedelta(minutes=self.lead_minutes)


@dataclass(frozen=True)
class PathPoint:
    ts: datetime
    temp_f: float  # predicted indoor under the plan
    sensor: str = "_ca"  # "_ca" or an entity id
    model: str = "v3"  # the engine that produced it


@dataclass(frozen=True)
class Timeline:
    """The frozen contract (``to_dict``/``from_dict`` below). ``DetailedTimeline`` adds the builder's working data."""

    schema_version: int
    zone: str
    generated_at: datetime
    day: date
    horizon_end: datetime
    steps: tuple[TimelineStep, ...]  # sorted by ts
    path: tuple[PathPoint, ...]  # sorted by (sensor, ts)
    latched: bool
    regen_reason: str | None

    def first_user_step(self, after: datetime | None = None) -> TimelineStep | None:
        """The earliest occupant step not yet past (#1276 invariant: Next Action equals this)."""
        for step in self.steps:
            if step.actor == Actor.YOU and (after is None or step.ts >= after):
                return step
        return None


@dataclass(frozen=True)
class WindowCandidate:
    kind: str  # planned | free_cooling | free_heating
    start: datetime
    end: datetime
    accepted: bool
    reason: str  # accepted | no_benefit | direction | unvalidated | no_model | overlaps | too_short
    hvac_saved_min: float | None
    discomfort_saved_fh: float | None
    outdoor_delta_f: float | None  # largest |outdoor - house| in the period
    end_reason: str  # outdoor_rise | comfort_edge | planned_end | bedtime | horizon
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "accepted": self.accepted,
            "reason": self.reason,
            "hvac_saved_min": None if self.hvac_saved_min is None else round(self.hvac_saved_min, 1),
            "discomfort_saved_fh": None if self.discomfort_saved_fh is None else round(self.discomfort_saved_fh, 2),
            "outdoor_delta_f": None if self.outdoor_delta_f is None else round(self.outdoor_delta_f, 1),
            "end_reason": self.end_reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PredictedRun:
    mode: str  # heat | cool
    start: datetime
    end: datetime
    minutes: float
    reason: str
    # Issue #1436: where in [start, end) the plan's simulation runs the equipment: (step start, minutes in that
    # 15-minute step), so the chart draws the expected minutes where they fall instead of the whole window
    bursts: tuple[tuple[datetime, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minutes": round(self.minutes, 1),
            "reason": self.reason,
            "bursts": [[t.isoformat(), round(m, 1)] for t, m in self.bursts],
        }


@dataclass(frozen=True)
class DetailedTimeline(Timeline):
    """A ``Timeline`` plus the builder's working data (debug, replay, tests). Surfaces read ``Timeline`` only."""

    path_source: str = "none"  # v3 | v3_replanned | <selected model id, e.g. v4> | sim | none
    band: tuple[tuple[datetime, float, float, str], ...] = ()  # (segment start, floor, ceiling, context)
    runs: tuple[PredictedRun, ...] = ()
    candidates: tuple[WindowCandidate, ...] = ()
    summary: tuple[str, ...] = ()
    grade: str = "low"  # overall confidence: low | medium | high
    hvac_minutes: float | None = None  # predicted equipment minutes over the horizon under the plan
    dropped: tuple[str, ...] = ()  # sentence keys dropped for missing facts
    hvac_hourly: tuple[tuple[datetime, float], ...] = ()  # (hour start, predicted equipment minutes that hour)
    generation: int = 1
    regenerated_for: tuple[str, ...] = ()
    days: tuple[Mapping[str, Any], ...] = ()  # R3b: per-day classification facts the plan was built from (plan_days)


def _step_to_dict(s: TimelineStep) -> dict[str, Any]:
    return {
        "ts": s.ts.isoformat(),
        "actor": s.actor.value,
        "kind": s.kind.value,
        "text": s.text,
        "reason": s.reason,
        "target_f": None if s.target_f is None else round(s.target_f, 1),
        "mode": s.mode,
        "confidence": s.confidence,
        "lead_minutes": s.lead_minutes,
        "source": s.source,
        "meta": {k: _jsonable(v) for k, v in s.meta.items()},
    }


def to_dict(t: Timeline) -> dict[str, Any]:
    """JSON-safe contract dict: ISO timestamps, enums as values, temperatures in internal degrees F."""
    return {
        "schema_version": t.schema_version,
        "zone": t.zone,
        "generated_at": t.generated_at.isoformat(),
        "day": t.day.isoformat(),
        "horizon_end": t.horizon_end.isoformat(),
        "steps": [_step_to_dict(s) for s in t.steps],
        "path": [
            {"ts": p.ts.isoformat(), "temp_f": round(p.temp_f, 1), "sensor": p.sensor, "model": p.model} for p in t.path
        ],
        "latched": t.latched,
        "regen_reason": t.regen_reason,
    }


def detail_dict(t: DetailedTimeline) -> dict[str, Any]:
    """The builder's working data as JSON (additive to ``to_dict``; not part of the frozen contract)."""
    return {
        "path_source": t.path_source,
        "grade": t.grade,
        "generation": t.generation,
        "regenerated_for": list(t.regenerated_for),
        "band": [{"ts": ts.isoformat(), "floor": lo, "ceiling": hi, "context": ctx} for ts, lo, hi, ctx in t.band],
        "runs": [r.to_dict() for r in t.runs],
        "candidates": [c.to_dict() for c in t.candidates],
        "summary": list(t.summary),
        "hvac_minutes": None if t.hvac_minutes is None else round(t.hvac_minutes, 1),
        "dropped": list(t.dropped),
        "days": [{k: (v.isoformat() if isinstance(v, (date, time)) else v) for k, v in day.items()} for day in t.days],
    }


def from_dict(d: Mapping[str, Any]) -> Timeline | None:
    """Rebuild a ``Timeline`` from ``to_dict`` output; None for an unknown schema or a malformed dict."""
    try:
        if int(d["schema_version"]) != TIMELINE_SCHEMA_VERSION:
            return None
        steps = tuple(
            TimelineStep(
                ts=datetime.fromisoformat(s["ts"]),
                actor=Actor(s["actor"]),
                kind=StepKind(s["kind"]),
                text=str(s["text"]),
                reason=str(s["reason"]),
                target_f=s.get("target_f"),
                mode=s.get("mode"),
                confidence=str(s.get("confidence", "ok")),
                lead_minutes=int(s.get("lead_minutes", 0)),
                source=str(s.get("source", "")),
                meta=dict(s.get("meta") or {}),
            )
            for s in d["steps"]
        )
        path = tuple(
            PathPoint(
                ts=datetime.fromisoformat(p["ts"]),
                temp_f=float(p["temp_f"]),
                sensor=str(p.get("sensor", "_ca")),
                model=str(p.get("model", "v3")),
            )
            for p in d["path"]
        )
        return Timeline(
            schema_version=TIMELINE_SCHEMA_VERSION,
            zone=str(d["zone"]),
            generated_at=datetime.fromisoformat(d["generated_at"]),
            day=date.fromisoformat(d["day"]),
            horizon_end=datetime.fromisoformat(d["horizon_end"]),
            steps=steps,
            path=path,
            latched=bool(d["latched"]),
            regen_reason=d.get("regen_reason"),
        )
    except KeyError, TypeError, ValueError:
        return None


# ---------------------------------------------------------------------------
# Helpers: time grid, interpolation, band segments
# ---------------------------------------------------------------------------


def _interp(points: Sequence[tuple[float, float]], t: float) -> float | None:
    if not points:
        return None
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    keys = [p[0] for p in points]
    i = bisect.bisect_right(keys, t)
    (t0, v0), (t1, v1) = points[i - 1], points[i]
    return v0 if t1 <= t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def asleep_at(at: datetime, wake: time | None, sleep: time | None) -> bool:
    """In the sleep window (automation._in_sleep_window, Issue #1376): wraps midnight unless sleep < wake."""
    if wake is None or sleep is None:
        return False
    t = at.time().replace(second=0, microsecond=0)
    if sleep > wake:
        return t >= sleep or t < wake
    if sleep < wake:
        return sleep <= t < wake
    return False


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    floor: float
    ceiling: float
    active: str
    context: str
    mode: str  # day-plan mode: heat | cool | off
    asleep: bool
    occupancy: str
    overlay: str | None = None  # pre_cool | tou_cool | tou_heat
    base_floor: float = 0.0  # the band before an overlay (what a wake/sleep step quotes)
    base_ceiling: float = 0.0

    def thermostat_mode(self, can_heat: bool, can_cool: bool) -> str:
        """The mode the thermostat is expected to hold in this segment (for the direction rule's swing margin)."""
        if self.mode == "heat" and can_heat:
            return "heat"
        if self.mode == "cool" and can_cool:
            return "cool"
        return "heat_cool" if (can_heat and can_cool) else ("heat" if can_heat else "cool" if can_cool else "off")


def _occupancy_for(snap: TimelineSnapshot, at: datetime) -> str:
    occ = snap.occupancy_mode
    if occ == OCCUPANCY_AWAY and at.date() != snap.now.date():
        return "home"  # the chart's target band applies away to today only
    return occ


def build_segments(snap: TimelineSnapshot, end: datetime) -> list[Segment]:
    """Band segments over [now, end): one per wake/sleep boundary (and per overlay edge), band from ``band_at``."""
    if snap.band_at is None:
        return []
    start = snap.now
    cuts: set[datetime] = {start}
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        for t in (snap.wake_time, snap.sleep_time):
            if t is not None:
                cut = datetime.combine(day, t, tzinfo=start.tzinfo)
                if start < cut < end:
                    cuts.add(cut)
        mid = datetime.combine(day, time(0, 0), tzinfo=start.tzinfo)
        if start < mid < end:
            cuts.add(mid)  # day-mode boundary
        day += timedelta(days=1)
    if snap.pre_cool_trigger is not None and start < snap.pre_cool_trigger < end:
        cuts.add(snap.pre_cool_trigger)
    if snap.tou_precondition_start is not None and start < snap.tou_precondition_start < end:
        cuts.add(snap.tou_precondition_start)
    if snap.tou_schedule_start is not None and start < snap.tou_schedule_start < end:
        cuts.add(snap.tou_schedule_start)
    ordered = sorted(cuts)
    segments: list[Segment] = []
    for i, cut in enumerate(ordered):
        seg_end = ordered[i + 1] if i + 1 < len(ordered) else end
        occ = _occupancy_for(snap, cut)
        band = snap.band_at(cut, occ)
        asleep = asleep_at(cut, snap.wake_time, snap.sleep_time)
        floor, ceiling = band.floor, band.ceiling
        overlay = None
        if (
            snap.pre_cool_trigger is not None
            and snap.pre_cool_target is not None
            and cut >= snap.pre_cool_trigger
            and asleep
            and occ in ("home", "guest")
        ):
            ceiling = min(ceiling, snap.pre_cool_target)
            overlay = "pre_cool"
        if (
            snap.tou_precondition_start is not None
            and snap.tou_schedule_start is not None
            and snap.tou_target is not None
            and snap.tou_precondition_start <= cut < snap.tou_schedule_start
        ):
            if snap.tou_mode == "cool":
                ceiling, overlay = min(ceiling, snap.tou_target), "tou_cool"
            elif snap.tou_mode == "heat":
                floor, overlay = max(floor, snap.tou_target), "tou_heat"
        mode = snap.day_modes.get(cut.date(), "off")
        segments.append(
            Segment(
                cut,
                seg_end,
                floor,
                ceiling,
                band.active,
                band.context,
                mode,
                asleep,
                occ,
                overlay,
                band.floor,
                band.ceiling,
            ),
        )
    return segments


class _SegmentIndex:
    def __init__(self, segments: Sequence[Segment]) -> None:
        self.segments = list(segments)
        self._starts = [s.start.timestamp() for s in self.segments]

    def at(self, ts: datetime) -> Segment | None:
        if not self.segments:
            return None
        i = bisect.bisect_right(self._starts, ts.timestamp()) - 1
        return self.segments[max(i, 0)]


# ---------------------------------------------------------------------------
# The plan simulator (the v3 ODE step, with thermostat edge-holding and a window period set)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRates:
    k_passive: float
    k_vent_window: float | None
    k_active_heat: float | None
    k_active_cool: float | None
    k_solar: float | None
    phase_h: float
    swing_heat: float | None
    swing_cool: float | None
    window_obs: int
    hvac_grade: str


def model_rates(model: Mapping[str, Any] | None) -> ModelRates | None:
    """Rates from the v3 thermal-model cache, or None when the envelope rate is not learned (no simulation)."""
    if not model:
        return None
    k_passive = model.get("k_passive")
    k_vent = model.get("k_vent_window")
    if not isinstance(k_passive, (int, float)) or k_passive >= 0:
        # Same bridge the v3 curve uses: a learned ventilated rate stands in for an unlearned envelope rate.
        if isinstance(k_vent, (int, float)) and k_vent < 0:
            k_passive = k_vent
        else:
            return None

    def num(key: str) -> float | None:
        v = model.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return ModelRates(
        k_passive=float(k_passive),
        k_vent_window=float(k_vent) if isinstance(k_vent, (int, float)) and k_vent < 0 else None,
        k_active_heat=num("k_active_heat"),
        k_active_cool=num("k_active_cool"),
        k_solar=num("k_solar"),
        phase_h=num("solar_phase_offset_h") or 0.0,
        swing_heat=num("swing_heat_f"),
        swing_cool=num("swing_cool_f"),
        window_obs=int(model.get("observation_count_vent_window", 0) or 0),
        hvac_grade=grade_hvac_confidence(dict(model)),
    )


def solar_factor(local_hour: int, phase_offset_h: float) -> float:
    """The v3 sun shape (coordinator._solar_factor): a half sine over the daytime, shifted by the learned phase."""
    eff = local_hour - int(round(phase_offset_h))
    if eff < THERMAL_SOLAR_DAYTIME_START_H or eff >= THERMAL_SOLAR_DAYTIME_END_H:
        return 0.0
    span = (THERMAL_SOLAR_DAYTIME_END_H - THERMAL_SOLAR_DAYTIME_START_H) / 2.0
    return math.sin(math.pi * (eff - THERMAL_SOLAR_DAYTIME_START_H) / (span * 2))


def ode_step(t: float, out: float, k: float, q: float, dt_h: float) -> float:
    """Exact solution of dT/dt = k (T - T_out) + q over dt (the v3 step, ``_simulate_indoor_physics_v3``)."""
    e = math.exp(k * dt_h)
    return out + (t - out) * e + (q / k) * (e - 1.0) if k != 0 else t + q * dt_h


@dataclass
class SimStep:
    ts: datetime
    temp: float  # house under the plan (equipment holding the band)
    free: float  # where the house would be this step with the equipment idle
    out: float
    seg: Segment
    window_open: bool
    hvac_min: float
    hvac_mode: str | None
    discomfort_fh: float


@dataclass
class Sim:
    steps: list[SimStep]
    hvac_minutes: float
    discomfort_fh: float

    def score(self) -> float:
        return self.hvac_minutes / 60.0 + DISCOMFORT_FH_PER_HVAC_H * self.discomfort_fh


Span = tuple[datetime, datetime]


class _Simulator:
    """One horizon, many window plans. ``run(spans)`` is cheap (about 200 steps)."""

    def __init__(self, snap: TimelineSnapshot, rates: ModelRates, index: _SegmentIndex, end: datetime) -> None:
        self.snap = snap
        self.rates = rates
        self.index = index
        self.end = end
        self.pts = sorted((d.timestamp(), float(v)) for d, v in snap.hourly if d.tzinfo is not None)
        self.start_t = snap.indoor_primary if snap.indoor_primary is not None else snap.indoor

    def run(self, spans: Sequence[Span]) -> Sim | None:
        if self.start_t is None or not self.pts or not self.index.segments:
            return None
        r, snap = self.rates, self.snap
        last_fc = self.pts[-1][0] + 1800
        dt_h = SIM_STEP_MINUTES / 60.0
        t = float(self.start_t)
        ts = snap.now
        steps: list[SimStep] = []
        hvac_total = 0.0
        disc_total = 0.0
        span_pts = [(a.timestamp(), b.timestamp()) for a, b in spans]
        while True:
            ts_next = ts + timedelta(minutes=SIM_STEP_MINUTES)
            if ts_next > self.end or ts_next.timestamp() > last_fc:
                break
            out = _interp(self.pts, ts_next.timestamp())
            assert out is not None
            seg = self.index.at(ts)
            assert seg is not None
            mid = ts.timestamp() + SIM_STEP_MINUTES * 30
            open_ = any(a <= mid < b for a, b in span_pts)
            k = r.k_vent_window if (open_ and r.k_vent_window is not None) else r.k_passive
            q_sun = (r.k_solar or 0.0) * solar_factor(ts.hour, r.phase_h)
            free = ode_step(t, out, k, q_sun, dt_h)
            nxt = free
            hvac_min = 0.0
            hvac_mode: str | None = None
            if not open_:
                # The thermostat holds whichever edge the house would otherwise cross (both armed; an away or
                # vacation band is just a wider band). The equipment rate is the learned one; the run is the
                # fraction of the step needed to get back to the edge.
                if free < seg.floor and snap.can_heat and r.k_active_heat:
                    rate = abs(r.k_active_heat)
                    gain = min(seg.floor - free, rate * dt_h)
                    nxt, hvac_min, hvac_mode = free + gain, gain / rate * 60.0, "heat"
                elif free > seg.ceiling and snap.can_cool and r.k_active_cool:
                    rate = abs(r.k_active_cool)
                    gain = min(free - seg.ceiling, rate * dt_h)
                    nxt, hvac_min, hvac_mode = free - gain, gain / rate * 60.0, "cool"
            sw_h = r.swing_heat if r.swing_heat is not None else 1.0
            sw_c = r.swing_cool if r.swing_cool is not None else 1.0
            disc = max(0.0, seg.floor - sw_h - nxt) + max(0.0, nxt - seg.ceiling - sw_c)
            disc_fh = disc * dt_h
            steps.append(SimStep(ts_next, nxt, free, out, seg, open_, hvac_min, hvac_mode, disc_fh))
            hvac_total += hvac_min
            disc_total += disc_fh
            t, ts = nxt, ts_next
        return Sim(steps, hvac_total, disc_total)


# ---------------------------------------------------------------------------
# Window candidates
# ---------------------------------------------------------------------------


def window_effect_grade(rates: ModelRates | None) -> str:
    """Confidence in the learned window effect: ``none`` unless it is both measured and distinguishable (gate)."""
    if rates is None or rates.k_vent_window is None:
        return "none"
    if abs(rates.k_vent_window) < WINDOW_MIN_RATIO * abs(rates.k_passive):
        return "none"
    grade = grade_solar_confidence(rates.window_obs)  # same 20 / 50 / 100 tiers as the other learned effects
    return grade if grade in ("none", "low") else ("medium" if grade == "medium" else "high")


def _direction_ok(snap: TimelineSnapshot, seg: Segment, outdoor: float, indoor: float, rates: ModelRates) -> bool:
    return outdoor_direction_ok_for_thermostat(
        outdoor,
        indoor,
        seg.floor,
        seg.ceiling,
        thermostat_mode=seg.thermostat_mode(snap.can_heat, snap.can_cool),
        swing_heat_f=rates.swing_heat,
        swing_cool_f=rates.swing_cool,
    )


def _planned_spans(snap: TimelineSnapshot, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """The classification's window periods as datetimes (today; the ODE-adjusted close when the plan has one)."""
    spans: list[tuple[str, datetime, datetime]] = []
    tz = snap.now.tzinfo
    today = snap.now.date()
    if snap.windows_recommended and snap.window_open_time is not None:
        open_dt = datetime.combine(today, snap.window_open_time, tzinfo=tz)
        close_dt: datetime | None = None
        if snap.nat_vent_cutoff is not None and snap.nat_vent_cutoff.date() == today:
            close_dt = snap.nat_vent_cutoff
        elif snap.window_close_time is not None:
            close_dt = datetime.combine(today, snap.window_close_time, tzinfo=tz)
        if close_dt is not None and close_dt > open_dt:
            spans.append(("planned", open_dt, close_dt))
    cutoff = snap.nat_vent_cutoff if snap.nat_vent_cutoff is not None and snap.nat_vent_cutoff.date() == today else None
    evening_open = (
        snap.evening_open_time
        if snap.evening_open_time is not None and snap.evening_open_time.date() == today
        else None
    )
    for a, b in snap.opportunity_spans:
        s, e = datetime.combine(today, a, tzinfo=tz), datetime.combine(today, b, tzinfo=tz)
        # the same dynamic values the briefing and the card used (nat_vent_plan): the ODE-adjusted morning close and
        # the forecast-derived evening reopening replace the static opportunity hours when they fall inside the span
        if a < time(12, 0) and cutoff is not None and s < cutoff < e:
            e = cutoff
        elif a >= time(15, 0) and evening_open is not None and s <= evening_open < e:
            s = evening_open
        if e > s:
            spans.append(("planned", s, e))
    if (
        snap.windows_recommended
        and snap.window_open_time is not None
        and evening_open is not None
        and not any(k == "planned" and a <= evening_open < b for k, a, b in spans)
    ):
        # warm / mild windows day: the plan's forecast-derived reopening tonight (the briefing's "reopen windows
        # around")
        spans.append(("planned", evening_open, datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=tz)))
    tomorrow = today + timedelta(days=1)
    for day in snap.plan_days:  # tomorrow's engine plan (the Analysis "Tomorrow (plan)" column reads these steps)
        o, c = day.get("window_open_time"), day.get("window_close_time")
        if (
            day.get("date") == tomorrow
            and day.get("windows_recommended")
            and isinstance(o, time)
            and isinstance(c, time)
        ):
            s, e = datetime.combine(tomorrow, o, tzinfo=tz), datetime.combine(tomorrow, c, tzinfo=tz)
            if e > s:
                spans.append(("planned", s, e))
    # nobody is home to open a window (away today, vacation every day)
    spans = [(k, a, b) for k, a, b in spans if _occupancy_for(snap, a) in ("home", "guest")]
    return [(k, max(a, snap.now), min(b, end)) for k, a, b in spans if min(b, end) > max(a, snap.now)]


def _scan_candidates(
    snap: TimelineSnapshot, base: Sim, rates: ModelRates, end: datetime
) -> list[tuple[str, datetime, datetime]]:
    """Free-cooling / free-heating periods found from the forecast against the baseline plan (plan section 6)."""
    found: list[tuple[str, datetime, datetime]] = []
    cur_kind: str | None = None
    cur_start: datetime | None = None
    prev_end: datetime | None = None

    def flush() -> None:
        nonlocal cur_kind, cur_start
        if cur_kind and cur_start and prev_end and prev_end - cur_start >= timedelta(minutes=WINDOW_MIN_SPAN_MIN):
            found.append((cur_kind, cur_start, prev_end))
        cur_kind = cur_start = None

    for st in base.steps:
        kind: str | None = None
        seg = st.seg
        if seg.occupancy not in ("home", "guest") or seg.asleep:
            kind = None  # nobody to open a window: away, or asleep
        elif st.free > seg.ceiling and st.out <= st.temp - FREE_COOL_MIN_DELTA_F and snap.can_cool:
            kind = "free_cooling"
        elif (
            FREE_HEAT_START_H <= st.ts.hour < FREE_HEAT_END_H
            and st.out >= st.temp + FREE_HEAT_MIN_DELTA_F
            and st.temp < (seg.floor + seg.ceiling) / 2.0
            and snap.can_heat
        ):
            kind = "free_heating"
        if kind is not None and not _direction_ok(snap, seg, st.out, st.temp, rates):
            kind = None
        if kind != cur_kind:
            flush()
            cur_kind, cur_start = kind, (st.ts - timedelta(minutes=SIM_STEP_MINUTES)) if kind else None
        prev_end = st.ts
    flush()
    return found


def _refine_span(
    snap: TimelineSnapshot,
    sim_fn: Callable[[Sequence[Span]], Sim | None],
    base: Sim,
    rates: ModelRates,
    kind: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime, str] | None:
    """Trim a span to the hours where the direction rule holds, and close it before the house drifts to an edge.

    The first hour is judged on the house as the thermostat has it under the plan (the baseline); after that on the
    house the open window is actually producing, so "outdoor stops helping" and "house reached the edge" are found
    where they happen.
    """
    win = sim_fn([(start, end)])
    if win is None:
        return None
    first: datetime | None = None
    last: datetime | None = None
    end_reason = "planned_end" if kind == "planned" else "horizon"
    seed = float(snap.indoor_primary if snap.indoor_primary is not None else snap.indoor or 0.0)
    for i, st in enumerate(win.steps):
        t0 = st.ts - timedelta(minutes=SIM_STEP_MINUTES)
        if t0 < start or st.ts > end:
            continue
        if first is None:
            house = base.steps[i - 1].temp if i > 0 else seed
        else:
            house = win.steps[i - 1].temp if i > 0 else seed
        if st.seg.asleep:
            if first is not None:
                end_reason = "bedtime"
                break
            continue
        ok = _direction_ok(snap, st.seg, st.out, house, rates)
        if first is None:
            if ok:
                first, last = t0, st.ts
            continue
        if not ok:
            end_reason = "outdoor_rise" if st.out >= house else "comfort_edge"
            break
        last = st.ts
    if first is None or last is None or last - first < timedelta(minutes=WINDOW_MIN_SPAN_MIN):
        return None
    if last >= end and kind != "planned":
        end_reason = "horizon" if end >= snap.now + timedelta(hours=TIMELINE_HORIZON_HOURS) else end_reason
    return first, last, end_reason


def _clip_to_awake(snap: TimelineSnapshot, a: datetime, b: datetime) -> tuple[datetime, datetime, str] | None:
    """A window period never runs into the sleep window (nobody is awake to close it): end it at bedtime."""
    if snap.sleep_time is None:
        return a, b, "planned_end"
    if asleep_at(a, snap.wake_time, snap.sleep_time):
        wake = _next_wake(snap, a)  # a span that starts before the occupant is up starts when they are
        if wake is None or wake >= b:
            return None
        a = wake
    bed = datetime.combine(a.date(), snap.sleep_time, tzinfo=a.tzinfo)
    if bed <= a:
        bed += timedelta(days=1)
    if b > bed:
        return (a, bed, "bedtime") if bed - a >= timedelta(minutes=WINDOW_MIN_SPAN_MIN) else None
    return a, b, "planned_end"


def _trim_engine_span(
    snap: TimelineSnapshot, base: Sim | None, rates: ModelRates | None, a: datetime, b: datetime
) -> tuple[datetime, datetime, str] | None:
    """D1: the engine's recommended span for an unvalidated window effect.

    Clipped at bedtime; where a baseline path and the learned swings exist, trimmed to the contiguous hours where the
    live direction rule (the one the Next Action card applies) holds on the house as the thermostat has it. Nothing
    else removes it: the engine recommended it and the occupant acts on it today.
    """
    clipped = _clip_to_awake(snap, a, b)
    if clipped is None:
        return None
    a, b, why = clipped
    if base is None or rates is None:
        return a, b, why
    first: datetime | None = None
    last: datetime | None = None
    for st in base.steps:
        t0 = st.ts - timedelta(minutes=SIM_STEP_MINUTES)
        if t0 < a or st.ts > b:
            continue
        ok = not st.seg.asleep and _direction_ok(snap, st.seg, st.out, st.temp, rates)
        if ok:
            if first is None:
                first = t0
            last = st.ts
        elif first is not None:
            why = "outdoor_rise" if st.out >= st.temp else "comfort_edge"
            break
    if first is None or last is None or last - first < timedelta(minutes=WINDOW_MIN_SPAN_MIN):
        return None
    return first, last, why


def engine_only_windows(snap: TimelineSnapshot, end: datetime) -> tuple[list[WindowCandidate], list[Span]]:
    """No usable simulation (no learned envelope rate, no seed temperature, no forecast): the engine's recommended
    periods stand, unvalidated (confidence low), clipped at bedtime. The live card applies the direction rule on the
    reading when it shows them."""
    results: list[WindowCandidate] = []
    accepted: list[Span] = []
    for kind, a, b in _planned_spans(snap, end):
        clipped = _clip_to_awake(snap, a, b)
        if clipped is None or any(clipped[0] < y and x < clipped[1] for x, y in accepted):
            results.append(WindowCandidate(kind, a, b, False, "too_short", None, None, None, "planned_end", "low"))
            continue
        ca, cb, why = clipped
        accepted.append((ca, cb))
        results.append(WindowCandidate(kind, ca, cb, True, "engine_unvalidated", None, None, None, why, "low"))
    return results, sorted(accepted)


def evaluate_windows(
    snap: TimelineSnapshot, sim: _Simulator, base: Sim, rates: ModelRates, end: datetime
) -> tuple[list[WindowCandidate], list[Span]]:
    """Candidates (planned + scanned), each judged by simulating the plan with and without it; greedy by benefit."""
    grade = window_effect_grade(rates)
    raw: list[tuple[str, datetime, datetime]] = [*_planned_spans(snap, end), *_scan_candidates(snap, base, rates, end)]
    raw.sort(key=lambda c: c[1])
    # drop exact duplicates of a scanned span inside a planned one: planned wins
    merged: list[tuple[str, datetime, datetime]] = []
    for k, a, b in raw:
        if merged and a < merged[-1][2] and k != "planned" and merged[-1][0] == "planned":
            continue
        merged.append((k, a, b))
    results: list[WindowCandidate] = []
    pending = []
    open_now: tuple[datetime, datetime, str] | None = None
    if snap.windows_physically_open and grade not in ("none", "low"):
        # A window is open right now: no gate on starting it (the occupant already did); the question is only when
        # it should close, judged by the same direction rule and edge-drift simulation.
        open_now = _refine_span(snap, sim.run, base, rates, "open_now", snap.now, end)
    planned_adopted: list[tuple[datetime, datetime, WindowCandidate]] = []
    for kind, a, b in merged:
        if kind == "planned":
            # D1 (R3b): the engine's current recommendation is the source. Unvalidated window effect: keep the
            # engine's times (trimmed only by the direction rule on the baseline house, clipped at bedtime),
            # confidence low. Validated: the simulation may ADJUST the timing; it never removes the advice.
            if grade in ("none", "low"):
                trimmed = _trim_engine_span(snap, base, rates, a, b)
                if trimmed is None:
                    results.append(
                        WindowCandidate(kind, a, b, False, "direction", None, None, None, "planned_end", "low")
                    )
                else:
                    ta, tb, why = trimmed
                    planned_adopted.append(
                        (
                            ta,
                            tb,
                            WindowCandidate(kind, ta, tb, True, "engine_unvalidated", None, None, None, why, "low"),
                        )
                    )
                continue
            refined = _refine_span(snap, sim.run, base, rates, kind, a, b)
            if refined is None:
                results.append(WindowCandidate(kind, a, b, False, "direction", None, None, None, "planned_end", grade))
                continue
            ra, rb, why = refined
            trial = sim.run([(ra, rb)])
            assert trial is not None
            saved_min = base.hvac_minutes - trial.hvac_minutes
            saved_fh = base.discomfort_fh - trial.discomfort_fh
            delta = max((abs(s.out - s.free) for s in trial.steps if s.window_open), default=0.0)
            beneficial = (saved_min >= WINDOW_MIN_HVAC_SAVED_MIN or saved_fh >= WINDOW_MIN_DISCOMFORT_SAVED_FH) and (
                base.score() - trial.score() > 0
            )
            planned_adopted.append(
                (
                    ra,
                    rb,
                    WindowCandidate(
                        kind,
                        ra,
                        rb,
                        True,
                        "accepted" if beneficial else "engine_no_benefit",
                        saved_min,
                        saved_fh,
                        delta,
                        why,
                        grade,
                    ),
                )
            )
            continue
        if grade in ("none", "low"):
            results.append(
                WindowCandidate(kind, a, b, False, "unvalidated", None, None, None, "planned_end", "low"),
            )
            continue
        refined = _refine_span(snap, sim.run, base, rates, kind, a, b)
        if refined is None:
            results.append(WindowCandidate(kind, a, b, False, "direction", None, None, None, "planned_end", grade))
            continue
        ra, rb, why = refined
        trial = sim.run([(ra, rb)])
        assert trial is not None
        saved_min = base.hvac_minutes - trial.hvac_minutes
        saved_fh = base.discomfort_fh - trial.discomfort_fh
        delta = max((abs(s.out - s.free) for s in trial.steps if s.window_open), default=0.0)
        pending.append((base.score() - trial.score(), kind, ra, rb, why, saved_min, saved_fh, delta))
    # accept greedily by net score, re-simulating with already-accepted periods
    accepted: list[Span] = []
    if open_now is not None:
        ra, rb, why = open_now
        accepted.append((snap.now, rb))
        results.append(WindowCandidate("open_now", snap.now, rb, True, "accepted", None, None, None, why, grade))
    for pa, pb, cand in planned_adopted:  # the engine's own periods are in before any scanned one competes
        if any(pa < b and a < pb for a, b in accepted):
            results.append(replace(cand, accepted=False, reason="overlaps"))
            continue
        accepted.append((pa, pb))
        results.append(cand)
    for _gain, kind, ra, rb, why, _m, _f, delta in sorted(pending, key=lambda p: -p[0]):
        if any(ra < b and a < rb for a, b in accepted):
            results.append(WindowCandidate(kind, ra, rb, False, "overlaps", None, None, delta, why, grade))
            continue
        cur = sim.run(accepted) or base
        with_c = sim.run([*accepted, (ra, rb)])
        assert with_c is not None
        saved_min = cur.hvac_minutes - with_c.hvac_minutes
        saved_fh = cur.discomfort_fh - with_c.discomfort_fh
        net = cur.score() - with_c.score()
        ok = (saved_min >= WINDOW_MIN_HVAC_SAVED_MIN or saved_fh >= WINDOW_MIN_DISCOMFORT_SAVED_FH) and net > 0
        if ok:
            accepted.append((ra, rb))
        results.append(
            WindowCandidate(
                kind, ra, rb, ok, "accepted" if ok else "no_benefit", saved_min, saved_fh, delta, why, grade
            )
        )
    results.sort(key=lambda c: c.start)
    return results, sorted(accepted)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


RUN_MAX_SPAN_H = 3.0  # a run sentence never spans more than this (a cold day would otherwise be one run)


def predicted_runs(sim: Sim, rates: ModelRates) -> list[PredictedRun]:
    """Cluster simulated equipment minutes into runs (plan section 9: < 45 min apart is one run, rounded to 5 min).

    A cluster also ends when the mode flips, the band segment changes (a new setpoint is a new run) or it would span
    more than ``RUN_MAX_SPAN_H``. Only at model confidence >= medium; otherwise no runs are claimed.
    """
    if rates.hvac_grade not in ("medium", "high"):
        return []
    runs: list[PredictedRun] = []
    cur: list[SimStep] = []
    for st in sim.steps:
        if st.hvac_min <= 0:
            continue
        if cur:
            gap = (st.ts - timedelta(minutes=SIM_STEP_MINUTES)) - cur[-1].ts
            span = st.ts - (cur[0].ts - timedelta(minutes=SIM_STEP_MINUTES))
            if (
                st.hvac_mode != cur[-1].hvac_mode
                or gap > timedelta(minutes=RUN_CLUSTER_GAP_MIN)
                or span > timedelta(hours=RUN_MAX_SPAN_H)
                or st.seg is not cur[-1].seg
            ):
                runs.append(_run(cur))
                cur = []
        cur.append(st)
    if cur:
        runs.append(_run(cur))
    return [r for r in runs if r.minutes >= RUN_MIN_MINUTES]


def _run(steps: list[SimStep]) -> PredictedRun:
    first, last = steps[0], steps[-1]
    mode = first.hvac_mode or "heat"
    edge = "floor" if mode == "heat" else "ceiling"
    return PredictedRun(
        mode,
        first.ts - timedelta(minutes=SIM_STEP_MINUTES),
        last.ts,
        sum(s.hvac_min for s in steps),
        f"hold_{edge}",
        tuple((s.ts - timedelta(minutes=SIM_STEP_MINUTES), s.hvac_min) for s in steps if s.hvac_min > 0),
    )


# ---------------------------------------------------------------------------
# Building the timeline
# ---------------------------------------------------------------------------


def _sid(ts: datetime, kind: str, extra: str = "") -> str:
    return f"{ts.strftime('%Y%m%dT%H%M')}:{kind}{extra}"


def _armed_edge(seg: Segment, pred_temp: float | None) -> str:
    """Which edge the single setpoint sits on: the day mode's, else the nearer one to the predicted room."""
    if seg.mode == "heat":
        return "floor"
    if seg.mode == "cool":
        return "ceiling"
    if pred_temp is not None:
        return "floor" if abs(pred_temp - seg.floor) <= abs(seg.ceiling - pred_temp) else "ceiling"
    return seg.active


def _confidence_from(*grades: str) -> str:
    return min(grades, key=lambda g: CONFIDENCE_ORDER.get(g, 0))


def build_timeline(
    snap: TimelineSnapshot, *, generation: int = 1, regenerated_for: Sequence[str] = ()
) -> DetailedTimeline:
    """The whole plan from one snapshot. Never raises on missing optional inputs: steps whose facts are missing drop."""
    now = snap.now
    end = now + timedelta(hours=TIMELINE_HORIZON_HOURS)
    unit = snap.unit
    segments = build_segments(snap, end)
    index = _SegmentIndex(segments)
    rates = model_rates(snap.thermal_model)
    sim_obj = _Simulator(snap, rates, index, end) if rates is not None else None
    base = sim_obj.run([]) if sim_obj is not None else None

    candidates: list[WindowCandidate] = []
    accepted: list[Span] = []
    if sim_obj is not None and base is not None and rates is not None:
        candidates, accepted = evaluate_windows(snap, sim_obj, base, rates, end)
    else:
        # D1 (R3b): without a usable simulation the engine's own recommendation still stands, unvalidated (low).
        candidates, accepted = engine_only_windows(snap, end)
    final = sim_obj.run(accepted) if sim_obj is not None else None

    dropped: list[str] = []
    steps: list[TimelineStep] = []

    def add(
        ts: datetime,
        kind: StepKind,
        key: str,
        grade: str,
        facts: dict[str, Any],
        *,
        sentence_facts: dict[str, Any] | None = None,
        after: str | None = None,
        target_f: float | None = None,
        mode: str | None = None,
    ) -> str | None:
        try:
            text = say(key, unit, ref=now, **(sentence_facts if sentence_facts is not None else facts))
        except MissingFact:
            dropped.append(key)
            return None
        actor = Actor.CA if kind in _CA_KINDS else Actor.YOU
        sid = _sid(ts, kind.value)
        meta = {
            "id": sid,
            "grade": grade,
            "status": "planned",
            "after": after,
            "facts": {k: _jsonable(v) for k, v in facts.items()},
        }
        steps.append(
            TimelineStep(
                ts=ts,
                actor=actor,
                kind=kind,
                text=text,
                reason=key,
                target_f=target_f,
                mode=mode,
                confidence="low" if grade == "low" else "ok",
                lead_minutes=YOU_STEP_LEAD_MIN if actor == Actor.YOU else 0,
                source=STEP_SOURCE[kind],
                meta=meta,
            )
        )
        return sid

    def predicted_at(ts: datetime) -> float | None:
        if not final:
            return None
        best = min(final.steps, key=lambda s: abs((s.ts - ts).total_seconds()), default=None)
        return best.temp if best else None

    # --- band-driven steps (every boundary after the first segment, which starts now) ---
    for prev, seg in zip(segments, segments[1:], strict=False):
        minute = seg.start.time().replace(second=0, microsecond=0)
        if seg.occupancy in ("home", "guest"):
            edge = _armed_edge(seg, predicted_at(seg.start))
            facts = {
                "t": seg.start,
                "floor": seg.base_floor,
                "ceiling": seg.base_ceiling,
                "setpoint_edge": edge,
                "setpoint": seg.base_floor if edge == "floor" else seg.base_ceiling,
                "context": seg.context,
                "mode": seg.mode,
            }
            if snap.wake_time is not None and minute == snap.wake_time and not seg.asleep:
                if snap.sleep_sensor_label:
                    facts["sensor"] = snap.primary_sensor_label
                    add(
                        seg.start,
                        StepKind.WAKE_BAND,
                        "wake_band.sensor_swap",
                        "high",
                        facts,
                        target_f=facts["setpoint"],
                        mode=seg.mode,
                    )
                else:
                    add(
                        seg.start,
                        StepKind.WAKE_BAND,
                        "wake_band.default",
                        "high",
                        facts,
                        target_f=facts["setpoint"],
                        mode=seg.mode,
                    )
            elif snap.sleep_time is not None and minute == snap.sleep_time and seg.asleep:
                if snap.sleep_sensor_label:
                    facts.update({"sensor": snap.sleep_sensor_label, "wake": _next_wake(snap, seg.start)})
                    add(
                        seg.start,
                        StepKind.SLEEP_BAND,
                        "sleep_band.sensor_swap",
                        "high",
                        facts,
                        target_f=facts["setpoint"],
                        mode=seg.mode,
                    )
                else:
                    add(
                        seg.start,
                        StepKind.SLEEP_BAND,
                        "sleep_band.default",
                        "high",
                        facts,
                        target_f=facts["setpoint"],
                        mode=seg.mode,
                    )
        if prev.mode != seg.mode and seg.occupancy in ("home", "guest"):
            # The day plan changes with the new calendar day; CA applies it when that day's schedule starts.
            at = seg.start
            if minute == time(0, 0) and snap.wake_time is not None:
                at = datetime.combine(seg.start.date(), snap.wake_time, tzinfo=seg.start.tzinfo)
            if at > now:
                add(
                    at,
                    StepKind.MODE_CHANGE,
                    "mode_change",
                    "medium",
                    {"t": at, "old": prev.mode, "new": seg.mode},
                    mode=seg.mode,
                )

    # occupancy hold (now)
    if segments and segments[0].occupancy in ("away", "vacation"):
        seg = segments[0]
        key = "occupancy_setback.vacation" if seg.occupancy == OCCUPANCY_VACATION else "occupancy_setback.away"
        # Issue #1404: the step names the away/vacation band itself. A time-of-use bank laid over this segment
        # (overlay tou_cool / tou_heat) is what the house is held to until the high-cost period starts, and the plan
        # list's "Now" row says so; calling the bank edge (e.g. 60-77 F) "the away band" contradicted it.
        _tou = seg.overlay in ("tou_cool", "tou_heat")
        add(
            now,
            StepKind.SETBACK_START,
            key,
            "high",
            {
                "t": now,
                "floor": seg.base_floor if _tou else seg.floor,
                "ceiling": seg.base_ceiling if _tou else seg.ceiling,
            },
        )

    # pre-cool / TOU
    # The weather pre-cool is a home-band action: an empty house (away today, vacation always) is held at its setback by
    # the engine's occupancy gate, so promising "CA pre-cools" there would be a step CA never performs. (TOU banking has
    # its own away/vacation path, apply_tou_away_vacation_precondition, so it stays.)
    if (
        snap.pre_cool_trigger is not None
        and snap.pre_cool_target is not None
        and snap.pre_cool_trigger > now
        and _occupancy_for(snap, snap.pre_cool_trigger) in ("home", "guest")
    ):
        add(
            snap.pre_cool_trigger,
            StepKind.PRECOOL_START,
            "pre_cool_start.weather",
            _confidence_from("medium", rates.hvac_grade if rates else "low"),
            {"t": snap.pre_cool_trigger, "target": snap.pre_cool_target, "cause": "weather_trend"},
            target_f=snap.pre_cool_target,
            mode="cool",
        )
    if (
        snap.tou_precondition_start is not None
        and snap.tou_precondition_start > now
        and snap.tou_target is not None
        and snap.tou_mode in ("cool", "heat")
    ):
        kind = StepKind.PRECOOL_START if snap.tou_mode == "cool" else StepKind.PREHEAT_START
        key = "pre_cool_start.tou" if snap.tou_mode == "cool" else "pre_heat_start.tou"
        add(
            snap.tou_precondition_start,
            kind,
            key,
            "high",
            {
                "t": snap.tou_precondition_start,
                "target": snap.tou_target,
                "start": snap.tou_schedule_start,
                "cause": "tou",
            },
            target_f=snap.tou_target,
            mode=snap.tou_mode,
        )

    # R3b: the three automation-executed actions Next Automation lists that earlier kinds do not cover.
    fan = {"whole_house_fan": "whole-house fan", "hvac_fan": "HVAC fan"}.get(snap.fan_mode)
    if (
        snap.occupancy_mode in (OCCUPANCY_AWAY, OCCUPANCY_VACATION)
        and snap.tou_av_start is not None
        and snap.tou_av_end is not None
        and snap.tou_av_expansion_f
        and snap.tou_av_end > snap.tou_av_start
        and snap.tou_av_end > now
    ):
        # Issue #1404 (mirror): inside the widened period the start is behind but the return at its end is still ahead,
        # so the plan keeps that step (it used to drop both once the period began).
        aseg = index.at(max(snap.tou_av_start, now))
        if aseg is not None:
            day_mode = snap.day_modes.get(snap.tou_av_start.date(), aseg.mode)
            widen_ceiling = day_mode != "heat"
            edge = aseg.ceiling + snap.tou_av_expansion_f if widen_ceiling else aseg.floor - snap.tou_av_expansion_f
            label = "vacation" if snap.occupancy_mode == OCCUPANCY_VACATION else "away"
            if snap.tou_av_start > now:
                add(
                    snap.tou_av_start,
                    StepKind.SETPOINT_CHANGE,
                    "setpoint_change.tou_expansion_start",
                    "medium",
                    {
                        "t": snap.tou_av_start,
                        "occupancy": label,
                        "edge": edge,
                        "end": snap.tou_av_end,
                        "side": "ceiling" if widen_ceiling else "floor",
                    },
                    target_f=edge,
                    mode=day_mode,
                )
            add(
                snap.tou_av_end,
                StepKind.SETPOINT_CHANGE,
                "setpoint_change.tou_expansion_end",
                "medium",
                {"t": snap.tou_av_end, "occupancy": label, "floor": aseg.floor, "ceiling": aseg.ceiling},
                target_f=aseg.ceiling if widen_ceiling else aseg.floor,
                mode=day_mode,
            )
    if snap.briefing_time is not None:
        for offset in (0, 1):
            at = datetime.combine(now.date() + timedelta(days=offset), snap.briefing_time, tzinfo=now.tzinfo)
            if now < at < end:
                add(at, StepKind.BRIEFING_SEND, "briefing_send.default", "high", {"t": at})
    if snap.ceiling_breach is not None and snap.ceiling_breach > now:
        cseg = index.at(snap.ceiling_breach)
        if cseg is not None:
            add(
                snap.ceiling_breach,
                StepKind.CEILING_GUARD,
                "ceiling_guard.default",
                "medium",
                {"t": snap.ceiling_breach, "ceiling": cseg.ceiling},
                target_f=cseg.ceiling,
                mode="cool",
            )
    if fan and snap.nat_vent_predicted_start is not None and snap.nat_vent_predicted_start > now:
        add(
            snap.nat_vent_predicted_start,
            StepKind.NAT_VENT_START,
            "natvent_start.predicted",
            "medium",
            {"t": snap.nat_vent_predicted_start, "fan": fan, "cause": "engine_prediction"},
        )

    # window periods (you) and the fan session they allow (ca)
    for cand in candidates:
        if not cand.accepted:
            continue
        key = {"planned": "windows_open.planned", "free_cooling": "windows_open.free_cooling"}.get(
            cand.kind, "windows_open.free_heating"
        )
        until = f" until about {_fmt_time(cand.end, now)}"
        conf = cand.confidence
        open_facts = {
            "t": cand.start,
            "end": cand.end,
            "delta": cand.outdoor_delta_f,
            "until": until,
            "kind": cand.kind,
            "hvac_saved_min": None if cand.hvac_saved_min is None else round(cand.hvac_saved_min, 1),
        }
        oid = None if cand.kind == "open_now" else add(cand.start, StepKind.WINDOWS_OPEN, key, conf, open_facts)
        close_key = {
            "outdoor_rise": "windows_close.outdoor_rise",
            "comfort_edge": "windows_close.comfort_edge",
            "planned_end": "windows_close.planned_end",
            "horizon": "windows_close.horizon",
            "bedtime": "windows_close.bedtime",
        }[cand.end_reason]
        edge_val = _edge_toward(final, cand)
        add(
            cand.end,
            StepKind.WINDOWS_CLOSE,
            close_key,
            conf,
            {"t": cand.end, "edge": edge_val, "reason": cand.end_reason},
        )
        if fan and (oid or (cand.kind == "open_now" and snap.nat_vent_active)):
            if oid:
                add(
                    cand.start,
                    StepKind.NAT_VENT_START,
                    "natvent_start",
                    _confidence_from(conf, "medium"),
                    {"t": cand.start, "fan": fan},
                    after=oid,
                )
            add(
                cand.end,
                StepKind.NAT_VENT_STOP,
                "natvent_stop",
                _confidence_from(conf, "medium"),
                {"t": cand.end, "fan": fan},
            )

    # override / grace: steps inside the hold wait; the engine resumes the schedule at the end
    hold_until: datetime | None = None
    if (snap.manual_override_active or snap.grace_active) and snap.grace_end is not None and snap.grace_end > now:
        hold_until = snap.grace_end
        seg = index.at(hold_until)
        if seg is not None:
            add(
                hold_until,
                StepKind.RESUME_SCHEDULE,
                "resume_schedule",
                "high",
                {"t": hold_until, "floor": seg.floor, "ceiling": seg.ceiling},
            )
    steps = _apply_holds(steps, snap, hold_until, unit, dropped)
    steps.sort(key=lambda s: (s.ts, 0 if s.actor == Actor.CA else 1, s.kind.value))

    # predicted path
    path, source = _choose_path(snap, accepted, final)
    runs = tuple(predicted_runs(final, rates)) if final is not None and rates is not None else ()
    summary = _summary(snap, index, candidates, accepted, runs, path, unit, end, dropped)

    conf = "low"
    if rates is not None:
        conf = _confidence_from(rates.hvac_grade if rates.hvac_grade != "none" else "low", "high")
    band = tuple((s.start, round(s.floor, 1), round(s.ceiling, 1), s.context) for s in segments)
    hourly_min: dict[datetime, float] = {}
    for st in final.steps if final is not None else ():
        if st.hvac_min > 0:
            h0 = (st.ts - timedelta(minutes=SIM_STEP_MINUTES)).replace(minute=0, second=0, microsecond=0)
            hourly_min[h0] = hourly_min.get(h0, 0.0) + st.hvac_min
    points = [PathPoint(t, v) for t, v in path]
    for sensor, pts in snap.sensor_paths.items():
        points.extend(PathPoint(t, v, sensor, snap.sensor_path_model) for t, v in pts)
    points.sort(key=lambda p: (p.sensor, p.ts))
    return DetailedTimeline(
        schema_version=TIMELINE_SCHEMA_VERSION,
        zone=snap.zone,
        generated_at=now,
        day=now.date(),
        horizon_end=end,
        steps=tuple(steps),
        path=tuple(points),
        latched=False,
        regen_reason=_regen_text(regenerated_for),
        path_source=source,
        band=band,
        runs=runs,
        candidates=tuple(candidates),
        summary=tuple(summary),
        grade=conf,
        hvac_minutes=final.hvac_minutes if final is not None else None,
        dropped=tuple(dropped),
        hvac_hourly=tuple(sorted(hourly_min.items())),
        generation=generation,
        regenerated_for=tuple(regenerated_for),
        days=tuple(dict(d) for d in snap.plan_days),
    )


def _regen_text(reasons: Sequence[str]) -> str | None:
    """None for the first timeline of a day (``first`` / ``new_day``); else the comma-joined reasons."""
    shown = [r for r in reasons if r not in ("first", "new_day")]
    return ",".join(shown) if shown else None


def _next_wake(snap: TimelineSnapshot, after: datetime) -> datetime | None:
    if snap.wake_time is None:
        return None
    cand = datetime.combine(after.date(), snap.wake_time, tzinfo=after.tzinfo)
    return cand if cand > after else cand + timedelta(days=1)


def _edge_toward(final: Sim | None, cand: WindowCandidate) -> float | None:
    if final is None:
        return None
    for st in final.steps:
        if abs((st.ts - cand.end).total_seconds()) <= SIM_STEP_MINUTES * 60:
            return st.seg.floor if cand.end_reason == "comfort_edge" else st.seg.ceiling
    return None


def _apply_holds(
    steps: list[TimelineStep],
    snap: TimelineSnapshot,
    hold_until: datetime | None,
    unit: str,
    dropped: list[str],
) -> list[TimelineStep]:
    """Steps inside an override/grace (or while a door pauses CA) are marked held, with the sentence to say so."""
    out: list[TimelineStep] = []
    for s in steps:
        ca_band = s.kind in (StepKind.WAKE_BAND, StepKind.SLEEP_BAND, StepKind.PRECOOL_START, StepKind.PREHEAT_START)
        text, hold = s.text, None
        if (
            hold_until is not None
            and s.ts < hold_until
            and s.kind != StepKind.RESUME_SCHEDULE
            and (s.actor == Actor.CA and ca_band)
        ):
            try:
                text, hold = say("held.suffix", unit, ref=snap.now, base=s.text, until=hold_until), "held.suffix"
            except MissingFact:
                dropped.append("held.suffix")
        elif snap.paused_by_door and s.actor == Actor.CA and ca_band and s.ts - snap.now < timedelta(hours=1):
            text, hold = say("held.pause", unit, base=s.text), "held.pause"
        if hold:
            s = replace(s, text=text, meta={**s.meta, "status": "held", "hold": hold})
        out.append(s)
    return out


def _choose_path(
    snap: TimelineSnapshot, accepted: Sequence[Span], final: Sim | None
) -> tuple[list[tuple[datetime, float]], str]:
    planned = [(a, b) for _k, a, b in _planned_spans(snap, snap.now + timedelta(hours=TIMELINE_HORIZON_HOURS))]
    # the v3 path models today's window plan only: tomorrow's period (R3b) does not make it a different plan
    today = snap.now.date()
    same_as_v3 = sorted(sp for sp in accepted if sp[0].date() == today) == sorted(
        sp for sp in planned if sp[0].date() == today
    )
    if snap.selected_path and same_as_v3:  # the occupant chose another model for the display (Settings)
        return list(snap.selected_path), snap.selected_path_model
    if snap.path_fn is not None:
        try:
            pts = snap.path_fn(list(accepted))
        except Exception:  # noqa: BLE001 - a broken path callback degrades to the next source, never the timeline
            _LOGGER.debug("timeline path_fn failed", exc_info=True)
            pts = None
        if pts:
            return list(pts), "v3" if same_as_v3 else "v3_replanned"
    if snap.v3_path and same_as_v3:
        return list(snap.v3_path), "v3"
    if final is not None:
        return [(s.ts, round(s.temp, 1)) for s in final.steps], "sim"
    return [], "none"


def _summary(
    snap: TimelineSnapshot,
    index: _SegmentIndex,
    candidates: Sequence[WindowCandidate],
    accepted: Sequence[Span],
    runs: Sequence[PredictedRun],
    path: Sequence[tuple[datetime, float]],
    unit: str,
    end: datetime,
    dropped: list[str],
) -> list[str]:
    lines: list[str] = []
    hours = f"{TIMELINE_HORIZON_HOURS} h"
    try:
        if accepted:
            lines.append(say("summary.windows_open", unit, ref=snap.now, count=len(accepted), hours=hours))
        elif candidates or snap.thermal_model is not None:
            lines.append(say("summary.windows_closed", unit, hours=hours, checked=len(candidates)))
    except MissingFact:
        dropped.append("summary")
    shown = [r for r in runs if r.start < snap.now + timedelta(hours=24)][:SUMMARY_MAX_RUNS]
    for r in shown:
        key = "run.heat" if r.mode == "heat" else "run.cool"
        seg = index.at(r.start)
        edge = None if seg is None else (seg.floor if r.mode == "heat" else seg.ceiling)
        try:
            lines.append(say(key, unit, ref=snap.now, minutes=r.minutes, start=r.start, end=r.end, edge=edge))
        except MissingFact:
            dropped.append(key)
    if path and (snap.forecast_age_h is None or snap.forecast_age_h <= FORECAST_MAX_AGE_H):
        day_pts = [(t, v) for t, v in path if 9 <= t.hour < 20 and t.date() == snap.now.date()]
        if day_pts:
            t, v = max(day_pts, key=lambda p: p[1])
            lines.append(say("peak.warmest", unit, ref=snap.now, temp=v, t=t))
    return lines


# ---------------------------------------------------------------------------
# Latch and regeneration
# ---------------------------------------------------------------------------


def _forecast_signature(snap: TimelineSnapshot) -> list[tuple[float, float]]:
    return sorted((d.timestamp(), float(v)) for d, v in snap.hourly if d.tzinfo is not None)


def forecast_shift(
    old: Sequence[tuple[float, float]], new: Sequence[tuple[float, float]], now: datetime
) -> tuple[float, float]:
    """``(mean |change| over the next 12 h, largest change of the next-24-h high or low)`` between two forecasts."""
    if not old or not new:
        return 0.0, 0.0
    t0 = now.timestamp()
    diffs = []
    for h in range(1, 13):
        t = t0 + 3600 * h
        a, b = _interp(old, t), _interp(new, t)
        if a is not None and b is not None:
            diffs.append(abs(b - a))
    mean = sum(diffs) / len(diffs) if diffs else 0.0

    def hi_lo(pts: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
        win = [v for t, v in pts if t0 <= t <= t0 + 86400]
        return (max(win), min(win)) if win else None

    a24, b24 = hi_lo(old), hi_lo(new)
    peak = max(abs(a24[0] - b24[0]), abs(a24[1] - b24[1])) if a24 and b24 else 0.0
    return mean, peak


@dataclass(frozen=True)
class PlanSignature:
    """What a latched timeline was built from; a material change to any of it regenerates."""

    day_type: str
    hvac_mode: str
    windows_recommended: bool
    occupancy: str
    fan_mode: str
    bands: tuple[tuple[float, float, float], ...]  # (segment start epoch, floor, ceiling)
    horizon_end: float  # epoch: bands are only comparable inside both horizons
    paused_by_door: bool
    override: bool
    grace: bool
    automation_enabled: bool
    windows_span: tuple[str, str] | None
    forecast: tuple[tuple[float, float], ...]

    @classmethod
    def of(cls, snap: TimelineSnapshot, segments: Sequence[Segment]) -> PlanSignature:
        today_mode = snap.day_modes.get(snap.now.date(), "")
        span = None
        if snap.windows_recommended and snap.window_open_time and snap.window_close_time:
            span = (snap.window_open_time.isoformat(), snap.window_close_time.isoformat())
        return cls(
            snap.day_type,
            today_mode,
            bool(snap.windows_recommended),
            snap.occupancy_mode,
            snap.fan_mode,
            tuple((s.start.timestamp(), round(s.base_floor, 1), round(s.base_ceiling, 1)) for s in segments),
            segments[-1].end.timestamp() if segments else snap.now.timestamp(),
            bool(snap.paused_by_door),
            bool(snap.manual_override_active),
            bool(snap.grace_active),
            bool(snap.automation_enabled),
            span,
            tuple(_forecast_signature(snap)),
        )


def material_changes(old: PlanSignature, new: PlanSignature, now: datetime) -> list[str]:
    """Why a latched plan must be regenerated, as stable reason codes (empty = keep the latched steps).

    "Material" (documented in docs/operation-timeline-spec.md): a classification change (day type, mode, windows
    recommendation or span), a setpoint or band change (any segment edge moves >= 0.5 F, a segment appears or goes),
    an occupancy change, a fan-mode change, a sensor-driven pause starting or ending, an override or grace starting or
    ending, automation switched on or off, or the forecast moving by more than N (mean change of the next 12 h >= 2 F,
    or the next-24-h high or low moved >= 3 F).
    """
    reasons: list[str] = []
    if (old.day_type, old.hvac_mode) != (new.day_type, new.hvac_mode):
        reasons.append("classification")
    elif old.windows_recommended != new.windows_recommended or old.windows_span != new.windows_span:
        reasons.append("window_plan")
    if old.occupancy != new.occupancy:
        reasons.append("occupancy")
    if old.fan_mode != new.fan_mode:
        reasons.append("fan_mode")
    if _bands_differ(old.bands, new.bands, now.timestamp(), min(old.horizon_end, new.horizon_end)):
        reasons.append("band")
    if old.paused_by_door != new.paused_by_door:
        reasons.append("pause")
    if (old.override, old.grace) != (new.override, new.grace):
        reasons.append("override")
    if old.automation_enabled != new.automation_enabled:
        reasons.append("automation")
    mean, peak = forecast_shift(old.forecast, new.forecast, now)
    if mean >= MATERIAL_FORECAST_MEAN_F or peak >= MATERIAL_FORECAST_PEAK_F:
        reasons.append("forecast")
    return reasons


def _band_in_force(bands: Sequence[tuple[float, float, float]], t: float) -> tuple[float, float] | None:
    best = None
    for start, lo, hi in bands:
        if start <= t:
            best = (lo, hi)
        else:
            break
    return best


def _bands_differ(
    old: Sequence[tuple[float, float, float]],
    new: Sequence[tuple[float, float, float]],
    now_ts: float,
    horizon_ts: float,
) -> bool:
    """True when the band in force differs by >= 0.5 F at any boundary of either list that is still ahead.

    Each list is anchored at its own generation time, so they are compared as functions of time: at every segment
    start of the NEW list (including its first, which is "now") against the OLD list's value at the same instant, and
    at every segment start of the OLD list that is still in the future against the NEW list. A boundary that merely
    slid into the past is therefore not a change.
    """
    probes = {t for t, _l, _h in new if t < horizon_ts} | {t for t, _l, _h in old if now_ts <= t < horizon_ts}
    for t in sorted(probes):
        a, b = _band_in_force(old, t), _band_in_force(new, t)
        if a is None or b is None:
            if (a is None) != (b is None) and t >= now_ts:
                return True
            continue
        if abs(a[0] - b[0]) >= MATERIAL_BAND_SHIFT_F or abs(a[1] - b[1]) >= MATERIAL_BAND_SHIFT_F:
            return True
    return False


@dataclass(frozen=True)
class LatchOutcome:
    timeline: DetailedTimeline
    regenerated: bool
    reasons: tuple[str, ...]


class TimelineLatch:
    """The per-day latch: the day's steps are generated once and kept; only a material change regenerates them.

    Each cycle ``update`` rebuilds a candidate from the fresh snapshot (cheap). If nothing material changed, the latched
    steps stay exactly as promised, except that a window period's close can move EARLIER (the occupant is never told
    to leave windows open longer than first said); the predicted path, runs, band and summary are always the fresh
    ones. A new local day, or a material change, replaces the latched timeline and bumps ``generation``.
    """

    def __init__(self) -> None:
        self._day: date | None = None
        self._timeline: DetailedTimeline | None = None
        self._signature: PlanSignature | None = None
        self._generation = 0

    @property
    def timeline(self) -> DetailedTimeline | None:
        return self._timeline

    def amend(self, fn: Callable[[DetailedTimeline], DetailedTimeline]) -> DetailedTimeline | None:
        """Replace the held timeline with ``fn(timeline)``: a pure advisory transformer (R6: ``apply_night_strategy``).

        The signature and generation are untouched, so an amendment never counts as a plan change; the next
        ``update`` keeps the amended steps (they are latched like any other) and the caller re-applies its transformer
        after every update to refresh what the fresh build replaces (the predicted path)."""
        if self._timeline is None:
            return None
        self._timeline = fn(self._timeline)
        return self._timeline

    def update(self, snap: TimelineSnapshot, *, force: str | None = None) -> LatchOutcome:
        end = snap.now + timedelta(hours=TIMELINE_HORIZON_HOURS)
        segments = build_segments(snap, end)
        sig = PlanSignature.of(snap, segments)
        reasons: list[str] = []
        if self._timeline is None or self._day != snap.now.date():
            reasons = ["new_day" if self._timeline is not None else "first"]
        elif force:
            reasons = [force]
        elif self._signature is not None:
            reasons = material_changes(self._signature, sig, snap.now)
        if reasons:
            self._generation += 1
            tl = replace(build_timeline(snap, generation=self._generation, regenerated_for=reasons), latched=True)
            self._day, self._timeline, self._signature = snap.now.date(), tl, sig
            return LatchOutcome(tl, True, tuple(reasons))
        assert self._timeline is not None
        fresh = build_timeline(snap, generation=self._generation, regenerated_for=self._timeline.regenerated_for)
        kept = _merge_latched(self._timeline, fresh, snap.now, snap.unit)
        self._timeline = kept
        return LatchOutcome(kept, False, ())


# Steps whose time is the engine's live answer each cycle (a rescheduled pre-cool, the plan's ceiling breach, the
# nat-vent start prediction, the grace end): the latch takes the fresh ones, as every surface did before R3b.
LIVE_KINDS = frozenset(
    {StepKind.PRECOOL_START, StepKind.PREHEAT_START, StepKind.CEILING_GUARD, StepKind.RESUME_SCHEDULE}
)


# Issue #1404 (mirror): the away/vacation high-cost widening follows the live TOU resolution, which can appear after the
# plan was latched (an occupancy change regenerates the plan before the next resolution names the window).
_LIVE_REASONS = frozenset(
    {"natvent_start.predicted", "setpoint_change.tou_expansion_start", "setpoint_change.tou_expansion_end"}
)


def _is_live(step: TimelineStep) -> bool:
    return step.kind in LIVE_KINDS or step.reason in _LIVE_REASONS


def _refresh_open_until(steps: list[TimelineStep], now: datetime, unit: str) -> list[TimelineStep]:
    """A window-open step's "until about X" always names the CURRENT close of its period (Issue #1230 X6).

    The latch keeps an open step as first promised (and marks it done once it is past), but a close may move earlier
    afterwards (the plan's ODE cutoff arrives at a later cycle). Without this the done step still read "until about
    4:46 PM" while the close step and the briefing said 11:00 AM: one list, two stories. Only the sentence and its
    ``end`` fact change; the step's time, id and status are untouched."""
    out: list[TimelineStep] = []
    for i, s in enumerate(steps):
        if s.kind == StepKind.WINDOWS_OPEN and "end" in s.facts and s.reason in SENTENCES:
            close = next((c for c in steps[i + 1 :] if c.kind == StepKind.WINDOWS_CLOSE and c.ts > s.ts), None)
            if close is not None and s.facts.get("end") != _jsonable(close.ts):
                until = f" until about {_fmt_time(close.ts, now)}"
                facts = {**s.facts, "t": s.ts, "end": close.ts, "until": until}
                try:
                    text = say(s.reason, unit, ref=now, **facts)
                except MissingFact:
                    text = s.text
                s = replace(
                    s, text=text, meta={**s.meta, "facts": {**s.facts, "end": _jsonable(close.ts), "until": until}}
                )
        out.append(s)
    return out


def _move_natvent_stops(
    steps: list[TimelineStep], fresh: DetailedTimeline, moved: Mapping[datetime, datetime]
) -> list[TimelineStep]:
    """A nat-vent stop follows its window close when the latch moves that close earlier (Issue #1439, the #1436 mirror).

    ``build_timeline`` emits the fan session's stop at the same instant as its period's close. The latch let the close
    move earlier but kept the stop as first promised, so the list read "9:50 AM: close the windows" then "10:00 AM: CA
    stops the whole-house fan as the window period ends", and the chart's nat-vent strip ran past the close. The stop
    takes the fresh plan's stop at the new close time (same sentence table); without one it is left as it was.
    """
    if not moved:
        return steps
    fresh_stops = {f.ts: f for f in fresh.steps if f.kind == StepKind.NAT_VENT_STOP}
    present = {s.id for s in steps}
    out: list[TimelineStep] = []
    for s in steps:
        new_ts = moved.get(s.ts) if s.kind == StepKind.NAT_VENT_STOP else None
        if new_ts is not None and new_ts in fresh_stops and s.status != "done":
            repl = fresh_stops[new_ts]
            if repl.id in present and repl.id != s.id:
                continue  # the plan already holds the stop at the new close: drop the stale one
            s = repl
        out.append(s)
    return out


def _merge_latched(
    latched: DetailedTimeline, fresh: DetailedTimeline, now: datetime, unit: str = "fahrenheit"
) -> DetailedTimeline:
    """Latched steps kept as promised (past ones marked done); a window close may only move EARLIER.

    Path, runs, band, candidates and summary are always the fresh ones: only the step list is latched.
    """
    fresh_closes = [f for f in fresh.steps if f.kind == StepKind.WINDOWS_CLOSE and f.ts >= now]
    window_kinds = (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE)
    latched_window_ts = sorted(w.ts for w in latched.steps if w.kind in window_kinds)
    used: set[int] = set()
    moved: dict[datetime, datetime] = {}  # latched close time -> the earlier close that replaced it
    steps: list[TimelineStep] = []
    for s in latched.steps:
        if s.meta.get("source_module") == "night_strategy":
            continue  # R6: night strategy steps are re-applied by the caller after every update, never latched
        if _is_live(s) and s.ts >= now:
            continue  # replaced below by the fresh answer
        if s.kind == StepKind.WINDOWS_CLOSE and s.ts >= now:
            # The replacement must close THIS period: strictly after the latched window step before it (its own open,
            # or the previous period's close for a period already open). Without that bound, tomorrow morning's close
            # was swapped for tonight's bedtime close (11.5 h earlier): the plan said "close the windows before you
            # turn in" twice and tomorrow's window period lost its close. A fresh close is used at most once.
            prev = max((t for t in latched_window_ts if t < s.ts), default=None)
            earlier = [
                f
                for f in fresh_closes
                if f.ts < s.ts
                and s.ts - f.ts <= timedelta(hours=12)
                and (prev is None or f.ts > prev)
                and id(f) not in used
            ]
            repl = min(earlier, key=lambda f: s.ts - f.ts, default=None)  # the nearest earlier close
            if repl is not None:
                used.add(id(repl))
                moved[s.ts] = repl.ts
                s = repl
        if s.ts < now and s.status == "planned":
            s = replace(s, meta={**s.meta, "status": "done"})
        steps.append(s)
    steps.extend(f for f in fresh.steps if _is_live(f) and f.ts >= now)
    steps = _move_natvent_stops(steps, fresh, moved)
    steps.sort(key=lambda s: (s.ts, 0 if s.actor == Actor.CA else 1, s.kind.value))
    steps = _refresh_open_until(steps, now, unit)
    return replace(
        fresh,
        generated_at=latched.generated_at,
        day=latched.day,
        steps=tuple(steps),
        latched=True,
        regen_reason=latched.regen_reason,
        generation=latched.generation,
        regenerated_for=latched.regenerated_for,
    )


def _register_night_sentences() -> None:
    """Plan section 9: the night strategy's sentences live in the one sentence table (R6 registration).

    ``night_strategy`` renders them itself (its temperatures are rounded to the half degree before conversion) and
    imports this module lazily, so the table is filled here once, at import, without a circular import."""
    from .night_strategy import NIGHT_SENTENCES  # noqa: PLC0415

    for key, ns_ in NIGHT_SENTENCES.items():
        SENTENCES.setdefault(
            key,
            Sentence(
                key,
                ns_.template,
                ns_.facts,
                ns_.source,
                ns_.guard,
                "advice only (DP-7): CA never starts a fan for a strategy; occupant steps only for an offered grade",
            ),
        )


try:
    _register_night_sentences()
except Exception:  # noqa: BLE001 - a broken prediction engine must cost the night plan, never the whole timeline
    _LOGGER.warning("night strategy sentences could not be registered; the night plan is off", exc_info=True)
