"""Inline unit tests for the pure post-fan setpoint verify core (architecture-reset Step 2).

Direct tests of decide_setpoint_verify() — each traces to the identical
decision logic previously hand-duplicated in _do_verify_after_fan_on() and
_do_verify_after_fan_off().
"""

from __future__ import annotations

from custom_components.climate_advisor.setpoint_verify_decision import (
    SetpointVerifyOutcome,
    decide_setpoint_verify,
)

_BASE = {
    "current_write_seq": 5,
    "verify_write_seq": 5,
    "expected_temp": 72.0,
    "expected_mode": "cool",
    "manual_override_active": False,
    "actual_temp": 72.0,
}


def _inputs(**overrides):
    return {**_BASE, **overrides}


def test_stale_when_write_seq_changed():
    outcome = decide_setpoint_verify(**_inputs(current_write_seq=6))
    assert outcome == SetpointVerifyOutcome.STALE


def test_no_setpoint_when_expected_temp_none():
    outcome = decide_setpoint_verify(**_inputs(expected_temp=None))
    assert outcome == SetpointVerifyOutcome.NO_SETPOINT


def test_no_setpoint_when_expected_mode_none():
    outcome = decide_setpoint_verify(**_inputs(expected_mode=None))
    assert outcome == SetpointVerifyOutcome.NO_SETPOINT


def test_override_active_skips_reassertion():
    outcome = decide_setpoint_verify(**_inputs(manual_override_active=True, actual_temp=80.0))
    assert outcome == SetpointVerifyOutcome.OVERRIDE_ACTIVE


def test_no_reading_when_actual_temp_none():
    outcome = decide_setpoint_verify(**_inputs(actual_temp=None))
    assert outcome == SetpointVerifyOutcome.NO_READING


def test_within_tolerance_no_action():
    outcome = decide_setpoint_verify(**_inputs(actual_temp=72.5))  # 0.5F diff, within 0.6F tolerance
    assert outcome == SetpointVerifyOutcome.WITHIN_TOLERANCE


def test_boundary_exactly_at_tolerance_no_action():
    """Non-strict boundary: abs(diff) > tolerance triggers reassert; == tolerance does not."""
    outcome = decide_setpoint_verify(**_inputs(actual_temp=72.6))  # exactly 0.6F diff
    assert outcome == SetpointVerifyOutcome.WITHIN_TOLERANCE


def test_reassert_when_beyond_tolerance():
    outcome = decide_setpoint_verify(**_inputs(actual_temp=73.0))  # 1.0F diff, beyond 0.6F tolerance
    assert outcome == SetpointVerifyOutcome.REASSERT


def test_stale_check_takes_priority_over_other_conditions():
    """Matches real code's if/return order: staleness is checked first."""
    outcome = decide_setpoint_verify(
        **_inputs(current_write_seq=99, expected_temp=None, manual_override_active=True, actual_temp=None)
    )
    assert outcome == SetpointVerifyOutcome.STALE
