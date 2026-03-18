"""Automation engine for Climate Advisor.

Manages the creation and dynamic adjustment of Home Assistant automations
based on the day classification and learning state.
"""
from __future__ import annotations

import logging
from datetime import time
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_change, async_track_state_change_event
from homeassistant.const import STATE_ON, STATE_OFF

from .classifier import DayClassification
from .const import (
    DOMAIN,
    DAY_TYPE_HOT,
    DAY_TYPE_WARM,
    DAY_TYPE_MILD,
    DAY_TYPE_COOL,
    DAY_TYPE_COLD,
    DOOR_WINDOW_PAUSE_SECONDS,
    OCCUPANCY_SETBACK_MINUTES,
    MAX_CONTINUOUS_RUNTIME_HOURS,
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
    ) -> None:
        """Initialize the automation engine."""
        self.hass = hass
        self.climate_entity = climate_entity
        self.weather_entity = weather_entity
        self.door_window_sensors = door_window_sensors
        self.notify_service = notify_service
        self.config = config
        self._active_listeners: list[Any] = []
        self._current_classification: DayClassification | None = None
        self._paused_by_door = False
        self._pre_pause_mode: str | None = None

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
        """Handle a door/window being opened for longer than the threshold.

        Called by the coordinator after the debounce period.
        """
        if self._paused_by_door:
            return  # Already paused

        # Get current mode before pausing
        state = self.hass.states.get(self.climate_entity)
        if state:
            self._pre_pause_mode = state.state

        if self._pre_pause_mode and self._pre_pause_mode != "off":
            self._paused_by_door = True
            await self._set_hvac_mode("off")

            # Notify
            friendly_name = entity_id.split(".")[-1].replace("_", " ").title()
            service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
            await self.hass.services.async_call(
                "notify",
                service_name,
                {
                    "message": (
                        f"🚪 HVAC paused — {friendly_name} has been open for "
                        f"{DOOR_WINDOW_PAUSE_SECONDS // 60} minutes. "
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
        self._pre_pause_mode = None

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
        """Remove all active listeners."""
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
