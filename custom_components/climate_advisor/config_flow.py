"""Config flow for Climate Advisor integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DOMAIN, DEFAULT_COMFORT_HEAT, DEFAULT_COMFORT_COOL, DEFAULT_SETBACK_HEAT, DEFAULT_SETBACK_COOL

_LOGGER = logging.getLogger(__name__)


class ClimateAdvisorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Advisor."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step — core entities and setpoints."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("weather_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather")
                    ),
                    vol.Required("climate_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate")
                    ),
                    vol.Optional("outdoor_temp_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                    ),
                    vol.Optional("indoor_temp_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                    ),
                    vol.Required("comfort_heat", default=DEFAULT_COMFORT_HEAT): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=55, max=80, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("comfort_cool", default=DEFAULT_COMFORT_COOL): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=68, max=85, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("setback_heat", default=DEFAULT_SETBACK_HEAT): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=45, max=65, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("setback_cool", default=DEFAULT_SETBACK_COOL): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=75, max=90, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("notify_service", default="notify.notify"): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the door/window sensor selection step."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_schedule()

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional("door_window_sensors", default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            device_class=["door", "window", "opening"],
                            multiple=True,
                        )
                    ),
                }
            ),
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the daily schedule step."""
        errors: dict[str, str] = {}
        _TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

        if user_input is not None:
            for field in ("wake_time", "sleep_time", "briefing_time"):
                value = user_input.get(field, "")
                if not _TIME_RE.match(value):
                    errors[field] = "invalid_time_format"

            if not errors:
                self._data.update(user_input)
                return self.async_create_entry(
                    title="Climate Advisor",
                    data=self._data,
                )

        _text_selector = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required("wake_time", default="06:30"): _text_selector,
                    vol.Required("sleep_time", default="22:30"): _text_selector,
                    vol.Required("briefing_time", default="06:00"): _text_selector,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ClimateAdvisorOptionsFlow:
        """Get the options flow handler."""
        return ClimateAdvisorOptionsFlow(config_entry)


class ClimateAdvisorOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Climate Advisor."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("comfort_heat", default=current.get("comfort_heat", DEFAULT_COMFORT_HEAT)): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=55, max=80, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("comfort_cool", default=current.get("comfort_cool", DEFAULT_COMFORT_COOL)): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=68, max=85, step=1, unit_of_measurement="°F", mode="slider")
                    ),
                    vol.Required("learning_enabled", default=current.get("learning_enabled", True)): selector.BooleanSelector(),
                    vol.Required("aggressive_savings", default=current.get("aggressive_savings", False)): selector.BooleanSelector(),
                }
            ),
        )
