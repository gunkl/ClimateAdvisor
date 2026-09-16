"""CA Dev Thermostat Sim — dev-only synthetic thermostat for testing Climate Advisor.

NOT SHIPPED. Lives outside custom_components/ on purpose: tools/deploy.py,
HACS, and hassfest all only know about custom_components/climate_advisor.
See dev_tools/ha_test_integrations/README.md for install/usage instructions
and issue #809 for background.

This integration reuses the real Climate Advisor ODE step function
(_simulate_indoor_physics from custom_components/climate_advisor/coordinator.py)
rather than reimplementing the thermal math, so the simulator can never drift
from production physics. It therefore requires climate_advisor to be
installed alongside it on the same Home Assistant instance.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_OCCUPANCY_SCHEDULES, DOMAIN, PLATFORMS
from .occupancy_schedule import _IMPORT_ERROR, ZoneOccupancyState, schedule_from_dict

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CA Dev Thermostat Sim from a config entry."""
    if _IMPORT_ERROR is not None:
        _LOGGER.error(
            "CA Dev Thermostat Sim requires the climate_advisor integration to be "
            "installed alongside it (occupancy_schedule.py imports scheduler.py). "
            "Import failed: %s",
            _IMPORT_ERROR,
        )
        raise ConfigEntryNotReady(
            "climate_advisor is not installed — CA Dev Thermostat Sim reuses its scheduler.py and cannot run standalone"
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = dict(entry.data)

    # Issue #898: build this zone's occupancy scheduler before forwarding to
    # platforms — switch.py's entities register themselves into it during their own
    # async_setup_entry, and climate.py's tick evaluates it every cycle.
    raw_schedules = entry.data.get(CONF_OCCUPANCY_SCHEDULES) or []
    schedules = [schedule_from_dict(raw) for raw in raw_schedules]
    hass.data[DOMAIN][entry.entry_id]["occupancy_state"] = ZoneOccupancyState(schedules=schedules, switches={})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CA Dev Thermostat Sim config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
