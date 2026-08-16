"""Nat-vent lifecycle FSM — the unified transition table (Issue #633, Block 5
Phase P completion, epic #594).

Assembles the 3 existing, already-validated pure pieces into one explicit
``(state, event) -> Transition`` function — the piece Phase P's earlier
sub-issues (#606/#607, #608/#609) deliberately left unbuilt:

- ``nat_vent_lifecycle.derive_nat_vent_lifecycle_state()`` — the 4-state enum
  this module reuses unchanged.
- ``nat_vent_gate.decide_nat_vent_gate()`` / ``decide_nat_vent_soft_start_gate()``
  — entry/reactivation decisions, called only while inactive/locked-out.
- ``nat_vent_exit.decide_nat_vent_exit()`` — the active-session exit chain,
  called only while active.

This module does not re-derive or duplicate any of that logic — it is
integration/wiring only: which pure function to call given the current state,
and how to map its result to the next state. Each of the 3 pieces already has
its own exhaustive unit-test coverage; this module's own tests focus on the
wiring correctness, not re-proving gate/exit logic already proven elsewhere.

**Markov property**: every transition is a pure function of (current state,
event) alone — no hidden extra memory. This is why ``ACTIVE_SOFT_START`` and
``ACTIVE_FULL_GATE`` are distinct states rather than one ``ACTIVE`` state plus
an out-of-band "how did we get here" flag (this precedent already exists in
``nat_vent_lifecycle.py``, reused here).

**v2 (Phase R prep, Issue #594 follow-up): soft-start escalation now modeled.**
``_transition_from_active()`` re-checks ``decide_nat_vent_gate()`` — the same
pure function ``_transition_from_inactive()`` already calls — whenever the
current state is ``ACTIVE_SOFT_START``, before running the exit chain. This
mirrors production's own upgrade check at ``automation.py``'s
"soft-start → full nat-vent upgrade" block (Issue #540), which independently
re-evaluates the identical full-gate condition each active tick. No new
decision logic was written — this reuses the one pure function already in
scope, the same discipline every other piece of this module already follows.

**Still-explicit scope boundary — the ``_idle_open`` widening.** Not modeled,
but re-classified: this is not omitted decision logic, it's a **caller-side
triggering precondition**. ``check_natural_vent_conditions()``
(Issue #244/#402/#504/#620) only re-evaluates reactivation on a given tick if
a contact sensor is open, HVAC is idle, debounce has settled, and grace isn't
blocking it (or grace + over-ceiling). That gate decides *whether this FSM's
entry logic runs at all this tick*, not what it should decide once run — the
same relationship ``paused_by_door`` already has to this module (an external
fact fed in, not re-derived here). Once Step 2's cutover work makes this FSM's
``transition()`` the thing production's own call site invokes, it will
naturally only fire when that same precondition already holds, since it's the
same call site — no separate modeling needed inside the transition table
itself.

**Cross-lifecycle inputs.** ``paused_by_door`` is a state *read* from the
door/window lifecycle (Issue #631's "communicating automata" design) — legacy
flag-derived today, unchanged in shape once door/window gets its own FSM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .nat_vent_exit import NatVentExitInputs, NatVentExitReason, decide_nat_vent_exit
from .nat_vent_gate import (
    NatVentGateInputs,
    NatVentSoftStartGateInputs,
    decide_nat_vent_gate,
    decide_nat_vent_soft_start_gate,
)
from .nat_vent_lifecycle import NatVentLifecycleState
from .nat_vent_reactivation_lockout import is_reactivation_locked_out


class NatVentFsmEventKind(Enum):
    """What prompted this transition evaluation. Every kind runs the same
    re-evaluation logic in v1 (see module docstring) — captured for the audit
    trail (epic success criterion #2), not yet branched on internally."""

    TICK = "tick"
    DOOR_PAUSE_STARTED = "door_pause_started"
    DOOR_PAUSE_ENDED = "door_pause_ended"
    GRACE_STARTED = "grace_started"
    GRACE_ENDED = "grace_ended"
    OVERRIDE_CONFIRMED = "override_confirmed"
    OVERRIDE_CLEARED = "override_cleared"


@dataclass(frozen=True)
class NatVentFsmInputs:
    """Every live reading this FSM's transition function may consult —
    explicit, nothing hidden. Union of ``NatVentGateInputs``,
    ``NatVentSoftStartGateInputs``, and ``NatVentExitInputs``' fields, plus the
    cross-lifecycle state reads (``paused_by_door``) and the lockout/outdoor-
    exit-time bookkeeping ``derive_nat_vent_lifecycle_state()`` already reads.
    """

    indoor: float | None
    outdoor: float | None
    comfort_heat_raw: float
    sleep_heat: float
    in_sleep_window: bool
    comfort_cool: float
    nat_vent_delta: float
    hysteresis: float
    fan_mode: str
    aggressive_savings: bool
    occupancy_mode: str
    thermal_confidence: str
    k_passive: float | None
    outdoor_today_peak: float | None
    outdoor_sample_count: int
    peak_decline_margin: float
    paused_by_door: bool
    outdoor_exit_time: datetime | None
    lockout_seconds: float
    now: datetime


@dataclass(frozen=True)
class NatVentFsmEvent:
    kind: NatVentFsmEventKind
    inputs: NatVentFsmInputs


@dataclass(frozen=True)
class NatVentTransition:
    """The transition's full record — audit trail by construction."""

    from_state: NatVentLifecycleState
    to_state: NatVentLifecycleState
    event_kind: NatVentFsmEventKind
    exit_reason: NatVentExitReason | None = None
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _exit_inputs(inputs: NatVentFsmInputs) -> NatVentExitInputs:
    return NatVentExitInputs(
        indoor=inputs.indoor,
        outdoor=inputs.outdoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        hysteresis=inputs.hysteresis,
        comfort_cool=inputs.comfort_cool,
        nat_vent_delta=inputs.nat_vent_delta,
        occupancy_mode=inputs.occupancy_mode,
        thermal_confidence=inputs.thermal_confidence,
        k_passive=inputs.k_passive,
    )


def _gate_inputs(inputs: NatVentFsmInputs) -> NatVentGateInputs:
    return NatVentGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_heat_raw=inputs.comfort_heat_raw,
        sleep_heat=inputs.sleep_heat,
        in_sleep_window=inputs.in_sleep_window,
        comfort_cool=inputs.comfort_cool,
        nat_vent_delta=inputs.nat_vent_delta,
        hysteresis=inputs.hysteresis,
        fan_mode=inputs.fan_mode,
        aggressive_savings=inputs.aggressive_savings,
    )


def _soft_start_inputs(inputs: NatVentFsmInputs, *, full_gate_active: bool) -> NatVentSoftStartGateInputs:
    return NatVentSoftStartGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_heat=inputs.comfort_heat_raw,
        comfort_cool=inputs.comfort_cool,
        fan_mode=inputs.fan_mode,
        outdoor_today_peak=inputs.outdoor_today_peak,
        outdoor_sample_count=inputs.outdoor_sample_count,
        peak_decline_margin=inputs.peak_decline_margin,
        full_gate_active=full_gate_active,
    )


def _transition_from_active(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    # Issue #540 (mirrored, Phase R prep): while soft-started, re-check the full
    # gate each tick — same pure function, same condition production's own
    # upgrade block re-evaluates. Escalating doesn't change the exit-chain
    # outcome below (decide_nat_vent_exit() treats soft-start and full-gate
    # identically), it only changes what state a NONE-exit tick lands on.
    escalated_state = current_state
    if current_state == NatVentLifecycleState.ACTIVE_SOFT_START and decide_nat_vent_gate(_gate_inputs(event.inputs)):
        escalated_state = NatVentLifecycleState.ACTIVE_FULL_GATE

    decision = decide_nat_vent_exit(_exit_inputs(event.inputs))
    if decision.reason == NatVentExitReason.NONE:
        return NatVentTransition(
            from_state=current_state, to_state=escalated_state, event_kind=event.kind, at=event.inputs.now
        )

    # Only the outdoor-rise exit records an outdoor_exit_time in production
    # (nat_vent_lifecycle.py's own documented field correspondence) — and only
    # hands off to the locked-out pause state while the monitored sensor is
    # still open; otherwise it's a clean inactive exit (grace, if any, is a
    # separate not-yet-migrated lifecycle's concern, not this FSM's).
    if decision.reason == NatVentExitReason.OUTDOOR_RISE and event.inputs.paused_by_door:
        next_state = NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT
    else:
        next_state = NatVentLifecycleState.INACTIVE

    return NatVentTransition(
        from_state=current_state,
        to_state=next_state,
        event_kind=event.kind,
        exit_reason=decision.reason,
        at=event.inputs.now,
    )


def _transition_from_inactive(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    inputs = event.inputs
    if inputs.paused_by_door and is_reactivation_locked_out(
        outdoor_exit_time=inputs.outdoor_exit_time, now=inputs.now, lockout_seconds=inputs.lockout_seconds
    ):
        return NatVentTransition(
            from_state=current_state,
            to_state=NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT,
            event_kind=event.kind,
            at=inputs.now,
        )

    full_gate = decide_nat_vent_gate(_gate_inputs(inputs))
    if full_gate:
        next_state = NatVentLifecycleState.ACTIVE_FULL_GATE
    elif decide_nat_vent_soft_start_gate(_soft_start_inputs(inputs, full_gate_active=full_gate)):
        next_state = NatVentLifecycleState.ACTIVE_SOFT_START
    else:
        next_state = NatVentLifecycleState.INACTIVE

    return NatVentTransition(from_state=current_state, to_state=next_state, event_kind=event.kind, at=inputs.now)


def transition(current_state: NatVentLifecycleState, event: NatVentFsmEvent) -> NatVentTransition:
    """The single entry point: given the current state and an event carrying a
    live-inputs snapshot, return the next state (and why).

    Dispatches to the exit chain while active, the entry/soft-start gates
    while inactive or locked-out — mirrors ``check_natural_vent_conditions()``'s
    own top-level ``if self._natural_vent_active:`` branch, the one structural
    decision every real call site already makes.
    """
    if current_state in (NatVentLifecycleState.ACTIVE_FULL_GATE, NatVentLifecycleState.ACTIVE_SOFT_START):
        return _transition_from_active(current_state, event)
    return _transition_from_inactive(current_state, event)
