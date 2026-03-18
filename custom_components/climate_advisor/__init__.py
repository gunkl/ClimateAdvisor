"""Climate Advisor — Intelligent HVAC management for Home Assistant.

This integration provides:
- Forecast-aware day classification (hot/warm/mild/cool/cold)
- Trend-based predictive HVAC control
- Daily briefings with human action recommendations
- Automatic door/window and occupancy response
- A learning engine that adapts to household patterns
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DOMAIN,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_WEATHER_SERVICE,
    TEMP_SOURCE_CLIMATE_FALLBACK,
)
from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to new format."""
    if config_entry.version == 1:
        _LOGGER.info("Migrating Climate Advisor config entry from version 1 to 2")
        new_data = {**config_entry.data}

        # Migrate outdoor temp source
        outdoor_entity = new_data.get("outdoor_temp_entity")
        if outdoor_entity:
            if outdoor_entity.startswith("input_number."):
                new_data["outdoor_temp_source"] = TEMP_SOURCE_INPUT_NUMBER
            else:
                new_data["outdoor_temp_source"] = TEMP_SOURCE_SENSOR
        else:
            new_data["outdoor_temp_source"] = TEMP_SOURCE_WEATHER_SERVICE
            new_data.pop("outdoor_temp_entity", None)

        # Migrate indoor temp source
        indoor_entity = new_data.get("indoor_temp_entity")
        if indoor_entity:
            if indoor_entity.startswith("input_number."):
                new_data["indoor_temp_source"] = TEMP_SOURCE_INPUT_NUMBER
            else:
                new_data["indoor_temp_source"] = TEMP_SOURCE_SENSOR
        else:
            new_data["indoor_temp_source"] = TEMP_SOURCE_CLIMATE_FALLBACK
            new_data.pop("indoor_temp_entity", None)

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2
        )
        _LOGGER.info("Migration to version 2 complete")
        # Fall through to v2→v3 migration

    if config_entry.version == 2:
        _LOGGER.info("Migrating Climate Advisor config entry from version 2 to 3")
        new_data = {**config_entry.data}
        new_data.setdefault("door_window_groups", [])
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=3
        )
        _LOGGER.info("Migration to version 3 complete")
        # Fall through to v3→v4 migration

    if config_entry.version == 3:
        _LOGGER.info("Migrating Climate Advisor config entry from version 3 to 4")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_NOTIFY, False)
        new_data.setdefault(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
        new_data.setdefault(CONF_AUTOMATION_GRACE_NOTIFY, True)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=4
        )
        _LOGGER.info("Migration to version 4 complete")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climate Advisor from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data))

    # Set up scheduled events and listeners
    await coordinator.async_setup()

    # Perform initial data fetch
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register service for accepting/dismissing learning suggestions
    async def handle_suggestion_response(call):
        """Handle user response to a learning suggestion."""
        action = call.data.get("action")  # "accept" or "dismiss"
        suggestion_key = call.data.get("suggestion_key")

        if action == "accept":
            changes = coordinator.learning.accept_suggestion(suggestion_key)
            _LOGGER.info("Suggestion accepted: %s → changes: %s", suggestion_key, changes)
            # Apply changes to coordinator config
            coordinator.config.update(changes)
        elif action == "dismiss":
            coordinator.learning.dismiss_suggestion(suggestion_key)
            _LOGGER.info("Suggestion dismissed: %s", suggestion_key)

    hass.services.async_register(
        DOMAIN,
        "respond_to_suggestion",
        handle_suggestion_response,
    )

    _LOGGER.info("Climate Advisor integration loaded successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Climate Advisor config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
