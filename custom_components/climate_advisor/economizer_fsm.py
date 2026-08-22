"""Economizer lifecycle FSM — the unified transition table for the two-phase
window-cooling subsystem (strangler-fig completion program, Phase 5,
Issue #746).

Assembles the pure pieces into one explicit ``(state, event) -> Transition``
function, mirroring ``nat_vent_fsm.py``'s shape (state-first dispatch, since —
like nat-vent — this is periodically re-evaluated from one real call site
each cycle, not handler-triggered from many):

- ``economizer_lifecycle.derive_economizer_lifecycle_state()`` — the 3-state
  enum this module reuses unchanged.
- ``economizer_gate.decide_economizer_transition()`` — the pure
  eligibility/phase-selection gate, called only once the two session-level
  short-circuits below have already been ruled out.

**Session-level short-circuits, modeled here rather than in the gate.** Two
conditions in production's ``check_window_cooling_opportunity()`` are checked
*before* the eligibility/phase gate ever runs, and they are genuinely
session-shaped (they read state outside the gate's own inputs), so — mirroring
where ``nat_vent_fsm.py`` places its own override/grace short-circuit, ahead
of the gate/exit calls it wraps — they live in this module's ``transition()``,
not in ``economizer_gate.py``:

1. **Not a hot day** (``day_type != DAY_TYPE_HOT``, including no classification
   yet). Production deactivates the economizer if it was active and returns
   ``False``. Modeled here as an unconditional transition to ``INACTIVE``.

2. **Natural ventilation is active.** Production returns ``False`` immediately
   — deliberately WITHOUT deactivating, even if the economizer was already
   active. This is a real, preserved asymmetry from case 1, not an oversight:
   nat-vent taking over is a "defer for now" signal, not an "economizer
   session ended" signal. Modeled here as a no-op transition (``to_state ==
   current_state``) with ``deferred=True`` so the caller can reproduce
   production's exact "return False without touching state" behavior even
   when the economizer was already active going in.

**Markov property**: every transition is a pure function of (current state,
event) alone — matches ``nat_vent_fsm.py``'s own discipline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .const import DAY_TYPE_HOT
from .economizer_gate import EconomizerGateInputs, decide_economizer_transition
from .economizer_lifecycle import EconomizerLifecycleState


class EconomizerFsmEventKind(Enum):
    """What prompted this transition evaluation. Only ``TICK`` is fed from the
    real call site today (the single 30-min-cycle call in ``coordinator.py`` —
    economizer has just one production call site, unlike fan/WHF's many)."""

    TICK = "tick"


@dataclass(frozen=True)
class EconomizerFsmInputs:
    """Every live reading this FSM's transition function may consult —
    explicit, nothing hidden. Union of the two session-level short-circuit
    inputs and ``EconomizerGateInputs``' own fields.

    Field-by-field correspondence to the real code:
      day_type                 -> self._current_classification.day_type
                                   (None when there is no current classification
                                   yet, matching production's ``if not c or
                                   c.day_type != DAY_TYPE_HOT`` combined guard)
      natural_vent_active       -> self._natural_vent_active
      outdoor, indoor           -> check_window_cooling_opportunity()'s own
                                    outdoor_temp/indoor_temp parameters
      comfort_cool, delta,
      windows_physically_open,
      in_window, aggressive_savings -> see EconomizerGateInputs
      now                       -> caller-resolved wall-clock time, same
                                    convention as nat_vent_fsm.py
    """

    day_type: str | None
    natural_vent_active: bool
    outdoor: float
    indoor: float | None
    comfort_cool: float
    delta: float
    windows_physically_open: bool
    in_window: bool
    aggressive_savings: bool
    now: datetime


@dataclass(frozen=True)
class EconomizerFsmEvent:
    kind: EconomizerFsmEventKind
    inputs: EconomizerFsmInputs


@dataclass(frozen=True)
class EconomizerTransition:
    """The transition's full record — audit trail by construction."""

    from_state: EconomizerLifecycleState
    to_state: EconomizerLifecycleState
    event_kind: EconomizerFsmEventKind
    # None when the gate was never reached (not-a-hot-day short-circuit);
    # otherwise the gate's own direction-check result, so the caller can
    # reproduce production's direction-rejected debug log without
    # recomputing free_cooling_direction_ok() a second time.
    direction_ok: bool | None = None
    # True only for the natural-vent-active short-circuit (case 2 above) —
    # signals "return False without touching state," distinct from a real
    # transition to INACTIVE (case 1, or gate-ineligible).
    deferred: bool = False
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _gate_inputs(inputs: EconomizerFsmInputs) -> EconomizerGateInputs:
    return EconomizerGateInputs(
        outdoor=inputs.outdoor,
        indoor=inputs.indoor,
        comfort_cool=inputs.comfort_cool,
        delta=inputs.delta,
        windows_physically_open=inputs.windows_physically_open,
        in_window=inputs.in_window,
        aggressive_savings=inputs.aggressive_savings,
    )


def transition(current_state: EconomizerLifecycleState, event: EconomizerFsmEvent) -> EconomizerTransition:
    """The single entry point: given the current state and an event carrying a
    live-inputs snapshot, return the next state (and why).

    Mirrors ``check_window_cooling_opportunity()``'s own top-to-bottom
    structure: not-hot-day short-circuit, then nat-vent-active short-circuit,
    then the eligibility/phase gate.
    """
    inputs = event.inputs

    if inputs.day_type != DAY_TYPE_HOT:
        return EconomizerTransition(
            from_state=current_state,
            to_state=EconomizerLifecycleState.INACTIVE,
            event_kind=event.kind,
            at=inputs.now,
        )

    if inputs.natural_vent_active:
        return EconomizerTransition(
            from_state=current_state,
            to_state=current_state,
            event_kind=event.kind,
            deferred=True,
            at=inputs.now,
        )

    decision = decide_economizer_transition(_gate_inputs(inputs))

    if not decision.eligible:
        return EconomizerTransition(
            from_state=current_state,
            to_state=EconomizerLifecycleState.INACTIVE,
            event_kind=event.kind,
            direction_ok=decision.direction_ok,
            at=inputs.now,
        )

    next_state = (
        EconomizerLifecycleState.COOL_DOWN if decision.phase == "cool-down" else EconomizerLifecycleState.MAINTAIN
    )
    return EconomizerTransition(
        from_state=current_state,
        to_state=next_state,
        event_kind=event.kind,
        direction_ok=decision.direction_ok,
        at=inputs.now,
    )
