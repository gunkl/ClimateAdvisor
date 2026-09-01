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

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from . import log_capture
from .api import API_VIEWS
from .const import (
    CONF_AI_API_KEY,
    CONF_AI_AUTO_REQUESTS_PER_DAY,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AI_INVESTIGATOR_MAX_TOKENS,
    CONF_AI_INVESTIGATOR_MODEL,
    CONF_AI_INVESTIGATOR_REASONING,
    CONF_AI_INVESTIGATOR_RPD,
    CONF_AI_MANUAL_REQUESTS_PER_DAY,
    CONF_AI_MAX_TOKENS,
    CONF_AI_MODEL,
    CONF_AI_MONTHLY_BUDGET,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TEMPERATURE,
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_DEFAULT_TOU_LEAD_MINUTES,
    CONF_EMAIL_BRIEFING,
    CONF_EMAIL_DOOR_WINDOW_PAUSE,
    CONF_EMAIL_GRACE_EXPIRED,
    CONF_EMAIL_GRACE_REPAUSE,
    CONF_EMAIL_NOTIFY,
    CONF_EMAIL_OCCUPANCY_HOME,
    CONF_FAN_MODE,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_PUSH_BRIEFING,
    CONF_PUSH_DOOR_WINDOW_PAUSE,
    CONF_PUSH_OCCUPANCY_HOME,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_SLEEP_COOL,
    CONF_SLEEP_HEAT,
    CONF_TEMP_UNIT,
    CONF_THRESHOLD_COOL,
    CONF_THRESHOLD_HOT,
    CONF_THRESHOLD_MILD,
    CONF_THRESHOLD_WARM,
    CONF_VACATION_TOGGLE,
    CONF_VACATION_TOGGLE_INVERT,
    CONF_WELCOME_HOME_DEBOUNCE,
    DEFAULT_AI_AUTO_REQUESTS_PER_DAY,
    DEFAULT_AI_ENABLED,
    DEFAULT_AI_INVESTIGATOR_ENABLED,
    DEFAULT_AI_INVESTIGATOR_MAX_TOKENS,
    DEFAULT_AI_INVESTIGATOR_MODEL,
    DEFAULT_AI_INVESTIGATOR_REASONING,
    DEFAULT_AI_INVESTIGATOR_RPD,
    DEFAULT_AI_MANUAL_REQUESTS_PER_DAY,
    DEFAULT_AI_MAX_TOKENS,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_MONTHLY_BUDGET,
    DEFAULT_AI_REASONING_EFFORT,
    DEFAULT_AI_TEMPERATURE,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    DEFAULT_THRESHOLD_COOL,
    DEFAULT_THRESHOLD_HOT,
    DEFAULT_THRESHOLD_MILD,
    DEFAULT_THRESHOLD_WARM,
    DEFAULT_TOU_LEAD_MINUTES,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    DOMAIN,
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    PANEL_FRONTEND_PATH,
    PANEL_URL,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    VERSION,
)
from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]

# Issue #796 Gap 5/9: the five zone-scoped services below are registered ONCE,
# domain-wide (HA's service registry has no per-config-entry namespace) — see
# _resolve_zone_coordinator()'s docstring for why every one of them now
# requires a call.data["entry_id"] field instead of closing over a specific
# zone's `coordinator`. This tuple is the single source of truth for the
# service-name set, read by both the registration guard in
# async_setup_entry() and the teardown loop in async_unload_entry().
ZONE_SCOPED_SERVICES = (
    "respond_to_suggestion",
    "force_reclassify",
    "resend_briefing",
    "dump_diagnostics",
    "reset_learning_data",
)

# Issue #796 Gap 6/8: the REST API views (api.py's API_VIEWS — every view's
# `url` class attribute is a fixed `API_*` constant from const.py, not
# per-entry-derived, confirmed by reading api.py) and the dashboard panel
# (PANEL_URL, PANEL_FRONTEND_PATH — both fixed module-level string constants
# in const.py) are shared, domain-wide resources, not per-zone resources — the
# same collision shape as ZONE_SCOPED_SERVICES above. Deliberately NOT stored
# under hass.data[DOMAIN]: api.py's _get_coordinator() does
# `next(iter(hass.data[DOMAIN].values()))`, and log_capture.py's identical
# comment explains why inserting a non-coordinator value into that dict would
# break it. Tracks whether this zone's async_setup_entry() call (or an
# earlier zone's) has already registered — or attempted and determined
# already-registered — the shared views/panel, so a second-and-later zone's
# setup never attempts a duplicate registration in the first place.
_PANEL_HASS_DATA_KEY = "climate_advisor_panel_registered"


def _resolve_weather_entity(hass: HomeAssistant, configured: str) -> str | None:
    """Try to resolve a stale weather entity ID.

    Returns the valid entity ID if exactly one weather entity exists,
    or None if the situation is ambiguous (0 or 2+ entities).
    """
    if hass.states.get(configured):
        return configured

    weather_entities = [state.entity_id for state in hass.states.async_all("weather")]

    if len(weather_entities) == 1:
        return weather_entities[0]

    return None


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

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)
        _LOGGER.info("Migration to version 2 complete")
        # Fall through to v2→v3 migration

    if config_entry.version == 2:
        _LOGGER.info("Migrating Climate Advisor config entry from version 2 to 3")
        new_data = {**config_entry.data}
        new_data.pop("door_window_groups", None)  # removed: groups are binary_sensor entities
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
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
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=4)
        _LOGGER.info("Migration to version 4 complete")

    if config_entry.version == 4:
        _LOGGER.info("Migrating Climate Advisor config entry from version 4 to 5")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_EMAIL_NOTIFY, True)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=5)
        _LOGGER.info("Migration to version 5 complete")

    if config_entry.version == 5:
        _LOGGER.info("Migrating Climate Advisor config entry from version 5 to 6")
        new_data = {**config_entry.data}

        configured_weather = new_data.get("weather_entity", "")
        if not hass.states.get(configured_weather):
            resolved = _resolve_weather_entity(hass, configured_weather)
            if resolved and resolved != configured_weather:
                _LOGGER.warning(
                    "Weather entity '%s' no longer exists; auto-resolved to '%s' (only weather entity available)",
                    configured_weather,
                    resolved,
                )
                new_data["weather_entity"] = resolved
            elif not resolved:
                _LOGGER.warning(
                    "Weather entity '%s' no longer exists and cannot be "
                    "auto-resolved (zero or multiple weather entities found). "
                    "Please update via integration options",
                    configured_weather,
                )

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=6)
        _LOGGER.info("Migration to version 6 complete")

    if config_entry.version == 6:
        _LOGGER.info("Migrating Climate Advisor config entry from version 6 to 7")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_HOME_TOGGLE, None)
        new_data.setdefault(CONF_HOME_TOGGLE_INVERT, False)
        new_data.setdefault(CONF_VACATION_TOGGLE, None)
        new_data.setdefault(CONF_VACATION_TOGGLE_INVERT, False)
        new_data.setdefault(CONF_GUEST_TOGGLE, None)
        new_data.setdefault(CONF_GUEST_TOGGLE_INVERT, False)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=7)
        _LOGGER.info("Migration to version 7 complete")

    if config_entry.version == 7:
        _LOGGER.info("Migrating Climate Advisor config entry from version 7 to 8")
        new_data = {**config_entry.data}
        # Replace global email_notify with per-event toggles
        old_email = new_data.pop(CONF_EMAIL_NOTIFY, True)
        # Email toggles inherit from old global setting
        new_data.setdefault(CONF_EMAIL_BRIEFING, old_email)
        new_data.setdefault(CONF_EMAIL_DOOR_WINDOW_PAUSE, old_email)
        new_data.setdefault(CONF_EMAIL_GRACE_EXPIRED, old_email)
        new_data.setdefault(CONF_EMAIL_GRACE_REPAUSE, old_email)
        new_data.setdefault(CONF_EMAIL_OCCUPANCY_HOME, old_email)
        # Push toggles all default True (preserves current behavior)
        new_data.setdefault(CONF_PUSH_BRIEFING, True)
        new_data.setdefault(CONF_PUSH_DOOR_WINDOW_PAUSE, True)
        new_data.setdefault(CONF_PUSH_OCCUPANCY_HOME, True)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=8)
        _LOGGER.info("Migration to version 8 complete")

    if config_entry.version == 8:
        _LOGGER.info("Migrating Climate Advisor config entry from version 8 to 9")
        new_data = {**config_entry.data}
        # Add temperature unit preference — fahrenheit default preserves existing behavior
        new_data.setdefault(CONF_TEMP_UNIT, "fahrenheit")
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=9)
        _LOGGER.info("Migration to version 9 complete")

    if config_entry.version == 9:
        _LOGGER.info("Migrating Climate Advisor config entry from version 9 to 10")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=10)
        _LOGGER.info("Migration to version 10 complete")

    if config_entry.version == 10:
        _LOGGER.info("Migrating Climate Advisor config entry from version 10 to 11")
        new_data = {**config_entry.data}
        for key in ("adaptive_preheat_enabled", "adaptive_setback_enabled", "weather_bias_enabled"):
            if key not in new_data or not isinstance(new_data[key], bool):
                new_data[key] = True
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=11)
        _LOGGER.info("Migration to version 11 complete")

    if config_entry.version == 11:
        _LOGGER.info("Migrating Climate Advisor config entry from version 11 to 12")
        new_data = {**config_entry.data}
        new_data.setdefault("min_preheat_minutes", 30)
        new_data.setdefault("max_preheat_minutes", 240)
        new_data.setdefault("default_preheat_minutes", 120)
        new_data.setdefault("preheat_safety_margin", 1.3)
        new_data.setdefault("max_setback_depth_f", 8.0)
        _int_defaults = {"min_preheat_minutes": 30, "max_preheat_minutes": 240, "default_preheat_minutes": 120}
        for key, default in _int_defaults.items():
            if not isinstance(new_data[key], (int, float)):
                new_data[key] = default
        _float_defaults = {"preheat_safety_margin": 1.3, "max_setback_depth_f": 8.0}
        for key, default in _float_defaults.items():
            if not isinstance(new_data[key], (int, float)):
                new_data[key] = default
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=12)
        _LOGGER.info("Migration to version 12 complete")

    if config_entry.version == 12:
        _LOGGER.info("Migrating Climate Advisor config entry from version 12 to 13")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_AI_ENABLED, DEFAULT_AI_ENABLED)
        new_data.setdefault(CONF_AI_API_KEY, "")
        new_data.setdefault(CONF_AI_MODEL, DEFAULT_AI_MODEL)
        new_data.setdefault(CONF_AI_REASONING_EFFORT, DEFAULT_AI_REASONING_EFFORT)
        new_data.setdefault(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS)
        new_data.setdefault(CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE)
        new_data.setdefault(CONF_AI_MONTHLY_BUDGET, DEFAULT_AI_MONTHLY_BUDGET)
        new_data.setdefault(CONF_AI_AUTO_REQUESTS_PER_DAY, DEFAULT_AI_AUTO_REQUESTS_PER_DAY)
        new_data.setdefault(CONF_AI_MANUAL_REQUESTS_PER_DAY, DEFAULT_AI_MANUAL_REQUESTS_PER_DAY)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=13)
        _LOGGER.info("Migration to version 13 complete")

    if config_entry.version == 13:
        _LOGGER.info("Migrating Climate Advisor config entry from version 13 to 14")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED)
        new_data.setdefault(CONF_AI_INVESTIGATOR_MODEL, DEFAULT_AI_INVESTIGATOR_MODEL)
        new_data.setdefault(CONF_AI_INVESTIGATOR_REASONING, DEFAULT_AI_INVESTIGATOR_REASONING)
        new_data.setdefault(CONF_AI_INVESTIGATOR_MAX_TOKENS, DEFAULT_AI_INVESTIGATOR_MAX_TOKENS)
        new_data.setdefault(CONF_AI_INVESTIGATOR_RPD, DEFAULT_AI_INVESTIGATOR_RPD)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=14)
        _LOGGER.info("Migration to version 14 complete")

    if config_entry.version == 14:
        _LOGGER.info("Migrating Climate Advisor config entry from version 14 to 15")
        new_data = {**config_entry.data}
        comfort_heat = new_data.get("comfort_heat", 70.0)
        comfort_cool = new_data.get("comfort_cool", 75.0)
        setback_heat = new_data.get("setback_heat", 60.0)
        setback_cool = new_data.get("setback_cool", 80.0)
        new_data.setdefault(
            CONF_SLEEP_HEAT,
            max(setback_heat + 0.1, comfort_heat - DEFAULT_SETBACK_DEPTH_F),
        )
        new_data.setdefault(
            CONF_SLEEP_COOL,
            min(setback_cool - 0.1, comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F),
        )
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=15)
        _LOGGER.info("Migration to version 15 complete")

    if config_entry.version == 15:
        _LOGGER.info("Migrating Climate Advisor config entry from version 15 to 16")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT)
        new_data.setdefault(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM)
        new_data.setdefault(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD)
        new_data.setdefault(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=16)
        _LOGGER.info("Migration to version 16 complete")

    if config_entry.version == 16:
        _LOGGER.info("Migrating Climate Advisor config entry from version 16 to 17")
        new_data = {**config_entry.data}
        if new_data.get(CONF_FAN_MODE) == FAN_MODE_BOTH:
            new_data[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
            _LOGGER.warning("fan_mode 'both' is no longer supported — migrated to 'whole_house_fan'")
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=17)
        _LOGGER.info("Migration to version 17 complete")

    if config_entry.version == 17:
        _LOGGER.info("Migrating Climate Advisor config entry from version 17 to 18")
        new_data = {**config_entry.data}
        new_data.setdefault(CONF_DEFAULT_TOU_LEAD_MINUTES, DEFAULT_TOU_LEAD_MINUTES)
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=18)
        _LOGGER.info("Migration to version 18 complete")

    return True


def _resolve_zone_coordinator(hass: HomeAssistant, call) -> ClimateAdvisorCoordinator:
    """Resolve the target zone's coordinator for a service call, at call time.

    Issue #796 Gap 5: previously each of the five services below was a
    closure over the `coordinator` local bound inside one specific
    async_setup_entry() call — with two zones configured, the second zone's
    setup silently overwrote the first zone's handler in HA's global service
    registry, so every call (including the destructive reset_learning_data)
    always acted on whichever zone set up last, with no error and no way for
    the caller to target the zone they actually meant.

    Every zone-scoped service now requires call.data["entry_id"] and this
    resolves it fresh against hass.data[DOMAIN] — the canonical per-entry
    lookup table set at async_setup_entry() — rather than any captured local.
    Raises ServiceValidationError (not a bare KeyError/None) for an unknown
    or already-unloaded entry_id, matching HA's convention for a user-facing
    service-call validation failure the frontend/CLI can render as an error
    rather than an unhandled exception.
    """
    entry_id = call.data.get("entry_id")
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id) if entry_id else None
    if coordinator is None:
        raise ServiceValidationError(
            f"Unknown or unloaded Climate Advisor zone entry_id '{entry_id}'. "
            "Check Settings > Devices & Services for the correct zone."
        )
    return coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climate Advisor from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Issue #578: capture real WARNING+/ERROR log records for the AI
    # Investigator's "System Errors/Warnings" section (see log_capture.py).
    # Stored outside hass.data[DOMAIN] on purpose — _get_coordinator() in
    # api.py does next(iter(hass.data[DOMAIN].values())), and dicts are
    # insertion-ordered, so putting this handler in that dict before the
    # coordinator is added would make every REST view resolve the log
    # handler instead of the coordinator.
    log_capture.install(hass)

    # Issue #573: any setup (reload or HA restart) means whatever was saved via
    # the options flow is now the active config — clear the "reload needed"
    # notice raised by ClimateAdvisorOptionsFlow._commit_section().
    ir.async_delete_issue(hass, DOMAIN, "reload_needed")

    # Defer weather entity validation until HA is fully started so all
    # entities are loaded — avoids false "not found" on startup race.
    async def _validate_weather_entity(_event=None):
        weather_entity = entry.data.get("weather_entity", "")
        if not hass.states.get(weather_entity):
            resolved = _resolve_weather_entity(hass, weather_entity)
            if resolved and resolved != weather_entity:
                _LOGGER.info(
                    "Weather entity '%s' not found — auto-resolved to '%s'",
                    weather_entity,
                    resolved,
                )
                hass.config_entries.async_update_entry(entry, data={**entry.data, "weather_entity": resolved})
                ir.async_delete_issue(hass, DOMAIN, "weather_entity_not_found")
                await hass.config_entries.async_reload(entry.entry_id)
            else:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    "weather_entity_not_found",
                    is_fixable=True,
                    is_persistent=True,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="weather_entity_not_found",
                    translation_placeholders={"entity_id": weather_entity},
                )
                _LOGGER.error(
                    "Weather entity '%s' not found — open Settings > System > Repairs "
                    "and click Fix to select the correct entity",
                    weather_entity,
                )
        else:
            ir.async_delete_issue(hass, DOMAIN, "weather_entity_not_found")

    if hass.is_running:
        # Integration reloaded after startup (e.g., from repairs flow) — validate now
        await _validate_weather_entity()
    else:
        # First startup — wait until all integrations are loaded
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _validate_weather_entity)

    coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)

    # Restore persisted state from last run (before setup registers listeners)
    await coordinator.async_restore_state()

    # Set up scheduled events and listeners
    await coordinator.async_setup()

    # Perform initial data fetch
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Issue #796 Gap 5/9: register the five zone-scoped services ONCE,
    # domain-wide, on whichever zone's async_setup_entry() runs first — not
    # once per zone. All five handlers now resolve their target coordinator
    # (or entry) at CALL TIME via _resolve_zone_coordinator(), keyed off the
    # required call.data["entry_id"] field, so re-registering identical
    # closures on every additional zone's setup would be pure churn with no
    # behavioral difference. has_service() is the existing HA-idiomatic
    # "already registered" check (also used by the multi-zone test harness).
    if not hass.services.has_service(DOMAIN, ZONE_SCOPED_SERVICES[0]):
        RESPOND_SUGGESTION_SCHEMA = vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required("action"): vol.In(["accept", "dismiss"]),
                vol.Required("suggestion_key"): cv.string,
            }
        )

        # Register service for accepting/dismissing learning suggestions
        async def handle_suggestion_response(call):
            """Handle user response to a learning suggestion."""
            zone_coordinator = _resolve_zone_coordinator(hass, call)
            action = call.data.get("action")  # "accept" or "dismiss"
            suggestion_key = call.data.get("suggestion_key")

            if action == "accept":
                changes = zone_coordinator.learning.accept_suggestion(suggestion_key)
                await hass.async_add_executor_job(zone_coordinator.learning.save_state)
                _LOGGER.info("Suggestion accepted: %s → changes: %s", suggestion_key, changes)
                # Apply changes to coordinator config
                zone_coordinator.config.update(changes)
            elif action == "dismiss":
                zone_coordinator.learning.dismiss_suggestion(suggestion_key)
                await hass.async_add_executor_job(zone_coordinator.learning.save_state)
                _LOGGER.info("Suggestion dismissed: %s", suggestion_key)

        hass.services.async_register(
            DOMAIN,
            "respond_to_suggestion",
            handle_suggestion_response,
            schema=RESPOND_SUGGESTION_SCHEMA,
        )

        # Register debug services
        ENTRY_ID_ONLY_SCHEMA = vol.Schema({vol.Required("entry_id"): cv.string})

        async def handle_force_reclassify(call):
            """Force a coordinator refresh / reclassification."""
            zone_coordinator = _resolve_zone_coordinator(hass, call)
            await zone_coordinator.async_request_refresh()

        async def handle_resend_briefing(call):
            """Re-send the daily briefing."""
            from homeassistant.util import dt as dt_util

            zone_coordinator = _resolve_zone_coordinator(hass, call)
            zone_coordinator._briefing_sent_today = False
            await zone_coordinator._async_send_briefing(dt_util.now())

        async def handle_dump_diagnostics(call):
            """Log a comprehensive diagnostic snapshot for troubleshooting.

            Kept (not deprecated) alongside the native `async_get_config_entry_diagnostics`
            hook in diagnostics.py for continuity — some users may have automations that
            already call this service. Builds its payload through the same shared helper
            so the two surfaces stay a single source of truth for the payload shape.
            """
            from .diagnostics import async_get_diagnostics_payload

            zone_coordinator = _resolve_zone_coordinator(hass, call)
            zone_entry = hass.config_entries.async_get_entry(zone_coordinator._entry_id)
            diag = await async_get_diagnostics_payload(hass, zone_entry)
            _LOGGER.info(
                "Diagnostic dump requested:\n%s",
                json.dumps(diag, indent=2, default=str),
            )

        hass.services.async_register(DOMAIN, "force_reclassify", handle_force_reclassify, schema=ENTRY_ID_ONLY_SCHEMA)
        hass.services.async_register(DOMAIN, "resend_briefing", handle_resend_briefing, schema=ENTRY_ID_ONLY_SCHEMA)
        hass.services.async_register(DOMAIN, "dump_diagnostics", handle_dump_diagnostics, schema=ENTRY_ID_ONLY_SCHEMA)

        # Schema for reset_learning_data service
        RESET_LEARNING_SCHEMA = vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Optional("scope", default="all"): vol.In(["thermal_model", "weather_bias", "suggestions", "all"]),
            }
        )

        async def handle_reset_learning_data(call) -> None:
            """Handle reset_learning_data service call."""
            zone_coordinator = _resolve_zone_coordinator(hass, call)
            scope = call.data.get("scope", "all")
            await hass.async_add_executor_job(zone_coordinator.learning.reset, scope)
            _LOGGER.info("Learning data reset via service: entry_id=%s scope=%s", call.data.get("entry_id"), scope)

        hass.services.async_register(
            DOMAIN,
            "reset_learning_data",
            handle_reset_learning_data,
            schema=RESET_LEARNING_SCHEMA,
        )

    # Issue #796 Gap 6: register the shared REST API views and dashboard panel
    # ONCE, domain-wide, on whichever zone's async_setup_entry() runs first —
    # mirrors the has_service() guard used for ZONE_SCOPED_SERVICES above.
    # This is a deliberate architectural choice, not just a crash-avoidance
    # patch: docs/multi-zone-spec.md's "Resolved Questions" section already
    # designs the future dashboard zone selector as ONE panel with an
    # entry_id-driven selector row, not one physical panel per zone — so
    # "register once, guarded" is the fix that matches where the dashboard is
    # headed, not a stopgap that per-entry-unique URLs would later need to be
    # unwound.
    #
    # This guard also fully closes Gap 6's safety concern independent of
    # PR3's (deliberately not run — see docs/multi-zone-spec.md Gap 6/PR3)
    # empirical question of whether a duplicate frontend_url_path registration
    # raises AFTER a coordinator's control loop is already live: with this
    # guard, a second-and-later zone's setup never attempts the registration
    # at all, so that specific crash-after-control-loop-start scenario cannot
    # occur regardless of PR3's unconfirmed answer. The try/except below is a
    # second line of defense only, in case this guard's own assumption is
    # ever violated (e.g. hass.data was reset without HA's internal
    # panel/view registries also being reset).
    if not hass.data.get(_PANEL_HASS_DATA_KEY):
        try:
            # Register REST API views for the dashboard panel
            for view_cls in API_VIEWS:
                hass.http.register_view(view_cls())

            # Register dashboard panel (iframe serving frontend/index.html)
            frontend_path = Path(__file__).parent / "frontend"
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_URL, str(frontend_path), cache_headers=False)]
            )
            import hashlib

            from homeassistant.components.frontend import async_register_built_in_panel

            _panel_bytes = await hass.async_add_executor_job((frontend_path / "index.html").read_bytes)
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
        except Exception as err:  # noqa: BLE001 — see comment above: any failure here
            # means the shared panel/views are already registered by another
            # zone (or something HA-internal we can't predict without PR3's
            # unrun empirical spike) — treated as expected, not fatal, so a
            # second zone's setup still completes successfully.
            _LOGGER.warning(
                "Panel registration skipped: already registered by another zone entry_id=%s reason=%s",
                entry.entry_id,
                err,
            )
        finally:
            # Set unconditionally (success or handled failure): either this
            # zone just registered the shared resources, or we've determined
            # they're already registered elsewhere — either way, no later
            # zone's setup should attempt this again.
            hass.data[_PANEL_HASS_DATA_KEY] = True

    _LOGGER.info("Climate Advisor v%s loaded successfully", VERSION)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Climate Advisor config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()

    # The log-capture handler is process-wide, not per-entry — only detach it
    # once no other config entries (this integration is effectively single-
    # instance, but guard the general case) are still using it.
    if not hass.data[DOMAIN]:
        log_capture.uninstall(hass)

        # Issue #796 Gap 9: the five zone-scoped services (ZONE_SCOPED_SERVICES)
        # are registered once, domain-wide, on whichever zone's
        # async_setup_entry() ran first (see the has_service() guard there) —
        # mirror that "domain-wide resource" lifetime here: only tear them
        # down once the LAST zone is gone, not on every unload. Removing them
        # per-unload while a sibling zone still exists would leave that
        # surviving zone with no way to call reset_learning_data/etc. at all,
        # even though its coordinator is still live in hass.data[DOMAIN].
        for service_name in ZONE_SCOPED_SERVICES:
            hass.services.async_remove(DOMAIN, service_name)

        # Issue #796 Gap 8: the dashboard panel and REST API views are the
        # same kind of domain-wide shared resource as the services above
        # (Gap 6) — remove them only once the LAST zone is gone, not on every
        # unload. Previously unconditional (fired on every unload regardless
        # of remaining zones), which would strand a surviving zone with no
        # dashboard/API access at all until its own entry happened to reload.
        from homeassistant.components.frontend import async_remove_panel

        async_remove_panel(hass, PANEL_FRONTEND_PATH)
        # Clear the registration flag so a later zone (re-added after the
        # last one was removed) re-registers the shared panel/views instead
        # of finding a stale "already registered" flag from this now-torn-down
        # instance.
        hass.data.pop(_PANEL_HASS_DATA_KEY, None)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
