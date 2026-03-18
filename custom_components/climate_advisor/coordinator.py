"""Data coordinator for Climate Advisor.

The coordinator is the central brain. It runs on a schedule, pulls forecast
data, classifies the day, triggers automations, sends briefings, and feeds
data to the learning engine.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .automation import AutomationEngine
from .briefing import generate_briefing
from .classifier import ForecastSnapshot, DayClassification, classify_day
from .learning import LearningEngine, DailyRecord
from .const import (
    DOMAIN,
    DOOR_WINDOW_PAUSE_SECONDS,
    OCCUPANCY_SETBACK_MINUTES,
    ATTR_DAY_TYPE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    ATTR_BRIEFING,
    ATTR_NEXT_ACTION,
    ATTR_AUTOMATION_STATUS,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_COMPLIANCE_SCORE,
)

_LOGGER = logging.getLogger(__name__)


class ClimateAdvisorCoordinator(DataUpdateCoordinator):
    """Coordinate all Climate Advisor activities."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
        )
        self.config = config
        self._unsub_listeners: list[Any] = []

        # Sub-components
        self.learning = LearningEngine(Path(hass.config.config_dir))
        self.automation_engine = AutomationEngine(
            hass=hass,
            climate_entity=config["climate_entity"],
            weather_entity=config["weather_entity"],
            door_window_sensors=config.get("door_window_sensors", []),
            notify_service=config["notify_service"],
            config=config,
        )

        # State
        self._current_classification: DayClassification | None = None
        self._today_record: DailyRecord | None = None
        self._briefing_sent_today = False
        self._door_open_timers: dict[str, Any] = {}

    async def async_setup(self) -> None:
        """Set up scheduled events and state listeners."""

        # Parse schedule times
        briefing_time = _parse_time(self.config.get("briefing_time", "06:00"))
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        # Schedule: daily briefing
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_send_briefing,
                hour=briefing_time.hour,
                minute=briefing_time.minute,
                second=0,
            )
        )

        # Schedule: morning wake-up
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_morning_wakeup,
                hour=wake_time.hour,
                minute=wake_time.minute,
                second=0,
            )
        )

        # Schedule: bedtime
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_bedtime,
                hour=sleep_time.hour,
                minute=sleep_time.minute,
                second=0,
            )
        )

        # Schedule: midnight — finalize daily record and reset
        self._unsub_listeners.append(
            async_track_time_change(
                self.hass,
                self._async_end_of_day,
                hour=23,
                minute=59,
                second=0,
            )
        )

        # Listeners: door/window sensors
        for sensor_id in self.config.get("door_window_sensors", []):
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    sensor_id,
                    self._async_door_window_changed,
                )
            )

        # Listeners: thermostat state (for tracking manual overrides and runtime)
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                self.config["climate_entity"],
                self._async_thermostat_changed,
            )
        )

        _LOGGER.info("Climate Advisor coordinator setup complete")

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min)."""
        forecast = await self._get_forecast()
        if forecast:
            self._current_classification = classify_day(forecast)
            await self.automation_engine.apply_classification(self._current_classification)

        # Build the data dict that sensors will read
        c = self._current_classification
        suggestions = self.learning.generate_suggestions()
        compliance = self.learning.get_compliance_summary()

        return {
            ATTR_DAY_TYPE: c.day_type if c else "unknown",
            ATTR_TREND: c.trend_direction if c else "unknown",
            ATTR_TREND_MAGNITUDE: c.trend_magnitude if c else 0,
            ATTR_BRIEFING: self._last_briefing if hasattr(self, "_last_briefing") else "",
            ATTR_NEXT_ACTION: self._compute_next_action(c),
            ATTR_AUTOMATION_STATUS: "active",
            ATTR_LEARNING_SUGGESTIONS: suggestions,
            ATTR_COMPLIANCE_SCORE: compliance.get("comfort_score", 1.0),
        }

    async def _get_forecast(self) -> ForecastSnapshot | None:
        """Pull forecast data from the weather entity."""
        weather_state = self.hass.states.get(self.config["weather_entity"])
        if not weather_state:
            _LOGGER.warning("Weather entity %s not available", self.config["weather_entity"])
            return None

        attrs = weather_state.attributes
        forecast = attrs.get("forecast", [])

        # Current outdoor temp — prefer dedicated sensor, fall back to weather entity
        outdoor_entity = self.config.get("outdoor_temp_entity")
        if outdoor_entity:
            outdoor_state = self.hass.states.get(outdoor_entity)
            current_outdoor = float(outdoor_state.state) if outdoor_state else attrs.get("temperature", 65)
        else:
            current_outdoor = attrs.get("temperature", 65)

        # Indoor temp — prefer dedicated sensor, fall back to climate entity
        indoor_entity = self.config.get("indoor_temp_entity")
        if indoor_entity:
            indoor_state = self.hass.states.get(indoor_entity)
            current_indoor = float(indoor_state.state) if indoor_state else None
        else:
            climate_state = self.hass.states.get(self.config["climate_entity"])
            current_indoor = (
                climate_state.attributes.get("current_temperature")
                if climate_state
                else None
            )

        # Extract today and tomorrow from forecast
        # Forecast structure varies by integration; handle common patterns
        today_high = current_outdoor
        today_low = current_outdoor
        tomorrow_high = current_outdoor
        tomorrow_low = current_outdoor

        if forecast and len(forecast) >= 2:
            today_fc = forecast[0]
            tomorrow_fc = forecast[1]
            today_high = today_fc.get("temperature", today_fc.get("tempHigh", current_outdoor))
            today_low = today_fc.get("templow", today_fc.get("tempLow", current_outdoor - 15))
            tomorrow_high = tomorrow_fc.get("temperature", tomorrow_fc.get("tempHigh", current_outdoor))
            tomorrow_low = tomorrow_fc.get("templow", tomorrow_fc.get("tempLow", current_outdoor - 15))

        return ForecastSnapshot(
            today_high=float(today_high),
            today_low=float(today_low),
            tomorrow_high=float(tomorrow_high),
            tomorrow_low=float(tomorrow_low),
            current_outdoor_temp=float(current_outdoor),
            current_indoor_temp=float(current_indoor) if current_indoor else None,
            current_humidity=attrs.get("humidity"),
            timestamp=dt_util.now(),
        )

    @callback
    async def _async_send_briefing(self, now: datetime) -> None:
        """Generate and send the daily briefing."""
        if self._briefing_sent_today:
            return

        forecast = await self._get_forecast()
        if not forecast:
            return

        classification = classify_day(forecast)
        self._current_classification = classification
        await self.automation_engine.apply_classification(classification)

        # Initialize today's learning record
        self._today_record = DailyRecord(
            date=dt_util.now().strftime("%Y-%m-%d"),
            day_type=classification.day_type,
            trend_direction=classification.trend_direction,
            windows_recommended=classification.windows_recommended,
            window_open_time=(
                classification.window_open_time.isoformat()
                if classification.window_open_time
                else None
            ),
            window_close_time=(
                classification.window_close_time.isoformat()
                if classification.window_close_time
                else None
            ),
            hvac_mode_recommended=classification.hvac_mode,
        )

        # Generate briefing text
        suggestions = self.learning.generate_suggestions()
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        briefing = generate_briefing(
            classification=classification,
            comfort_heat=self.config["comfort_heat"],
            comfort_cool=self.config["comfort_cool"],
            setback_heat=self.config["setback_heat"],
            setback_cool=self.config["setback_cool"],
            wake_time=wake_time,
            sleep_time=sleep_time,
            learning_suggestions=suggestions if suggestions else None,
        )

        self._last_briefing = briefing

        # Send notification
        await self.hass.services.async_call(
            "notify",
            self.config["notify_service"].replace("notify.", ""),
            {
                "message": briefing,
                "title": "🏠 Your Home Climate Plan for Today",
            },
        )

        self._briefing_sent_today = True
        _LOGGER.info("Daily briefing sent — day type: %s", classification.day_type)

    @callback
    async def _async_morning_wakeup(self, now: datetime) -> None:
        """Handle morning wake-up."""
        await self.automation_engine.handle_morning_wakeup()

    @callback
    async def _async_bedtime(self, now: datetime) -> None:
        """Handle bedtime setback."""
        await self.automation_engine.handle_bedtime()

    @callback
    async def _async_end_of_day(self, now: datetime) -> None:
        """Finalize the day's record and reset for tomorrow."""
        if self._today_record:
            self.learning.record_day(self._today_record)
            _LOGGER.info("Day record saved for learning")

        self._today_record = None
        self._briefing_sent_today = False

    @callback
    async def _async_door_window_changed(self, event: Event) -> None:
        """Handle a door/window sensor state change."""
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")
        if not new_state:
            return

        if new_state.state == "on":  # "on" typically means open for binary sensors
            # Start a timer — if still open after threshold, pause HVAC
            _LOGGER.debug("Door/window opened: %s — starting timer", entity_id)
            # In a full implementation, use async_call_later for the debounce
            # For now, signal the automation engine
            await self.automation_engine.handle_door_window_open(entity_id)
            if self._today_record:
                self._today_record.door_window_pause_events += 1
        else:
            # Check if ALL monitored sensors are closed
            all_closed = all(
                self.hass.states.is_state(s, "off")
                for s in self.config.get("door_window_sensors", [])
            )
            if all_closed:
                await self.automation_engine.handle_all_doors_windows_closed()

    @callback
    async def _async_thermostat_changed(self, event: Event) -> None:
        """Track thermostat changes for learning (detect manual overrides)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        # Detect manual override: temperature changed but not by us
        new_temp = new_state.attributes.get("temperature")
        old_temp = old_state.attributes.get("temperature")

        if new_temp != old_temp and self._today_record:
            # This is a rough heuristic — in production you'd track which
            # changes were initiated by the integration vs. manual
            self._today_record.manual_overrides += 1
            _LOGGER.debug("Possible manual override detected: %s -> %s", old_temp, new_temp)

    def _compute_next_action(self, c: DayClassification | None) -> str:
        """Compute the next recommended human action for display."""
        if not c:
            return "Waiting for forecast data..."

        now = dt_util.now().time()

        if c.windows_recommended:
            if c.window_open_time and now < c.window_open_time:
                return f"Open windows at {c.window_open_time.strftime('%I:%M %p')}"
            elif c.window_close_time and now < c.window_close_time:
                return f"Close windows by {c.window_close_time.strftime('%I:%M %p')}"

        if c.day_type == DAY_TYPE_HOT:
            return "Keep windows and blinds closed. AC is handling it."
        elif c.day_type == DAY_TYPE_COLD:
            return "Keep doors closed — help the heater out."

        return "No action needed right now. Automation is handling it."

    async def async_shutdown(self) -> None:
        """Clean up on shutdown."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        self.automation_engine.cleanup()


def _parse_time(time_str: str) -> time:
    """Parse a time string like '06:30' into a time object."""
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))
