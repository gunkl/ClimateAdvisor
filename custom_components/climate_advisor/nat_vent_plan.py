"""Single source of truth for warm/mild-day natural-ventilation timing (Issue #817).

Before this module existed, "when should windows close" was computed independently
in briefing.py (called from generate_briefing() for the TLDR table and conversational
body) and in coordinator.py (_compute_next_automation_action(), for the "Next
Automation" status card). Nothing stopped a third caller from doing the same thing a
fourth way — which is exactly how #528 silently reintroduced a duplicate 2 days after
#518 promised there'd never be one. compute_nat_vent_plan() is now the only place this
math happens; callers read the result, they never re-derive it.

Dependency-free by design (same shape as storage_paths.py / fan_status.py /
thermal_lead_time.py): takes pre-built prediction curves and a plain callable, never
imports from briefing.py, coordinator.py, or automation.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

from .const import CEILING_PRECOOL_FALLBACK_MIN
from .nat_vent_gate import resolve_comfort_heat
from .temperature import find_temperature_crossing
from .thermal_lead_time import compute_lead_minutes_from_rate

_LOGGER = logging.getLogger(__name__)

_NAT_VENT_CUTOFF_MARGIN_F = 1.0  # forecast-hour margin — distinct from the live-control gates'
# own boundary choices (nat_vent_gate.py's strict <, fan_thermostat_decision.py's non-strict >=);
# this is a PREDICTIVE identification of "the hour nat-vent stops being viable", not a live
# control decision, so a small conservative buffer is appropriate here specifically.


def _nat_vent_cutoff_reached(outdoor_temp: float, indoor_temp: float) -> bool:
    """Architecture-reset (Issue #429 consolidation): the shared outdoor-vs-indoor
    predicate compute_nat_vent_plan() hand-rolled as `outdoor >= indoor - 1.0` —
    now a single shared definition. This is only half of the real activation gate's
    predicate — see the comfort-floor scan in compute_nat_vent_plan() (Issue #535)."""
    return outdoor_temp >= indoor_temp - _NAT_VENT_CUTOFF_MARGIN_F


def describe_nat_vent_cutoff_reason(reason: str | None) -> str:
    """Single source of truth for how ``nat_vent_cutoff_reason`` reads as text (Issue #847).

    Before this existed, ``briefing.py``'s ``_warm_day_plan()`` and ``coordinator.py``'s
    ``_compute_next_automation_action()`` each independently decided how to phrase the
    same ``nat_vent_cutoff_reason`` value from the shared ``nat_vent_plan`` dict —
    ``_warm_day_plan()`` said "hold the heat in" for ``comfort_floor``, while the Next
    Automation card said "outdoor will stop helping" unconditionally, regardless of
    which reason actually won. ``_mild_day_plan()`` had no branch at all. That let the
    two dashboard surfaces show contradictory framing for one underlying fact even
    after #814/#817/#818 already unified the *time* onto one cached
    ``self._nat_vent_plan``. This mirrors that fix for the *wording*: exactly one
    function decides what each reason means in words; every consumer calls it and
    builds its own sentence/phrase shape around the returned fragment.

    Any new field added to ``nat_vent_plan`` that gets rendered as user-facing text in
    more than one place should get the same treatment — route through a shared
    function here, never a second inline ``if reason == ...`` branch in a consumer.

    Args:
        reason: "comfort_floor", "outdoor_rise", or None (mirrors
            ``compute_nat_vent_plan()``'s ``nat_vent_cutoff_reason`` return value).

    Returns:
        A short, lower-case comfort-impact phrase fragment describing *why* windows
        should close — no automation-mechanism words (Status Card Ontology,
        CLAUDE.md §Status Card Ontology), so it's safe to embed in both a full
        conversational sentence (briefing.py) and a compact status-card phrase
        (coordinator.py):
          "comfort_floor"      -> "to hold the heat in"
          "outdoor_rise"/None  -> "before outdoor air warms past indoor"
    """
    if reason == "comfort_floor":
        return "to hold the heat in"
    return "before outdoor air warms past indoor"


def compute_nat_vent_plan(
    predicted_indoor: list[dict] | None,
    predicted_outdoor: list[dict] | None,
    comfort_cool: float,
    k_active_cool: float | None = None,
    comfort_heat_raw: float | None = None,
    sleep_heat: float | None = None,
    in_sleep_window_fn: Callable[[datetime], bool] | None = None,
    window_open_time: time | None = None,
) -> dict:
    """Derive warm/mild-day timing events from ODE predicted curves.

    Args:
        comfort_heat_raw, sleep_heat, in_sleep_window_fn: optional (Issue #535) — when
            all three are provided, nat_vent_cutoff also scans for the comfort-floor
            crossing the real activation gate (decide_nat_vent_gate() in
            nat_vent_gate.py) requires (`indoor > comfort_heat`) but this predictive
            scan previously never modeled. When omitted, behavior is unchanged from
            before #535 (outdoor-crossing only).
        window_open_time: optional (Issue #814 follow-up) — neither the outdoor-crossing
            nor comfort-floor scan was bounded to start no earlier than when nat-vent
            could actually begin, so an overnight passive-decay floor-crossing (windows
            still closed, HVAC off) could be found and reported as the nat_vent_cutoff
            even though it occurs at or before the window even opens — producing a
            displayed "Open 6:00 AM – 6:00 AM" (or an even earlier, pre-open close time).
            When given, both scans only consider timestamps whose time-of-day is >= this
            value. Omitted (None), behavior is unchanged (existing callers/tests that
            don't pass it keep today's unbounded scan).

    Returns a dict with keys:
      nat_vent_cutoff: datetime | None — earlier of the outdoor-crossing and (if the
          three optional params are given) comfort-floor crossing
      nat_vent_cutoff_reason: str | None — "outdoor_rise" or "comfort_floor", whichever
          produced nat_vent_cutoff; None if nat_vent_cutoff is None
      comfort_floor_crossing_time: datetime | None — Issue #821: the comfort-floor
          crossing, populated whenever the scan finds one, REGARDLESS of whether it
          also won nat_vent_cutoff (unlike nat_vent_cutoff/nat_vent_cutoff_reason above,
          which only surface it when it's the earlier of the two crossings). Consumed
          by ``ode_floor_guard.py`` — a heat-day-agnostic floor-crossing scan is needed
          for the comfort-floor defense fix independent of which crossing wins the
          nat-vent-specific cutoff race. Still computed exactly once, here, per Issue
          #817's single-source-of-truth architecture — ``ode_floor_guard.py`` reads this
          cached field rather than re-scanning ``predicted_indoor`` itself.
      ceiling_breach_time: datetime | None — first hour indoor > comfort_cool
      precool_start_time: datetime | None — ceiling_breach_time minus computed lead
      any_nat_vent_window: bool — True if outdoor < indoor at any point
      nat_vent_recovers: bool — True if outdoor drops back below indoor after cutoff.
          Issue #788: only ever computed for the "outdoor_rise" cutoff reason — always
          False for "comfort_floor" cutoffs (left at its initial-dict default, never
          overwritten). A "comfort_floor" cutoff fires specifically in the branch where
          outdoor_crossing did NOT win the race (see the `elif floor_crossing is not
          None` below), which means outdoor is already below indoor at cutoff time — the
          "recovery" test this field reports on was never actually unmet, so treating it
          as a genuine later event is a false positive that told occupants to reopen
          windows minutes after being told to close them.
      recovery_time: datetime | None — first timestamp after cutoff where outdoor < indoor
          again. Same "outdoor_rise"-only restriction as nat_vent_recovers above; always
          None for "comfort_floor" cutoffs.
    """
    result: dict = {
        "nat_vent_cutoff": None,
        "nat_vent_cutoff_reason": None,
        "comfort_floor_crossing_time": None,
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

    def _after_open(ts: datetime) -> bool:
        # Strict > , not >= : a crossing found in the exact same hour windows open
        # would still render as a zero-width "Open 6:00 AM – 6:00 AM" — require the
        # close time to be strictly later than the open time for it to be worth showing.
        return window_open_time is None or ts.time() > window_open_time

    outdoor_crossing = find_temperature_crossing(
        predicted_indoor, predicted_outdoor, lambda ts, o, i: _after_open(ts) and _nat_vent_cutoff_reached(o, i)
    )

    # Issue #535: comfort-floor crossing — the real activation gate (decide_nat_vent_gate())
    # requires indoor > comfort_heat as one of its four conditions; this predictive scan
    # previously never modeled that term. Only reads the indoor curve (no outdoor pairing
    # needed — same shape as ceiling_breach_time below), but still requires a matching
    # entry in predicted_outdoor via find_temperature_crossing() so it can only fire at a
    # timestamp both curves actually cover.
    #
    # Issue #814 follow-up: neither this nor outdoor_crossing above was bounded to start
    # no earlier than window_open_time — an overnight passive-decay floor-crossing (windows
    # still closed, HVAC off, well before the window ever opens) could be found and reported
    # as the nat_vent_cutoff, producing a displayed "Open 6:00 AM – 6:00 AM" (or worse, a
    # close time before the open time) whenever the floor was reached before/at open. The
    # _after_open() guard above/below restores the invariant that a reported cutoff can never
    # be earlier than when nat-vent could actually have started.
    floor_crossing = None
    if comfort_heat_raw is not None and sleep_heat is not None and in_sleep_window_fn is not None:
        floor_crossing = find_temperature_crossing(
            predicted_indoor,
            predicted_outdoor,
            lambda ts, _o, i: (
                _after_open(ts) and i <= resolve_comfort_heat(comfort_heat_raw, sleep_heat, in_sleep_window_fn(ts))
            ),
        )

    # Issue #821: unconditionally surfaced, unlike nat_vent_cutoff below (which only
    # carries whichever crossing wins the race against outdoor_crossing).
    result["comfort_floor_crossing_time"] = floor_crossing

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
        lead_min = compute_lead_minutes_from_rate(
            delta_t=comfort_cool - t_in_now,
            rate=k_active_cool,
            min_minutes=30.0,
            max_minutes=240.0,
            safety_multiplier=1.3,
            fallback_minutes=float(CEILING_PRECOOL_FALLBACK_MIN),
        )
        result["precool_start_time"] = result["ceiling_breach_time"] - timedelta(minutes=lead_min)

    # nat_vent_recovers / recovery_time: outdoor drops back below indoor AFTER the cutoff.
    # Issue #788: only meaningful for an "outdoor_rise" cutoff (windows closed BECAUSE
    # outdoor rose above indoor, so outdoor dropping back below indoor is a genuine,
    # later, actionable event). For "comfort_floor" cutoffs, this branch only wins the
    # race when outdoor_crossing did NOT fire first (see the `elif floor_crossing is not
    # None` above) — which means outdoor is already below indoor at cutoff time. Running
    # this scan there would find a "recovery" that was never really unmet, producing a
    # reopen recommendation minutes after telling the occupant to close windows. Leave
    # recovery_time=None / nat_vent_recovers=False (their initial-dict defaults) for
    # "comfort_floor" cutoffs.
    if result["nat_vent_cutoff"] is not None and result["nat_vent_cutoff_reason"] == "outdoor_rise":
        result["recovery_time"] = find_temperature_crossing(
            predicted_indoor, predicted_outdoor, lambda _ts, o, i: o < i, after=result["nat_vent_cutoff"]
        )
        result["nat_vent_recovers"] = result["recovery_time"] is not None

    _LOGGER.debug(
        "NatVentPlan: nat_vent_cutoff=%s (%s), ceiling_breach=%s, precool_start=%s, recovers=%s, recovery_time=%s",
        result["nat_vent_cutoff"],
        result["nat_vent_cutoff_reason"],
        result["ceiling_breach_time"],
        result["precool_start_time"],
        result["nat_vent_recovers"],
        result["recovery_time"],
    )

    return result
