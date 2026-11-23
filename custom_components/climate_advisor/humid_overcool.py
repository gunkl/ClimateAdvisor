"""Humid overcool (Issue #1694, plan #1507 H3, owner decisions D3/D5): the pure decision, no Home Assistant imports.

Occupant view: on a muggy day the AC's short runs cool the room but barely dry it, so the house stays sticky. With
the option on (Off by default), while a cooling run is active and the house's dew point is latched at or above
60 °F, Climate Advisor sets the thermostat 1 °F below the comfort target, 2 °F after 30 minutes if the house is still
at or above 60 °F, for at most 60 minutes per run. The AC runs about 25 minutes longer, the room ends about 1 °F cooler
and about 1 °F dew point drier, still inside the comfort band. A dry day is unchanged. Never in the sleep window (D5).

What this module decides (the engine owns the state, the gate and the writes):

* **Engage** (``idle``): a cooling run is reported (``hvac_action`` cooling only), the overcool latch is on (indoor
  dew point >= 60 °F, released at or below 58 °F: ``humidity_facts.OVERCOOL_HUMID_LATCH``), the gate is open and the
  gap rule leaves room for a step -> 1 °F, ``overcool_started``. Observe-only (automation off) only reports "would
  overcool", once per run, and changes nothing.
* **Step** (``active``): 30 minutes after engaging, still cooling and still at or above the trigger -> 2 °F.
* **Release mid-run** (``active`` -> ``done``): the latch let go (dried, or the dew point became unknown) or the
  60-minute cap -> 0 at once (a write), no re-engage until the run has ended.
* **Run ended** (``active`` -> ``coasting``): not cooling for ``OVERCOOL_RUN_END_GRACE_S`` (the Ecobee goes
  cooling -> fan 91 % of the time and restarts within 5 minutes 8 % of the time, so a blip keeps the overcool). The
  lowered setpoint is HELD through the post-run coast (``CYCLE_POST_OFF_WINDOW_S``, 45 min, the #1512 coast window),
  then restored (a write); the next cooling start restores earlier (and may engage the new run).
* **Gate closed** (an owner took the thermostat: the occupant, a pause, the fan, away, TOU bank, pre-cool, the sleep
  window, a grace, another mode): 0 at once, no write of its own (the owner writes its band; the occupant's value is
  never restored over), no re-engage this run. A window open past its debounce with no pause has no owner, so that
  one close asks for one restore write. ``FREEZE`` (a door/window inside its debounce) holds everything as it is.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .const import (
    CYCLE_POST_OFF_WINDOW_S,
    OVERCOOL_MAX_F,
    OVERCOOL_RUN_CAP_S,
    OVERCOOL_RUN_END_GRACE_S,
    OVERCOOL_STEP_AFTER_S,
    OVERCOOL_STEP_F,
)
from .remote_offset import combine_adjustments
from .time_utils import seconds_since

PHASE_IDLE = "idle"  # no overcool; a humid cooling run may engage
PHASE_ACTIVE = "active"  # the commanded cool setpoint is lowered during a run
PHASE_COASTING = "coasting"  # the run ended; the lowered setpoint is held until the coast window closes
PHASE_DONE = "done"  # released for the rest of this run (dried, cap, gate): no re-engage until the run ends

ACTION_NONE = "none"
ACTION_ENGAGE = "engage"
ACTION_STEP = "step"
ACTION_RELEASE = "release"  # mid-run: the plain target is written now
ACTION_RUN_ENDED = "run_ended"  # held through the coast
ACTION_RESTORE = "restore"  # the coast closed or the next run started: the plain target is written now
ACTION_ZERO = "zero"  # a gate closed: the adjustment is dropped, the owner writes
ACTION_WOULD_ENGAGE = "would_engage"  # observe-only: logged, nothing changes

REASON_DRIED = "dried"
REASON_CAP = "cap"
REASON_RUN_ENDED = "run_ended"
REASON_COAST_OVER = "coast_over"
REASON_NEXT_RUN = "next_run"

EVENT_STARTED = "overcool_started"
EVENT_ENDED = "overcool_ended"

_EPS = 1e-6


@dataclass(frozen=True)
class OvercoolState:
    """The overcool now (engine-owned, in memory only: a restart writes the normal setpoint)."""

    phase: str = PHASE_IDLE
    magnitude_f: float = 0.0  # requested lowering (°F, positive); the write path clamps it
    engaged_at: datetime | None = None
    not_cooling_since: datetime | None = None
    stopped_at: datetime | None = None  # the run's end (first non-cooling report) while coasting
    observe_reported: bool = False  # observe-only: "would overcool" already reported for this run


@dataclass(frozen=True)
class OvercoolInputs:
    """What the decision reads (resolved by the engine from its own predicates)."""

    now: datetime
    enabled: bool  # the option
    observe_only: bool  # automation off (dry run)
    gate: str  # "open" | "frozen" | "closed"
    gate_reason: str | None
    cooling: bool  # the thermostat reports hvac_action cooling now
    humid: bool  # the overcool latch is on (indoor dew point >= 60 °F, held above 58 °F)
    at_trigger: bool  # the decision dew point is at or above the trigger now (for the second step)
    headroom_f: float  # how far the gap rule lets overcool lower the commanded setpoint for the current target


@dataclass(frozen=True)
class OvercoolDecision:
    state: OvercoolState
    action: str = ACTION_NONE
    reason: str | None = None
    write: bool = False  # the engine re-sends the comfort target now (the write path applies the new term)
    event: str | None = None


def _since(at: datetime | None, now: datetime) -> float | None:
    if at is None:
        return None
    try:
        return seconds_since(at, now=now)
    except TypeError:  # naive vs aware: not the same clock
        return None


def _run_ended(state: OvercoolState, now: datetime) -> bool:
    elapsed = _since(state.not_cooling_since, now)
    return elapsed is not None and elapsed >= OVERCOOL_RUN_END_GRACE_S


def _engage(state: OvercoolState, inp: OvercoolInputs) -> OvercoolDecision:
    """A cooling run with the latch on and room under the gap rule: the first step (or "would", observe-only)."""
    if not (inp.cooling and inp.humid and inp.gate == "open") or inp.headroom_f < OVERCOOL_STEP_F - _EPS:
        return OvercoolDecision(state)
    if inp.observe_only:
        if state.observe_reported:
            return OvercoolDecision(state)
        return OvercoolDecision(replace(state, observe_reported=True), ACTION_WOULD_ENGAGE)
    engaged = OvercoolState(PHASE_ACTIVE, OVERCOOL_STEP_F, inp.now, None, None, state.observe_reported)
    return OvercoolDecision(engaged, ACTION_ENGAGE, write=True, event=EVENT_STARTED)


def decide_overcool(state: OvercoolState, inp: OvercoolInputs) -> OvercoolDecision:
    """The next overcool state and what to do now. Pure; never raises on odd input (it holds)."""
    if not inp.enabled:
        return OvercoolDecision(OvercoolState())
    tracked = replace(
        state,
        not_cooling_since=None if inp.cooling else (state.not_cooling_since or inp.now),
    )
    ended = _run_ended(tracked, inp.now)
    lowering = tracked.phase in (PHASE_ACTIVE, PHASE_COASTING)
    if inp.gate == "closed":
        if lowering:
            done = OvercoolState(
                PHASE_IDLE if (ended or tracked.phase == PHASE_COASTING) else PHASE_DONE,
                0.0,
                None,
                tracked.not_cooling_since,
                None,
                tracked.observe_reported,
            )
            return OvercoolDecision(
                done,
                ACTION_ZERO,
                f"gate:{inp.gate_reason or 'other'}",
                write=inp.gate_reason == "window_open",  # no owner writes: one plain-target write
                event=EVENT_ENDED if tracked.phase == PHASE_ACTIVE else None,
            )
        # Idle or done: nothing is lowered; an owner holds the thermostat. Run-end tracking continues below.
        if tracked.phase == PHASE_DONE and ended:
            return OvercoolDecision(replace(tracked, phase=PHASE_IDLE, observe_reported=False))
        return OvercoolDecision(tracked)
    if inp.gate == "frozen":
        return OvercoolDecision(tracked)  # a door/window inside its debounce: hold everything as it is
    if tracked.phase == PHASE_DONE:
        if ended:
            return OvercoolDecision(replace(tracked, phase=PHASE_IDLE, observe_reported=False))
        return OvercoolDecision(tracked)
    if tracked.phase == PHASE_IDLE:
        if ended:
            tracked = replace(tracked, observe_reported=False)
        return _engage(tracked, inp)
    if tracked.phase == PHASE_COASTING:
        if inp.cooling:
            restored = OvercoolState()
            nxt = _engage(restored, inp)
            if nxt.action == ACTION_ENGAGE:
                return OvercoolDecision(nxt.state, ACTION_ENGAGE, REASON_NEXT_RUN, write=True, event=EVENT_STARTED)
            return OvercoolDecision(nxt.state, ACTION_RESTORE, REASON_NEXT_RUN, write=True)
        coast = _since(tracked.stopped_at, inp.now)
        if coast is not None and coast >= CYCLE_POST_OFF_WINDOW_S:
            return OvercoolDecision(
                OvercoolState(not_cooling_since=tracked.not_cooling_since),
                ACTION_RESTORE,
                REASON_COAST_OVER,
                write=True,
            )
        return OvercoolDecision(tracked)
    # PHASE_ACTIVE
    if ended:
        coasting = replace(tracked, phase=PHASE_COASTING, stopped_at=tracked.not_cooling_since)
        return OvercoolDecision(coasting, ACTION_RUN_ENDED, REASON_RUN_ENDED, event=EVENT_ENDED)
    engaged_s = _since(tracked.engaged_at, inp.now) or 0.0
    if not inp.humid or engaged_s >= OVERCOOL_RUN_CAP_S:
        done = replace(tracked, phase=PHASE_DONE, magnitude_f=0.0, engaged_at=None)
        reason = REASON_DRIED if not inp.humid else REASON_CAP
        return OvercoolDecision(done, ACTION_RELEASE, reason, write=True, event=EVENT_ENDED)
    if (
        tracked.magnitude_f < OVERCOOL_MAX_F - _EPS
        and engaged_s >= OVERCOOL_STEP_AFTER_S
        and inp.cooling
        and inp.at_trigger
        and inp.headroom_f > tracked.magnitude_f + _EPS
    ):
        stepped = replace(tracked, magnitude_f=min(tracked.magnitude_f + OVERCOOL_STEP_F, OVERCOOL_MAX_F))
        return OvercoolDecision(stepped, ACTION_STEP, write=True)
    return OvercoolDecision(tracked)


def next_deadline_s(state: OvercoolState, now: datetime) -> float | None:
    """Seconds until the next moment the decision can change without a new report (step, cap, run end, coast end).

    None when nothing is due (idle with a cooling run still to start, or done while cooling continues). The engine
    re-arms one timer at this, so the step, the cap and the restore land on time between the 5-minute ticks.
    """
    due: list[float] = []

    def _add(at: datetime | None, after_s: float) -> None:
        elapsed = _since(at, now)
        if elapsed is not None:
            due.append(max(1.0, after_s - elapsed))

    if state.phase == PHASE_ACTIVE:
        if state.magnitude_f < OVERCOOL_MAX_F - _EPS:
            _add(state.engaged_at, OVERCOOL_STEP_AFTER_S)
        _add(state.engaged_at, OVERCOOL_RUN_CAP_S)
        _add(state.not_cooling_since, OVERCOOL_RUN_END_GRACE_S)
    elif state.phase == PHASE_COASTING:
        _add(state.stopped_at, CYCLE_POST_OFF_WINDOW_S)
    elif state.phase == PHASE_DONE:
        _add(state.not_cooling_since, OVERCOOL_RUN_END_GRACE_S)
    return min(due) if due else None


def overcool_headroom_f(target_f: float, floor_f: float, gap_f: float, already_f: float = 0.0) -> float:
    """How far overcool may lower a cool setpoint whose comfort target is ``target_f``: never closer than ``gap_f``
    (``setpoint_combinations.MIN_HEAT_COOL_GAP_F``) to the floor of the band being written, after ``already_f`` (the
    remote-sensor offset already lowering it). 0 when there is no room (a band narrower than the gap never overcools).
    The same clamp the write path applies (``remote_offset.combine_adjustments``), asked for an unlimited lowering.
    """
    combined = combine_adjustments(
        "cool",
        target_f,
        (("remote", -abs(already_f)), ("overcool", -1.0e6)),
        floor=floor_f,
        ceiling=float("inf"),
        gap_f=gap_f,
    )
    return abs(combined[1][1])
