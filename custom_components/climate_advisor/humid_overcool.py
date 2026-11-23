"""Humid overcool (Issue #1694, plan #1507 H3, owner decisions D3/D5): the pure decision, no Home Assistant imports.

Occupant view: on a muggy day the AC's short runs cool the room but barely dry it, so the house stays sticky. With
the option on (Off by default), while a cooling run is active and the house's dew point is latched at or above
60 °F, Climate Advisor sets the thermostat 1 °F below the comfort target, 2 °F after 30 minutes if the house is still
at or above 60 °F, for at most 60 lowered minutes per humid spell (back-to-back runs share them).
The AC runs about 25 minutes longer, the room ends about 1 °F cooler
and about 1 °F dew point drier, still inside the comfort band. A dry day is unchanged. Never in the sleep window (D5).

What this module decides (the engine owns the state, the gate and the writes):

* **Engage** (``idle``): a cooling run is reported (``hvac_action`` cooling only), the overcool latch is on (indoor
  dew point >= 60 °F, released at or below 58 °F: ``humidity_facts.OVERCOOL_HUMID_LATCH``), the gate is open and the
  gap rule leaves room for a step -> 1 °F, ``overcool_started``. Observe-only (automation off) only reports "would
  overcool", once per run, and changes nothing.
* **Step** (``active``): 30 minutes after engaging, still cooling and still at or above the trigger -> 2 °F.
* **Release mid-run** (``active`` -> ``done``): the latch let go (dried, or the dew point became unknown) or the humid
  spell's 60 lowered minutes are used (active and coasting time, across back-to-back runs; renewed only when the latch
  releases and re-arms; owner decision held, review W3A-A A2) -> 0 at once (a write), no re-engage this spell.
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
    # The per-humid-spell budget (owner decision held, review W3A-A A2): lowered time (active and coasting) counted
    # against OVERCOOL_RUN_CAP_S across back-to-back runs, reset when the overcool latch releases.
    spell_used_s: float = 0.0  # lowered seconds of this humid spell already over (closed stretches)
    lowered_since: datetime | None = None  # start of the lowered stretch in force (kept across a coast re-engage)


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
    # The decision dew point is known (review FINAL-A A-4): an unknown one releases the latch (``humid`` False) but is
    # not "the house dried", so the spell budget is renewed only on a known dew point at or below the release.
    dew_point_known: bool = True


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


def budget_used_s(state: OvercoolState, now: datetime) -> float:
    """Lowered seconds of this humid spell so far: the closed stretches plus the stretch in force."""
    return state.spell_used_s + (_since(state.lowered_since, now) or 0.0)


def _close_stretch(state: OvercoolState, now: datetime) -> OvercoolState:
    """The lowered stretch ends (restore, release, gate): its time joins the spell's used budget."""
    return replace(state, spell_used_s=budget_used_s(state, now), lowered_since=None)


def _spent(state: OvercoolState, now: datetime) -> bool:
    return budget_used_s(state, now) >= OVERCOOL_RUN_CAP_S - _EPS


def _engage(state: OvercoolState, inp: OvercoolInputs) -> OvercoolDecision:
    """A cooling run with the latch on, budget left and room under the gap rule: the first step (or "would",
    observe-only). A stretch already in force (a coast re-engage) keeps counting from where it started."""
    if not (inp.cooling and inp.humid and inp.gate == "open") or inp.headroom_f < OVERCOOL_STEP_F - _EPS:
        return OvercoolDecision(state)
    if _spent(state, inp.now):
        return OvercoolDecision(state)  # this humid spell's 60 minutes are used: wait for the latch to re-arm
    if inp.observe_only:
        if state.observe_reported:
            return OvercoolDecision(state)
        return OvercoolDecision(replace(state, observe_reported=True), ACTION_WOULD_ENGAGE)
    engaged = OvercoolState(
        phase=PHASE_ACTIVE,
        magnitude_f=OVERCOOL_STEP_F,
        engaged_at=inp.now,
        observe_reported=state.observe_reported,
        spell_used_s=state.spell_used_s,
        lowered_since=state.lowered_since or inp.now,
    )
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
    dried = not inp.humid and inp.dew_point_known  # an unknown dew point is not a dry house: the spell goes on
    if dried and not lowering and (tracked.spell_used_s or tracked.lowered_since is not None):
        tracked = replace(tracked, spell_used_s=0.0, lowered_since=None)  # the humid spell is over: a fresh budget
    if inp.gate == "closed":
        if lowering:
            closed = _close_stretch(tracked, inp.now)
            done = OvercoolState(
                phase=PHASE_IDLE if (ended or tracked.phase == PHASE_COASTING) else PHASE_DONE,
                not_cooling_since=tracked.not_cooling_since,
                observe_reported=tracked.observe_reported,
                spell_used_s=closed.spell_used_s,
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
            # The next run: re-engage on the same stretch while the spell has budget, else restore (and stay out
            # until the latch releases and re-arms).
            restored = OvercoolState(
                observe_reported=tracked.observe_reported,
                spell_used_s=tracked.spell_used_s,
                lowered_since=tracked.lowered_since,
            )
            nxt = _engage(restored, inp)
            if nxt.action == ACTION_ENGAGE:
                return OvercoolDecision(nxt.state, ACTION_ENGAGE, REASON_NEXT_RUN, write=True, event=EVENT_STARTED)
            return OvercoolDecision(_close_stretch(nxt.state, inp.now), ACTION_RESTORE, REASON_NEXT_RUN, write=True)
        coast = _since(tracked.stopped_at, inp.now)
        if coast is not None and coast >= CYCLE_POST_OFF_WINDOW_S:
            closed = _close_stretch(tracked, inp.now)
            return OvercoolDecision(
                OvercoolState(
                    not_cooling_since=tracked.not_cooling_since,
                    observe_reported=tracked.observe_reported,
                    spell_used_s=closed.spell_used_s,
                ),
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
    if not inp.humid or _spent(tracked, inp.now):
        done = replace(_close_stretch(tracked, inp.now), phase=PHASE_DONE, magnitude_f=0.0, engaged_at=None)
        if dried:
            done = replace(done, spell_used_s=0.0)  # dried: the spell is over, the next one has its full budget
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
        _add(state.lowered_since, OVERCOOL_RUN_CAP_S - state.spell_used_s)  # this spell's budget runs out
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
