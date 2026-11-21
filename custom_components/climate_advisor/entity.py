"""Shared base entity for Climate Advisor's sensor and switch platforms.

Home Assistant quality scale (Issue #1637): ``common-modules`` puts the base entity in ``entity.py``, and
``devices`` asks for complete device information. Every entity of a zone belongs to one device per config
entry, a service device (``DeviceEntryType.SERVICE``): Climate Advisor is software running on top of the
zone's thermostat and sensors, not a piece of hardware.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VERSION
from .coordinator import ClimateAdvisorCoordinator


def zone_device_info(entry: ConfigEntry) -> DeviceInfo:
    """The one device that groups a zone's entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Climate Advisor",
        model="Zone Controller",
        sw_version=VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


class ClimateAdvisorEntity(CoordinatorEntity):
    """A zone entity: unique id ``<entry_id>_<key>``, named from strings.json, on the zone's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry, key: str) -> None:
        """Initialize the entity for one zone (config entry)."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Issue #1341 (D6): the name comes from strings.json ``entity.<platform>.<key>.name`` (HA's own method),
        # never from ``_attr_name``, which would override the translation.
        self._attr_translation_key = key
        self._attr_device_info = zone_device_info(entry)
