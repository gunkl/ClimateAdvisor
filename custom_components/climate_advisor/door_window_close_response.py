"""Pure decision core for all-doors-windows-closed (Issue #637, Block 5 Phase 2).

Reimplements ``handle_all_doors_windows_closed()``'s post-nat-vent-check
``if self._pre_pause_mode:`` split — the #523 bug's exit-side twin (the entry
side is ``door_window_pause_entry.py``).

**Explicit scope boundary.** The nat-vent handoff (``if self._natural_vent_active:
await self._exit_nat_vent(...); return``) and Issue #277's whole-house-fan stop
are NOT modeled here — both are cross-lifecycle/shell concerns the assembly FSM
handles as its own transition branch BEFORE calling this function, same as
``nat_vent_fsm.py``'s active-session dispatch happening before its own gate
calls. This module only answers "given we are NOT in a nat-vent handoff, what
should happen to the pause state?"
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DoorCloseResponseOutcome(Enum):
    NOOP_NOT_PAUSED = "noop_not_paused"
    RESTORE_AND_GRACE = "restore_and_grace"
    CLEAR_NO_GRACE = "clear_no_grace"


@dataclass(frozen=True)
class DoorCloseResponseInputs:
    """Field-by-field correspondence to ``handle_all_doors_windows_closed()``:

    paused_by_door -> self._paused_by_door
    pre_pause_mode -> self._pre_pause_mode
    """

    paused_by_door: bool
    pre_pause_mode: str | None


def decide_door_close_response(inputs: DoorCloseResponseInputs) -> DoorCloseResponseOutcome:
    """Pure reimplementation of the post-nat-vent-check pause-clear branch."""
    if not inputs.paused_by_door:
        return DoorCloseResponseOutcome.NOOP_NOT_PAUSED
    if inputs.pre_pause_mode:
        return DoorCloseResponseOutcome.RESTORE_AND_GRACE
    return DoorCloseResponseOutcome.CLEAR_NO_GRACE
