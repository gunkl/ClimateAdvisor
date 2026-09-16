"""Time-of-Use (TOU) scheduler — schedule storage/resolution + pre-conditioning phase
decision (Issue #786).

A stateless-helper module (the ``fan_lifecycle.py`` precedent: a dedicated module the
automation engine and coordinator *consult*, never merged into either). Owns:

- The ``Schedule`` record shape (up to 5 stored in ``config_entry.data["schedules"]`` —
  the cap is enforced by the config-flow layer, not here; see ``MAX_SCHEDULES``).
- ``resolve_active_schedules()`` — is a ``cost_period`` schedule covering *this instant*,
  and if so which cost tag? Absence of any covering schedule is implicit "normal" cost —
  there is no explicit "normal" tag (confirmed design decision).
- ``resolve_tou_phase()`` — the pre-conditioning decision: is an upcoming high-cost
  schedule close enough that the home should start banking toward the opposite comfort-
  band edge right now, using the learned thermal rate to size the lead time?

Root request (Issue #11 comment, DeppressedCabbage): pre-cool/pre-heat a few degrees
before a scheduled high electricity-rate window. This module generalizes that to
arbitrary day-of-week/time windows without any user-authored temperature field — the
banking target is always the home's own existing comfort-band edge (or the sleep-window
edge, if the pre-conditioning lead time overlaps the sleep schedule), and the lead time is
computed from the home's own learned thermal response rate. See
``docs/scheduler-spec.md`` for the full contract once written.

**Coast phase needs no code here (or anywhere) — confirmed, not assumed** (Issue #786
plan prerequisite check, ``tests/test_tou_precondition.py``): ``automation.py``'s
``_apply_comfort_band()`` issues a single-setpoint *threshold* command (a real thermostat
only acts once indoor crosses that edge), so once pre-conditioning stops being called,
the normal 30-minute cycle already re-arms exactly the right "coast until this edge"
command for free. This module therefore only ever needs to decide when to *start*
pre-conditioning — there is no ``COASTING`` state to model.

**Midnight/day-of-week resolution.** ``_compute_target_band_schedule()``
(``coordinator.py``) already solves midnight wraparound for a single wake/sleep pair via
an ``h_n = h + 24 if (night_owl and h < wake_h) else h`` numeric shift — but that idiom
alone is insufficient once day-of-week is added: shifting the hour by +24 silently
attributes the post-midnight portion to *whatever calendar day "now" is*, which is wrong
for a schedule like "Friday 11pm-1am" evaluated at 12:30am Saturday — the post-midnight
portion is still Friday's schedule. This module instead tests day-of-week membership
against two explicit calendar days (today and yesterday) rather than reusing the pure
numeric shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from enum import Enum

from .automation import _in_sleep_window
from .const import (
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    OCCUPANCY_VACATION,
    TOU_SETBACK_PRECOND_MAX_DELTA_F,
    VACATION_SETBACK_EXTRA,
)
from .nat_vent_gate import resolve_comfort_cool, resolve_comfort_heat
from .thermal_lead_time import compute_lead_minutes_from_rate

MAX_SCHEDULES = 5  # enforced by config_flow.py, not this module — see module docstring

ALL_DAYS = "all"
WEEKDAY_ABBREVS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

COST_TAG_HIGH = "high"
COST_TAG_LOW = "low"

# Pre-conditioning lead-time bounds (Issue #786) — a distinct trigger from the ODE
# ceiling guard's own bounds (ode_ceiling_guard.py's _LEAD_MIN_FLOOR/_LEAD_MIN_CEIL),
# scoped independently since TOU banking and weather-driven proactive cooling are two
# separate reasons to pre-condition (see automation.py's resolve_pre_cool_modifier()
# docstring on why these stay architecturally separate).
_TOU_LEAD_MIN_FLOOR = 30.0
_TOU_LEAD_MIN_CEIL = 240.0
_TOU_LEAD_MIN_SAFETY_MULTIPLIER = 1.3
# Default when the "default_tou_lead_minutes" config key is absent (e.g. an entry
# created before Issue #797 added it) — kept in sync with const.py's
# DEFAULT_TOU_LEAD_MINUTES, which config_flow.py uses as the form's default.
_TOU_LEAD_MIN_FALLBACK = 45.0

# How far ahead resolve_tou_phase() looks for an upcoming schedule start. Must be >=
# _TOU_LEAD_MIN_CEIL so the longest possible computed lead time is never missed.
_LOOKAHEAD = timedelta(minutes=_TOU_LEAD_MIN_CEIL)


@dataclass(frozen=True)
class Schedule:
    """One user-authored cost_period schedule (Issue #786 requirement 5/6 — no
    temperature fields; only day-of-week/time/cost-tag)."""

    id: str
    name: str
    days: tuple[str, ...]  # 3-letter weekday abbrevs (see WEEKDAY_ABBREVS), or (ALL_DAYS,)
    start: str  # "HH:MM" or "HH:MM:SS" (HA's TimeSelector's native format) — local wall-clock,
    end: str  # civil time, not an elapsed duration. _parse_hhmm() ignores any seconds component.
    cost_tag: str  # COST_TAG_HIGH | COST_TAG_LOW


@dataclass(frozen=True)
class ScheduleResolution:
    """What's true about scheduled cost right now."""

    cost_tag: str | None  # None = implicit normal (no covering schedule)
    active_schedule_ids: tuple[str, ...]
    schedule_end: datetime | None  # for Status-card "ends HH:MM" display


class TOUPhase(Enum):
    NONE = "none"
    PRECONDITIONING = "preconditioning"


@dataclass(frozen=True)
class TOUPhaseResolution:
    """``phase`` answers "should automation act right now" (PRECONDITIONING only while
    ``now`` sits inside ``[precondition_start, schedule_start)``). The other fields are
    populated whenever a qualifying upcoming ``high`` schedule was found within the
    lookahead window, REGARDLESS of ``phase`` — a caller rendering a chart's future band
    (coordinator.py's ``_compute_target_band_schedule()``) needs the full window shape for
    timestamps that aren't "now", not just the current instant's act-or-not answer."""

    phase: TOUPhase
    target: float | None  # the banking setpoint
    mode: str | None  # "heat" | "cool"
    schedule_id: str | None
    schedule_start: datetime | None
    precondition_start: datetime | None  # window's start instant — schedule_start - lead_minutes
    # Issue #899, away/vacation only (always None for the Home/Guest resolution built by
    # resolve_tou_phase()): half the capped split-delta — how many degrees the away/
    # vacation band edge should temporarily widen by while the TOU window is active.
    # `target` above doubles as the precool/preheat depth for this resolution (the other
    # half of the same capped delta), so nothing new is needed there.
    expansion_f: float | None = None
    # Issue #899, away/vacation only: the instant the TOU window ITSELF ends (schedule_start
    # + the schedule's own duration) — needed to know when the temporary band expansion
    # should revert, since (unlike Home/Guest's open-ended "coast" phase) away/vacation's
    # widened edge is only correct for the duration of the window, not indefinitely after.
    window_end: datetime | None = None


def _parse_hhmm(value: str) -> float:
    """Parse "HH:MM" or "HH:MM:SS" (HA's TimeSelector returns the latter) into hours.

    Only the hour/minute components matter for schedule-boundary resolution — any
    seconds component is ignored.
    """
    parts = value.split(":")
    return int(parts[0]) + int(parts[1]) / 60.0


def _weekday_abbrev(moment: datetime) -> str:
    return WEEKDAY_ABBREVS[moment.weekday()]


def _days_match(days: tuple[str, ...], weekday: str) -> bool:
    return ALL_DAYS in days or weekday in days


def is_schedule_active_at(schedule: Schedule, now: datetime) -> bool:
    """Is ``schedule`` active at ``now`` — civil-time, day-of-week aware, midnight-safe.

    All hour comparisons use local wall-clock ``now.hour + now.minute/60.0`` on an
    already-``dt_util``-local-aware timestamp, matching ``_compute_target_band_schedule``'s
    civil-time convention (schedule boundaries are civil-time definitions, not elapsed
    durations).
    """
    if not schedule.days:
        return False
    start_h = _parse_hhmm(schedule.start)
    end_h = _parse_hhmm(schedule.end)
    h = now.hour + now.minute / 60.0
    today = _weekday_abbrev(now)

    if end_h <= start_h:
        # Crosses midnight: two explicit day-windows, not a single h_n += 24 shift —
        # see module docstring.
        yesterday = _weekday_abbrev(now - timedelta(days=1))
        if _days_match(schedule.days, today) and start_h <= h < 24:
            return True
        return _days_match(schedule.days, yesterday) and 0 <= h < end_h

    return _days_match(schedule.days, today) and start_h <= h < end_h


def _schedule_duration_hours(schedule: Schedule) -> float:
    """Civil-time duration of ``schedule``'s window, midnight-safe (Issue #899).

    Unlike ``_schedule_end_datetime()``, this needs no ``now`` and doesn't require the
    schedule to already be active — it's a pure property of the schedule's own start/end,
    used to size how far the house is predicted to drift over the *upcoming* window, before
    it has begun.
    """
    start_h = _parse_hhmm(schedule.start)
    end_h = _parse_hhmm(schedule.end)
    if end_h <= start_h:
        return (24.0 - start_h) + end_h
    return end_h - start_h


def _schedule_end_datetime(schedule: Schedule, now: datetime) -> datetime:
    """The datetime this *currently active* schedule's window ends (today, or tomorrow
    for a midnight-spanning schedule whose end falls after midnight). Caller must have
    already confirmed the schedule is active at ``now``."""
    end_h = _parse_hhmm(schedule.end)
    start_h = _parse_hhmm(schedule.start)
    end_hour, end_minute = int(end_h), round((end_h - int(end_h)) * 60)
    end_today = datetime.combine(now.date(), dt_time(end_hour, end_minute), tzinfo=now.tzinfo)
    if end_h <= start_h and now.hour + now.minute / 60.0 >= start_h:
        # We're in the pre-midnight portion of a midnight-spanning window — the end
        # instant is tomorrow.
        return end_today + timedelta(days=1)
    return end_today


def resolve_active_schedules(schedules: list[Schedule], now: datetime) -> ScheduleResolution:
    """What's true about scheduled cost right now — the "is any cost_period active"
    check used for Status-card display and (independently) as an input to
    ``resolve_tou_phase()``'s own currently-active check.

    Multiple simultaneously-active schedules are resolved independently (no ``elif``
    short-circuit) — if two schedules of conflicting cost_tag ever overlap, both ids are
    reported and the first-listed tag wins (a config-flow-layer concern to warn about,
    not something this resolver hides).
    """
    active = [s for s in schedules if is_schedule_active_at(s, now)]
    if not active:
        return ScheduleResolution(cost_tag=None, active_schedule_ids=(), schedule_end=None)

    winner = active[0]
    return ScheduleResolution(
        cost_tag=winner.cost_tag,
        active_schedule_ids=tuple(s.id for s in active),
        schedule_end=_schedule_end_datetime(winner, now),
    )


def _next_start_within(schedule: Schedule, now: datetime, lookahead: timedelta) -> datetime | None:
    """The next datetime (today or tomorrow) ``schedule``'s window *begins*, if it falls
    within ``[now, now + lookahead]``; else ``None``. Only considers future starts —
    pre-conditioning only matters before a window begins, not while already active."""
    if not schedule.days:
        return None
    start_h = _parse_hhmm(schedule.start)
    start_hour, start_minute = int(start_h), round((start_h - int(start_h)) * 60)
    for day_offset in (0, 1):
        candidate_date = (now + timedelta(days=day_offset)).date()
        candidate = datetime.combine(candidate_date, dt_time(start_hour, start_minute), tzinfo=now.tzinfo)
        if not _days_match(schedule.days, _weekday_abbrev(candidate)):
            continue
        if now <= candidate <= now + lookahead:
            return candidate
    return None


def resolve_tou_phase(
    schedules: list[Schedule],
    now: datetime,
    current_indoor_temp: float | None,
    hvac_mode: str,
    thermal_model: dict | None,
    config: dict,
) -> TOUPhaseResolution:
    """Should the home start pre-conditioning right now for an upcoming ``high``-tagged
    schedule?

    Direction follows the day's own anticipated HVAC need (mirrors the original request:
    pre-*cool* before a high-cost window on a cooling day, pre-*heat* before one on a
    heating day) — a warm/off day with no clear direction is skipped (``TOUPhase.NONE``):

    - ``hvac_mode == "cool"``: bank toward the comfort-band FLOOR (``comfort_heat``, or
      ``sleep_heat`` if the lead-time window overlaps the sleep schedule — via
      ``resolve_comfort_heat()``), driven by cooling now (``k_active_cool``).
    - ``hvac_mode == "heat"``: bank toward the comfort-band CEILING (``comfort_cool``,
      sleep-aware via ``resolve_comfort_cool()``), driven by heating now
      (``k_active_heat``).

    Returns the EARLIEST qualifying upcoming ``high`` schedule's decision (schedules are
    evaluated in list order; ties resolve to whichever starts soonest).
    """
    if current_indoor_temp is None or hvac_mode not in ("heat", "cool"):
        return TOUPhaseResolution(TOUPhase.NONE, None, None, None, None, None)

    thermal_model = thermal_model or {}
    candidates: list[tuple[datetime, Schedule]] = []
    for schedule in schedules:
        if schedule.cost_tag != COST_TAG_HIGH:
            continue
        start_at = _next_start_within(schedule, now, _LOOKAHEAD)
        if start_at is not None:
            candidates.append((start_at, schedule))

    if not candidates:
        return TOUPhaseResolution(TOUPhase.NONE, None, None, None, None, None)

    candidates.sort(key=lambda pair: pair[0])
    schedule_start, schedule = candidates[0]

    in_sleep_window_at_start = _in_sleep_window(schedule_start, config)
    if hvac_mode == "cool":
        target = resolve_comfort_heat(
            comfort_heat_raw=float(config.get("comfort_heat", 68.0)),
            sleep_heat=float(config.get("sleep_heat", 64.0)),
            in_sleep_window=in_sleep_window_at_start,
        )
        rate = thermal_model.get("k_active_cool")
        mode = "cool"
    else:
        target = resolve_comfort_cool(
            comfort_cool_raw=float(config.get("comfort_cool", 74.0)),
            sleep_cool=float(config.get("sleep_cool", 72.0)),
            in_sleep_window=in_sleep_window_at_start,
        )
        rate = thermal_model.get("k_active_heat")
        mode = "heat"

    delta_t = abs(current_indoor_temp - target)
    fallback_minutes = float(config.get("default_tou_lead_minutes", _TOU_LEAD_MIN_FALLBACK))
    lead_minutes = compute_lead_minutes_from_rate(
        delta_t=delta_t,
        rate=rate,
        min_minutes=_TOU_LEAD_MIN_FLOOR,
        max_minutes=_TOU_LEAD_MIN_CEIL,
        safety_multiplier=_TOU_LEAD_MIN_SAFETY_MULTIPLIER,
        fallback_minutes=fallback_minutes,
    )

    precondition_start = schedule_start - timedelta(minutes=lead_minutes)
    phase = TOUPhase.PRECONDITIONING if precondition_start <= now < schedule_start else TOUPhase.NONE
    return TOUPhaseResolution(phase, target, mode, schedule.id, schedule_start, precondition_start)


def resolve_tou_away_vacation_phase(
    schedules: list[Schedule],
    now: datetime,
    current_indoor_temp: float | None,
    current_outdoor_temp: float | None,
    hvac_mode: str,
    thermal_model: dict | None,
    config: dict,
    occupancy_mode: str,
) -> TOUPhaseResolution:
    """Away/Vacation TOU pre-conditioning (Issue #899, grounded in Issue #892).

    Unlike ``resolve_tou_phase()`` (Home/Guest), this does NOT bank toward the comfort-band
    edge — that would waste energy conditioning a home nobody occupies. Instead it predicts
    how many degrees the house would passively drift during the upcoming high-cost window
    (from the home's own envelope decay rate, NOT the HVAC's active rate — see below), caps
    that at ``TOU_SETBACK_PRECOND_MAX_DELTA_F``, and splits the capped amount in half:
    half becomes precool/preheat DEPTH below/above the away/vacation setback edge (the
    ``target`` this resolution returns), half becomes a temporary EXPANSION of that same
    edge for the duration of the window (``expansion_f``) — applied by
    ``select_comfort_band()``'s ``tou_expansion_f`` parameter, not by this module. Worked
    example (Issue #892, the reporting user's own numbers): setpoint 76°F, ~6°F predicted
    drift over a 4.4hr window → precool to 73°F before the window, ceiling widens to 79°F
    for the window's duration; the house drifts 73→79°F without the AC ever running during
    the expensive period.

    **The rate used for `required_delta` is deliberately NOT `k_active_cool`/`k_active_heat`
    (the HVAC's own active-conditioning rate) — it's `k_passive * (indoor - outdoor)`**, the
    same passive-envelope-decay formula already used at ``automation.py``'s nat-vent floor-
    imminence guard and ``nat_vent_exit.py``'s proactive-floor exit. "How far will the house
    drift on its own while the HVAC sits idle during the TOU window" is a passive-envelope
    question, not a question about how fast the HVAC can move the temperature while running
    — `k_active_*` remains correct for the lead-time calculation below (that phase genuinely
    is HVAC-driven), but using it for `required_delta` would answer the wrong question.

    When `k_passive` isn't confidently known yet (no observations, or `confidence_k_passive`/
    `confidence` is "none"), `required_delta` falls back to the cap itself
    (`TOU_SETBACK_PRECOND_MAX_DELTA_F`) rather than skipping the feature — the same "degrade
    gracefully, don't go inert" precedent `compute_lead_minutes_from_rate()`'s own
    `fallback_minutes` already establishes for the adjacent lead-time half of this same
    calculation. This is a real, intentional tradeoff: a home with no thermal-model
    confidence yet gets the maximum allowed precondition/expansion rather than none, until
    its model gains confidence.

    Returns the EARLIEST qualifying upcoming ``high`` schedule's decision, same convention
    as ``resolve_tou_phase()``. ``expansion_f``/``window_end`` are populated whenever a
    qualifying schedule was found, REGARDLESS of ``phase`` — same rationale as
    ``resolve_tou_phase()``'s own docstring (a caller needs the full window shape for
    timestamps that aren't "now", not just the current instant's answer).

    **A currently-ACTIVE high-cost window takes priority over an upcoming one** (checked
    first, via ``is_schedule_active_at()``) — this matters specifically for away/vacation
    (unlike ``resolve_tou_phase()``, which never needs this): once a window's own start
    time has passed, ``_next_start_within()`` can no longer find it (it only searches
    FUTURE starts), so without this check the coordinator's "is the window active right
    now, should the band be expanded" logic would never see the very window it started
    pre-conditioning for. When a schedule is active, ``schedule_start``/``window_end`` are
    derived from it directly (``_schedule_end_datetime()`` minus the schedule's own
    duration) and ``phase`` is correctly ``NONE`` (``PRECONDITIONING`` only ever means
    "before the window, actively driving toward the precool/preheat target" — once inside
    the window the coordinator reads ``window_end`` directly rather than ``phase``).
    """
    if hvac_mode not in ("heat", "cool"):
        return TOUPhaseResolution(TOUPhase.NONE, None, None, None, None, None)

    thermal_model = thermal_model or {}

    active_schedule = next(
        (s for s in schedules if s.cost_tag == COST_TAG_HIGH and is_schedule_active_at(s, now)), None
    )
    if active_schedule is not None:
        schedule = active_schedule
        window_end = _schedule_end_datetime(schedule, now)
        schedule_start = window_end - timedelta(hours=_schedule_duration_hours(schedule))
    else:
        candidates: list[tuple[datetime, Schedule]] = []
        for schedule in schedules:
            if schedule.cost_tag != COST_TAG_HIGH:
                continue
            start_at = _next_start_within(schedule, now, _LOOKAHEAD)
            if start_at is not None:
                candidates.append((start_at, schedule))

        if not candidates:
            return TOUPhaseResolution(TOUPhase.NONE, None, None, None, None, None)

        candidates.sort(key=lambda pair: pair[0])
        schedule_start, schedule = candidates[0]
        window_end = schedule_start + timedelta(hours=_schedule_duration_hours(schedule))

    setback_heat = float(config.get("setback_heat", DEFAULT_SETBACK_HEAT))
    setback_cool = float(config.get("setback_cool", DEFAULT_SETBACK_COOL))
    if occupancy_mode == OCCUPANCY_VACATION:
        setback_heat -= VACATION_SETBACK_EXTRA
        setback_cool += VACATION_SETBACK_EXTRA

    k_passive = thermal_model.get("k_passive")
    confidence = thermal_model.get("confidence_k_passive", thermal_model.get("confidence", "none"))
    if (
        k_passive is not None
        and k_passive < 0
        and confidence in ("medium", "high")
        and current_indoor_temp is not None
        and current_outdoor_temp is not None
    ):
        passive_rate = k_passive * (current_indoor_temp - current_outdoor_temp)  # °F/hr
        required_delta = abs(passive_rate) * _schedule_duration_hours(schedule)
    else:
        # No confident passive rate — reuse the cap itself as the fallback degree value
        # (see docstring above) rather than doing nothing.
        required_delta = TOU_SETBACK_PRECOND_MAX_DELTA_F

    capped_delta = min(required_delta, TOU_SETBACK_PRECOND_MAX_DELTA_F)
    half = capped_delta / 2.0

    if hvac_mode == "cool":
        target = setback_cool - half  # precool depth below the away/vacation ceiling
        rate = thermal_model.get("k_active_cool")
        mode = "cool"
    else:
        target = setback_heat + half  # preheat depth above the away/vacation floor
        rate = thermal_model.get("k_active_heat")
        mode = "heat"

    fallback_minutes = float(config.get("default_tou_lead_minutes", _TOU_LEAD_MIN_FALLBACK))
    lead_minutes = compute_lead_minutes_from_rate(
        delta_t=half,
        rate=rate,
        min_minutes=_TOU_LEAD_MIN_FLOOR,
        max_minutes=_TOU_LEAD_MIN_CEIL,
        safety_multiplier=_TOU_LEAD_MIN_SAFETY_MULTIPLIER,
        fallback_minutes=fallback_minutes,
    )

    precondition_start = schedule_start - timedelta(minutes=lead_minutes)
    phase = TOUPhase.PRECONDITIONING if precondition_start <= now < schedule_start else TOUPhase.NONE
    return TOUPhaseResolution(
        phase, target, mode, schedule.id, schedule_start, precondition_start, expansion_f=half, window_end=window_end
    )
