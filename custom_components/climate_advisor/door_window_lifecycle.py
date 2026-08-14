"""Pure session-state derivation for the door/window pause/grace lifecycle (Issue #637,
Block 5 Phase 2, epic #594).

Mirrors ``nat_vent_lifecycle.py``'s role for nat-vent: answers "given the engine's
current flags, what SESSION state is door/window pause/grace in right now?" — a
question ``docs/grace-periods-spec.md`` itself flags as having no named state enum
in the code today, only an undifferentiated NORMAL/PAUSED/GRACE table plus a
"special case" nat-vent overlap row.

**Five states, not three.** ``_paused_with_hvac_already_off`` (Issue #523's fix)
makes "all sensors close" resolve to two different destinations depending on
whether HVAC was actively interrupted when the pause started — a flag-only
PAUSED can't express that distinction as a clean state read without re-deriving
history, so ``PAUSED_ACTIVE``/``PAUSED_IDLE`` are separate states here, same
precedent as nat-vent's own ``ACTIVE_SOFT_START``/``ACTIVE_FULL_GATE`` split.

**``PAUSED_DURING_GRACE`` — confirmed reachable, not hypothetical.** The
production invariant "``_paused_by_door`` and ``_grace_active`` are mutually
exclusive" does not actually hold: two reachable paths set one without clearing
the other — ``handle_door_window_open()`` falling through to
``_pause_for_door_window()`` while ``_grace_active`` is still True (its own
outdoor-too-warm guard only blocks the branch, it never re-checks after falling
through to the nat-vent-gate-failed pause), and ``_exit_nat_vent()``'s
sensor-still-open branch setting ``_paused_by_door = True`` directly without
touching ``_grace_active`` at all. See the Phase 2 plan
(``docs/grace-periods-spec.md`` and the epic #594 tracking issue) for the full
static-trace evidence. This module models the combination as a legitimate fifth
state rather than silently collapsing it — the alternative (picking a winner)
would misrepresent what production is actually doing, which is exactly the risk
this whole FSM effort exists to eliminate.

Purely a read of existing truth — no new state is stored anywhere. Not called
from any production decision path; differentially validated in shadow mode
only, same discipline as ``nat_vent_lifecycle.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DoorWindowLifecycleState(Enum):
    """The five states the door/window pause/grace lifecycle can be in."""

    NORMAL = "normal"
    PAUSED_ACTIVE = "paused_active"
    PAUSED_IDLE = "paused_idle"
    GRACE = "grace"
    PAUSED_DURING_GRACE = "paused_during_grace"


@dataclass(frozen=True)
class DoorWindowLifecycleInputs:
    """Every flag the derivation reads — explicit, nothing hidden.

    Field-by-field correspondence to the real ``AutomationEngine`` attributes:
      paused_by_door              -> self._paused_by_door
      paused_with_hvac_already_off -> self._paused_with_hvac_already_off (Issue #523)
      grace_active                -> self._grace_active
    """

    paused_by_door: bool
    paused_with_hvac_already_off: bool
    grace_active: bool


def derive_door_window_lifecycle_state(inputs: DoorWindowLifecycleInputs) -> DoorWindowLifecycleState:
    """Pure derivation of the current door/window lifecycle state from existing flags.

    Precedence:

    1. ``paused_by_door`` and ``grace_active`` both True — ``PAUSED_DURING_GRACE``
       (the confirmed-reachable composite state, see module docstring).
    2. ``paused_by_door`` alone — ``PAUSED_ACTIVE`` or ``PAUSED_IDLE``, split on
       ``paused_with_hvac_already_off``.
    3. ``grace_active`` alone — ``GRACE``.
    4. Neither — ``NORMAL``.
    """
    if inputs.paused_by_door and inputs.grace_active:
        return DoorWindowLifecycleState.PAUSED_DURING_GRACE

    if inputs.paused_by_door:
        if inputs.paused_with_hvac_already_off:
            return DoorWindowLifecycleState.PAUSED_IDLE
        return DoorWindowLifecycleState.PAUSED_ACTIVE

    if inputs.grace_active:
        return DoorWindowLifecycleState.GRACE

    return DoorWindowLifecycleState.NORMAL
