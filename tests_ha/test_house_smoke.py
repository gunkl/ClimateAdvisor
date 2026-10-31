"""The house builds, runs and closes (Step 2 Increment 2 smoke)."""

from __future__ import annotations

import logging

import pytest

from tests_ha.house import CLIMATE_ENTITY

pytestmark = pytest.mark.house


async def test_closed_loop_house_is_taken_over_by_climate_advisor(make_house, caplog) -> None:
    """A warm house with a hot day ahead: CA commands the thermostat and the sim reports a real action."""
    house = await make_house(units="us", profile="closed_loop", initial_indoor_f=80.0, outdoor_f=88.0, high_f=92.0)
    await house.advance(minutes=20)
    assert any(c.origin.startswith("ca:") for c in house.calls), house.calls
    assert not [r for r in caplog.records if "Setpoint validation FAILED" in r.getMessage()]


async def test_scripted_profile_holds_indoor_fixed(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=78.0)
    house.set_indoor(78.0)
    await house.advance(minutes=30)
    assert house.thermostat.indoor_f == pytest.approx(78.0, abs=0.01)


async def test_metric_house_commands_are_accepted(make_house, caplog) -> None:
    """Celsius zone on a metric HA: CA's setpoints are in range for HA (finding F4 pair set together)."""
    house = await make_house(units="metric", profile="scripted", initial_indoor_f=82.0, outdoor_f=90.0, high_f=95.0)
    house.set_indoor(82.0)
    await house.advance(minutes=30)
    assert house.state(CLIMATE_ENTITY).attributes["current_temperature"] == pytest.approx(27.8, abs=0.1)
    assert not [r for r in caplog.records if "Setpoint validation FAILED" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and "Provided temperature" in r.getMessage()]
