"""Comfort-family FSM — anti-chatter wrapper around the nearest-edge leaf
(Issue #1066, Design D; originally Issue #827; settling guard Issue #1073).

Mirrors ``economizer_fsm.py``'s split: a pure decision leaf
(``comfort_family_decision.py``) plus this thin transition layer.

- ``ComfortFamilyState`` — the 2-state enum (``HEATING``/``COOLING``).
- ``transition(current_state, event, dwell_state) -> ComfortFamilyTransition``
  — the single entry point, called once per relevant cycle. ``current_state``
  may be None (family unknown: thermostat off/heat_cool/auto/unavailable); the
  leaf then uses its default family as tie-break, unless a family ran recently
  (``settling_from_family``), which the settling guard then holds (Issue #1149).

What this module owns that the leaf does not:

0. **The settling guard lives in the leaf, not here.** A breach the leaf HOLDS (outcome HOLD,
   ``decision.held``) is a non-SWITCH to this layer: the state stays and the dwell clock does not move.
1. **Min-dwell anti-chatter (600 s default).** A non-breach SWITCH within
   ``min_dwell_seconds`` of the last genuine transition is locked out
   (``locked_out=True``, state held). A breach switch (indoor outside
   [floor, ceiling]) ALWAYS proceeds — comfort protection overrides the lock —
   and advances the clock. ``dwell_since`` moves only on a genuine transition,
   never on HOLD/NOT_APPLICABLE/locked-out ticks.
2. **The ``comfort_family_switch_locked_out`` signal** for the shell to emit.
3. **Restart persistence.** ``dwell_since`` itself is not persisted; the real
   thermostat mode is the memory for ``current_state``. The dwell clock, however,
   is fed by ``ComfortFamilyFsmInputs.last_mode_change_at`` -- the newest entry of
   the shell's persisted mode-change ring buffer (Issue #1073) -- so a mode change
   by any writer (and one made just before a restart) still arms the lock.

Supersedes the against-grain/candidate/recovery bookkeeping of
#821/#827/#843/#858/#867. No Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .comfort_family_decision import (
    ComfortFamilyDecision,
    ComfortFamilyInputs,
    ComfortFamilyOutcome,
    decide_comfort_family,
)


class ComfortFamilyState(Enum):
    """The two families the comfort-defense FSM tracks."""

    HEATING = "heating"
    COOLING = "cooling"


class ComfortFamilyEventKind(Enum):
    """What prompted this evaluation. Only ``TICK`` is fed today."""

    TICK = "tick"


@dataclass(frozen=True)
class ComfortFamilyDwellState:
    """Cross-call bookkeeping, owned by the shell (not restart-persisted)."""

    dwell_since: datetime | None = None


@dataclass(frozen=True)
class ComfortFamilyFsmInputs:
    """Every live reading the transition may consult, plus the dwell config.

    Same fields as ``ComfortFamilyInputs`` (minus ``current_family``, derived
    from the state argument) plus ``min_dwell_seconds``
    (``comfort_mode_switch_min_interval_s``).
    """

    default_family: str
    indoor: float | None
    floor: float | None
    ceiling: float | None
    margin_f: float
    natural_vent_active: bool
    whf_owns_hvac: bool
    now: datetime
    min_dwell_seconds: float
    supports_heat: bool = True
    supports_cool: bool = True
    tou_precondition_active: bool = False
    # Issue #1073 (S3): timestamp of the newest thermostat MODE CHANGE by any writer (selector,
    # restore, TOU, ODE guard, pause, nat-vent, user), read by the shell from the persisted
    # mode-change ring buffer. Pure data: the leaf never touches the buffer or Home Assistant.
    last_mode_change_at: datetime | None = None
    # Issue #1073 settling guard inputs, passed straight to the leaf (see ComfortFamilyInputs).
    tolerance_f: float = 0.0
    native_family: str | None = None
    opposite_ran_min_ago: float | None = None
    recency_window_min: float = 0.0
    # Issue #1149: the family the guard treats as being left while the live state is None (see leaf).
    settling_from_family: str | None = None


@dataclass(frozen=True)
class ComfortFamilyEvent:
    kind: ComfortFamilyEventKind
    inputs: ComfortFamilyFsmInputs


@dataclass(frozen=True)
class ComfortFamilyTransition:
    """Full transition record (audit trail by construction)."""

    from_state: ComfortFamilyState | None
    to_state: ComfortFamilyState | None
    event_kind: ComfortFamilyEventKind
    decision: ComfortFamilyDecision
    dwell_state: ComfortFamilyDwellState
    # True when the leaf wanted a non-breach switch but min-dwell blocked it;
    # the shell should emit "comfort_family_switch_locked_out".
    locked_out: bool = False
    at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state


def _leaf_inputs(inputs: ComfortFamilyFsmInputs, current_state: ComfortFamilyState | None) -> ComfortFamilyInputs:
    return ComfortFamilyInputs(
        current_family=current_state.value if current_state is not None else None,
        default_family=inputs.default_family,
        indoor=inputs.indoor,
        floor=inputs.floor,
        ceiling=inputs.ceiling,
        margin_f=inputs.margin_f,
        natural_vent_active=inputs.natural_vent_active,
        whf_owns_hvac=inputs.whf_owns_hvac,
        now=inputs.now,
        supports_heat=inputs.supports_heat,
        supports_cool=inputs.supports_cool,
        tou_precondition_active=inputs.tou_precondition_active,
        tolerance_f=inputs.tolerance_f,
        native_family=inputs.native_family,
        opposite_ran_min_ago=inputs.opposite_ran_min_ago,
        recency_window_min=inputs.recency_window_min,
        settling_from_family=inputs.settling_from_family,
    )


def _state_for(family: str | None) -> ComfortFamilyState | None:
    if family == ComfortFamilyState.HEATING.value:
        return ComfortFamilyState.HEATING
    if family == ComfortFamilyState.COOLING.value:
        return ComfortFamilyState.COOLING
    return None


def transition(
    current_state: ComfortFamilyState | None,
    event: ComfortFamilyEvent,
    dwell_state: ComfortFamilyDwellState | None = None,
) -> ComfortFamilyTransition:
    """Given the current state (None = unknown), live inputs and dwell
    bookkeeping, return the next state and why."""
    inputs = event.inputs
    state = dwell_state if dwell_state is not None else ComfortFamilyDwellState()
    decision = decide_comfort_family(_leaf_inputs(inputs, current_state))

    if decision.outcome is not ComfortFamilyOutcome.SWITCH:
        # HOLD / NOT_APPLICABLE: the dwell clock never moves. With an unknown
        # current state, adopt a HOLD decision's target (if any).
        to_state = current_state
        if to_state is None and decision.outcome is ComfortFamilyOutcome.HOLD:
            to_state = _state_for(decision.target_family)
        return ComfortFamilyTransition(
            from_state=current_state,
            to_state=to_state,
            event_kind=event.kind,
            decision=decision,
            dwell_state=state,
            at=inputs.now,
        )

    # Issue #1073 (S3): ONE mode-change clock. The lock runs from the newest mode change by ANY
    # writer (persisted ring buffer, survives restart); ``state.dwell_since`` (selector-only,
    # in-memory) is kept as a floor so a selector switch always counts even if the shell could
    # not read the buffer.
    _dwell_marks = [t for t in (state.dwell_since, inputs.last_mode_change_at) if t is not None]
    dwell_since = None
    for _mark in _dwell_marks:
        try:
            if dwell_since is None or _mark > dwell_since:
                dwell_since = _mark
        except TypeError:  # naive vs aware mix: ignore the unusable mark
            continue
    if not decision.breach and dwell_since is not None:
        try:
            elapsed = (inputs.now - dwell_since).total_seconds()
        except TypeError:
            elapsed = inputs.min_dwell_seconds  # unusable clock: do not lock
        if elapsed < inputs.min_dwell_seconds:
            return ComfortFamilyTransition(
                from_state=current_state,
                to_state=current_state,
                event_kind=event.kind,
                decision=decision,
                dwell_state=state,
                locked_out=True,
                at=inputs.now,
            )

    return ComfortFamilyTransition(
        from_state=current_state,
        to_state=_state_for(decision.target_family),
        event_kind=event.kind,
        decision=decision,
        dwell_state=ComfortFamilyDwellState(dwell_since=inputs.now),
        at=inputs.now,
    )
