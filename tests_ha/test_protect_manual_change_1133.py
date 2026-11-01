"""Real-HA test for Issue #1133: a window opening after the occupant's heat change must not pause it.

Occupant story: a cold morning. The person raises the heat at the thermostat (a manual change, with
"Manual Change Pause" set to 0 minutes so it has no timer), then opens a window, then closes it again.
Owner rule 1: HVAC stays off while a window is open UNLESS a manual override turns it on -- the manual change
wins. Before the fix Climate Advisor switched the heat OFF over the person's change the moment the window
had been open past the debounce, and on closing the window restored its own setpoint instead of theirs.

Positive control: the same flow with no manual change still pauses for the window and restores on closing.

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
    "initial_indoor_f": 62.0,
    "start": datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
    "outdoor_f": 38.0,  # far too cold for nat-vent: an open window means "pause", never "ventilate"
    "high_f": 44.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
    # manual_grace_seconds=0: no grace period follows the manual change (Manual Change Pause = 0).
    "ca_config": {"sensor_debounce_seconds": 30, "override_confirm_seconds": 60, "manual_grace_seconds": 0},
}
_OCCUPANT_SETPOINT_F = 75.0


async def _occupant_raises_heat(house) -> None:
    await house.advance(minutes=6)  # past the 300 s start-up window that suppresses override detection
    await house.occupant_sets_thermostat(setpoint_f=_OCCUPANT_SETPOINT_F)
    await house.advance(minutes=3)  # past the 60 s override confirmation
    engine = house.coordinator.automation_engine
    assert engine._manual_override_active, "precondition: the manual change was confirmed as an override"
    assert engine._grace_active is False, "precondition: no grace period protects the override"
    assert house.thermostat.mode == "heat"
    assert house.thermostat.setpoint_f == _OCCUPANT_SETPOINT_F


async def test_heat_set_by_the_occupant_is_never_paused_by_a_later_window(make_house) -> None:
    house = await make_house(**_HOUSE)
    engine = house.coordinator.automation_engine
    await _occupant_raises_heat(house)

    await house.open("window")
    await house.advance(minutes=10)  # well past the sensor debounce, and a 5-minute revisit or two

    assert engine._paused_by_door is False, "the occupant's heat is protected: no window pause is recorded"
    assert house.thermostat.mode == "heat", "heat stays on with the window open: the manual change wins"
    assert house.thermostat.setpoint_f == _OCCUPANT_SETPOINT_F

    await house.close_contact("window")
    await house.advance(minutes=10)

    assert engine._paused_by_door is False
    assert house.thermostat.mode == "heat"
    assert house.thermostat.setpoint_f == _OCCUPANT_SETPOINT_F, "closing the window must not replace their setpoint"
    assert engine._manual_override_active, "nothing ended the override: it stays until the occupant or Cancel does"


async def test_positive_control_window_without_a_manual_change_pauses_and_restores(make_house) -> None:
    """Control: no manual change -> the open window pauses HVAC (recorded) and closing it hands heat back."""
    house = await make_house(**_HOUSE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)

    await house.open("window")
    await house.advance(minutes=3)

    assert house.thermostat.mode == "off", "precondition: HVAC was turned off for the open window"
    assert engine._paused_by_door is True

    await house.close_contact("window")
    await house.advance(minutes=3)

    assert engine._paused_by_door is False
    assert house.thermostat.mode == "heat"
