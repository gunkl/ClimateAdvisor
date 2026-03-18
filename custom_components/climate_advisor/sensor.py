"""Sensor platform for Climate Advisor.

Exposes the day classification, briefing, learning metrics, and next
recommended action as Home Assistant sensors for use in dashboards
and other automations.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ATTR_DAY_TYPE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    ATTR_BRIEFING,
    ATTR_NEXT_ACTION,
    ATTR_AUTOMATION_STATUS,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_COMPLIANCE_SCORE,
)
from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Advisor sensors from a config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        ClimateAdvisorDayTypeSensor(coordinator, entry),
        ClimateAdvisorTrendSensor(coordinator, entry),
        ClimateAdvisorNextActionSensor(coordinator, entry),
        ClimateAdvisorBriefingSensor(coordinator, entry),
        ClimateAdvisorComplianceSensor(coordinator, entry),
        ClimateAdvisorStatusSensor(coordinator, entry),
    ]

    async_add_entities(entities)


class ClimateAdvisorBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Climate Advisor sensors."""

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = f"Climate Advisor {name}"
        self._attr_icon = icon
        self._key = key

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if self.coordinator.data:
            return self.coordinator.data.get(self._key)
        return None


class ClimateAdvisorDayTypeSensor(ClimateAdvisorBaseSensor):
    """Sensor showing today's day type classification."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_DAY_TYPE, "Day Type", "mdi:weather-partly-cloudy")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Include trend info as attributes."""
        data = self.coordinator.data or {}
        return {
            "trend_direction": data.get(ATTR_TREND, "unknown"),
            "trend_magnitude": data.get(ATTR_TREND_MAGNITUDE, 0),
        }


class ClimateAdvisorTrendSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the temperature trend."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_TREND, "Trend", "mdi:trending-up")

    @property
    def icon(self) -> str:
        """Dynamic icon based on trend direction."""
        value = self.native_value
        if value == "warming":
            return "mdi:trending-up"
        elif value == "cooling":
            return "mdi:trending-down"
        return "mdi:trending-neutral"


class ClimateAdvisorNextActionSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the next recommended human action."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_NEXT_ACTION, "Next Action", "mdi:human-greeting")


class ClimateAdvisorBriefingSensor(ClimateAdvisorBaseSensor):
    """Sensor holding the full daily briefing text."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_BRIEFING, "Daily Briefing", "mdi:email-outline")

    @property
    def native_value(self) -> str | None:
        """Return a truncated version for the state (HA has a 255 char limit)."""
        full = self.coordinator.data.get(ATTR_BRIEFING, "") if self.coordinator.data else ""
        if len(full) > 250:
            return full[:247] + "..."
        return full

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Store the full briefing text as an attribute."""
        return {
            "full_briefing": self.coordinator.data.get(ATTR_BRIEFING, "") if self.coordinator.data else "",
        }


class ClimateAdvisorComplianceSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the comfort compliance score."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_COMPLIANCE_SCORE, "Comfort Score", "mdi:check-circle")

    @property
    def native_value(self) -> float | None:
        """Return compliance as a percentage."""
        value = super().native_value
        if value is not None:
            return round(value * 100, 1)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Include learning suggestions count."""
        data = self.coordinator.data or {}
        suggestions = data.get(ATTR_LEARNING_SUGGESTIONS, [])
        return {
            "pending_suggestions": len(suggestions),
            "suggestions": suggestions,
        }


class ClimateAdvisorStatusSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the overall automation status."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_AUTOMATION_STATUS, "Status", "mdi:home-thermometer")
