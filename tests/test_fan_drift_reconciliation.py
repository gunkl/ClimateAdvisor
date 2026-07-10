"""Inline unit tests for the pure fan-drift-reconciliation core (architecture-reset Step 2).

Direct tests of decide_fan_drift_reconciliation() — mirrors the
test_nat_vent_gate.py / test_fan_thermostat_decision.py pattern. Each case
traces to a real production test in tests/test_fan_control.py's
TestReconcileFanPhysicalDrift, not an invented boundary.
"""

from __future__ import annotations

from custom_components.climate_advisor.fan_drift_reconciliation import (
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    FanDriftInputs,
    FanDriftOutcome,
    decide_fan_drift_reconciliation,
)

_BASE = {
    "fan_active": True,
    "fan_mode": FAN_MODE_WHOLE_HOUSE,
    "recent_fan_command": False,
    "physical_state_available": True,
    "physical_on": False,
    "tick_count": 0,
}


def _inputs(**overrides) -> FanDriftInputs:
    return FanDriftInputs(**{**_BASE, **overrides})


def test_reset_when_fan_not_active():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(fan_active=False, tick_count=1))
    assert outcome == FanDriftOutcome.RESET
    assert tick == 0


def test_noop_for_hvac_mode_archetype():
    """FAN_MODE_HVAC (not WHF/BOTH) has no separate physical entity — no-op, tick unchanged."""
    outcome, tick = decide_fan_drift_reconciliation(_inputs(fan_mode="hvac_fan", tick_count=1))
    assert outcome == FanDriftOutcome.NOOP
    assert tick == 1


def test_noop_for_both_archetype_reaches_physical_check():
    """FAN_MODE_BOTH is a valid archetype for this check (not excluded like HVAC-only)."""
    outcome, tick = decide_fan_drift_reconciliation(_inputs(fan_mode=FAN_MODE_BOTH, physical_on=True))
    assert outcome == FanDriftOutcome.RESET
    assert tick == 0


def test_reset_on_recent_ca_command_echo():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(recent_fan_command=True, tick_count=1))
    assert outcome == FanDriftOutcome.RESET
    assert tick == 0


def test_noop_when_no_physical_state_callback():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(physical_state_available=False, tick_count=1))
    assert outcome == FanDriftOutcome.NOOP
    assert tick == 1


def test_noop_command_only_mode_physical_on_none():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(physical_on=None, tick_count=1))
    assert outcome == FanDriftOutcome.NOOP
    assert tick == 1


def test_reset_when_physical_state_agrees():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(physical_on=True, tick_count=1))
    assert outcome == FanDriftOutcome.RESET
    assert tick == 0


def test_first_drift_tick_awaits_confirmation():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(physical_on=False, tick_count=0))
    assert outcome == FanDriftOutcome.AWAITING
    assert tick == 1


def test_second_consecutive_drift_tick_corrects():
    outcome, tick = decide_fan_drift_reconciliation(_inputs(physical_on=False, tick_count=1))
    assert outcome == FanDriftOutcome.CORRECT
    assert tick == 0
