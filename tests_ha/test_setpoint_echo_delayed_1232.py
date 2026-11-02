"""Real-HA test for Issue #1232: Climate Advisor's own setpoint, reported back late, is never the occupant.

Occupant story: overnight Climate Advisor nudges the setpoint (the remote-sensor loop, #1194). A cloud thermostat
reports each write 30-120 s later and carries none of Climate Advisor's Context. Before the fix a report past
30 s read as the person touching the dial: a false "manual override" and the automation paused for the manual
grace period (1 h by default), so the house stopped following its plan while everyone slept. A person who REALLY
changes the setpoint must still be caught.

The simulated thermostat answers ``set_temperature`` after ``set_echo_delay()`` seconds (default 0 = instant),
without the caller's Context -- how a cloud thermostat behaves. Run in both fan configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
SIM_DOMAIN = "ca_dev_thermostat_sim"

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 2, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
}
_CA_CONFIG = {"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 3600, "override_confirm_seconds": 60}


async def _quiet_house(make_house, fan):
    house = await make_house(fan=fan, ca_config=dict(_CA_CONFIG), **_HOUSE)
    await house.advance(seconds=420)  # past the start-up window and CA's own start-up commands
    return house


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


async def _ca_nudges(house, setpoint_f: float, echo_delay_s: float) -> None:
    """Climate Advisor writes a setpoint; the thermostat will report it ``echo_delay_s`` later."""
    _sim(house).set_echo_delay(echo_delay_s)
    engine = house.coordinator.automation_engine
    await engine._set_temperature(setpoint_f, reason="test nudge", mode="heat")
    await house._drain()
    _sim(house).set_echo_delay(0)
    assert echo_delay_s == 0 or house.thermostat.setpoint_f != setpoint_f, (
        "precondition: the thermostat has not answered"
    )


def _overrides(house) -> int:
    return house.coordinator._today_record.manual_overrides


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("echo_s", [45, 110])
async def test_late_echo_of_ca_setpoint_is_not_an_override(make_house, fan, echo_s) -> None:
    house = await _quiet_house(make_house, fan)
    engine = house.coordinator.automation_engine
    before = house.thermostat.setpoint_f
    await _ca_nudges(house, before + 2.0, echo_delay_s=echo_s)
    await house.advance(seconds=echo_s + 20)
    assert house.thermostat.setpoint_f == before + 2.0, "precondition: the delayed echo arrived"
    assert _overrides(house) == 0, "CA's own late echo was counted as the occupant"
    assert not engine._manual_override_active and not engine._override_confirm_pending
    await house.advance(minutes=5)
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert not engine._grace_active, "a false override paused automation for the manual grace period"


@pytest.mark.parametrize("fan", _FANS)
async def test_occupant_change_before_the_echo_is_still_an_override(make_house, fan) -> None:
    house = await _quiet_house(make_house, fan)
    engine = house.coordinator.automation_engine
    before = house.thermostat.setpoint_f
    await _ca_nudges(house, before + 2.0, echo_delay_s=110)
    await house.advance(seconds=45)
    await house.occupant_sets_thermostat(setpoint_f=before + 6.0)  # a different value, from the person
    await house.advance(seconds=120)
    assert _overrides(house) >= 1, "the occupant's real change was swallowed as CA's echo"
    assert engine._manual_override_active or engine._override_confirm_pending


@pytest.mark.parametrize("fan", _FANS)
async def test_equal_value_after_the_window_is_the_occupant(make_house, fan) -> None:
    house = await _quiet_house(make_house, fan)
    before = house.thermostat.setpoint_f
    # CA re-asserts the setpoint the thermostat already holds: no state change, so no echo ever consumes the
    # remembered write -- only the 180 s window can end it.
    await _ca_nudges(house, before, echo_delay_s=0)
    await house.advance(seconds=200)  # just past the 180 s memory
    await house.occupant_sets_thermostat(setpoint_f=before + 3.0)
    await house.advance(seconds=30)
    after_first = _overrides(house)
    await house.occupant_sets_thermostat(setpoint_f=before)  # back to the value CA wrote, past the window
    await house.advance(seconds=90)
    # The +3 change alone already counts; the dial-back to CA's value must be counted too (a memory that never
    # expired would swallow it as CA's echo).
    assert _overrides(house) > after_first, "the occupant's change to a value CA wrote long ago was read as CA's echo"
