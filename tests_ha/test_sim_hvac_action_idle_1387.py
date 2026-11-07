"""Real-HA test for Issue #1387: the dev thermostat sim reports idle while it applies no power.

Occupant story (dev/test house): the simulated thermostat showed "heating" for hours while the room just held at
its setpoint, so Climate Advisor's analysis, runtime counters and charts saw heat running that never ran. The sim
must report heating/cooling only for a step that really applies power, like a real thermostat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "closed_loop",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 70.0,
    "outdoor_f": 50.0,
    "high_f": 55.0,
    "low_f": 45.0,
    "initial_hvac_mode": "heat",
}


def _sim(house):
    return house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]


async def _tick(sim, seconds: int = 30) -> None:
    await sim._async_tick(sim._last_update_ts + timedelta(seconds=seconds))


async def test_compressor_holding_at_setpoint_reports_idle_not_heating(make_house) -> None:
    house = await make_house(**_HOUSE)
    sim = _sim(house)
    sim._hvac_mode = type(sim._hvac_mode)("heat")
    sim._target_temp = 68.0
    sim._current_temp = 68.0  # holding at the setpoint
    sim._compressor_on = True  # the state machine stays on through the deadband (it stops at target + deadband)
    sim._last_on_ts = None
    await _tick(sim)
    assert sim._compressor_on is True, "the compressor decision itself is unchanged"
    assert sim.hvac_action == "idle"


async def test_heating_below_setpoint_still_reports_heating(make_house) -> None:
    house = await make_house(**_HOUSE)
    sim = _sim(house)
    sim._hvac_mode = type(sim._hvac_mode)("heat")
    sim._target_temp = 68.0
    sim._current_temp = 60.0  # far below: the compressor starts and applies power
    sim._compressor_on = False
    sim._last_off_ts = None
    await _tick(sim)
    assert sim._compressor_on is True
    assert sim.hvac_action == "heating"
