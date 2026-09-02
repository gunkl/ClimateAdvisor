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

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CA Dev Thermostat Sim from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = dict(entry.data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CA Dev Thermostat Sim config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
