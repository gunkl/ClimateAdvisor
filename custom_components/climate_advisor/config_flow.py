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
    AI_MODELS,
    AI_REASONING_OPTIONS,
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
    CONF_EMAIL_BRIEFING,
    CONF_EMAIL_DOOR_WINDOW_PAUSE,
    CONF_EMAIL_GRACE_EXPIRED,
    CONF_EMAIL_GRACE_REPAUSE,
    CONF_EMAIL_OCCUPANCY_HOME,
    CONF_FAN_ENTITY,
    CONF_FAN_MIN_RUNTIME_PER_HOUR,
    CONF_FAN_MODE,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_PUSH_BRIEFING,
    CONF_PUSH_DOOR_WINDOW_PAUSE,
    CONF_PUSH_OCCUPANCY_HOME,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_TEMP_UNIT,
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
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_FAN_MIN_RUNTIME_PER_HOUR,
    DEFAULT_FAN_MODE,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    DEFAULT_TEMP_UNIT,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    DOMAIN,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
)
from .temperature import CELSIUS, FAHRENHEIT, from_fahrenheit, to_fahrenheit

_LOGGER = logging.getLogger(__name__)

_NOTIFY_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

FAN_MODE_OPTIONS = [
    selector.SelectOptionDict(value=FAN_MODE_DISABLED, label="Disabled (no fan control)"),
    selector.SelectOptionDict(value=FAN_MODE_WHOLE_HOUSE, label="Whole house fan (dedicated entity)"),
    selector.SelectOptionDict(value=FAN_MODE_HVAC, label="HVAC fan mode"),
    selector.SelectOptionDict(value=FAN_MODE_BOTH, label="Both (whole house fan + HVAC fan mode)"),
]

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

# Menu options for the options flow (Issue #50)
OPTIONS_MENU_OPTIONS = [
    "core",
    "temperature_sources",
    "sensors",
    "occupancy",
    "schedule",
    "notifications",
    "advanced",
    "ai_settings",
    "save",
]

TEMP_UNIT_OPTIONS = [
    {"value": FAHRENHEIT, "label": "Fahrenheit (°F)"},
    {"value": CELSIUS, "label": "Celsius (°C)"},
]


def _needs_entity(source: str) -> bool:
    """Return True if the source type requires an entity selection."""
    return source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)


def _entity_selector_for_source(source: str) -> selector.EntitySelector:
    """Return an EntitySelector appropriate for the given source type."""
    if source == TEMP_SOURCE_INPUT_NUMBER:
        return selector.EntitySelector(selector.EntitySelectorConfig(domain="input_number"))
    # Default: sensor with temperature device class
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature"))


class ClimateAdvisorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Advisor."""

    VERSION = 15

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial setup step — core entities and notify service."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate notify_service format
            notify_svc = user_input.get("notify_service", "")
            if not _NOTIFY_SERVICE_RE.match(notify_svc):
                errors["notify_service"] = "invalid_notify_service"

            if not errors:
                self._data.update(user_input)
                _LOGGER.debug(
                    "Config flow — weather=%s, climate=%s",
                    user_input.get("weather_entity"),
                    user_input.get("climate_entity"),
                )
                return await self.async_step_unit()

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
                    vol.Required("notify_service", default="notify.notify"): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_unit(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Select temperature unit."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_setpoints()
        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TEMP_UNIT, default=DEFAULT_TEMP_UNIT): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=TEMP_UNIT_OPTIONS,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_setpoints(self, user_input: dict | None = None) -> config_entries.ConfigFlowResult:
        """Configure temperature setpoints."""
        errors: dict[str, str] = {}
        unit = self._data.get(CONF_TEMP_UNIT, FAHRENHEIT)
        is_celsius = unit == CELSIUS

        if user_input is not None:
            # Cross-field validation (on display values — ordering is unit-invariant)
            if user_input.get("setback_heat", 0) >= user_input.get("comfort_heat", 999):
                errors["setback_heat"] = "setback_must_be_lower"
            if user_input.get("setback_cool", 999) <= user_input.get("comfort_cool", 0):
                errors["setback_cool"] = "setback_must_be_higher"
            if user_input.get("sleep_heat", 999) >= user_input.get("comfort_heat", 0):
                errors["sleep_heat"] = "sleep_must_be_below_comfort"
            if user_input.get("sleep_heat", 0) <= user_input.get("setback_heat", 999):
                errors["sleep_heat"] = "sleep_must_be_above_setback"
            if user_input.get("sleep_cool", 0) <= user_input.get("comfort_cool", 999):
                errors["sleep_cool"] = "sleep_must_be_above_comfort"
            if user_input.get("sleep_cool", 999) >= user_input.get("setback_cool", 0):
                errors["sleep_cool"] = "sleep_must_be_below_setback"
            if not errors:
                # Convert display values → stored °F (canonical internal unit)
                converted = {**user_input}
                for key in ("comfort_heat", "comfort_cool", "setback_heat", "setback_cool", "sleep_heat", "sleep_cool"):
                    if key in converted:
                        converted[key] = to_fahrenheit(converted[key], unit)
                self._data.update(converted)
                return await self.async_step_temperature_sources()

        # Slider ranges and defaults depend on chosen unit
        if is_celsius:
            ranges = {
                "comfort_heat": (13, 27, 21, 0.5),
                "comfort_cool": (20, 29, 24, 0.5),
                "setback_heat": (7, 18, 16, 0.5),
                "setback_cool": (24, 32, 27, 0.5),
                "sleep_heat": (13, 26, 19, 0.5),
                "sleep_cool": (21, 32, 26, 0.5),
            }
            unit_label = "°C"
        else:
            ranges = {
                "comfort_heat": (55, 80, DEFAULT_COMFORT_HEAT, 1),
                "comfort_cool": (68, 85, DEFAULT_COMFORT_COOL, 1),
                "setback_heat": (45, 65, DEFAULT_SETBACK_HEAT, 1),
                "setback_cool": (75, 90, DEFAULT_SETBACK_COOL, 1),
                "sleep_heat": (55, 79, 66, 1),
                "sleep_cool": (69, 89, 78, 1),
            }
            unit_label = "°F"

        def _num(key: str) -> selector.NumberSelector:
            mn, mx, default, step = ranges[key]
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=mn,
                    max=mx,
                    step=step,
                    unit_of_measurement=unit_label,
                    mode="slider",
                )
            )

        return self.async_show_form(
            step_id="setpoints",
            data_schema=vol.Schema(
                {
                    vol.Required("comfort_heat", default=ranges["comfort_heat"][2]): _num("comfort_heat"),
                    vol.Required("comfort_cool", default=ranges["comfort_cool"][2]): _num("comfort_cool"),
                    vol.Required("setback_heat", default=ranges["setback_heat"][2]): _num("setback_heat"),
                    vol.Required("setback_cool", default=ranges["setback_cool"][2]): _num("setback_cool"),
                    vol.Required("sleep_heat", default=ranges["sleep_heat"][2]): _num("sleep_heat"),
                    vol.Required("sleep_cool", default=ranges["sleep_cool"][2]): _num("sleep_cool"),
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
                    vol.Required("outdoor_temp_source", default=TEMP_SOURCE_WEATHER_SERVICE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=OUTDOOR_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("indoor_temp_source", default=TEMP_SOURCE_CLIMATE_FALLBACK): selector.SelectSelector(
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

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the door/window sensor selection step."""
        if user_input is not None:
            # Convert minutes (UI) to seconds (internal storage)
            for key in (
                CONF_SENSOR_DEBOUNCE,
                CONF_MANUAL_GRACE_PERIOD,
                CONF_AUTOMATION_GRACE_PERIOD,
                CONF_OVERRIDE_CONFIRM_PERIOD,
            ):
                if key in user_input:
                    user_input[key] = int(user_input[key] * 60)
            self._data.update(user_input)
            return await self.async_step_occupancy()

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional("door_window_sensors", default=[]): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            multiple=True,
                        )
                    ),
                    vol.Optional(CONF_SENSOR_POLARITY_INVERTED, default=False): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SENSOR_DEBOUNCE, default=DEFAULT_SENSOR_DEBOUNCE_SECONDS // 60
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_PERIOD, default=DEFAULT_MANUAL_GRACE_SECONDS // 60
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=240,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_PERIOD, default=DEFAULT_AUTOMATION_GRACE_SECONDS // 60
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=240,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_OVERRIDE_CONFIRM_PERIOD, default=DEFAULT_OVERRIDE_CONFIRM_SECONDS // 60
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(CONF_FAN_MODE, default=DEFAULT_FAN_MODE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=FAN_MODE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_FAN_ENTITY,
                        description={"suggested_value": None},
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["fan", "switch"])),
                    vol.Optional(
                        CONF_FAN_MIN_RUNTIME_PER_HOUR,
                        default=DEFAULT_FAN_MIN_RUNTIME_PER_HOUR,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=60, step=1, unit_of_measurement="min", mode="box")
                    ),
                }
            ),
        )

    async def async_step_occupancy(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the occupancy awareness step."""
        if user_input is not None:
            if CONF_WELCOME_HOME_DEBOUNCE in user_input:
                user_input[CONF_WELCOME_HOME_DEBOUNCE] = int(user_input[CONF_WELCOME_HOME_DEBOUNCE] * 60)
            self._data.update(user_input)
            return await self.async_step_schedule()

        return self.async_show_form(
            step_id="occupancy",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_HOME_TOGGLE,
                        description={"suggested_value": None},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(CONF_HOME_TOGGLE_INVERT, default=False): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_VACATION_TOGGLE,
                        description={"suggested_value": None},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(CONF_VACATION_TOGGLE_INVERT, default=False): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_GUEST_TOGGLE,
                        description={"suggested_value": None},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(CONF_GUEST_TOGGLE_INVERT, default=False): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_WELCOME_HOME_DEBOUNCE,
                        default=DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=480,
                            step=5,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                }
            ),
        )

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the daily schedule step."""
        if user_input is not None:
            self._data.update(user_input)
            _LOGGER.info(
                "Config entry created — wake=%s, sleep=%s, briefing=%s",
                self._data.get("wake_time"),
                self._data.get("sleep_time"),
                self._data.get("briefing_time"),
            )
            return self.async_create_entry(
                title="Climate Advisor",
                data=self._data,
            )

        _time_selector = selector.TimeSelector()

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required("wake_time", default="06:30:00"): _time_selector,
                    vol.Required("sleep_time", default="22:30:00"): _time_selector,
                    vol.Required("briefing_time", default="06:00:00"): _time_selector,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ClimateAdvisorOptionsFlow:
        """Get the options flow handler."""
        return ClimateAdvisorOptionsFlow()


class ClimateAdvisorOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Climate Advisor — menu-based navigation (Issue #50)."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._updates: dict[str, Any] = {}

    # ---- Menu ----

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=OPTIONS_MENU_OPTIONS,
        )

    # ---- Core Settings ----

    async def async_step_core(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Core entities and temperature setpoints."""
        errors: dict[str, str] = {}

        current = self.config_entry.data
        unit = current.get(CONF_TEMP_UNIT, FAHRENHEIT)
        is_celsius = unit == CELSIUS

        if user_input is not None:
            # Validate notify_service format
            notify_svc = user_input.get("notify_service", "")
            if not _NOTIFY_SERVICE_RE.match(notify_svc):
                errors["notify_service"] = "invalid_notify_service"
            # Cross-field: setback must be more conservative than comfort
            if user_input.get("setback_heat", 0) >= user_input.get("comfort_heat", 999):
                errors["setback_heat"] = "setback_must_be_lower"
            if user_input.get("setback_cool", 999) <= user_input.get("comfort_cool", 0):
                errors["setback_cool"] = "setback_must_be_higher"
            if user_input.get("sleep_heat", 999) >= user_input.get("comfort_heat", 0):
                errors["sleep_heat"] = "sleep_must_be_below_comfort"
            if user_input.get("sleep_heat", 0) <= user_input.get("setback_heat", 999):
                errors["sleep_heat"] = "sleep_must_be_above_setback"
            if user_input.get("sleep_cool", 0) <= user_input.get("comfort_cool", 999):
                errors["sleep_cool"] = "sleep_must_be_above_comfort"
            if user_input.get("sleep_cool", 999) >= user_input.get("setback_cool", 0):
                errors["sleep_cool"] = "sleep_must_be_below_setback"

            if not errors:
                # Convert display values → stored °F
                unit_in = user_input.get(CONF_TEMP_UNIT, FAHRENHEIT)
                for key in ("comfort_heat", "comfort_cool", "setback_heat", "setback_cool", "sleep_heat", "sleep_cool"):
                    if key in user_input:
                        user_input[key] = to_fahrenheit(user_input[key], unit_in)
                self._updates.update(user_input)
                return await self.async_step_init()

        if is_celsius:
            ranges = {
                "comfort_heat": (13, 27, 0.5),
                "comfort_cool": (20, 29, 0.5),
                "setback_heat": (7, 18, 0.5),
                "setback_cool": (24, 32, 0.5),
                "sleep_heat": (13, 26, 0.5),
                "sleep_cool": (21, 32, 0.5),
            }
            unit_label = "°C"
            # Pre-fill: convert stored °F to display unit
            comfort_heat_disp = from_fahrenheit(current.get("comfort_heat", DEFAULT_COMFORT_HEAT), unit)
            comfort_cool_disp = from_fahrenheit(current.get("comfort_cool", DEFAULT_COMFORT_COOL), unit)
            setback_heat_disp = from_fahrenheit(current.get("setback_heat", DEFAULT_SETBACK_HEAT), unit)
            setback_cool_disp = from_fahrenheit(current.get("setback_cool", DEFAULT_SETBACK_COOL), unit)
            sleep_heat_disp = from_fahrenheit(current.get("sleep_heat", 66.0), unit)
            sleep_cool_disp = from_fahrenheit(current.get("sleep_cool", 78.0), unit)
        else:
            ranges = {
                "comfort_heat": (55, 80, 1),
                "comfort_cool": (68, 85, 1),
                "setback_heat": (45, 65, 1),
                "setback_cool": (75, 90, 1),
                "sleep_heat": (55, 79, 1),
                "sleep_cool": (69, 89, 1),
            }
            unit_label = "°F"
            # Stored in °F already — use directly
            comfort_heat_disp = current.get("comfort_heat", DEFAULT_COMFORT_HEAT)
            comfort_cool_disp = current.get("comfort_cool", DEFAULT_COMFORT_COOL)
            setback_heat_disp = current.get("setback_heat", DEFAULT_SETBACK_HEAT)
            setback_cool_disp = current.get("setback_cool", DEFAULT_SETBACK_COOL)
            sleep_heat_disp = current.get("sleep_heat", 66.0)
            sleep_cool_disp = current.get("sleep_cool", 78.0)

        return self.async_show_form(
            step_id="core",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "weather_entity",
                        default=current.get("weather_entity"),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
                    vol.Required(
                        "climate_entity",
                        default=current.get("climate_entity"),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
                    vol.Required(
                        CONF_TEMP_UNIT,
                        default=current.get(CONF_TEMP_UNIT, FAHRENHEIT),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=TEMP_UNIT_OPTIONS,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        "comfort_heat",
                        default=comfort_heat_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["comfort_heat"][0],
                            max=ranges["comfort_heat"][1],
                            step=ranges["comfort_heat"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "comfort_cool",
                        default=comfort_cool_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["comfort_cool"][0],
                            max=ranges["comfort_cool"][1],
                            step=ranges["comfort_cool"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "setback_heat",
                        default=setback_heat_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["setback_heat"][0],
                            max=ranges["setback_heat"][1],
                            step=ranges["setback_heat"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "setback_cool",
                        default=setback_cool_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["setback_cool"][0],
                            max=ranges["setback_cool"][1],
                            step=ranges["setback_cool"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "sleep_heat",
                        default=sleep_heat_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["sleep_heat"][0],
                            max=ranges["sleep_heat"][1],
                            step=ranges["sleep_heat"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "sleep_cool",
                        default=sleep_cool_disp,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=ranges["sleep_cool"][0],
                            max=ranges["sleep_cool"][1],
                            step=ranges["sleep_cool"][2],
                            unit_of_measurement=unit_label,
                            mode="slider",
                        )
                    ),
                    vol.Required(
                        "notify_service",
                        default=current.get("notify_service", "notify.notify"),
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    # ---- Temperature Sources ----

    async def async_step_temperature_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Temperature source selection."""
        if user_input is not None:
            self._updates.update(user_input)
            _LOGGER.debug(
                "Options — outdoor_source=%s, indoor_source=%s",
                user_input.get("outdoor_temp_source"),
                user_input.get("indoor_temp_source"),
            )
            return await self.async_step_init()

        current = self.config_entry.data

        return self.async_show_form(
            step_id="temperature_sources",
            data_schema=vol.Schema(
                {
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
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_number"])),
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
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_number"])),
                }
            ),
        )

    # ---- Sensors & Fan ----

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Door/window sensor and fan configuration."""
        if user_input is not None:
            # Convert minutes (UI) to seconds (internal storage)
            for key in (
                CONF_SENSOR_DEBOUNCE,
                CONF_MANUAL_GRACE_PERIOD,
                CONF_AUTOMATION_GRACE_PERIOD,
                CONF_OVERRIDE_CONFIRM_PERIOD,
            ):
                if key in user_input:
                    user_input[key] = int(user_input[key] * 60)
            self._updates.update(user_input)
            return await self.async_step_init()

        current = self.config_entry.data

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "door_window_sensors",
                        default=current.get("door_window_sensors", []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["binary_sensor"],
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        CONF_SENSOR_POLARITY_INVERTED,
                        default=current.get(CONF_SENSOR_POLARITY_INVERTED, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_SENSOR_DEBOUNCE,
                        default=current.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS) // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_MANUAL_GRACE_PERIOD,
                        default=current.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS) // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=240,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_PERIOD,
                        default=current.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS) // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=240,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_OVERRIDE_CONFIRM_PERIOD,
                        default=current.get(CONF_OVERRIDE_CONFIRM_PERIOD, DEFAULT_OVERRIDE_CONFIRM_SECONDS) // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                    vol.Optional(
                        CONF_FAN_MODE,
                        default=current.get(CONF_FAN_MODE, DEFAULT_FAN_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=FAN_MODE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(
                        CONF_FAN_ENTITY,
                        description={"suggested_value": current.get(CONF_FAN_ENTITY)},
                    ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["fan", "switch"])),
                    vol.Optional(
                        CONF_FAN_MIN_RUNTIME_PER_HOUR,
                        default=current.get(CONF_FAN_MIN_RUNTIME_PER_HOUR, DEFAULT_FAN_MIN_RUNTIME_PER_HOUR),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=60, step=1, unit_of_measurement="min", mode="box")
                    ),
                }
            ),
        )

    # ---- Occupancy ----

    async def async_step_occupancy(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Occupancy awareness configuration."""
        if user_input is not None:
            if CONF_WELCOME_HOME_DEBOUNCE in user_input:
                user_input[CONF_WELCOME_HOME_DEBOUNCE] = int(user_input[CONF_WELCOME_HOME_DEBOUNCE] * 60)
            self._updates.update(user_input)
            return await self.async_step_init()

        current = self.config_entry.data

        return self.async_show_form(
            step_id="occupancy",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_HOME_TOGGLE,
                        description={"suggested_value": current.get(CONF_HOME_TOGGLE)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(
                        CONF_HOME_TOGGLE_INVERT,
                        default=current.get(CONF_HOME_TOGGLE_INVERT, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_VACATION_TOGGLE,
                        description={"suggested_value": current.get(CONF_VACATION_TOGGLE)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(
                        CONF_VACATION_TOGGLE_INVERT,
                        default=current.get(CONF_VACATION_TOGGLE_INVERT, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_GUEST_TOGGLE,
                        description={"suggested_value": current.get(CONF_GUEST_TOGGLE)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "switch"])
                    ),
                    vol.Optional(
                        CONF_GUEST_TOGGLE_INVERT,
                        default=current.get(CONF_GUEST_TOGGLE_INVERT, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_WELCOME_HOME_DEBOUNCE,
                        default=current.get(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS) // 60,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=480,
                            step=5,
                            unit_of_measurement="minutes",
                            mode="box",
                        )
                    ),
                }
            ),
        )

    # ---- Schedule ----

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Daily schedule configuration."""
        if user_input is not None:
            self._updates.update(user_input)
            return await self.async_step_init()

        current = self.config_entry.data
        _time_selector = selector.TimeSelector()

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "wake_time",
                        default=current.get("wake_time", "06:30:00"),
                    ): _time_selector,
                    vol.Required(
                        "sleep_time",
                        default=current.get("sleep_time", "22:30:00"),
                    ): _time_selector,
                    vol.Required(
                        "briefing_time",
                        default=current.get("briefing_time", "06:00:00"),
                    ): _time_selector,
                }
            ),
        )

    # ---- Notifications (Issue #50) ----

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Notification preferences — per-event push and email toggles."""
        if user_input is not None:
            self._updates.update(user_input)
            return await self.async_step_init()

        current = self.config_entry.data

        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(
                {
                    # Push notification toggles
                    vol.Optional(
                        CONF_PUSH_BRIEFING,
                        default=current.get(CONF_PUSH_BRIEFING, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_PUSH_DOOR_WINDOW_PAUSE,
                        default=current.get(CONF_PUSH_DOOR_WINDOW_PAUSE, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_PUSH_OCCUPANCY_HOME,
                        default=current.get(CONF_PUSH_OCCUPANCY_HOME, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_MANUAL_GRACE_NOTIFY,
                        default=current.get(CONF_MANUAL_GRACE_NOTIFY, False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_AUTOMATION_GRACE_NOTIFY,
                        default=current.get(CONF_AUTOMATION_GRACE_NOTIFY, True),
                    ): selector.BooleanSelector(),
                    # Email notification toggles
                    vol.Optional(
                        CONF_EMAIL_BRIEFING,
                        default=current.get(CONF_EMAIL_BRIEFING, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EMAIL_DOOR_WINDOW_PAUSE,
                        default=current.get(CONF_EMAIL_DOOR_WINDOW_PAUSE, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EMAIL_GRACE_EXPIRED,
                        default=current.get(CONF_EMAIL_GRACE_EXPIRED, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EMAIL_GRACE_REPAUSE,
                        default=current.get(CONF_EMAIL_GRACE_REPAUSE, True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_EMAIL_OCCUPANCY_HOME,
                        default=current.get(CONF_EMAIL_OCCUPANCY_HOME, True),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # ---- Advanced ----

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Learning and behavior settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            min_p = user_input.get("min_preheat_minutes", 30)
            max_p = user_input.get("max_preheat_minutes", 240)
            default_p = user_input.get("default_preheat_minutes", 120)
            if min_p > max_p:
                errors["min_preheat_minutes"] = "preheat_min_exceeds_max"
            if default_p > max_p:
                errors["default_preheat_minutes"] = "preheat_default_exceeds_max"
            if not errors:
                self._updates.update(user_input)
                return await self.async_step_init()

        current = self.config_entry.data

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "learning_enabled",
                        default=current.get("learning_enabled", True),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "adaptive_preheat_enabled",
                        default=current.get("adaptive_preheat_enabled", True),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "adaptive_setback_enabled",
                        default=current.get("adaptive_setback_enabled", True),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "weather_bias_enabled",
                        default=current.get("weather_bias_enabled", True),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        "min_preheat_minutes",
                        default=current.get("min_preheat_minutes", 30),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=5, max=60, step=5, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        "max_preheat_minutes",
                        default=current.get("max_preheat_minutes", 240),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=30, max=240, step=15, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        "default_preheat_minutes",
                        default=current.get("default_preheat_minutes", 120),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=10, max=120, step=5, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        "preheat_safety_margin",
                        default=current.get("preheat_safety_margin", 1.3),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1.0, max=2.0, step=0.05, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        "max_setback_depth_f",
                        default=current.get("max_setback_depth_f", 8.0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1.0, max=10.0, step=0.5, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Required(
                        "aggressive_savings",
                        default=current.get("aggressive_savings", False),
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    # ---- AI Settings ----

    async def async_step_ai_settings(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """AI Settings — Claude API configuration."""
        errors: dict[str, str] = {}
        current = self.config_entry.data

        if user_input is not None:
            ai_enabled = user_input.get(CONF_AI_ENABLED, DEFAULT_AI_ENABLED)
            new_key = user_input.get(CONF_AI_API_KEY, "")
            existing_key = current.get(CONF_AI_API_KEY, "")

            # If AI enabled but no key provided and no existing key, require one
            if ai_enabled and not new_key and not existing_key:
                errors[CONF_AI_API_KEY] = "ai_key_required"

            # Validate temperature range
            ai_temp = user_input.get(CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE)
            if not (0.0 <= float(ai_temp) <= 1.0):
                errors[CONF_AI_TEMPERATURE] = "ai_temperature_out_of_range"

            # Validate max_tokens range
            ai_max_tokens = int(user_input.get(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS))
            if not (256 <= ai_max_tokens <= 8192):
                errors[CONF_AI_MAX_TOKENS] = "ai_max_tokens_out_of_range"

            investigator_max_tokens = int(
                user_input.get(CONF_AI_INVESTIGATOR_MAX_TOKENS, DEFAULT_AI_INVESTIGATOR_MAX_TOKENS)
            )
            if not (256 <= investigator_max_tokens <= 32768):
                errors[CONF_AI_INVESTIGATOR_MAX_TOKENS] = "ai_max_tokens_out_of_range"

            if not errors and new_key and new_key != existing_key and ai_enabled:
                try:
                    from .claude_api import ClaudeAPIClient

                    test_config = {**current, **user_input}
                    client = ClaudeAPIClient(test_config)
                    success, _message = await client.async_test_connection()
                    if not success:
                        errors["base"] = "ai_connection_failed"
                except Exception:  # noqa: BLE001
                    errors["base"] = "ai_connection_failed"

            if not errors:
                # If the user left the key field blank, preserve the existing key
                merged = {**user_input}
                if not new_key:
                    if existing_key:
                        merged[CONF_AI_API_KEY] = existing_key
                    else:
                        merged.pop(CONF_AI_API_KEY, None)

                # Apply int() conversion for number fields
                for key in (
                    CONF_AI_MAX_TOKENS,
                    CONF_AI_MONTHLY_BUDGET,
                    CONF_AI_AUTO_REQUESTS_PER_DAY,
                    CONF_AI_MANUAL_REQUESTS_PER_DAY,
                    CONF_AI_INVESTIGATOR_MAX_TOKENS,
                    CONF_AI_INVESTIGATOR_RPD,
                ):
                    if key in merged:
                        merged[key] = int(merged[key])

                self._updates.update(merged)
                return await self.async_step_init()

        # Build masked key status placeholder for description
        existing_key = current.get(CONF_AI_API_KEY, "")
        if existing_key:
            key_status = "••••" + existing_key[-4:] if len(existing_key) > 4 else "••••"
        else:
            key_status = "Not configured"

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AI_ENABLED,
                    default=current.get(CONF_AI_ENABLED, DEFAULT_AI_ENABLED),
                ): selector.BooleanSelector(),
                vol.Optional(CONF_AI_API_KEY, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_AI_MODEL,
                    default=current.get(CONF_AI_MODEL, DEFAULT_AI_MODEL),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": m, "label": m} for m in AI_MODELS],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_AI_REASONING_EFFORT,
                    default=current.get(CONF_AI_REASONING_EFFORT, DEFAULT_AI_REASONING_EFFORT),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": r, "label": r.capitalize()} for r in AI_REASONING_OPTIONS],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_AI_MAX_TOKENS,
                    default=current.get(CONF_AI_MAX_TOKENS, DEFAULT_AI_MAX_TOKENS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=256, max=8192, step=256, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_AI_TEMPERATURE,
                    default=current.get(CONF_AI_TEMPERATURE, DEFAULT_AI_TEMPERATURE),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.0, max=1.0, step=0.1, mode=selector.NumberSelectorMode.SLIDER)
                ),
                vol.Optional(
                    CONF_AI_MONTHLY_BUDGET,
                    default=current.get(CONF_AI_MONTHLY_BUDGET, DEFAULT_AI_MONTHLY_BUDGET),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_AI_AUTO_REQUESTS_PER_DAY,
                    default=current.get(CONF_AI_AUTO_REQUESTS_PER_DAY, DEFAULT_AI_AUTO_REQUESTS_PER_DAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=50, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_AI_MANUAL_REQUESTS_PER_DAY,
                    default=current.get(CONF_AI_MANUAL_REQUESTS_PER_DAY, DEFAULT_AI_MANUAL_REQUESTS_PER_DAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=100, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_AI_INVESTIGATOR_ENABLED,
                    default=current.get(CONF_AI_INVESTIGATOR_ENABLED, DEFAULT_AI_INVESTIGATOR_ENABLED),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_AI_INVESTIGATOR_MODEL,
                    default=current.get(CONF_AI_INVESTIGATOR_MODEL, DEFAULT_AI_INVESTIGATOR_MODEL),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": m, "label": m} for m in AI_MODELS],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_AI_INVESTIGATOR_REASONING,
                    default=current.get(CONF_AI_INVESTIGATOR_REASONING, DEFAULT_AI_INVESTIGATOR_REASONING),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": r, "label": r.title()} for r in AI_REASONING_OPTIONS],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_AI_INVESTIGATOR_MAX_TOKENS,
                    default=current.get(CONF_AI_INVESTIGATOR_MAX_TOKENS, DEFAULT_AI_INVESTIGATOR_MAX_TOKENS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=256, max=32768, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_AI_INVESTIGATOR_RPD,
                    default=current.get(CONF_AI_INVESTIGATOR_RPD, DEFAULT_AI_INVESTIGATOR_RPD),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=20, mode=selector.NumberSelectorMode.BOX)
                ),
            }
        )

        return self.async_show_form(
            step_id="ai_settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={"key_status": key_status},
        )

    # ---- Save & Close ----

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Merge all updates and save."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, **self._updates},
        )
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        _LOGGER.info("Options updated — reload triggered")
        return self.async_create_entry(title="", data={})
