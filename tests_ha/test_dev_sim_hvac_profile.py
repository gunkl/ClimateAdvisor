"""Real-HA smoke test for the dev thermostat sim's HVAC profiles (owner request 2026-11-12).

Developer story (no occupant impact: the sim is dev tooling, but what it teaches Climate Advisor is what the owner's
simulated zones show): those zones could never teach the cycle model a heating rate. They reported whole degrees,
each run moved the room ~0.6 F, and every zone had the same small system. A profile now sets the rate (presets
undersized 2 / extreme weather 5 / normal 12 / oversized 18 F/h), the reported precision, a companion sensor at tenths
on the thermostat's device, sensor lag, and the blower overrun reported as ``hvac_action: fan`` after a run.

The real sim entity runs its own tick on the real HA event loop; these read the states Climate Advisor would read.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest
from homeassistant.helpers import entity_registry as er

from tests_ha.house.sim_profile import CLIMATE_ENTITY, COMPANION_ENTITY, SimProfile, sim_entity

pytestmark = pytest.mark.house

WINTER = {
    "contacts": [],
    "startup": "settled",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 70.0,
    "outdoor_f": 50.0,
    "high_f": 55.0,
    "low_f": 45.0,
    "initial_hvac_mode": "heat",
    "sim_config": {"min_run_seconds": 120, "min_off_seconds": 120},
}


def _collapse(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(s)
    return out


async def test_profile_overrun_and_companion_tenths(make_sim) -> None:
    """Normal furnace, 2-min blower overrun, companion sensor: heating -> fan -> idle, and the companion has tenths."""
    profile = SimProfile(heat_rate="normal", companion_sensor=True, blower_overrun_heat_min=2.0)
    house = await make_sim(profile=profile, **WINTER)
    sim = sim_entity(house)
    climate = house.hass.states.get(CLIMATE_ENTITY)
    assert climate.attributes["heat_rate_f_per_h"] == 12.0 and climate.attributes["heat_rate_preset"] == "normal"

    # the companion sits on the climate entity's device, where Climate Advisor looks for it (Issue #1230)
    reg = er.async_get(house.hass)
    climate_reg, companion_reg = reg.async_get(CLIMATE_ENTITY), reg.async_get(COMPANION_ENTITY)
    assert companion_reg is not None and climate_reg is not None
    assert climate_reg.device_id is not None and companion_reg.device_id == climate_reg.device_id
    assert companion_reg.original_device_class == "temperature"

    actions: list[str] = []
    fractional: set[float] = set()
    for _ in range(2 * 120):  # 2 h of 30 s ticks
        await house.advance(seconds=30)
        actions.append(house.hass.states.get(CLIMATE_ENTITY).attributes["hvac_action"])
        companion = house.hass.states.get(COMPANION_ENTITY)
        assert companion.attributes["unit_of_measurement"] == "°F"
        value = float(companion.state)
        assert value == round(sim.reported_temperature, 1), (value, sim.reported_temperature)
        if value != round(value):
            fractional.add(value)

    seq = _collapse(actions)
    pattern = ["heating", "fan", "idle"]
    assert any(seq[i : i + 3] == pattern for i in range(len(seq) - 2)), seq
    # every overrun lasts the configured 2 min (4 ticks); a run that CA's own setpoint change cut short may differ
    fan_runs = [len(list(g)) for k, g in itertools.groupby(actions) if k == "fan"]
    assert fan_runs and 4 in fan_runs, fan_runs
    assert len(fractional) >= 5, sorted(fractional)  # tenths, not whole degrees
    # the climate entity itself stays at Home Assistant's default (whole degrees on a Fahrenheit system)
    current = house.hass.states.get(CLIMATE_ENTITY).attributes["current_temperature"]
    assert current == round(current), current


async def test_default_profile_is_unchanged(make_sim) -> None:
    """A default profile: no companion, no fan phase, the entry's own 6 F/h, whole degrees."""
    house = await make_sim(profile=SimProfile(), **WINTER)
    climate = house.hass.states.get(CLIMATE_ENTITY)
    assert climate.attributes["heat_rate_f_per_h"] == 6.0 and climate.attributes["heat_rate_preset"] == "custom"
    assert climate.attributes["sensor_lag_min"] == 0.0 and climate.attributes["blower_phase"] == "off"
    assert house.hass.states.get(COMPANION_ENTITY) is None
    assert er.async_get(house.hass).async_get(CLIMATE_ENTITY).device_id is None  # no device without a companion
    actions = []
    for _ in range(120):
        await house.advance(seconds=30)
        actions.append(house.hass.states.get(CLIMATE_ENTITY).attributes["hvac_action"])
    assert "fan" not in actions and "heating" in actions, _collapse(actions)


async def test_runtime_controls_and_lag(make_sim) -> None:
    """The rate number and preset select follow each other and persist; precision and lag apply at runtime."""
    house = await make_sim(profile=SimProfile(heat_rate=3.0, companion_sensor=True), **WINTER)
    hass, sim = house.hass, sim_entity(house)
    rate_number, preset_select = "number.house_thermostat_heating_rate", "select.house_thermostat_heating_rate_preset"
    assert float(hass.states.get(rate_number).state) == 3.0 and hass.states.get(preset_select).state == "custom"

    await hass.services.async_call("number", "set_value", {"entity_id": rate_number, "value": 18}, blocking=True)
    assert hass.states.get(preset_select).state == "oversized"
    assert house.sim_entry.data["k_active_heat"] == 18.0 and house.sim_entry.data["heat_rate_preset"] == "oversized"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": preset_select, "option": "undersized"}, blocking=True
    )
    assert float(hass.states.get(rate_number).state) == 2.0
    assert hass.states.get(CLIMATE_ENTITY).attributes["heat_rate_f_per_h"] == 2.0

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.house_thermostat_reported_precision", "option": "tenth"},
        blocking=True,
    )
    house.set_indoor(68.37)
    assert hass.states.get(CLIMATE_ENTITY).attributes["current_temperature"] == 68.4

    # 10-min lag: after a jump of the air, the reading (climate and companion) follows it, not at once
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.house_thermostat_sensor_lag", "value": 10}, blocking=True
    )
    sim._current_temp = 75.0  # the air jumps; the sensor has not seen it yet
    await house.advance(minutes=10)
    reading = float(hass.states.get(COMPANION_ENTITY).state)
    air = sim._current_temp
    # one time constant: about 63 % of the way from 68.4 to the (decaying) air
    assert 68.4 + 0.5 * (air - 68.4) < reading < air - 0.5, (reading, air)
    assert hass.states.get(CLIMATE_ENTITY).attributes["air_temperature"] == round(air, 3)


async def test_idle_gap_before_the_overrun(make_sim) -> None:
    """``idle_before_overrun_s`` 1 (the owner's Ecobee): heating -> idle, then fan 1 s later, then idle."""
    profile = SimProfile(heat_rate="normal", blower_overrun_heat_min=2.0, idle_before_overrun_s=1.0)
    house = await make_sim(profile=profile, **WINTER)
    seen: list[str] = []

    def _on_change(event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.entity_id == CLIMATE_ENTITY:
            seen.append(new.attributes.get("hvac_action"))

    house.hass.bus.async_listen("state_changed", _on_change)
    for _ in range(2 * 120):
        await house.advance(seconds=30)
    seq = _collapse(seen)
    assert any(seq[i : i + 3] == ["heating", "idle", "fan"] for i in range(len(seq) - 2)), seq
    assert not any(seq[i : i + 2] == ["heating", "fan"] for i in range(len(seq) - 1)), seq


async def test_edge_delay_never_shows_heating_in_off_mode(make_sim) -> None:
    """With ``edge_report_delay_s`` a mode change is shown at once (verification 2026-11-13)."""
    house = await make_sim(profile=SimProfile(heat_rate="normal", edge_report_delay_s=60.0), **WINTER)
    sim = sim_entity(house)
    for _ in range(240):
        await house.advance(seconds=30)
        if sim._true_action() == "heating" and house.thermostat.action == "heating":
            break
    assert house.thermostat.action == "heating"
    await house.occupant_sets_thermostat(mode="off")
    assert house.thermostat.action != "heating"
