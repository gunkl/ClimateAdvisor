"""Pure decision core for grace-period expiry (Issue #637, Block 5 Phase 2).

Reimplements ``_on_grace_expired()``'s first-match-wins chain: planned-window
exemption, sensor-still-open re-pause, Issue #483's adopt-matching-decision
check, and the plain clear.

**Explicit scope boundary.** Issue #530's RF-timer-boundary settle-window side
effect (arming ``_timer_boundary_settle_until`` before any branch below is
even evaluated) is NOT modeled — it is orthogonal to which outcome fires, a
pure side effect keyed on ``source == "manual"`` and a separate flag, and stays
shell-side, same discipline as nat-vent's soft-start-escalation exclusion.
Notification text selection also stays shell-side (I/O).

``override_matches_current_decision`` is caller-resolved (via the real
``_override_matches_current_decision()``, which needs live ``DayClassification``
comparison this module has no business re-deriving) rather than computed here —
same "no logic duplicated across modules" rule as
``door_window_open_response.py``'s ``nat_vent_gate_entered`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GraceExpiryOutcome(Enum):
    CLEAR_PLANNED_WINDOW = "clear_planned_window"
    RE_PAUSE = "re_pause"
    ADOPT_OVERRIDE = "adopt_override"
    CLEAR_NORMAL = "clear_normal"


@dataclass(frozen=True)
class GraceExpiryInputs:
    """Field-by-field correspondence to ``_on_grace_expired()``:

    within_planned_window            -> self._is_within_planned_window_period()
    any_sensor_open                   -> self._any_monitored_sensor_open()
    source                             -> the grace's own source ("manual" | "automation")
    override_matches_current_decision -> self._override_matches_current_decision(
                                          self._current_classification), only ever
                                          consulted when source == "manual"
    """

    within_planned_window: bool
    any_sensor_open: bool
    source: str
    override_matches_current_decision: bool


def decide_grace_expiry_outcome(inputs: GraceExpiryInputs) -> GraceExpiryOutcome:
    """Pure reimplementation of ``_on_grace_expired()``'s first-match-wins chain."""
    if inputs.within_planned_window:
        return GraceExpiryOutcome.CLEAR_PLANNED_WINDOW

    if inputs.any_sensor_open:
        return GraceExpiryOutcome.RE_PAUSE

    if inputs.source == "manual" and inputs.override_matches_current_decision:
        return GraceExpiryOutcome.ADOPT_OVERRIDE

    return GraceExpiryOutcome.CLEAR_NORMAL
