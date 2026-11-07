"""Real-HA test for Issue #1397: a thermostat that reports Climate Advisor's setpoint 0.1 C low is not the occupant.

Occupant story (a Celsius HomeKit zone, heating): Climate Advisor writes 18.0 C; the thermostat reports 18.0, then
about a minute later 17.9 -- nobody touched it. Before the fix Climate Advisor read the 17.9 as the occupant turning
the dial: a manual override, the zone paused for the manual grace, a rewrite of 18.0 when it ended and the next round
of the same, all evening. After the fix a report within 0.5 F (0.28 C) of the setpoint Climate Advisor wrote, once the
thermostat has shown it, is the thermostat holding that setpoint. A person who really changes it is still caught.

The simulated thermostat reports each ``set_temperature`` after ``set_echo_delay()`` seconds and then, with
``set_setpoint_drift()``, the same setpoint 0.1 low a little later, with none of Climate Advisor's Context -- how the
reporter's thermostat behaves. Home Assistant runs metric, Climate Advisor in Celsius. Both fan configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
SIM_DOMAIN = "ca_dev_thermostat_sim"
_18C_F = 64.4  # 18.0 C in Climate Advisor's internal F

_HOUSE = {
    "units": "metric",
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 2, 0, tzinfo=UTC),
    "initial_indoor_f": 64.0,
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
}
_CA_CONFIG = {"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 1800, "override_confirm_seconds": 600}


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


async def _drifting_house(make_house, fan):
    house = await make_house(fan=fan, ca_config=dict(_CA_CONFIG), **_HOUSE)
    await house.advance(seconds=420)  # past the start-up window and CA's own start-up commands
    _sim(house).set_setpoint_drift(-0.1, delay_s=31)
    return house


async def _ca_writes_18c(house) -> None:
    """Climate Advisor writes 18.0 C; the thermostat reports it 36 s later, then 17.9 C 31 s after that."""
    _sim(house).set_echo_delay(36)
    await house.coordinator.automation_engine._set_temperature(_18C_F, reason="test write", mode="heat")
    await house._drain()
    _sim(house).set_echo_delay(0)


def _overrides(house) -> int:
    return house.coordinator._today_record.manual_overrides


def _reported_c(house) -> float:
    return float(house.hass.states.get(_sim(house).entity_id).attributes["temperature"])


@pytest.mark.parametrize("fan", _FANS)
async def test_the_thermostat_reporting_cas_setpoint_0_1_c_low_is_not_an_override(make_house, fan) -> None:
    house = await _drifting_house(make_house, fan)
    engine = house.coordinator.automation_engine
    await _ca_writes_18c(house)
    await house.advance(seconds=40)
    assert _reported_c(house) == pytest.approx(18.0), "precondition: the thermostat reported CA's 18.0 C"
    await house.advance(seconds=40)
    assert _reported_c(house) == pytest.approx(17.9), "precondition: the thermostat then drifted to 17.9 C"

    await house.advance(minutes=45)  # past the 10-minute confirmation and a whole 30-minute grace
    assert _overrides(house) == 0, "#1397: the thermostat's own 0.1 C drift was counted as the occupant"
    assert not engine._manual_override_active and not engine._override_confirm_pending
    assert not engine._grace_active, "a false override paused the zone for the manual grace period"


@pytest.mark.parametrize("fan", _FANS)
async def test_the_occupant_turning_the_dial_one_step_is_still_an_override(make_house, fan) -> None:
    """Control: a 0.5 C step (0.9 F) is larger than the tolerance."""
    house = await _drifting_house(make_house, fan)
    engine = house.coordinator.automation_engine
    await _ca_writes_18c(house)
    await house.advance(seconds=120)
    assert _reported_c(house) == pytest.approx(17.9), "precondition: CA's setpoint, drifted"
    await house.occupant_sets_thermostat(setpoint_f=65.3)  # 18.5 C
    await house.advance(seconds=60)
    assert _overrides(house) >= 1, "the occupant's real change was swallowed as the thermostat's drift"
    assert engine._manual_override_active or engine._override_confirm_pending
