"""Config flow for CA Dev Thermostat Sim.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
Follows the selector conventions used by custom_components/climate_advisor's
own config_flow.py (vol.Schema + homeassistant.helpers.selector).

NOTE ON HA VERSION: async_step_reconfigure()/_get_reconfigure_entry()/
async_update_reload_and_abort() are the modern (2024.x+) unified reconfigure-flow
API. Like climate.py's own note, this was NOT verified against a locally-installed
`homeassistant` package — none exists in this repo/venv. Test on a real Home
Assistant instance before relying on it.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_COMFORT_COOL,
    CONF_COMFORT_HEAT,
    CONF_INITIAL_TEMP_F,
    CONF_K_ACTIVE_COOL,
    CONF_K_ACTIVE_HEAT,
    CONF_K_PASSIVE,
    CONF_OUTDOOR_SOURCE,
    CONF_TICK_SECONDS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_INITIAL_TEMP_F,
    DEFAULT_K_ACTIVE_COOL,
    DEFAULT_K_ACTIVE_HEAT,
    DEFAULT_K_PASSIVE,
    DEFAULT_TICK_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _num(
    *, min_: float, max_: float, step: float, unit: str | None = None, mode: str = "box"
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=min_, max=max_, step=step, unit_of_measurement=unit, mode=mode)
    )


def _build_schema(*, defaults: dict[str, Any]) -> vol.Schema:
    """Build the identity/physics/tick-rate schema shared by creation and reconfigure.

    ``defaults`` is either the module's DEFAULT_* constants (initial creation) or an
    existing entry's current ``data`` (reconfigure) — one schema definition either
    way, so the two flows can't silently drift apart on field set, bounds, or units.
    """
    return vol.Schema(
        {
            vol.Required("name", default=defaults.get("name", "Simulated Thermostat")): selector.TextSelector(),
            vol.Required(CONF_INITIAL_TEMP_F, default=defaults.get(CONF_INITIAL_TEMP_F, DEFAULT_INITIAL_TEMP_F)): _num(
                min_=32, max_=110, step=0.1, unit="°F"
            ),
            vol.Required(CONF_K_PASSIVE, default=defaults.get(CONF_K_PASSIVE, DEFAULT_K_PASSIVE)): _num(
                min_=-2.0, max_=-0.01, step=0.01, unit="1/hr"
            ),
            vol.Required(CONF_K_ACTIVE_HEAT, default=defaults.get(CONF_K_ACTIVE_HEAT, DEFAULT_K_ACTIVE_HEAT)): _num(
                min_=0.1, max_=20.0, step=0.1, unit="°F/hr"
            ),
            vol.Required(CONF_K_ACTIVE_COOL, default=defaults.get(CONF_K_ACTIVE_COOL, DEFAULT_K_ACTIVE_COOL)): _num(
                min_=-20.0, max_=-0.1, step=0.1, unit="°F/hr"
            ),
            vol.Required(CONF_COMFORT_HEAT, default=defaults.get(CONF_COMFORT_HEAT, DEFAULT_COMFORT_HEAT)): _num(
                min_=32, max_=110, step=0.5, unit="°F"
            ),
            vol.Required(CONF_COMFORT_COOL, default=defaults.get(CONF_COMFORT_COOL, DEFAULT_COMFORT_COOL)): _num(
                min_=32, max_=110, step=0.5, unit="°F"
            ),
            vol.Required(
                CONF_OUTDOOR_SOURCE, default=defaults.get(CONF_OUTDOOR_SOURCE, vol.UNDEFINED)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["weather", "sensor"])),
            vol.Required(CONF_TICK_SECONDS, default=defaults.get(CONF_TICK_SECONDS, DEFAULT_TICK_SECONDS)): _num(
                min_=5, max_=3600, step=1, unit="s"
            ),
        }
    )


class CaDevThermostatSimConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CA Dev Thermostat Sim."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Collect the simulated thermostat's identity, physics params, and tick rate."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)

        return self.async_show_form(step_id="user", data_schema=_build_schema(defaults={}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> Any:
        """Retune an existing entry's physics/comfort/tick-rate without deleting it.

        Previously this integration had no reconfigure step at all — the only way to
        change, say, k_active_heat/k_active_cool after creation was to delete and
        re-add the entry, losing its restored simulation state. This reuses the exact
        same schema async_step_user() builds (see _build_schema() above), pre-filled
        from the entry's current data instead of the module defaults.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=user_input, title=user_input["name"])

        return self.async_show_form(
            step_id="reconfigure", data_schema=_build_schema(defaults=dict(entry.data)), errors=errors
        )
