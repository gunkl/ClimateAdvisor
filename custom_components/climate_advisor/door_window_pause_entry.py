"""Pure decision core for entering a door/window pause (Issue #637, Block 5 Phase 2).

Reimplements the branch structure of ``_pause_for_door_window()`` — the single
choke point Issue #523 introduced after ``handle_door_window_open()`` and
``_re_pause_for_open_sensor()`` used to hand-roll this off-vs-already-off split
separately and drifted out of sync.

Scope: this module answers "given the current HVAC mode, what should happen on
pause entry?" as a pure function. It does NOT perform the mode write, the
notification, or the event emission — those stay shell-side (they're I/O),
exactly as ``nat_vent_gate.py``'s split from its own shell callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PauseEntryOutcome(Enum):
    """Mirrors ``_pause_for_door_window()``'s three-way branch on live HVAC mode."""

    PAUSE_ACTIVE = "pause_active"  # mode not in (off, unavailable, unknown) — real mode transition
    PAUSE_IDLE = "pause_idle"  # mode == "off" — flag-only pause, Issue #523
    NOOP_NOT_APPLICABLE = "noop_not_applicable"  # mode in (unavailable, unknown) — no pause recorded


@dataclass(frozen=True)
class PauseEntryInputs:
    """Field-by-field correspondence to the real code:

    hvac_mode -> ``self.hass.states.get(self.climate_entity)`` -> ``state.state``
                 (``None`` when the entity itself is missing, distinct from the
                 string ``"unavailable"``/``"unknown"`` — the real code checks
                 ``mode and mode not in (...)`` so ``None`` also falls to the
                 not-applicable branch)
    """

    hvac_mode: str | None


_NOT_ACTIVE_MODES = ("off", "unavailable", "unknown")


def decide_pause_entry(inputs: PauseEntryInputs) -> PauseEntryOutcome:
    """Pure reimplementation of ``_pause_for_door_window()``'s mode branch.

    Precedence mirrors the real code exactly: a mode outside (off, unavailable,
    unknown) — including ``None`` — is an active pause; ``"off"`` is an idle
    pause; ``"unavailable"``/``"unknown"`` is a no-op (the real method's implicit
    fall-through — neither branch's condition matches, so nothing is set).
    """
    mode = inputs.hvac_mode
    if mode and mode not in _NOT_ACTIVE_MODES:
        return PauseEntryOutcome.PAUSE_ACTIVE
    if mode == "off":
        return PauseEntryOutcome.PAUSE_IDLE
    return PauseEntryOutcome.NOOP_NOT_APPLICABLE
