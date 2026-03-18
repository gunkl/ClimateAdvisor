"""Automation engine for Climate Advisor.

Manages the creation and dynamic adjustment of Home Assistant automations
based on the day classification and learning state.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .classifier import DayClassification
from .const import (
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class AutomationEngine:
    """Manages HVAC automations based on daily classification."""

    def __init__(
        self,
        hass: HomeAssistant,
        climate_entity: str,
        weather_entity: str,
        door_window_sensors: list[str],
        notify_service: str,
        config: dict[str, Any],
        sensor_polarity_inverted: bool = False,
    ) -> None:
        """Initialize the automation engine."""
        self.hass = hass
        self.climate_entity = climate_entity
        self.weather_entity = weather_entity
        self.door_window_sensors = door_window_sensors
        self.notify_service = notify_service
        self.config = config
        self.sensor_polarity_inverted = sensor_polarity_inverted
        self._active_listeners: list[Any] = []
        self._current_classification: DayClassification | None = None
        self._paused_by_door = False
        self._pre_pause_mode: str | None = None

        # Grace period state
        self._manual_grace_cancel: Any | None = None
        self._automation_grace_cancel: Any | None = None
        self._grace_active = False
        self._last_resume_source: str | None = None

    @property
    def is_paused_by_door(self) -> bool:
        """Whether HVAC is currently paused due to an open door/window."""
        return self._paused_by_door

    async def apply_classification(self, classification: DayClassification) -> None:
        """Apply a new day classification — adjust HVAC behavior accordingly.

        This is called once in the morning and can be called again if
        conditions change significantly mid-day.
        """
        self._current_classification = classification
        _LOGGER.info(
            "Applying classification: %s (trend: %s %s°F)",
            classification.day_type,
            classification.trend_direction,
            classification.trend_magnitude,
        )

        # Set the base HVAC mode
        if classification.hvac_mode in ("heat", "cool"):
            await self._set_hvac_mode(classification.hvac_mode)
            await self._set_temperature_for_mode(classification)
        elif classification.hvac_mode == "off":
            await self._set_hvac_mode("off")

        # Handle pre-conditioning
        if classification.pre_condition and classification.pre_condition_target:
            await self._schedule_pre_condition(classification)

    async def _set_hvac_mode(self, mode: str) -> None:
        """Set the thermostat HVAC mode."""
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.climate_entity, "hvac_mode": mode},
        )
        _LOGGER.debug("Set HVAC mode to %s", mode)

    async def _set_temperature(self, temperature: float) -> None:
        """Set the thermostat target temperature."""
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": self.climate_entity, "temperature": temperature},
        )
        _LOGGER.debug("Set temperature to %s°F", temperature)

    async def _set_temperature_for_mode(self, c: DayClassification) -> None:
        """Set temperature based on the classification and current period."""
        if c.hvac_mode == "heat":
            target = self.config["comfort_heat"]
        elif c.hvac_mode == "cool":
            target = self.config["comfort_cool"]
            if c.pre_condition and c.pre_condition_target and c.pre_condition_target < 0:
                # Pre-cool: target is below comfort
                target = target + c.pre_condition_target
        else:
            return

        await self._set_temperature(target)

    async def _schedule_pre_condition(self, c: DayClassification) -> None:
        """Schedule pre-heating or pre-cooling based on trend.

        For warming trends: more aggressive setback (handled by setback_modifier)
        For cooling trends: pre-heat in the evening
        """
        if c.trend_direction == "cooling" and c.pre_condition_target and c.pre_condition_target > 0:
            # Pre-heat: schedule a bump for 7pm
            preheat_target = self.config["comfort_heat"] + c.pre_condition_target
            _LOGGER.info(
                "Scheduling pre-heat to %s°F for this evening (cold front coming)",
                preheat_target,
            )
            # In a full implementation, this would register a time-based listener
            # For now, store the intent for the coordinator to act on
            self.config["_pending_preheat"] = {
                "time": "19:00",
                "target": preheat_target,
                "duration_hours": 2,
            }

    async def handle_door_window_open(self, entity_id: str) -> None:
        """Handle a door/window being opened for longer than the debounce period.

        Called by the coordinator after the debounce period.
        """
        if self._paused_by_door:
            return  # Already paused

        if self._grace_active:
            _LOGGER.info(
                "Door/window open (%s) but %s grace period active — not pausing",
                entity_id,
                self._last_resume_source,
            )
            return

        # Get current mode before pausing
        state = self.hass.states.get(self.climate_entity)
        if state:
            self._pre_pause_mode = state.state

        if self._pre_pause_mode and self._pre_pause_mode != "off":
            self._paused_by_door = True
            await self._set_hvac_mode("off")

            # Notify
            debounce_minutes = self.config.get(
                CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS
            ) // 60
            friendly_name = entity_id.split(".")[-1].replace("_", " ").title()
            service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
            await self.hass.services.async_call(
                "notify",
                service_name,
                {
                    "message": (
                        f"🚪 HVAC paused — {friendly_name} has been open for "
                        f"{debounce_minutes} minutes. "
                        f"Heating/cooling will resume when it's closed."
                    ),
                    "title": "Climate Advisor",
                },
            )
            _LOGGER.info("Paused HVAC due to open: %s", entity_id)

    async def handle_all_doors_windows_closed(self) -> None:
        """Resume HVAC after all monitored doors/windows are closed."""
        if not self._paused_by_door:
            return

        self._paused_by_door = False
        if self._pre_pause_mode:
            await self._set_hvac_mode(self._pre_pause_mode)
            if self._current_classification:
                await self._set_temperature_for_mode(self._current_classification)
            _LOGGER.info("Resumed HVAC after doors/windows closed")
            self._start_grace_period("automation")
        self._pre_pause_mode = None

    async def handle_manual_override_during_pause(self) -> None:
        """Handle when user manually turns HVAC on during a sensor pause.

        Called by the coordinator when it detects a thermostat mode change
        from 'off' to something else while paused_by_door is True.
        """
        if not self._paused_by_door:
            return
        _LOGGER.info("Manual HVAC override detected during door/window pause")
        self._paused_by_door = False
        self._pre_pause_mode = None
        self._start_grace_period("manual")

    def _start_grace_period(self, source: str) -> None:
        """Start a grace period after HVAC is resumed.

        Args:
            source: "manual" for user-initiated overrides,
                    "automation" for Climate Advisor resumptions.
        """
        self._cancel_grace_timers()

        if source == "manual":
            duration = self.config.get(
                CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS
            )
            should_notify = self.config.get(CONF_MANUAL_GRACE_NOTIFY, False)
        else:
            duration = self.config.get(
                CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS
            )
            should_notify = self.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True)

        if duration <= 0:
            return  # Grace period disabled

        self._grace_active = True
        self._last_resume_source = source

        @callback
        def _grace_expired(_now: Any) -> None:
            """Grace period has elapsed."""
            self._grace_active = False
            self._last_resume_source = None
            self._manual_grace_cancel = None
            self._automation_grace_cancel = None
            _LOGGER.info("%s grace period expired (%d seconds)", source, duration)

            if should_notify:
                service_name = (
                    self.notify_service.split(".")[-1]
                    if "." in self.notify_service
                    else self.notify_service
                )
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "notify",
                        service_name,
                        {
                            "message": (
                                f"⏱️ {source.capitalize()} grace period expired "
                                f"({duration // 60} minutes). HVAC will now respond "
                                f"normally to door/window sensor changes."
                            ),
                            "title": "Climate Advisor",
                        },
                    )
                )

        cancel = async_call_later(self.hass, duration, _grace_expired)
        if source == "manual":
            self._manual_grace_cancel = cancel
        else:
            self._automation_grace_cancel = cancel

        _LOGGER.info("Started %s grace period (%d seconds)", source, duration)

    def _cancel_grace_timers(self) -> None:
        """Cancel any active grace period timers."""
        if self._manual_grace_cancel:
            self._manual_grace_cancel()
            self._manual_grace_cancel = None
        if self._automation_grace_cancel:
            self._automation_grace_cancel()
            self._automation_grace_cancel = None
        self._grace_active = False
        self._last_resume_source = None

    async def handle_occupancy_away(self) -> None:
        """Handle everyone leaving — apply setback."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            setback = self.config["setback_heat"] + c.setback_modifier
            await self._set_temperature(setback)
            _LOGGER.info("Occupancy away — heat setback to %s°F", setback)
        elif c.hvac_mode == "cool":
            setback = self.config["setback_cool"] - c.setback_modifier
            await self._set_temperature(setback)
            _LOGGER.info("Occupancy away — cool setback to %s°F", setback)

    async def handle_occupancy_home(self) -> None:
        """Handle someone returning — restore comfort."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode in ("heat", "cool"):
            await self._set_temperature_for_mode(c)
            _LOGGER.info("Occupancy returned — restoring comfort setpoint")

        # Notify with estimated recovery time
        service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
        await self.hass.services.async_call(
            "notify",
            service_name,
            {
                "message": "🏠 Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes.",
                "title": "Climate Advisor",
            },
        )

    async def handle_bedtime(self) -> None:
        """Apply bedtime setback."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            bedtime_target = self.config["comfort_heat"] - 4 + c.setback_modifier
            await self._set_temperature(bedtime_target)
            _LOGGER.info("Bedtime setback — heat to %s°F", bedtime_target)
        elif c.hvac_mode == "cool":
            bedtime_target = self.config["comfort_cool"] + 3
            await self._set_temperature(bedtime_target)
            _LOGGER.info("Bedtime setback — cool to %s°F", bedtime_target)

    async def handle_morning_wakeup(self) -> None:
        """Restore comfort for morning wake-up."""
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            await self._set_temperature(self.config["comfort_heat"])
        elif c.hvac_mode == "cool":
            await self._set_temperature(self.config["comfort_cool"])

        _LOGGER.info("Morning wake-up — restoring comfort setpoint")

    def cleanup(self) -> None:
        """Remove all active listeners and cancel pending timers."""
        self._cancel_grace_timers()
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
