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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ai_skills import AISkillRegistry
    from .claude_api import ClaudeAPIClient

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .automation import AutomationEngine, compute_bedtime_setback
from .briefing import generate_briefing
from .classifier import DayClassification, ForecastSnapshot, classify_day
from .const import (
    AI_REPORT_HISTORY_CAP,
    AI_REPORTS_FILE,
    ATTR_AI_STATUS,
    ATTR_AUTOMATION_STATUS,
    ATTR_BRIEFING,
    ATTR_BRIEFING_SHORT,
    ATTR_COMPLIANCE_SCORE,
    ATTR_CONTACT_STATUS,
    ATTR_DAY_TYPE,
    ATTR_FAN_OVERRIDE_SINCE,
    ATTR_FAN_RUNNING,
    ATTR_FAN_RUNTIME,
    ATTR_FAN_STATUS,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_RUNTIME_TODAY,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    CONF_AI_API_KEY,
    CONF_AI_ENABLED,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_VACATION_TOGGLE,
    CONF_VACATION_TOGGLE_INVERT,
    CONF_WEATHER_BIAS,
    DAY_TYPE_COLD,
    DAY_TYPE_HOT,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    DOMAIN,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_DISABLED,
    MAX_THERMAL_RATE_F_PER_HOUR,
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_THERMAL_RATE_F_PER_HOUR,
    MIN_THERMAL_SESSION_MINUTES,
    MIN_WEATHER_BIAS_APPLY_F,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_SETBACK_MINUTES,
    OCCUPANCY_VACATION,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    VERSION,
)
from .learning import DailyRecord, LearningEngine
from .state import StatePersistence
from .temperature import convert_delta, format_temp, to_fahrenheit

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
        self.automation_engine._sensor_check_callback = self._any_sensor_open

        # AI subsystem (only if enabled and API key present)
        self.claude_client: ClaudeAPIClient | None = None
        self.ai_skills: AISkillRegistry | None = None
        self._ai_report_history: list[dict] = []
        if config.get(CONF_AI_ENABLED) and config.get(CONF_AI_API_KEY):
            from .ai_skills import AISkillRegistry as _AISkillRegistry
            from .ai_skills_activity import register_activity_skill
            from .claude_api import ClaudeAPIClient as _ClaudeAPIClient

            self.claude_client = _ClaudeAPIClient(config)
            self.ai_skills = _AISkillRegistry()
            register_activity_skill(self.ai_skills)
            _LOGGER.info("AI subsystem initialized — model: %s", config.get("ai_model", "unknown"))
        else:
            _LOGGER.debug(
                "AI subsystem disabled — enabled: %s, key present: %s",
                config.get(CONF_AI_ENABLED, False),
                bool(config.get(CONF_AI_API_KEY)),
            )

        # Startup safety — first update checks HVAC state before applying classification
        self._first_run: bool = True

        # State
        self._current_classification: DayClassification | None = None
        self._today_record: DailyRecord | None = None
        self._briefing_sent_today = False
        self._last_briefing: str = ""
        self._last_briefing_short: str = ""
        self._briefing_day_type: str | None = None
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
        self._hvac_session_start_indoor_temp: float | None = None
        self._hvac_session_start_outdoor_temp: float | None = None
        self._hvac_session_mode: str | None = None  # "heat" or "cool"
        self._last_violation_check: datetime | None = None

        # Occupancy state machine
        self._occupancy_mode: str = OCCUPANCY_HOME
        self._occupancy_away_since: datetime | None = None
        self._unsub_occupancy_listeners: list[Any] = []
        self._occupancy_away_timer_cancel: Any | None = None

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

        # Listeners: fan entity (for detecting manual fan overrides)
        fan_entity = self.config.get(CONF_FAN_ENTITY)
        if fan_entity:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    fan_entity,
                    self._async_fan_entity_changed,
                )
            )

        _LOGGER.info("Climate Advisor v%s coordinator setup complete", VERSION)

    async def async_restore_state(self) -> None:
        """Restore operational state from disk after startup."""
        await self.hass.async_add_executor_job(self.learning.load_state)
        state = await self.hass.async_add_executor_job(self._state_persistence.load)
        if not state:
            _LOGGER.debug("No persisted state found — starting fresh")
            return

        today_str = dt_util.now().strftime("%Y-%m-%d")
        state_date = state.get("date", "")
        yesterday_str = (dt_util.now() - timedelta(days=1)).strftime("%Y-%m-%d")

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
                await self.hass.async_add_executor_job(self.learning.save_state)
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
                    window_open_time=(time.fromisoformat(wot) if wot else None),
                    window_close_time=(time.fromisoformat(wct) if wct else None),
                    setback_modifier=cls_data.get("setback_modifier", 0.0),
                )
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Failed to restore classification: %s", err)

        # Temperature history
        temp_hist = state.get("temp_history", {})
        self._outdoor_temp_history = [(ts, t) for ts, t in temp_hist.get("outdoor", [])]
        self._indoor_temp_history = [(ts, t) for ts, t in temp_hist.get("indoor", [])]

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
        self._last_briefing_short = briefing.get("last_text_short", "")
        self._briefing_day_type = briefing.get("briefing_day_type")

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

        # Load AI report history if AI subsystem is active
        if self.claude_client:
            await self.hass.async_add_executor_job(self._load_ai_reports)

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
                "window_open_time": (c.window_open_time.isoformat() if c.window_open_time else None),
                "window_close_time": (c.window_close_time.isoformat() if c.window_close_time else None),
                "setback_modifier": c.setback_modifier,
                "window_opportunity_morning": c.window_opportunity_morning,
                "window_opportunity_evening": c.window_opportunity_evening,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat() if c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat() if c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat() if c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat() if c.window_opportunity_evening_end else None
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
                "last_text_short": self._last_briefing_short,
                "briefing_day_type": self._briefing_day_type,
            },
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
            "occupancy_away_since": (self._occupancy_away_since.isoformat() if self._occupancy_away_since else None),
        }

    async def _async_save_state(self) -> None:
        """Persist current operational state to disk."""
        state_dict = self._build_state_dict()
        await self.hass.async_add_executor_job(self._state_persistence.save, state_dict)

    async def async_store_ai_report(self, result: dict) -> None:
        """Store an AI activity report result and persist to disk."""
        import json  # noqa: F401 — imported for _save_ai_reports called via executor
        from datetime import datetime as _datetime

        report_entry = {
            "timestamp": _datetime.now().isoformat(),
            "result": result,
        }
        self._ai_report_history.append(report_entry)
        # Cap the list
        if len(self._ai_report_history) > AI_REPORT_HISTORY_CAP:
            self._ai_report_history = self._ai_report_history[-AI_REPORT_HISTORY_CAP:]
        # Persist to disk via executor (blocking I/O)
        await self.hass.async_add_executor_job(self._save_ai_reports)

    def _save_ai_reports(self) -> None:
        """Save AI report history to disk (atomic write)."""
        import json
        import os

        filepath = self.hass.config.path(AI_REPORTS_FILE)
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._ai_report_history, f, indent=2, default=str)
            os.replace(tmp_path, filepath)
        except Exception:
            _LOGGER.exception("Failed to save AI reports to %s", filepath)
            import contextlib

            with contextlib.suppress(OSError):
                os.remove(tmp_path)

    def _load_ai_reports(self) -> None:
        """Load AI report history from disk."""
        import json

        filepath = self.hass.config.path(AI_REPORTS_FILE)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._ai_report_history = data[-AI_REPORT_HISTORY_CAP:]
                _LOGGER.debug("Loaded %d AI reports from disk", len(self._ai_report_history))
            else:
                _LOGGER.warning("AI reports file has unexpected format, starting fresh")
                self._ai_report_history = []
        except FileNotFoundError:
            self._ai_report_history = []
        except Exception:
            _LOGGER.exception("Failed to load AI reports from %s", filepath)
            self._ai_report_history = []

    def get_ai_report_history(self) -> list[dict]:
        """Return the AI report history for dashboard display."""
        return list(self._ai_report_history)

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
        if guest_entity and self._is_toggle_on(guest_entity, cfg.get(CONF_GUEST_TOGGLE_INVERT, False)):
            return OCCUPANCY_GUEST

        # Vacation
        vacation_entity = cfg.get(CONF_VACATION_TOGGLE)
        if vacation_entity and self._is_toggle_on(vacation_entity, cfg.get(CONF_VACATION_TOGGLE_INVERT, False)):
            return OCCUPANCY_VACATION

        # Home/Away
        home_entity = cfg.get(CONF_HOME_TOGGLE)
        if home_entity:
            if self._is_toggle_on(home_entity, cfg.get(CONF_HOME_TOGGLE_INVERT, False)):
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

    def _cancel_occupancy_away_timer(self) -> None:
        """Cancel any pending occupancy away setback timer."""
        if self._occupancy_away_timer_cancel:
            self._occupancy_away_timer_cancel()
            self._occupancy_away_timer_cancel = None
            _LOGGER.debug("Occupancy away timer cancelled")

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
            # Cancel pending away setback timer
            self._cancel_occupancy_away_timer()
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
            self._cancel_occupancy_away_timer()
            await self.automation_engine.handle_occupancy_vacation()
        elif new_mode == OCCUPANCY_AWAY:
            delay_seconds = OCCUPANCY_SETBACK_MINUTES * 60
            _LOGGER.info(
                "Starting %d-minute occupancy away timer before applying setback",
                OCCUPANCY_SETBACK_MINUTES,
            )
            self._cancel_occupancy_away_timer()

            @callback
            def _occupancy_away_timer_expired(_now: Any) -> None:
                self._occupancy_away_timer_cancel = None
                _LOGGER.info("Occupancy away timer expired — applying setback")
                self.hass.async_create_task(self.automation_engine.handle_occupancy_away())

            self._occupancy_away_timer_cancel = async_call_later(
                self.hass,
                delay_seconds,
                _occupancy_away_timer_expired,
            )
        elif new_mode in present_modes:
            self._cancel_occupancy_away_timer()
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

    def _is_recent_hvac_command(self, threshold_seconds: float = 3.0) -> bool:
        """Check if an HVAC command was issued very recently (race guard)."""
        cmd_time = self.automation_engine._hvac_command_time
        if cmd_time is None:
            return False
        return (dt_util.now() - cmd_time).total_seconds() < threshold_seconds

    def _any_sensor_open(self) -> bool:
        """Return True if any monitored contact sensor is currently open."""
        return any(self._is_sensor_open(s) for s in self._resolved_sensors)

    def _check_startup_override(
        self,
        climate_state: Any,
        classification: Any,
    ) -> bool:
        """Check if manual override should be set on first run (Issue #42).

        Only sets override when the current HVAC mode differs from what
        the classification recommends.  If they match, no override is
        needed — Climate Advisor already agrees with the current state.

        Returns True if override was set, False otherwise.
        """
        if not climate_state or climate_state.state in (
            "off",
            "unavailable",
            "unknown",
        ):
            return False

        current = climate_state.state
        recommended = classification.hvac_mode
        if current != recommended:
            _LOGGER.info(
                "First run: HVAC is '%s' but classification recommends '%s' — treating as manual override",
                current,
                recommended,
            )
            self.automation_engine._manual_override_active = True
            self.automation_engine._manual_override_mode = current
            self.automation_engine._manual_override_time = dt_util.now().isoformat()
            return True

        _LOGGER.info(
            "First run: HVAC is '%s' which matches classification — no override needed",
            current,
        )
        return False

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min)."""
        # Re-resolve group membership in case it changed
        new_resolved = self._resolve_monitored_sensors()
        if set(new_resolved) != set(self._resolved_sensors):
            _LOGGER.info("Door/window sensor membership changed; updating listeners")
            self._unsubscribe_door_window_listeners()
            self._resolved_sensors = new_resolved
            self._subscribe_door_window_listeners()

        forecast = await self._get_forecast()
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        if forecast:
            prev_type = self._current_classification.day_type if self._current_classification else None
            self._current_classification = classify_day(forecast, previous_day_type=prev_type)

            # Startup safety: only set manual override if the current HVAC
            # mode differs from the classification (Issue #42)
            if self._first_run:
                self._first_run = False
                climate_state = self.hass.states.get(self.config["climate_entity"])
                self._check_startup_override(climate_state, self._current_classification)

            await self.automation_engine.apply_classification(self._current_classification)

            # If the day type changed since the briefing was generated,
            # regenerate the briefing text without re-sending notifications (Issue #78).
            if (
                self._briefing_sent_today
                and self._briefing_day_type is not None
                and self._current_classification.day_type != self._briefing_day_type
            ):
                _LOGGER.info(
                    "Classification changed %s → %s; regenerating briefing text",
                    self._briefing_day_type,
                    self._current_classification.day_type,
                )
                self._last_briefing, self._last_briefing_short = self._build_briefing_text(self._current_classification)
                self._briefing_day_type = self._current_classification.day_type
                await self._async_save_state()

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
            self._outdoor_temp_history.append((now_str, forecast.current_outdoor_temp))
            # Keep automation engine's outdoor temp current for natural vent decisions
            self.automation_engine.update_outdoor_temp(forecast.current_outdoor_temp)
            if forecast.current_indoor_temp is not None:
                self._indoor_temp_history.append((now_str, forecast.current_indoor_temp))

                # Track comfort violations (elapsed minutes since last check, capped at 30)
                if self._today_record:
                    comfort_low = self.config.get("comfort_heat", 70)
                    comfort_high = self.config.get("comfort_cool", 75)
                    now = dt_util.now()
                    if self._last_violation_check is not None:
                        elapsed_minutes = min((now - self._last_violation_check).total_seconds() / 60, 30.0)
                    else:
                        elapsed_minutes = 30.0
                    self._last_violation_check = now
                    if forecast.current_indoor_temp < comfort_low or forecast.current_indoor_temp > comfort_high:
                        self._today_record.comfort_violations_minutes += elapsed_minutes

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

            # Re-evaluate natural vent conditions while any sensor is open
            if self._any_sensor_open():
                await self.automation_engine.check_natural_vent_conditions()

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
                    "Weather entity still unavailable after startup retries; will try again at next scheduled update"
                )

        # Build the data dict that sensors will read
        c = self._current_classification
        suggestions = self.learning.generate_suggestions()
        compliance = self.learning.get_compliance_summary()

        # HVAC action (compressor/fan actual operation state) and today's runtime
        _climate_entity_id = self.config.get("climate_entity", "")
        _cs = self.hass.states.get(_climate_entity_id) if _climate_entity_id else None
        hvac_action = _cs.attributes.get("hvac_action", "") if _cs else ""
        _base_runtime = self._today_record.hvac_runtime_minutes if self._today_record else 0.0
        _session_elapsed = (dt_util.now() - self._hvac_on_since).total_seconds() / 60 if self._hvac_on_since else 0.0
        hvac_runtime_today = round(_base_runtime + _session_elapsed, 1)

        next_auto = self._compute_next_automation_action(c)
        return {
            ATTR_DAY_TYPE: c.day_type if c else "unknown",
            ATTR_TREND: c.trend_direction if c else "unknown",
            ATTR_TREND_MAGNITUDE: c.trend_magnitude if c else 0,
            ATTR_BRIEFING: self._last_briefing,
            ATTR_BRIEFING_SHORT: self._last_briefing_short,
            ATTR_NEXT_ACTION: self._compute_next_action(c, self._get_indoor_temp()),
            ATTR_AUTOMATION_STATUS: self._compute_automation_status(),
            ATTR_LEARNING_SUGGESTIONS: suggestions,
            ATTR_COMPLIANCE_SCORE: compliance.get("comfort_score", 1.0),
            ATTR_NEXT_AUTOMATION_ACTION: next_auto[0],
            ATTR_NEXT_AUTOMATION_TIME: next_auto[1],
            ATTR_OCCUPANCY_MODE: self._occupancy_mode,
            ATTR_LAST_ACTION_TIME: self.automation_engine._last_action_time,
            ATTR_LAST_ACTION_REASON: self.automation_engine._last_action_reason,
            ATTR_FAN_STATUS: self._compute_fan_status(),
            ATTR_FAN_RUNTIME: self.automation_engine._get_fan_runtime_minutes(),
            ATTR_FAN_OVERRIDE_SINCE: self.automation_engine._fan_override_time,
            ATTR_FAN_RUNNING: self.automation_engine._fan_active,
            ATTR_HVAC_ACTION: hvac_action,
            ATTR_HVAC_RUNTIME_TODAY: hvac_runtime_today,
            ATTR_CONTACT_STATUS: self._compute_contact_status(),
            ATTR_AI_STATUS: self.claude_client.get_status()["status"] if self.claude_client else "disabled",
        }

    def _get_outdoor_temp(self, weather_attrs: dict) -> float:
        """Read outdoor temperature based on configured source type."""
        source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)
        unit = self.config.get("temp_unit", "fahrenheit")

        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            entity_id = self.config.get("outdoor_temp_entity")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state:
                    try:
                        return to_fahrenheit(float(state.state), unit)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Outdoor temp entity %s has non-numeric state %r; falling back to weather attribute",
                            entity_id,
                            state.state,
                        )

        # weather_service source or fallback
        return to_fahrenheit(float(weather_attrs.get("temperature", 65)), unit)

    def _get_indoor_temp(self) -> float | None:
        """Read indoor temperature based on configured source type."""
        source = self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)
        unit = self.config.get("temp_unit", "fahrenheit")

        if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
            entity_id = self.config.get("indoor_temp_entity")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state:
                    try:
                        return to_fahrenheit(float(state.state), unit)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Indoor temp entity %s has non-numeric state %r; treating as unavailable",
                            entity_id,
                            state.state,
                        )
            return None

        # climate_fallback source
        climate_state = self.hass.states.get(self.config["climate_entity"])
        if climate_state:
            temp = climate_state.attributes.get("current_temperature")
            return to_fahrenheit(float(temp), unit) if temp is not None else None
        return None

    async def _get_forecast_data(self) -> list:
        """Get forecast data using the weather.get_forecasts service.

        Falls back to the deprecated forecast attribute if the service
        call is unavailable.
        """
        weather_entity = self.config["weather_entity"]
        if not self.hass.states.get(weather_entity):
            return []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "daily"},
                blocking=True,
                return_response=True,
            )
            forecasts = response.get(weather_entity, {}).get("forecast", []) if response else []
            if forecasts:
                return forecasts
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "weather.get_forecasts service call failed for %s; falling back to forecast attribute",
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
        if not self.hass.states.get(weather_entity):
            return []
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            return response.get(weather_entity, {}).get("forecast", []) if response else []
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

        # Extract today and tomorrow from forecast by matching dates.
        # HA daily forecasts shift forward as the day progresses, so
        # forecast[0] may be tonight or tomorrow — never assume index == day.
        today_high = current_outdoor
        today_low = current_outdoor
        tomorrow_high = current_outdoor
        tomorrow_low = current_outdoor

        today_fc = None
        tomorrow_fc = None
        if forecast:
            now_date = dt_util.now().date()
            tomorrow_date = now_date + timedelta(days=1)
            for entry in forecast:
                fc_dt = entry.get("datetime", "")
                try:
                    fc_date = datetime.fromisoformat(fc_dt).date()
                except (ValueError, TypeError):
                    continue
                if fc_date == now_date and today_fc is None:
                    today_fc = entry
                elif fc_date == tomorrow_date and tomorrow_fc is None:
                    tomorrow_fc = entry

            # If today's entry is missing (late evening), use first entry
            # as a fallback for "today" so we still have some data.
            if today_fc is None and tomorrow_fc is None and len(forecast) >= 2:
                today_fc = forecast[0]
                tomorrow_fc = forecast[1]
            elif today_fc is None and tomorrow_fc is not None and len(forecast) >= 1:
                today_fc = forecast[0]

        if today_fc:
            today_high = today_fc.get("temperature", today_fc.get("tempHigh", current_outdoor))
            today_low = today_fc.get("templow", today_fc.get("tempLow", current_outdoor - 15))
        if tomorrow_fc:
            tomorrow_high = tomorrow_fc.get("temperature", tomorrow_fc.get("tempHigh", current_outdoor))
            tomorrow_low = tomorrow_fc.get("templow", tomorrow_fc.get("tempLow", current_outdoor - 15))

        unit = self.config.get("temp_unit", "fahrenheit")
        today_high = to_fahrenheit(today_high, unit)
        today_low = to_fahrenheit(today_low, unit)
        tomorrow_high = to_fahrenheit(tomorrow_high, unit)
        tomorrow_low = to_fahrenheit(tomorrow_low, unit)

        # The forecast API returns "remaining period" data — as the day
        # progresses, today's high drops to the current temp and today's low
        # becomes tonight's expected low (not this morning's actual low).
        # Fix: use observed temperature history to capture the true daily
        # high and low, so the classification stays stable all day.
        if self._outdoor_temp_history:
            observed_temps = [t for _, t in self._outdoor_temp_history]
            observed_high = max(observed_temps)
            observed_low = min(observed_temps)
            today_high = max(today_high, observed_high)
            today_low = min(today_low, observed_low)

        # Apply learned weather bias correction to tomorrow's forecast
        if self.config.get("learning_enabled", True) and self.config.get(CONF_WEATHER_BIAS, True):
            weather_bias = self.learning.get_weather_bias()
            if weather_bias["confidence"] != "none":
                bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, weather_bias["high_bias"]))
                bias_l = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, weather_bias["low_bias"]))
                if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F:
                    tomorrow_high += bias_h
                if abs(bias_l) >= MIN_WEATHER_BIAS_APPLY_F:
                    tomorrow_low += bias_l
                _LOGGER.debug(
                    "Weather bias applied: high_bias=%.1f°F low_bias=%.1f°F → tomorrow_high=%.1f°F tomorrow_low=%.1f°F",
                    bias_h,
                    bias_l,
                    tomorrow_high,
                    tomorrow_low,
                )
        else:
            _LOGGER.debug("Skipping weather bias correction: learning_enabled or weather_bias_enabled is False")

        _LOGGER.debug(
            "Forecast parse — entries=%d, today_match=%s, tomorrow_match=%s, "
            "today_high=%.1f, today_low=%.1f, tomorrow_high=%.1f, "
            "tomorrow_low=%.1f (outdoor=%.1f)",
            len(forecast) if forecast else 0,
            today_fc.get("datetime", "?") if today_fc else "NONE",
            tomorrow_fc.get("datetime", "?") if tomorrow_fc else "NONE",
            today_high,
            today_low,
            tomorrow_high,
            tomorrow_low,
            current_outdoor,
        )

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

    def _build_briefing_text(
        self, classification: DayClassification, suggestions: list | None = None
    ) -> tuple[str, str]:
        """Generate briefing text for the given classification.

        Returns (briefing_full, briefing_short).  No notifications are sent.
        """
        if suggestions is None:
            suggestions = self.learning.generate_suggestions()
        wake_time = _parse_time(self.config.get("wake_time", "06:30"))
        sleep_time = _parse_time(self.config.get("sleep_time", "22:30"))

        thermal_model = {}
        if self.config.get("learning_enabled", True):
            thermal_model = self.learning.get_thermal_model()
        adaptive_thermal_active = thermal_model.get("confidence", "none") != "none"

        bedtime_setback_heat: float | None = None
        bedtime_setback_cool: float | None = None
        if classification is not None:
            hvac_mode = classification.hvac_mode
            if hvac_mode == "heat":
                bedtime_setback_heat = compute_bedtime_setback(self.config, thermal_model, classification)
            elif hvac_mode == "cool":
                bedtime_setback_cool = compute_bedtime_setback(self.config, thermal_model, classification)

        _LOGGER.debug(
            "Bedtime setback: heat=%s cool=%s",
            bedtime_setback_heat,
            bedtime_setback_cool,
        )

        briefing_kwargs = dict(
            classification=classification,
            comfort_heat=self.config["comfort_heat"],
            comfort_cool=self.config["comfort_cool"],
            setback_heat=self.config["setback_heat"],
            setback_cool=self.config["setback_cool"],
            wake_time=wake_time,
            sleep_time=sleep_time,
            learning_suggestions=suggestions if suggestions else None,
            debounce_seconds=self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS),
            manual_grace_seconds=self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS),
            automation_grace_seconds=self.config.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS),
            grace_active=self.automation_engine._grace_active,
            grace_source=self.automation_engine._last_resume_source,
            temp_unit=self.config.get("temp_unit", "fahrenheit"),
            bedtime_setback_heat=bedtime_setback_heat,
            bedtime_setback_cool=bedtime_setback_cool,
            adaptive_thermal_active=adaptive_thermal_active,
        )
        return generate_briefing(**briefing_kwargs), generate_briefing(**briefing_kwargs, verbosity="tldr_only")

    async def _async_send_briefing(self, now: datetime) -> None:
        """Generate and send the daily briefing."""
        if self._briefing_sent_today:
            return

        forecast = await self._get_forecast()
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        if not forecast:
            return

        prev_type = self._current_classification.day_type if self._current_classification else None
        classification = classify_day(forecast, previous_day_type=prev_type)
        self._current_classification = classification

        # Inject thermal model into automation engine for adaptive scheduling
        if self.config.get("learning_enabled", True):
            thermal_model = self.learning.get_thermal_model()
            self.automation_engine._thermal_model = thermal_model
        else:
            thermal_model = {}
            self.automation_engine._thermal_model = {}
        confidence = thermal_model.get("confidence", "none")
        obs_count = thermal_model.get("observation_count_heat", 0) + thermal_model.get("observation_count_cool", 0)
        _LOGGER.debug(
            "Thermal model: confidence=%s observations=%d heat_rate=%s cool_rate=%s",
            confidence,
            obs_count,
            thermal_model.get("heating_rate_f_per_hour"),
            thermal_model.get("cooling_rate_f_per_hour"),
        )
        await self.automation_engine.apply_classification(classification)

        # Initialize today's learning record
        self._today_record = DailyRecord(
            date=dt_util.now().strftime("%Y-%m-%d"),
            day_type=classification.day_type,
            trend_direction=classification.trend_direction,
            windows_recommended=classification.windows_recommended,
            window_open_time=(classification.window_open_time.isoformat() if classification.window_open_time else None),
            window_close_time=(
                classification.window_close_time.isoformat() if classification.window_close_time else None
            ),
            hvac_mode_recommended=classification.hvac_mode,
        )

        # Capture raw forecast high/low for weather bias learning
        if (
            self.config.get("learning_enabled", True)
            and self._today_record is not None
            and self._current_classification
        ):
            self._today_record.forecast_high_f = self._current_classification.today_high
            self._today_record.forecast_low_f = self._current_classification.today_low

        # Generate briefing text and track which suggestions were sent
        suggestions = self.learning.generate_suggestions()
        if self._today_record:
            self._today_record.suggestion_sent = self.learning.get_last_suggestion_keys()

        self._last_briefing, self._last_briefing_short = self._build_briefing_text(
            classification, suggestions=suggestions
        )
        self._briefing_day_type = classification.day_type

        # In observe-only mode, skip sending the notification
        if not self._automation_enabled:
            _LOGGER.info("[DRY RUN] Briefing generated but notification skipped (automation disabled)")
            self._briefing_sent_today = True
            await self._async_save_state()
            return

        # Send push notification — short TLDR summary
        _notify_svc = self.config["notify_service"]
        _notify_name = _notify_svc.split(".")[-1] if "." in _notify_svc else _notify_svc
        if self.config.get("push_briefing", True):
            await self.hass.services.async_call(
                "notify",
                _notify_name,
                {"message": self._last_briefing_short, "title": "🏠 Your Home Climate Plan for Today"},
            )
        # Send email — full briefing
        if self.config.get("email_briefing", True):
            await self.hass.services.async_call(
                "notify",
                "send_email",
                {"message": self._last_briefing, "title": "🏠 Your Home Climate Plan for Today"},
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
                    sum(t for _, t in self._indoor_temp_history) / len(self._indoor_temp_history),
                    1,
                )
            # Capture observed outdoor high/low for weather bias learning
            if self.config.get("learning_enabled", True) and self._outdoor_temp_history:
                observed_temps = [t for _, t in self._outdoor_temp_history]
                self._today_record.observed_high_f = round(max(observed_temps), 1)
                self._today_record.observed_low_f = round(min(observed_temps), 1)
            # Flush any accumulated HVAC runtime
            self._flush_hvac_runtime()
            self.learning.record_day(self._today_record)
            await self.hass.async_add_executor_job(self.learning.save_state)
            _LOGGER.info("Day record saved for learning")

        self._today_record = None
        self._briefing_sent_today = False
        self._briefing_day_type = None
        self._hvac_on_since = None
        self._last_violation_check = None
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

            debounce_sec = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
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
                        c = self._current_classification
                        _LOGGER.debug(
                            "Debounce expired, sensor still open: %s "
                            "(classification=%s, hvac_mode=%s, windows_recommended=%s, "
                            "planned_window_active=%s)",
                            eid,
                            c.day_type if c else "none",
                            c.hvac_mode if c else "none",
                            c.windows_recommended if c else False,
                            self.automation_engine._is_within_planned_window_period(),
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
                                    self._today_record.window_open_actual_time = dt_util.now().isoformat()

                            # Always track physical window opens (independent of recommendations)
                            if not self._today_record.windows_physically_opened:
                                self._today_record.windows_physically_opened = True
                                self._today_record.window_physical_open_time = dt_util.now().isoformat()

                            await self._async_save_state()

                self.hass.async_create_task(_do_debounce())

            cancel = async_call_later(self.hass, debounce_sec, _debounce_expired)
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
            all_closed = all(not self._is_sensor_open(s) for s in self._resolved_sensors)
            if all_closed:
                # Track window close time if we were tracking compliance
                if (
                    self._today_record
                    and self._today_record.windows_opened
                    and self._today_record.window_close_actual_time is None
                ):
                    self._today_record.window_close_actual_time = dt_util.now().isoformat()
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
        if self.automation_engine.is_paused_by_door and new_state.state not in ("off", "unavailable", "unknown"):
            if not self.automation_engine._hvac_command_pending and not self._is_recent_hvac_command(
                threshold_seconds=3.0
            ):
                _LOGGER.info(
                    "Manual HVAC override detected during door/window pause: %s -> %s",
                    old_state.state,
                    new_state.state,
                )
                await self.automation_engine.handle_manual_override_during_pause()
                self._cancel_all_debounce_timers()
            else:
                _LOGGER.debug(
                    "Skipping pause-override detection: HVAC mode change was automation-initiated "
                    "(pending=%s, recent_command=%s)",
                    self.automation_engine._hvac_command_pending,
                    self._is_recent_hvac_command(threshold_seconds=3.0),
                )
        elif (
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and not self.automation_engine._manual_override_active
            and not self.automation_engine._hvac_command_pending
            and not self._is_recent_hvac_command()
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
            self._hvac_session_start_indoor_temp = self._get_indoor_temp()
            weather_entity = self.config.get("weather_entity")
            weather_attrs = (
                self.hass.states.get(weather_entity).attributes
                if weather_entity and self.hass.states.get(weather_entity)
                else {}
            )
            self._hvac_session_start_outdoor_temp = self._get_outdoor_temp(weather_attrs)
            action = new_state.attributes.get("hvac_action", "").lower()
            if action == "heating":
                self._hvac_session_mode = "heat"
            elif action == "cooling":
                self._hvac_session_mode = "cool"
            else:
                self._hvac_session_mode = None
        elif was_running and not is_running:
            # HVAC just turned off — flush runtime
            self._flush_hvac_runtime()
            self._record_thermal_observation(new_state)
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
                self._today_record.override_details.append(
                    {
                        "time": dt_util.now().strftime("%H:%M"),
                        "old_temp": old_val,
                        "new_temp": new_val,
                        "direction": "up" if magnitude > 0 else "down",
                        "magnitude": abs(magnitude),
                    }
                )
            except (ValueError, TypeError):
                pass  # Non-numeric temps, skip detail recording
            _LOGGER.debug("Possible manual override detected: %s -> %s", old_temp, new_temp)
            await self._async_save_state()

        # Detect manual fan_mode attribute changes on thermostat (Issue #37)
        new_fan_mode = new_state.attributes.get("fan_mode")
        old_fan_mode = old_state.attributes.get("fan_mode")
        if (
            new_fan_mode is not None
            and old_fan_mode is not None
            and new_fan_mode != old_fan_mode
            and not self.automation_engine._fan_command_pending
            and not self.automation_engine._fan_override_active
        ):
            _LOGGER.info(
                "Manual HVAC fan_mode change detected: %s -> %s",
                old_fan_mode,
                new_fan_mode,
            )
            self.automation_engine.handle_fan_manual_override()

    async def _async_fan_entity_changed(self, event: Event) -> None:
        """Detect manual fan entity state changes (Issue #37)."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        if new_state.state == old_state.state:
            return

        # Skip if this change was initiated by us
        if self.automation_engine._fan_command_pending:
            return

        # Skip if fan override is already active
        if self.automation_engine._fan_override_active:
            return

        on_states = {"on"}
        is_on = new_state.state in on_states

        if is_on and not self.automation_engine._fan_active:
            # Fan turned on externally — manual override
            _LOGGER.info(
                "Manual fan override detected: %s -> %s (integration expected fan off)",
                old_state.state,
                new_state.state,
            )
            self.automation_engine.handle_fan_manual_override()
        elif not is_on and self.automation_engine._fan_active:
            # Fan turned off externally — manual override
            _LOGGER.info(
                "Manual fan override detected: %s -> %s (integration expected fan on)",
                old_state.state,
                new_state.state,
            )
            self.automation_engine.handle_fan_manual_override()

    def _record_thermal_observation(self, new_state: Any) -> None:
        """Record a thermal observation when HVAC session ends."""
        if not self.config.get("learning_enabled", True):
            _LOGGER.debug("Skipping thermal observation: learning_enabled=False")
            return
        if self._hvac_on_since is None:
            return
        session_minutes = (dt_util.now() - self._hvac_on_since).total_seconds() / 60.0
        if session_minutes < MIN_THERMAL_SESSION_MINUTES:
            return
        if self._hvac_session_start_indoor_temp is None:
            return
        if self._hvac_session_mode not in ("heat", "cool"):
            return

        end_indoor = self._get_indoor_temp()
        if end_indoor is None:
            return

        temp_delta = abs(end_indoor - self._hvac_session_start_indoor_temp)
        rate = temp_delta / (session_minutes / 60.0)
        if rate < MIN_THERMAL_RATE_F_PER_HOUR or rate > MAX_THERMAL_RATE_F_PER_HOUR:
            _LOGGER.debug(
                "Thermal obs skipped: rate=%.2f°F/hr outside [%.1f, %.1f] range",
                rate,
                MIN_THERMAL_RATE_F_PER_HOUR,
                MAX_THERMAL_RATE_F_PER_HOUR,
            )
            return

        obs = {
            "timestamp": dt_util.now().isoformat(),
            "date": dt_util.now().date().isoformat(),
            "hvac_mode": self._hvac_session_mode,
            "session_minutes": round(session_minutes, 1),
            "rate_f_per_hour": round(rate, 3),
            "outdoor_temp_f": round(self._hvac_session_start_outdoor_temp, 1)
            if self._hvac_session_start_outdoor_temp is not None
            else 0.0,
            "start_indoor_f": round(self._hvac_session_start_indoor_temp, 1),
            "end_indoor_f": round(end_indoor, 1),
        }
        self.learning.record_thermal_observation(obs)

        if self._today_record is not None:
            self._today_record.thermal_session_count += 1
            if (
                self._today_record.peak_hvac_rate_f_per_hour is None
                or rate > self._today_record.peak_hvac_rate_f_per_hour
            ):
                self._today_record.peak_hvac_rate_f_per_hour = round(rate, 3)

        _LOGGER.debug(
            "Thermal obs recorded: mode=%s rate=%.2f°F/hr session=%.0fmin outdoor=%.1f°F",
            self._hvac_session_mode,
            rate,
            session_minutes,
            obs["outdoor_temp_f"],
        )

    def _compute_next_action(self, c: DayClassification | None, indoor_temp: float | None = None) -> str:
        """Compute the next recommended human action for display."""
        if not c:
            return "Waiting for forecast data..."

        if self._occupancy_mode == OCCUPANCY_VACATION:
            return "On vacation — deep energy-saving setback active."
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return "You're away — automation managing temperature."

        now = dt_util.now().time()
        unit = self.config.get("temp_unit", "fahrenheit")
        comfort_cool = self.config.get("comfort_cool", 75)

        if c.windows_recommended:
            if c.window_open_time and now < c.window_open_time:
                return f"Open windows at {c.window_open_time.strftime('%I:%M %p')}"
            elif c.window_close_time and now < c.window_close_time:
                return f"Close windows by {c.window_close_time.strftime('%I:%M %p')}"
            elif now >= time(ECONOMIZER_EVENING_START_HOUR, 0):
                return "Open windows to cool down — outdoor air may be cooler now."

        if c.day_type == DAY_TYPE_HOT:
            threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
            if c.window_opportunity_morning and now < time(ECONOMIZER_MORNING_END_HOUR, 0):
                end_t = time(ECONOMIZER_MORNING_END_HOUR, 0).strftime("%I:%M %p").lstrip("0")
                return f"Open windows if outdoor temp is below {format_temp(threshold, unit)} (until {end_t})"
            elif c.window_opportunity_evening and now >= time(ECONOMIZER_EVENING_START_HOUR, 0):
                return f"Open windows if outdoor temp is below {format_temp(threshold, unit)}"
            return "Keep windows and blinds closed. AC is handling it."
        elif c.day_type == DAY_TYPE_COLD:
            return "Keep doors closed — help the heater out."

        if indoor_temp is not None and indoor_temp > comfort_cool:
            return f"Indoor temp is {format_temp(indoor_temp, unit)} — open windows or turn on a fan to cool down."

        return "No action needed right now. Automation is handling it."

    def _compute_automation_status(self) -> str:
        """Compute the current automation status string."""
        if not self._automation_enabled:
            return "disabled"
        # Check if windows are open during a planned window period (not a pause)
        if self.automation_engine._is_within_planned_window_period() and self._any_sensor_open():
            return "windows open (as planned)"
        if self.automation_engine.natural_vent_active:
            return "natural ventilation"
        if self.automation_engine.is_paused_by_door:
            return "paused — door/window open"
        if self.automation_engine._grace_active:
            if self.automation_engine._resumed_from_pause:
                return "resumed — door/window override"
            source = self.automation_engine._last_resume_source or "automation"
            return f"grace period ({source})"
        if self._occupancy_mode == OCCUPANCY_VACATION:
            return "active (vacation)"
        if self._occupancy_mode == OCCUPANCY_AWAY:
            return "active (away)"
        if self._occupancy_mode == OCCUPANCY_GUEST:
            return "active (guest)"
        return "active"

    def _compute_fan_status(self) -> str:
        """Compute the current fan status string."""
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return "disabled"
        if ae._fan_override_active:
            return "override — on" if ae._fan_active else "override — off"
        if ae._fan_active:
            return "active"
        return "inactive"

    def _compute_contact_status(self) -> str:
        """Compute the contact sensor summary string."""
        if not self._resolved_sensors:
            return "no sensors"
        open_count = sum(1 for s in self._resolved_sensors if self._is_sensor_open(s))
        if open_count == 0:
            return "all closed"
        return f"{open_count} open"

    def _compute_contact_details(self) -> list[dict[str, Any]]:
        """Return per-sensor details for contact status attributes."""
        details = []
        for sensor_id in self._resolved_sensors:
            friendly = sensor_id.split(".")[-1].replace("_", " ").title()
            details.append(
                {
                    "entity_id": sensor_id,
                    "friendly_name": friendly,
                    "open": self._is_sensor_open(sensor_id),
                }
            )
        return details

    def _compute_next_automation_action(self, c: DayClassification | None) -> tuple[str, str]:
        """Compute the next scheduled automation action and its time.

        Returns:
            Tuple of (action_description, execution_time_str).
        """
        if not c:
            return ("Waiting for classification...", "")

        now = dt_util.now()
        now_time = now.time()

        # Check if windows are open during planned window period
        if self.automation_engine._is_within_planned_window_period() and self._any_sensor_open():
            return ("Windows open as recommended", "")

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
            unit = self.config.get("temp_unit", "fahrenheit")
            if c.hvac_mode in ("heat", "cool"):
                thermal_model = getattr(self.automation_engine, "_thermal_model", None) or {}
                bedtime_target = compute_bedtime_setback(self.config, thermal_model, c)
                _LOGGER.debug(
                    "Bedtime setback: %.1f°F (via compute_bedtime_setback)",
                    bedtime_target,
                )
                mode_label = "heat" if c.hvac_mode == "heat" else "cool"
                events.append((st, f"Bedtime — {mode_label} setback to {format_temp(bedtime_target, unit)}"))
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

    @property
    def yesterday_record(self) -> dict | None:
        """Return yesterday's learning record, if available."""
        yesterday_str = (dt_util.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return self.learning.get_record_by_date(yesterday_str)

    @property
    def tomorrow_plan(self) -> dict | None:
        """Return a projected plan for tomorrow based on current classification."""
        c = self._current_classification
        if not c:
            return None

        tomorrow_str = (dt_util.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # Classify tomorrow by swapping tomorrow's temps into "today" position.
        # Trend will show as "stable" since we lack the day-after-tomorrow forecast.
        tomorrow_forecast = ForecastSnapshot(
            today_high=c.tomorrow_high,
            today_low=c.tomorrow_low,
            tomorrow_high=c.tomorrow_high,
            tomorrow_low=c.tomorrow_low,
            current_outdoor_temp=c.today_low,
        )
        tomorrow_class = classify_day(tomorrow_forecast)

        return {
            "date": tomorrow_str,
            "day_type": tomorrow_class.day_type,
            "trend_direction": tomorrow_class.trend_direction,
            "hvac_mode": tomorrow_class.hvac_mode,
            "windows_recommended": tomorrow_class.windows_recommended,
            "window_open_time": (
                tomorrow_class.window_open_time.isoformat() if tomorrow_class.window_open_time else None
            ),
            "window_close_time": (
                tomorrow_class.window_close_time.isoformat() if tomorrow_class.window_close_time else None
            ),
            "pre_condition": tomorrow_class.pre_condition,
            "expected_high": c.tomorrow_high,
            "expected_low": c.tomorrow_low,
        }

    def get_chart_data(self) -> dict[str, Any]:
        """Build chart data for the dashboard panel.

        Returns a dict with four series: predicted outdoor, predicted indoor,
        actual outdoor, and actual indoor temperatures over a 24-hour period.
        """
        now = dt_util.now()
        current_hour = now.hour + now.minute / 60.0

        predicted_outdoor, predicted_indoor = compute_predicted_temps(
            self._current_classification,
            self.config,
            self._hourly_forecast_temps,
            thermal_model=getattr(self.automation_engine, "_thermal_model", None),
        )

        thermal_model = self.learning.get_thermal_model() if self.learning else {}
        unit = self.config.get("temp_unit", "fahrenheit")
        _LOGGER.debug(
            "Chart data: thermal_model confidence=%s heat_obs=%d cool_obs=%d",
            thermal_model.get("confidence", "none"),
            thermal_model.get("observation_count_heat", 0),
            thermal_model.get("observation_count_cool", 0),
        )
        return {
            "predicted_outdoor": predicted_outdoor,
            "predicted_indoor": predicted_indoor,
            "actual_outdoor": [{"time": ts, "temp": t} for ts, t in self._outdoor_temp_history],
            "actual_indoor": [{"time": ts, "temp": t} for ts, t in self._indoor_temp_history],
            "current_hour": round(current_hour, 1),
            "thermal_model": {
                "confidence": thermal_model.get("confidence", "none"),
                "observation_count_heat": thermal_model.get("observation_count_heat", 0),
                "observation_count_cool": thermal_model.get("observation_count_cool", 0),
                "heating_rate": (
                    convert_delta(thermal_model["heating_rate_f_per_hour"], unit)
                    if thermal_model.get("heating_rate_f_per_hour") is not None
                    else None
                ),
                "cooling_rate": (
                    convert_delta(thermal_model["cooling_rate_f_per_hour"], unit)
                    if thermal_model.get("cooling_rate_f_per_hour") is not None
                    else None
                ),
                "unit": unit,
            },
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
                "window_open_time": (c.window_open_time.isoformat() if c and c.window_open_time else None),
                "window_close_time": (c.window_close_time.isoformat() if c and c.window_close_time else None),
                "window_opportunity_morning": c.window_opportunity_morning if c else None,
                "window_opportunity_evening": c.window_opportunity_evening if c else None,
                "window_opportunity_morning_start": (
                    c.window_opportunity_morning_start.isoformat() if c and c.window_opportunity_morning_start else None
                ),
                "window_opportunity_morning_end": (
                    c.window_opportunity_morning_end.isoformat() if c and c.window_opportunity_morning_end else None
                ),
                "window_opportunity_evening_start": (
                    c.window_opportunity_evening_start.isoformat() if c and c.window_opportunity_evening_start else None
                ),
                "window_opportunity_evening_end": (
                    c.window_opportunity_evening_end.isoformat() if c and c.window_opportunity_evening_end else None
                ),
                "pre_condition": c.pre_condition if c else None,
                "pre_condition_target": c.pre_condition_target if c else None,
                "setback_modifier": c.setback_modifier if c else None,
                "today_low": c.today_low if c else None,
                "tomorrow_low": c.tomorrow_low if c else None,
            },
            "last_action_time": ae._last_action_time,
            "last_action_reason": ae._last_action_reason,
            "manual_override_active": ae._manual_override_active,
            "manual_override_mode": ae._manual_override_mode,
            "manual_override_time": ae._manual_override_time,
            "manual_grace_duration": ae.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS),
            "next_automation_action": self.data.get(ATTR_NEXT_AUTOMATION_ACTION, "") if self.data else "",
            "next_automation_time": self.data.get(ATTR_NEXT_AUTOMATION_TIME, "") if self.data else "",
            # Fan state (Issue #37)
            "fan_active": ae._fan_active,
            "fan_on_since": ae._fan_on_since,
            "fan_runtime_minutes": ae._get_fan_runtime_minutes(),
            "fan_override_active": ae._fan_override_active,
            "fan_override_time": ae._fan_override_time,
            "fan_mode_config": ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
            "economizer_active": ae._economizer_active,
            "economizer_phase": ae._economizer_phase,
            "resumed_from_pause": ae._resumed_from_pause,
            "occupancy_away_timer_pending": self._occupancy_away_timer_cancel is not None,
        }

    async def async_shutdown(self) -> None:
        """Clean up on shutdown."""
        # Flush HVAC runtime and save state before cleanup
        self._flush_hvac_runtime()
        await self._async_save_state()

        # Cancel any pending occupancy away setback timer
        self._cancel_occupancy_away_timer()

        # Cancel any pending debounce timers
        for cancel in self._door_open_timers.values():
            cancel()
        self._door_open_timers.clear()

        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        self._unsubscribe_door_window_listeners()
        self.automation_engine.cleanup()


def _compute_ramp_hours(temp_delta: float, hvac_mode: str, thermal_model: dict | None) -> float:
    """Compute heating/cooling ramp duration in hours from thermal model."""
    if thermal_model is None or thermal_model.get("confidence") == "none":
        return 0.5
    if hvac_mode == "heat":
        rate = thermal_model.get("heating_rate_f_per_hour")
    else:
        rate = thermal_model.get("cooling_rate_f_per_hour")
    if not rate:
        return 0.5
    return max(temp_delta / rate, 0.25)


def compute_predicted_temps(
    classification: DayClassification | None,
    config: dict[str, Any],
    hourly_forecast: list[dict] | None = None,
    thermal_model: dict | None = None,
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
    predicted_outdoor = _build_outdoor_curve(high=c.today_high, low=c.today_low, hourly_forecast=hourly_forecast)

    # --- Predicted indoor temps (from schedule + setpoints) ---
    predicted_indoor: list[dict] = []
    wake = _parse_time(config.get("wake_time", "06:30"))
    sleep = _parse_time(config.get("sleep_time", "22:30"))
    wake_h = wake.hour + wake.minute / 60.0
    sleep_h = sleep.hour + sleep.minute / 60.0

    comfort = config.get("comfort_heat", 70) if c.hvac_mode == "heat" else config.get("comfort_cool", 75)
    setback = config.get("setback_heat", 60) if c.hvac_mode == "heat" else config.get("setback_cool", 80)
    setback += c.setback_modifier

    bedtime_depth = DEFAULT_SETBACK_DEPTH_F if c.hvac_mode == "heat" else DEFAULT_SETBACK_DEPTH_COOL_F
    bedtime_setback = comfort - bedtime_depth + c.setback_modifier if c.hvac_mode == "heat" else comfort + bedtime_depth

    ramp_h_morning = _compute_ramp_hours(abs(comfort - setback), c.hvac_mode, thermal_model)
    ramp_h_evening = _compute_ramp_hours(abs(comfort - bedtime_setback), c.hvac_mode, thermal_model)

    for h in range(24):
        if h < wake_h:
            temp = setback  # overnight setback
        elif h < wake_h + ramp_h_morning:
            # ramping from setback to comfort
            frac = (h - wake_h) / ramp_h_morning
            temp = setback + frac * (comfort - setback)
        elif h < sleep_h:
            if c.hvac_mode == "off" and predicted_outdoor:
                # drift toward outdoor when HVAC off
                outdoor_t = predicted_outdoor[h]["temp"]
                drift_rate = (
                    3.0
                    if (
                        c.windows_recommended
                        and c.window_open_time
                        and c.window_close_time
                        and c.window_open_time.hour <= h < c.window_close_time.hour
                    )
                    else 1.5
                )
                # Simple drift model: move toward outdoor at drift_rate °/hr
                diff = outdoor_t - comfort
                temp = comfort + min(abs(diff), drift_rate) * (1 if diff > 0 else -1)
            else:
                temp = comfort
        elif h < sleep_h + ramp_h_evening:
            # ramping from comfort to bedtime setback
            frac = (h - sleep_h) / ramp_h_evening
            temp = comfort + frac * (bedtime_setback - comfort)
        else:
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
            "temp": round(mid + amp * math.cos(2 * math.pi * (h - 15) / 24), 1),
        }
        for h in range(24)
    ]


def _build_outdoor_curve(
    high: float,
    low: float,
    hourly_forecast: list[dict] | None,
) -> list[dict]:
    """Build 24 hourly outdoor temperature predictions.

    Uses actual hourly forecast data for the *shape* of the curve (when
    peaks and troughs occur), then normalises the result so the range
    spans the daily forecast ``high`` / ``low``.  Falls back to the
    sinusoidal model when no usable hourly data is available.
    """
    if not hourly_forecast:
        return _cosine_outdoor_curve(high, low)

    # Parse hourly entries into an integer-hour lookup (today only).
    # Use dt_util for timezone-aware "today" so UTC datetimes are
    # compared against the correct local date.
    today = dt_util.now().date()
    known: dict[int, float] = {}
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            # Convert to local time before extracting the date so that
            # UTC timestamps map to the correct calendar day.
            local_dt = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
            if local_dt.date() != today:
                continue
            known[local_dt.hour] = float(temp)
        except (ValueError, TypeError):
            continue

    if not known:
        return _cosine_outdoor_curve(high, low)

    # Fill all 24 hours: known values, linear interpolation for gaps,
    # cosine fallback at the edges.
    cosine = {p["hour"]: p["temp"] for p in _cosine_outdoor_curve(high, low)}
    known_hours = sorted(known)
    raw: list[float] = []

    for h in range(24):
        if h in known:
            raw.append(known[h])
        else:
            before = [k for k in known_hours if k < h]
            after = [k for k in known_hours if k > h]
            if before and after:
                h0, h1 = before[-1], after[0]
                frac = (h - h0) / (h1 - h0)
                raw.append(known[h0] + frac * (known[h1] - known[h0]))
            else:
                raw.append(cosine[h])

    # Normalise so the curve spans the daily high/low.  The hourly
    # forecast often has a narrower range than the daily summary; this
    # keeps the shape realistic while honouring the reported extremes.
    raw_min = min(raw)
    raw_max = max(raw)
    if raw_max - raw_min > 0.1:
        scale = (high - low) / (raw_max - raw_min)
        result = [{"hour": h, "temp": round(low + (t - raw_min) * scale, 1)} for h, t in enumerate(raw)]
    else:
        # Flat or near-flat hourly data — fall back to cosine
        result = _cosine_outdoor_curve(high, low)

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
