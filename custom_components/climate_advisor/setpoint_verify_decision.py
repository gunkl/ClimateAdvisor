"""Pure decision core for the post-fan setpoint verify check (architecture-reset Step 2).

`_activate_fan()` and `_deactivate_fan()` each scheduled a 30-second post-fan
setpoint-verify callback with byte-for-byte IDENTICAL decision logic (found
during the #429 direction-gate consolidation sweep — this isn't a direction
gate, but the exact same copy-paste-drift risk the sweep was looking for).
Both re-assert the last commanded setpoint if the thermostat (e.g. an Ecobee
reverting to its own comfort program) doesn't match it within tolerance.
"""

from __future__ import annotations

from enum import Enum

_TOLERANCE_F = 0.6  # same tolerance as _check_single_setpoint_accepted()


class SetpointVerifyOutcome(Enum):
    """The outcomes the post-fan setpoint verify decision can produce."""

    STALE = "stale"  # a newer command superseded this verify — skip
    NO_SETPOINT = "no_setpoint"  # no active setpoint captured at schedule time
    OVERRIDE_ACTIVE = "override_active"  # genuine confirmed override — don't fight it
    NO_READING = "no_reading"  # no current thermostat state/temperature attribute
    WITHIN_TOLERANCE = "within_tolerance"  # matches expected setpoint — no action
    REASSERT = "reassert"  # mismatch beyond tolerance — re-assert the setpoint


def decide_setpoint_verify(
    *,
    current_write_seq: int,
    verify_write_seq: int,
    expected_temp: float | None,
    expected_mode: str | None,
    manual_override_active: bool,
    actual_temp: float | None,
) -> SetpointVerifyOutcome:
    """Pure reimplementation of _do_verify_after_fan_on()/_do_verify_after_fan_off()'s
    (identical) decision logic. The shell owns actually calling _set_temperature()
    when the outcome is REASSERT."""
    if current_write_seq != verify_write_seq:
        return SetpointVerifyOutcome.STALE
    if expected_temp is None or expected_mode is None:
        return SetpointVerifyOutcome.NO_SETPOINT
    if manual_override_active:
        return SetpointVerifyOutcome.OVERRIDE_ACTIVE
    if actual_temp is None:
        return SetpointVerifyOutcome.NO_READING
    if abs(actual_temp - expected_temp) > _TOLERANCE_F:
        return SetpointVerifyOutcome.REASSERT
    return SetpointVerifyOutcome.WITHIN_TOLERANCE
