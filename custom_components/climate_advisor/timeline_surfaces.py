"""The occupant-facing surfaces that READ the operation timeline (Issue #1230 R3b).

Occupant view: the card that says "Open windows at 10:00 AM", the card that says what the thermostat does next, the
briefing's list of the day's steps, the Analysis tab's tomorrow column and the REST endpoint used to be five separate
computations of "the plan". They could disagree (Issue #1276: the card told a house at its heat floor to open up while
the plan said nothing of the kind). They now all read ONE timeline (``operation_timeline.Timeline``), so what the
occupant is told in one place is what every other place says.

Pure module: functions of a ``Timeline`` plus the live readings the caller passes in. No engine, classifier, briefing,
coordinator or Home Assistant import (``tests/test_operation_timeline_structural.py`` enforces it). Guards that
are about
the LIVE moment, not the plan (the direction rule against the reading, "the windows are open right now", the occupancy
and fan-mode scope) are applied here on top of the plan step, exactly as the old card applied them; a surface never
advises something the timeline does not hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

from .operation_timeline import (
    Actor,
    MissingFact,
    StepKind,
    Timeline,
    TimelineStep,
    say,
)
from .temperature import format_temp

# Kinds that are the user's window actions (the only "you" steps today).
_WINDOW_KINDS = (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE)

# Next Automation: the automation-executed steps it may list, with the card label each one gets. Constants live HERE so
# tests/test_status_card_ontology.py can scan them for banned words ("waiting", "paused", "grace", "window") and
# clock times (Status Card Ontology: Next Automation never narrates mechanism state and never tells the occupant to
# do something CA cannot do). The time is the card's second value, never part of the label.
NEXT_AUTOMATION_LABELS: dict[str, str] = {
    "briefing_send": "Send daily briefing",
    "wake_band.heat": "Morning wake-up — restore heat comfort",
    "wake_band.cool": "Morning wake-up — restore cool comfort",
    "wake_band.check": "Morning wake-up check",
    "sleep_band.heat": "Bedtime — heat setback to {temp}",
    "sleep_band.cool": "Bedtime — cool setback to {temp}",
    "sleep_band.check": "Bedtime check",
    "precool_start.weather": "Pre-cool ceiling ({temp})",
    "precool_start.tou": "Pre-cool for TOU schedule ({temp})",
    "preheat_start.tou": "Pre-heat for TOU schedule ({temp})",
    "natvent_start.predicted": "Natural ventilation",
    "ceiling_guard": "AC turns on to hold the ceiling",
    # Issue #1404: the end of a time-of-use bank (its high-cost period starts). ``{after}`` is what follows, from the
    # shared hold resolver: "back to the Away band 60–80°F" / "the Away band widened to 60–83°F until 9:00 PM".
    "tou_bank_end": "High-cost period starts — {after}",
    # Issue #1404 (mirror): the away/vacation widening for the high-cost period, and its end.
    "tou_expansion_start": "High-cost period starts — setback widens to {temp}",
    "tou_expansion_end": "High-cost period ends — setback back to {temp}",
}

# Kinds a Next Automation candidate may come from that are listed even when they fall on another local day (the
# overnight pre-cool crosses midnight; a forecast-derived start is the next one whenever it is).
_ANYTIME_KINDS = frozenset(
    {StepKind.PRECOOL_START, StepKind.PREHEAT_START, StepKind.NAT_VENT_START, StepKind.CEILING_GUARD}
)


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------
# Window steps (Next Action card, briefing, Analysis)
# ---------------------------------------------------------------------------


def window_steps(tl: Timeline, day: Any | None = None) -> list[TimelineStep]:
    """The occupant's window steps, in order; ``day`` (a ``date``) limits them to one local day."""
    return [
        s for s in tl.steps if s.actor == Actor.YOU and s.kind in _WINDOW_KINDS and (day is None or s.ts.date() == day)
    ]


def window_periods(tl: Timeline, day: Any | None = None) -> list[tuple[TimelineStep | None, TimelineStep]]:
    """(open step or None, close step) pairs. An open step is None when the window was already open at generation."""
    periods: list[tuple[TimelineStep | None, TimelineStep]] = []
    pending: TimelineStep | None = None
    for s in window_steps(tl, day):
        if s.kind == StepKind.WINDOWS_OPEN:
            pending = s
        else:
            periods.append((pending, s))
            pending = None
    return periods


def card_user_step(tl: Timeline, now: datetime, *, windows_open: bool) -> TimelineStep | None:
    """The occupant step the Next Action card is about, today only.

    * inside a planned period (its open time reached, its close not): the open step while the windows are shut (the
      occupant should open them), the close step once they are open;
    * otherwise the next window step still ahead today.

    This is the #1276 invariant made concrete: the card never names a window action the timeline does not hold.
    """
    today = now.date()
    for opened, closed in window_periods(tl, today):
        start = opened.ts if opened is not None else None
        if closed.ts <= now:
            continue
        if start is not None and start > now:
            return opened
        # inside the period
        if opened is not None and not windows_open:
            return opened
        return closed
    return None


@dataclass(frozen=True)
class CardLive:
    """The live readings the Next Action card applies on top of the plan step (all internal degrees F)."""

    unit: str
    indoor: float | None
    outdoor: float | None
    windows_open: bool
    direction_ok: bool  # the one live direction rule (coordinator._outdoor_direction_ok) against the armed band
    heat_side_veto: bool  # outdoor cooler than indoor: air going the wrong way for a house the heat is holding
    armed_heat: float
    armed_cool: float


DIRECTION_VETO = "direction_veto"  # ``meta["hold_kind"]`` of a window step the live direction rule vetoes


def is_direction_vetoed(step: TimelineStep) -> bool:
    """Whether a window step is marked held by the live direction rule (status ``held``)."""
    return step.status == "held" and step.meta.get("hold_kind") == DIRECTION_VETO


def _live_open_ok(step: TimelineStep, live: CardLive) -> bool:
    """Whether the live direction rule lets the occupant open the windows for this (already-started) open step."""
    o, i = live.outdoor, live.indoor
    if o is None or i is None:
        return True  # unknown readings never veto
    if live.direction_ok:
        if step.reason == "windows_open.free_heating" and o > i:
            return True
        if o < i:
            return True
    return False


def apply_direction_veto(tl: Timeline, now: datetime, live: CardLive) -> Timeline:
    """Mark the window-open step the live direction rule vetoes right now (Issue #1276, every surface).

    A planned window period can be running while the live reading says opening would work against the house (outdoor
    cooler than a house the heat is holding at its floor, or outdoor not cooler than indoor). The Next Action card
    already said "keep windows closed"; the plan list and the briefing still carried the open step, so the occupant read
    "Open windows" beside "Keep windows closed". This returns a copy of the timeline whose current open step is
    ``held`` (``hold_kind`` ``direction_veto``) and worded with the same sentence as the card. Pure and idempotent; the
    latched plan is never written. Only the step the card is about is touched: a later period stays a plan.
    """
    step = card_user_step(tl, now, windows_open=live.windows_open)
    if (
        step is None
        or step.kind != StepKind.WINDOWS_OPEN
        or step.ts > now
        or is_direction_vetoed(step)
        or _live_open_ok(step, live)
    ):
        return tl
    o, i = live.outdoor, live.indoor
    assert o is not None
    assert i is not None
    try:
        if live.heat_side_veto:
            key = "held.direction_veto.heat"
            text = say(key, live.unit, outdoor=o, floor=live.armed_heat)
        else:
            key = "held.direction_veto.warm"
            text = say(key, live.unit, outdoor=o, indoor=i)
    except MissingFact:  # unreachable (readings are known above); a surface never fails on a wording
        return tl
    held = replace(
        step,
        text=text,
        meta={**step.meta, "status": "held", "hold": key, "hold_kind": DIRECTION_VETO},
    )
    return replace(tl, steps=tuple(held if s is step else s for s in tl.steps))


def window_card_text(tl: Timeline, now: datetime, live: CardLive) -> tuple[str, TimelineStep] | None:
    """The Next Action card's window text and the step it came from, or None when the plan has no window step today.

    The timing comes from the timeline step; the direction rule is applied to the live reading (a plan hour that the
    reading now contradicts becomes "keep closed", never an invitation). The veto is a property of the step
    (``apply_direction_veto``), so the card, the plan list and the briefing say the same sentence.
    """
    tl = apply_direction_veto(tl, now, live)
    step = card_user_step(tl, now, windows_open=live.windows_open)
    if step is None:
        return None
    unit = live.unit
    o, i = live.outdoor, live.indoor
    known = o is not None and i is not None
    if step.kind == StepKind.WINDOWS_OPEN:
        if step.ts > now:
            return f"Open windows at {_clock(step.ts)}", step
        if is_direction_vetoed(step):
            return step.text, step
        if known:
            assert o is not None
            assert i is not None
            if step.reason == "windows_open.free_heating" and o > i:
                return (
                    f"Open windows briefly — outdoor's warmer ({format_temp(o, unit)} vs {format_temp(i, unit)}).",
                    step,
                )
            return (
                f"Open windows — outdoor's cooler now ({format_temp(o, unit)} vs {format_temp(i, unit)}).",
                step,
            )
        return "Open windows — outdoor air may be cooler now.", step
    # a close step
    if live.windows_open and known and not live.direction_ok:
        assert o is not None
        assert i is not None
        if live.heat_side_veto:
            return (
                f"Close windows to hold heat — outdoor's cooler ({format_temp(o, unit)} vs {format_temp(i, unit)}).",
                step,
            )
        return f"Close windows — outdoor's not helping now ({format_temp(o, unit)} vs {format_temp(i, unit)}).", step
    return f"Close windows by {_clock(step.ts)}", step


def advises_opening(text: str) -> bool:
    """Whether a Next Action text tells the occupant to open windows (the invariant tests and the harness hook use
    it)."""
    return text.startswith("Open windows")


# ---------------------------------------------------------------------------
# Next Automation
# ---------------------------------------------------------------------------


def _label_key(step: TimelineStep) -> str | None:
    k = step.kind
    if k == StepKind.BRIEFING_SEND:
        return "briefing_send"
    if k == StepKind.WAKE_BAND:
        return (
            "wake_band.heat" if step.mode == "heat" else "wake_band.cool" if step.mode == "cool" else "wake_band.check"
        )
    if k == StepKind.SLEEP_BAND:
        return (
            "sleep_band.heat"
            if step.mode == "heat"
            else "sleep_band.cool"
            if step.mode == "cool"
            else "sleep_band.check"
        )
    if k == StepKind.PRECOOL_START:
        return "precool_start.tou" if step.reason == "pre_cool_start.tou" else "precool_start.weather"
    if k == StepKind.PREHEAT_START:
        return "preheat_start.tou"
    if k == StepKind.NAT_VENT_START:
        return (
            "natvent_start.predicted" if step.reason == "natvent_start.predicted" else None
        )  # the planned one follows the occupant's window
    if k == StepKind.CEILING_GUARD:
        return "ceiling_guard"
    if k == StepKind.SETPOINT_CHANGE and step.reason == "setpoint_change.tou_expansion_start":
        return "tou_expansion_start"
    if k == StepKind.SETPOINT_CHANGE and step.reason == "setpoint_change.tou_expansion_end":
        return "tou_expansion_end"
    return None


def next_automation_label(step: TimelineStep, unit: str) -> str | None:
    """The Next Automation card label for one CA step, or None when the step is not a card candidate."""
    key = _label_key(step)
    if key is None:
        return None
    template = NEXT_AUTOMATION_LABELS[key]
    if "{temp}" not in template:
        return template
    facts = step.facts
    if key == "sleep_band.heat":
        temp = facts.get("floor")
    elif key == "sleep_band.cool":
        temp = facts.get("ceiling")
    else:
        temp = step.target_f
    if temp is None:
        return None
    return template.format(temp=format_temp(float(temp), unit))


def tou_bank_end_label(after: str) -> str:
    """The Next Automation label for the end of a time-of-use bank (Issue #1404); ``after`` is the band text that
    follows it (``automation.hold_after_text()``), so the card says the same as Status and the plan list."""
    return NEXT_AUTOMATION_LABELS["tou_bank_end"].format(after=after)


def next_automation_candidates(tl: Timeline, now: datetime, unit: str) -> list[tuple[datetime, str, TimelineStep]]:
    """Every automation-executed step that is still ahead, with its card label (earliest first)."""
    out: list[tuple[datetime, str, TimelineStep]] = []
    for s in tl.steps:
        if s.actor != Actor.CA or s.ts <= now:
            continue
        if s.ts.date() != now.date() and s.kind not in _ANYTIME_KINDS:
            continue
        label = next_automation_label(s, unit)
        if label is not None:
            out.append((s.ts, label, s))
    out.sort(key=lambda e: e[0])
    return out


def next_automation(tl: Timeline, now: datetime, unit: str) -> tuple[str, str, TimelineStep | None]:
    """(action, time label, step) for the card; ("No more actions today", "", None) when nothing is left."""
    cands = next_automation_candidates(tl, now, unit)
    if not cands:
        return "No more actions today", "", None
    ts, label, step = cands[0]
    return label, _clock(ts), step


# ---------------------------------------------------------------------------
# Briefing operations section
# ---------------------------------------------------------------------------

_OPERATIONS_MAX_STEPS = 12


def operations_lines(tl: Timeline, now: datetime, *, until: datetime | None = None) -> list[str]:
    """The briefing's list of what happens next, straight from the timeline's step sentences (no recompute).

    Today's remaining steps up to ``until`` (default: the next local midnight), in time order. The daily briefing send
    itself is not listed (it is how the occupant is reading this).
    """
    end = (
        until if until is not None else datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=now.tzinfo)
    )
    steps = [
        s
        for s in tl.steps
        if s.ts >= now - timedelta(minutes=1) and s.ts < end and s.kind != StepKind.BRIEFING_SEND and s.status != "done"
    ]
    return [f"  • {s.text}" for s in steps[:_OPERATIONS_MAX_STEPS]]


# ---------------------------------------------------------------------------
# Window times for the briefing facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineWindows:
    """Today's window periods as the timeline holds them (the briefing's close/open facts read this)."""

    periods: tuple[tuple[datetime | None, datetime], ...]
    # The period the live direction rule vetoes right now (its open step is held): the briefing says "closed for now"
    # instead of "close by", and does not promise the open. None, "heat" or "warm" (which sentence the step carries).
    vetoed: str | None = None


def timeline_windows(tl: Timeline, day: Any) -> TimelineWindows:
    live: list[tuple[datetime | None, datetime]] = []
    vetoed: str | None = None
    for o, c in window_periods(tl, day):
        if o is not None and is_direction_vetoed(o):
            vetoed = "heat" if o.meta.get("hold") == "held.direction_veto.heat" else "warm"
            continue
        live.append((o.ts if o else None, c.ts))
    return TimelineWindows(tuple(live), vetoed)


# ---------------------------------------------------------------------------
# Chart activity strips right of NOW (Issue #1436)
# ---------------------------------------------------------------------------
#
# Occupant view: under the chart, each activity strip is solid where it shows what happened and faded where it shows
# what the plan expects. The faded part used to come from an older hour-by-hour predictor that knew nothing of the
# plan's close time, the comfort floor or the night strategy, so it drew "Nat-vent" and "Win plan" all night under a
# plan that said "7:14 PM: close the windows". Every faded strip is now a span of the plan itself: a window period
# (open step to close step), a nat-vent session (start step to stop step), a predicted heat/AC run, the setback the plan
# holds, an override CA resumes from. Nothing here plans; the spans are read off the timeline.

STRIP_ROWS = ("heating", "cooling", "fan", "natvent", "windows", "windowsplan", "away", "override")


def _window_spans(tl: Timeline, now: datetime) -> list[tuple[datetime, datetime, TimelineStep]]:
    """The plan's window periods as (start, end, the step the hover names), vetoed periods left out.

    A period already open when the plan was made has no open step: it runs from ``now``. A period the live direction
    rule vetoes (its open step is held, the card says "keep windows closed") is not a period the plan expects, exactly
    as the briefing's window times leave it out (``timeline_windows``). The hover names the step still ahead: the open
    step before the period starts, the close step once it has.
    """
    out: list[tuple[datetime, datetime, TimelineStep]] = []
    for o, c in window_periods(tl):
        if o is not None and is_direction_vetoed(o):
            continue
        start = o.ts if o is not None else min(now, c.ts)
        out.append((start, c.ts, o if o is not None and o.ts > now else c))
    return out


def planned_windows_open(tl: Timeline, at: datetime) -> bool:
    """Whether the plan expects the windows open at ``at`` (one of its window periods covers it).

    The chart log records this on every row (``windows_planned``) so the PAST "Win plan" strip shows what the plan said
    at the time, the same meaning as the faded part (Issue #1436).
    """
    return any(a <= at < b for a, b, _s in _window_spans(tl, at))


def _session_spans(
    steps: list[TimelineStep], start_kind: StepKind, stop_kind: StepKind, now: datetime
) -> list[tuple[datetime, datetime, TimelineStep | None, TimelineStep | None]]:
    """(start, end, start step, stop step) pairs of a start/stop step kind, in time order.

    A stop with no start before it is a session already running when the plan was made (``now`` on). A start with no
    stop is returned with ``end == start`` and no stop step (the caller decides what ends it).
    """
    out: list[tuple[datetime, datetime, TimelineStep | None, TimelineStep | None]] = []
    pending: TimelineStep | None = None
    for s in steps:
        if s.kind == start_kind:
            pending = pending or s
        elif s.kind == stop_kind:
            out.append((pending.ts if pending is not None else min(now, s.ts), s.ts, pending, s))
            pending = None
    if pending is not None:
        out.append((pending.ts, pending.ts, pending, None))
    return out


def _intersect(a: datetime, b: datetime, periods: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    return [(max(a, pa), min(b, pb)) for pa, pb in periods if min(b, pb) > max(a, pa)]


def _run_text(mode: str, minutes: float, start: datetime, end: datetime) -> str:
    verb = "Heating" if mode == "heat" else "Cooling"
    edge = "floor" if mode == "heat" else "ceiling"
    return (
        f"{verb} expected for about {max(5, int(round(minutes / 5.0)) * 5)} min between {_clock(start)} and "
        f"{_clock(end)}, holding the band's {edge}."
    )


def strip_spans(tl: Timeline, now: datetime) -> list[dict[str, Any]]:
    """The chart's activity strips from ``now`` to the end of the plan, read off the timeline (Issue #1436).

    One dict per span: ``row`` (a ``STRIP_ROWS`` id), ``start`` / ``end`` (ISO), ``kind`` (the step kind it came from,
    or ``run``), ``step_id``, ``step_ts`` and ``actor`` of that step, ``text`` (the plan step's own sentence),
    ``text_short`` (that sentence without its leading time, as the plan list shows it) and ``confidence``. A run has no
    step: its ``text`` is its own sentence and ``step_ts`` is None.
    Rules (docs/operation-timeline-spec.md, "Chart activity strips"):

    * ``windowsplan``: each window period, open step to close step (``window_periods``); a period open already runs
      from now; a period the live direction rule vetoes is left out.
    * ``natvent``: each nat-vent session, start step to stop step (a stop with no start: a session running now; a
      start with no stop ends at the next window close), clipped to the window periods: the session only runs while a
      window the plan opens is open.
    * ``fan``: the fan those sessions run, plus the night strategy's fan steps (fan on to fan off).
    * ``heating`` / ``cooling``: the plan's predicted heat and AC runs (``DetailedTimeline.runs``).
    * ``away``: the setback the plan holds now (``setback_start``): to the next midnight for Away (the plan holds the
      home band from tomorrow, ``operation_timeline._occupancy_for``), to the end of the plan for Vacation.
    * ``override``: an occupant change CA holds, from now to the ``resume_schedule`` step.
    * ``windows`` (windows physically open) has no plan part: the plan's expectation is ``windowsplan``.
    """
    horizon = tl.horizon_end
    steps = sorted(tl.steps, key=lambda s: s.ts)
    out: list[dict[str, Any]] = []

    def add(row: str, a: datetime, b: datetime, step: TimelineStep | None, *, text: str = "", kind: str = "") -> None:
        a2, b2 = max(a, now), min(b, horizon)
        if b2 <= a2:
            return
        out.append(
            {
                "row": row,
                "start": a2.isoformat(),
                "end": b2.isoformat(),
                "kind": step.kind.value if step is not None else kind,
                "step_id": step.id if step is not None else "",
                "step_ts": step.ts.isoformat() if step is not None else None,
                "actor": step.actor.value if step is not None else Actor.CA.value,
                "text": step.text if step is not None else text,
                # the hover shows the step's time beside it, so the sentence without its leading time (#1399)
                "text_short": plan_list_text(step.text, step.actor.value) if step is not None else text,
                "confidence": step.confidence if step is not None else "ok",
            }
        )

    windows = _window_spans(tl, now)
    periods = [(a, b) for a, b, _s in windows]
    for a, b, step in windows:
        add("windowsplan", a, b, step)

    vetoed_ids = {s.id for s in steps if is_direction_vetoed(s) and s.id}
    sessions: list[tuple[datetime, datetime, TimelineStep | None]] = []
    for a, b, start, stop in _session_spans(steps, StepKind.NAT_VENT_START, StepKind.NAT_VENT_STOP, now):
        if start is not None and start.after in vetoed_ids:
            continue  # the fan session that follows an open the live rule vetoes is not expected either
        if stop is None:
            close = next((c for c in steps if c.kind == StepKind.WINDOWS_CLOSE and c.ts > a), None)
            if close is None:
                continue  # a predicted start with nothing in the plan to end it: its marker on the chart names it
            b = close.ts
        hover = start if start is not None and start.ts > now else (stop or start)
        for ia, ib in _intersect(a, b, periods):
            sessions.append((ia, ib, hover))
            add("natvent", ia, ib, hover)
    for ia, ib, hover in sessions:
        add("fan", ia, ib, hover)
    for a, b, on, off in _session_spans(steps, StepKind.FAN_ON, StepKind.FAN_OFF, now):
        if off is None:
            continue
        add("fan", a, b, on if on is not None and on.ts > now else off)

    for r in getattr(tl, "runs", ()) or ():
        row = "heating" if r.mode == "heat" else "cooling" if r.mode == "cool" else None
        if row is not None:
            add(row, r.start, r.end, None, text=_run_text(r.mode, r.minutes, r.start, r.end), kind="run")

    for s in steps:
        if s.kind == StepKind.SETBACK_START:
            end = (
                horizon
                if s.reason.endswith(".vacation")
                else datetime.combine(s.ts.date() + timedelta(days=1), time(0, 0), tzinfo=s.ts.tzinfo)
            )
            add("away", s.ts, end, s)
        elif s.kind == StepKind.RESUME_SCHEDULE:
            add("override", now, s.ts, s)

    order = {r: i for i, r in enumerate(STRIP_ROWS)}
    out.sort(key=lambda d: (order.get(d["row"], 99), d["start"]))
    return out


def strip_covers(spans: list[dict[str, Any]], row: str, at: datetime) -> bool:
    """Whether a ``strip_spans`` row covers the instant ``at`` (start inclusive, end exclusive)."""
    for d in spans:
        if d.get("row") != row:
            continue
        try:
            if datetime.fromisoformat(d["start"]) <= at < datetime.fromisoformat(d["end"]):
                return True
        except KeyError, TypeError, ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# REST view
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plan list row text (Issue #1399)
# ---------------------------------------------------------------------------

# The time a table sentence opens with ("8:30 PM: ", "Sun 6:15 AM: "): ``operation_timeline._fmt_time`` plus ": ".
# Kept loose on the weekday and AM/PM words (strftime is locale-dependent); a sentence that does not open with a time
# (a direction-veto rewrite, a summary) does not match and is left whole.
_TIME_LEAD = re.compile(r"^(?:[^\W\d]{2,4}\.? )?\d{1,2}:\d{2}(?: ?[^\W\d]{1,4}\.?)?: ")

# After the time is gone: leading phrases rewritten so the row reads as its own sentence. (old, new, actor or None for
# any actor); the first match wins, so the specific phrases come before the bare "CA ". "CA" is dropped only on a CA
# step, where the row's actor badge already says CA; an occupant step keeps its "you" wording.
_LEAD_REWRITES: tuple[tuple[str, str, str | None], ...] = (
    ("bedtime, CA ", "bedtime: ", Actor.CA.value),
    ("bedtime, ", "bedtime: ", None),
    ("nobody is home, CA ", "nobody is home: ", Actor.CA.value),
    ("vacation mode, CA ", "vacation mode: ", Actor.CA.value),
    ("high-cost period, CA ", "high-cost period: ", Actor.CA.value),
    ("the high-cost period ends, CA ", "the high-cost period ends: ", Actor.CA.value),
    ("once a window is open, CA ", "once a window is open: ", Actor.CA.value),
    ("your change ends and CA ", "your change ends: ", Actor.CA.value),
    ("CA ", "", Actor.CA.value),
)


def plan_list_text(text: str, actor: str) -> str:
    """A step's sentence as the "Next 24 hours" row shows it, beside its own time and actor badge.

    The row's left columns already show the time (with the weekday on another day) and who acts, so the sentence's own
    leading time is dropped, a leading "CA" is dropped on a CA step, and the first letter is capitalised:
    "8:30 PM: bedtime, CA switches to the sleep band, ..." -> "Bedtime: switches to the sleep band, ...".
    Derived from the final sentence (after any hold or veto rewrite), so it never says anything the full sentence does
    not; the full ``text`` stays the canonical wording for every surface that shows a step on its own.
    """
    m = _TIME_LEAD.match(text)
    if m is None:
        return text
    rest = text[m.end() :]
    for old, new, who in _LEAD_REWRITES:
        if (who is None or who == actor) and rest.startswith(old):
            rest = new + rest[len(old) :]
            break
    if not rest:
        return text
    return rest[0].upper() + rest[1:]


def timeline_api_payload(
    contract: dict[str, Any], detail: dict[str, Any] | None, unit: str, convert: Any
) -> dict[str, Any]:
    """The REST body: the contract dict with temperatures in the display unit, ``unit`` added, and each step's
    ``text_short`` (its plan-list row wording, ``plan_list_text``; a view field, not part of the stored contract).

    ``convert`` is the caller's ``display_temp(value_f, unit)`` (kept out of this module so it stays unit-library
    free). Sentences were already written in the user's unit by the builder, so only the numeric fields convert.
    """
    out = dict(contract)
    out["unit"] = unit
    steps = []
    for s in contract.get("steps", []):
        s2 = dict(s)
        s2["text_short"] = plan_list_text(str(s2.get("text", "")), str(s2.get("actor", "")))
        if s2.get("target_f") is not None:
            s2["target_f"] = convert(s2["target_f"], unit)
        meta = dict(s2.get("meta") or {})
        facts = dict(meta.get("facts") or {})
        for key in ("floor", "ceiling", "setpoint", "target", "edge"):
            if isinstance(facts.get(key), (int, float)):
                facts[key] = convert(facts[key], unit)
        if "delta" in facts and isinstance(facts["delta"], (int, float)):
            facts["delta"] = round(float(facts["delta"]) * (5.0 / 9.0 if unit == "celsius" else 1.0), 1)
        meta["facts"] = facts
        s2["meta"] = meta
        steps.append(s2)
    out["steps"] = steps
    out["path"] = [{**p, "temp_f": convert(p["temp_f"], unit)} for p in contract.get("path", [])]
    if detail is not None:
        d = dict(detail)
        d["band"] = [
            {**b, "floor": convert(b["floor"], unit), "ceiling": convert(b["ceiling"], unit)} for b in d.get("band", [])
        ]
        d["days"] = [
            {
                **day,
                **{k: convert(day[k], unit) for k in ("expected_high", "expected_low") if day.get(k) is not None},
            }
            for day in d.get("days", [])
        ]
        out["detail"] = d
    return out
