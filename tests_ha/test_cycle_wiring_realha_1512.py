"""Real-HA test for Issues #1512 / #1516: the pooled cycle model learns from real heating cycles.

Occupant story: the furnace heats the room in runs, the room keeps drifting after each stop (the coast), and Home
Assistant restarts now and then (a restart is a gap, never the furnace stopping). The REAL coordinator listeners run
against the dev thermostat sim: every run is recorded in seconds from the dedicated sensor, stored, fitted by the
production ``cycle_model`` in the executor, and (v3 primary) becomes the heating rate and its grade. A restart in the
middle of a run keeps the clean part before it and never produces an off edge.

The dev sim has no thermal momentum (see tests_ha/test_coast_overshoot_1233.py), so the measured coast is ~0: what is
pinned here is the plumbing, the gap handling and the applied result, not a coast magnitude. The sim was NOT extended.
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


async def _seed_fine_sensor(house) -> None:
    house.hass.states.async_set(_FINE_SENSOR, "70.00", {"unit_of_measurement": "°F"})


async def _slices(house, n: int) -> None:
    sim = house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]
    for _ in range(n):  # 30 s slices
        await house.advance(seconds=30)
        house.hass.states.async_set(_FINE_SENSOR, f"{sim._current_temp:.2f}", {"unit_of_measurement": "°F"})


async def _until_heating(house, limit: int = 600) -> None:
    for _ in range(limit):
        await _slices(house, 1)
        if house.thermostat.action == "heating":
            return
    raise AssertionError("the sim never started heating")


async def test_real_heating_cycles_are_fitted_applied_and_survive_a_restart_gap(make_house) -> None:
    house = await make_house(
        **{
            **_HOUSE,
            "ca_config": {
                "indoor_temp_source": "sensor",
                "indoor_temp_entity": _FINE_SENSOR,
                "primary_engine": "v3",
            },
            "before_ca_setup": _seed_fine_sensor,
        }
    )
    await _slices(house, 8 * 120)  # eight hours of runs
    coord = house.coordinator
    stored = coord._cycle_store.cycles("heat")
    assert len(stored) >= 4, len(stored)
    natural = [c for c in stored if c["cut"] is None and c["t_off"] is not None]
    assert natural, "no cycle closed naturally (off edge + coast window)"
    assert all(c["entity"] == _FINE_SENSOR for c in natural)
    assert any(s[0] > c["t_off"] for c in natural for s in c["samples"]), "the post-off coast was recorded"

    cache = coord.learning._state.thermal_model_cache
    fit = cache["cycle_fit_heat"]
    assert fit["ne"] >= 3 and fit["grade"] in ("low", "medium", "high"), fit
    assert fit["applied"] is True and cache["k_active_heat"] == pytest.approx(fit["q"], abs=1e-3)
    assert 0.5 <= fit["q"] <= 15.0 and abs(fit["coast_area_f"]) < 1.5, fit  # the sim has no momentum
    model = coord.learning.get_thermal_model()
    assert model["confidence"] == fit["grade"] and model["heating_rate_f_per_hour"] == round(fit["q"], 2)
    assert coord.learning.get_thermal_model()["k_passive"] == cache["k_passive"]  # untouched by the cycle fit
    assert "cycle_fit_heat" in coord._build_learning_health()
    assert any(r["source"] == "cycle" for r in coord.decision_rates_summary())
    before = len(stored)

    # a restart in the middle of a run: no off edge, the cycle keeps its clean part with cut="restart"
    await _until_heating(house)
    await _slices(house, 6)
    await house.restart(down_for_seconds=90)
    coord = house.coordinator
    after = coord._cycle_store.cycles("heat")
    assert len(after) >= before + 1, (before, len(after))
    cut = [c for c in after if c["cut"] == "restart"]
    assert cut and cut[-1]["t_off"] is None, "the restart must not look like the furnace stopping"
    # the run resumed after the gap is recorded as its own cycle, its first reading flagged gap_before
    await _slices(house, 60)
    resumed = [c for c in coord._cycle_store.cycles("heat") if c["samples"] and c["samples"][0][2] & 16]
    assert resumed or house.thermostat.action != "heating"
