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
    """Pure decision of the post-fan setpoint verify (``AutomationEngine._arm_post_fan_setpoint_verify``, once
    the two identical fan-on/fan-off closures). The shell owns actually calling _set_temperature()
    when the outcome is REASSERT, and first waits out the echo window of a write the thermostat
    has not answered yet (Issue #1272)."""
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


class ModeAlignmentOutcome(Enum):
    """Outcomes of the read-back "did the thermostat take the requested mode?" check (Issue #1049).

    A sibling of ``SetpointVerifyOutcome`` -- deliberately NOT extra members of it
    (``tools/sim_harness/full_surface_integration.py`` maps every member of that enum).
    The ``EXCLUDED_*`` members say *why* a check was not evaluated so the write record can
    carry the reason without the shell re-deriving the exclusion order.
    """

    STALE = "stale"  # a newer write superseded this check
    NOT_APPLICABLE = "not_applicable"  # no mode was ever requested
    EXCLUDED_DRY_RUN = "excluded_dry_run"
    EXCLUDED_OVERRIDE = "excluded_manual_override"  # user owns the thermostat (override / confirm pending)
    EXCLUDED_WHF = "excluded_whf_owned"
    EXCLUDED_PAUSED = "excluded_paused"  # off-by-design door/window pause, requested mode is not "off"
    EXCLUDED_BUSY = "excluded_command_in_progress"  # a newer command is still in flight
    UNREADABLE = "unreadable"  # thermostat unavailable/unknown/None -- cannot judge
    ALIGNED = "aligned"
    MISALIGNED = "misaligned"


_UNREADABLE_MODE_STATES = frozenset({"", "unavailable", "unknown"})


def decide_mode_alignment(
    *,
    current_write_seq: int,
    check_write_seq: int,
    requested_mode: str | None,
    live_state: object,
    manual_override_active: bool,
    override_confirm_pending: bool,
    paused_by_door: bool,
    whf_owns_hvac: bool,
    dry_run: bool,
    command_in_progress: bool,
) -> ModeAlignmentOutcome:
    """Pure read-back decision: is the thermostat in the mode Climate Advisor last requested?

    Sensi/ecobee ignore the ``hvac_mode`` kwarg of ``climate.set_temperature`` and Sensi reports
    ``temperature: null`` while Off, so the setpoint verify alone cannot see a thermostat that
    never switched on (Issue #1048). Order matters: staleness first, then "nothing requested",
    then every reason the user/another subsystem owns the thermostat, then readability.
    A door/window pause only excludes non-"off" requests -- an "off" request that the thermostat
    ignores is exactly the Off-direction miss this check exists to catch.
    """
    if current_write_seq != check_write_seq:
        return ModeAlignmentOutcome.STALE
    if not requested_mode:
        return ModeAlignmentOutcome.NOT_APPLICABLE
    if dry_run:
        return ModeAlignmentOutcome.EXCLUDED_DRY_RUN
    if manual_override_active or override_confirm_pending:
        return ModeAlignmentOutcome.EXCLUDED_OVERRIDE
    if whf_owns_hvac:
        return ModeAlignmentOutcome.EXCLUDED_WHF
    if paused_by_door and requested_mode != "off":
        return ModeAlignmentOutcome.EXCLUDED_PAUSED
    if command_in_progress:
        return ModeAlignmentOutcome.EXCLUDED_BUSY
    if not isinstance(live_state, str) or live_state in _UNREADABLE_MODE_STATES:
        return ModeAlignmentOutcome.UNREADABLE
    if live_state == requested_mode:
        return ModeAlignmentOutcome.ALIGNED
    return ModeAlignmentOutcome.MISALIGNED
