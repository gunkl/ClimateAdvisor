"""Optional companion temperature sensor for CA Dev Thermostat Sim (HVAC profile, owner request 2026-11-12).

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Many real thermostats report ``current_temperature`` in whole degrees while the same device also exposes a finer
temperature sensor (an Ecobee's reports in about 0.18 F steps). This sensor is that finer reading: the thermostat's
own reported reading (the simulated air, through the sensor lag when one is set), in tenths of a degree F.

It sits on the SAME device as the climate entity, with ``device_class: temperature`` and a room name ("Temperature"),
which is exactly what Climate Advisor's ``indoor_temp.discover_companion_sensors`` looks for (Issue #1230). Created
only when the entry's ``companion_sensor`` option is on.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .climate import sim_device_info
from .const import CONF_COMPANION_SENSOR, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the companion sensor when the entry asks for one."""
    if not entry.data.get(CONF_COMPANION_SENSOR):
        return
    sensor = CompanionTemperatureSensor(entry)
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["companion_entity"] = sensor
    async_add_entities([sensor])


class CompanionTemperatureSensor(SensorEntity):
    """The thermostat's room reading at tenths of a degree F, on the thermostat's device."""

    _attr_has_entity_name = True
    _attr_name = "Temperature"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_companion_temperature"
        self._attr_device_info = sim_device_info(entry)

    @property
    def native_value(self) -> float | None:
        thermostat = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("thermostat_entity")
        if thermostat is None:
            return None
        reading = thermostat.reported_temperature
        return None if reading is None else round(float(reading), 1)
