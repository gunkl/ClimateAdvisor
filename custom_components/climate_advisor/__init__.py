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

from .const import DOMAIN
from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


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
