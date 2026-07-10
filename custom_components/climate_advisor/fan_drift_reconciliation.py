"""Pure decision core for fan physical-state drift reconciliation (Issue #423).

Architecture-reset Step 2 (session state machine slice): pure reimplementation
of ``_reconcile_fan_physical_drift()``'s decision logic — the 2-tick-confirm
self-healing check that detects a stale ``_fan_active=True`` with no matching
physical fan and corrects it. Mirrors the same pattern as
``nat_vent_gate.py``/``fan_thermostat_decision.py``: every field on
``FanDriftInputs`` traces to a real read in the production method, and this
function returns only the decision (outcome + next tick count) — the shell
still owns applying the side effects (clearing flags, starting grace, emitting
the event).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

FAN_MODE_WHOLE_HOUSE = "whole_house_fan"
FAN_MODE_BOTH = "both"

_DRIFT_CONFIRM_TICKS = 2


class FanDriftOutcome(Enum):
    """The three real outcomes _reconcile_fan_physical_drift() can produce."""

    RESET = "reset"  # tick count -> 0, no correction (fan inactive / command echo / agrees)
    NOOP = "noop"  # tick count unchanged, no correction (not-applicable archetype / no ground truth)
    AWAITING = "awaiting"  # tick count incremented, not yet at the 2-tick confirmation threshold
    CORRECT = "correct"  # confirmed drift over 2 ticks -> tick count resets to 0, correction fires


@dataclass(frozen=True)
class FanDriftInputs:
    """Every input the drift-reconciliation check may read — explicit, nothing hidden.

    Field-by-field correspondence to the real code:
      fan_active               -> self._fan_active
      fan_mode                 -> config CONF_FAN_MODE
      recent_fan_command       -> self._is_recent_fan_command_callback(threshold_seconds=30.0)
      physical_state_available -> whether self._get_fan_physical_state_callback is set
      physical_on              -> self._get_fan_physical_state_callback() — None means
                                   command-only mode (no ground truth to compare against)
      tick_count                -> self._fan_drift_tick_count (persisted across backstop ticks)
    """

    fan_active: bool
    fan_mode: str
    recent_fan_command: bool
    physical_state_available: bool
    physical_on: bool | None
    tick_count: int


def decide_fan_drift_reconciliation(inputs: FanDriftInputs) -> tuple[FanDriftOutcome, int]:
    """Pure reimplementation of _reconcile_fan_physical_drift()'s decision logic.

    Returns (outcome, next_tick_count). The shell applies outcome-specific side
    effects (RESET/NOOP/AWAITING have none besides the tick-count update;
    CORRECT additionally clears fan flags, starts grace, and emits fan_cancel).
    """
    if not inputs.fan_active:
        return FanDriftOutcome.RESET, 0

    if inputs.fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
        return FanDriftOutcome.NOOP, inputs.tick_count

    if inputs.recent_fan_command:
        return FanDriftOutcome.RESET, 0

    if not inputs.physical_state_available:
        return FanDriftOutcome.NOOP, inputs.tick_count

    if inputs.physical_on is None:
        return FanDriftOutcome.NOOP, inputs.tick_count

    if inputs.physical_on:
        return FanDriftOutcome.RESET, 0

    # physical_on is False but fan_active is True — disagreement.
    next_count = inputs.tick_count + 1
    if next_count < _DRIFT_CONFIRM_TICKS:
        return FanDriftOutcome.AWAITING, next_count

    return FanDriftOutcome.CORRECT, 0
