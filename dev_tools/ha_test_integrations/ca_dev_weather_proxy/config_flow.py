"""Config flow for CA Dev Weather Proxy.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONDITION_OPTIONS,
    CONF_BASE_TEMP_F,
    CONF_CONDITION,
    CONF_DIURNAL_SWING_F,
    CONF_PHASE_OFFSET_H,
    DEFAULT_BASE_TEMP_F,
    DEFAULT_CONDITION,
    DEFAULT_DIURNAL_SWING_F,
    DEFAULT_PHASE_OFFSET_H,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class CaDevWeatherProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CA Dev Weather Proxy."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Collect the synthetic weather curve's parameters."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)

        schema = vol.Schema(
            {
                vol.Required("name", default="Simulated Weather"): selector.TextSelector(),
                vol.Required(CONF_BASE_TEMP_F, default=DEFAULT_BASE_TEMP_F): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-40, max=130, step=0.5, unit_of_measurement="°F", mode="box")
                ),
                vol.Required(CONF_DIURNAL_SWING_F, default=DEFAULT_DIURNAL_SWING_F): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=60, step=0.5, unit_of_measurement="°F", mode="box")
                ),
                vol.Required(CONF_PHASE_OFFSET_H, default=DEFAULT_PHASE_OFFSET_H): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=24, step=0.5, unit_of_measurement="hr", mode="box")
                ),
                vol.Required(CONF_CONDITION, default=DEFAULT_CONDITION): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=CONDITION_OPTIONS,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
