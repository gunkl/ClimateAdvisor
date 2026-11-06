"""A thermostat's finer temperature sensor is logged beside its whole-degree reading (Issue #1230 R1).

Occupant view: the hallway thermostat's climate entity reports 69 F in whole degrees, but the same physical device also
exposes a 0.18 F-step temperature sensor (68.54). Climate Advisor never logged that finer sensor, so the prediction
engine could not learn the heating cycle. Now the chart log carries it. This asks real Home Assistant's device and
entity registries (not CA's fakes) which sensors share the climate entity's device. Logging only: no decision changes.
"""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor.const import DOMAIN
from tests_ha.test_unload_cleanup import _seed_entry, _setup_zone

TEMP = "sensor.santa_maria_hallway_current_temperature"
HUMIDITY = "sensor.santa_maria_hallway_humidity"
DISABLED_TEMP = "sensor.santa_maria_hallway_disabled_temperature"
OTHER_DEVICE_TEMP = "sensor.garage_temperature"


def _register(hass: HomeAssistant, climate_id: str) -> None:
    """Device with the climate entity, a temperature sensor, a humidity sensor and a disabled temperature sensor."""
    host = MockConfigEntry(domain="test")
    host.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(config_entry_id=host.entry_id, identifiers={("test", "hallway")})
    other = dev_reg.async_get_or_create(config_entry_id=host.entry_id, identifiers={("test", "garage")})
    domain, _, object_id = climate_id.partition(".")
    ent_reg.async_get_or_create(domain, "test", "hallway-climate", suggested_object_id=object_id, device_id=device.id)
    for entity_id, uid, device_class, kw in (
        (TEMP, "t", "temperature", {}),
        (HUMIDITY, "h", "humidity", {}),
        (DISABLED_TEMP, "d", "temperature", {"disabled_by": RegistryEntryDisabler.USER}),
    ):
        ent_reg.async_get_or_create(
            "sensor",
            "test",
            uid,
            suggested_object_id=entity_id.split(".")[1],
            device_id=device.id,
            original_device_class=device_class,
            entity_category=None if device_class else EntityCategory.DIAGNOSTIC,
            **kw,
        )
    ent_reg.async_get_or_create(
        "sensor",
        "test",
        "g",
        suggested_object_id=OTHER_DEVICE_TEMP.split(".")[1],
        device_id=other.id,
        original_device_class="temperature",
    )


async def test_device_temperature_sensor_is_logged_in_a_chart_row(hass: HomeAssistant, tmp_path) -> None:
    climate_id = _seed_entry()["data"]["climate_entity"]
    _register(hass, climate_id)
    entry = await _setup_zone(hass, tmp_path)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    for entity_id, value in ((TEMP, "68.54"), (HUMIDITY, "40"), (DISABLED_TEMP, "70"), (OTHER_DEVICE_TEMP, "75")):
        hass.states.async_set(entity_id, value, {"unit_of_measurement": "°F", "device_class": "temperature"})
    coordinator._companion_temp_sensors_cache = None  # entities appeared after setup; re-resolve

    sensors = coordinator._read_chart_indoor_sensors()
    assert sensors[climate_id] == 70  # the climate entity's whole-degree attribute is still there
    assert sensors[TEMP] == 68.54  # native resolution, not 69
    assert HUMIDITY not in sensors and DISABLED_TEMP not in sensors and OTHER_DEVICE_TEMP not in sensors

    coordinator._chart_log.append(hvac="heating", fan=False, indoor=70.0, outdoor=50.0, indoor_sensors=sensors)
    row = coordinator._chart_log.get_entries("24h")[-1]
    assert row["indoor_sensors"][TEMP] == 68.54

    # Unavailable companion -> null, others unaffected.
    hass.states.async_set(TEMP, "unavailable")
    assert coordinator._read_chart_indoor_sensors()[TEMP] is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_control_reads_the_finer_sensor_and_falls_back(hass: HomeAssistant, tmp_path) -> None:
    """Issue #1230 R1b: CA's indoor reading is 68.54 (not the thermostat's 69) once proven, 69 when the sensor drops.

    Occupant view: heat/cool edges and the chart's ``indoor`` see the real temperature instead of 1 F steps, and a
    dead companion sensor never stalls anything: CA simply goes back to the thermostat's own reading.
    """
    climate_id = _seed_entry()["data"]["climate_entity"]
    _register(hass, climate_id)
    entry = await _setup_zone(hass, tmp_path)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator._companion.entities == (TEMP,)

    climate_attrs = dict(hass.states.get(climate_id).attributes)
    hass.states.async_set(climate_id, "heat", {**climate_attrs, "current_temperature": 69})
    hass.states.async_set(TEMP, "68.54", {"unit_of_measurement": "°F", "device_class": "temperature"})

    assert coordinator._get_indoor_temp() == 68.54
    assert coordinator.automation_engine._get_indoor_temp_f() == 68.54  # the engine decides on the same value
    reading = coordinator._get_indoor_temp_with_provenance()
    assert reading.source_entity == TEMP and reading.primary_value == 68.54

    hass.states.async_set(TEMP, "unavailable")
    assert coordinator._get_indoor_temp() == 69.0
    assert coordinator._get_indoor_temp_with_provenance().companion_reason == "unavailable"

    # Back to the finer sensor, then the occupant turns the option off: exactly today's behavior.
    hass.states.async_set(TEMP, "68.72", {"unit_of_measurement": "°F", "device_class": "temperature"})
    assert coordinator._get_indoor_temp() == 68.72
    coordinator.config = {**coordinator.config, "thermostat_companion_sensor": "off"}
    assert coordinator._get_indoor_temp() == 69.0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
