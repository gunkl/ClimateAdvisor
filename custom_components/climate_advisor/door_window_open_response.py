"""Pure decision chain for a fresh door/window open event -- the handler's real chain (Issue #637, #1142).

``handle_door_window_open()`` asks the nat-vent FSM "may a fan session start now?" (one shell helper, one answer),
resolves the few engine facts around it into ``DoorOpenResponseInputs`` and dispatches on the enum this module
returns. The chain is the handler's order, exactly:

1. already paused                          -> ``ALREADY_PAUSED_NOOP``
2. the occupant's thermostat change is protected (owner rule 1, #1133/#1148)  -> ``PROTECTED_CHANGE_NOOP``
3. a grace that holds the pause and no fan session would start now             -> ``GRACE_SUPPRESSED``
   (#655/#1142 D3: "would start" is the nat-vent FSM's answer, forecast/floor guards and lockout included)
4. the temperature gate refused            -> ``PAUSE``
5. the occupant's fan-off hold             -> ``PAUSE_FAN_OFF_HOLD`` (#1006/#1205)
6. the reactivation lockout                -> ``PAUSE_REACTIVATION_LOCKOUT`` (#699)
7. a start rule vetoed the start           -> ``PAUSE_START_VETOED`` (forecast peak, thermal floor, no fan: #1138/#1140)
8. otherwise                               -> ``START_NAT_VENT`` (full gate or soft start)

The planned window is not an input: it gates the lazy sync reconcile, never this event (#1211, #1139).
Whether the fan command then actually took effect is the handler's business (it falls through to the pause).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DoorOpenResponse(Enum):
    ALREADY_PAUSED_NOOP = "already_paused_noop"
    PROTECTED_CHANGE_NOOP = "protected_change_noop"
    GRACE_SUPPRESSED = "grace_suppressed"
    START_NAT_VENT = "start_nat_vent"
    PAUSE = "pause"
    PAUSE_FAN_OFF_HOLD = "pause_fan_off_hold"
    PAUSE_REACTIVATION_LOCKOUT = "pause_reactivation_lockout"
    PAUSE_START_VETOED = "pause_start_vetoed"


@dataclass(frozen=True)
class DoorOpenResponseInputs:
    """Caller-resolved facts, field by field from ``handle_door_window_open()``:

    paused_by_door           -> self._paused_by_door
    manual_change_protected  -> self._manual_change_protected()
    grace_active             -> self._grace_active
    grace_ignored            -> the running grace is the occupant's blower press or a fan-off grace
                                (``_grace_protects_no_hvac_on_change()``): stopping or running a fan turns no HVAC
                                on, so it is no reason to hold off the pause (#1172, #1201)
    start_viable             -> the nat-vent FSM's answer is a start (full gate or soft start)
    start_vetoed             -> the temperature gate passed but a start rule refused it (``veto`` is set)
    fan_off_hold_active      -> self._fan_off_hold_active()
    reactivation_locked_out  -> is_reactivation_locked_out(...)
    """

    paused_by_door: bool
    manual_change_protected: bool
    grace_active: bool
    grace_ignored: bool
    start_viable: bool
    start_vetoed: bool
    fan_off_hold_active: bool
    reactivation_locked_out: bool


def decide_door_open_response(inputs: DoorOpenResponseInputs) -> DoorOpenResponse:
    """The handler's guard chain, in its order (see the module docstring)."""
    if inputs.paused_by_door:
        return DoorOpenResponse.ALREADY_PAUSED_NOOP
    if inputs.manual_change_protected:
        return DoorOpenResponse.PROTECTED_CHANGE_NOOP
    if inputs.grace_active and not inputs.grace_ignored and not inputs.start_viable:
        return DoorOpenResponse.GRACE_SUPPRESSED
    if not (inputs.start_viable or inputs.start_vetoed):
        return DoorOpenResponse.PAUSE
    if inputs.fan_off_hold_active:
        return DoorOpenResponse.PAUSE_FAN_OFF_HOLD
    if inputs.reactivation_locked_out:
        return DoorOpenResponse.PAUSE_REACTIVATION_LOCKOUT
    if inputs.start_vetoed:
        return DoorOpenResponse.PAUSE_START_VETOED
    return DoorOpenResponse.START_NAT_VENT
