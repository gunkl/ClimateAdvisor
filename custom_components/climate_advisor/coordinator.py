"""Data coordinator for Climate Advisor.

The coordinator is the central brain. It runs on a schedule, pulls forecast
data, classifies the day, triggers automations, sends briefings, and feeds
data to the learning engine.
"""

from __future__ import annotations

import contextlib
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
from .chart_log import ChartStateLog
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
    ATTR_FORECAST_HIGH,
    ATTR_FORECAST_HIGH_TOMORROW,
    ATTR_FORECAST_LOW,
    ATTR_FORECAST_LOW_TOMORROW,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_RUNTIME_TODAY,
    ATTR_INDOOR_TEMP,
    ATTR_LAST_ACTION_REASON,
    ATTR_LAST_ACTION_TIME,
    ATTR_LEARNING_SUGGESTIONS,
    ATTR_NEXT_ACTION,
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
    ATTR_OCCUPANCY_MODE,
    ATTR_OUTDOOR_TEMP,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    CHART_LOG_MAX_DAYS,
    CONF_AI_API_KEY,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
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
    EVENT_LOG_CAP,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    INVESTIGATION_REPORT_HISTORY_CAP,
    INVESTIGATION_REPORTS_FILE,
    MAX_WEATHER_BIAS_APPLY_F,
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
    THERMAL_MAX_ACTIVE_SAMPLES,
    THERMAL_MAX_POST_HEAT_SAMPLES,
    THERMAL_MIN_DECAY_F,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_POST_HEAT_TIMEOUT_MINUTES,
    THERMAL_STABILIZATION_THRESHOLD_F,
    THERMAL_STABILIZATION_WINDOW_MINUTES,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    VACATION_SETBACK_EXTRA,
    VERSION,
)
from .learning import DailyRecord, LearningEngine
from .state import StatePersistence
from .temperature import convert_delta, format_temp, from_fahrenheit, to_fahrenheit

_LOGGER = logging.getLogger(__name__)

# Degrees below comfort_heat at which outdoor temp is too cold to recommend opening windows.
# With default comfort_heat=70°F this means outdoor must be ≥ 55°F for windows to be recommended.
_WINDOWS_EXTREME_COLD_MARGIN = 15.0


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
        self._chart_log = ChartStateLog(Path(hass.config.config_dir), max_days=CHART_LOG_MAX_DAYS)
        self._chart_log.load()
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
        self.automation_engine._emit_event_callback = self._emit_event

        # Event log ring buffer (Issue #76) — timestamped automation events for debug download
        self._event_log: list[dict] = []

        # AI subsystem (only if enabled and API key present)
        self.claude_client: ClaudeAPIClient | None = None
        self.ai_skills: AISkillRegistry | None = None
        self._ai_report_history: list[dict] = []
        self._investigation_report_history: list[dict] = []
        if config.get(CONF_AI_ENABLED) and config.get(CONF_AI_API_KEY):
            from .ai_skills import AISkillRegistry as _AISkillRegistry
            from .ai_skills_activity import register_activity_skill
            from .ai_skills_investigator import register_investigator_skill
            from .claude_api import ClaudeAPIClient as _ClaudeAPIClient

            self.claude_client = _ClaudeAPIClient(config)
            self.ai_skills = _AISkillRegistry()
            register_activity_skill(self.ai_skills)
            if config.get(CONF_AI_INVESTIGATOR_ENABLED, False):
                register_investigator_skill(self.ai_skills)
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
        self._thermal_factors: dict | None = None

        # Observe-only mode: when disabled, automation still runs but skips actions
        self._automation_enabled: bool = True

        # HVAC runtime tracking
        self._hvac_on_since: datetime | None = None
        self._last_outdoor_temp: float | None = None  # most recent outdoor reading for gate checks
        # Thermal observation pipeline (Issue #114)
        self._pending_thermal_event: dict | None = None
        self._pre_heat_sample_buffer: list[dict] = []  # rolling pre-heat window, max 15
        self._startup_hvac_initialized: bool = False  # Issue #96: prevents repeated late-start init
        self._last_state_contradiction_time: datetime | None = None  # dedup for state_contradiction_warning events
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

        # Start minimum fan runtime rolling cycle (Issue #77) — not clock-aligned
        await self.automation_engine.start_min_fan_runtime_cycles()

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

        # Restore AI stats regardless of date boundary — monthly budget and cumulative
        # counters must persist across reboots. Daily counters self-correct via
        # _reset_daily_counters_if_needed() inside restore_persistent_stats().
        if self.claude_client:
            ai_stats = state.get("ai_stats")
            if ai_stats and isinstance(ai_stats, dict):
                self.claude_client.restore_persistent_stats(ai_stats)

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

        # Occupancy state — sync to engine so guards are active from startup (Issue #85)
        self._occupancy_mode = state.get("occupancy_mode", OCCUPANCY_HOME)
        self.automation_engine.set_occupancy_mode(self._occupancy_mode)
        away_since = state.get("occupancy_away_since")
        if away_since:
            try:
                self._occupancy_away_since = datetime.fromisoformat(away_since)
            except (ValueError, TypeError):
                self._occupancy_away_since = None

        # Load AI report history if AI subsystem is active
        if self.claude_client:
            await self.hass.async_add_executor_job(self._load_ai_reports)
            await self.hass.async_add_executor_job(self._load_investigation_reports)

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
            "ai_stats": self.claude_client.get_persistent_stats() if self.claude_client else {},
        }

    async def _async_save_state(self) -> None:
        """Persist current operational state to disk."""
        state_dict = self._build_state_dict()
        await self.hass.async_add_executor_job(self._state_persistence.save, state_dict)

    async def async_store_ai_report(self, result: dict) -> None:
        """Store an AI activity report result and persist to disk."""
        import json  # noqa: F401 — imported for _save_ai_reports called via executor

        report_entry = {
            "timestamp": dt_util.now().isoformat(),
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

    async def async_store_investigation_report(self, result: dict) -> None:
        """Store an investigation report result in history and persist to disk."""

        entry = {
            "timestamp": dt_util.now().isoformat(),
            "result": result,
        }
        self._investigation_report_history.append(entry)
        if len(self._investigation_report_history) > INVESTIGATION_REPORT_HISTORY_CAP:
            self._investigation_report_history = self._investigation_report_history[-INVESTIGATION_REPORT_HISTORY_CAP:]
        await self.hass.async_add_executor_job(self._save_investigation_reports)

    def get_investigation_report_history(self) -> list[dict]:
        """Return a copy of the investigation report history."""
        return list(self._investigation_report_history)

    def _save_investigation_reports(self) -> None:
        """Save investigation report history to disk (atomic write)."""
        import json
        import os
        import sys

        filepath = self.hass.config.path(INVESTIGATION_REPORTS_FILE)
        tmp_path = filepath + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._investigation_report_history, f, indent=2, default=str)
            if sys.platform != "win32":
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, filepath)
        except Exception:
            _LOGGER.exception("Failed to save investigation reports to %s", filepath)
            import contextlib

            with contextlib.suppress(OSError):
                os.remove(tmp_path)

    def _load_investigation_reports(self) -> None:
        """Load investigation report history from disk."""
        import json

        filepath = self.hass.config.path(INVESTIGATION_REPORTS_FILE)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._investigation_report_history = data[-INVESTIGATION_REPORT_HISTORY_CAP:]
                _LOGGER.debug(
                    "Loaded %d investigation reports from disk",
                    len(self._investigation_report_history),
                )
            else:
                _LOGGER.warning("Investigation reports file has unexpected format, starting fresh")
                self._investigation_report_history = []
        except FileNotFoundError:
            self._investigation_report_history = []
        except Exception:
            _LOGGER.exception("Failed to load investigation reports from %s", filepath)
            self._investigation_report_history = []

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
        # Issue #85: sync occupancy mode to engine immediately so guards
        # take effect even before the delayed away timer fires
        self.automation_engine.set_occupancy_mode(new_mode)

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

    def _apply_outdoor_windows_gate(self) -> None:
        """Gate windows_recommended against current outdoor temp (Issue #111).

        The classifier sets windows_recommended based on forecast day-type only.
        This method clears the flag when current outdoor conditions would push
        indoor temps outside the comfort zone:
          - outdoor > comfort_cool  → opening windows would overheat the house
          - outdoor < comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN  → extreme cold

        Called after every classify_day() in _async_update_data() and
        async_send_briefing(). No-op when classification is None,
        windows_recommended is already False, or outdoor temp is unavailable.
        """
        c = self._current_classification
        if c is None or not c.windows_recommended:
            return

        outdoor = self._last_outdoor_temp
        if outdoor is None:
            return  # No current data — keep classifier's recommendation

        comfort_cool = float(self.config.get("comfort_cool", 75))
        comfort_heat = float(self.config.get("comfort_heat", 70))

        if outdoor > comfort_cool:
            _LOGGER.debug(
                "windows_recommended → False: outdoor %.1f°F above comfort_cool %.1f°F",
                outdoor,
                comfort_cool,
            )
            c.windows_recommended = False
        elif outdoor < comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN:
            _LOGGER.debug(
                "windows_recommended → False: outdoor %.1f°F below extreme-cold threshold %.1f°F",
                outdoor,
                comfort_heat - _WINDOWS_EXTREME_COLD_MARGIN,
            )
            c.windows_recommended = False

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
        self.automation_engine._hourly_forecast_temps = self._hourly_forecast_temps
        if forecast:
            prev_type = self._current_classification.day_type if self._current_classification else None
            self._current_classification = classify_day(forecast, previous_day_type=prev_type)
            self._last_outdoor_temp = forecast.current_outdoor_temp
            self._apply_outdoor_windows_gate()

            # Chart log: emit classification_change event when day type changes
            if prev_type is not None and prev_type != self._current_classification.day_type:
                with contextlib.suppress(Exception):
                    self._chart_log.append(
                        hvac=self._current_classification.hvac_mode or "",
                        fan=self._fan_is_running() if self.automation_engine else False,
                        indoor=forecast.current_indoor_temp,
                        outdoor=forecast.current_outdoor_temp,
                        windows_open=self._any_sensor_open(),
                        windows_recommended=bool(self._current_classification.windows_recommended),
                        event="classification_change",
                    )

            # Startup safety: only set manual override if the current HVAC
            # mode differs from the classification (Issue #42)
            if self._first_run:
                self._first_run = False
                climate_state = self.hass.states.get(self.config["climate_entity"])
                self._check_startup_override(climate_state, self._current_classification)
                # Recover any pending thermal event that survived restart
                recovered = await self.hass.async_add_executor_job(self.learning.recover_pending_event_on_startup)
                if recovered:
                    _LOGGER.info(
                        "Startup: recovered thermal observation event_id=%s mode=%s grade=%s",
                        recovered.get("event_id", "?"),
                        recovered.get("hvac_mode", "?"),
                        recovered.get("confidence_grade", "?"),
                    )

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
        hvac_mode = _cs.state if _cs else ""
        fan_mode = _cs.attributes.get("fan_mode", "") if _cs else ""

        # Issue #96 Root Cause D: Late-start thermal session for HVAC running at HA startup.
        # _hvac_on_since is only set via state transitions in _async_thermostat_changed.
        # If HA restarts mid-HVAC-session, no transition fires and thermal obs are skipped.
        if (
            _cs is not None
            and str(hvac_action).lower() in {"heating", "cooling"}
            and self._hvac_on_since is None
            and not self._startup_hvac_initialized
        ):
            self._startup_hvac_initialized = True
            await self._initialize_hvac_session_from_current_state(_cs)

        # Emit a structured warning event when the HVAC entity reports an active action
        # (heating/cooling/fan) while hvac_mode is "off".  This surfaces the contradiction
        # in the investigator event log so it is not invisible outside the AI narrative.
        # Suppress when Climate Advisor itself activated fan-only mode (natural ventilation).
        _active_hvac_actions = {"heating", "cooling", "fan"}
        if hvac_mode == "off" and str(hvac_action).lower() in _active_hvac_actions:
            # Suppress when CA activated the fan OR when thermostat ground-truth shows
            # fan running (untracked) — either way the fan action is not a contradiction.
            _ca_fan_running = self.automation_engine._fan_active or self.automation_engine._natural_vent_active
            _fan_untracked = str(hvac_action).lower() == "fan" and self._compute_fan_status() == "running (untracked)"
            _is_expected_fan = str(hvac_action).lower() == "fan" and (_ca_fan_running or _fan_untracked)
            if not _is_expected_fan:
                _now = dt_util.now()
                _dedup_window = timedelta(minutes=30)
                if (
                    self._last_state_contradiction_time is None
                    or (_now - self._last_state_contradiction_time) > _dedup_window
                ):
                    self._emit_event(
                        "state_contradiction_warning",
                        {"hvac_mode": hvac_mode, "hvac_action": hvac_action},
                    )
                    self._last_state_contradiction_time = _now

        _base_runtime = self._today_record.hvac_runtime_minutes if self._today_record else 0.0
        _session_elapsed = (dt_util.now() - self._hvac_on_since).total_seconds() / 60 if self._hvac_on_since else 0.0
        hvac_runtime_today = round(_base_runtime + _session_elapsed, 1)

        # --- Thermal observation pipeline sampling ---
        self._update_pre_heat_buffer()
        self._sample_thermal_event()
        await self._check_stabilization()

        # --- Temperatures for coordinator.data (sensor entities + AI context) ---
        _indoor_temp = self._get_indoor_temp()
        _outdoor_temp = forecast.current_outdoor_temp if forecast else None

        next_auto = self._compute_next_automation_action(c)
        fan_running = self.automation_engine._fan_active
        result = {
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
            ATTR_FAN_RUNNING: fan_running,
            ATTR_HVAC_ACTION: hvac_action,
            ATTR_HVAC_RUNTIME_TODAY: hvac_runtime_today,
            ATTR_CONTACT_STATUS: self._compute_contact_status(),
            ATTR_AI_STATUS: self.claude_client.get_status()["status"] if self.claude_client else "disabled",
            ATTR_INDOOR_TEMP: _indoor_temp,
            ATTR_OUTDOOR_TEMP: _outdoor_temp,
            ATTR_FORECAST_HIGH: c.today_high if c else None,
            ATTR_FORECAST_LOW: c.today_low if c else None,
            ATTR_FORECAST_HIGH_TOMORROW: c.tomorrow_high if c else None,
            ATTR_FORECAST_LOW_TOMORROW: c.tomorrow_low if c else None,
        }

        # Append chart log entry (every coordinator tick — 30-min cadence)
        with contextlib.suppress(Exception):
            indoor_temp = forecast.current_indoor_temp if forecast else None
            outdoor_temp = forecast.current_outdoor_temp if forecast else None
            # Extract current-hour prediction to persist alongside actual reading
            _pred_outdoor_val: float | None = None
            _pred_indoor_val: float | None = None
            if self._current_classification:
                _pred_out, _pred_in = compute_predicted_temps(
                    self._current_classification,
                    self.config,
                    self._hourly_forecast_temps,
                    thermal_model=getattr(self.automation_engine, "_thermal_model", None),
                    thermal_factors=self._thermal_factors,
                )
                _now_h = dt_util.now().hour
                if _pred_out and _now_h < len(_pred_out):
                    _pred_outdoor_val = _pred_out[_now_h]["temp"]
                if _pred_in and _now_h < len(_pred_in):
                    _pred_indoor_val = _pred_in[_now_h]["temp"]
            _hvac_action_str = str(hvac_action).lower() if hvac_action else ""
            _hvac_mode_str = str(hvac_mode).lower() if hvac_mode else ""
            _fan_mode_str = str(fan_mode).lower() if fan_mode else ""
            # Only remap fan→heating/cooling when fan_mode is auto (fan runs as part of
            # HVAC cycle). When fan_mode=on the fan circulates independently — "fan"
            # action does not imply active heating or cooling (#109 regression fix).
            _fan_is_auto = not _fan_mode_str or _fan_mode_str.startswith("auto")
            if _hvac_action_str == "fan" and _fan_is_auto:
                if _hvac_mode_str == "heat":
                    _hvac_action_str = "heating"
                elif _hvac_mode_str in ("cool", "heat_cool"):
                    _hvac_action_str = "cooling"
            self._chart_log.append(
                hvac=_hvac_action_str,
                fan=self._fan_is_running(),
                indoor=indoor_temp,
                outdoor=outdoor_temp,
                windows_open=self._any_sensor_open(),
                windows_recommended=bool(self._current_classification.windows_recommended)
                if self._current_classification
                else False,
                pred_outdoor=_pred_outdoor_val,
                pred_indoor=_pred_indoor_val,
            )
            self._chart_log.save()

        with contextlib.suppress(Exception):
            self._thermal_factors = _compute_thermal_factors(self._chart_log.get_entries("7d"))

        return result

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
                    fc_obj = datetime.fromisoformat(fc_dt)
                    fc_date = dt_util.as_local(fc_obj).date() if fc_obj.tzinfo else fc_obj.date()
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
            occupancy_mode=self._occupancy_mode,
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
        self._last_outdoor_temp = forecast.current_outdoor_temp
        self._apply_outdoor_windows_gate()

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
            # Watchdog: if HVAC ran significantly but no thermal observations were recorded, warn
            if self._today_record.hvac_runtime_minutes > 30.0 and self._today_record.thermal_session_count == 0:
                _LOGGER.warning(
                    "Thermal learning watchdog: %.1f min HVAC runtime today but zero thermal"
                    " observations recorded — check HA logs for 'Thermal obs skipped' entries",
                    self._today_record.hvac_runtime_minutes,
                )
                self._emit_event(
                    "thermal_learning_no_observations",
                    {"hvac_runtime_minutes": round(self._today_record.hvac_runtime_minutes, 1)},
                )
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

                            # Track window compliance — credit any open during a windows-recommended day
                            c = self._current_classification
                            if c and c.windows_recommended and not self._today_record.windows_opened:
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
                await self.automation_engine.handle_manual_override_during_pause(
                    old_mode=old_state.state,
                    new_mode=new_state.state,
                    classification_mode=(
                        self._current_classification.hvac_mode if self._current_classification else None
                    ),
                )
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
            with contextlib.suppress(Exception):
                _indoor = self._get_indoor_temp()
                _ov_weather_entity = self.config.get("weather_entity")
                _ov_weather_attrs = (
                    self.hass.states.get(_ov_weather_entity).attributes
                    if _ov_weather_entity and self.hass.states.get(_ov_weather_entity)
                    else {}
                )
                _outdoor_val = self._get_outdoor_temp(_ov_weather_attrs)
                self._chart_log.append(
                    hvac=new_state.state,
                    fan=self._fan_is_running(),
                    indoor=_indoor,
                    outdoor=_outdoor_val,
                    windows_open=self._any_sensor_open(),
                    windows_recommended=(
                        bool(self._current_classification.windows_recommended)
                        if self._current_classification
                        else False
                    ),
                    event="override",
                )
            self.automation_engine.handle_manual_override(
                old_mode=old_state.state,
                new_mode=new_state.state,
                classification_mode=(self._current_classification.hvac_mode if self._current_classification else None),
            )

        # HVAC runtime tracking via hvac_action (preferred) or mode
        new_action = new_state.attributes.get("hvac_action", "").lower()
        old_action = old_state.attributes.get("hvac_action", "").lower()
        running_actions = {"heating", "cooling"}

        if old_action in running_actions or new_action in running_actions:
            # At least one side shows active heating/cooling — hvac_action is providing a
            # meaningful signal, prefer it for precise on/off edge detection.
            was_running = old_action in running_actions
            is_running = new_action in running_actions
        else:
            # hvac_action gives no heating/cooling signal (both are "fan", "idle", or absent).
            # Some thermostats report hvac_action="fan" persistently (even when off/idle),
            # which would trap this branch indefinitely if we used the old `new_action and
            # old_action` guard.  Fall back to hvac_mode state for reliable edge detection.
            idle_modes = {"off", "unavailable", "unknown", ""}
            was_running = old_state.state not in idle_modes
            is_running = new_state.state not in idle_modes

        if not was_running and is_running:
            # HVAC just turned on — determine session_mode from hvac_action or hvac_mode
            self._hvac_on_since = dt_util.now()
            action = new_action
            if action == "heating":
                session_mode = "heat"
            elif action == "cooling":
                session_mode = "cool"
            elif new_state.state == "heat":
                # Fallback: some thermostats report hvac_action="fan" or "idle" briefly
                # at compressor startup before transitioning to "heating".
                session_mode = "heat"
            elif new_state.state == "cool":
                session_mode = "cool"
            elif new_state.state == "fan_only":
                session_mode = "fan_only"
            else:
                session_mode = None
            if session_mode:
                await self._start_thermal_event(session_mode)
        elif was_running and not is_running:
            # HVAC just turned off — flush runtime and end active phase
            self._flush_hvac_runtime()
            await self._end_active_phase()
            self._hvac_on_since = None
        elif was_running and is_running and old_action != new_action:
            # heat_cool mode: hvac_action switched heating↔cooling mid-session
            if old_action in running_actions and new_action in running_actions:
                _LOGGER.info(
                    "heat_cool mid-session switch %s → %s: abandoning current event",
                    old_action,
                    new_action,
                )
                await self._abandon_thermal_event("heat_cool mode switch mid-session")
                new_session_mode = "heat" if new_action == "heating" else "cool"
                await self._start_thermal_event(new_session_mode)

        # If thermostat is now fully off, clear any stale HVAC-based fan active flag.
        # Only applies to HVAC/Both fan modes — whole-house fans run independently.
        # Natural ventilation is intentionally hvac_mode=off + fan active — do not clear.
        ae = self.automation_engine
        if new_state.state == "off" and ae._fan_active and not ae._fan_override_active:
            _fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            if _fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH) and not ae._natural_vent_active:
                _LOGGER.warning("Thermostat set to off while HVAC fan was marked active — clearing stale fan state")
                ae._fan_active = False

        # Chart_log: event-driven write when hvac_action transitions in/out of heating/cooling.
        # 30-minute polling can miss short cycles entirely — this captures the start and end
        # edge of every real heating/cooling event regardless of when the next poll fires.
        _chart_active_actions = {"heating", "cooling"}
        _was_chart_active = old_action in _chart_active_actions
        _is_chart_active = new_action in _chart_active_actions
        if _was_chart_active != _is_chart_active:
            with contextlib.suppress(Exception):
                self._chart_log.append(
                    hvac=new_action or new_state.state.lower(),
                    fan=self._fan_is_running(),
                    indoor=self._get_indoor_temp(),
                    outdoor=None,
                    windows_open=self._any_sensor_open(),
                    windows_recommended=(
                        bool(self._current_classification.windows_recommended)
                        if self._current_classification
                        else False
                    ),
                    event="hvac_action_change",
                )
                self._chart_log.save()

        # Detect manual override: temperature changed but not by us
        new_temp = new_state.attributes.get("temperature")
        old_temp = old_state.attributes.get("temperature")

        if (
            new_temp != old_temp
            and self._today_record
            and not self.automation_engine._temp_command_pending
            and not self.automation_engine._hvac_command_pending
            and not self._is_recent_hvac_command(threshold_seconds=30.0)
        ):
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
            and not self.automation_engine._hvac_command_pending
            and not self._is_recent_hvac_command(threshold_seconds=30.0)
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

    async def _initialize_hvac_session_from_current_state(self, climate_state: Any) -> None:
        """Late-start HVAC session when HA restarted mid-session (Issue #96).

        Sets session start from current time. Thermal observations will cover
        only the post-restart portion — better than zero observations.
        Called from _async_update_data() on first update if HVAC is already running.
        """
        self._hvac_on_since = dt_util.now()
        action = climate_state.attributes.get("hvac_action", "").lower()
        if action == "heating":
            session_mode = "heat"
        elif action == "cooling":
            session_mode = "cool"
        elif climate_state.state == "heat":
            session_mode = "heat"
        elif climate_state.state == "cool":
            session_mode = "cool"
        elif climate_state.state == "fan_only":
            session_mode = "fan_only"
        else:
            session_mode = None
        _LOGGER.warning(
            "Late-start HVAC session initialized: mode=%s (HVAC was running at HA startup — "
            "session duration will be shorter than actual)",
            session_mode,
        )
        if session_mode:
            await self._start_thermal_event(session_mode)

    # ------------------------------------------------------------------
    # Thermal observation pipeline (Issue #114)
    # ------------------------------------------------------------------

    def _get_current_sample(self, elapsed_minutes: float) -> dict:
        """Build a sample dict from current sensor readings."""
        indoor = self._get_indoor_temp()
        weather_entity = self.config.get("weather_entity")
        weather_attrs = (
            self.hass.states.get(weather_entity).attributes
            if weather_entity and self.hass.states.get(weather_entity)
            else {}
        )
        outdoor = self._get_outdoor_temp(weather_attrs)
        return {
            "timestamp": dt_util.now().isoformat(),
            "indoor_temp_f": indoor if indoor is not None else 0.0,
            "outdoor_temp_f": outdoor if outdoor is not None else 0.0,
            "elapsed_minutes": elapsed_minutes,
        }

    def _update_pre_heat_buffer(self) -> None:
        """Append current reading to the rolling pre-heat buffer (max 15 entries).

        Called every update cycle when no active thermal event is running.
        """
        if self._pending_thermal_event is not None:
            return
        from .const import THERMAL_PRE_HEAT_BUFFER_MINUTES

        now = dt_util.now()
        sample = self._get_current_sample(0.0)
        sample["timestamp"] = now.isoformat()
        self._pre_heat_sample_buffer.append(sample)
        # Keep only entries within the buffer window
        cutoff = (now - timedelta(minutes=THERMAL_PRE_HEAT_BUFFER_MINUTES)).isoformat()
        self._pre_heat_sample_buffer = [s for s in self._pre_heat_sample_buffer if s["timestamp"] >= cutoff]
        # Hard cap at 15
        if len(self._pre_heat_sample_buffer) > 15:
            self._pre_heat_sample_buffer = self._pre_heat_sample_buffer[-15:]

    async def _start_thermal_event(self, session_mode: str) -> None:
        """Begin a new thermal observation event.

        Abandons any existing event first. Snapshots the pre-heat buffer.
        """
        if not self.config.get("learning_enabled", True):
            return
        if self._pending_thermal_event is not None:
            await self._abandon_thermal_event("new HVAC session started")

        import uuid

        now = dt_util.now()
        # Re-stamp pre-heat buffer relative to elapsed_minutes=0 at event start
        pre_samples = []
        for s in self._pre_heat_sample_buffer:
            try:
                from homeassistant.util import dt as dt_util2

                ts = dt_util2.parse_datetime(s["timestamp"])
                elapsed = (now - ts).total_seconds() / 60.0 if ts else 0.0
            except Exception:
                elapsed = 0.0
            pre_samples.append(
                {
                    "timestamp": s["timestamp"],
                    "indoor_temp_f": s["indoor_temp_f"],
                    "outdoor_temp_f": s["outdoor_temp_f"],
                    "elapsed_minutes": -elapsed,  # negative = before session start
                }
            )

        indoor = self._get_indoor_temp()
        event = {
            "event_id": str(uuid.uuid4()),
            "created_at": now.isoformat(),
            "hvac_mode": session_mode,
            "session_mode": session_mode,
            "status": "active",
            "active_start": now.isoformat(),
            "active_end": None,
            "stabilized_at": None,
            "pre_heat_samples": pre_samples,
            "active_samples": [],
            "post_heat_samples": [],
            "start_indoor_f": indoor,
            "end_indoor_f": None,
            "peak_indoor_f": indoor,
            "start_outdoor_f": None,
            "session_minutes": None,
            "schema_version": 1,
        }
        # Grab first sample now
        first_sample = self._get_current_sample(0.0)
        event["active_samples"].append(first_sample)
        event["start_outdoor_f"] = first_sample["outdoor_temp_f"]

        self._pending_thermal_event = event
        self.learning.set_pending_thermal_event(event)
        await self.hass.async_add_executor_job(self.learning.save_state)
        _LOGGER.info(
            "Thermal event started: event_id=%s mode=%s indoor=%.1f°F",
            event["event_id"],
            session_mode,
            indoor if indoor is not None else 0.0,
        )

    def _sample_thermal_event(self) -> None:
        """Append a sample to the active or post_heat list. Called every update cycle."""
        if self._pending_thermal_event is None:
            return
        status = self._pending_thermal_event.get("status")
        if status not in ("active", "post_heat"):
            return

        # Skip sample when indoor temp is unavailable — recording 0.0 would corrupt regression
        if self._get_indoor_temp() is None:
            return

        active_start_str = self._pending_thermal_event.get("active_start")
        try:
            from homeassistant.util import dt as dt_util2

            active_start = dt_util2.parse_datetime(active_start_str) if active_start_str else dt_util.now()
        except Exception:
            active_start = dt_util.now()
        elapsed = (dt_util.now() - active_start).total_seconds() / 60.0

        sample = self._get_current_sample(elapsed)

        if status == "active":
            samples = self._pending_thermal_event["active_samples"]
            if len(samples) < THERMAL_MAX_ACTIVE_SAMPLES:
                samples.append(sample)
            # Update peak indoor
            indoor = sample["indoor_temp_f"]
            cur_peak = self._pending_thermal_event.get("peak_indoor_f")
            if indoor and (cur_peak is None or indoor > cur_peak):
                self._pending_thermal_event["peak_indoor_f"] = indoor
        else:  # post_heat
            samples = self._pending_thermal_event["post_heat_samples"]
            if len(samples) < THERMAL_MAX_POST_HEAT_SAMPLES:
                samples.append(sample)
            # Persist every 5th post-heat sample for crash safety
            if len(samples) % 5 == 0:
                self.learning.set_pending_thermal_event(self._pending_thermal_event)

    async def _end_active_phase(self) -> None:
        """Transition active → post_heat when HVAC action stops."""
        if self._pending_thermal_event is None or self._pending_thermal_event.get("status") != "active":
            return
        now = dt_util.now()
        self._pending_thermal_event["status"] = "post_heat"
        self._pending_thermal_event["active_end"] = now.isoformat()

        # Compute session_minutes
        active_start_str = self._pending_thermal_event.get("active_start")
        try:
            from homeassistant.util import dt as dt_util2

            active_start = dt_util2.parse_datetime(active_start_str) if active_start_str else now
        except Exception:
            active_start = now
        self._pending_thermal_event["session_minutes"] = (now - active_start).total_seconds() / 60.0

        self.learning.set_pending_thermal_event(self._pending_thermal_event)
        await self.hass.async_add_executor_job(self.learning.save_state)
        _LOGGER.info(
            "Thermal event active → post_heat: event_id=%s session=%.1f min",
            self._pending_thermal_event.get("event_id", "?"),
            self._pending_thermal_event["session_minutes"],
        )

    async def _check_stabilization(self) -> None:
        """Check if post-heat temperature has stabilized or timed out.

        Called every update cycle during post_heat phase.
        Commits on stabilization; abandons on timeout.
        """
        if self._pending_thermal_event is None or self._pending_thermal_event.get("status") != "post_heat":
            return

        active_end_str = self._pending_thermal_event.get("active_end")
        try:
            from homeassistant.util import dt as dt_util2

            active_end = dt_util2.parse_datetime(active_end_str) if active_end_str else dt_util.now()
        except Exception:
            active_end = dt_util.now()

        elapsed_post = (dt_util.now() - active_end).total_seconds() / 60.0

        # Timeout check
        if elapsed_post > THERMAL_POST_HEAT_TIMEOUT_MINUTES:
            await self._abandon_thermal_event("post_heat timeout exceeded")
            return

        # Stabilization check: last THERMAL_STABILIZATION_WINDOW_MINUTES of samples
        post_samples = self._pending_thermal_event.get("post_heat_samples", [])
        if len(post_samples) < THERMAL_MIN_POST_HEAT_SAMPLES:
            return

        # Collect samples from the last THERMAL_STABILIZATION_WINDOW_MINUTES
        recent = []
        now_iso = dt_util.now()
        for s in reversed(post_samples):
            try:
                sample_ts = dt_util.parse_datetime(s["timestamp"])
                if sample_ts and (now_iso - sample_ts).total_seconds() / 60.0 <= THERMAL_STABILIZATION_WINDOW_MINUTES:
                    recent.append(s)
            except Exception:
                pass
        recent.reverse()

        if len(recent) < 2:
            return

        indoor_vals = [s["indoor_temp_f"] for s in recent]
        if max(indoor_vals) - min(indoor_vals) < THERMAL_STABILIZATION_THRESHOLD_F:
            self._pending_thermal_event["status"] = "stabilized"
            self._pending_thermal_event["stabilized_at"] = dt_util.now().isoformat()
            self._pending_thermal_event["end_indoor_f"] = indoor_vals[-1]

            # Plateau guard: require at least THERMAL_MIN_DECAY_F of post-heat decay
            # before committing.  If the temp barely moved the HVAC never meaningfully
            # ran and the observation would pollute the model with a near-zero rate.
            peak_f = self._pending_thermal_event.get("peak_indoor_f")
            end_f = indoor_vals[-1]
            if peak_f is not None and (peak_f - end_f) < THERMAL_MIN_DECAY_F:
                _LOGGER.info(
                    "Thermal event plateau guard: event_id=%s peak=%.2f end=%.2f decay=%.2f < %.2f — abandoning",
                    self._pending_thermal_event.get("event_id", "?"),
                    peak_f,
                    end_f,
                    peak_f - end_f,
                    THERMAL_MIN_DECAY_F,
                )
                await self._abandon_thermal_event("plateau guard: insufficient post-heat decay")
                await self._async_save_state()
                return

            _LOGGER.info(
                "Thermal event stabilized: event_id=%s post_samples=%d",
                self._pending_thermal_event.get("event_id", "?"),
                len(post_samples),
            )
            await self._commit_thermal_event()

    async def _commit_thermal_event(self) -> None:
        """Compute physics parameters and record the thermal observation."""
        if self._pending_thermal_event is None:
            return
        if not self.config.get("learning_enabled", True):
            self._pending_thermal_event = None
            self.learning.set_pending_thermal_event(None)
            return

        obs = await self.hass.async_add_executor_job(
            self.learning._commit_event_from_dict,
            self._pending_thermal_event,
            None,
        )

        if obs and self._today_record is not None:
            self._today_record.thermal_session_count += 1

        self._pending_thermal_event = None
        self.learning.set_pending_thermal_event(None)
        await self.hass.async_add_executor_job(self.learning.save_state)

    async def _abandon_thermal_event(self, reason: str) -> None:
        """Discard the current pending thermal event."""
        if self._pending_thermal_event is not None:
            _LOGGER.info(
                "Thermal event abandoned: event_id=%s reason=%s",
                self._pending_thermal_event.get("event_id", "?"),
                reason,
            )
        self._pending_thermal_event = None
        self.learning.set_pending_thermal_event(None)
        await self.hass.async_add_executor_job(self.learning.save_state)

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

    def _emit_event(self, event_type: str, data: dict) -> None:
        """Append a timestamped event to the in-memory event log ring buffer (Issue #76)."""
        entry: dict[str, Any] = {"time": dt_util.now().isoformat(), "type": event_type, **data}
        self._event_log.append(entry)
        if len(self._event_log) > EVENT_LOG_CAP:
            self._event_log.pop(0)

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
        if self.automation_engine._override_confirm_pending:
            return "override pending (confirming...)"
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

    def _fan_is_running(self) -> bool:
        """Return True if the fan is running for any reason.

        Covers CA-activated, manual override, and untracked states so that
        chart_log entries correctly reflect fan activity even when CA's own
        _fan_active flag is False (e.g. post-heat blowdown still in progress).
        """
        return self._compute_fan_status() not in {"inactive", "disabled"}

    def _compute_fan_status(self) -> str:
        """Compute the current fan status string.

        Priority order:
        1. CA-activated fan (_fan_active=True) → "active"
        2. Manual override → "running (manual override)" / "off (manual override)"
        3. Ground-truth fallback: read thermostat fan_mode/hvac_action — catches
           post-restart state, user/Ecobee-initiated fan runs that CA didn't command.
        4. "inactive"
        """
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return "disabled"
        if ae._fan_override_active:
            return "running (manual override)" if ae._fan_active else "off (manual override)"
        if ae._fan_active:
            return "active"
        # Ground-truth fallback: CA's flag says inactive, but check what the
        # thermostat is actually doing. Catches post-restart and externally-run fan.
        if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
            climate_entity_id = self.config.get("climate_entity", "")
            cs = self.hass.states.get(climate_entity_id) if climate_entity_id else None
            if cs is not None:
                thermostat_fan_mode = cs.attributes.get("fan_mode", "")
                thermostat_hvac_action = str(cs.attributes.get("hvac_action", "")).lower()
                if thermostat_fan_mode == "on" or thermostat_hvac_action == "fan":
                    return "running (untracked)"
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

    def get_chart_data(self, range_str: str = "24h") -> dict[str, Any]:
        """Build chart data for the dashboard panel.

        Returns a dict with four series: predicted outdoor, predicted indoor,
        actual outdoor, and actual indoor temperatures over a 24-hour period,
        plus a rolling state log filtered/downsampled to the requested range.

        range_str: one of "6h", "12h", "24h", "3d", "7d", "30d", "1y"
        """
        now = dt_util.now()
        current_hour = now.hour + now.minute / 60.0

        thermal_model = self.learning.get_thermal_model() if self.learning else {}
        unit = self.config.get("temp_unit", "fahrenheit")
        _LOGGER.debug(
            "Chart data: thermal_model confidence=%s heat_obs=%d cool_obs=%d",
            thermal_model.get("confidence", "none"),
            thermal_model.get("observation_count_heat", 0),
            thermal_model.get("observation_count_cool", 0),
        )
        log_entries = self._chart_log.get_entries(range_str)
        actual_outdoor = []
        actual_indoor = []
        for _e in log_entries:
            _ts = _e.get("ts")
            if not _ts:
                continue
            # Raw/hourly buckets use "indoor"/"outdoor"; daily buckets use "indoor_avg"/"outdoor_avg"
            _indoor = _e.get("indoor") if _e.get("indoor") is not None else _e.get("indoor_avg")
            _outdoor = _e.get("outdoor") if _e.get("outdoor") is not None else _e.get("outdoor_avg")
            if _indoor is not None:
                actual_indoor.append({"time": _ts, "temp": _indoor})
            if _outdoor is not None:
                actual_outdoor.append({"time": _ts, "temp": _outdoor})

        def _conv(v: float | None) -> float | None:
            return round(from_fahrenheit(v, unit), 1) if v is not None else None

        actual_outdoor = [{"time": p["time"], "temp": _conv(p["temp"])} for p in actual_outdoor]
        actual_indoor = [{"time": p["time"], "temp": _conv(p["temp"])} for p in actual_indoor]

        predicted_indoor = [
            {"ts": p["ts"], "temp": _conv(p["temp"])}
            for p in _build_predicted_indoor_future(
                self._hourly_forecast_temps,
                self.config,
                now,
                current_indoor_temp=self._get_indoor_temp(),
                thermal_model=self.learning.get_thermal_model(),
                occupancy_mode=self._occupancy_mode,
                classification=self._current_classification,
            )
        ]

        def _conv_log_entry(e: dict) -> dict:
            e = dict(e)
            for k in ("pred_outdoor", "pred_indoor", "pred_outdoor_avg", "pred_indoor_avg"):
                if e.get(k) is not None:
                    e[k] = _conv(e[k])
            return e

        log_entries = [_conv_log_entry(e) for e in log_entries]

        forecast_outdoor = [
            {"ts": p["ts"], "temp": _conv(p["temp"])}
            for p in _build_future_forecast_outdoor(self._hourly_forecast_temps)
        ]

        # Build timestamp list for _compute_target_band_schedule — same parse pattern
        # as _build_predicted_indoor_future.
        _band_timestamps = []
        for _fc_entry in self._hourly_forecast_temps or []:
            _dt_str = _fc_entry.get("datetime") or _fc_entry.get("time")
            if not _dt_str:
                continue
            try:
                _dt_obj = datetime.fromisoformat(_dt_str)
                _band_timestamps.append(dt_util.as_local(_dt_obj) if _dt_obj.tzinfo else _dt_obj)
            except (ValueError, TypeError):
                continue

        return {
            "predicted_indoor": predicted_indoor,
            "forecast_outdoor": forecast_outdoor,
            "actual_outdoor": actual_outdoor,
            "actual_indoor": actual_indoor,
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
            "state_log": log_entries,
            "target_band": [
                {"ts": e["ts"], "lower": _conv(e["lower"]), "upper": _conv(e["upper"])}
                for e in _compute_target_band_schedule(
                    _band_timestamps,
                    self.config,
                    self._occupancy_mode,
                    now,
                    setback_modifier=(
                        getattr(self._current_classification, "setback_modifier", 0.0)
                        if self._current_classification is not None
                        else 0.0
                    ),
                    thermal_model=self.learning.get_thermal_model(),
                    classification=self._current_classification,
                )
            ],
            "unit": unit,
        }

    def get_debug_state(self) -> dict[str, Any]:
        """Return serializable debug state for the dashboard."""
        ae = self.automation_engine
        c = self._current_classification
        unit = self.config.get("temp_unit", "fahrenheit")

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
            "grace_end_time": getattr(ae, "_grace_end_time", None),
            "door_window_sensors": sensor_states,
            "pending_debounce_timers": list(self._door_open_timers.keys()),
            "classification": {
                "day_type": c.day_type if c else None,
                "trend_direction": c.trend_direction if c else None,
                "trend_magnitude": round(convert_delta(c.trend_magnitude, unit), 1) if c else None,
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
                "pre_condition_target": (
                    round(from_fahrenheit(c.pre_condition_target, unit), 1)
                    if c and c.pre_condition_target is not None
                    else None
                ),
                "setback_modifier": round(convert_delta(c.setback_modifier or 0, unit), 1) if c else None,
                "today_low": (round(from_fahrenheit(c.today_low, unit), 1) if c and c.today_low is not None else None),
                "tomorrow_low": (
                    round(from_fahrenheit(c.tomorrow_low, unit), 1) if c and c.tomorrow_low is not None else None
                ),
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
            "unit": unit,
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


def _compute_thermal_factors(chart_entries: list[dict]) -> dict:
    """Compute thermal lag and conditional differential from historical chart data.

    Returns:
        {
            "time_lag_hours": float,
            "cold_diff": float,    # indoor-outdoor when outdoor < 60°F (HVAC idle)
            "mild_diff": float,    # indoor-outdoor when outdoor 60-70°F (HVAC idle)
            "warm_diff": float,    # indoor-outdoor when outdoor > 70°F (HVAC idle)
            "has_data": bool,
        }
    """
    valid = [e for e in chart_entries if e.get("indoor") is not None and e.get("outdoor") is not None]
    if len(valid) < 20:
        return {
            "time_lag_hours": 1.0,
            "cold_diff": 15.0,
            "mild_diff": 8.0,
            "warm_diff": 0.0,
            "has_data": False,
        }

    # Time lag: cross-correlation of consecutive outdoor vs indoor changes
    outdoors = [e["outdoor"] for e in valid]
    indoors = [e["indoor"] for e in valid]
    d_out = [outdoors[i + 1] - outdoors[i] for i in range(len(outdoors) - 1)]
    d_in = [indoors[i + 1] - indoors[i] for i in range(len(indoors) - 1)]
    best_lag, best_score = 0, float("-inf")
    for lag in range(min(5, len(d_out))):
        score = sum(d_out[i] * d_in[i + lag] for i in range(len(d_out) - lag))
        if score > best_score:
            best_score, best_lag = score, lag

    # Conditional differential from HVAC-idle entries
    idle_hvac = {"", "idle", "off"}
    buckets: dict[str, list[float]] = {"cold": [], "mild": [], "warm": []}
    for e in valid:
        if str(e.get("hvac", "")).lower() not in idle_hvac:
            continue
        delta = e["indoor"] - e["outdoor"]
        outdoor = e["outdoor"]
        if outdoor < 60:
            buckets["cold"].append(delta)
        elif outdoor < 70:
            buckets["mild"].append(delta)
        else:
            buckets["warm"].append(delta)

    def _median(vals: list[float], fallback: float) -> float:
        if len(vals) < 3:
            return fallback
        s = sorted(vals)
        return s[len(s) // 2]

    return {
        "time_lag_hours": float(best_lag),
        "cold_diff": round(_median(buckets["cold"], 15.0), 1),
        "mild_diff": round(_median(buckets["mild"], 8.0), 1),
        "warm_diff": round(_median(buckets["warm"], 0.0), 1),
        "has_data": True,
    }


def _outdoor_conditional_diff(outdoor: float, thermal_factors: dict) -> float:
    """Return the learned indoor-outdoor differential for a given outdoor temp.

    Linear interpolation over ±2°F transition zones at bucket boundaries (60°F, 70°F)
    eliminates the hard 7.6°F jump that occurs when outdoor crosses a threshold.
    """
    cold = thermal_factors.get("cold_diff", 15.0)
    mild = thermal_factors.get("mild_diff", 8.0)
    warm = thermal_factors.get("warm_diff", 0.0)

    if outdoor <= 58.0:
        return cold
    elif outdoor < 62.0:
        frac = (outdoor - 58.0) / 4.0  # 0 at 58°F, 1 at 62°F
        return cold + frac * (mild - cold)
    elif outdoor <= 68.0:
        return mild
    elif outdoor < 72.0:
        frac = (outdoor - 68.0) / 4.0  # 0 at 68°F, 1 at 72°F
        return mild + frac * (warm - mild)
    else:
        return warm


def _simulate_indoor_physics(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float | None,
    dt_hours: float,
    setpoint: float | None,
    *,
    comfort_heat: float,
    comfort_cool: float,
) -> float:
    """Advance indoor temperature by dt_hours using the two-parameter ODE.

    dT/dt = k_passive * (T - T_outdoor) + Q
    Q = k_active when HVAC is driving toward setpoint, 0 otherwise.

    Q sign: for heating k_active > 0; for cooling k_active < 0.
    HVAC is considered active when:
    - setpoint >= comfort_heat and T < setpoint (heating needed)
    - setpoint <= comfort_cool and T > setpoint (cooling needed)
    """
    import math

    k_p = k_passive
    q = 0.0
    if setpoint is not None and k_active is not None:
        if setpoint >= comfort_heat and t_start < setpoint:
            q = abs(k_active)  # heating: always positive
        elif setpoint <= comfort_cool and t_start > setpoint:
            q = -abs(k_active)  # cooling: always negative

    exp_kp = math.exp(k_p * dt_hours)
    t_next = (
        t_outdoor + (t_start - t_outdoor) * exp_kp + (q / k_p) * (exp_kp - 1) if k_p != 0 else t_start + q * dt_hours
    )

    # Clamp: heating won't overshoot setpoint; cooling won't undershoot
    if setpoint is not None:
        if q > 0:
            t_next = min(t_next, setpoint)
        elif q < 0:
            t_next = max(t_next, setpoint)
    return t_next


def _compute_target_band_schedule(
    hourly_timestamps: list,
    config: dict,
    occupancy_mode: str,
    now: Any,
    setback_modifier: float = 0.0,
    thermal_model: dict | None = None,
    classification: Any | None = None,
) -> list[dict]:
    """Compute the dynamic target band (lower/upper) for each hourly timestamp.

    Returns a list of dicts: [{"ts": ISO_str, "lower": float, "upper": float}].

    Logic per timestamp:
    - Away today: flat setback band (shifted by setback_modifier).
    - Vacation (any day): deep setback band (setback ± VACATION_SETBACK_EXTRA + modifier).
    - Home/guest or future days when away: wake/sleep schedule with ramps.
      Wake ramp: 2h linear interpolation from sleep setback → comfort band.
      Sleep ramp: 1h linear interpolation from comfort band → sleep setback.

    Night-owl schedules (sleep_time < wake_time across midnight) are handled by
    normalising sleep_h += 24 and h += 24 when h < wake_h, keeping comparisons
    in chronological order.

    When thermal_model and classification are both provided, sleep_heat is derived
    via compute_bedtime_setback() — matching automation.py's adaptive setpoint logic.
    """
    comfort_heat = float(config.get("comfort_heat", 70))
    comfort_cool = float(config.get("comfort_cool", 75))
    setback_heat = float(config.get("setback_heat", 60))
    setback_cool = float(config.get("setback_cool", 80))
    sleep_heat = float(config.get("sleep_heat", comfort_heat - 4.0))
    sleep_cool = float(config.get("sleep_cool", comfort_cool + 3.0))

    # G1/G2: use compute_bedtime_setback() when thermal model + classification available —
    # aligns chart band with the adaptive sleep setpoint used by automation.py for both
    # heat (sleep_heat raised toward comfort) and cool (sleep_cool lowered toward comfort).
    if thermal_model is not None and classification is not None:
        _hvac_mode = getattr(classification, "hvac_mode", None)
        if _hvac_mode == "heat":
            sleep_heat = compute_bedtime_setback(config, thermal_model, classification)
        elif _hvac_mode == "cool":
            sleep_cool = compute_bedtime_setback(config, thermal_model, classification)

    # I3: apply setback_modifier to setback bounds (mirrors automation.py behaviour)
    setback_heat_eff = setback_heat + setback_modifier
    setback_cool_eff = setback_cool + setback_modifier

    wake_time = _parse_time(config.get("wake_time", "06:30"))
    sleep_time_cfg = _parse_time(config.get("sleep_time", "22:30"))
    wake_h = wake_time.hour + wake_time.minute / 60.0
    sleep_h = sleep_time_cfg.hour + sleep_time_cfg.minute / 60.0
    wake_ramp_h = 2.0
    sleep_ramp_h = 1.0

    # I6: midnight wraparound — night-owl schedules where sleep < wake (e.g. sleep=01:00, wake=09:00)
    night_owl = wake_h > sleep_h
    if night_owl:
        sleep_h += 24  # normalise to a > wake_h value (e.g. 1 → 25)

    now_date = now.date() if hasattr(now, "date") else None

    result = []
    for ts in hourly_timestamps:
        if ts is None:
            continue
        ts_date = ts.date() if hasattr(ts, "date") else None

        # I5: vacation applies setback to ALL days (not just today); away only applies to today
        if occupancy_mode == OCCUPANCY_VACATION:
            lower = setback_heat_eff - VACATION_SETBACK_EXTRA
            upper = setback_cool_eff + VACATION_SETBACK_EXTRA
        elif occupancy_mode == OCCUPANCY_AWAY and ts_date == now_date:
            lower = setback_heat_eff
            upper = setback_cool_eff
        else:
            # Home/guest schedule (or future days when away)
            h = ts.hour + ts.minute / 60.0
            # I6: normalise h for night-owl schedules
            h_n = h + 24 if (night_owl and h < wake_h) else h

            if h_n < wake_h:
                # Pre-wake: sleep band
                lower = sleep_heat
                upper = sleep_cool
            elif h_n < wake_h + wake_ramp_h:
                # Wake ramp: interpolate toward comfort
                frac = (h_n - wake_h) / wake_ramp_h
                lower = sleep_heat + frac * (comfort_heat - sleep_heat)
                upper = sleep_cool + frac * (comfort_cool - sleep_cool)
            elif h_n < sleep_h:
                # Awake: comfort band
                lower = comfort_heat
                upper = comfort_cool
            elif h_n < sleep_h + sleep_ramp_h:
                # Sleep ramp: interpolate toward sleep setback
                frac = (h_n - sleep_h) / sleep_ramp_h
                lower = comfort_heat + frac * (sleep_heat - comfort_heat)
                upper = comfort_cool + frac * (sleep_cool - comfort_cool)
            else:
                # Post-sleep: sleep band
                lower = sleep_heat
                upper = sleep_cool

        result.append({"ts": ts.isoformat(), "lower": round(lower, 1), "upper": round(upper, 1)})

    return result


def _build_predicted_indoor_future(
    hourly_forecast: list[dict] | None,
    config: dict[str, Any],
    now: Any,
    current_indoor_temp: float | None = None,
    thermal_model: dict | None = None,
    occupancy_mode: str = OCCUPANCY_HOME,
    classification: Any | None = None,
) -> list[dict]:
    """Build future predicted indoor temps from the automation plan.

    When thermal_model has "low" confidence or above, uses the physics ODE:
      T(t+dt) = T_outdoor + (T - T_outdoor)*exp(k_p*dt) + (Q/k_p)*(exp(k_p*dt) - 1)
    Otherwise falls back to the setpoint-schedule approach (mirrors automation plan).

    Fallback (setpoint-schedule):
    - heat days: sleep_heat (or comfort_heat−4°F default) overnight, comfort_heat waking
    - cool days: sleep_cool (or comfort_cool+3°F default) overnight, comfort_cool waking
    - off days: outdoor + 2°F buffer, floored at setback_heat

    Returns list of {"ts": ISO_str, "temp": float} for hours strictly after now.
    """
    if not hourly_forecast:
        _LOGGER.debug("_build_predicted_indoor_future: no hourly_forecast — returning empty")
        return []

    _LOGGER.debug(
        "_build_predicted_indoor_future: %d forecast entries, now=%s",
        len(hourly_forecast),
        now.isoformat() if hasattr(now, "isoformat") else now,
    )

    comfort_heat = float(config.get("comfort_heat", 70))
    comfort_cool = float(config.get("comfort_cool", 75))
    setback_heat = float(config.get("setback_heat", 60))  # absolute floor for heat
    setback_cool = float(config.get("setback_cool", 80))  # absolute ceiling for cool

    # Mirror automation engine (automation.py compute_setback_temp) and
    # compute_predicted_temps (coordinator.py ~line 2678) — use sleep_heat/sleep_cool if
    # configured; otherwise default to comfort ± DEFAULT_SETBACK_DEPTH_*F.
    # setback_heat/setback_cool remain as hard floor/ceiling guards.
    setback_temp_heat = float(config.get("sleep_heat", comfort_heat - DEFAULT_SETBACK_DEPTH_F))
    setback_temp_heat = max(setback_temp_heat, setback_heat)
    setback_temp_cool = float(config.get("sleep_cool", comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F))
    setback_temp_cool = min(setback_temp_cool, setback_cool)

    # --- Classify each future day by forecast high ---
    day_highs: dict = {}
    parse_errors = 0
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        if not dt_str:
            parse_errors += 1
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            local_ts = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
            temp = entry.get("temperature")
            if temp is not None:
                day_highs.setdefault(local_ts.date(), []).append(float(temp))
        except (ValueError, TypeError) as exc:
            parse_errors += 1
            _LOGGER.debug("_build_predicted_indoor_future: skipping %r — %s", dt_str, exc)

    if parse_errors:
        _LOGGER.warning(
            "_build_predicted_indoor_future: %d entries failed to parse",
            parse_errors,
        )
    if not day_highs:
        _LOGGER.warning(
            "_build_predicted_indoor_future: no valid entries in %d-entry forecast — "
            "predicted indoor will be empty. First entry: %r",
            len(hourly_forecast),
            hourly_forecast[0] if hourly_forecast else None,
        )
        return []

    def _day_mode(temps: list[float]) -> str:
        high = max(temps)
        if high >= THRESHOLD_HOT:
            return "cool"
        if high >= THRESHOLD_WARM or high >= THRESHOLD_MILD:
            return "off"
        return "heat"

    day_modes = {d: _day_mode(t) for d, t in day_highs.items()}
    _LOGGER.debug(
        "_build_predicted_indoor_future: %d days classified: %s",
        len(day_modes),
        {str(d): m for d, m in sorted(day_modes.items())},
    )

    # Decide whether to use physics simulation or setpoint-schedule fallback.
    # Physics requires: thermal_model with confidence != "none", k_passive, and a seed temp.
    _use_physics = False
    _k_passive: float | None = None
    _k_active_heat: float | None = None
    _k_active_cool: float | None = None
    if thermal_model and current_indoor_temp is not None:
        _conf = thermal_model.get("confidence", "none")
        _k_passive = thermal_model.get("k_passive")
        _k_active_heat = thermal_model.get("k_active_heat")
        _k_active_cool = thermal_model.get("k_active_cool")
        if _conf != "none" and _k_passive is not None and _k_passive < 0:
            _use_physics = True
            _LOGGER.debug(
                "_build_predicted_indoor_future: using physics model "
                "(conf=%s k_passive=%.4f k_active_heat=%s k_active_cool=%s)",
                _conf,
                _k_passive,
                f"{_k_active_heat:.2f}" if _k_active_heat is not None else "None",
                f"{_k_active_cool:.2f}" if _k_active_cool is not None else "None",
            )
        else:
            _LOGGER.debug(
                "_build_predicted_indoor_future: using fallback ramp (conf=%s k_passive=%s indoor=%s)",
                _conf,
                f"{_k_passive:.4f}" if _k_passive is not None else "None",
                f"{current_indoor_temp:.1f}" if current_indoor_temp is not None else "None",
            )
    elif not _use_physics:
        _LOGGER.debug("_build_predicted_indoor_future: using fallback ramp (no model or no indoor temp)")

    # B3: Pre-compute the full band schedule for all future timestamps in one call,
    # then look up per entry. Avoids re-parsing config + ramp math 24+ times.
    _band_config = dict(config)
    _band_config["sleep_heat"] = setback_temp_heat
    _band_config["sleep_cool"] = setback_temp_cool
    _future_timestamps_for_band: list = []
    for _fc in hourly_forecast:
        _dt_s = _fc.get("datetime") or _fc.get("time")
        if not _dt_s:
            continue
        try:
            _dt_o = datetime.fromisoformat(_dt_s)
            _lts = dt_util.as_local(_dt_o) if _dt_o.tzinfo else _dt_o
            if _lts > now:
                _future_timestamps_for_band.append(_lts)
        except (ValueError, TypeError):
            pass
    _band_schedule = _compute_target_band_schedule(
        _future_timestamps_for_band,
        _band_config,
        occupancy_mode,
        now,
        thermal_model=thermal_model,
        classification=classification,
    )
    _band_lookup: dict[str, dict] = {b["ts"]: b for b in _band_schedule}

    result = []
    skipped_past = 0
    _t_current = current_indoor_temp  # running indoor temp for physics simulation
    _prev_ts = now  # previous timestamp for dt calculation

    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        if not dt_str:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            local_ts = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
        except (ValueError, TypeError):
            continue
        if local_ts <= now:
            skipped_past += 1
            continue
        outdoor = entry.get("temperature")
        mode = day_modes.get(local_ts.date(), "off")

        # Look up pre-computed band entry for this timestamp
        _band = _band_lookup.get(local_ts.isoformat(), {"lower": comfort_heat, "upper": comfort_cool})

        if _use_physics and _t_current is not None and outdoor is not None:
            if mode == "heat":
                setpoint = _band["lower"]
                k_active_for_mode = _k_active_heat
            elif mode == "cool":
                setpoint = _band["upper"]
                k_active_for_mode = _k_active_cool
            else:
                setpoint = None  # HVAC off — pure passive decay
                k_active_for_mode = None

            # Time step in hours between consecutive entries
            try:
                dt_hours = (local_ts - _prev_ts).total_seconds() / 3600.0
            except Exception:
                dt_hours = 1.0
            dt_hours = max(dt_hours, 1 / 60.0)  # floor at 1 min

            _t_current = _simulate_indoor_physics(
                _t_current,
                float(outdoor),
                _k_passive,  # type: ignore[arg-type]
                k_active_for_mode,
                dt_hours,
                setpoint,
                comfort_heat=comfort_heat,
                comfort_cool=comfort_cool,
            )
            temp = _t_current
        else:
            # Setpoint-schedule fallback
            if mode == "heat":
                temp = _band["lower"]
            elif mode == "cool":
                temp = _band["upper"]
            else:
                temp = max(setback_heat, float(outdoor) + 2.0) if outdoor is not None else comfort_heat

        _prev_ts = local_ts
        result.append({"ts": local_ts.isoformat(), "temp": round(temp, 1)})

    _LOGGER.debug(
        "_build_predicted_indoor_future: %d past skipped, %d future returned",
        skipped_past,
        len(result),
    )
    if not result:
        _LOGGER.warning(
            "_build_predicted_indoor_future: zero future entries (now=%s, forecast %r → %r)",
            now.isoformat() if hasattr(now, "isoformat") else now,
            ((hourly_forecast[0].get("datetime") or hourly_forecast[0].get("time")) if hourly_forecast else None),
            ((hourly_forecast[-1].get("datetime") or hourly_forecast[-1].get("time")) if hourly_forecast else None),
        )
    return result


def compute_predicted_temps(
    classification: DayClassification | None,
    config: dict[str, Any],
    hourly_forecast: list[dict] | None = None,
    thermal_model: dict | None = None,
    thermal_factors: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Compute predicted outdoor and indoor hourly temperatures.

    This is a standalone function so it can be tested without a coordinator.

    Uses a unified equilibrium model:
    - For hvac_mode="off" (mild/warm days): indoor = max(HVAC_floor, outdoor_lagged + cond_diff)
    - For hvac_mode="heat" or "cool": follow HVAC schedule (setback→comfort ramps),
      equilibrium adjusts drift at edges.

    Returns:
        (predicted_outdoor, predicted_indoor) — each a list of 24 dicts
        with 'hour' and 'temp' keys, or empty lists if no classification.
    """
    if not classification:
        return [], []

    c = classification

    # --- Predicted outdoor temps ---
    predicted_outdoor = _build_outdoor_curve(high=c.today_high, low=c.today_low, hourly_forecast=hourly_forecast)

    # --- Thermal factors ---
    _tf = thermal_factors or {}
    _lag_h = max(0, int(round(_tf.get("time_lag_hours", 1.0))))

    # --- HVAC floor and ceiling for this day type ---
    if c.hvac_mode == "heat":
        hvac_floor = config.get("comfort_heat", 70)
        hvac_ceiling = None
    elif c.hvac_mode == "cool":
        hvac_floor = config.get("setback_cool", 80) + c.setback_modifier
        hvac_ceiling = config.get("comfort_cool", 75)
    else:  # "off" / mild
        hvac_floor = config.get("setback_heat", 60) + c.setback_modifier
        hvac_ceiling = None

    # --- Schedule timing (for heat/cool days where HVAC actively ramps) ---
    comfort = config.get("comfort_heat", 70) if c.hvac_mode != "cool" else config.get("comfort_cool", 75)
    if c.hvac_mode == "heat":
        setback = config.get("setback_heat", 60) + c.setback_modifier
    elif c.hvac_mode == "cool":
        setback = config.get("setback_cool", 80) + c.setback_modifier
    else:
        setback = hvac_floor

    wake = _parse_time(config.get("wake_time", "06:30"))
    sleep = _parse_time(config.get("sleep_time", "22:30"))
    wake_h = wake.hour + wake.minute / 60.0
    sleep_h = sleep.hour + sleep.minute / 60.0

    if c.hvac_mode == "heat":
        _sleep_h = config.get("sleep_heat", comfort - DEFAULT_SETBACK_DEPTH_F)
        bedtime_setback = _sleep_h + c.setback_modifier
    elif c.hvac_mode == "cool":
        # Cool mode: setback_modifier is not applied to bedtime (original behavior preserved)
        _sleep_c = config.get("sleep_cool", comfort + DEFAULT_SETBACK_DEPTH_COOL_F)
        bedtime_setback = _sleep_c
    else:
        bedtime_setback = comfort  # off-mode: unused in schedule loop
    ramp_h_morning = _compute_ramp_hours(abs(comfort - setback), c.hvac_mode, thermal_model)
    ramp_h_evening = _compute_ramp_hours(abs(comfort - bedtime_setback), c.hvac_mode, thermal_model)

    # Running indoor state for exponential smoothing (off-day only).
    # Seed with hour-0 equilibrium so the first step uses a physical starting point.
    if predicted_outdoor and c.hvac_mode not in ("heat", "cool"):
        _out0 = predicted_outdoor[0]["temp"]
        _cd0 = _outdoor_conditional_diff(_out0, _tf)
        _prev_indoor = max(hvac_floor, _out0 + _cd0)
    else:
        _prev_indoor = comfort

    predicted_indoor: list[dict] = []
    for h in range(24):
        if predicted_outdoor:
            lag_idx = max(0, h - _lag_h)
            out_t = predicted_outdoor[lag_idx]["temp"]
            cond_diff = _outdoor_conditional_diff(out_t, _tf)
            equilibrium = out_t + cond_diff
        else:
            equilibrium = comfort

        if c.hvac_mode in ("heat", "cool"):
            # HVAC actively holds setpoints: follow schedule
            if h < wake_h:
                temp = setback
            elif h < wake_h + ramp_h_morning:
                frac = (h - wake_h) / ramp_h_morning
                temp = setback + frac * (comfort - setback)
            elif h < sleep_h:
                temp = comfort
            elif h < sleep_h + ramp_h_evening:
                frac = (h - sleep_h) / ramp_h_evening
                temp = comfort + frac * (bedtime_setback - comfort)
            else:
                temp = bedtime_setback
        else:
            # hvac_mode == "off": CA manages floor (heater), no active cooling ceiling.
            # Exponential smoothing: alpha=1/lag_h so lag controls convergence speed,
            # not an index offset. For lag=1 (alpha=1.0) this is identical to instantaneous.
            _alpha = 1.0 / max(1, _lag_h)
            raw = _prev_indoor + _alpha * (equilibrium - _prev_indoor)
            temp = max(hvac_floor, raw)

        _prev_indoor = temp  # track for exponential smoothing

        # Apply ceiling for cool days during waking hours
        if hvac_ceiling is not None and wake_h <= h < sleep_h:
            temp = min(temp, hvac_ceiling)

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


def _build_future_forecast_outdoor(
    hourly_forecast: list[dict] | None,
) -> list[dict]:
    """Extract future hourly outdoor temps from the weather forecast.

    Returns all entries at or after now as {"ts": ISO_string, "temp": float}.
    Covers all available forecast days (2-10+), not just today.
    Unlike _build_outdoor_curve, values are NOT normalised to daily high/low —
    the raw forecast temperatures are used directly.
    """
    if not hourly_forecast:
        return []
    now = dt_util.now()
    result = []
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            local_dt = dt_util.as_local(dt_obj) if dt_obj.tzinfo else dt_obj
            if local_dt < now:
                continue
            result.append({"ts": local_dt.isoformat(), "temp": round(float(temp), 1)})
        except (ValueError, TypeError):
            continue
    result.sort(key=lambda x: x["ts"])
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
