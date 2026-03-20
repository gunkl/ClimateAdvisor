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
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_VACATION_TOGGLE,
    CONF_VACATION_TOGGLE_INVERT,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    OCCUPANCY_HOME,
    OCCUPANCY_AWAY,
    OCCUPANCY_VACATION,
    OCCUPANCY_GUEST,
    ATTR_OCCUPANCY_MODE,
    DOMAIN,
    VERSION,
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
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
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
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
    ATTR_LAST_ACTION_TIME,
    ATTR_LAST_ACTION_REASON,
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
        self.automation_engine._revisit_callback = self.async_request_refresh

        # Startup safety — first update checks HVAC state before applying classification
        self._first_run: bool = True

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
        self._hourly_forecast_temps: list[dict] = []

        # Observe-only mode: when disabled, automation still runs but skips actions
        self._automation_enabled: bool = True

        # HVAC runtime tracking
        self._hvac_on_since: datetime | None = None

        # Occupancy state machine
        self._occupancy_mode: str = OCCUPANCY_HOME
        self._occupancy_away_since: datetime | None = None
        self._unsub_occupancy_listeners: list[Any] = []

    @property
    def automation_enabled(self) -> bool:
        """Whether automation actions are enabled (False = observe-only)."""
        return self._automation_enabled

    def set_automation_enabled(self, enabled: bool) -> None:
        """Enable or disable automation actions (observe-only mode)."""
        self._automation_enabled = enabled
        self.automation_engine.dry_run = not enabled
        _LOGGER.info(
            "Automation %s",
            "enabled" if enabled else "disabled (observe-only)",
        )
        self.hass.async_create_task(self._async_save_state())

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

        # Listeners: occupancy toggles
        self._subscribe_occupancy_listeners()
        self._occupancy_mode = self._compute_occupancy_mode()

        # Listeners: thermostat state (for tracking manual overrides and runtime)
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass,
                self.config["climate_entity"],
                self._async_thermostat_changed,
            )
        )

        _LOGGER.info("Climate Advisor v%s coordinator setup complete", VERSION)

    async def async_restore_state(self) -> None:
        """Restore operational state from disk after startup."""
        await self.hass.async_add_executor_job(self.learning.load_state)
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
                rec_data = state["today_record"]
                # Normalize suggestion_sent for backward compat
                sent = rec_data.get("suggestion_sent")
                if sent is None:
                    rec_data["suggestion_sent"] = []
                elif isinstance(sent, str):
                    rec_data["suggestion_sent"] = [sent]
                recovered = DailyRecord(**rec_data)
                self.learning.record_day(recovered)
                await self.hass.async_add_executor_job(
                    self.learning.save_state
                )
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
                # Normalize suggestion_sent for backward compat (was str|None, now list)
                sent = record_data.get("suggestion_sent")
                if sent is None:
                    record_data["suggestion_sent"] = []
                elif isinstance(sent, str):
                    record_data["suggestion_sent"] = [sent]
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

        # Observe-only mode
        self._automation_enabled = state.get("automation_enabled", True)
        self.automation_engine.dry_run = not self._automation_enabled

        # Occupancy state
        self._occupancy_mode = state.get("occupancy_mode", OCCUPANCY_HOME)
        away_since = state.get("occupancy_away_since")
        if away_since:
            try:
                self._occupancy_away_since = datetime.fromisoformat(away_since)
            except (ValueError, TypeError):
                self._occupancy_away_since = None

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
                "window_opportunity_morning": c.window_opportunity_morning,
                "window_opportunity_evening": c.window_opportunity_evening,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat()
                    if c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat()
                    if c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat()
                    if c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat()
                    if c.window_opportunity_evening_end else None
                ),
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
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
            "occupancy_away_since": (
                self._occupancy_away_since.isoformat()
                if self._occupancy_away_since
                else None
            ),
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

    # ── Occupancy toggle methods ─────────────────────────────────────

    def _is_toggle_on(self, entity_id: str, invert: bool) -> bool:
        """Check if a toggle entity is effectively ON, respecting invert."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown"):
            if state:
                _LOGGER.warning(
                    "Occupancy toggle %s is %s — treating as OFF",
                    entity_id,
                    state.state,
                )
            return False
        raw_on = state.state == "on"
        return not raw_on if invert else raw_on

    def _compute_occupancy_mode(self) -> str:
        """Compute effective occupancy mode from toggle entities (priority order)."""
        cfg = self.config

        # Guest (highest priority)
        guest_entity = cfg.get(CONF_GUEST_TOGGLE)
        if guest_entity and self._is_toggle_on(
            guest_entity, cfg.get(CONF_GUEST_TOGGLE_INVERT, False)
        ):
            return OCCUPANCY_GUEST

        # Vacation
        vacation_entity = cfg.get(CONF_VACATION_TOGGLE)
        if vacation_entity and self._is_toggle_on(
            vacation_entity, cfg.get(CONF_VACATION_TOGGLE_INVERT, False)
        ):
            return OCCUPANCY_VACATION

        # Home/Away
        home_entity = cfg.get(CONF_HOME_TOGGLE)
        if home_entity:
            if self._is_toggle_on(
                home_entity, cfg.get(CONF_HOME_TOGGLE_INVERT, False)
            ):
                return OCCUPANCY_HOME
            return OCCUPANCY_AWAY

        # No toggles configured
        return OCCUPANCY_HOME

    def _subscribe_occupancy_listeners(self) -> None:
        """Subscribe to state changes for all configured occupancy toggles."""
        for conf_key in (CONF_HOME_TOGGLE, CONF_VACATION_TOGGLE, CONF_GUEST_TOGGLE):
            entity_id = self.config.get(conf_key)
            if entity_id:
                self._unsub_occupancy_listeners.append(
                    async_track_state_change_event(
                        self.hass,
                        entity_id,
                        self._async_occupancy_toggle_changed,
                    )
                )

    def _unsubscribe_occupancy_listeners(self) -> None:
        """Unsubscribe all occupancy toggle listeners."""
        for unsub in self._unsub_occupancy_listeners:
            unsub()
        self._unsub_occupancy_listeners.clear()

    async def _async_occupancy_toggle_changed(self, event: Event) -> None:
        """Handle an occupancy toggle state change."""
        new_mode = self._compute_occupancy_mode()

        if new_mode == self._occupancy_mode:
            return  # No effective change

        old_mode = self._occupancy_mode
        _LOGGER.info(
            "Occupancy mode changed: %s -> %s (trigger: %s)",
            old_mode,
            new_mode,
            event.data.get("entity_id", "unknown"),
        )

        # Track away minutes
        now = dt_util.now()
        present_modes = {OCCUPANCY_HOME, OCCUPANCY_GUEST}
        was_present = old_mode in present_modes
        is_present = new_mode in present_modes

        if was_present and not is_present:
            # Leaving home
            self._occupancy_away_since = now
        elif not was_present and is_present:
            # Returning home
            if self._occupancy_away_since and self._today_record:
                elapsed = (now - self._occupancy_away_since).total_seconds() / 60.0
                self._today_record.occupancy_away_minutes += elapsed
                _LOGGER.debug(
                    "Away duration: %.1f minutes added to daily record",
                    elapsed,
                )
            self._occupancy_away_since = None

        self._occupancy_mode = new_mode

        # Call appropriate automation handler
        if new_mode == OCCUPANCY_VACATION:
            await self.automation_engine.handle_occupancy_vacation()
        elif new_mode == OCCUPANCY_AWAY:
            await self.automation_engine.handle_occupancy_away()
        elif new_mode in present_modes:
            await self.automation_engine.handle_occupancy_home()

        await self._async_save_state()

    # ── End occupancy methods ──────────────────────────────────────

    def _cancel_all_debounce_timers(self) -> None:
        """Cancel all pending door/window debounce timers.

        Called when a manual HVAC override is detected so that orphaned
        debounce timers for still-open sensors cannot interfere with the
        manual grace period.
        """
        if self._door_open_timers:
            _LOGGER.info(
                "Cancelling %d pending debounce timer(s) due to manual override",
                len(self._door_open_timers),
            )
            for cancel in self._door_open_timers.values():
                cancel()
            self._door_open_timers.clear()

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
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        if forecast:
            self._current_classification = classify_day(forecast)

            # Startup safety: if HVAC is already running on first update,
            # treat it as a manual override to avoid disrupting current state
            if self._first_run:
                self._first_run = False
                climate_state = self.hass.states.get(self.config["climate_entity"])
                if climate_state and climate_state.state not in (
                    "off", "unavailable", "unknown",
                ):
                    _LOGGER.info(
                        "First run: HVAC is %s — treating as manual override "
                        "to avoid disrupting current state",
                        climate_state.state,
                    )
                    self.automation_engine._manual_override_active = True
                    self.automation_engine._manual_override_mode = (
                        climate_state.state
                    )
                    self.automation_engine._manual_override_time = (
                        dt_util.now().isoformat()
                    )

            await self.automation_engine.apply_classification(self._current_classification)

            # Reset startup retry state on success
            if self._startup_retries_remaining < 5:
                _LOGGER.info(
                    "Weather entity now available; classified as %s day",
                    self._current_classification.day_type,
                )
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

            # Check economizer opportunity (window cooling on hot days)
            if self._today_record:
                windows_open = self._today_record.windows_physically_opened and (
                    self._today_record.window_physical_close_time is None
                )
                await self.automation_engine.check_window_cooling_opportunity(
                    forecast.current_outdoor_temp,
                    forecast.current_indoor_temp,
                    windows_open,
                    current_hour=dt_util.now().hour,
                )

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
                _LOGGER.warning(
                    "Weather entity not ready; retry %d remaining in %ds",
                    self._startup_retries_remaining + 1,
                    delay,
                )
                @callback
                def _schedule_retry(_now: Any) -> None:
                    self.hass.async_create_task(self.async_request_refresh())

                async_call_later(self.hass, delay, _schedule_retry)
            else:
                _LOGGER.warning(
                    "Weather entity still unavailable after startup retries; "
                    "will try again at next scheduled update"
                )

        # Build the data dict that sensors will read
        c = self._current_classification
        suggestions = self.learning.generate_suggestions()
        compliance = self.learning.get_compliance_summary()

        next_auto = self._compute_next_automation_action(c)
        return {
            ATTR_DAY_TYPE: c.day_type if c else "unknown",
            ATTR_TREND: c.trend_direction if c else "unknown",
            ATTR_TREND_MAGNITUDE: c.trend_magnitude if c else 0,
            ATTR_BRIEFING: self._last_briefing,
            ATTR_NEXT_ACTION: self._compute_next_action(c),
            ATTR_AUTOMATION_STATUS: self._compute_automation_status(),
            ATTR_LEARNING_SUGGESTIONS: suggestions,
            ATTR_COMPLIANCE_SCORE: compliance.get("comfort_score", 1.0),
            ATTR_NEXT_AUTOMATION_ACTION: next_auto[0],
            ATTR_NEXT_AUTOMATION_TIME: next_auto[1],
            ATTR_OCCUPANCY_MODE: self._occupancy_mode,
            ATTR_LAST_ACTION_TIME: self.automation_engine._last_action_time,
            ATTR_LAST_ACTION_REASON: self.automation_engine._last_action_reason,
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

    async def _get_hourly_forecast_data(self) -> list:
        """Get hourly forecast data from the weather entity.

        Returns a list of hourly forecast dicts, or [] if the weather
        integration does not support hourly forecasts or the call fails.
        """
        weather_entity = self.config["weather_entity"]
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            return (
                response.get(weather_entity, {}).get("forecast", [])
                if response
                else []
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Hourly forecast not available for %s; using cosine model",
                weather_entity,
            )
            return []

    async def _get_forecast(self) -> ForecastSnapshot | None:
        """Pull forecast data from the weather entity."""
        weather_entity = self.config["weather_entity"]
        weather_state = self.hass.states.get(weather_entity)
        if not weather_state:
            _LOGGER.debug(
                "Weather entity %s not found — repair issue should be active",
                weather_entity,
            )
            return None

        # Entity exists but isn't reporting data yet (common after restart)
        if weather_state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Weather entity %s is %s — treating as not ready",
                weather_entity,
                weather_state.state,
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

    async def _async_send_briefing(self, now: datetime) -> None:
        """Generate and send the daily briefing."""
        if self._briefing_sent_today:
            return

        forecast = await self._get_forecast()
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
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

        # Generate briefing text and track which suggestions were sent
        suggestions = self.learning.generate_suggestions()
        if self._today_record:
            self._today_record.suggestion_sent = self.learning.get_last_suggestion_keys()
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        briefing_kwargs = dict(
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
        briefing = generate_briefing(**briefing_kwargs)
        briefing_short = generate_briefing(**briefing_kwargs, verbosity="tldr_only")

        self._last_briefing = briefing

        # In observe-only mode, skip sending the notification
        if not self._automation_enabled:
            _LOGGER.info(
                "[DRY RUN] Briefing generated but notification skipped "
                "(automation disabled)"
            )
            self._briefing_sent_today = True
            await self._async_save_state()
            return

        # Send push notification — short TLDR summary
        _notify_svc = self.config["notify_service"]
        _notify_name = _notify_svc.split(".")[-1] if "." in _notify_svc else _notify_svc
        await self.hass.services.async_call(
            "notify",
            _notify_name,
            {"message": briefing_short, "title": "🏠 Your Home Climate Plan for Today"},
        )
        # Send email — full briefing
        if self.config.get(CONF_EMAIL_NOTIFY, True):
            await self.hass.services.async_call(
                "notify",
                "send_email",
                {"message": briefing, "title": "🏠 Your Home Climate Plan for Today"},
            )

        self._briefing_sent_today = True
        _LOGGER.info("Daily briefing sent — day type: %s", classification.day_type)
        await self._async_save_state()

    async def _async_morning_wakeup(self, now: datetime) -> None:
        """Handle morning wake-up."""
        await self.automation_engine.handle_morning_wakeup()

    async def _async_bedtime(self, now: datetime) -> None:
        """Handle bedtime setback."""
        await self.automation_engine.handle_bedtime()

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
            await self.hass.async_add_executor_job(
                self.learning.save_state
            )
            _LOGGER.info("Day record saved for learning")

        self._today_record = None
        self._briefing_sent_today = False
        self._hvac_on_since = None
        self._outdoor_temp_history.clear()
        self._indoor_temp_history.clear()
        self._hourly_forecast_temps.clear()
        await self._async_save_state()

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
            def _debounce_expired(_now: Any, eid: str = entity_id) -> None:
                """Debounce period elapsed — schedule async check."""
                async def _do_debounce() -> None:
                    self._door_open_timers.pop(eid, None)
                    if self._is_sensor_open(eid):
                        _LOGGER.debug(
                            "Debounce expired, sensor still open: %s", eid
                        )
                        await self.automation_engine.handle_door_window_open(eid)
                        if self._today_record:
                            self._today_record.door_window_pause_events += 1
                            sensor_key = eid.split(".")[-1]
                            self._today_record.door_pause_by_sensor[sensor_key] = (
                                self._today_record.door_pause_by_sensor.get(sensor_key, 0) + 1
                            )

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

                            # Always track physical window opens (independent of recommendations)
                            if not self._today_record.windows_physically_opened:
                                self._today_record.windows_physically_opened = True
                                self._today_record.window_physical_open_time = dt_util.now().isoformat()

                            await self._async_save_state()

                self.hass.async_create_task(_do_debounce())

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
                # Track physical close time (independent of recommendations)
                if (
                    self._today_record
                    and self._today_record.windows_physically_opened
                    and self._today_record.window_physical_close_time is None
                ):
                    self._today_record.window_physical_close_time = dt_util.now().isoformat()
                await self.automation_engine.handle_all_doors_windows_closed()
                await self._async_save_state()

    async def _async_thermostat_changed(self, event: Event) -> None:
        """Track thermostat changes for learning (detect manual overrides)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        # Detect manual HVAC override during a door/window pause.
        # Note: we intentionally do NOT require old_state == "off" here.
        # The async _set_hvac_mode("off") service call may not have
        # propagated to HA's state machine yet when the user quickly
        # turns HVAC back on, so old_state could still be the pre-pause
        # mode (e.g. "cool"). The paused_by_door flag is authoritative.
        if (
            self.automation_engine.is_paused_by_door
            and new_state.state not in ("off", "unavailable", "unknown")
        ):
            _LOGGER.info(
                "Manual HVAC override detected during door/window pause: %s -> %s",
                old_state.state,
                new_state.state,
            )
            await self.automation_engine.handle_manual_override_during_pause()
            self._cancel_all_debounce_timers()
        elif (
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and not self.automation_engine._manual_override_active
            and self._current_classification
            and new_state.state != self._current_classification.hvac_mode
        ):
            # Mode changed outside of door/window pause to something
            # different from what classification dictates — manual override
            _LOGGER.info(
                "Manual HVAC override detected: %s -> %s (classification wants %s)",
                old_state.state,
                new_state.state,
                self._current_classification.hvac_mode,
            )
            self.automation_engine.handle_manual_override()

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
            try:
                old_val = float(old_temp)
                new_val = float(new_temp)
                magnitude = round(new_val - old_val, 1)
                self._today_record.override_details.append({
                    "time": dt_util.now().strftime("%H:%M"),
                    "old_temp": old_val,
                    "new_temp": new_val,
                    "direction": "up" if magnitude > 0 else "down",
                    "magnitude": abs(magnitude),
                })
            except (ValueError, TypeError):
                pass  # Non-numeric temps, skip detail recording
            _LOGGER.debug("Possible manual override detected: %s -> %s", old_temp, new_temp)
            await self._async_save_state()

    def _compute_next_action(self, c: DayClassification | None) -> str:
        """Compute the next recommended human action for display."""
        if not c:
            return "Waiting for forecast data..."

        if self._occupancy_mode == OCCUPANCY_VACATION:
            return "On vacation — deep energy-saving setback active."
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return "You're away — automation managing temperature."

        now = dt_util.now().time()

        if c.windows_recommended:
            if c.window_open_time and now < c.window_open_time:
                return f"Open windows at {c.window_open_time.strftime('%I:%M %p')}"
            elif c.window_close_time and now < c.window_close_time:
                return f"Close windows by {c.window_close_time.strftime('%I:%M %p')}"

        if c.day_type == DAY_TYPE_HOT:
            comfort_cool = self.config.get("comfort_cool", 75)
            threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
            if c.window_opportunity_morning and now < time(ECONOMIZER_MORNING_END_HOUR, 0):
                end_t = time(ECONOMIZER_MORNING_END_HOUR, 0).strftime("%I:%M %p").lstrip("0")
                return f"Open windows if outdoor temp is below {threshold:.0f}°F (until {end_t})"
            elif c.window_opportunity_evening and now >= time(ECONOMIZER_EVENING_START_HOUR, 0):
                return f"Open windows if outdoor temp is below {threshold:.0f}°F"
            return "Keep windows and blinds closed. AC is handling it."
        elif c.day_type == DAY_TYPE_COLD:
            return "Keep doors closed — help the heater out."

        return "No action needed right now. Automation is handling it."

    def _compute_automation_status(self) -> str:
        """Compute the current automation status string."""
        if not self._automation_enabled:
            return "disabled"
        if self.automation_engine.is_paused_by_door:
            return "paused — door/window open"
        if self.automation_engine._grace_active:
            source = self.automation_engine._last_resume_source or "automation"
            return f"grace period ({source})"
        if self._occupancy_mode == OCCUPANCY_VACATION:
            return "active (vacation)"
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return "active (away)"
        if self._occupancy_mode == OCCUPANCY_GUEST:
            return "active (guest)"
        return "active"

    def _compute_next_automation_action(
        self, c: DayClassification | None
    ) -> tuple[str, str]:
        """Compute the next scheduled automation action and its time.

        Returns:
            Tuple of (action_description, execution_time_str).
        """
        if not c:
            return ("Waiting for classification...", "")

        now = dt_util.now()
        now_time = now.time()

        # Check if automation is paused
        if self.automation_engine.is_paused_by_door:
            return ("Waiting — HVAC paused (door/window open)", "")

        if self.automation_engine._grace_active:
            source = self.automation_engine._last_resume_source or "automation"
            return (f"Grace period active ({source})", "")

        # Build list of upcoming scheduled events
        wake_time = self.config.get("wake_time", "06:30:00")
        sleep_time = self.config.get("sleep_time", "22:30:00")
        briefing_time = self.config.get("briefing_time", "06:00:00")

        # Parse time strings to time objects
        def _parse_time(t: str) -> time:
            parts = t.split(":")
            return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

        events: list[tuple[time, str]] = []

        bt = _parse_time(briefing_time)
        wt = _parse_time(wake_time)
        st = _parse_time(sleep_time)

        if now_time < bt:
            events.append((bt, "Send daily briefing"))
        if now_time < wt:
            if c.hvac_mode in ("heat", "cool"):
                events.append((wt, f"Morning wake-up — restore {c.hvac_mode} comfort"))
            else:
                events.append((wt, "Morning wake-up check"))
        if now_time < st:
            if c.hvac_mode == "heat":
                bedtime_target = self.config.get("comfort_heat", 70) - 4 + c.setback_modifier
                events.append((st, f"Bedtime — heat setback to {bedtime_target:.0f}°F"))
            elif c.hvac_mode == "cool":
                bedtime_target = self.config.get("comfort_cool", 75) + 3
                events.append((st, f"Bedtime — cool setback to {bedtime_target:.0f}°F"))
            else:
                events.append((st, "Bedtime check"))

        if not events:
            return ("No more actions today", "")

        # Sort by time, return earliest
        events.sort(key=lambda e: e[0])
        next_time, next_desc = events[0]
        time_str = next_time.strftime("%I:%M %p").lstrip("0")
        return (next_desc, time_str)

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
            self._current_classification, self.config, self._hourly_forecast_temps
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
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
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
                "window_opportunity_morning": c.window_opportunity_morning if c else None,
                "window_opportunity_evening": c.window_opportunity_evening if c else None,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat()
                    if c and c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat()
                    if c and c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat()
                    if c and c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat()
                    if c and c.window_opportunity_evening_end else None
                ),
                "pre_condition": c.pre_condition if c else None,
                "pre_condition_target": c.pre_condition_target if c else None,
                "setback_modifier": c.setback_modifier if c else None,
            },
            "last_action_time": ae._last_action_time,
            "last_action_reason": ae._last_action_reason,
            "manual_override_active": ae._manual_override_active,
            "manual_override_mode": ae._manual_override_mode,
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
    hourly_forecast: list[dict] | None = None,
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

    # --- Predicted outdoor temps ---
    predicted_outdoor = _build_outdoor_curve(
        high=c.today_high, low=c.today_low, hourly_forecast=hourly_forecast
    )

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


def _cosine_outdoor_curve(high: float, low: float) -> list[dict]:
    """Sinusoidal outdoor temperature model (peak 3 PM, trough 3 AM).

    This is the original prediction model, now used as a fallback when
    hourly forecast data is not available from the weather integration.
    """
    mid = (high + low) / 2.0
    amp = (high - low) / 2.0
    return [
        {
            "hour": h,
            "temp": round(
                mid + amp * math.cos(2 * math.pi * (h - 15) / 24), 1
            ),
        }
        for h in range(24)
    ]


def _build_outdoor_curve(
    high: float,
    low: float,
    hourly_forecast: list[dict] | None,
) -> list[dict]:
    """Build 24 hourly outdoor temperature predictions.

    Uses actual hourly forecast data when available.  Falls back to a
    sinusoidal interpolation from today_high / today_low when not.
    """
    if not hourly_forecast:
        return _cosine_outdoor_curve(high, low)

    # Parse hourly entries into an integer-hour lookup (today only)
    today = datetime.now().date()
    known: dict[int, float] = {}
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            if dt_obj.date() != today:
                continue
            known[dt_obj.hour] = float(temp)
        except (ValueError, TypeError):
            continue

    if not known:
        return _cosine_outdoor_curve(high, low)

    # Fill all 24 hours: known values, linear interpolation for gaps,
    # cosine fallback at the edges.
    cosine = {p["hour"]: p["temp"] for p in _cosine_outdoor_curve(high, low)}
    known_hours = sorted(known)
    result: list[dict] = []

    for h in range(24):
        if h in known:
            result.append({"hour": h, "temp": round(known[h], 1)})
        else:
            before = [k for k in known_hours if k < h]
            after = [k for k in known_hours if k > h]
            if before and after:
                h0, h1 = before[-1], after[0]
                frac = (h - h0) / (h1 - h0)
                temp = known[h0] + frac * (known[h1] - known[h0])
                result.append({"hour": h, "temp": round(temp, 1)})
            else:
                # Edge: before all known or after all known → cosine fill
                result.append({"hour": h, "temp": cosine[h]})

    return result


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
