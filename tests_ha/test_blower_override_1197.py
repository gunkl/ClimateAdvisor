"""Real-HA tests for Issue #1197 decisions 1 and 2 (owner decisions 2026-11-01): the thermostat's blower.

Occupant stories:

* Whole-house-fan house. The person presses the thermostat's own fan button (On, then back to Auto). That blower is
  not Climate Advisor's fan: nothing is recorded, no hold-off period starts, and Climate Advisor sends no command.
* hvac_fan house, hot afternoon, cooling running. The person switches the thermostat blower On for circulation and a
  window then stays open. Owner rule 3: after the sensor debounce the cooling goes off (a blower press turns no
  heating or cooling on), and the person's blower keeps running (#1180). Before the debounce it does nothing.

Positive control: with the blower pressed but the window never opened, the cooling keeps running.

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOT = {
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 82.0,
    "outdoor_f": 96.0,
    "high_f": 98.0,
    "low_f": 78.0,
    "initial_hvac_mode": "cool",
}
_DEBOUNCE_S = 120


def _ca(calls):
    return [c for c in calls if c.origin.startswith("ca:") and c.domain in ("climate", "fan", "switch")]


async def test_whf_house_ignores_the_thermostat_blower(make_house) -> None:
    house = await make_house(fan="whf", startup="settled", ca_config={"fan_min_runtime_per_hour": 0}, **_HOT)
    await house.advance(minutes=6)  # past the start-up window that suppresses override detection
    engine = house.coordinator.automation_engine
    calls_before = len(house.calls)

    await house.occupant_sets_fan_mode("on")
    await house.advance(minutes=2)
    assert engine._fan_override_active is False, "the thermostat blower is not Climate Advisor's fan"
    assert engine._grace_active is False

    await house.occupant_sets_fan_mode("auto")
    await house.advance(minutes=2)
    assert engine._fan_override_active is False
    assert engine._grace_active is False
    assert engine._last_grace_trigger not in ("fan_off", "fan_manual_override")

    fan_commands = [c for c in _ca(house.calls[calls_before:]) if c.service in ("set_fan_mode", "turn_on", "turn_off")]
    assert fan_commands == [], f"Climate Advisor commanded a fan over the occupant's blower press: {fan_commands}"


async def _hvac_fan_house_with_blower_pressed(make_house):
    house = await make_house(
        fan="hvac_fan",
        contacts=["window"],
        startup="settled",
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": _DEBOUNCE_S,
            "manual_grace_seconds": 1800,
        },
        **_HOT,
    )
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    await house.occupant_sets_fan_mode("on")
    await house.advance(minutes=1)
    assert engine._fan_override_active and engine._fan_override_is_hvac_fan is True, "precondition: blower override"
    assert engine._grace_active, "precondition: the blower press started a grace period"
    assert house.thermostat.mode == "cool", "precondition: cooling is running"
    return house, engine


async def test_blower_grace_does_not_block_the_window_pause(make_house) -> None:
    house, engine = await _hvac_fan_house_with_blower_pressed(make_house)

    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S - 40)  # still inside the sensor debounce
    assert house.thermostat.mode == "cool", "a window open for less than the debounce never pauses the cooling"
    assert engine._paused_by_door is False

    await house.advance(minutes=2)  # past the debounce
    assert engine._paused_by_door is True
    assert house.thermostat.mode == "off", "the cooling goes off for the open window"
    assert house.thermostat.fan_mode == "on", "the occupant's blower keeps running (#1180)"
    assert engine._fan_override_active is True


async def test_positive_control_blower_without_a_window_keeps_cooling(make_house) -> None:
    house, engine = await _hvac_fan_house_with_blower_pressed(make_house)
    await house.advance(minutes=5)
    assert engine._paused_by_door is False
    assert house.thermostat.mode == "cool"
