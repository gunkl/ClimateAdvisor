"""Data coordinator for Climate Advisor.

The coordinator is the central brain. It runs on a schedule, pulls forecast
data, classifies the day, triggers automations, sends briefings, and feeds
data to the learning engine.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_change,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .automation import AutomationEngine
from .briefing import generate_briefing
from .classifier import ForecastSnapshot, DayClassification, classify_day
from .learning import LearningEngine, DailyRecord
from .state import StatePersistence
from .const import (
    CONF_SENSOR_POLARITY_INVERTED,
    DOMAIN,
    DAY_TYPE_HOT,
    DAY_TYPE_COLD,
    ATTR_DAY_TYPE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    ATTR_BRIEFING,
    ATTR_NEXT_ACTION,
    ATTR_AUTOMATION_STATUS,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_COMPLIANCE_SCORE,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_WEATHER_SERVICE,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    CONF_EMAIL_NOTIFY,
    CONF_SENSOR_DEBOUNCE,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_AUTOMATION_GRACE_PERIOD,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
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
        self._unsub_dw_listeners: list[Any] = []
        self._resolved_sensors: list[str] = []

        # Sub-components
        self._state_persistence = StatePersistence(Path(hass.config.config_dir))
        self.learning = LearningEngine(Path(hass.config.config_dir))
        self.automation_engine = AutomationEngine(
            hass=hass,
            climate_entity=config["climate_entity"],
            weather_entity=config["weather_entity"],
            door_window_sensors=config.get("door_window_sensors", []),
            notify_service=config["notify_service"],
            config=config,
            sensor_polarity_inverted=config.get(CONF_SENSOR_POLARITY_INVERTED, False),
        )

        # State
        self._current_classification: DayClassification | None = None
        self._today_record: DailyRecord | None = None
        self._briefing_sent_today = False
        self._last_briefing: str = ""
        self._door_open_timers: dict[str, Any] = {}

        # Startup retry state — gentle backoff when weather entity isn't ready
        self._startup_retries_remaining: int = 5
        self._startup_retry_delay: int = 30  # seconds; doubles each attempt

        # Temperature history for dashboard chart (cleared at end of day)
        self._outdoor_temp_history: list[tuple[str, float]] = []
        self._indoor_temp_history: list[tuple[str, float]] = []

        # HVAC runtime tracking
        self._hvac_on_since: datetime | None = None

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

        # Listeners: door/window sensors (resolve groups into individual sensors)
        self._resolved_sensors = self._resolve_monitored_sensors()
        self._subscribe_door_window_listeners()

        # Listeners: thermostat state (for tracking manual overrides and runtime)
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                self.config["climate_entity"],
                self._async_thermostat_changed,
            )
        )

        _LOGGER.info("Climate Advisor coordinator setup complete")

    async def async_restore_state(self) -> None:
        """Restore operational state from disk after startup."""
        state = await self.hass.async_add_executor_job(
            self._state_persistence.load
        )
        if not state:
            _LOGGER.debug("No persisted state found — starting fresh")
            return

        today_str = dt_util.now().strftime("%Y-%m-%d")
        state_date = state.get("date", "")
        yesterday_str = (
            dt_util.now() - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        # If the state is from yesterday, recover the DailyRecord to learning
        if state_date == yesterday_str and state.get("today_record"):
            try:
                recovered = DailyRecord(**state["today_record"])
                self.learning.record_day(recovered)
                _LOGGER.info("Recovered yesterday's record during startup")
            except (TypeError, KeyError) as err:
                _LOGGER.warning("Failed to recover yesterday's record: %s", err)

        if state_date != today_str:
            _LOGGER.debug(
                "Persisted state is from %s (today is %s) — starting fresh",
                state_date,
                today_str,
            )
            return

        # Same-day restore
        _LOGGER.info("Restoring same-day state from %s", state.get("last_saved"))

        # Classification
        cls_data = state.get("classification")
        if cls_data:
            try:
                wot = cls_data.get("window_open_time")
                wct = cls_data.get("window_close_time")
                self._current_classification = DayClassification(
                    day_type=cls_data["day_type"],
                    trend_direction=cls_data["trend_direction"],
                    trend_magnitude=cls_data.get("trend_magnitude", 0),
                    today_high=cls_data["today_high"],
                    today_low=cls_data["today_low"],
                    tomorrow_high=cls_data["tomorrow_high"],
                    tomorrow_low=cls_data["tomorrow_low"],
                    hvac_mode=cls_data.get("hvac_mode", ""),
                    pre_condition=cls_data.get("pre_condition", False),
                    pre_condition_target=cls_data.get("pre_condition_target"),
                    windows_recommended=cls_data.get("windows_recommended", False),
                    window_open_time=(
                        time.fromisoformat(wot) if wot else None
                    ),
                    window_close_time=(
                        time.fromisoformat(wct) if wct else None
                    ),
                    setback_modifier=cls_data.get("setback_modifier", 0.0),
                )
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Failed to restore classification: %s", err)

        # Temperature history
        temp_hist = state.get("temp_history", {})
        self._outdoor_temp_history = [
            (ts, t) for ts, t in temp_hist.get("outdoor", [])
        ]
        self._indoor_temp_history = [
            (ts, t) for ts, t in temp_hist.get("indoor", [])
        ]

        # Today's record
        record_data = state.get("today_record")
        if record_data:
            try:
                self._today_record = DailyRecord(**record_data)
            except (TypeError, KeyError) as err:
                _LOGGER.warning("Failed to restore today's record: %s", err)

        # Briefing state
        briefing = state.get("briefing_state", {})
        self._briefing_sent_today = briefing.get("sent_today", False)
        self._last_briefing = briefing.get("last_text", "")

        # Automation state
        auto_state = state.get("automation_state", {})
        if auto_state:
            self.automation_engine.restore_state(auto_state)

        _LOGGER.info("State restore complete")

    def _build_state_dict(self) -> dict[str, Any]:
        """Serialize current operational state for persistence."""
        c = self._current_classification
        cls_dict = None
        if c:
            cls_dict = {
                "day_type": c.day_type,
                "trend_direction": c.trend_direction,
                "trend_magnitude": c.trend_magnitude,
                "today_high": c.today_high,
                "today_low": c.today_low,
                "tomorrow_high": c.tomorrow_high,
                "tomorrow_low": c.tomorrow_low,
                "hvac_mode": c.hvac_mode,
                "pre_condition": c.pre_condition,
                "pre_condition_target": c.pre_condition_target,
                "windows_recommended": c.windows_recommended,
                "window_open_time": (
                    c.window_open_time.isoformat() if c.window_open_time else None
                ),
                "window_close_time": (
                    c.window_close_time.isoformat() if c.window_close_time else None
                ),
                "setback_modifier": c.setback_modifier,
            }

        record_dict = None
        if self._today_record:
            from dataclasses import asdict
            record_dict = asdict(self._today_record)

        return {
            "date": dt_util.now().strftime("%Y-%m-%d"),
            "last_saved": dt_util.now().isoformat(),
            "classification": cls_dict,
            "temp_history": {
                "outdoor": list(self._outdoor_temp_history),
                "indoor": list(self._indoor_temp_history),
            },
            "automation_state": self.automation_engine.get_serializable_state(),
            "today_record": record_dict,
            "briefing_state": {
                "sent_today": self._briefing_sent_today,
                "last_text": self._last_briefing,
            },
        }

    async def _async_save_state(self) -> None:
        """Persist current operational state to disk."""
        state_dict = self._build_state_dict()
        await self.hass.async_add_executor_job(
            self._state_persistence.save, state_dict
        )

    def _flush_hvac_runtime(self) -> None:
        """Flush accumulated HVAC runtime to today's record."""
        if self._hvac_on_since and self._today_record:
            now = dt_util.now()
            elapsed = (now - self._hvac_on_since).total_seconds() / 60.0
            self._today_record.hvac_runtime_minutes += elapsed
            self._hvac_on_since = now  # Reset to now for continued tracking

    def _resolve_monitored_sensors(self) -> list[str]:
        """Resolve all monitored sensor entity IDs.

        Returns the configured door_window_sensors list directly. Binary sensor
        groups in HA are themselves binary_sensor entities, so they can be
        monitored without expansion — their state reflects member states.
        """
        return list(self.config.get("door_window_sensors", []))

    def _subscribe_door_window_listeners(self) -> None:
        """Subscribe to state changes for all resolved door/window sensors."""
        for sensor_id in self._resolved_sensors:
            self._unsub_dw_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    sensor_id,
                    self._async_door_window_changed,
                )
            )

    def _unsubscribe_door_window_listeners(self) -> None:
        """Unsubscribe all door/window sensor listeners."""
        for unsub in self._unsub_dw_listeners:
            unsub()
        self._unsub_dw_listeners.clear()

    def _is_sensor_open(self, entity_id: str) -> bool:
        """Check if a door/window sensor is in the 'open' state, respecting polarity."""
        inverted = self.config.get(CONF_SENSOR_POLARITY_INVERTED, False)
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        if inverted:
            return state.state == "off"
        return state.state == "on"

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min)."""
        # Re-resolve group membership in case it changed
        new_resolved = self._resolve_monitored_sensors()
        if set(new_resolved) != set(self._resolved_sensors):
            _LOGGER.info(
                "Door/window sensor membership changed; updating listeners"
            )
            self._unsubscribe_door_window_listeners()
            self._resolved_sensors = new_resolved
            self._subscribe_door_window_listeners()

        forecast = await self._get_forecast()
        if forecast:
            self._current_classification = classify_day(forecast)
            await self.automation_engine.apply_classification(self._current_classification)

            # Reset startup retry state on success
            if self._startup_retries_remaining < 5:
                _LOGGER.debug("Weather entity available; startup retry cleared")
                self._startup_retries_remaining = 5
                self._startup_retry_delay = 30

            # Record temperature history for dashboard chart
            now_str = dt_util.now().isoformat()
            self._outdoor_temp_history.append(
                (now_str, forecast.current_outdoor_temp)
            )
            if forecast.current_indoor_temp is not None:
                self._indoor_temp_history.append(
                    (now_str, forecast.current_indoor_temp)
                )

                # Track comfort violations (~30 min per update cycle)
                if self._today_record:
                    comfort_low = self.config.get("comfort_heat", 70)
                    comfort_high = self.config.get("comfort_cool", 75)
                    if (
                        forecast.current_indoor_temp < comfort_low
                        or forecast.current_indoor_temp > comfort_high
                    ):
                        self._today_record.comfort_violations_minutes += 30.0

            # Save state after classification update
            await self._async_save_state()
        else:
            # Weather entity not ready yet (common after HA restart).
            # Retry with gentle backoff: 30s → 60s → 120s → 240s → 480s
            # Total wait ≈ 15 min before falling back to normal 30-min poll.
            if self._startup_retries_remaining > 0:
                delay = self._startup_retry_delay
                self._startup_retries_remaining -= 1
                self._startup_retry_delay = min(delay * 2, 480)
                _LOGGER.debug(
                    "Weather entity not ready; retry %d remaining in %ds",
                    self._startup_retries_remaining + 1,
                    delay,
                )
                async_call_later(
                    self.hass, delay, lambda _: self.async_request_refresh()
                )
            else:
                _LOGGER.warning(
                    "Weather entity still unavailable after startup retries; "
                    "will try again at next scheduled update"
                )

        # Build the data dict that sensors will read
        c = self._current_classification
        suggestions = self.learning.generate_suggestions()
        compliance = self.learning.get_compliance_summary()

        return {
            ATTR_DAY_TYPE: c.day_type if c else "unknown",
            ATTR_TREND: c.trend_direction if c else "unknown",
            ATTR_TREND_MAGNITUDE: c.trend_magnitude if c else 0,
            ATTR_BRIEFING: self._last_briefing,
            ATTR_NEXT_ACTION: self._compute_next_action(c),
            ATTR_AUTOMATION_STATUS: "active",
            ATTR_LEARNING_SUGGESTIONS: suggestions,
            ATTR_COMPLIANCE_SCORE: compliance.get("comfort_score", 1.0),
        }

    def _get_outdoor_temp(self, weather_attrs: dict) -> float:
        """Read outdoor temperature based on configured source type."""
        source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)

        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            entity_id = self.config.get("outdoor_temp_entity")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state:
                    try:
                        return float(state.state)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Outdoor temp entity %s has non-numeric state %r; "
                            "falling back to weather attribute",
                            entity_id,
                            state.state,
                        )

        # weather_service source or fallback
        return float(weather_attrs.get("temperature", 65))

    def _get_indoor_temp(self) -> float | None:
        """Read indoor temperature based on configured source type."""
        source = self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)

        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            entity_id = self.config.get("indoor_temp_entity")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state:
                    try:
                        return float(state.state)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Indoor temp entity %s has non-numeric state %r; "
                            "treating as unavailable",
                            entity_id,
                            state.state,
                        )
            return None

        # climate_fallback source
        climate_state = self.hass.states.get(self.config["climate_entity"])
        if climate_state:
            temp = climate_state.attributes.get("current_temperature")
            return float(temp) if temp is not None else None
        return None

    async def _get_forecast_data(self) -> list:
        """Get forecast data using the weather.get_forecasts service.

        Falls back to the deprecated forecast attribute if the service
        call is unavailable.
        """
        weather_entity = self.config["weather_entity"]
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            forecasts = (
                response.get(weather_entity, {}).get("forecast", [])
                if response
                else []
            )
            if forecasts:
                return forecasts
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "weather.get_forecasts service call failed for %s; "
                "falling back to forecast attribute",
                weather_entity,
            )

        # Fallback: deprecated forecast attribute
        weather_state = self.hass.states.get(weather_entity)
        if weather_state:
            return weather_state.attributes.get("forecast", [])
        return []

    async def _get_forecast(self) -> ForecastSnapshot | None:
        """Pull forecast data from the weather entity."""
        weather_state = self.hass.states.get(self.config["weather_entity"])
        if not weather_state:
            _LOGGER.warning(
                "Weather entity %s not found in Home Assistant. "
                "Check that the entity ID is correct in the integration options.",
                self.config["weather_entity"],
            )
            return None

        attrs = weather_state.attributes

        current_outdoor = self._get_outdoor_temp(attrs)
        current_indoor = self._get_indoor_temp()
        forecast = await self._get_forecast_data()

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
            current_indoor_temp=float(current_indoor) if current_indoor is not None else None,
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
            debounce_seconds=self.config.get(
                CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS
            ),
            manual_grace_seconds=self.config.get(
                CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS
            ),
            automation_grace_seconds=self.config.get(
                CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS
            ),
            grace_active=self.automation_engine._grace_active,
            grace_source=self.automation_engine._last_resume_source,
        )

        self._last_briefing = briefing

        # Send notification
        _notify_svc = self.config["notify_service"]
        _notify_name = _notify_svc.split(".")[-1] if "." in _notify_svc else _notify_svc
        _briefing_payload = {
            "message": briefing,
            "title": "🏠 Your Home Climate Plan for Today",
        }
        await self.hass.services.async_call(
            "notify", _notify_name, _briefing_payload
        )
        if self.config.get(CONF_EMAIL_NOTIFY, True):
            await self.hass.services.async_call(
                "notify", "send_email", _briefing_payload
            )

        self._briefing_sent_today = True
        _LOGGER.info("Daily briefing sent — day type: %s", classification.day_type)
        await self._async_save_state()

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
            # Compute avg indoor temp from history
            if self._indoor_temp_history:
                self._today_record.avg_indoor_temp = round(
                    sum(t for _, t in self._indoor_temp_history)
                    / len(self._indoor_temp_history),
                    1,
                )
            # Flush any accumulated HVAC runtime
            self._flush_hvac_runtime()
            self.learning.record_day(self._today_record)
            _LOGGER.info("Day record saved for learning")

        self._today_record = None
        self._briefing_sent_today = False
        self._hvac_on_since = None
        self._outdoor_temp_history.clear()
        self._indoor_temp_history.clear()
        await self._async_save_state()

    @callback
    async def _async_door_window_changed(self, event: Event) -> None:
        """Handle a door/window sensor state change with debounce."""
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")
        if not new_state:
            return

        if self._is_sensor_open(entity_id):
            # Sensor transitioned to open — start debounce timer if not already running
            if entity_id in self._door_open_timers:
                return  # Timer already pending for this sensor

            debounce_sec = self.config.get(
                CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS
            )
            _LOGGER.debug(
                "Door/window opened: %s — debounce started (%ds)",
                entity_id,
                debounce_sec,
            )

            @callback
            async def _debounce_expired(_now: Any, eid: str = entity_id) -> None:
                """Debounce period elapsed — check if sensor is still open."""
                self._door_open_timers.pop(eid, None)
                if self._is_sensor_open(eid):
                    _LOGGER.debug(
                        "Debounce expired, sensor still open: %s", eid
                    )
                    await self.automation_engine.handle_door_window_open(eid)
                    if self._today_record:
                        self._today_record.door_window_pause_events += 1

                        # Track window compliance during recommended window period
                        c = self._current_classification
                        if (
                            c
                            and c.windows_recommended
                            and c.window_open_time
                            and c.window_close_time
                            and not self._today_record.windows_opened
                        ):
                            now_time = dt_util.now().time()
                            if c.window_open_time <= now_time <= c.window_close_time:
                                self._today_record.windows_opened = True
                                self._today_record.window_open_actual_time = (
                                    dt_util.now().isoformat()
                                )

                        await self._async_save_state()

            cancel = async_call_later(
                self.hass, debounce_sec, _debounce_expired
            )
            self._door_open_timers[entity_id] = cancel
        else:
            # Sensor transitioned to closed — cancel any pending debounce timer
            cancel = self._door_open_timers.pop(entity_id, None)
            if cancel:
                cancel()
                _LOGGER.debug(
                    "Door/window closed during debounce: %s — timer cancelled",
                    entity_id,
                )

            # Check if ALL monitored sensors are now closed
            all_closed = all(
                not self._is_sensor_open(s) for s in self._resolved_sensors
            )
            if all_closed:
                # Track window close time if we were tracking compliance
                if (
                    self._today_record
                    and self._today_record.windows_opened
                    and self._today_record.window_close_actual_time is None
                ):
                    self._today_record.window_close_actual_time = (
                        dt_util.now().isoformat()
                    )
                await self.automation_engine.handle_all_doors_windows_closed()
                await self._async_save_state()

    @callback
    async def _async_thermostat_changed(self, event: Event) -> None:
        """Track thermostat changes for learning (detect manual overrides)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        # Detect manual HVAC override during a door/window pause
        if (
            self.automation_engine.is_paused_by_door
            and old_state.state == "off"
            and new_state.state not in ("off", "unavailable", "unknown")
        ):
            _LOGGER.info(
                "Manual HVAC override detected during door/window pause: %s -> %s",
                old_state.state,
                new_state.state,
            )
            await self.automation_engine.handle_manual_override_during_pause()

        # HVAC runtime tracking via hvac_action (preferred) or mode
        new_action = new_state.attributes.get("hvac_action", "").lower()
        old_action = old_state.attributes.get("hvac_action", "").lower()
        running_actions = {"heating", "cooling"}

        if new_action and old_action:
            # Use hvac_action when available (more accurate)
            was_running = old_action in running_actions
            is_running = new_action in running_actions
        else:
            # Fall back to mode-based tracking
            idle_modes = {"off", "unavailable", "unknown", ""}
            was_running = old_state.state not in idle_modes
            is_running = new_state.state not in idle_modes

        if not was_running and is_running:
            # HVAC just turned on
            self._hvac_on_since = dt_util.now()
        elif was_running and not is_running:
            # HVAC just turned off — flush runtime
            self._flush_hvac_runtime()
            self._hvac_on_since = None
            await self._async_save_state()

        # Detect manual override: temperature changed but not by us
        new_temp = new_state.attributes.get("temperature")
        old_temp = old_state.attributes.get("temperature")

        if new_temp != old_temp and self._today_record:
            # This is a rough heuristic — in production you'd track which
            # changes were initiated by the integration vs. manual
            self._today_record.manual_overrides += 1
            _LOGGER.debug("Possible manual override detected: %s -> %s", old_temp, new_temp)
            await self._async_save_state()

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

    @property
    def current_classification(self) -> DayClassification | None:
        """Return the current day classification."""
        return self._current_classification

    @property
    def today_record(self) -> DailyRecord | None:
        """Return today's learning record."""
        return self._today_record

    def get_chart_data(self) -> dict[str, Any]:
        """Build chart data for the dashboard panel.

        Returns a dict with four series: predicted outdoor, predicted indoor,
        actual outdoor, and actual indoor temperatures over a 24-hour period.
        """
        now = dt_util.now()
        current_hour = now.hour + now.minute / 60.0

        predicted_outdoor, predicted_indoor = compute_predicted_temps(
            self._current_classification, self.config
        )

        return {
            "predicted_outdoor": predicted_outdoor,
            "predicted_indoor": predicted_indoor,
            "actual_outdoor": [
                {"time": ts, "temp": t} for ts, t in self._outdoor_temp_history
            ],
            "actual_indoor": [
                {"time": ts, "temp": t} for ts, t in self._indoor_temp_history
            ],
            "current_hour": round(current_hour, 1),
        }

    def get_debug_state(self) -> dict[str, Any]:
        """Return serializable debug state for the dashboard."""
        ae = self.automation_engine
        c = self._current_classification

        # Door/window sensor states
        sensor_states = {}
        for sensor_id in self._resolved_sensors:
            sensor_states[sensor_id] = {
                "open": self._is_sensor_open(sensor_id),
                "friendly_name": sensor_id.split(".")[-1].replace("_", " ").title(),
            }

        return {
            "paused_by_door": ae.is_paused_by_door,
            "pre_pause_mode": ae._pre_pause_mode,
            "grace_active": ae._grace_active,
            "last_resume_source": ae._last_resume_source,
            "door_window_sensors": sensor_states,
            "pending_debounce_timers": list(self._door_open_timers.keys()),
            "classification": {
                "day_type": c.day_type if c else None,
                "trend_direction": c.trend_direction if c else None,
                "trend_magnitude": c.trend_magnitude if c else None,
                "hvac_mode": c.hvac_mode if c else None,
                "windows_recommended": c.windows_recommended if c else None,
                "window_open_time": (
                    c.window_open_time.isoformat() if c and c.window_open_time else None
                ),
                "window_close_time": (
                    c.window_close_time.isoformat() if c and c.window_close_time else None
                ),
                "pre_condition": c.pre_condition if c else None,
                "pre_condition_target": c.pre_condition_target if c else None,
                "setback_modifier": c.setback_modifier if c else None,
            },
        }

    async def async_shutdown(self) -> None:
        """Clean up on shutdown."""
        # Flush HVAC runtime and save state before cleanup
        self._flush_hvac_runtime()
        await self._async_save_state()

        # Cancel any pending debounce timers
        for cancel in self._door_open_timers.values():
            cancel()
        self._door_open_timers.clear()

        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        self._unsubscribe_door_window_listeners()
        self.automation_engine.cleanup()


def compute_predicted_temps(
    classification: DayClassification | None,
    config: dict[str, Any],
) -> tuple[list[dict], list[dict]]:
    """Compute predicted outdoor and indoor hourly temperatures.

    This is a standalone function so it can be tested without a coordinator.

    Returns:
        (predicted_outdoor, predicted_indoor) — each a list of 24 dicts
        with 'hour' and 'temp' keys, or empty lists if no classification.
    """
    if not classification:
        return [], []

    c = classification

    # --- Predicted outdoor temps (sinusoidal interpolation) ---
    predicted_outdoor: list[dict] = []
    high = c.today_high
    low = c.today_low
    mid = (high + low) / 2.0
    amp = (high - low) / 2.0
    for h in range(24):
        # Peak at ~15:00 (3 PM), trough at ~5:00 (5 AM)
        temp = mid + amp * math.cos(2 * math.pi * (h - 15) / 24)
        predicted_outdoor.append({"hour": h, "temp": round(temp, 1)})

    # --- Predicted indoor temps (from schedule + setpoints) ---
    predicted_indoor: list[dict] = []
    wake = _parse_time(config.get("wake_time", "06:30"))
    sleep = _parse_time(config.get("sleep_time", "22:30"))
    wake_h = wake.hour + wake.minute / 60.0
    sleep_h = sleep.hour + sleep.minute / 60.0

    comfort = (
        config.get("comfort_heat", 70)
        if c.hvac_mode == "heat"
        else config.get("comfort_cool", 75)
    )
    setback = (
        config.get("setback_heat", 60)
        if c.hvac_mode == "heat"
        else config.get("setback_cool", 80)
    )
    setback += c.setback_modifier

    for h in range(24):
        if h < wake_h:
            temp = setback  # overnight setback
        elif h < wake_h + 0.5:
            # ramping from setback to comfort
            frac = (h - wake_h) / 0.5
            temp = setback + frac * (comfort - setback)
        elif h < sleep_h:
            if c.hvac_mode == "off" and predicted_outdoor:
                # drift toward outdoor when HVAC off
                outdoor_t = predicted_outdoor[h]["temp"]
                drift_rate = 3.0 if (
                    c.windows_recommended
                    and c.window_open_time
                    and c.window_close_time
                    and c.window_open_time.hour <= h < c.window_close_time.hour
                ) else 1.5
                # Simple drift model: move toward outdoor at drift_rate °/hr
                diff = outdoor_t - comfort
                temp = comfort + min(abs(diff), drift_rate) * (1 if diff > 0 else -1)
            else:
                temp = comfort
        elif h < sleep_h + 0.5:
            # ramping from comfort to bedtime setback
            bedtime_setback = (
                comfort - 4 + c.setback_modifier
                if c.hvac_mode == "heat"
                else comfort + 3
            )
            frac = (h - sleep_h) / 0.5
            temp = comfort + frac * (bedtime_setback - comfort)
        else:
            bedtime_setback = (
                comfort - 4 + c.setback_modifier
                if c.hvac_mode == "heat"
                else comfort + 3
            )
            temp = bedtime_setback
        predicted_indoor.append({"hour": h, "temp": round(temp, 1)})

    return predicted_outdoor, predicted_indoor


def _parse_time(time_str: str) -> time:
    """Parse a time string like '06:30' into a time object."""
    try:
        parts = time_str.split(":")
        if len(parts) < 2:
            raise ValueError(f"Expected HH:MM format, got {time_str!r}")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, AttributeError):
        _LOGGER.warning(
            "Could not parse time %r — defaulting to 06:00",
            time_str,
        )
        return time(6, 0)
