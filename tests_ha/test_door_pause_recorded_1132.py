"""Real-HA test for Issue #1132: a window opened during a manual override must still pause (and restore) HVAC.

Occupant story: it is warm, the person has just turned the cooling down at the thermostat (a manual change,
with the hold-off after a manual change set to zero), then opens a window. Climate Advisor turns the cooling
off for the open window. Before the fix it never RECORDED that pause, so closing the window restored nothing
and the house stayed uncooled until the person touched the thermostat again.

Positive control: the same flow with no manual change never leaves HVAC off without a pause or nat-vent session.

Prove the test is not vacuous with ``scripts/revert_proof.py`` (fails on the pre-fix tree, passes on HEAD).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "initial_indoor_f": 82.0,
    # 09:00, outdoors 74F (cooler than indoors and under the 77F nat-vent ceiling), so the plain nat-vent entry
    # gate passes -- but the hourly forecast climbs to 84F by 11:00, inside the 2 h look-ahead, so the
    # forecast-peak guard rules nat-vent out and the window means "pause". The FSM's simplified gate cannot see
    # that guard (the #1132 defect).
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "outdoor_f": 73.5,
    "high_f": 95.0,
    "low_f": 52.0,
    "initial_hvac_mode": "cool",
    # manual_grace_seconds=0: no grace period follows the manual change (Manual Change Pause = 0).
    "ca_config": {"sensor_debounce_seconds": 30, "override_confirm_seconds": 60, "manual_grace_seconds": 0},
}


async def _open_window(house, *, manual_change: bool) -> None:
    await house.advance(minutes=6)  # past the 300 s start-up window that suppresses override detection
    if manual_change:
        await house.occupant_sets_thermostat(setpoint_f=72.0)
        await house.advance(minutes=3)  # past the 60 s override confirmation
        engine = house.coordinator.automation_engine
        assert engine._manual_override_active, "precondition: the manual change was confirmed as an override"
        assert engine._grace_active is False, "precondition: no grace period protects the override"
    await house.open("window")
    await house.advance(minutes=3)  # past the sensor debounce


async def test_window_during_manual_override_records_pause_and_close_restores(make_house) -> None:
    house = await make_house(**_HOUSE)
    engine = house.coordinator.automation_engine
    await _open_window(house, manual_change=True)

    assert house.thermostat.mode == "off", "precondition: HVAC was turned off for the open window"
    assert engine._paused_by_door is True, "HVAC was turned off for the window, so the pause must be recorded"

    await house.close_contact("window")
    await house.advance(minutes=3)

    assert engine._paused_by_door is False
    assert house.thermostat.mode != "off", "closing the window must hand the occupant their cooling back"


async def test_positive_control_window_without_manual_change_never_leaves_hvac_off_unexplained(make_house) -> None:
    """Control: with no manual change HVAC turned off for the window is always explained by a recorded pause or
    by a running nat-vent session, and closing the window then leaves the house with cooling or the fan."""
    house = await make_house(**_HOUSE)
    engine = house.coordinator.automation_engine
    await _open_window(house, manual_change=False)

    if house.thermostat.mode == "off":
        assert engine._paused_by_door or engine._natural_vent_active, "HVAC off with no pause and no nat-vent session"

    await house.close_contact("window")
    await house.advance(minutes=6)

    assert engine._paused_by_door is False
    assert house.thermostat.mode != "off" or engine._natural_vent_active
