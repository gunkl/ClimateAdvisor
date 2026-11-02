"""Real-HA test for Issue #1234: Climate Advisor learns the thermostat's OWN heat hysteresis.

Occupant story: the home's thermostat starts heat 1.4 F below its setpoint and stops 0.9 F below it (an asymmetric
deadband, like the owner's real thermostat). The learned heat swing must come out near 1.4 F, not the 0.5-0.7 F the old
"half the temperature rise per run" rule produced, so the dashboard and the AI explanations talk about the thermostat's
real behaviour (and the later remote-sensor loop holds the room the way the occupant set their own thermostat).

The REAL coordinator thermostat listener runs against the dev thermostat sim configured with the asymmetric deadband.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    "sim_config": {
        "deadband_heat_f": 1.4,
        "off_offset_heat_f": -0.9,
        "min_run_seconds": 120,
        "min_off_seconds": 120,
    },
}


_FINE_SENSOR = "sensor.house_indoor_fine"


async def _run(house, hours: int, *, mirror_fine: bool) -> dict:
    sim = house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]
    for _ in range(hours * 120):  # 30 s slices
        await house.advance(seconds=30)
        if mirror_fine:  # a dedicated 0.01 F sensor reading the same room as the thermostat
            house.hass.states.async_set(_FINE_SENSOR, f"{sim._current_temp:.2f}", {"unit_of_measurement": "\u00b0F"})
    return house.coordinator.learning.get_thermal_model()


async def test_fine_sensor_learns_the_real_start_point(make_house) -> None:
    """The thermostat starts heat 1.4 F below setpoint: a fine sensor learns ~1.4, stop point kept apart (~0.9)."""
    house = await make_house(
        **{
            **_HOUSE,
            "ca_config": {"indoor_temp_source": "sensor", "indoor_temp_entity": _FINE_SENSOR},
            "before_ca_setup": _seed_fine_sensor,
        }
    )
    model = await _run(house, 8, mirror_fine=True)
    assert model["observation_count_swing_heat"] >= 5, model
    assert 1.2 <= model["swing_heat_f"] <= 1.7, model
    assert model["swing_heat_resolution_f"] == 0.0
    off = model["swing_heat_off_point_f"]
    assert off is not None and 0.5 <= off <= 1.3, f"the stop point stays a separate field near 0.9: {off}"


async def test_whole_degree_thermostat_learns_an_honest_interval(make_house) -> None:
    """On the thermostat's own whole-degree reading (HA rounds it) the start is setpoint-1, +/-0.5, which holds 1.4."""
    house = await make_house(**_HOUSE)
    model = await _run(house, 8, mirror_fine=False)
    assert model["observation_count_swing_heat"] >= 5, model
    assert model["swing_heat_f"] == pytest.approx(1.0)
    assert model["swing_heat_resolution_f"] == pytest.approx(1.0)
    assert model["swing_heat_f"] - 0.5 <= 1.4 <= model["swing_heat_f"] + 0.5


async def _seed_fine_sensor(house) -> None:
    house.hass.states.async_set(_FINE_SENSOR, "70.00", {"unit_of_measurement": "\u00b0F"})
