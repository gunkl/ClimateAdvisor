"""Pure session-state derivation for the natural-ventilation lifecycle (Issue #606,
Block 5 / epic #594, Phase 1).

Distinct from ``nat_vent_gate.py``'s ``decide_nat_vent_gate()``/
``decide_nat_vent_soft_start_gate()`` (which answer "should nat-vent (re)activate
right now?" as one-shot boolean gates) and from
``nat_vent_reactivation_lockout.py``'s ``is_reactivation_locked_out()`` (which
answers "is the post-exit cooldown still in effect?"). This module answers a
different question: **given the engine's current flags, what SESSION state is
nat-vent in right now?** Today that question has no single answer anywhere in
the codebase — it's always reconstructed ad hoc from a combination of
``_natural_vent_active``, ``_nat_vent_soft_start``, ``_paused_by_door``, and
``_nat_vent_outdoor_exit_time``. This module names the four states that
combination can represent and provides one pure function to derive the current
one.

Purely a read of existing truth — no new state is stored anywhere. Wired onto
``AutomationEngine`` as a read-only property (see automation.py's
``nat_vent_lifecycle_state``), not called from any production decision path.
Differentially validated against production's real nat-vent event sequence
across every golden/pending scenario — see
``tests/test_nat_vent_lifecycle_state.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .nat_vent_reactivation_lockout import is_reactivation_locked_out


class NatVentLifecycleState(Enum):
    """The four states a nat-vent session can be in, per Issue #594's epic text
    ("_natural_vent_active is the leading first candidate" for a lifecycle-scoped
    state machine)."""

    INACTIVE = "inactive"
    ACTIVE_FULL_GATE = "active_full_gate"
    ACTIVE_SOFT_START = "active_soft_start"
    PAUSED_REACTIVATION_LOCKOUT = "paused_reactivation_lockout"


@dataclass(frozen=True)
class NatVentLifecycleInputs:
    """Every flag the derivation reads — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      natural_vent_active   -> self._natural_vent_active
      nat_vent_soft_start   -> self._nat_vent_soft_start
      paused_by_door        -> self._paused_by_door
      outdoor_exit_time     -> self._nat_vent_outdoor_exit_time (only ever set by
                                the outdoor-reversal exit path — see
                                _exit_nat_vent()'s docstring)
      now                   -> caller-resolved wall-clock time (this module never
                                touches dt_util.now(), same convention as
                                nat_vent_gate.py's in_sleep_window)
      lockout_seconds       -> config CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, falls
                                back to NAT_VENT_REACTIVATION_LOCKOUT_S
    """

    natural_vent_active: bool
    nat_vent_soft_start: bool
    paused_by_door: bool
    outdoor_exit_time: datetime | None
    now: datetime
    lockout_seconds: float


def derive_nat_vent_lifecycle_state(inputs: NatVentLifecycleInputs) -> NatVentLifecycleState:
    """Pure derivation of the current nat-vent session state from existing flags.

    Precedence (checked in this order, matching how the four states are
    mutually distinguishable from the flag combination alone):

    1. ``natural_vent_active=True`` — an active session; soft-start sub-mode
       distinguishes ``ACTIVE_SOFT_START`` from ``ACTIVE_FULL_GATE``.
    2. ``natural_vent_active=False`` and paused with an outdoor-exit timestamp
       still within the reactivation lockout window — ``PAUSED_REACTIVATION_LOCKOUT``.
    3. Otherwise — ``INACTIVE`` (covers clean idle, a door/window pause NOT
       caused by a nat-vent outdoor-reversal exit, and a lockout that has
       already elapsed).
    """
    if inputs.natural_vent_active:
        if inputs.nat_vent_soft_start:
            return NatVentLifecycleState.ACTIVE_SOFT_START
        return NatVentLifecycleState.ACTIVE_FULL_GATE

    if inputs.paused_by_door and is_reactivation_locked_out(
        outdoor_exit_time=inputs.outdoor_exit_time,
        now=inputs.now,
        lockout_seconds=inputs.lockout_seconds,
    ):
        return NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT

    return NatVentLifecycleState.INACTIVE
