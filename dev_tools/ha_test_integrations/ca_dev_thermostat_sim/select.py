"""HVAC profile selects for CA Dev Thermostat Sim (owner request 2026-11-12).

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Heating and cooling rate presets (undersized 2 / extreme weather 5 / normal 12 / oversized 18 F/h, or custom) and
the climate entity's reported precision (whole / half / tenth, or Home Assistant's default). Values live on the
thermostat entity; its setters store them in the entry's data (no reload). The rate numbers (number.py) and these
selects follow each other: picking a preset sets the rate, and typing a preset's rate selects that preset.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hvac_profile import PRECISION_OPTIONS, RATE_PRESET_OPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the rate-preset and precision selects for this zone."""
    async_add_entities([RatePresetSelect(entry, "heat"), RatePresetSelect(entry, "cool"), PrecisionSelect(entry)])


class _ProfileSelect(SelectEntity):
    _attr_has_entity_name = False
    _attr_should_poll = False

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


class RatePresetSelect(_ProfileSelect):
    """Heating or cooling rate preset."""

    _attr_options = list(RATE_PRESET_OPTIONS)

    def __init__(self, entry: ConfigEntry, direction: str) -> None:
        label = "Heating Rate Preset" if direction == "heat" else "Cooling Rate Preset"
        super().__init__(entry, f"{direction}_rate_preset", label)
        self._direction = direction

    @property
    def current_option(self) -> str | None:
        thermostat = self._thermostat()
        return None if thermostat is None else thermostat.rate_preset(self._direction)

    async def async_select_option(self, option: str) -> None:
        if (thermostat := self._thermostat()) is not None:
            thermostat.set_rate_preset(self._direction, option)


class PrecisionSelect(_ProfileSelect):
    """The climate entity's reported precision."""

    _attr_options = list(PRECISION_OPTIONS)

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "precision", "Reported Precision")

    @property
    def current_option(self) -> str | None:
        thermostat = self._thermostat()
        return None if thermostat is None else thermostat.precision_option

    async def async_select_option(self, option: str) -> None:
        if (thermostat := self._thermostat()) is not None:
            thermostat.set_precision(option)
