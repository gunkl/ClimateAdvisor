"""Climate Advisor — Intelligent HVAC management for Home Assistant.

This integration provides:
- Forecast-aware day classification (hot/warm/mild/cool/cold)
- Trend-based predictive HVAC control
- Daily briefings with human action recommendations
- Automatic door/window and occupancy response
- A learning engine that adapts to household patterns
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import API_VIEWS
from .const import (
    PANEL_FRONTEND_PATH,
    PANEL_URL,
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_EMAIL_NOTIFY,
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
        new_data.pop("door_window_groups", None)  # removed: groups are binary_sensor entities
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

    if config_entry.version == 4:
        _LOGGER.info("Migrating Climate Advisor config entry from version 4 to 5")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_EMAIL_NOTIFY, True)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=5
        )
        _LOGGER.info("Migration to version 5 complete")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climate Advisor from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data))

    # Restore persisted state from last run (before setup registers listeners)
    await coordinator.async_restore_state()

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

    # Register debug services
    async def handle_force_reclassify(call):
        """Force a coordinator refresh / reclassification."""
        await coordinator.async_request_refresh()

    async def handle_resend_briefing(call):
        """Re-send the daily briefing."""
        from homeassistant.util import dt as dt_util

        coordinator._briefing_sent_today = False
        await coordinator._async_send_briefing(dt_util.now())

    async def handle_dump_diagnostics(call):
        """Log a comprehensive diagnostic snapshot for troubleshooting."""
        from homeassistant.util import dt as dt_util

        diag = {
            "timestamp": dt_util.now().isoformat(),
            "debug_state": coordinator.get_debug_state(),
            "chart_data_summary": {
                "outdoor_points": len(coordinator._outdoor_temp_history),
                "indoor_points": len(coordinator._indoor_temp_history),
            },
            "learning_summary": coordinator.learning.get_compliance_summary(),
            "config": {
                k: v for k, v in coordinator.config.items()
                if k != "notify_service"
            },
            "briefing_state": {
                "sent_today": coordinator._briefing_sent_today,
                "briefing_length": len(coordinator._last_briefing),
            },
        }
        _LOGGER.info(
            "Diagnostic dump requested:\n%s",
            json.dumps(diag, indent=2, default=str),
        )

    hass.services.async_register(DOMAIN, "force_reclassify", handle_force_reclassify)
    hass.services.async_register(DOMAIN, "resend_briefing", handle_resend_briefing)
    hass.services.async_register(DOMAIN, "dump_diagnostics", handle_dump_diagnostics)

    # Register REST API views for the dashboard panel
    for view_cls in API_VIEWS:
        hass.http.register_view(view_cls())

    # Register dashboard panel (iframe serving frontend/index.html)
    frontend_path = Path(__file__).parent / "frontend"
    from homeassistant.components.http import StaticPathConfig
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_URL, str(frontend_path), cache_headers=False)]
    )
    from homeassistant.components.frontend import async_register_built_in_panel
    import hashlib
    _panel_bytes = await hass.async_add_executor_job(
        (frontend_path / "index.html").read_bytes
    )
    _panel_hash = hashlib.md5(_panel_bytes).hexdigest()[:8]
    async_register_built_in_panel(
        hass,
        "iframe",
        sidebar_title="Climate Advisor",
        sidebar_icon="mdi:thermostat",
        frontend_url_path=PANEL_FRONTEND_PATH,
        require_admin=False,
        config={"url": f"{PANEL_URL}/index.html?v={_panel_hash}"},
    )

    _LOGGER.info("Climate Advisor integration loaded successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Climate Advisor config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()

    # Remove the dashboard panel
    from homeassistant.components.frontend import async_remove_panel
    async_remove_panel(hass, PANEL_FRONTEND_PATH)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
