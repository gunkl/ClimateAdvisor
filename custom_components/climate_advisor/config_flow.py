"""Config flow for Climate Advisor integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_SENSOR_DEBOUNCE,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_AUTOMATION_GRACE_NOTIFY,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_COMFORT_COOL,
    DEFAULT_SETBACK_HEAT,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_WEATHER_SERVICE,
    TEMP_SOURCE_CLIMATE_FALLBACK,
)

_LOGGER = logging.getLogger(__name__)

OUTDOOR_SOURCE_OPTIONS = [
    selector.SelectOptionDict(value=TEMP_SOURCE_WEATHER_SERVICE, label="Weather service (recommended)"),
    selector.SelectOptionDict(value=TEMP_SOURCE_SENSOR, label="Dedicated sensor"),
    selector.SelectOptionDict(value=TEMP_SOURCE_INPUT_NUMBER, label="Input helper (input_number)"),
]

INDOOR_SOURCE_OPTIONS = [
    selector.SelectOptionDict(value=TEMP_SOURCE_CLIMATE_FALLBACK, label="Climate entity (recommended)"),
    selector.SelectOptionDict(value=TEMP_SOURCE_SENSOR, label="Dedicated sensor"),
    selector.SelectOptionDict(value=TEMP_SOURCE_INPUT_NUMBER, label="Input helper (input_number)"),
]


def _needs_entity(source: str) -> bool:
    """Return True if the source type requires an entity selection."""
    return source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)


def _entity_selector_for_source(source: str) -> selector.EntitySelector:
    """Return an EntitySelector appropriate for the given source type."""
    if source == TEMP_SOURCE_INPUT_NUMBER:
        return selector.EntitySelector(
            selector.EntitySelectorConfig(domain="input_number")
        )
    # Default: sensor with temperature device class
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
    )


class ClimateAdvisorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Advisor."""

    VERSION = 4

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
            return await self.async_step_temperature_sources()

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

    async def async_step_temperature_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle temperature source type selection."""
        if user_input is not None:
            self._data.update(user_input)

            # Route to entity picker steps if needed
            if _needs_entity(self._data.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)):
                return await self.async_step_outdoor_temp_entity()
            if _needs_entity(self._data.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)):
                return await self.async_step_indoor_temp_entity()
            return await self.async_step_sensors()

        return self.async_show_form(
            step_id="temperature_sources",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "outdoor_temp_source", default=TEMP_SOURCE_WEATHER_SERVICE
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=OUTDOOR_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        "indoor_temp_source", default=TEMP_SOURCE_CLIMATE_FALLBACK
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=INDOOR_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_outdoor_temp_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle outdoor temperature entity selection."""
        if user_input is not None:
            self._data.update(user_input)
            # Check if indoor also needs an entity
            if _needs_entity(self._data.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)):
                return await self.async_step_indoor_temp_entity()
            return await self.async_step_sensors()

        source = self._data.get("outdoor_temp_source", TEMP_SOURCE_SENSOR)
        return self.async_show_form(
            step_id="outdoor_temp_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("outdoor_temp_entity"): _entity_selector_for_source(source),
                }
            ),
        )

    async def async_step_indoor_temp_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle indoor temperature entity selection."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_sensors()

        source = self._data.get("indoor_temp_source", TEMP_SOURCE_SENSOR)
        return self.async_show_form(
            step_id="indoor_temp_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("indoor_temp_entity"): _entity_selector_for_source(source),
                }
            ),
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
                    vol.Optional("door_window_groups", default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["group"],
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        CONF_SENSOR_POLARITY_INVERTED, default=False
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SENSOR_DEBOUNCE, default=DEFAULT_SENSOR_DEBOUNCE_SECONDS
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=900, step=30,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_PERIOD, default=DEFAULT_MANUAL_GRACE_SECONDS
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=7200, step=60,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_NOTIFY, default=False
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_PERIOD, default=DEFAULT_AUTOMATION_GRACE_SECONDS
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=7200, step=60,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_NOTIFY, default=True
                    ): selector.BooleanSelector(),
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
                    vol.Required(
                        "outdoor_temp_source",
                        default=current.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=OUTDOOR_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        "outdoor_temp_entity",
                        description={"suggested_value": current.get("outdoor_temp_entity")},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor", "input_number"])
                    ),
                    vol.Required(
                        "indoor_temp_source",
                        default=current.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=INDOOR_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        "indoor_temp_entity",
                        description={"suggested_value": current.get("indoor_temp_entity")},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor", "input_number"])
                    ),
                    vol.Optional(
                        "door_window_sensors",
                        default=current.get("door_window_sensors", []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            device_class=["door", "window", "opening"],
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        "door_window_groups",
                        default=current.get("door_window_groups", []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["group"],
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        CONF_SENSOR_POLARITY_INVERTED,
                        default=current.get(CONF_SENSOR_POLARITY_INVERTED, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SENSOR_DEBOUNCE,
                        default=current.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=900, step=30,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_PERIOD,
                        default=current.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=7200, step=60,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_NOTIFY,
                        default=current.get(CONF_MANUAL_GRACE_NOTIFY, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_PERIOD,
                        default=current.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=7200, step=60,
                            unit_of_measurement="seconds",
                            mode="slider",
                        )
                    ),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_NOTIFY,
                        default=current.get(CONF_AUTOMATION_GRACE_NOTIFY, True),
                    ): selector.BooleanSelector(),
                }
            ),
        )
