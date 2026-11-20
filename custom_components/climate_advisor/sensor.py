"""Sensor platform for Climate Advisor.

Exposes the day classification, briefing, learning metrics, and next
recommended action as Home Assistant sensors for use in dashboards
and other automations.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import i18n
from .automation import active_thresholds_for
from .const import (
    ATTR_AI_STATUS,
    ATTR_AUTOMATION_STATUS,
    ATTR_BRIEFING,
    ATTR_BRIEFING_SHORT,
    ATTR_COMPLIANCE_SCORE,
    ATTR_CONTACT_STATUS,
    ATTR_COOL_THRESHOLD,
    ATTR_DAY_TYPE,
    ATTR_EFFECTIVE_TARGET_TEMP,
    ATTR_FAN_OVERRIDE_SINCE,
    ATTR_FAN_RUNNING,
    ATTR_FAN_RUNTIME,
    ATTR_FAN_STATUS,
    ATTR_FORECAST_BIAS_CONFIDENCE,
    ATTR_FORECAST_HIGH,
    ATTR_FORECAST_HIGH_BIAS,
    ATTR_FORECAST_LOW,
    ATTR_FORECAST_LOW_BIAS,
    ATTR_HEAT_THRESHOLD,
    ATTR_INDOOR_TEMP,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTION_MSGS,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_OUTDOOR_TEMP,
    ATTR_THERMAL_CONFIDENCE,
    ATTR_THERMAL_COOLING_RATE,
    ATTR_THERMAL_HEATING_RATE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    DOMAIN,
    VERSION,
)
from .coordinator import ClimateAdvisorCoordinator
from .temperature import FAHRENHEIT, convert_delta, display_delta, display_temp

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Advisor sensors from a config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        ClimateAdvisorDayTypeSensor(coordinator, entry),
        ClimateAdvisorTrendSensor(coordinator, entry),
        ClimateAdvisorNextActionSensor(coordinator, entry),
        ClimateAdvisorBriefingSensor(coordinator, entry),
        ClimateAdvisorComplianceSensor(coordinator, entry),
        ClimateAdvisorStatusSensor(coordinator, entry),
        ClimateAdvisorNextAutomationSensor(coordinator, entry),
        ClimateAdvisorNextAutomationTimeSensor(coordinator, entry),
        ClimateAdvisorOccupancySensor(coordinator, entry),
        ClimateAdvisorLastActionTimeSensor(coordinator, entry),
        ClimateAdvisorLastActionReasonSensor(coordinator, entry),
        ClimateAdvisorFanStatusSensor(coordinator, entry),
        ClimateAdvisorContactStatusSensor(coordinator, entry),
        ClimateAdvisorAIStatusSensor(coordinator, entry),
        ClimateAdvisorIndoorTempSensor(coordinator, entry),
        ClimateAdvisorOutdoorTempSensor(coordinator, entry),
        ClimateAdvisorForecastHighSensor(coordinator, entry),
        ClimateAdvisorForecastLowSensor(coordinator, entry),
        ClimateAdvisorEffectiveTargetTempSensor(coordinator, entry),
        ClimateAdvisorHeatThresholdSensor(coordinator, entry),
        ClimateAdvisorCoolThresholdSensor(coordinator, entry),
    ]

    async_add_entities(entities)
    _LOGGER.debug("Registered %d Climate Advisor sensor entities", len(entities))


class ClimateAdvisorBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Climate Advisor sensors."""

    _empty_data_warned: bool = False
    _truncation_warned: bool = False

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
        key: str,
        icon: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Issue #1341 (D6): the name comes from strings.json ``entity.sensor.<key>.name`` (HA's own method),
        # never from ``_attr_name``, which would override the translation. The key is the unique-id suffix.
        self._attr_has_entity_name = True
        self._attr_translation_key = key
        self._attr_icon = icon
        self._key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Climate Advisor",
            model="Zone Controller",
            sw_version=VERSION,
        )

    def _lang(self) -> str:
        """The language HA uses for entity names (D2): a shipped language, ``en`` when unreadable."""
        return i18n.hass_language(getattr(self, "hass", None))

    def _prose(self, value: Any) -> Any:
        """Render a coordinator value for display (Issue #1341, D6).

        A ``Msg``/``Code`` the coordinator provides, or a ``str`` carrier of one (``CodedText``, ``CardText``,
        ``MsgText``), is rendered in HA's language; plain prose (or anything else) passes through unchanged, so a
        value can migrate to a message without a change here.
        """
        if isinstance(value, i18n.Code):
            return i18n.render(value, self._lang())
        msg = i18n.msg_of(value)
        if msg is not None:
            return i18n.render(msg, self._lang())
        return value

    @staticmethod
    def _code_attrs(value: Any) -> dict[str, Any]:
        """Stable ``code``/``params`` attributes for automations: the status code (else the message key) and the
        message's wire params, for a ``Msg``/``Code`` or any ``str`` carrier of one.

        ``code`` is None (and ``params`` empty) while the coordinator still hands over plain prose.
        """
        if isinstance(value, i18n.Code):
            value = i18n.Msg(value.key)
        msg = i18n.msg_of(value)
        if msg is not None:
            return {"code": i18n.code_of(value) or msg.key, "params": msg.to_wire()["params"]}
        code = i18n.code_of(value)
        return {"code": code, "params": {}}

    def _raw(self, key: str | None = None) -> Any:
        """The coordinator's value for ``key`` (this sensor's own by default), before rendering."""
        return self.coordinator.data.get(key or self._key) if self.coordinator.data else None

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if self.coordinator.data:
            return self._prose(self.coordinator.data.get(self._key))
        if not self._empty_data_warned:
            _LOGGER.debug(
                "Coordinator data empty for sensor %s — returning None",
                self._key,
            )
            self._empty_data_warned = True
        return None

    def _capped_state(self, text: str, *, limit: int = 250, level: int = logging.DEBUG) -> str:
        """Truncate text to HA's 255-char sensor state limit, logging once if triggered.

        This is a safety net, not the primary fix (Issue #555) — callers should shrink
        their source data so this rarely/never actually triggers.
        """
        if len(text) <= limit:
            return text
        if not self._truncation_warned:
            _LOGGER.log(
                level,
                "%s state truncated — %d chars exceeds %d-char limit",
                getattr(self, "name", None) or self._key,
                len(text),
                limit,
            )
            self._truncation_warned = True
        return text[: limit - 3] + "..."


class _ProseSensor(ClimateAdvisorBaseSensor):
    """A sensor whose state is occupant-facing prose (Issue #1341, D6).

    The state is rendered in HA's language when the coordinator hands over a message; ``code``/``params`` are the
    stable, language-independent form of the same value for automations (``code`` is None for legacy plain prose).
    """

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The message key and parameters behind the state."""
        return self._code_attrs(self._raw())


class ClimateAdvisorDayTypeSensor(ClimateAdvisorBaseSensor):
    """Sensor showing today's day type classification."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_DAY_TYPE, "mdi:weather-partly-cloudy")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Include trend info as attributes."""
        data = self.coordinator.data or {}
        unit = self.coordinator.config.get("temp_unit", FAHRENHEIT)
        return {
            "trend_direction": data.get(ATTR_TREND, "unknown"),
            # Issue #1048: internal degF delta -> the user's unit (scale only).
            "trend_magnitude": display_delta(data.get(ATTR_TREND_MAGNITUDE, 0), unit),
        }


class ClimateAdvisorTrendSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the temperature trend."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_TREND, "mdi:trending-up")

    @property
    def icon(self) -> str:
        """Dynamic icon based on trend direction."""
        value = self.native_value
        if value == "warming":
            return "mdi:trending-up"
        elif value == "cooling":
            return "mdi:trending-down"
        return "mdi:trending-neutral"


class ClimateAdvisorNextActionSensor(_ProseSensor):
    """Sensor showing the next recommended human action."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_NEXT_ACTION, "mdi:human-greeting")


class ClimateAdvisorNextAutomationSensor(_ProseSensor):
    """Sensor showing the next scheduled automation action."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator,
            entry,
            ATTR_NEXT_AUTOMATION_ACTION,
            "mdi:robot",
        )


class ClimateAdvisorNextAutomationTimeSensor(ClimateAdvisorBaseSensor):
    """Sensor showing when the next automation action will execute."""

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator,
            entry,
            ATTR_NEXT_AUTOMATION_TIME,
            "mdi:clock-outline",
        )


class ClimateAdvisorBriefingSensor(ClimateAdvisorBaseSensor):
    """Sensor holding the full daily briefing text."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_BRIEFING, "mdi:email-outline")

    @property
    def native_value(self) -> str | None:
        """Return the short TLDR briefing as the sensor state."""
        if not self.coordinator.data:
            return ""
        short = self.coordinator.data.get(ATTR_BRIEFING_SHORT, "")
        text = self._prose(short or self.coordinator.data.get(ATTR_BRIEFING, ""))
        return self._capped_state(text, level=logging.WARNING)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Store the full briefing text and untruncated TLDR, plus the state message's code/params."""
        data = self.coordinator.data or {}
        short = data.get(ATTR_BRIEFING_SHORT, "")
        return {
            "full_briefing": self._prose(data.get(ATTR_BRIEFING, "")),
            "tldr": self._prose(short),
            **self._code_attrs(short or data.get(ATTR_BRIEFING, "")),
        }


class ClimateAdvisorComplianceSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the comfort compliance score."""

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_COMPLIANCE_SCORE, "mdi:check-circle")

    @property
    def native_value(self) -> float | None:
        """Return compliance as a percentage."""
        value = super().native_value
        if value is not None:
            return round(value * 100, 1)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Include learning suggestions count, comfort range context, and thermal model data."""
        data = self.coordinator.data or {}
        suggestions = data.get(ATTR_LEARNING_SUGGESTIONS, [])
        suggestion_msgs = [m for m in (data.get(ATTR_LEARNING_SUGGESTION_MSGS) or []) if isinstance(m, i18n.Msg)]
        today = self.coordinator.today_record
        unit = self.coordinator.config.get("temp_unit", FAHRENHEIT)
        attrs: dict[str, Any] = {
            "pending_suggestions": len(suggestions),
            # Issue #1341 (D6): the suggestion text in HA's language, with its stable key and params for automations.
            "suggestions": [self._prose(m) for m in suggestion_msgs],
            "suggestion_codes": [self._code_attrs(m) for m in suggestion_msgs],
            "comfort_violations_minutes_today": today.comfort_violations_minutes if today else 0.0,
            # Issue #1048: config comfort settings are internal degF; show them in the user's unit.
            "comfort_range_low": display_temp(self.coordinator.config.get("comfort_heat", 70), unit),
            "comfort_range_high": display_temp(self.coordinator.config.get("comfort_cool", 75), unit),
        }
        thermal = self.coordinator.learning.get_thermal_model(learning_health=self.coordinator._build_learning_health())
        heat_rate_f = thermal.get("heating_rate_f_per_hour")
        cool_rate_f = thermal.get("cooling_rate_f_per_hour")
        attrs[ATTR_THERMAL_HEATING_RATE] = convert_delta(heat_rate_f, unit) if heat_rate_f is not None else None
        attrs[ATTR_THERMAL_COOLING_RATE] = convert_delta(cool_rate_f, unit) if cool_rate_f is not None else None
        attrs[ATTR_THERMAL_CONFIDENCE] = thermal.get("confidence", "none")
        # Issue #1512: the evidence behind the heat + cool rates (older evidence gives way as new cycles replace it,
        # never with time alone), not a cumulative counter.
        attrs["thermal_observation_count"] = thermal.get("observation_count_heat", 0) + thermal.get(
            "observation_count_cool", 0
        )
        health = thermal.get("learning_health", {})
        attrs["thermal_learning_health"] = (
            {
                obs_type: {
                    "attempts": h.get("attempts", 0),
                    "committed": h.get("committed", 0),
                    "rejections": h.get("rejections", {}),
                    "last_rejection_reason": (h["last_rejection"]["reason_code"] if h.get("last_rejection") else None),
                    # Issues #1512/#1516: why the latest heating/cooling run was not used (None when not recorded).
                    "last_rejection_signal_f": (
                        h["last_rejection"].get("signal_f") if h.get("last_rejection") else None
                    ),
                    "last_rejection_minutes": (h["last_rejection"].get("minutes") if h.get("last_rejection") else None),
                    "last_rejection_k_passive_source": (
                        h["last_rejection"].get("k_passive_source") if h.get("last_rejection") else None
                    ),
                }
                for obs_type, h in health.items()
                if isinstance(h, dict)
            }
            if health
            else {}
        )
        weather_bias = self.coordinator.learning.get_weather_bias()
        attrs[ATTR_FORECAST_HIGH_BIAS] = convert_delta(weather_bias.get("high_bias", 0.0), unit)
        attrs[ATTR_FORECAST_LOW_BIAS] = convert_delta(weather_bias.get("low_bias", 0.0), unit)
        attrs[ATTR_FORECAST_BIAS_CONFIDENCE] = weather_bias.get("confidence", "none")
        return attrs


class ClimateAdvisorStatusSensor(_ProseSensor):
    """Sensor showing the overall automation status."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_AUTOMATION_STATUS, "mdi:home-thermometer")


class ClimateAdvisorOccupancySensor(ClimateAdvisorBaseSensor):
    """Sensor showing the current occupancy mode."""

    def __init__(self, coordinator, entry):
        """Initialize the occupancy mode sensor."""
        super().__init__(coordinator, entry, ATTR_OCCUPANCY_MODE, "mdi:home-account")


class ClimateAdvisorLastActionTimeSensor(ClimateAdvisorBaseSensor):
    """Sensor showing when the last HVAC action was taken."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_LAST_ACTION_TIME, "mdi:clock-check-outline")


class ClimateAdvisorLastActionReasonSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the reason for the last HVAC action."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_LAST_ACTION_REASON, "mdi:text-box-outline")

    @property
    def native_value(self) -> str | None:
        """Return a truncated version for the state (HA 255 char limit)."""
        full = self._prose(self._raw() or "")
        if not full:
            return None
        return self._capped_state(full)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Store the full reason text as an attribute, with the stable code/params of its message."""
        raw = self._raw() or ""
        return {"full_reason": self._prose(raw), **self._code_attrs(raw)}


class ClimateAdvisorFanStatusSensor(_ProseSensor):
    """Sensor showing the current fan status (active/inactive/override/disabled).

    An ENUM-like state: the documented "Fan Status Values" tokens are what users' automations compare, so the
    state stays the raw English token in every language (never rendered through the catalog). The display name of
    the slug tokens comes from ``entity.sensor.fan_status.state.*`` (HA's own method); the stable ``code``/``params``
    are exposed as attributes.
    """

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_FAN_STATUS, "mdi:fan")

    @property
    def native_value(self) -> Any:
        """The raw English fan-status token."""
        raw = self._raw()
        return str(raw) if raw is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose fan runtime and economizer phase as attributes."""
        if not self.coordinator.data:
            return {}
        return {
            "fan_runtime_minutes": round(self.coordinator.data.get(ATTR_FAN_RUNTIME, 0.0), 1),
            "fan_override_since": self.coordinator.data.get(ATTR_FAN_OVERRIDE_SINCE),
            "fan_running": self.coordinator.data.get(ATTR_FAN_RUNNING, False),
            **self._code_attrs(self._raw()),
        }


class ClimateAdvisorContactStatusSensor(_ProseSensor):
    """Sensor showing the current door/window contact sensor status."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, ATTR_CONTACT_STATUS, "mdi:door-open")

    @property
    def icon(self) -> str:
        """Dynamic icon based on contact status."""
        # Issue #1341: decided from the sensors themselves, never from the (translated) summary text. Any open or
        # unreadable sensor shows the open door; no sensors, or all closed, shows the closed door.
        details = self.coordinator._compute_contact_details()
        if any(d["open"] or d["unreadable"] for d in details):
            return "mdi:door-open"
        return "mdi:door-closed"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose per-sensor details as attributes."""
        details = self.coordinator._compute_contact_details()
        open_count = sum(1 for d in details if d["open"])
        return {
            "sensor_count": len(details),
            "open_count": open_count,
            "paused_by_door": self.coordinator.automation_engine.is_paused_by_door,
            "natural_vent_active": self.coordinator.automation_engine.natural_vent_active,
            "sensors": details,
            **self._code_attrs(self._raw()),
        }


class ClimateAdvisorAIStatusSensor(ClimateAdvisorBaseSensor):
    """Sensor showing the AI integration status."""

    def __init__(self, coordinator, entry):
        """Initialize the AI status sensor."""
        super().__init__(coordinator, entry, ATTR_AI_STATUS, "mdi:robot")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return AI status details."""
        if self.coordinator.claude_client:
            status = self.coordinator.claude_client.get_status()
            return {
                "last_request_time": status.get("last_request_time"),
                "error_count": status.get("error_count", 0),
                "total_requests": status.get("total_requests", 0),
                "model_in_use": status.get("model"),
                # Issue #1336: which provider (and AI Task entity, "" = HA's preferred one) writes the reports
                "provider": status.get("provider"),
                "ai_task_entity": status.get("ai_task_entity", ""),
                "circuit_breaker": status.get("circuit_breaker_state", "closed"),
                "monthly_cost_estimate": status.get("monthly_cost_estimate", 0.0),
                "auto_requests_today": status.get("auto_requests_today", 0),
                "manual_requests_today": status.get("manual_requests_today", 0),
            }
        return {"status": "disabled"}


class ClimateAdvisorIndoorTempSensor(ClimateAdvisorBaseSensor):
    """Current indoor temperature sensor, tracked by HA Recorder for long-term history.

    Reports in °F. HA automatically converts to °C for users with metric preferences.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the indoor temperature sensor."""
        super().__init__(coordinator, entry, ATTR_INDOOR_TEMP, "mdi:thermometer")


class ClimateAdvisorOutdoorTempSensor(ClimateAdvisorBaseSensor):
    """Current outdoor temperature sensor, tracked by HA Recorder for long-term history."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the outdoor temperature sensor."""
        super().__init__(coordinator, entry, ATTR_OUTDOOR_TEMP, "mdi:thermometer-lines")


class ClimateAdvisorEffectiveTargetTempSensor(ClimateAdvisorBaseSensor):
    """Always-populated "what is the house aiming for right now" temperature
    (Issue #998), regardless of which mechanism is responsible.

    Never goes unavailable during normal operation — see
    coordinator._compute_effective_target_now()'s docstring for the 3-tier
    priority (HVAC setpoint -> fan cycling target -> passive comfort-band edge).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the effective target temperature sensor."""
        super().__init__(
            coordinator,
            entry,
            ATTR_EFFECTIVE_TARGET_TEMP,
            "mdi:thermostat",
        )


class _ClimateAdvisorThresholdSensor(ClimateAdvisorBaseSensor):
    """One edge of the comfort band Climate Advisor holds the house to right now (Issue #1345).

    Heat Threshold = the band floor (CA heats below it), Cool Threshold = the band ceiling (CA
    cools above it). Both read ONE ``automation.active_thresholds_for()`` result -- the same
    assembly as the status API's ``ca_target_heat`` / ``ca_target_cool`` -- so the sensors, the
    API and the thermostat arming can never disagree, and the two edges always come from the
    same band. Unlike Target Temperature they never mirror the thermostat's live setpoint, so
    an observe-only install sees what CA would hold, not what its own controller set.

    Native °F like the other temperature sensors; HA converts to the system unit.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _edge: str  # "heat" | "cool"
    _resolve_failed_warned: bool = False

    def _thresholds(self) -> dict[str, Any] | None:
        try:
            return active_thresholds_for(self.coordinator)
        except Exception:  # noqa: BLE001 - a resolver fault must not break the state write
            if not self._resolve_failed_warned:
                _LOGGER.warning("Threshold unavailable: sensor=%s", self._key, exc_info=True)
                self._resolve_failed_warned = True
            return None

    @property
    def native_value(self) -> float | None:
        """The threshold in °F, or None when it cannot be resolved."""
        thresholds = self._thresholds()
        if thresholds is None or thresholds[self._edge] is None:
            return None
        return float(thresholds[self._edge])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which band is in force (comfort / prefer_savings / sleep / away / vacation / tou_expansion).

        ``provisional`` is True right after a restart, before the first classification: the
        value is then a clock-only placeholder that nothing is armed with yet.
        """
        thresholds = self._thresholds()
        if thresholds is None:
            return {"context": None, "provisional": None}
        return {"context": thresholds["context"], "provisional": thresholds["provisional"]}


class ClimateAdvisorHeatThresholdSensor(_ClimateAdvisorThresholdSensor):
    """Heat threshold in force: the comfort band floor (Issue #1345)."""

    _edge = "heat"

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the heat threshold sensor."""
        super().__init__(coordinator, entry, ATTR_HEAT_THRESHOLD, "mdi:thermometer-low")


class ClimateAdvisorCoolThresholdSensor(_ClimateAdvisorThresholdSensor):
    """Cool threshold in force: the comfort band ceiling (Issue #1345)."""

    _edge = "cool"

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the cool threshold sensor."""
        super().__init__(coordinator, entry, ATTR_COOL_THRESHOLD, "mdi:thermometer-high")


class ClimateAdvisorForecastHighSensor(ClimateAdvisorBaseSensor):
    """Today's forecast high temperature, tracked by HA Recorder for long-term history."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the forecast high temperature sensor."""
        super().__init__(coordinator, entry, ATTR_FORECAST_HIGH, "mdi:thermometer-chevron-up")


class ClimateAdvisorForecastLowSensor(ClimateAdvisorBaseSensor):
    """Today's forecast low temperature, tracked by HA Recorder for long-term history."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, coordinator: ClimateAdvisorCoordinator, entry: ConfigEntry) -> None:
        """Initialize the forecast low temperature sensor."""
        super().__init__(coordinator, entry, ATTR_FORECAST_LOW, "mdi:thermometer-chevron-down")
