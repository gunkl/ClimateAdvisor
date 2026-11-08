"""Indoor and outdoor humidity and feels-like land on the real coordinator's chart rows (Issue #1414).

Occupant view: on a hot humid afternoon the Status chart shows the room at 70 % and feeling like about 106 F, and the
outdoors at 65 % and feeling hotter still; on a mild day it shows the humidity but no feels-like line; on a cold windy
morning it shows the wind chill outside. The thermostat's own humidity sensor is found through real Home Assistant's
device and entity registries, and the climate entity's TARGET humidity is never used. Chart display only.
"""

from __future__ import annotations

import pytest
from homeassistant.components.climate.const import ClimateEntityStateAttribute
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.const import DOMAIN
from tests_ha.test_unload_cleanup import _seed_entry, _setup_zone

HUMIDITY = "sensor.ecobee_humidity"
OUTDOOR_NAMED = "sensor.ecobee_outdoor_humidity"


def _register(hass: HomeAssistant, climate_id: str) -> None:
    """The thermostat device: the climate entity, its room humidity sensor and an 'Outdoor humidity' sensor."""
    host = MockConfigEntry(domain="test")
    host.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=host.entry_id, identifiers={("test", "ecobee")})
    ent_reg = er.async_get(hass)
    domain, _, object_id = climate_id.partition(".")
    ent_reg.async_get_or_create(domain, "test", "ecobee-climate", suggested_object_id=object_id, device_id=device.id)
    for entity_id, uid, name in ((HUMIDITY, "h", "Humidity"), (OUTDOOR_NAMED, "oh", "Outdoor humidity")):
        ent_reg.async_get_or_create(
            "sensor",
            "test",
            uid,
            suggested_object_id=entity_id.split(".")[1],
            device_id=device.id,
            original_device_class="humidity",
            original_name=name,
        )


def _set_weather(hass, weather_id, *, temp, rh, wind, unit="mph") -> None:
    attrs = dict(hass.states.get(weather_id).attributes)
    attrs.update({"temperature": temp, "humidity": rh, "wind_speed": wind, "wind_speed_unit": unit})
    hass.states.async_set(weather_id, "sunny", attrs)


def _set_room(hass, climate_id, *, temp, rh) -> None:
    attrs = dict(hass.states.get(climate_id).attributes)
    # the TARGET humidity attribute is set to a decoy: it must never reach a row
    attrs.update({"current_temperature": temp, "humidity": 99, "current_humidity": 41})
    hass.states.async_set(climate_id, "cool", attrs)
    hass.states.async_set(HUMIDITY, str(rh), {"unit_of_measurement": "%", "device_class": "humidity"})
    hass.states.async_set(OUTDOOR_NAMED, "12", {"unit_of_measurement": "%", "device_class": "humidity"})


def _row(coordinator) -> dict:
    coordinator._append_occupancy_chart_row("occupancy_change")  # a real append site, real _read_chart_context
    return coordinator._chart_log.raw_entries()[-1]


def test_climate_humidity_attribute_is_the_target() -> None:
    """Why the climate ``humidity`` attribute is never read: in Home Assistant it is the TARGET humidity."""
    assert ClimateEntityStateAttribute.TARGET_HUMIDITY == "humidity"
    assert ClimateEntityStateAttribute.CURRENT_HUMIDITY == "current_humidity"


async def test_rows_carry_humidity_and_feels_like(hass: HomeAssistant, tmp_path) -> None:
    seed = _seed_entry()["data"]
    climate_id, weather_id = seed["climate_entity"], seed["weather_entity"]
    _register(hass, climate_id)
    entry = await _setup_zone(hass, tmp_path)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._companion_sensors_cache = {}  # entities appeared after setup; re-resolve

    # Hot humid afternoon.
    _set_room(hass, climate_id, temp=90, rh=70)
    _set_weather(hass, weather_id, temp=92, rh=65, wind=5)
    row = _row(coordinator)
    assert row["indoor"] == 90 and row["humidity"] == 70.0  # the thermostat's own sensor, not 99 / 41 / 12
    assert row["feelslike"] == pytest.approx(105.92, abs=0.01)
    assert row["outdoor"] == 92 and row["outdoor_humidity"] == 65.0
    assert row["outdoor_feelslike"] == pytest.approx(108.26, abs=0.01)  # NWS table: 92 F / 65 % -> 108 F

    # Mild day: humidity, no feels-like.
    _set_room(hass, climate_id, temp=72, rh=45.5)
    _set_weather(hass, weather_id, temp=70, rh=50, wind=5)
    row = _row(coordinator)
    assert row["humidity"] == 45.5 and row["outdoor_humidity"] == 50.0
    assert "feelslike" not in row and "outdoor_feelslike" not in row

    # Cold windy morning: wind chill outside (20 mph given as km/h), none indoors.
    _set_room(hass, climate_id, temp=68, rh=35)
    _set_weather(hass, weather_id, temp=30, rh=80, wind=32.18688, unit="km/h")
    row = _row(coordinator)
    assert row["outdoor_feelslike"] == pytest.approx(17.36, abs=0.01)
    assert "feelslike" not in row

    # The chart payload carries the fields (°F install: unconverted values).
    chart = coordinator.get_chart_data("24h")
    last = chart["state_log"][-1]
    assert last["outdoor_humidity"] == 80.0 and last["outdoor_feelslike"] == pytest.approx(17.4, abs=0.05)

    # Issue #1459: the capabilities block, built on HA's executor thread as the REST view does, finds the thermostat's
    # own humidity sensor through the device lookup the rows above made on the event loop (cache only: the thermostat's
    # current_humidity is gone here, so only the device sensor can make it True).
    attrs = {k: v for k, v in hass.states.get(climate_id).attributes.items() if k != "current_humidity"}
    hass.states.async_set(climate_id, "cool", attrs)
    caps = (await hass.async_add_executor_job(coordinator.get_chart_data, "24h"))["capabilities"]
    assert caps["humidity_indoor"] is True and caps["humidity_outdoor"] is True
    # no lookup made on the loop yet (as after a restart): the executor never reads the registry, and unknown means
    # available, so the chip does not lock while time passes
    coordinator._companion_sensors_cache = {}
    caps = (await hass.async_add_executor_job(coordinator.get_chart_data, "24h"))["capabilities"]
    assert caps["humidity_indoor"] is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
