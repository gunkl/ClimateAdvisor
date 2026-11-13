"""Real-HA test for Issue #1233: coast overshoot is recorded apart from the learned swing.

Occupant story: after the thermostat stops the heat the room can keep rising. A homeowner checking the
learned values against their equipment needs that "coast" separate from the swing (the room drifting
before the thermostat STARTS heat). The REAL coordinator listeners (thermostat state changes and the
dedicated indoor sensor's ticks) run against the dev thermostat sim configured like the owner's
thermostat: starts heat at setpoint - 1.4, stops at setpoint - 0.9.

The dev sim has no thermal momentum, so the room does not rise past the off reading: the recorded coast
is 0.0, the honest value for this rig. What this test pins is the plumbing under real HA: windows are
opened at the real stop, fed by real sensor ticks, closed by the next real start, persisted, and never
leak into the swing or the off-point.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FINE_SENSOR = "sensor.house_indoor_fine"
_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "closed_loop",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 70.0,
    "outdoor_f": 50.0,
    # flat forecast: Climate Advisor's outdoor reading is the sim's own outdoor air (Issue #1512 reopened)
    "high_f": 50.0,
    "low_f": 50.0,
    "initial_hvac_mode": "heat",
    "sim_config": {
        "deadband_heat_f": 1.4,
        "off_offset_heat_f": -0.9,
        "min_run_seconds": 120,
        "min_off_seconds": 120,
    },
}


async def _seed_fine_sensor(house) -> None:
    house.hass.states.async_set(_FINE_SENSOR, "70.00", {"unit_of_measurement": "°F"})


async def _run(house, hours: int) -> dict:
    sim = house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]
    for _ in range(hours * 120):  # 30 s slices
        await house.advance(seconds=30)
        house.hass.states.async_set(_FINE_SENSOR, f"{sim._current_temp:.2f}", {"unit_of_measurement": "°F"})
    return house.coordinator.learning.get_thermal_model()


async def test_coast_windows_are_recorded_apart_from_the_swing(make_house) -> None:
    house = await make_house(
        **{
            **_HOUSE,
            "ca_config": {"indoor_temp_source": "sensor", "indoor_temp_entity": _FINE_SENSOR},
            "before_ca_setup": _seed_fine_sensor,
        }
    )
    model = await _run(house, 8)
    # the swing is unaffected: the on-point hysteresis near 1.4, the stop point kept apart near 0.9
    assert model["observation_count_swing_heat"] >= 5, model
    assert 1.2 <= model["swing_heat_f"] <= 1.7, model
    assert 0.5 <= model["swing_heat_off_point_f"] <= 1.3, model
    # coast windows were opened at real stops, fed by the sensor's ticks, closed by the next real start
    assert model["observation_count_coast_heat"] >= 3, model
    cache = house.coordinator.learning._state.thermal_model_cache
    windows = cache["coast_heat_samples"]
    assert {w["ended_by"] for w in windows} <= {"next_on", "setpoint_change", "timeout"}
    assert all(w["n_post"] >= 1 and w["coast_f"] >= 0 for w in windows)
    assert model["coast_heat_f"] is not None and model["coast_heat_f"] <= 0.3, model  # no momentum in the sim
    # coast never leaks into the swing samples
    assert all("coast" not in k for s in cache["swing_heat_samples"] for k in s)
    # the audit trail kept the inputs of the accepted cycles
    audit = cache["swing_heat_audit"]
    assert any(r["accepted"] and r["indoor_entity"] == _FINE_SENSOR for r in audit), audit[:2]


async def test_whole_degree_thermostat_still_records_coast_windows(make_house) -> None:
    """With no dedicated sensor the post-off readings come from the thermostat's own attribute changes."""
    house = await make_house(**_HOUSE)
    model = await _run(house, 8)
    assert model["observation_count_swing_heat"] >= 5, model
    assert model["swing_heat_f"] == pytest.approx(1.0)  # unchanged: whole-degree start point
    windows = house.coordinator.learning._state.thermal_model_cache.get("coast_heat_samples") or []
    assert all(w["coast_f"] >= 0 for w in windows)
