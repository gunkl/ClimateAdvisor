"""Real-HA test for Issue #1345: the Heat / Cool Threshold sensors.

Occupant story: someone runs Climate Advisor in observe-only mode next to their own controller and wants to graph
"what band would Climate Advisor hold my house to" against what their controller did. Home Assistant keeps
history for sensor states, not attributes, so the two band edges must be real sensors on the zone's device,
in the occupant's unit, and they must say exactly what the status API says (``ca_target_heat`` /
``ca_target_cool``) -- never the thermostat's own setpoint.

Real Home Assistant, the real coordinator, the real sensor platform and the real status view. Explicit asserts
only (no snapshot entry is added).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.api import ClimateAdvisorStatusView
from tests_ha.house.builder import CA_DOMAIN, SIM_DOMAIN

pytestmark = pytest.mark.house

_KEYS = {"heat_threshold": "Heat Threshold", "cool_threshold": "Cool Threshold"}


async def _status(hass, entry_id: str | None = None) -> dict:
    request = SimpleNamespace(
        app={"hass": hass},
        query={"entry_id": entry_id} if entry_id else {},
        method="GET",
        path="/api/climate_advisor/status",
        remote="t",
    )
    response = await ClimateAdvisorStatusView().get(request)
    return json.loads(response.text)


def _registry_entry(hass, entry_id: str, key: str):
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    return next(e for e in er.async_entries_for_config_entry(reg, entry_id) if e.unique_id == f"{entry_id}_{key}")


def _zone_device_id(hass, entry_id: str) -> str:
    from homeassistant.helpers import device_registry as dr

    devices = [
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        if (CA_DOMAIN, entry_id) in d.identifiers
    ]
    assert len(devices) == 1, devices
    return devices[0].id


def _assert_registered(hass, entry_id: str) -> None:
    from homeassistant.helpers.entity import EntityCategory

    device_id = _zone_device_id(hass, entry_id)
    for key, name in _KEYS.items():
        ent = _registry_entry(hass, entry_id, key)
        assert ent.device_id == device_id, (key, ent.device_id)
        assert ent.entity_category is EntityCategory.DIAGNOSTIC
        assert ent.disabled_by is None  # enabled by default
        assert ent.original_name == name
        state = hass.states.get(ent.entity_id)
        assert state is not None and state.state not in ("unknown", "unavailable"), (ent.entity_id, state)
        assert state.attributes["device_class"] == "temperature"
        assert state.attributes["state_class"] == "measurement"


async def test_threshold_sensors_register_and_match_the_status_api_fahrenheit(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    _assert_registered(hass, entry_id)

    heat, cool = house.sensor("heat_threshold"), house.sensor("cool_threshold")
    assert heat.attributes["unit_of_measurement"] == "°F"
    # Seed zone, 14:00, home and awake: the plain comfort band, armed (the day is classified).
    assert float(heat.state) == pytest.approx(70.0) and float(cool.state) == pytest.approx(75.0)
    for st in (heat, cool):
        assert st.attributes["context"] == "comfort" and st.attributes["provisional"] is False

    body = await _status(hass)
    assert float(heat.state) == pytest.approx(body["ca_target_heat"])
    assert float(cool.state) == pytest.approx(body["ca_target_cool"])


async def test_threshold_sensors_convert_to_celsius_and_match_the_status_api(make_house) -> None:
    """A metric install: HA converts the native °F to °C, and the result equals the API's display value."""
    house = await make_house(units="metric", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    _assert_registered(house.hass, house.ca_entry.entry_id)

    heat, cool = house.sensor("heat_threshold"), house.sensor("cool_threshold")
    assert heat.attributes["unit_of_measurement"] == "°C"
    assert float(heat.state) == pytest.approx(21.1, abs=0.05)  # 70 °F
    assert float(cool.state) == pytest.approx(23.9, abs=0.05)  # 75 °F

    body = await _status(house.hass)
    assert float(heat.state) == pytest.approx(body["ca_target_heat"], abs=0.05)
    assert float(cool.state) == pytest.approx(body["ca_target_cool"], abs=0.05)


async def test_threshold_sensors_show_the_away_band(make_house) -> None:
    """Nobody home: both edges are the setback band and the context says "away", like the API."""
    house = await make_house(
        units="us",
        profile="scripted",
        initial_indoor_f=72.0,
        ca_config={"home_toggle_entity": "input_boolean.home"},
        extra_states={"input_boolean.home": "off"},
    )
    await house.advance(minutes=40)

    heat, cool = house.sensor("heat_threshold"), house.sensor("cool_threshold")
    assert heat.attributes["context"] == "away" and cool.attributes["context"] == "away"
    assert float(heat.state) == pytest.approx(60.0) and float(cool.state) == pytest.approx(80.0)
    body = await _status(house.hass)
    assert (float(heat.state), float(cool.state)) == pytest.approx((body["ca_target_heat"], body["ca_target_cool"]))


async def test_two_zones_each_get_their_own_threshold_sensors(make_house) -> None:
    """Multi-zone: each zone's sensors sit on that zone's device and carry that zone's own band."""
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass

    sim2 = MockConfigEntry(
        domain=SIM_DOMAIN,
        title="Upstairs Thermostat",
        data={**house.sim_entry.data, "name": "Upstairs Thermostat"},
    )
    sim2.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sim2.entry_id)
    await hass.async_block_till_done()
    upstairs_climate = next(
        s.entity_id for s in hass.states.async_all("climate") if s.entity_id != "climate.house_thermostat"
    )
    zone2 = MockConfigEntry(
        domain=CA_DOMAIN,
        title="Upstairs",
        version=house.ca_entry.version,
        minor_version=house.ca_entry.minor_version,
        data={**house.ca_entry.data, "climate_entity": upstairs_climate, "comfort_heat": 66.0, "comfort_cool": 78.0},
        options=dict(house.ca_entry.options),
    )
    zone2.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(zone2.entry_id)
        await house.advance(minutes=6)

        zone1_id = house.ca_entry.entry_id
        _assert_registered(hass, zone1_id)
        _assert_registered(hass, zone2.entry_id)
        assert _zone_device_id(hass, zone1_id) != _zone_device_id(hass, zone2.entry_id)

        def _value(entry_id: str, key: str) -> float:
            return float(hass.states.get(_registry_entry(hass, entry_id, key).entity_id).state)

        assert (_value(zone1_id, "heat_threshold"), _value(zone1_id, "cool_threshold")) == (70.0, 75.0)
        assert (_value(zone2.entry_id, "heat_threshold"), _value(zone2.entry_id, "cool_threshold")) == (66.0, 78.0)
        body2 = await _status(hass, zone2.entry_id)
        assert (body2["ca_target_heat"], body2["ca_target_cool"]) == (66.0, 78.0)
    finally:
        for entry in (zone2, sim2):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
