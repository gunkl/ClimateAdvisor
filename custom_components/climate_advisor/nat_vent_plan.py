"""Single source of truth for hot/warm/mild-day natural-ventilation timing
(Issue #817, extended to Hot's displayed briefing/status text by Issue #876).

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


def _nat_vent_reopen_reached(outdoor_temp: float, indoor_temp: float) -> bool:
    """Symmetric opposite of ``_nat_vent_cutoff_reached()`` (Issue #876): outdoor has
    cooled back down far enough below indoor that reopening windows helps again.
    Reuses the same ``_NAT_VENT_CUTOFF_MARGIN_F`` so the morning-close and
    evening-open crossings are symmetric by construction, rather than the evening
    side being defined against an unrelated absolute threshold."""
    return outdoor_temp <= indoor_temp - _NAT_VENT_CUTOFF_MARGIN_F


def resolve_with_fallback(dynamic: datetime | None, static: time | None) -> time | None:
    """Single source of truth for "use the ODE-dynamic time if available, else fall
    back to the static configured hour" (Issue #876).

    Before this existed, this exact ``x.time() if x is not None else y`` check was
    hand-rolled independently in ``briefing.py`` (twice), ``coordinator.py``'s Next
    Automation card, and ``automation.py``'s planned-window-period gate — the same
    class of duplication this module's own docstring already identifies as the root
    cause of Issue #528 (a duplicate silently reintroduced after #518 supposedly
    fixed it). Every caller that needs "dynamic value, else static fallback" should
    call this rather than writing its own inline conditional.
    """
    return dynamic.time() if dynamic is not None else static


def resolve_window_pair(
    dynamic_close: datetime | None,
    static_close: time | None,
    dynamic_open: datetime | None,
    static_open: time | None,
) -> tuple[time | None, time | None]:
    """Single source of truth for resolving a close/open pair *together* (Issue #878).

    Before this existed, every caller resolved its close time and its open time as
    two independent ``resolve_with_fallback()`` calls, with nothing checking the pair
    against each other. That let Hot's briefing render "Close by 6:00 PM / Open
    5:00 PM+" the day after Issue #876 shipped: the ODE outdoor-crossing scan
    genuinely resolved ``nat_vent_cutoff`` to 6:00 PM (no upper bound — a legitimate
    forecast outcome), while ``evening_open_time`` had no crossing past that hour and
    silently fell back to the static ``ECONOMIZER_EVENING_START_HOUR`` (5:00 PM) —
    an open time before the close time it's paired with. Warm/Mild had a *partial*
    guard (close vs. the static open hour, in ``briefing.py``) that was never
    generalized to check a resolved pair against each other, and was never extended
    to Hot at all.

    This function is the one place that invariant is enforced, for every day type
    and every current/future caller: if the resolved open time is not strictly after
    the resolved close time, the open time is dropped (not the close time — the close
    time is the more load-bearing half of the pair) and a WARNING is logged, per
    CLAUDE.md's Observability Requirements ("WARNING when a target value is clamped
    or overridden by a guard").

    Issue #878-followup: when BOTH ``dynamic_close`` and ``dynamic_open`` are real
    (dated) datetimes, the ordering check compares them directly rather than via
    their date-stripped ``.time()`` values. A legitimate overnight reopen (close
    7 PM today, reopen 2 AM tomorrow) is 7 hours *after* close on the real
    timeline, but ``time(2, 0) <= time(19, 0)`` reads as "before" on a bare clock
    face — the deployed v0.7.31 build compared exactly that and silently dropped
    every dynamic-pair reopen that fell after midnight, which is the ordinary
    shape of a hot day's evening reopen, not a rare edge case. When either side is
    a static (dateless) fallback hour, "same evening" is the only meaning
    available, so the original same-day clock-time comparison is kept — that
    comparison is what correctly caught the *original* #878 incident (dynamic
    close 6 PM vs. static-fallback open 5 PM, same evening) and must not change.

    Returns:
        (close_time, open_time_or_None)
    """
    close = resolve_with_fallback(dynamic_close, static_close)
    open_ = resolve_with_fallback(dynamic_open, static_open)
    if open_ is not None and close is not None:
        if dynamic_close is not None and dynamic_open is not None:
            ordered = dynamic_open > dynamic_close
        else:
            ordered = open_ > close
        if not ordered:
            _LOGGER.warning(
                "resolve_window_pair: dropping open time %s (%s) — not strictly after"
                " resolved close time %s (%s); a close/open pair must never render with"
                " open at or before close",
                open_,
                "dynamic" if dynamic_open is not None else "static fallback",
                close,
                "dynamic" if dynamic_close is not None else "static fallback",
            )
            open_ = None
    return close, open_


def describe_close_timing(close_time_str: str, already_reached: bool) -> str:
    """Shared fragment for the close-time half of a window-timing sentence
    (Issue #878-followup) — the same treatment ``describe_nat_vent_cutoff_reason()``
    already gives the *reason* half, applied here to the *time* half.

    ``already_reached`` mirrors ``compute_nat_vent_plan()``'s
    ``nat_vent_cutoff_already_reached``: the ODE crossing scan is forward-only from
    "now", so when the close condition is already true at the moment the curve is
    built, the scan can only return the first available future timestamp — not a
    genuine future prediction, just "now" rounded to the next grid point. Rendering
    that as "Close by 7:00 PM" hours after the real (morning) crossing already
    passed asserts a false future-scheduled event. Every caller (Hot/Warm/Mild TLDR
    rows and conversational sentences, ``coordinator.py``'s Next User Action card)
    must call this rather than re-deriving the same branch six times over.

    Args:
        close_time_str: the already-formatted clock time (e.g. "7:00 PM") to use
            when the close is a genuine future prediction.
        already_reached: whether the close condition was already true when the
            curve started.

    Returns:
        A short phrase fragment: either the passed-through formatted time, or an
        "already true" phrase naming the present moment instead of a false future
        clock time.
    """
    if already_reached:
        return "now"
    return close_time_str


def describe_nat_vent_cutoff_reason(reason: str | None) -> str:
    """Single source of truth for how ``nat_vent_cutoff_reason`` reads as text (Issue #847).

    Before this existed, ``briefing.py``'s ``_warm_day_plan()`` and ``coordinator.py``'s
    ``_compute_next_automation_action()`` each independently decided how to phrase the
    same ``nat_vent_cutoff_reason`` value from the shared ``nat_vent_plan`` dict —
    ``_warm_day_plan()`` said "hold the heat in" for ``comfort_floor`` (reworded in
    Issue #869 — see below), while the Next
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
          "comfort_floor"      -> "since indoor's already down to your comfort floor"
          "outdoor_rise"/None  -> "before outdoor air warms past indoor"

        Issue #869: "comfort_floor" previously read "to hold the heat in", which reads
        backwards on the WARM/MILD day types where this function is actually used —
        both day types' overall framing is banking coolness for later, and "hold the
        heat in" sounds like a winter/heating framing that contradicts that narrative.
        A "comfort_floor" cutoff is a distinct, legitimate reason (indoor has already
        fallen to the overnight comfort floor, protecting against an overnight low —
        not part of the day's cool-banking strategy), so the new phrasing names that
        directly instead of borrowing heating-season language. Still a single shared
        fragment — no new parameter, no second inline branch in a consumer.
    """
    if reason == "comfort_floor":
        return "since indoor's already down to your comfort floor"
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
      evening_open_time: datetime | None — renamed from ``recovery_time`` (Issue #876;
          ``nat_vent_recovers`` is retired in favor of a plain
          ``evening_open_time is not None`` check at call sites) but the underlying
          Issue #788 semantics are UNCHANGED, not generalized: this field is still
          only computed for an "outdoor_rise" cutoff — first timestamp after
          ``nat_vent_cutoff`` where outdoor has cooled back below indoor again (the
          symmetric opposite of the morning-close crossing, via
          ``_nat_vent_reopen_reached()``). For a "comfort_floor" cutoff this stays
          ``None`` by design: that cutoff fires because indoor fell too low, and
          outdoor cooling further from there is the same bad direction that caused
          the close, not a signal that reopening helps — using the same predicate
          there would reproduce the exact "Close at 8am, reopen at 9am" contradiction
          Issue #788 was filed to fix (verified against that issue's reported
          scenario during Issue #876's implementation: the generalized version
          fabricates an evening_open_time one hour after a comfort_floor cutoff in
          that exact case). A future comfort_floor-specific "safe to reopen" event
          would need its own predicate (outdoor warming back toward indoor, the
          opposite direction) — out of scope here.
      nat_vent_cutoff_already_reached: bool — Issue #878-followup: True when
          ``nat_vent_cutoff`` equals the *first* timestamp the scan could possibly
          have examined (the first entry in ``predicted_indoor`` with a matching
          ``predicted_outdoor`` entry, at-or-after ``window_open_time``). Both
          ``predicted_indoor``/``predicted_outdoor`` are forward-only from "now"
          (built fresh each cycle), so this means the close condition was already
          true at the moment the curve was built — not a genuine future
          prediction, just the nearest available future grid point. Callers must
          not render this as a scheduled future clock time (see
          ``describe_close_timing()``); doing so produced a live "Close by 7:00 PM"
          hours after the real morning crossing had already passed.
    """
    result: dict = {
        "nat_vent_cutoff": None,
        "nat_vent_cutoff_reason": None,
        "comfort_floor_crossing_time": None,
        "ceiling_breach_time": None,
        "precool_start_time": None,
        "any_nat_vent_window": False,
        "evening_open_time": None,
        "nat_vent_cutoff_already_reached": False,
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

    # Issue #878-followup: was the winning cutoff the very first timestamp the scan
    # could possibly have examined? If so, the close condition was already true when
    # the (forward-only) curve was built — the scan found "now", not a real future
    # prediction. Reuses find_temperature_crossing() with an always-true comparator
    # to get "the first candidate timestamp," rather than re-deriving the _after_open()
    # + curve-alignment logic a second time (DRY).
    if result["nat_vent_cutoff"] is not None:
        first_candidate_ts = find_temperature_crossing(
            predicted_indoor, predicted_outdoor, lambda ts, _o, _i: _after_open(ts)
        )
        result["nat_vent_cutoff_already_reached"] = result["nat_vent_cutoff"] == first_candidate_ts

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

    # evening_open_time (Issue #876 rename of recovery_time/nat_vent_recovers): first
    # timestamp after cutoff where outdoor has cooled back down far enough below indoor
    # to make reopening worthwhile again — the symmetric opposite of the morning-close
    # crossing above. Issue #788's outdoor_rise-only restriction is preserved unchanged
    # (see the field's docstring above for why generalizing this to comfort_floor
    # cutoffs would reproduce the exact bug #788 fixed).
    if result["nat_vent_cutoff"] is not None and result["nat_vent_cutoff_reason"] == "outdoor_rise":
        result["evening_open_time"] = find_temperature_crossing(
            predicted_indoor,
            predicted_outdoor,
            lambda _ts, o, i: _nat_vent_reopen_reached(o, i),
            after=result["nat_vent_cutoff"],
        )

    _LOGGER.debug(
        "NatVentPlan: nat_vent_cutoff=%s (%s, already_reached=%s), ceiling_breach=%s, precool_start=%s,"
        " evening_open_time=%s",
        result["nat_vent_cutoff"],
        result["nat_vent_cutoff_reason"],
        result["nat_vent_cutoff_already_reached"],
        result["ceiling_breach_time"],
        result["precool_start_time"],
        result["evening_open_time"],
    )

    return result
