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

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .operation_timeline import (
    Actor,
    StepKind,
    Timeline,
    TimelineStep,
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


def window_card_text(tl: Timeline, now: datetime, live: CardLive) -> tuple[str, TimelineStep] | None:
    """The Next Action card's window text and the step it came from, or None when the plan has no window step today.

    The timing comes from the timeline step; the direction rule is applied to the live reading (a plan hour that the
    reading now contradicts becomes "keep closed", never an invitation).
    """
    step = card_user_step(tl, now, windows_open=live.windows_open)
    if step is None:
        return None
    unit = live.unit
    o, i = live.outdoor, live.indoor
    known = o is not None and i is not None
    if step.kind == StepKind.WINDOWS_OPEN:
        if step.ts > now:
            return f"Open windows at {_clock(step.ts)}", step
        if known:
            assert o is not None
            assert i is not None
            if live.direction_ok:
                if step.reason == "windows_open.free_heating" and o > i:
                    return (
                        f"Open windows briefly — outdoor's warmer ({format_temp(o, unit)} vs {format_temp(i, unit)}).",
                        step,
                    )
                if o < i:
                    return (
                        f"Open windows — outdoor's cooler now ({format_temp(o, unit)} vs {format_temp(i, unit)}).",
                        step,
                    )
            if live.heat_side_veto:
                return (
                    f"Keep windows closed to hold heat — outdoor ({format_temp(o, unit)}) would cool the house below"
                    f" its {format_temp(live.armed_heat, unit)} floor.",
                    step,
                )
            return (
                f"Keep windows closed for now — outdoor ({format_temp(o, unit)}) isn't cooler than indoor"
                f" ({format_temp(i, unit)}) yet.",
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


def timeline_windows(tl: Timeline, day: Any) -> TimelineWindows:
    return TimelineWindows(tuple((o.ts if o else None, c.ts) for o, c in window_periods(tl, day)))


# ---------------------------------------------------------------------------
# REST view
# ---------------------------------------------------------------------------


def timeline_api_payload(
    contract: dict[str, Any], detail: dict[str, Any] | None, unit: str, convert: Any
) -> dict[str, Any]:
    """The REST body: the contract dict with temperatures in the display unit and ``unit`` added.

    ``convert`` is the caller's ``display_temp(value_f, unit)`` (kept out of this module so it stays unit-library
    free). Sentences were already written in the user's unit by the builder, so only the numeric fields convert.
    """
    out = dict(contract)
    out["unit"] = unit
    steps = []
    for s in contract.get("steps", []):
        s2 = dict(s)
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
