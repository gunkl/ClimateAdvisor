"""Pure session-state derivation for the override/grace joint lifecycle (Issue #639,
Block 5 Phase 3, epic #594).

**Two composed state pieces, not one flat enum.** Override and grace are not one state
machine layered on a single boolean — they are two orthogonal-but-coupled pieces. Grace
has 6 distinct trigger sources (``fan_manual_override``, ``fan_off``, ``dashboard_resume``,
``override_confirmed``, ``window_close``, ``nat_vent_exit``), only 2 of which
(``_GRACE_TRIGGERS_PROTECTING_OVERRIDE``, automation.py:150) correspond to a real override.
The other 4 grace types run with zero override behind them by design. Override
confirmation (``_override_confirm_pending``) precedes only the thermostat-override grace
path, not fan/dashboard-resume/window-close. Collapsing this into one flat enum (mirroring
``NatVentLifecycleState``/``DoorWindowLifecycleState``) would misrepresent reachability —
grace outlives override in 3 of 4 ``GraceExpiryOutcome`` branches — the same risk Phase 2's
``PAUSED_DURING_GRACE`` finding warns against.

``GraceState`` folds ``_grace_protects_override`` in as a 3-way split (not a separate bool
alongside a 2-way ACTIVE) because that flag is a trigger-keyed membership test
(Issue #530) carried at grace-start time, not something to re-derive after the fact —
"which grace am I in" is a single read matching the single-timer-slot invariant
(``_start_grace_period()`` always cancels any running timer first — no two concurrent
graces are ever reachable, so ``GraceState`` needs no composite/parallel arm).

``derive_override_grace_lifecycle_state()`` returns a plain
``tuple[OverrideConfirmState, GraceState]`` — never a merged Enum. Only a subset of the
2x3=6 cross-product cells are reachable in production; see the reachability table below.

Purely a read of existing truth — no new state is stored anywhere. Not called from any
production decision path; differentially validated in shadow mode only, same discipline
as ``door_window_lifecycle.py``/``nat_vent_lifecycle.py``.

**Reachability table** (confirm state x grace state):

  IDLE / NONE                      — normal operation, no override, no grace. Reachable.
  IDLE / ACTIVE_PROTECTING_OVERRIDE — an override was already confirmed (confirm window
                                       closed) and its grace is running. Reachable — the
                                       common case after ``_confirm_override()``.
  IDLE / ACTIVE_UNPROTECTED         — fan-off, window-close, or dashboard-resume grace
                                       running with no override. Reachable — the common
                                       case for those 4 non-override triggers.
  PENDING / NONE                    — a possible override was just detected, confirmation
                                       window running, no grace yet (grace only starts once
                                       confirmed). Reachable — the common transient state.
  PENDING / ACTIVE_PROTECTING_OVERRIDE — a NEW override is being detected while a PRIOR
                                       override's grace is still active (Issue #282's
                                       "Fix D" supersession branch in
                                       ``coordinator._async_thermostat_changed()`` clears
                                       the old override via ``clear_manual_override()``
                                       before starting the new confirmation window, which
                                       cancels grace too — so this cell is transiently
                                       reachable only inside that clear-then-restart
                                       sequence, never as a stable observed state).
  PENDING / ACTIVE_UNPROTECTED      — a possible override is detected while a non-override
                                       grace (fan-off/window-close/dashboard-resume) is
                                       running. Reachable — confirmation and non-override
                                       grace are independent flags with no mutual guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OverrideConfirmState(Enum):
    """Whether a detected override is currently in its confirmation window."""

    IDLE = "idle"
    PENDING = "pending"


class GraceState(Enum):
    """Whether a grace period is running, and whether it protects a real override."""

    NONE = "none"
    ACTIVE_PROTECTING_OVERRIDE = "active_protecting_override"
    ACTIVE_UNPROTECTED = "active_unprotected"


OverrideGraceLifecycleState = tuple[OverrideConfirmState, GraceState]


@dataclass(frozen=True)
class OverrideGraceLifecycleInputs:
    """Every flag the derivation reads — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      override_confirm_pending -> self._override_confirm_pending
      grace_active              -> self._grace_active
      grace_protects_override   -> self._grace_protects_override
    """

    override_confirm_pending: bool
    grace_active: bool
    grace_protects_override: bool


def derive_override_grace_lifecycle_state(
    inputs: OverrideGraceLifecycleInputs,
) -> OverrideGraceLifecycleState:
    """Pure derivation of the current (confirm, grace) state pair from existing flags."""
    confirm = OverrideConfirmState.PENDING if inputs.override_confirm_pending else OverrideConfirmState.IDLE

    if not inputs.grace_active:
        grace = GraceState.NONE
    elif inputs.grace_protects_override:
        grace = GraceState.ACTIVE_PROTECTING_OVERRIDE
    else:
        grace = GraceState.ACTIVE_UNPROTECTED

    return (confirm, grace)
