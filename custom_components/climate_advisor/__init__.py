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
from pathlib import Path

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started

from . import ai_settings_sync, i18n, log_capture, zone_registry
from .api import API_VIEWS, ZONELESS_VIEWS
from .const import (
    AI_PROVIDER_BUILTIN_CLAUDE,
    AI_REASONING_HIGH,
    AI_REASONING_MEDIUM,
    AI_SETTINGS_BACKUP_KEY,
    AI_SHARED_DATA_KEY,
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
    CONF_AI_PROVIDER,
    CONF_AI_REASONING_EFFORT,
    CONF_AI_TEMPERATURE,
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_BRIEFING_NOTIFICATIONS_ENABLED,
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
    ENTRY_SCOPED_FILES,
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    ISSUE_AI_SETTINGS_UNIFIED,
    ISSUE_BUILTIN_CLAUDE_DEPRECATED,
    PANEL_FRONTEND_PATH,
    PANEL_URL,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    VERSION,
)
from .coordinator import ClimateAdvisorCoordinator
from .dashboard_api import DASHBOARD_VIEWS
from .services import _resolve_zone_coordinator as _resolve_zone_coordinator  # re-exported for callers and tests
from .services import async_setup_services
from .storage_paths import remove_entry_scoped_files
from .zone_registry import ClimateAdvisorConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]

# Set up from the UI only; no YAML configuration (needed now that the integration has async_setup, #1637).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the five zone-scoped services once, for the life of Home Assistant (quality scale #1637).

    Issue #796 Gap 5/9 made them domain-wide (each call names its zone by ``entry_id``). They used to be
    registered by the first zone's setup and removed with the last zone, so a call made while no zone was
    loaded failed as "service not found". Now the call exists and fails with a clear, translated "zone not
    loaded" error instead (services.py).

    Issue #1673: the translation catalogs are read here, once, before any zone is set up. Home Assistant sets
    the zones up concurrently right after this returns, and each zone's first refresh renders text.
    """
    await hass.async_add_executor_job(i18n.ensure_catalogs)
    async_setup_services(hass)
    return True


# Issue #796 Gap 6/8: the REST API views (api.py's API_VIEWS — every view's
# `url` class attribute is a fixed `API_*` constant from const.py, not
# per-entry-derived, confirmed by reading api.py) and the dashboard panel
# (PANEL_URL, PANEL_FRONTEND_PATH — both fixed module-level string constants
# in const.py) are shared, domain-wide resources, not per-zone resources — the
# same collision shape as ZONE_SCOPED_SERVICES above. Install-wide, so under its
# own hass.data key (Issue #1639: the zones themselves are entry.runtime_data,
# see zone_registry.py). Tracks whether this zone's async_setup_entry() call (or an
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

    if config_entry.version == 18:
        _LOGGER.info("Migrating Climate Advisor config entry from version 18 to 19")
        # Issue #808: config entries created before the duplicate-zone guard
        # existed have unique_id=None, which makes _abort_if_unique_id_configured()
        # a no-op for them — backfill it from climate_entity so the guard
        # actually protects installs upgrading from a pre-0.7.1 version, not
        # just fresh installs created after it.
        new_data = {**config_entry.data}
        climate_entity = new_data.get("climate_entity")
        existing_unique_id = getattr(config_entry, "unique_id", None)
        new_unique_id = existing_unique_id or climate_entity

        other_entries = hass.config_entries.async_entries(DOMAIN)
        if isinstance(other_entries, list) and climate_entity:
            for other in other_entries:
                if (
                    getattr(other, "entry_id", None) != config_entry.entry_id
                    and getattr(other, "unique_id", None) == climate_entity
                ):
                    _LOGGER.warning(
                        "Zone '%s' shares climate_entity '%s' with another already-migrated "
                        "zone; only one will be resolvable by unique_id going forward. "
                        "Remove or repoint one of the duplicate zones in Settings -> "
                        "Devices & Services",
                        config_entry.entry_id,
                        climate_entity,
                    )
                    break

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=19, unique_id=new_unique_id)
        _LOGGER.info("Migration to version 19 complete")

    if config_entry.version == 19:
        _LOGGER.info("Migrating Climate Advisor config entry from version 19 to 20")
        # Issue #817 Part 3: on a multi-zone install, every zone independently sending its own
        # briefing push/email spams the same person with one copy per zone. Backfill the new
        # per-zone gate so exactly one already-configured zone defaults to notifying — the
        # stably-first one, via the same helper config_flow.py uses for a brand-new zone
        # (zone_registry.default_briefing_notifications_enabled — DRY, one implementation of
        # "pick the stably-first zone" for both callers). Single-zone installs keep sending
        # (unchanged behavior, matching every prior migration's preserve-current-behavior
        # precedent); multi-zone installs get True on the stably-first zone, False on the rest.
        new_data = {**config_entry.data}
        new_data.setdefault(
            CONF_BRIEFING_NOTIFICATIONS_ENABLED,
            zone_registry.default_briefing_notifications_enabled(hass, config_entry.entry_id),
        )
        # unique_id explicitly forwarded unchanged (not omitted) — this migration has nothing
        # to do with zone identity, only notification defaults; passing it through avoids any
        # ambiguity about whether an omitted kwarg here could reset it.
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=20, unique_id=config_entry.unique_id
        )
        _LOGGER.info("Migration to version 20 complete")

    if config_entry.version == 20:
        _LOGGER.info("Migrating Climate Advisor config entry from version 20 to 21")
        new_data = _migrate_ai_settings_v21(hass, config_entry)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=21, unique_id=config_entry.unique_id
        )
        _LOGGER.info("Migration to version 21 complete")

    return True


def _ai_v21_values(values: dict) -> dict:
    """Issue #1336: a set of AI values in their v21 form — the provider named, ``high`` reasoning as ``medium``."""
    out = dict(values)
    if out:
        out[CONF_AI_PROVIDER] = ai_settings_sync.provider_of(out)
    if out.get(CONF_AI_REASONING_EFFORT) == AI_REASONING_HIGH:
        out[CONF_AI_REASONING_EFFORT] = AI_REASONING_MEDIUM
    return out


def _ai_named_provider(values: dict) -> dict:
    """Issue #1336: AI values with only the new ``ai_provider`` key filled in.

    Used to decide whether a zone's own originals need a backup: naming the provider is not a change the
    occupant made, so it alone does not create a second copy of the zone's settings (and key).
    """
    out = dict(values)
    if out:
        out[CONF_AI_PROVIDER] = ai_settings_sync.provider_of(out)
    return out


def _migrate_ai_settings_v21(hass: HomeAssistant, config_entry: ConfigEntry) -> dict:
    """The v20 -> v21 data of one zone (Issue #1336): AI settings become install-wide.

    Occupant view: a zone's AI settings now apply to every zone. When exactly one zone had AI on, every zone
    gets its settings (and the Investigator); when several had different settings, the stably-first zone's
    are kept and a Repairs notice says so. A zone with a Claude key stays on built-in Claude; others get Home
    Assistant AI Task. Reasoning "high" becomes "medium".

    Nothing stored is lost: ``ai_settings_sync.unified_entry_data`` backs up a zone's own original AI values
    under ``AI_SETTINGS_BACKUP_KEY`` whenever they differ from the values it now holds. Every zone is judged by
    its ORIGINAL values (the backup once it has one), so the outcome does not depend on which zone migrates
    first.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not isinstance(entries, list) or not entries:
        entries = [config_entry]
    # The data of this entry as it is now (earlier hops may have changed it); siblings as stored.
    entries_data = [config_entry.data if e.entry_id == config_entry.entry_id else e.data for e in entries]
    values, conflict = ai_settings_sync.pick_install_ai_values(entries_data, _ai_v21_values)

    if values is None:
        # No zone has AI configured: this zone's own values, in their v21 form.
        own = ai_settings_sync.extract_ai_values(config_entry.data)
        new_data = (
            ai_settings_sync.unified_entry_data(config_entry.data, _ai_v21_values(own), _ai_named_provider)
            if own
            else dict(config_entry.data)
        )
        _LOGGER.info("AI settings migrated: no zone has AI configured — zone keeps its own values")
        return new_data

    install_values = _ai_v21_values(values)
    new_data = ai_settings_sync.unified_entry_data(config_entry.data, install_values, _ai_named_provider)
    winner = next(
        (
            e
            for e in entries
            if ai_settings_sync.is_ai_configured(
                ai_settings_sync.original_ai_values(
                    config_entry.data if e.entry_id == config_entry.entry_id else e.data
                )
            )
        ),
        config_entry,
    )
    winner_title = str(getattr(winner, "title", None) or winner.entry_id)
    _LOGGER.info(
        "AI settings migrated: install-wide values from zone %s provider=%s backed_up=%s conflict=%s",
        winner_title,
        install_values.get(CONF_AI_PROVIDER),
        AI_SETTINGS_BACKUP_KEY in new_data,
        conflict,
    )
    if conflict:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_AI_SETTINGS_UNIFIED,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_AI_SETTINGS_UNIFIED,
            translation_placeholders={"zone": winner_title},
        )
    return new_data


def _update_builtin_claude_deprecation_issue(hass: HomeAssistant) -> None:
    """Raise the install-wide "built-in Claude is deprecated" Repairs notice while any zone uses it (Issue #1336).

    Deleted otherwise, so it clears on the setup after the occupant switches to Home Assistant AI Task.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    uses_builtin = isinstance(entries, list) and any(
        ai_settings_sync.is_ai_configured(e.data) and ai_settings_sync.provider_of(e.data) == AI_PROVIDER_BUILTIN_CLAUDE
        for e in entries
    )
    if uses_builtin:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_BUILTIN_CLAUDE_DEPRECATED,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_BUILTIN_CLAUDE_DEPRECATED,
        )
        _LOGGER.warning("Built-in Claude is deprecated: choose a Home Assistant AI Task entity in the AI settings")
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_BUILTIN_CLAUDE_DEPRECATED)


_PANEL_HASH_FILES = (
    "entry.html",
    "index.html",
    "new_dashboard.html",
    "i18n.js",
    "intl-messageformat.iife.js",
)


def _panel_asset_bytes(frontend_path: Path) -> bytes:
    """Bytes the panel ?v= hash covers (blocking reads: call in the executor).

    Issue #1341: besides the three pages, the i18n runtime and every catalog file under frontend/i18n/
    (sorted, so the hash is stable) change the hash, so an updated translation busts the iframe cache.
    """
    chunks = [(frontend_path / name).read_bytes() for name in _PANEL_HASH_FILES if (frontend_path / name).is_file()]
    catalog_dir = frontend_path / "i18n"
    if catalog_dir.is_dir():
        chunks.extend(path.read_bytes() for path in sorted(catalog_dir.rglob("*.json")))
    return b"".join(chunks)


async def async_setup_entry(hass: HomeAssistant, entry: ClimateAdvisorConfigEntry) -> bool:
    """Set up Climate Advisor from a config entry."""
    # Issue #578: capture real WARNING+/ERROR log records for the AI
    # Investigator's "System Errors/Warnings" section (see log_capture.py).
    # Install-wide, so under its own hass.data key (Issue #1639: the zones themselves are entry.runtime_data).
    log_capture.install(hass)

    # Issue #1341: read the translation catalogs (blocking file reads) once, in the executor, before
    # anything renders. Issue #1673: ``async_setup`` already loaded them before any zone; this call is a no-op
    # then, and waits (never skips) if another zone's load is still running. ``ensure_catalogs`` never raises,
    # records the attempt and is a no-op afterwards, so a failed load (bad JSON, missing file, mismatched index)
    # never stops setup and never retries inside the event loop: rendering then falls back (English, then the key).
    await hass.async_add_executor_job(i18n.ensure_catalogs)

    # Issue #812: one-time migration off the old domain-wide (unscoped)
    # issue ids. Any install with an open Repairs card raised under the OLD
    # "weather_entity_not_found"/"reload_needed" ids (pre-#812) would
    # otherwise have that card orphaned forever, since the new entry-scoped
    # create/delete calls below never touch it. async_delete_issue() is
    # documented as a no-op if the issue doesn't exist (see
    # homeassistant/helpers/issue_registry.py: IssueRegistry.async_delete()
    # does `self.issues.pop((domain, issue_id), None)`), so this is safe to
    # run unconditionally on every zone's setup, not just once.
    ir.async_delete_issue(hass, DOMAIN, "weather_entity_not_found")
    ir.async_delete_issue(hass, DOMAIN, "reload_needed")

    # Issue #813: zone_resolution_ambiguous is_persistent=True, so it survives
    # an HA restart in .storage/repairs.issue_registry — an install that had
    # it raised under the OLD unconditional-at-setup logic (any 2+ zone
    # install, regardless of whether ambiguity ever actually occurred) would
    # otherwise keep showing that stale card forever after upgrading to this
    # fix, since the new code only ever CREATES it when get_default_coordinator()
    # actually takes the ambiguous fallback (see zone_registry.py) and never
    # explicitly clears a pre-existing one. Clear unconditionally on every
    # setup — safe/idempotent (no-op if absent, same as the two deletes
    # above) — and let it be re-raised only if this session's requests
    # actually hit the ambiguous path again. This also gives the card correct
    # "fresh start each restart" semantics going forward, not just a one-time
    # migration.
    ir.async_delete_issue(hass, DOMAIN, "zone_resolution_ambiguous")

    # Issue #573: any setup (reload or HA restart) means whatever was saved via
    # the options flow is now the active config — clear the "reload needed"
    # notice raised by ClimateAdvisorOptionsFlow._commit_section(). Entry-scoped
    # (Issue #812) so this only clears THIS zone's own notice.
    ir.async_delete_issue(hass, DOMAIN, f"reload_needed_{entry.entry_id}")

    # Issue #1336: the deprecation notice follows the install's current AI provider.
    _update_builtin_claude_deprecation_issue(hass)

    # Defer weather entity validation until HA is fully started so all
    # entities are loaded — avoids false "not found" on startup race.
    async def _validate_weather_entity(_hass: HomeAssistant) -> None:
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
                ir.async_delete_issue(hass, DOMAIN, f"weather_entity_not_found_{entry.entry_id}")
                # Issue #1102 (F7): schedule, never await, a reload from inside setup —
                # awaiting async_reload here waits on this entry's own setup lock
                # (self-deadlock), and HA documents async_schedule_reload for integrations.
                hass.config_entries.async_schedule_reload(entry.entry_id)
            else:
                # Issue #812: entry-scoped issue_id + data={"entry_id": ...}
                # so a multi-zone install's Repairs "Fix" targets THIS zone
                # specifically — see repairs.py's WeatherEntityRepairFlow/
                # _resolve_target_entry().
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    f"weather_entity_not_found_{entry.entry_id}",
                    is_fixable=True,
                    is_persistent=True,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="weather_entity_not_found",
                    translation_placeholders={"entity_id": weather_entity},
                    data={"entry_id": entry.entry_id},
                )
                _LOGGER.error(
                    "Weather entity '%s' not found — open Settings > System > Repairs "
                    "and click Fix to select the correct entity",
                    weather_entity,
                )
        else:
            ir.async_delete_issue(hass, DOMAIN, f"weather_entity_not_found_{entry.entry_id}")

    # Issue #1102 (F6): HA's async_at_started runs now if HA is already running (a reload)
    # or once on EVENT_HOMEASSISTANT_STARTED (first boot). Its cancel goes to the entry's
    # on_unload, so a STARTED listener can no longer outlive an unload of this entry.
    entry.async_on_unload(async_at_started(hass, _validate_weather_entity))

    coordinator = ClimateAdvisorCoordinator(hass, dict(entry.data), entry_id=entry.entry_id)

    # Restore persisted state from last run (before setup registers listeners)
    await coordinator.async_restore_state()

    # Set up scheduled events and listeners
    await coordinator.async_setup()

    # Perform initial data fetch
    await coordinator.async_config_entry_first_refresh()

    # Issue #1639 (quality scale runtime-data): the zone's coordinator lives on its own config entry. Home Assistant
    # drops it again when the entry unloads, and zone_registry lists a zone only while its entry is LOADED, so an
    # unloaded, reloading or failed zone is never answered from a stale coordinator.
    entry.runtime_data = coordinator

    # Issue #813 correction: this used to unconditionally raise
    # zone_resolution_ambiguous here just because a 2nd zone was loaded, with
    # a comment claiming it was "the persistent half of the same signal" as
    # get_default_coordinator()'s WARNING log. That was false — this fired at
    # every setup regardless of whether get_default_coordinator() had ever
    # actually been asked to guess. Combined with #812/#820 closing off the
    # dashboard's own ambiguous-fallback paths, that meant every multi-zone
    # install permanently showed "Ambiguous zone selection" in Repairs even
    # when no ambiguity could occur anymore — the user correctly flagged
    # still seeing this after #812 shipped. The issue is now raised from
    # inside zone_registry.get_default_coordinator() itself, at the exact
    # moment (and only when) it actually resolves an ambiguous fallback —
    # same throttle token as its WARNING log line, so the two stay in
    # lockstep by construction instead of by two independently-maintained
    # conditions. See zone_registry.py's get_default_coordinator().

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Issue #1637 (quality scale action-setup): the five zone-scoped services are registered once, in
    # async_setup() (services.py), not here.

    # Issue #796 Gap 6: register the shared REST API views and dashboard panel
    # ONCE, domain-wide, on whichever zone's async_setup_entry() runs first —
    # (the services, unlike these, are registered in async_setup(), #1637).
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
        # Wrapped in zone_scope() so the warning below (and any future log line
        # added to this block) attributes to the zone whose async_setup_entry()
        # actually ran it, instead of falling back to "unknown zone" — the
        # coordinator for this zone is already on entry.runtime_data as of the
        # assignment above, so its zone_label is fully resolvable here.
        with log_capture.zone_scope(coordinator.zone_label):
            try:
                # Register REST API views for the dashboard panel
                for view_cls in (*API_VIEWS, *DASHBOARD_VIEWS, *ZONELESS_VIEWS):
                    hass.http.register_view(view_cls())

                # Register dashboard panel (iframe serving frontend/entry.html, which opens the new dashboard
                # by default or classic index.html when this browser chose classic)
                frontend_path = Path(__file__).parent / "frontend"
                from homeassistant.components.http import StaticPathConfig

                await hass.http.async_register_static_paths(
                    [StaticPathConfig(PANEL_URL, str(frontend_path), cache_headers=False)]
                )
                import hashlib

                from homeassistant.components.frontend import async_register_built_in_panel

                _panel_bytes = await hass.async_add_executor_job(_panel_asset_bytes, frontend_path)
                _panel_hash = hashlib.md5(_panel_bytes).hexdigest()[:8]
                async_register_built_in_panel(
                    hass,
                    "iframe",
                    sidebar_title="Climate Advisor",
                    sidebar_icon="mdi:thermostat",
                    frontend_url_path=PANEL_FRONTEND_PATH,
                    require_admin=False,
                    config={"url": f"{PANEL_URL}/entry.html?v={_panel_hash}"},
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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A zone was deleted: remove its storage files and its own Repairs issues.

    Occupant view: after deleting a zone, nothing of it is left behind: no state, learning, chart log or
    forecast archive file on the SD card, and no Repairs card offering to "fix" a zone that no longer exists.
    HA calls this after ``async_unload_entry`` (which already ran the final save), and only on deletion, not
    on a reload or settings save. Never raises: a failed delete is logged and must not block the removal.
    """
    for issue_id in (f"weather_entity_not_found_{entry.entry_id}", f"reload_needed_{entry.entry_id}"):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    try:
        await hass.async_add_executor_job(
            remove_entry_scoped_files, Path(hass.config.config_dir), entry.entry_id, ENTRY_SCOPED_FILES
        )
    except Exception:  # noqa: BLE001 - removal of a zone must never fail on cleanup
        _LOGGER.exception("Climate Advisor: could not clean up files of removed zone %s", entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ClimateAdvisorConfigEntry) -> bool:
    """Unload a Climate Advisor config entry."""
    coordinator: ClimateAdvisorCoordinator = entry.runtime_data
    await coordinator.async_shutdown()
    # Issue #1639: the zones still loaded besides this one. HA has already moved this entry to
    # UNLOAD_IN_PROGRESS, so it is not in async_loaded_entries(); it is excluded by id as well, so the count does
    # not depend on that ordering.
    remaining = [eid for eid in zone_registry.loaded_zones(hass) if eid != entry.entry_id]

    # Issue #796 Transitional Safety Window: clear side of the
    # zone_resolution_ambiguous Repairs issue raised in async_setup_entry()
    # above. Its own standalone check against the <= 1 threshold — NOT nested
    # inside the "if not remaining:" block below, which only fires at
    # exactly 0 remaining zones (a different threshold, used for the
    # domain-wide service/panel teardown that block owns). Going from 2 zones
    # to 1 must clear this issue even though that block doesn't fire yet.
    if len(remaining) <= 1:
        ir.async_delete_issue(hass, DOMAIN, "zone_resolution_ambiguous")
        # Clear zone_registry's WARNING throttle state in lockstep with the
        # Repairs issue above — see reset_warning_state()'s docstring.
        zone_registry.reset_warning_state(hass)

    # The log-capture handler is process-wide, not per-entry — only detach it
    # once no other config entries (this integration is effectively single-
    # instance, but guard the general case) are still using it.
    if not remaining:
        log_capture.uninstall(hass)

        # Issue #1637: the five zone-scoped services are NOT removed here any more. They are registered once
        # in async_setup() and live as long as Home Assistant (quality scale action-setup); a call naming an
        # unloaded zone fails with the translated unknown_zone error.

        # Issue #796 Gap 8: the dashboard panel and REST API views are
        # domain-wide shared resources
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
        # Issue #1336: the install-wide AI client goes with the last zone, like the panel.
        hass.data.pop(AI_SHARED_DATA_KEY, None)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
