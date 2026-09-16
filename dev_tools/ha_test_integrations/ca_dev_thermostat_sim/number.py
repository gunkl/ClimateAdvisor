"""Manual current-temperature override for CA Dev Thermostat Sim (Issue #898).

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

A one-shot input: setting this number jumps the zone's simulated indoor
temperature directly (e.g. to instantly test behavior at 78°F without waiting
for physics to get there), then the display resets to None so it's visually
clear this is a one-shot action, not a held override — the thermostat's own
current_temperature is the real, physics-driven value going forward (Issue
#898 decision 3).

Manual setpoint override needs no equivalent entity here — the existing
SimulatedThermostat.async_set_temperature() (climate.py) already provides it
via the standard HA climate UI; building a second mechanism would duplicate a
capability that already exists.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CURRENT_TEMP_OVERRIDE_MAX_F, CONF_CURRENT_TEMP_OVERRIDE_MIN_F, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the manual current-temperature override entity for this zone."""
    async_add_entities([CurrentTemperatureOverride(entry)])


class CurrentTemperatureOverride(NumberEntity):
    """Setting this jumps the zone's simulated thermostat to a new current
    temperature, then resets its own display (Issue #898 decision 3)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "°F"
    _attr_native_min_value = CONF_CURRENT_TEMP_OVERRIDE_MIN_F
    _attr_native_max_value = CONF_CURRENT_TEMP_OVERRIDE_MAX_F
    _attr_native_step = 0.1

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_current_temp_override"
        self._attr_name = "Set Current Temperature"
        self._attr_native_value: float | None = None

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        """Apply the override to the zone's thermostat, then reset this entity's
        own displayed value back to None (Issue #898 decision 3) — the injected
        value takes effect on the thermostat immediately, this input is not
        meant to keep showing it afterward."""
        zone_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        thermostat = zone_data.get("thermostat_entity")
        if thermostat is None:
            _LOGGER.warning(
                "CA Dev Thermostat Sim %s: no thermostat entity registered for this zone yet — override not applied",
                self.entity_id,
            )
            return

        thermostat.apply_manual_temperature_override(value)
        _LOGGER.info(
            "CA Dev Thermostat Sim %s: manual current-temperature override applied — %.1f°F",
            self.entity_id,
            value,
        )

        self._attr_native_value = None
        self.async_write_ha_state()
