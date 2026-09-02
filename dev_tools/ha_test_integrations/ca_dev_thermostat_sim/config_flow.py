"""Config flow for CA Dev Thermostat Sim.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
Follows the selector conventions used by custom_components/climate_advisor's
own config_flow.py (vol.Schema + homeassistant.helpers.selector).
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


class CaDevThermostatSimConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CA Dev Thermostat Sim."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Collect the simulated thermostat's identity, physics params, and tick rate."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)

        schema = vol.Schema(
            {
                vol.Required("name", default="Simulated Thermostat"): selector.TextSelector(),
                vol.Required(CONF_INITIAL_TEMP_F, default=DEFAULT_INITIAL_TEMP_F): _num(
                    min_=32, max_=110, step=0.1, unit="°F"
                ),
                vol.Required(CONF_K_PASSIVE, default=DEFAULT_K_PASSIVE): _num(
                    min_=-2.0, max_=-0.01, step=0.01, unit="1/hr"
                ),
                vol.Required(CONF_K_ACTIVE_HEAT, default=DEFAULT_K_ACTIVE_HEAT): _num(
                    min_=0.1, max_=20.0, step=0.1, unit="°F/hr"
                ),
                vol.Required(CONF_K_ACTIVE_COOL, default=DEFAULT_K_ACTIVE_COOL): _num(
                    min_=-20.0, max_=-0.1, step=0.1, unit="°F/hr"
                ),
                vol.Required(CONF_COMFORT_HEAT, default=DEFAULT_COMFORT_HEAT): _num(
                    min_=32, max_=110, step=0.5, unit="°F"
                ),
                vol.Required(CONF_COMFORT_COOL, default=DEFAULT_COMFORT_COOL): _num(
                    min_=32, max_=110, step=0.5, unit="°F"
                ),
                vol.Required(CONF_OUTDOOR_SOURCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["weather", "sensor"])
                ),
                vol.Required(CONF_TICK_SECONDS, default=DEFAULT_TICK_SECONDS): _num(
                    min_=5, max_=3600, step=1, unit="s"
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
