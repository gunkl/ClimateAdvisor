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
from .hvac_profile import BLOWER_MAX_MIN, RATE_MAX_F_PER_H, RATE_MIN_F_PER_H, SENSOR_LAG_MAX_MIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the manual current-temperature override and the HVAC profile numbers for this zone."""
    async_add_entities(
        [
            CurrentTemperatureOverride(entry),
            RateNumber(entry, "heat"),
            RateNumber(entry, "cool"),
            SensorLagNumber(entry),
            BlowerOverrunNumber(entry, "heat"),
            BlowerOverrunNumber(entry, "cool"),
        ]
    )


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


# --------------------------------------------------------------------------- HVAC profile runtime controls
# Owner request 2026-11-12: heating/cooling rate, sensor lag and blower overrun adjustable while the zone runs. Each
# reads its value from the thermostat entity and writes through its setter, which also stores the value in the
# entry's data (no reload), so a change survives a restart and shows in Reconfigure.


class _ProfileNumber(NumberEntity):
    """A thermostat profile value as a number box. Subclasses name the getter and setter."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, key: str, label: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = f"{entry.data.get('name') or entry.title} {label}"

    def _thermostat(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("thermostat_entity")

    async def async_added_to_hass(self) -> None:
        self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry.entry_id, {}).setdefault(
            "profile_controls", []
        ).append(self)

    async def async_will_remove_from_hass(self) -> None:
        controls = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("profile_controls", [])
        if self in controls:
            controls.remove(self)


class RateNumber(_ProfileNumber):
    """Heating or cooling capacity in F/h (a magnitude for both)."""

    _attr_native_unit_of_measurement = "°F/h"
    _attr_native_min_value = RATE_MIN_F_PER_H
    _attr_native_max_value = RATE_MAX_F_PER_H
    _attr_native_step = 0.1

    def __init__(self, entry: ConfigEntry, direction: str) -> None:
        super().__init__(entry, f"{direction}_rate", "Heating Rate" if direction == "heat" else "Cooling Rate")
        self._direction = direction

    @property
    def native_value(self) -> float | None:
        thermostat = self._thermostat()
        return None if thermostat is None else thermostat.rate(self._direction)

    async def async_set_native_value(self, value: float) -> None:
        if (thermostat := self._thermostat()) is not None:
            thermostat.set_rate(self._direction, value)


class SensorLagNumber(_ProfileNumber):
    """First-order lag (minutes) from the simulated air to the reported reading(s)."""

    _attr_native_unit_of_measurement = "min"
    _attr_native_min_value = 0.0
    _attr_native_max_value = SENSOR_LAG_MAX_MIN
    _attr_native_step = 0.5

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "sensor_lag", "Sensor Lag")

    @property
    def native_value(self) -> float | None:
        thermostat = self._thermostat()
        return None if thermostat is None else thermostat.sensor_lag_min

    async def async_set_native_value(self, value: float) -> None:
        if (thermostat := self._thermostat()) is not None:
            thermostat.set_sensor_lag(value)


class BlowerOverrunNumber(_ProfileNumber):
    """Minutes the blower keeps running (hvac_action ``fan``) after a heat or cool run stops."""

    _attr_native_unit_of_measurement = "min"
    _attr_native_min_value = 0.0
    _attr_native_max_value = BLOWER_MAX_MIN
    _attr_native_step = 0.5

    def __init__(self, entry: ConfigEntry, direction: str) -> None:
        label = "Heat Blower Overrun" if direction == "heat" else "Cool Blower Overrun"
        super().__init__(entry, f"blower_overrun_{direction}", label)
        self._direction = direction

    @property
    def native_value(self) -> float | None:
        thermostat = self._thermostat()
        return None if thermostat is None else thermostat.blower_overrun_min(self._direction)

    async def async_set_native_value(self, value: float) -> None:
        if (thermostat := self._thermostat()) is not None:
            thermostat.set_blower_overrun(self._direction, value)
