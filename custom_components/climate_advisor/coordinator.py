"""Data coordinator for Climate Advisor.

The coordinator is the central brain. It runs on a schedule, pulls forecast
data, classifies the day, triggers automations, sends briefings, and feeds
data to the learning engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ai_skills import AISkillRegistry
    from .claude_api import ClaudeAPIClient

from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .automation import AutomationEngine, _in_sleep_window, compute_bedtime_setback
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
    ATTR_HVAC_FAN_STATUS,
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
    ATTR_WHF_STATUS,
    CHART_LOG_MAX_DAYS,
    CONF_AI_API_KEY,
    CONF_AI_ENABLED,
    CONF_AI_INVESTIGATOR_ENABLED,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_FAN_STATE_ENTITY,
    CONF_FAN_STATE_FEEDBACK,
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_NAT_VENT_HYSTERESIS_F,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    CONF_SLEEP_HEAT,
    CONF_THRESHOLD_COOL,
    CONF_THRESHOLD_HOT,
    CONF_THRESHOLD_MILD,
    CONF_THRESHOLD_WARM,
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
    DEFAULT_THRESHOLD_COOL,
    DEFAULT_THRESHOLD_HOT,
    DEFAULT_THRESHOLD_MILD,
    DEFAULT_THRESHOLD_WARM,
    DOMAIN,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_TEMP_DELTA,
    EVENT_LOG_CAP,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    INVESTIGATION_REPORT_HISTORY_CAP,
    INVESTIGATION_REPORTS_FILE,
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_WEATHER_BIAS_APPLY_F,
    NAT_VENT_HYSTERESIS_F,
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENTILATED_DECAY,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_SETBACK_MINUTES,
    OCCUPANCY_VACATION,
    PRED_ARCHIVE_HORIZON_HOURS,
    REJECT_ABANDONED,
    REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY,
    REJECT_AC_NO_COOL_SETPOINTS,
    REJECT_AC_NO_SETPOINT_BREACH,
    REJECT_AC_SETPOINT_OUT_OF_RANGE,
    REJECT_AC_SETPOINT_UNSTABLE,
    REJECT_NO_INTERIOR_PEAK,
    REJECT_OLS_BAD_FIT,
    REJECT_OLS_BOUNDS,
    REJECT_OLS_WRONG_SIGN,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_BLOCKS,
    REJECT_TOO_FEW_SAMPLES,
    REJECT_WINDOW_TOO_SHORT,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
    THERMAL_BUCKET_INTERP_HALF_F,
    THERMAL_CHART_LOG_PASSIVE_MIN_DT_F,
    THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES,
    THERMAL_CHART_LOG_VENT_MIN_MINUTES,
    THERMAL_COLD_BUCKET_LIMIT_F,
    THERMAL_DUAL_AGREE_REL,
    THERMAL_DUAL_OLS_GOOD,
    THERMAL_DUAL_OLS_OK,
    THERMAL_FAN_MIN_SAMPLES,
    THERMAL_FAN_MIN_SIGNAL_F,
    THERMAL_FAN_SAMPLE_INTERVAL_S,
    THERMAL_HVAC_MIN_DECAY_F,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
    THERMAL_MAX_ACTIVE_SAMPLES,
    THERMAL_MAX_OBS_SAMPLES,
    THERMAL_MAX_POST_HEAT_SAMPLES,
    THERMAL_MILD_BUCKET_LIMIT_F,
    THERMAL_MIN_DECAY_SAMPLES,
    THERMAL_MIN_POST_HEAT_SAMPLES,
    THERMAL_MIN_R_SQUARED,
    THERMAL_PASSIVE_MIN_DELTA_F,
    THERMAL_PASSIVE_MIN_SAMPLES,
    THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
    THERMAL_POST_HEAT_TIMEOUT_MINUTES,
    THERMAL_ROLLING_MAX_WINDOW_MINUTES,
    THERMAL_ROLLING_MIN_DELTA_T_F,
    THERMAL_ROLLING_MIN_WINDOW_MINUTES,
    THERMAL_SOLAR_DAYTIME_END_H,
    THERMAL_SOLAR_DAYTIME_START_H,
    THERMAL_SOLAR_FACTOR_MIN_RANGE,
    THERMAL_SOLAR_MIN_RATE_F_PER_HR,
    THERMAL_SOLAR_MIN_SAMPLES,
    THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES,
    THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H,
    THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F,
    THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F,
    THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H,
    THERMAL_SOLAR_PHASE_ALPHA,
    THERMAL_SOLAR_PHASE_MIN_DT_F,
    THERMAL_SOLAR_PHASE_MIN_ENTRIES,
    THERMAL_SOLAR_PHASE_MIN_WINDOW_H,
    THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT,
    THERMAL_SOLAR_PHASE_OFFSET_MAX,
    THERMAL_SOLAR_PHASE_OFFSET_MIN,
    THERMAL_SOLAR_SAMPLE_INTERVAL_S,
    THERMAL_VENT_MIN_SAMPLES,
    THERMAL_VENT_MIN_SIGNAL_F,
    THERMAL_VENTILATED_MIN_DELTA_F,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    VACATION_SETBACK_EXTRA,
    VERSION,
)
from .learning import DailyRecord, LearningEngine, compute_k_passive_blocks
from .state import StatePersistence
from .temperature import convert_delta, format_temp, from_fahrenheit, to_fahrenheit

_LOGGER = logging.getLogger(__name__)

# Degrees below comfort_heat at which outdoor temp is too cold to recommend opening windows.
# With default comfort_heat=70°F this means outdoor must be ≥ 55°F for windows to be recommended.
_WINDOWS_EXTREME_COLD_MARGIN = 15.0

# Maximum rejection events retained per obs_type in the in-memory rejection log.
# Matches the per-obs-type cap enforced by LearningState.rejection_log on load.
_REJECTION_LOG_CAP: int = 100

# Plausible indoor temperature range in Fahrenheit.  Values outside this band indicate
# a sensor glitch (e.g. a thermostat echoing its new setpoint into current_temperature
# during a setpoint-only transition) and are treated as unavailable rather than
# propagated into the chart log.
_MIN_PLAUSIBLE_INDOOR_F: float = 40.0
_MAX_PLAUSIBLE_INDOOR_F: float = 110.0


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
        # Restart-cause diagnostics (Issue #403): set True when a homeassistant.restart/stop
        # service call is observed before shutdown, so async_shutdown() can distinguish a
        # user-initiated restart from a crash.
        self._user_initiated_shutdown = False

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
        self.automation_engine._request_refresh_callback = lambda: self.hass.async_create_task(
            self.async_request_refresh()
        )
        # Issue #359: post-grace fan check callback — called by engine when any grace period expires
        self.automation_engine._post_grace_fan_check_callback = self._on_post_grace_fan_check
        _LOGGER.debug(
            "Climate Advisor startup: temp_unit=%s, comfort_heat=%.1f, comfort_cool=%.1f",
            config.get("temp_unit", "fahrenheit"),
            config.get("comfort_heat", 0),
            config.get("comfort_cool", 0),
        )

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
        self._door_open_timer_expiry: dict[str, str] = {}

        # Overnight pre-cool phase (Issue #258): scheduled once per warming-trend day
        self._pre_cool_trigger_scheduled: bool = False
        self._pre_cool_trigger_cancel: Any | None = None
        self._pre_cool_status: str | None = None  # surfaced in status API
        self._pre_cool_trigger_dt: datetime | None = None  # full tz-aware trigger datetime
        self._pre_cool_target: float | None = None  # pre-cool ceiling target temp

        # Startup coalescing (Issue #321): suppress override detection for 5 min after restart
        self._startup_coalesce_active: bool = True
        self._startup_timer_fired: bool = False
        self._startup_coalesce_expiry: str | None = None

        # Startup retry state — gentle backoff when weather entity isn't ready
        self._startup_retries_remaining: int = 5
        self._startup_retry_delay: int = 30  # seconds; doubles each attempt

        # Temperature history for dashboard chart (cleared at end of day)
        self._outdoor_temp_history: list[tuple[str, float]] = []
        self._indoor_temp_history: list[tuple[str, float]] = []
        self._hourly_forecast_temps: list[dict] = []
        self._last_predicted_indoor: list[dict] = []
        self._pred_archive: dict[int, float] = {}
        self._thermal_factors: dict | None = None

        # Observe-only mode: when disabled, automation still runs but skips actions
        self._automation_enabled: bool = True

        # HVAC runtime tracking
        self._hvac_on_since: datetime | None = None
        self._last_outdoor_temp: float | None = None  # most recent outdoor reading for gate checks
        # Issue #130 D16: fallback outdoor temp when weather entity is temporarily unavailable
        self._last_known_outdoor_f: float | None = None
        self._last_known_outdoor_ts: datetime | None = None
        # Thermal observation pipeline (Issue #114)
        self._pending_thermal_event: dict | None = None
        self._pending_observations: dict = {}  # keyed by obs_type string
        self._rejection_log: dict[str, list[dict]] = {}  # keyed by obs_type; capped at _REJECTION_LOG_CAP
        self._pre_heat_sample_buffer: list[dict] = []  # rolling pre-heat window, max 15
        self._startup_hvac_initialized: bool = False  # Issue #96: prevents repeated late-start init
        self._last_state_contradiction_time: datetime | None = None  # dedup for state_contradiction_warning events
        self._untracked_fan_active: bool = False  # Issue #331 follow-up: entry/exit dedup for fan_running_untracked
        self._fan_state_entity_unavailable_warned: bool = False  # Issue #359: WHF Type 2 fallback warning dedup
        self._last_commanded_fan_state: bool | None = None  # Issue #361: command-only mode — last on/off commanded
        self._last_violation_check: datetime | None = None
        # Chart_log endpoint estimator backfill flags (Issue #137)
        self._passive_k_backfilled: bool = False  # True after chart_log passive windows processed
        self._vent_k_backfilled: bool = False  # True after chart_log overnight ventilated windows processed
        # Dual-estimator backfill flags (v2): runs block-OLS alongside endpoint estimator
        self._passive_k_backfill_v2: bool = False
        self._vent_k_backfill_v2: bool = False
        # Solar phase offset (Issue #147)
        self._solar_phase_offset: float = THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
        self._solar_phase_backfill: bool = False
        self._solar_phase_ac_backfill: bool = False  # Issue #312: AC duty cycle estimator
        # Periodic daily re-fit tracker (Issue #310): date of last incremental fit
        self._last_solar_phase_fit_date: date | None = None

        # GitHub issues cache — separate TTL for open vs closed issues
        self._github_open_cache: list[dict] | None = None
        self._github_open_cache_ts: float = 0.0
        self._github_closed_cache: list[dict] | None = None
        self._github_closed_cache_ts: float = 0.0

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

        # Schedule: thermal observation sampler (5-min independent of 30-min update cycle)
        # Decay obs need ~6 samples per 30-min rolling window; the coordinator cycle alone
        # yields only 1 sample per window, which is below the OLS floor.
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_thermal_sample_tick,
                timedelta(minutes=5),
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

        # Listeners: fan state entity (Issue #359: WHF Type 2 dual-entity support)
        # When a separate physical-state entity is configured and differs from the command entity,
        # register an additional listener so physical on/off transitions are detected.
        _fan_state_entity = self.config.get(CONF_FAN_STATE_ENTITY)
        if _fan_state_entity and _fan_state_entity != fan_entity:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass,
                    _fan_state_entity,
                    self._async_fan_entity_changed,
                )
            )

        # Issue #361: log fan control mode (state-feedback vs command-only) at startup
        _fan_mode_cfg = self.config.get(CONF_FAN_MODE, "")
        if _fan_mode_cfg not in ("", "none", None, FAN_MODE_DISABLED):
            _feedback_mode = "state-feedback" if self._fan_state_feedback_enabled() else "command-only"
            _LOGGER.info(
                "Fan control mode: %s (fan_entity=%s, fan_state_entity=%s, fan_state_feedback=%s)",
                _feedback_mode,
                self.config.get(CONF_FAN_ENTITY, ""),
                self.config.get(CONF_FAN_STATE_ENTITY, ""),
                self._fan_state_feedback_enabled(),
            )

        # Listeners: indoor and outdoor temp entities — re-evaluate fan on every temp change (Issue #327).
        # Indoor temp: only register a dedicated listener when a separate sensor entity is configured;
        # when indoor comes from the thermostat's current_temperature attribute the existing
        # _async_thermostat_changed dispatch (below) already fires on attribute changes.
        _indoor_temp_source = self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK)
        _indoor_temp_entity = (
            self.config.get("indoor_temp_entity")
            if _indoor_temp_source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
            else None
        )
        if _indoor_temp_entity:

            @callback
            def _async_indoor_temp_changed(event: Any) -> None:
                ae = self.automation_engine
                if ae._fan_active or ae._natural_vent_active:
                    self.hass.async_create_task(
                        ae.fan_thermostat_check(
                            indoor=self._get_indoor_temp(),
                            outdoor=self._last_outdoor_temp,
                            trigger="indoor",
                        )
                    )

            self._unsub_listeners.append(
                async_track_state_change_event(self.hass, _indoor_temp_entity, _async_indoor_temp_changed)
            )

        # Outdoor temp: register a listener on the configured outdoor sensor entity (Issue #327).
        # The thermostat listener does NOT carry outdoor temp, so outdoor sensor changes are invisible
        # until the 30-min cycle without this listener.
        _outdoor_temp_source = self.config.get("outdoor_temp_source", TEMP_SOURCE_WEATHER_SERVICE)
        _outdoor_temp_entity = (
            self.config.get("outdoor_temp_entity")
            if _outdoor_temp_source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
            else None
        )
        if _outdoor_temp_entity:

            @callback
            def _async_outdoor_temp_changed(event: Any) -> None:
                ae = self.automation_engine
                if ae._fan_active or ae._natural_vent_active:
                    new_state = event.data.get("new_state")
                    if new_state is not None:
                        try:
                            unit = self.config.get("temp_unit", "fahrenheit")
                            new_outdoor = to_fahrenheit(float(new_state.state), unit)
                            self._last_outdoor_temp = new_outdoor
                        except (ValueError, TypeError):
                            pass
                    self.hass.async_create_task(
                        ae.fan_thermostat_check(
                            indoor=self._get_indoor_temp(),
                            outdoor=self._last_outdoor_temp,
                            trigger="outdoor",
                        )
                    )

            self._unsub_listeners.append(
                async_track_state_change_event(self.hass, _outdoor_temp_entity, _async_outdoor_temp_changed)
            )

        _LOGGER.info(
            "Fan control: watching indoor=%s outdoor=%s thermostat=%s for thermostatic re-eval",
            _indoor_temp_entity or "(thermostat attr)",
            _outdoor_temp_entity or "(weather service / 30-min poll)",
            self.config["climate_entity"],
        )

        # Startup coalescing: suppress override detection for 5 minutes, then evaluate state (Issue #321)
        _coalesce_expiry = dt_util.now() + timedelta(seconds=300)
        self._startup_coalesce_expiry = _coalesce_expiry.isoformat()

        @callback
        def _on_startup_coalesce_timer(_now: Any) -> None:
            self._startup_timer_fired = True
            self.hass.async_create_task(self.async_request_refresh())

        async_call_later(self.hass, 300, _on_startup_coalesce_timer)
        _LOGGER.info(
            "Startup coalescing window started — override detection suppressed for 300s, coalescing at %s",
            _coalesce_expiry.strftime("%H:%M:%S"),
        )

        # Start minimum fan runtime rolling cycle (Issue #77) — not clock-aligned
        await self.automation_engine.start_min_fan_runtime_cycles()

        # Listener: detect user-initiated HA restart/stop (Issue #403) — best-effort restart
        # cause diagnostics. Distinguishes a user pressing "Restart Home Assistant" from a
        # crash so async_restore_state() can classify the boundary correctly.
        @callback
        def _async_call_service_event(event: Event) -> None:
            if event.data.get("domain") == "homeassistant" and event.data.get("service") in (
                "restart",
                "stop",
            ):
                self._user_initiated_shutdown = True

        self._unsub_listeners.append(self.hass.bus.async_listen(EVENT_CALL_SERVICE, _async_call_service_event))

        # Listener: persist restart-cause diagnostics on a real HA restart (Issue #413).
        # EVENT_HOMEASSISTANT_STOP fires on homeassistant.restart/stop and on HAOS/deploy
        # restarts, but async_unload_entry() (which calls async_shutdown()) does NOT — HA
        # only unloads config entries on entry removal/reload, not on a full restart. Without
        # this listener, clean_shutdown/last_shutdown_version/user_initiated_restart were only
        # ever written on the rare entry-unload path, so the restart-cause classifier in
        # async_restore_state() fell through to "unknown" on every real-world restart.
        @callback
        def _async_homeassistant_stop(_event: Event) -> None:
            self.hass.async_create_task(self._persist_shutdown_diagnostics())

        self._unsub_listeners.append(self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_homeassistant_stop))

        _LOGGER.info("Climate Advisor v%s coordinator setup complete", VERSION)

    @callback
    def _async_thermal_sample_tick(self, now: datetime) -> None:
        """Sample active thermal observations on the 5-min tick."""
        self._sample_all_observations()

    async def async_restore_state(self) -> None:
        """Restore operational state from disk after startup."""
        _LOGGER.info("Climate Advisor v%s starting up", VERSION)
        await self.hass.async_add_executor_job(self.learning.load_state)
        # Restore rejection_log from LearningState (load_state() already validated and capped it)
        loaded_rl = self.learning._state.rejection_log
        if isinstance(loaded_rl, dict):
            self._rejection_log = {
                k: v[-_REJECTION_LOG_CAP:] if isinstance(v, list) else [] for k, v in loaded_rl.items()
            }
        else:
            self._rejection_log = {}
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

        # Grace state is cleared by restore_state() (clean-slate design, Issue #282).
        # The coordinator does not reschedule grace timers on restart.

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

        # Chart_log endpoint estimator backfill flags (Issue #137)
        self._passive_k_backfilled = bool(state.get("passive_k_backfilled", False))
        self._vent_k_backfilled = bool(state.get("vent_k_backfilled", False))
        # Dual-estimator backfill flags (v2)
        self._passive_k_backfill_v2 = bool(state.get("passive_k_backfill_v2", False))
        self._vent_k_backfill_v2 = bool(state.get("vent_k_backfill_v2", False))
        # Solar phase offset backfill flag (Issue #147)
        self._solar_phase_backfill = bool(state.get("solar_phase_backfill", False))
        self._solar_phase_ac_backfill = bool(state.get("solar_phase_ac_backfill", False))  # Issue #312
        # Periodic daily re-fit tracker (Issue #310)
        _fit_date_str = state.get("last_solar_phase_fit_date")
        self._last_solar_phase_fit_date = date.fromisoformat(_fit_date_str) if _fit_date_str else None

        # Prediction archive — restore only on same-day restores (already gated above)
        raw_archive = state.get("pred_archive")
        if isinstance(raw_archive, dict):
            restored: dict[int, float] = {}
            for k, v in raw_archive.items():
                try:
                    restored[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            self._pred_archive = restored

        # Load AI report history if AI subsystem is active
        if self.claude_client:
            await self.hass.async_add_executor_job(self._load_ai_reports)
            await self.hass.async_add_executor_job(self._load_investigation_reports)

        # Restore event log ring buffer and emit restart boundary marker
        saved_log = state.get("event_log")
        if isinstance(saved_log, list):
            self._event_log = saved_log[-EVENT_LOG_CAP:]

        # Restart-cause classification (Issue #403): compare the persisted last-shutdown
        # version against VERSION, and check whether the prior shutdown was clean.
        _last_shutdown_version = self.learning._state.last_shutdown_version
        _clean_shutdown = self.learning._state.clean_shutdown
        _restart_payload: dict[str, Any] = {"recovered_events": len(self._event_log)}
        if isinstance(_last_shutdown_version, str) and _last_shutdown_version and _last_shutdown_version != VERSION:
            _cause = "version_changed"
            _LOGGER.info("Version changed: %s -> %s", _last_shutdown_version, VERSION)
            self._emit_event(
                "version_changed",
                {"old_version": _last_shutdown_version, "new_version": VERSION},
            )
            _restart_payload["old_version"] = _last_shutdown_version
            _restart_payload["new_version"] = VERSION
        elif _clean_shutdown:
            _cause = "user_restart"
        else:
            _cause = "unknown"
        _restart_payload["cause"] = _cause
        self._emit_event("system_restarted", _restart_payload)

        # Reset in-memory clean_shutdown so an unclean exit before the next clean shutdown
        # is correctly classified as "unknown" rather than stale-carrying "user_restart".
        # Not persisted here — it will be written on the next save_state() call.
        self.learning._state.clean_shutdown = False

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
            "pred_archive": {str(k): v for k, v in self._pred_archive.items()},
            "passive_k_backfilled": self._passive_k_backfilled,
            "vent_k_backfilled": self._vent_k_backfilled,
            "passive_k_backfill_v2": self._passive_k_backfill_v2,
            "vent_k_backfill_v2": self._vent_k_backfill_v2,
            "solar_phase_backfill": self._solar_phase_backfill,
            "solar_phase_ac_backfill": self._solar_phase_ac_backfill,  # Issue #312
            "last_solar_phase_fit_date": (
                self._last_solar_phase_fit_date.isoformat() if self._last_solar_phase_fit_date else None
            ),
            "event_log": list(self._event_log),
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
            "report_type": "activity",
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
                cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
                recent = [e for e in data if isinstance(e, dict) and e.get("timestamp", "") >= cutoff]
                self._ai_report_history = recent[-AI_REPORT_HISTORY_CAP:]
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

    def delete_ai_report(self, timestamp: str) -> bool:
        """Remove an activity report by timestamp. Returns True if removed."""
        before = len(self._ai_report_history)
        self._ai_report_history = [e for e in self._ai_report_history if e.get("timestamp") != timestamp]
        return len(self._ai_report_history) < before

    async def async_store_investigation_report(self, result: dict) -> None:
        """Store an investigation report result in history and persist to disk."""

        entry = {
            "timestamp": dt_util.now().isoformat(),
            "report_type": "investigation",
            "result": result,
        }
        self._investigation_report_history.append(entry)
        if len(self._investigation_report_history) > INVESTIGATION_REPORT_HISTORY_CAP:
            self._investigation_report_history = self._investigation_report_history[-INVESTIGATION_REPORT_HISTORY_CAP:]
        await self.hass.async_add_executor_job(self._save_investigation_reports)

    def get_investigation_report_history(self) -> list[dict]:
        """Return a copy of the investigation report history."""
        return list(self._investigation_report_history)

    def delete_investigation_report(self, timestamp: str) -> bool:
        """Remove an investigation report by timestamp. Returns True if removed."""
        before = len(self._investigation_report_history)
        self._investigation_report_history = [
            e for e in self._investigation_report_history if e.get("timestamp") != timestamp
        ]
        return len(self._investigation_report_history) < before

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
                cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
                recent = [e for e in data if isinstance(e, dict) and e.get("timestamp", "") >= cutoff]
                self._investigation_report_history = recent[-INVESTIGATION_REPORT_HISTORY_CAP:]
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
            self._door_open_timer_expiry.clear()

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

    def _is_recent_temp_command(self, threshold_seconds: float = 30.0) -> bool:
        """Check if a temperature setpoint command was issued recently (race guard)."""
        cmd_time = self.automation_engine._temp_command_time
        if cmd_time is None:
            return False
        return (dt_util.now() - cmd_time).total_seconds() < threshold_seconds

    def _is_recent_fan_command(self, threshold_seconds: float = 30.0) -> bool:
        """Check if a fan command was issued recently (race guard)."""
        cmd_time = self.automation_engine._fan_command_time
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

    async def _do_startup_coalesce(self) -> None:
        """Proactively coalesce HVAC and nat-vent state 5 minutes after restart (Issue #321)."""
        open_sensors = [s for s in self._resolved_sensors if self._is_sensor_open(s)]
        indoor = self._get_indoor_temp()
        outdoor = self._last_outdoor_temp
        c = self._current_classification

        _LOGGER.info(
            "Startup coalescing: outdoor=%s°F, indoor=%s°F, open_sensors=%s, classification=%s",
            f"{outdoor:.1f}" if outdoor is not None else "?",
            f"{indoor:.1f}" if indoor is not None else "?",
            open_sensors,
            c.day_type if c else "none",
        )

        nat_vent_activated = False
        hvac_commanded = False

        if open_sensors and outdoor is not None and indoor is not None:
            comfort_heat = float(self.config.get("comfort_heat", 70))
            comfort_cool = float(self.config.get("comfort_cool", 75))
            nat_vent_delta = float(self.config.get("natural_vent_delta", 3.0))
            nat_vent_threshold = comfort_cool + nat_vent_delta
            if outdoor < indoor and indoor > comfort_heat and outdoor < nat_vent_threshold:
                _LOGGER.info(
                    "Startup coalescing: nat-vent conditions met"
                    " (outdoor %.1f°F < indoor %.1f°F, indoor > comfort_heat %.1f°F,"
                    " outdoor < threshold %.1f°F) — activating nat-vent",
                    outdoor,
                    indoor,
                    comfort_heat,
                    nat_vent_threshold,
                )
                first_sensor = open_sensors[0]
                _LOGGER.debug("[coalesce-diag] before handle_door_window_open(%s)", first_sensor)
                await self.automation_engine.handle_door_window_open(first_sensor)
                _LOGGER.debug("[coalesce-diag] after handle_door_window_open(%s)", first_sensor)
                nat_vent_activated = True
            else:
                _LOGGER.info(
                    "Startup coalescing: nat-vent conditions not met"
                    " (outdoor=%.1f, indoor=%.1f, comfort_heat=%.1f, threshold=%.1f)",
                    outdoor or 0,
                    indoor or 0,
                    float(self.config.get("comfort_heat", 70)),
                    float(self.config.get("comfort_cool", 75)) + float(self.config.get("natural_vent_delta", 3.0)),
                )

        if not nat_vent_activated and c:
            climate_state = self.hass.states.get(self.config.get("climate_entity", ""))
            current_mode = climate_state.state if climate_state else "unknown"
            _LOGGER.info(
                "Startup coalescing: HVAC mode=%s, classification=%s — applying classification",
                current_mode,
                c.hvac_mode,
            )
            indoor_temp = self._get_indoor_temp()
            _LOGGER.debug("[coalesce-diag] before apply_classification() [coalesce path]")
            await self.automation_engine.apply_classification(
                c,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=indoor_temp,
            )
            _LOGGER.debug("[coalesce-diag] after apply_classification() [coalesce path]")
            hvac_commanded = True

        # Issue #327: Startup fan reconciliation.
        # Read the physical fan state from the thermostat and let the engine decide whether
        # to adopt it (nat-vent eligible), turn it off (no longer eligible), or leave it alone.
        # Runs AFTER nat-vent + classification so the engine has a settled HVAC state to reconcile
        # against.  The 5-min coalescing window already suppresses override detection in
        # _async_thermostat_changed, so the fan command here won't be misread as a manual override.
        _climate_state_reconcile = self.hass.states.get(self.config.get("climate_entity", ""))
        if _climate_state_reconcile:
            _attrs_reconcile = _climate_state_reconcile.attributes
            _fan_mode_reconcile = _attrs_reconcile.get("fan_mode", "")
            _hvac_action_reconcile = _attrs_reconcile.get("hvac_action", "")
            _thermostat_fan_running = _fan_mode_reconcile == "on" or _hvac_action_reconcile == "fan"
        else:
            _fan_mode_reconcile = "unknown"
            _hvac_action_reconcile = "unknown"
            _thermostat_fan_running = False
        _LOGGER.debug("[coalesce-diag] before reconcile_fan_on_startup()")
        await self.automation_engine.reconcile_fan_on_startup(
            indoor=indoor,
            outdoor=outdoor,
            thermostat_fan_running=_thermostat_fan_running,
            any_sensor_open=self._any_sensor_open(),
        )
        _LOGGER.debug("[coalesce-diag] after reconcile_fan_on_startup()")

        self._emit_event(
            "startup_coalesced",
            {
                "nat_vent_activated": nat_vent_activated,
                "hvac_commanded": hvac_commanded,
                "sensors_open_count": len(open_sensors),
            },
        )
        self._startup_coalesce_active = False
        _LOGGER.info("Startup coalescing complete — startup grace period ended")
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast and update classification (runs every 30 min)."""
        # Re-resolve group membership in case it changed
        _LOGGER.debug("[coalesce-diag] _async_update_data: enter")
        new_resolved = self._resolve_monitored_sensors()
        if set(new_resolved) != set(self._resolved_sensors):
            _LOGGER.info("Door/window sensor membership changed; updating listeners")
            self._unsubscribe_door_window_listeners()
            self._resolved_sensors = new_resolved
            self._subscribe_door_window_listeners()

        _LOGGER.debug("[coalesce-diag] _async_update_data: before _get_forecast()")
        forecast = await self._get_forecast()
        _LOGGER.debug("[coalesce-diag] _async_update_data: after _get_forecast() — forecast=%s", forecast is not None)
        self._hourly_forecast_temps = await self._get_hourly_forecast_data()
        self.automation_engine._hourly_forecast_temps = self._hourly_forecast_temps
        if forecast:
            prev_type = self._current_classification.day_type if self._current_classification else None
            _thresh = {
                "threshold_hot": self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
                "threshold_warm": self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
                "threshold_mild": self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
                "threshold_cool": self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
            }
            self._current_classification = classify_day(forecast, previous_day_type=prev_type, **_thresh)
            self._last_outdoor_temp = forecast.current_outdoor_temp
            self._apply_outdoor_windows_gate()

            # Chart log: emit classification_change event when day type changes
            if prev_type is not None and prev_type != self._current_classification.day_type:
                with contextlib.suppress(Exception):
                    _chart_hvac_cc = self._read_chart_hvac_action()
                    _LOGGER.debug(
                        "chart_log append: event=classification_change hvac=%r fan=%s",
                        _chart_hvac_cc,
                        self._fan_is_running() if self.automation_engine else False,
                    )
                    self._chart_log.append(
                        hvac=_chart_hvac_cc,
                        fan=self._fan_is_running() if self.automation_engine else False,
                        indoor=forecast.current_indoor_temp,
                        outdoor=forecast.current_outdoor_temp,
                        windows_open=self._any_sensor_open(),
                        windows_recommended=bool(self._current_classification.windows_recommended),
                        event="classification_change",
                        fan_running=self._fan_physically_running() if self.automation_engine else False,
                        nat_vent_active=bool(
                            self.automation_engine._natural_vent_active if self.automation_engine else False
                        ),
                    )

            # Startup safety: on first run, skip override detection — coalescing window handles it (Issue #321)
            if self._first_run:
                self._first_run = False
                # Recover v3 pending_observations that survived restart
                _pending_obs = self.learning._state.pending_observations
                if isinstance(_pending_obs, dict):
                    for _obs_type, _obs in list(_pending_obs.items()):
                        if not isinstance(_obs, dict):
                            continue
                        if _obs.get("_legacy_event"):
                            # Legacy HVAC event migrated into pending_observations — use HVAC commit path
                            session_mode = _obs.get("session_mode") or _obs.get("hvac_mode") or "heat"
                            _obs["session_mode"] = session_mode
                            self._pending_observations[_obs_type] = _obs
                        else:
                            # Bug 2 fix: For HVAC obs, check the right sample list based
                            # on the current phase.  Pre-fix obs had 'samples': [] which
                            # shadowed active_samples in the generic fallback, causing all
                            # HVAC observations to be discarded on every HA restart.
                            _hvac_types_sr = {OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL}
                            if _obs_type in _hvac_types_sr:
                                _phase_sr = _obs.get("_phase", "active")
                                if _phase_sr == "post_heat":
                                    samples = _obs.get("post_heat_samples", [])
                                    min_s = THERMAL_MIN_POST_HEAT_SAMPLES
                                else:
                                    # Active phase: any sample is worth recovering so
                                    # post-heat observation window can continue after restart.
                                    samples = _obs.get("active_samples", [])
                                    # Fall back to generic 'samples' key for pre-fix persisted obs
                                    if not samples:
                                        samples = _obs.get("samples", [])
                                    min_s = 1
                            else:
                                samples = _obs.get("samples", _obs.get("active_samples", []))
                                min_s = {
                                    OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_MIN_SAMPLES,
                                    OBS_TYPE_FAN_ONLY_DECAY: THERMAL_FAN_MIN_SAMPLES,
                                    OBS_TYPE_VENTILATED_DECAY: THERMAL_VENT_MIN_SAMPLES,
                                    OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_MIN_SAMPLES,
                                }.get(_obs_type, 10)
                            if len(samples) >= min_s:
                                self._pending_observations[_obs_type] = _obs
                                _LOGGER.info(
                                    "Startup: recovered v3 observation type=%s obs_id=%s samples=%d phase=%s",
                                    _obs_type,
                                    _obs.get("obs_id", "?"),
                                    len(samples),
                                    _obs.get("_phase", "active"),
                                )

                # Chart_log endpoint estimator backfill (Issue #137): run once on first startup
                # after the new code is deployed. The chart_log is loaded in __init__ so entries
                # are already available. Flags survive restart so backfill runs exactly once.
                if self.config.get("learning_enabled", True):
                    if not self._passive_k_backfilled:
                        self._run_passive_chart_log_fit(backfill=True)
                        self._passive_k_backfilled = True
                        _LOGGER.info("chart_log_endpoint: passive k_passive backfill complete")
                    if not self._vent_k_backfilled:
                        self._run_ventilated_chart_log_fit(backfill=True)
                        self._vent_k_backfilled = True
                        _LOGGER.info("chart_log_endpoint: ventilated k_vent_window backfill complete")
                    if not self._passive_k_backfill_v2:
                        self._run_passive_chart_log_fit(backfill=True)
                        self._passive_k_backfill_v2 = True
                        _LOGGER.info("chart_log_endpoint v2: passive k_passive dual-estimator backfill complete")
                    if not self._vent_k_backfill_v2:
                        self._run_ventilated_chart_log_fit(backfill=True)
                        self._vent_k_backfill_v2 = True
                        _LOGGER.info("chart_log_endpoint v2: ventilated k_vent_window dual-estimator backfill complete")
                    if not self._solar_phase_backfill:
                        self._run_solar_phase_chart_log_fit(backfill=True)
                        self._solar_phase_backfill = True
                        self._last_solar_phase_fit_date = dt_util.now().date()
                        _LOGGER.info("chart_log solar_phase: phase offset backfill complete")
                    if not self._solar_phase_ac_backfill:
                        self._run_ac_duty_solar_phase_fit()
                        # Flag is set inside the method after completion

            # Bug 1 (Issue #321): Startup coalescing — evaluate state at t+5min instead of detecting override at t+30s
            _LOGGER.debug(
                "[coalesce-diag] coalesce condition check: startup_timer_fired=%s"
                " startup_coalesce_active=%s current_classification=%s",
                self._startup_timer_fired,
                self._startup_coalesce_active,
                self._current_classification is not None,
            )
            if self._startup_timer_fired and self._startup_coalesce_active and self._current_classification:
                _LOGGER.debug("[coalesce-diag] before _do_startup_coalesce()")
                await self._do_startup_coalesce()
                _LOGGER.debug("[coalesce-diag] after _do_startup_coalesce()")

            # Periodic daily solar phase re-fit (Issue #310): run once per calendar day
            # using only the last 2 days of chart_log (backfill=False). Stamping
            # _last_solar_phase_fit_date in the one-shot block above prevents a double-fit
            # on the same cycle when a fresh install runs both blocks back-to-back.
            if self.config.get("learning_enabled", True):
                self._maybe_run_periodic_solar_phase_fit()

            # Refresh the thermal model on every 30-min cycle, not just at the daily
            # briefing. get_thermal_model() is a pure computation (no I/O), so calling it
            # 48×/day is negligible. Refreshing here:
            #   1. Restores the model after HA restart (daily briefing is the only other
            #      writer, so _thermal_model is {} for the rest of the day after a restart)
            #   2. Keeps thermal_equilibrium_f current as outdoor_temp and solar_factor
            #      change through the day (6 AM conditions are wrong by afternoon)
            #   3. Applies mid-day observation commits to same-day automation decisions
            if self.config.get("learning_enabled", True) and self.automation_engine:
                self.automation_engine._thermal_model = self.learning.get_thermal_model(
                    outdoor_temp_f=self._last_outdoor_temp,
                    solar_factor=_solar_factor(dt_util.now().hour),
                )
                self._solar_phase_offset = (
                    self.automation_engine._thermal_model.get("solar_phase_offset_h")
                    or THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
                )
                _LOGGER.debug(
                    "thermal model refreshed (30-min cycle): confidence=%s k_passive=%s solar_phase_offset=%.1f",
                    self.automation_engine._thermal_model.get("confidence", "none"),
                    self.automation_engine._thermal_model.get("k_passive"),
                    self._solar_phase_offset,
                )

            # Compute and cache ODE prediction for ceiling guard + chart reuse.
            # Offloaded to executor — ODE integration + OLS math blocks the event loop otherwise.
            self._last_predicted_indoor = await self.hass.async_add_executor_job(
                functools.partial(
                    _build_predicted_indoor_future,
                    self._hourly_forecast_temps,
                    self.config,
                    dt_util.now(),
                    current_indoor_temp=self._get_indoor_temp(),
                    thermal_model=self.automation_engine._thermal_model if self.automation_engine else {},
                    occupancy_mode=self._occupancy_mode,
                    classification=self._current_classification,
                )
            )
            _LOGGER.debug(
                "Caching predicted indoor curve: %d points, [0]=%s",
                len(self._last_predicted_indoor),
                f"{self._last_predicted_indoor[0]['temp']:.1f}°F" if self._last_predicted_indoor else "none",
            )

            # Populate first-write-wins prediction archive (PRED_ARCHIVE_HORIZON_HOURS lookahead).
            # setdefault ensures the earliest (most advance) prediction is kept per 30-min slot.
            _archive_cutoff = dt_util.now() + timedelta(hours=PRED_ARCHIVE_HORIZON_HOURS)
            for _ae in self._last_predicted_indoor:
                try:
                    _ae_dt = datetime.fromisoformat(_ae["ts"])
                except (ValueError, KeyError):
                    continue
                if _ae_dt > _archive_cutoff:
                    break
                self._pred_archive.setdefault(self._pred_archive_key(_ae_dt), _ae["temp"])

            # Bug 2 (Issue #321): Detect stuck grace — override active but timer callback
            # never fired. Force-clear so automation resumes on this cycle.
            _ae = self.automation_engine
            if _ae._manual_override_active and not _ae._grace_active and _ae._grace_end_time is not None:
                _stuck_end = dt_util.parse_datetime(_ae._grace_end_time)
                if _stuck_end is not None and dt_util.now() > _stuck_end:
                    _LOGGER.error(
                        "Stuck grace detected: manual_override_active=True but grace_end_time"
                        " %s is in the past and no grace timer is active. Force-clearing"
                        " override (Issue #321).",
                        _ae._grace_end_time,
                    )
                    _ae.clear_manual_override(reason="stuck_grace_recovery")
                    self._emit_event("stuck_grace_recovered", {"grace_end_time": _ae._grace_end_time})

            _LOGGER.debug("[coalesce-diag] before apply_classification() [regular cycle path]")
            await self.automation_engine.apply_classification(
                self._current_classification,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=self._get_indoor_temp(),
            )
            _LOGGER.debug("[coalesce-diag] after apply_classification() [regular cycle path]")

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
                    if (
                        forecast.current_indoor_temp < comfort_low or forecast.current_indoor_temp > comfort_high
                    ) and not self._is_nat_vent_tolerated_deviation(
                        forecast.current_indoor_temp, comfort_low, comfort_high
                    ):
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
                _LOGGER.debug("[coalesce-diag] before check_natural_vent_conditions()")
                await self.automation_engine.check_natural_vent_conditions()
                _LOGGER.debug("[coalesce-diag] after check_natural_vent_conditions()")

            # Save state after classification update
            _LOGGER.debug("[coalesce-diag] before _async_save_state()")
            await self._async_save_state()
            _LOGGER.debug("[coalesce-diag] after _async_save_state() — _async_update_data exiting normally")
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

        # Issue #331 follow-up: surface an UNTRACKED fan (the thermostat running its own
        # blower/fan that CA did not command) in the event log so it is not invisible.
        # Deduped entry/exit: emit once when the fan enters the untracked-running state and
        # once when it clears — never per cooling-cycle. Classify the inferred source.
        _is_untracked = self._compute_fan_status() == "running (untracked)"
        _untracked_logged = getattr(self, "_untracked_fan_active", False)
        if _is_untracked and not _untracked_logged:
            _cs2 = self.hass.states.get(self.config.get("climate_entity", ""))
            _t_mode = _cs2.state if _cs2 else "unknown"
            _t_action = str(_cs2.attributes.get("hvac_action", "")) if _cs2 else ""
            _t_fan = str(_cs2.attributes.get("fan_mode", "")) if _cs2 else ""
            _source = (
                f"thermostat blower during {_t_mode} cycle"
                if _t_mode in ("cool", "heat", "heat_cool")
                else "thermostat fan schedule/circulation"
            )
            _fan_mode_val = self.automation_engine.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _fan_device = (
                "whf"
                if _fan_mode_val == FAN_MODE_WHOLE_HOUSE
                else "hvac_fan"
                if _fan_mode_val == FAN_MODE_HVAC
                else "both"
                if _fan_mode_val == FAN_MODE_BOTH
                else "none"
            )
            self._emit_event(
                "fan_running_untracked",
                {
                    "hvac_action": _t_action,
                    "fan_mode": _t_fan,
                    "thermostat_mode": _t_mode,
                    "source": _source,
                    "fan_device": _fan_device,
                },
            )
            self._untracked_fan_active = True
        elif not _is_untracked and _untracked_logged:
            _fan_mode_clr = self.automation_engine.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _fan_device_clr = (
                "whf"
                if _fan_mode_clr == FAN_MODE_WHOLE_HOUSE
                else "hvac_fan"
                if _fan_mode_clr == FAN_MODE_HVAC
                else "both"
                if _fan_mode_clr == FAN_MODE_BOTH
                else "none"
            )
            self._emit_event("fan_untracked_cleared", {"fan_device": _fan_device_clr})
            self._untracked_fan_active = False

        # Issue #359 Fix D: periodic backstop — reconcile an untracked fan at each 30-min cycle.
        # The one-shot trigger in _async_thermostat_changed (~line 2826) is guarded by
        # not _fan_override_active, but that flag may already be True from Block 3 in the same
        # event, leaving the untracked fan permanently unresolved.  This backstop catches it.
        if (
            _is_untracked
            and not self.automation_engine._fan_override_active
            and not self.automation_engine._grace_active
        ):
            _LOGGER.info("Fan running untracked with no active override/grace — triggering periodic reconciliation")
            _cs_bst = self.hass.states.get(self.config.get("climate_entity", ""))
            _bst_hvac_action = str(_cs_bst.attributes.get("hvac_action", "")).lower() if _cs_bst else ""
            if _bst_hvac_action not in ("heating", "cooling"):
                await self.automation_engine.reconcile_fan_on_startup(
                    indoor=self._get_indoor_temp(),
                    outdoor=self._last_outdoor_temp,
                    thermostat_fan_running=True,
                    any_sensor_open=self._any_sensor_open(),
                )
            else:
                _LOGGER.warning(
                    "Periodic reconciliation skipped: HVAC actively %s — fan is thermostat blower",
                    _bst_hvac_action,
                )

        # Issue #361: command-only fan reconciliation (fan_state_feedback=False).
        # When the fan entity only echoes the last command, we cannot detect physical overrides
        # via state changes.  Instead, assert the desired state idempotently each 30-min cycle.
        _ae_cmd = self.automation_engine
        if not self._fan_state_feedback_enabled() and _ae_cmd is not None:
            _fan_mode_cmd = self.config.get(CONF_FAN_MODE, "")
            if _fan_mode_cmd not in ("", "none", None, FAN_MODE_DISABLED):
                _desired_on = bool(_ae_cmd._fan_active)
                _grace_on = bool(_ae_cmd._grace_active)
                _override_on = bool(_ae_cmd._fan_override_active)
                _last_cmd = self._last_commanded_fan_state
                if _desired_on and _last_cmd is not True and not _grace_on and not _override_on:
                    _LOGGER.info(
                        "Fan command-only assert: desired=on last_commanded=%s → issuing on command (fan_entity=%s)",
                        _last_cmd,
                        self.config.get(CONF_FAN_ENTITY, ""),
                    )
                    await self._async_command_fan_entity(on=True)
                    self._last_commanded_fan_state = True
                elif not _desired_on and _last_cmd is not False and not _grace_on and not _override_on:
                    _LOGGER.info(
                        "Fan command-only assert: desired=off last_commanded=%s → issuing off command (fan_entity=%s)",
                        _last_cmd,
                        self.config.get(CONF_FAN_ENTITY, ""),
                    )
                    await self._async_command_fan_entity(on=False)
                    self._last_commanded_fan_state = False
                else:
                    _LOGGER.debug(
                        "Fan command-only assert: desired=%s last_commanded=%s — no command needed",
                        "on" if _desired_on else "off",
                        _last_cmd,
                    )

        _base_runtime = self._today_record.hvac_runtime_minutes if self._today_record else 0.0
        _session_elapsed = (dt_util.now() - self._hvac_on_since).total_seconds() / 60 if self._hvac_on_since else 0.0
        hvac_runtime_today = round(_base_runtime + _session_elapsed, 1)

        # --- Thermal observation pipeline sampling ---
        self._update_pre_heat_buffer()
        self._sample_all_observations()
        if hasattr(self, "_pending_observations") and self._pending_observations:
            _LOGGER.info(
                "Thermal pipeline: %d pending observations active",
                len(self._pending_observations),
            )
        for _hvac_obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
            await self._check_hvac_stabilization(_hvac_obs_type)

        # --- Temperatures for coordinator.data (sensor entities + AI context) ---
        _indoor_temp = self._get_indoor_temp()
        _outdoor_temp = forecast.current_outdoor_temp if forecast else None

        # Schedule overnight pre-cool if a warming trend is active (idempotent — runs once per day)
        self._maybe_schedule_pre_cool()

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
            ATTR_WHF_STATUS: self._compute_whf_status(),
            ATTR_HVAC_FAN_STATUS: self._compute_hvac_fan_status(),
            ATTR_FAN_RUNTIME: self.automation_engine._get_fan_runtime_minutes(),
            ATTR_FAN_OVERRIDE_SINCE: self.automation_engine._fan_override_time,
            ATTR_FAN_RUNNING: fan_running,
            ATTR_HVAC_ACTION: hvac_action,
            "hvac_mode": hvac_mode,
            ATTR_HVAC_RUNTIME_TODAY: hvac_runtime_today,
            ATTR_CONTACT_STATUS: self._compute_contact_status(),
            ATTR_AI_STATUS: self.claude_client.get_status()["status"] if self.claude_client else "disabled",
            ATTR_INDOOR_TEMP: _indoor_temp,
            ATTR_OUTDOOR_TEMP: _outdoor_temp,
            ATTR_FORECAST_HIGH: c.today_high if c else None,
            ATTR_FORECAST_LOW: c.today_low if c else None,
            ATTR_FORECAST_HIGH_TOMORROW: c.tomorrow_high if c else None,
            ATTR_FORECAST_LOW_TOMORROW: c.tomorrow_low if c else None,
            "pre_cool_status": self._pre_cool_status,
            # Issue #361: WHF command-only mode status fields
            "whf_mode": (
                "disabled"
                if self.config.get(CONF_FAN_MODE, "") in ("", "none", None, FAN_MODE_DISABLED)
                else ("state-feedback" if self._fan_state_feedback_enabled() else "command-only")
            ),
            "whf_last_commanded": (
                "on"
                if self._last_commanded_fan_state is True
                else "off"
                if self._last_commanded_fan_state is False
                else None
            ),
            "whf_desired": bool(self.automation_engine._fan_active) if self.automation_engine else None,
        }

        # Append chart log entry (every coordinator tick — 30-min cadence)
        with contextlib.suppress(Exception):
            indoor_temp = forecast.current_indoor_temp if forecast else None
            outdoor_temp = forecast.current_outdoor_temp if forecast else None
            # Extract current-hour prediction to persist alongside actual reading
            _pred_outdoor_val: float | None = None
            _pred_indoor_val: float | None = None
            if indoor_temp is None:
                _LOGGER.debug(
                    "chart log: indoor_temp unavailable — skipping pred_indoor write"
                    " (thermostat may be unknown/unavailable)"
                )
            _now_dt = dt_util.now()
            _pred_outdoor_val = _extract_current_hour_forecast_temp(self._hourly_forecast_temps, _now_dt)
            # First-write-wins archive: pred_indoor reflects ODE made ~4h ago.
            # Falls back to current ODE[0] only during warmup (first 4h after restart/install).
            _archived_pred = self._lookup_pred_archive(_now_dt)
            if _archived_pred is not None:
                _pred_indoor_val = _archived_pred
            elif self._last_predicted_indoor:
                _pred_indoor_val = self._last_predicted_indoor[0].get("temp")  # warmup fallback
            _chart_hvac_poll = self._read_chart_hvac_action()
            # Read thermostat setpoint and convert to °F for chart_log storage.
            _setpoint_f: float | None = None
            _chart_unit = self.config.get("temp_unit", "fahrenheit")
            _climate_state = self.hass.states.get(self.config["climate_entity"])
            if _climate_state and _climate_state.state in ("heat", "cool"):
                _raw_sp = _climate_state.attributes.get("target_temperature")
                if _raw_sp is not None:
                    _setpoint_f = to_fahrenheit(float(_raw_sp), _chart_unit)
            _LOGGER.debug(
                "chart_log append: event=30min_poll hvac=%r fan=%s",
                _chart_hvac_poll,
                self._fan_is_running(),
            )
            self._chart_log.append(
                hvac=_chart_hvac_poll,
                fan=self._fan_is_running(),
                indoor=indoor_temp,
                outdoor=outdoor_temp,
                windows_open=self._any_sensor_open(),
                windows_recommended=bool(self._current_classification.windows_recommended)
                if self._current_classification
                else False,
                pred_outdoor=_pred_outdoor_val,
                pred_indoor=_pred_indoor_val,
                setpoint=_setpoint_f,
                fan_running=self._fan_physically_running(),
                nat_vent_active=bool(self.automation_engine._natural_vent_active if self.automation_engine else False),
            )
            self._chart_log.save()
            _LOGGER.debug(
                "chart_log pred_indoor=%.1f indoor=%.1f delta=%+.1f (%s)",
                _pred_indoor_val if _pred_indoor_val is not None else float("nan"),
                indoor_temp if indoor_temp is not None else float("nan"),
                (_pred_indoor_val - indoor_temp)
                if (_pred_indoor_val is not None and indoor_temp is not None)
                else float("nan"),
                "archive" if _archived_pred is not None else ("ode-warmup" if self._last_predicted_indoor else "none"),
            )

        with contextlib.suppress(Exception):
            self._thermal_factors = _compute_thermal_factors(self._chart_log.get_entries("7d"))

        # Purge archive entries older than 7 days (bounded at ≤336 entries at 30-min resolution).
        _archive_expire_cutoff = int((dt_util.now() - timedelta(days=7)).timestamp())
        self._pred_archive = {k: v for k, v in self._pred_archive.items() if k >= _archive_expire_cutoff}

        # Detect and emit post-cycle incidents
        self._detect_and_emit_incidents()

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
                        val_f = to_fahrenheit(float(state.state), unit)
                        if _MIN_PLAUSIBLE_INDOOR_F <= val_f <= _MAX_PLAUSIBLE_INDOOR_F:
                            return val_f
                        _LOGGER.warning(
                            "Indoor temp %.1f°F from %s is outside plausible range"
                            " [%.0f, %.0f]°F; treating as unavailable",
                            val_f,
                            entity_id,
                            _MIN_PLAUSIBLE_INDOOR_F,
                            _MAX_PLAUSIBLE_INDOOR_F,
                        )
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
            if temp is not None:
                try:
                    val_f = to_fahrenheit(float(temp), unit)
                    if _MIN_PLAUSIBLE_INDOOR_F <= val_f <= _MAX_PLAUSIBLE_INDOOR_F:
                        return val_f
                    _LOGGER.warning(
                        "Indoor temp %.1f°F from %s is outside plausible range [%.0f, %.0f]°F; treating as unavailable",
                        val_f,
                        self.config["climate_entity"],
                        _MIN_PLAUSIBLE_INDOOR_F,
                        _MAX_PLAUSIBLE_INDOOR_F,
                    )
                except (ValueError, TypeError):
                    pass
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
        # HA daily forecasts vary by provider: some include today, some start
        # from tomorrow. Some use UTC midnight datetimes (e.g.
        # 2026-05-16T00:00:00+00:00 = 2026-05-15 17:00 PDT), which
        # dt_util.as_local() shifts to the previous local day. Build a
        # date-keyed dict so we never assume array position == calendar day.
        today_high = current_outdoor
        today_low = current_outdoor
        tomorrow_high = current_outdoor
        tomorrow_low = current_outdoor

        today_fc = None
        tomorrow_fc = None
        if forecast:
            # Use local calendar date for "today" and extract the raw date from
            # each forecast entry without timezone conversion. Weather APIs that
            # use UTC midnight timestamps (e.g. 2026-05-31T00:00:00Z) intend
            # the date portion (2026-05-31) as the forecast date — comparing
            # that raw date against the local calendar date is correct at all
            # hours. Using UTC for "now" breaks in the evening when UTC has
            # rolled to the next calendar day but local time hasn't (Issue #190).
            now_local = dt_util.now()
            now_date = now_local.date()
            tomorrow_date = now_date + timedelta(days=1)
            _LOGGER.debug(
                "_get_forecast raw datetimes (first 5): %s",
                [e.get("datetime") for e in forecast[:5]],
            )
            forecast_by_date: dict = {}
            for entry in forecast:
                fc_dt = entry.get("datetime", "")
                try:
                    fc_obj = datetime.fromisoformat(fc_dt)
                    # Raw date: no tz conversion. API date intent, compared against local now_date.
                    fc_date = fc_obj.date()
                    forecast_by_date.setdefault(fc_date, entry)
                except (ValueError, TypeError):
                    continue
            today_fc = forecast_by_date.get(now_date)
            tomorrow_fc = forecast_by_date.get(tomorrow_date)
            available_dates = sorted(forecast_by_date.keys())
            if today_fc is None and available_dates:
                _LOGGER.warning(
                    "_get_forecast: no entry for today (%s local); available dates: %s",
                    now_date,
                    available_dates,
                )
            if tomorrow_fc is None and available_dates:
                _LOGGER.warning(
                    "_get_forecast: no entry for tomorrow (%s local); available dates: %s",
                    tomorrow_date,
                    available_dates,
                )
            _LOGGER.info(
                "_get_forecast matched: today=%s raw_temp=%s, tomorrow=%s raw_temp=%s",
                now_date,
                today_fc.get("temperature") if today_fc else f"none→{current_outdoor}°F fallback",
                tomorrow_date,
                tomorrow_fc.get("temperature") if tomorrow_fc else f"none→{current_outdoor}°F fallback",
            )

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
            thermal_model = self.learning.get_thermal_model(learning_health=self._build_learning_health())
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
            predicted_indoor_future=self._last_predicted_indoor or None,
            predicted_outdoor_future=(
                _build_future_forecast_outdoor(
                    self._hourly_forecast_temps,
                    classification=classification,
                )
                or None
            ),
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
        _thresh = {
            "threshold_hot": self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
            "threshold_warm": self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
            "threshold_mild": self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
            "threshold_cool": self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
        }
        classification = classify_day(forecast, previous_day_type=prev_type, **_thresh)
        self._current_classification = classification
        self._last_outdoor_temp = forecast.current_outdoor_temp
        self._apply_outdoor_windows_gate()

        # Daily incremental solar phase re-fit (Issue #310/#312)
        if self.config.get("learning_enabled", True):
            self._maybe_run_periodic_solar_phase_fit()

        # Inject thermal model into automation engine for adaptive scheduling
        if self.config.get("learning_enabled", True):
            thermal_model = self.learning.get_thermal_model(
                learning_health=self._build_learning_health(),
                outdoor_temp_f=forecast.current_outdoor_temp,
                solar_factor=_solar_factor(now.hour),
            )
            self.automation_engine._thermal_model = thermal_model
            self._solar_phase_offset = thermal_model.get("solar_phase_offset_h") or THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT
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
        # Update cached ODE prediction for ceiling guard.
        # thermal_model is already computed from self.learning.get_thermal_model() above.
        # Offloaded to executor — ODE integration + OLS math blocks the event loop otherwise.
        self._last_predicted_indoor = await self.hass.async_add_executor_job(
            functools.partial(
                _build_predicted_indoor_future,
                self._hourly_forecast_temps,
                self.config,
                dt_util.now(),
                current_indoor_temp=self._get_indoor_temp(),
                thermal_model=thermal_model,
                occupancy_mode=self._occupancy_mode,
                classification=classification,
            )
        )
        _LOGGER.debug(
            "Caching predicted indoor curve (briefing): %d points",
            len(self._last_predicted_indoor),
        )
        await self.automation_engine.apply_classification(
            classification,
            predicted_indoor=self._last_predicted_indoor,
            indoor_temp=self._get_indoor_temp(),
        )

        # Initialize today's learning record, preserving any counters already accumulated
        # today (e.g. after an HA restart mid-day that fires briefing again).
        _today_str = dt_util.now().strftime("%Y-%m-%d")
        _prev = self._today_record if (self._today_record and self._today_record.date == _today_str) else None
        self._today_record = DailyRecord(
            date=_today_str,
            day_type=classification.day_type,
            trend_direction=classification.trend_direction,
            windows_recommended=classification.windows_recommended,
            window_open_time=(classification.window_open_time.isoformat() if classification.window_open_time else None),
            window_close_time=(
                classification.window_close_time.isoformat() if classification.window_close_time else None
            ),
            hvac_mode_recommended=classification.hvac_mode,
            hvac_runtime_minutes=_prev.hvac_runtime_minutes if _prev else 0.0,
            comfort_violations_minutes=_prev.comfort_violations_minutes if _prev else 0.0,
            manual_overrides=_prev.manual_overrides if _prev else 0,
            thermal_session_count=_prev.thermal_session_count if _prev else 0,
            occupancy_away_minutes=_prev.occupancy_away_minutes if _prev else 0.0,
            windows_opened=_prev.windows_opened if _prev else False,
            window_open_actual_time=_prev.window_open_actual_time if _prev else None,
            override_details=list(_prev.override_details) if _prev else [],
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
        await self.automation_engine.handle_morning_wakeup(indoor_temp=self._get_indoor_temp())

    async def _async_bedtime(self, now: datetime) -> None:
        """Handle bedtime setback."""
        await self.automation_engine.handle_bedtime()

    def _compute_pre_cool_trigger_time(self) -> datetime | None:
        """Compute the pre-cool trigger time for tonight.

        Primary: nat-vent window close time + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES.
        Fallback: wake_time - PRE_COOL_WAKE_OFFSET_HOURS.
        Returns None if there is no warming trend today.
        """
        from .const import (
            CONF_SLEEP_COOL,
            PRE_COOL_MIN_HEADROOM_F,
            PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
            PRE_COOL_WAKE_OFFSET_HOURS,
        )

        c = self._current_classification
        if not c or c.setback_modifier >= 0:
            return None

        # Verify the pre-cool target would actually differ from sleep_cool
        sleep_cool = float(self.config.get(CONF_SLEEP_COOL) or self.config.get("sleep_cool", 78.0))
        comfort_heat = float(self.config.get("comfort_heat", 70.0))
        pre_cool_target = max(sleep_cool + c.setback_modifier, comfort_heat + PRE_COOL_MIN_HEADROOM_F)
        if pre_cool_target >= sleep_cool:
            _LOGGER.info(
                "Pre-cool scheduling: clamped target (%.1f°F) == sleep_cool (%.1f°F); skipping",
                pre_cool_target,
                sleep_cool,
            )
            return None

        now = dt_util.now()
        today = now.date()

        # Primary: nat-vent window close time + delay
        if c.window_close_time is not None:
            wct_dt = dt_util.as_local(datetime.combine(today, c.window_close_time).replace(tzinfo=None))
            # If window close is before midnight (typical), use today; else tomorrow
            if wct_dt < now:
                wct_dt = wct_dt + timedelta(days=1)
            trigger = wct_dt + timedelta(minutes=PRE_COOL_POST_NAT_VENT_DELAY_MINUTES)
            _LOGGER.info(
                "Pre-cool scheduled for %s (nat-vent close %s + %dmin); target %.1f°F",
                trigger.strftime("%H:%M"),
                c.window_close_time.strftime("%H:%M"),
                PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
                pre_cool_target,
            )
            return trigger

        # Fallback: wake_time - offset
        wake_str = self.config.get("wake_time", "06:30")
        wake_h, wake_m = int(wake_str.split(":")[0]), int(wake_str.split(":")[1])
        wake_dt = dt_util.as_local(datetime.combine(today, time(wake_h, wake_m)).replace(tzinfo=None))
        # If wake_time already passed, schedule for tomorrow night
        if wake_dt < now:
            wake_dt = wake_dt + timedelta(days=1)
        trigger = wake_dt - timedelta(hours=PRE_COOL_WAKE_OFFSET_HOURS)
        _LOGGER.info(
            "Pre-cool scheduled for %s (wake_time %s - %.0fh fallback); target %.1f°F",
            trigger.strftime("%H:%M"),
            wake_str,
            PRE_COOL_WAKE_OFFSET_HOURS,
            pre_cool_target,
        )
        return trigger

    def _maybe_schedule_pre_cool(self) -> None:
        """Schedule the overnight pre-cool trigger if a warming trend is active and not yet scheduled."""
        if self._pre_cool_trigger_scheduled:
            return
        trigger_time = self._compute_pre_cool_trigger_time()
        if trigger_time is None:
            return
        now = dt_util.now()
        if trigger_time <= now:
            _LOGGER.info("Pre-cool trigger time %s already passed; skipping scheduling", trigger_time.strftime("%H:%M"))
            return

        # Build the pre-cool target for status display
        from .const import CONF_SLEEP_COOL, PRE_COOL_MIN_HEADROOM_F

        c = self._current_classification
        sleep_cool = float(self.config.get(CONF_SLEEP_COOL) or self.config.get("sleep_cool", 78.0))
        comfort_heat = float(self.config.get("comfort_heat", 70.0))
        pre_cool_target = max(sleep_cool + c.setback_modifier, comfort_heat + PRE_COOL_MIN_HEADROOM_F)

        self._pre_cool_trigger_cancel = async_track_point_in_time(self.hass, self._async_pre_cool_trigger, trigger_time)
        self._pre_cool_trigger_scheduled = True
        self._pre_cool_trigger_dt = trigger_time
        self._pre_cool_target = pre_cool_target
        self._pre_cool_status = (
            f"Pre-cool scheduled ({pre_cool_target:.0f}°F @ {trigger_time.strftime('%I:%M %p').lstrip('0')})"
        )

    async def _async_pre_cool_trigger(self, now: datetime) -> None:
        """Handle the overnight pre-cool trigger point."""
        self._pre_cool_trigger_dt = None  # trigger has fired; no longer a future candidate
        nat_vent_just_closed = not self.automation_engine.natural_vent_active
        indoor_temp = self._get_indoor_temp()
        result = await self.automation_engine.handle_pre_cool(
            indoor_temp=indoor_temp,
            nat_vent_just_closed=nat_vent_just_closed,
        )
        _LOGGER.info("Pre-cool trigger handler completed: %s", result)
        if "suppressed" in result:
            self._pre_cool_status = result.replace("suppressed: ", "pre-cool suppressed — ")
        elif "applied" in result:
            self._pre_cool_status = result.replace("applied: ", "pre-cool active (").rstrip() + ")"
        await self.async_refresh()

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

        # Reset pre-cool state for the new day
        if self._pre_cool_trigger_cancel is not None:
            self._pre_cool_trigger_cancel()
            self._pre_cool_trigger_cancel = None
        self._pre_cool_trigger_scheduled = False
        self._pre_cool_status = None
        self._pre_cool_trigger_dt = None
        self._pre_cool_target = None

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
            _expiry_time = dt_util.now() + timedelta(seconds=debounce_sec)
            _expiry_iso = _expiry_time.isoformat()
            self._door_open_timer_expiry[entity_id] = _expiry_iso
            _LOGGER.info(
                "Contact sensor opened: %s — debounce started (%ds), nat vent eval at %s",
                entity_id,
                debounce_sec,
                _expiry_time.strftime("%H:%M:%S"),
            )

            @callback
            def _debounce_expired(_now: Any, eid: str = entity_id) -> None:
                """Debounce period elapsed — schedule async check."""

                async def _do_debounce() -> None:
                    self._door_open_timers.pop(eid, None)
                    self._door_open_timer_expiry.pop(eid, None)
                    if self._is_sensor_open(eid):
                        c = self._current_classification
                        _LOGGER.info(
                            "Debounce expired for %s — evaluating nat vent conditions "
                            "(classification=%s, hvac_mode=%s, windows_recommended=%s)",
                            eid,
                            c.day_type if c else "none",
                            c.hvac_mode if c else "none",
                            c.windows_recommended if c else False,
                        )
                        await self.automation_engine.handle_door_window_open(eid)
                        # Trigger coordinator refresh so sensor entities reflect post-evaluation state
                        self.hass.async_create_task(self.async_request_refresh())
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
            # Trigger coordinator refresh so next_automation sensor shows debounce pending state
            self.hass.async_create_task(self.async_request_refresh())
        else:
            # Sensor transitioned to closed — cancel any pending debounce timer
            cancel = self._door_open_timers.pop(entity_id, None)
            self._door_open_timer_expiry.pop(entity_id, None)
            if cancel:
                cancel()
                _LOGGER.info("Contact sensor closed: %s — debounce cancelled", entity_id)

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

        # Bug 1 (Issue #321): Suppress override detection during startup coalescing window
        if self._startup_coalesce_active:
            _LOGGER.debug(
                "Startup coalescing active — suppressing thermostat override detection for %s",
                new_state.state if new_state else "unknown",
            )
            return

        # Bug 3 (Issue #321): Per-temperature-tick nat-vent cycling re-evaluation.
        # Fires on every thermostat state event (including attribute-only changes) when
        # a nat-vent session is active so the fan cycles before the hard comfort-floor exit.
        _new_temp_attr = new_state.attributes.get("current_temperature")
        _old_temp_attr = old_state.attributes.get("current_temperature")
        if (
            _new_temp_attr is not None
            and _new_temp_attr != _old_temp_attr
            and self.automation_engine._natural_vent_active
        ):
            await self.automation_engine.nat_vent_temperature_check(float(_new_temp_attr))

        # Issue #327: Thermostatic fan re-evaluation on every indoor temp tick.
        # Fires whenever the thermostat reports a new current_temperature and a CA fan is running
        # (nat-vent OR regular fan-only).  The engine method is idempotent; calling it here when
        # nat_vent_temperature_check already ran above is safe — they target different exit paths.
        if (
            _new_temp_attr is not None
            and _new_temp_attr != _old_temp_attr
            and (self.automation_engine._fan_active or self.automation_engine._natural_vent_active)
        ):
            await self.automation_engine.fan_thermostat_check(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                trigger="tick",
            )

        # Expected-state confirmation suppression: if thermostat is confirming an automation
        # command (same mode, within 2 minutes), this is not a user override.
        # Covers cloud-thermostat lag where _hvac_command_pending is already cleared by the time
        # the state-change event arrives (e.g. 3–30s for Ecobee/Nest cloud round-trips).
        _last_cmd_mode = self.automation_engine._last_commanded_hvac_mode
        _last_cmd_time = self.automation_engine._last_commanded_hvac_time
        # Single-setpoint only (Issue #301): the dual-setpoint heat_cool path is removed.
        # CA always sends one call with hvac_mode + single temperature; no _pending_setpoint_low/high.
        _is_expected_confirmation = (
            _last_cmd_mode is not None
            and _last_cmd_time is not None
            and new_state.state == _last_cmd_mode
            and (dt_util.now() - _last_cmd_time).total_seconds() < 120
        )

        # Detect manual HVAC override during a door/window pause.
        # Note: we intentionally do NOT require old_state == "off" here.
        # The async _set_hvac_mode("off") service call may not have
        # propagated to HA's state machine yet when the user quickly
        # turns HVAC back on, so old_state could still be the pre-pause
        # mode (e.g. "cool"). The paused_by_door flag is authoritative.
        # We DO require old_state != new_state to skip attribute-only events
        # (e.g. hvac_action idle→cooling) where the HVAC mode didn't change.
        if (
            self.automation_engine.is_paused_by_door
            and old_state.state != new_state.state
            and new_state.state not in ("off", "unavailable", "unknown")
        ):
            _any_command_pending = (
                self.automation_engine._hvac_command_pending
                or self.automation_engine._fan_command_pending
                or self.automation_engine._temp_command_pending
            )
            if _is_expected_confirmation:
                _LOGGER.debug(
                    "Skipping pause-override: thermostat confirmed automation command (mode=%s, commanded %.1fs ago)",
                    _last_cmd_mode,
                    (dt_util.now() - _last_cmd_time).total_seconds(),
                )
            elif not _any_command_pending and not self._is_recent_hvac_command(threshold_seconds=3.0):
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
                    "(hvac_pending=%s, fan_pending=%s, temp_pending=%s, recent_command=%s)",
                    self.automation_engine._hvac_command_pending,
                    self.automation_engine._fan_command_pending,
                    self.automation_engine._temp_command_pending,
                    self._is_recent_hvac_command(threshold_seconds=3.0),
                )
        elif (
            # Fix D (Issue #282): mode change to a DIFFERENT mode while grace is active.
            # The existing elif below guards with `not _manual_override_active`, so this
            # branch fires first and handles the re-override case.
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and self.automation_engine._manual_override_active
            and new_state.state != self.automation_engine._manual_override_mode
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self.automation_engine._temp_command_pending
            and not self._is_recent_hvac_command()
            and not _is_expected_confirmation
        ):
            # User switched to a different mode while grace was running — clear the
            # old override and register the new one so the grace period restarts.
            _LOGGER.info(
                "New mode change during active override grace: %s → was overriding %s — restarting",
                new_state.state,
                self.automation_engine._manual_override_mode,
            )
            self.automation_engine.clear_manual_override(reason="new_override_during_grace")
            self.automation_engine.handle_manual_override(
                old_mode=old_state.state,
                new_mode=new_state.state,
                classification_mode=(self._current_classification.hvac_mode if self._current_classification else None),
            )
        elif (
            old_state.state != new_state.state
            and new_state.state not in ("unavailable", "unknown")
            and not self.automation_engine._manual_override_active
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self.automation_engine._temp_command_pending
            and not self._is_recent_hvac_command()
            and not _is_expected_confirmation
            and self._current_classification
            and new_state.state
            != (
                self.automation_engine._last_commanded_hvac_mode
                or (self._current_classification.hvac_mode if self._current_classification else None)
            )
        ):
            # Mode changed outside of door/window pause to something
            # different from what CA is actively controlling — manual override
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
                _chart_hvac_ov = self._read_chart_hvac_action()
                _LOGGER.debug(
                    "chart_log append: event=override hvac=%r fan=%s",
                    _chart_hvac_ov,
                    self._fan_is_running(),
                )
                self._chart_log.append(
                    hvac=_chart_hvac_ov,
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
                    fan_running=self._fan_physically_running(),
                    nat_vent_active=bool(
                        self.automation_engine._natural_vent_active if self.automation_engine else False
                    ),
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

        _LOGGER.info(
            "_async_thermostat_changed: hvac action=%s was_running=%s is_running=%s",
            new_action,
            was_running,
            is_running,
        )

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
                await self._start_hvac_observation(session_mode)
        elif was_running and not is_running:
            # HVAC just turned off — flush runtime and end active phase
            self._flush_hvac_runtime()
            for _hvac_ot in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                self._end_hvac_active_phase(_hvac_ot)
            self._hvac_on_since = None
            self.hass.async_create_task(self._async_save_state())
        elif was_running and is_running and old_action != new_action:
            # heat_cool mode: hvac_action switched heating↔cooling mid-session
            if old_action in running_actions and new_action in running_actions:
                _LOGGER.info(
                    "heat_cool mid-session switch %s → %s: abandoning current event",
                    old_action,
                    new_action,
                )
                for _hvac_ot in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                    self._abandon_observation(_hvac_ot, "heat_cool mode switch mid-session")
                new_session_mode = "heat" if new_action == "heating" else "cool"
                await self._start_hvac_observation(new_session_mode)

        # Issue #347: Post-startup reconcile for thermostat-autonomous fan-on.
        # When hvac_action transitions to "fan" (e.g. thermostat fan-circulation between
        # AC cycles) outside the startup coalesce window, and CA does not own the fan,
        # enforce the invariant: a running fan always has an explicit owner — adopt as
        # nat-vent or turn off, never indefinite limbo.
        # Guard: skip if fan_mode also changed in this same event — that signals a
        # user action whose override detection runs in the §9b block below (line ~3004).
        _old_fan_mode_347 = old_state.attributes.get("fan_mode", "")
        _new_fan_mode_347 = new_state.attributes.get("fan_mode", "")
        _ae_347 = self.automation_engine
        if (
            old_action != "fan"
            and new_action == "fan"
            and _old_fan_mode_347 == _new_fan_mode_347
            and not _ae_347._fan_active
            and not _ae_347._natural_vent_active
            and not _ae_347._fan_override_active
        ):
            _LOGGER.info(
                "hvac_action transitioned to fan while CA does not own fan — "
                "trigger=post_startup_reconcile old_action=%s",
                old_action,
            )
            await _ae_347.reconcile_fan_on_startup(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                thermostat_fan_running=True,
                any_sensor_open=self._any_sensor_open(),
            )

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
                _LOGGER.debug(
                    "chart_log append: event=hvac_action_change hvac=%r fan=%s",
                    new_action,
                    self._fan_is_running(),
                )
                self._chart_log.append(
                    hvac=new_action,
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
                    fan_running=self._fan_physically_running(),
                    nat_vent_active=bool(
                        self.automation_engine._natural_vent_active if self.automation_engine else False
                    ),
                )
                self._chart_log.save()

        # Bug 3 fix: Event-driven sampling for active HVAC observations.
        # The 5-min polling tick (_sample_all_observations) can miss short HVAC cycles
        # (<5 min) entirely, leaving active_samples with only the 1 initial sample.
        # With n=1 there are 0 consecutive pairs — OLS cannot run and k_active is never
        # fitted.  Adding a sample here on every thermostat state change (temperature
        # update, attribute change) during an active HVAC session ensures short cycles
        # accumulate enough samples for OLS.
        # Guard: only sample if HVAC is still actively heating/cooling (same phase),
        # and at least 60 seconds have elapsed since the last sample to avoid flooding.
        if new_action in ("heating", "cooling") and old_action == new_action:
            _active_obs_type = OBS_TYPE_HVAC_HEAT if new_action == "heating" else OBS_TYPE_HVAC_COOL
            self._ensure_pending_observations()
            _active_obs = self._pending_observations.get(_active_obs_type)
            _obs_phase_ok = _active_obs is not None and _active_obs.get("_phase") == "active"
            if _obs_phase_ok and _active_obs.get("status") == "monitoring":
                _active_start_str = _active_obs.get("active_start")
                try:
                    _active_start_ts = dt_util.parse_datetime(_active_start_str) if _active_start_str else None
                    _elapsed_active = (
                        (dt_util.now() - _active_start_ts).total_seconds() / 60.0 if _active_start_ts else 0.0
                    )
                except Exception:
                    _elapsed_active = 0.0
                # Decimation gate: at least 60 s between event-driven samples
                _last_evt_str = _active_obs.get("last_event_sample_time")
                _elapsed_since_last = 61.0  # default: allow first sample
                if _last_evt_str:
                    try:
                        _last_evt_ts = dt_util.parse_datetime(_last_evt_str)
                        if _last_evt_ts:
                            _elapsed_since_last = (dt_util.now() - _last_evt_ts).total_seconds()
                    except Exception:
                        pass
                if _elapsed_since_last >= 60.0:
                    _evt_sample = self._get_current_sample(_elapsed_active)
                    _active_samples = _active_obs.get("active_samples", [])
                    from custom_components.climate_advisor.const import (
                        THERMAL_MAX_ACTIVE_SAMPLES as _THERMAL_MAX_ACTIVE,
                    )

                    if len(_active_samples) < _THERMAL_MAX_ACTIVE:
                        _active_samples.append(_evt_sample)
                        _active_obs["last_event_sample_time"] = dt_util.now().isoformat()
                        _ind = _evt_sample.get("indoor_temp_f")
                        _cur_peak = _active_obs.get("peak_indoor_f")
                        if _ind and (_cur_peak is None or _ind > _cur_peak):
                            _active_obs["peak_indoor_f"] = _ind
                        _LOGGER.debug(
                            "Event-driven HVAC sample added: type=%s n_active=%d elapsed=%.1fmin",
                            _active_obs_type,
                            len(_active_samples),
                            _elapsed_active,
                        )

        # Detect manual override: temperature changed but not by us
        # In heat_cool mode the thermostat exposes target_temp_high/target_temp_low, not temperature.
        # _setpoint_override_detected gates Block 3 (fan_mode): a single thermostat event that
        # includes BOTH a setpoint change AND a fan_mode change must only fire the setpoint path.

        # Issue #359 Fix A: compute fan-cancel flag BEFORE Block 2 so it can guard setpoint
        # override detection.  When an ecobee user turns the fan off, the thermostat simultaneously
        # restores its comfort-program setpoint — Block 2 would otherwise misread that as a manual
        # setpoint override and start a grace period that blocks CA's intended setpoint.
        _b2_old_fan_mode = old_state.attributes.get("fan_mode")
        _b2_new_fan_mode = new_state.attributes.get("fan_mode")
        _fan_cancel_in_this_event = (
            _b2_old_fan_mode is not None
            and _b2_old_fan_mode == "on"
            and _b2_new_fan_mode is not None
            and _b2_new_fan_mode != "on"
        )

        _setpoint_override_detected = False
        if new_state.state == "heat_cool":
            _new_high = new_state.attributes.get("target_temp_high")
            _old_high = old_state.attributes.get("target_temp_high")
            _new_low = new_state.attributes.get("target_temp_low")
            _old_low = old_state.attributes.get("target_temp_low")
            _setpoint_changed = (_new_high != _old_high) or (_new_low != _old_low)
            # Use the cooling (high) setpoint as the representative value for override_details
            new_temp, old_temp = _new_high, _old_high
        else:
            new_temp = new_state.attributes.get("temperature")
            old_temp = old_state.attributes.get("temperature")
            _setpoint_changed = new_temp != old_temp

        if (
            _setpoint_changed
            and self._today_record
            and not self.automation_engine._temp_command_pending
            and not self.automation_engine._hvac_command_pending
            and not self.automation_engine._fan_command_pending
            and not self._is_recent_hvac_command(threshold_seconds=30.0)
            and not self._is_recent_temp_command(threshold_seconds=30.0)
            and not self._is_recent_fan_command(threshold_seconds=30.0)
            and not _fan_cancel_in_this_event  # Issue #359 Fix A: fan-off echo suppresses grace
        ):
            # Mark setpoint detection as fired so Block 3 (fan_mode) is suppressed for this event.
            # A single event that changes both setpoint and fan_mode has one root cause; two
            # simultaneous grace periods from one event would confuse the automation engine.
            _setpoint_override_detected = True
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
            # Setpoint-only override: mode matches what CA is actively controlling.
            # Use _last_commanded_hvac_mode so heat_cool mode is handled (classification.hvac_mode
            # is always "cool"/"heat"/"off" — never "heat_cool" — so the old check missed heat_cool).
            ae = self.automation_engine
            _ca_active_mode = ae._last_commanded_hvac_mode or (
                self._current_classification.hvac_mode if self._current_classification else None
            )
            if (
                not ae._manual_override_active
                and not ae._override_confirm_pending
                and self._current_classification is not None
                and new_state.state == _ca_active_mode
            ):
                _LOGGER.info(
                    "Setpoint-only manual override detected: %s -> %s (mode=%s matches CA active mode)",
                    old_temp,
                    new_temp,
                    new_state.state,
                )
                ae.handle_manual_override(
                    source="setpoint",
                    old_mode=old_state.state,
                    new_mode=new_state.state,
                    classification_mode=(
                        self._current_classification.hvac_mode if self._current_classification else None
                    ),
                    old_setpoint_f=old_temp,
                    new_setpoint_f=new_temp,
                )
        elif _fan_cancel_in_this_event and _setpoint_changed:
            # Issue #359 Fix A: fan-off echo branch — the thermostat restored its comfort-program
            # setpoint as a side-effect of the fan being turned off (ecobee behavior).  Do NOT start
            # a grace period.  Instead, schedule a re-assertion so CA's intended setpoint wins after
            # the thermostat settles.
            _LOGGER.info(
                "Setpoint override suppressed: fan-off echo detected, scheduling re-assertion (thermostat=%s)",
                new_temp,
            )
            self.hass.async_create_task(self._async_reassert_setpoint_after_fan_off())

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
            and not _is_expected_confirmation
            and not self._is_recent_fan_command(threshold_seconds=30.0)
            and not _setpoint_override_detected
        ):
            _fan_ct = self.automation_engine._fan_command_time
            try:
                _fan_cmd_age = (
                    f"{(dt_util.now() - _fan_ct).total_seconds():.0f}s ago" if _fan_ct is not None else "None"
                )
            except (TypeError, AttributeError):
                _fan_cmd_age = "None"
            try:
                _hvac_cmd_age = (
                    f"{(dt_util.now() - _last_cmd_time).total_seconds():.0f}s ago"
                    if _last_cmd_time is not None
                    else "None"
                )
            except (TypeError, AttributeError):
                _hvac_cmd_age = "None"
            _LOGGER.info(
                "Manual HVAC fan_mode change detected: %s -> %s (fan_cmd=%s, hvac_cmd=%s, expected_confirmation=%s)",
                old_fan_mode,
                new_fan_mode,
                _fan_cmd_age,
                _hvac_cmd_age,
                _is_expected_confirmation,
            )
            # Issue #359 Fix B: direction-aware dispatch — fan-off routes to on_fan_turned_off()
            # (clears fan state, gates nat-vent re-activation) instead of handle_fan_manual_override()
            # (which sets the "user turned fan ON" override flag).
            if _fan_cancel_in_this_event:
                self.automation_engine.on_fan_turned_off(fan_before=str(old_fan_mode), fan_after=str(new_fan_mode))
            else:
                self.automation_engine.handle_fan_manual_override(
                    fan_before=str(old_fan_mode), fan_after=str(new_fan_mode)
                )

    async def _async_command_fan_entity(self, *, on: bool) -> None:
        """Issue a turn_on or turn_off service call to the configured WHF fan entity (Issue #361).

        Used by command-only reconciliation; reuses the same domain-split pattern as
        automation.py ``_activate_fan()`` / ``_deactivate_fan()``.
        """
        fan_entity_id = self.config.get(CONF_FAN_ENTITY)
        if not fan_entity_id:
            _LOGGER.debug("_async_command_fan_entity: no fan_entity configured — skipping")
            return
        domain = fan_entity_id.split(".")[0]  # "fan" or "switch"
        service = "turn_on" if on else "turn_off"
        _LOGGER.debug(
            "_async_command_fan_entity: %s.%s entity_id=%s",
            domain,
            service,
            fan_entity_id,
        )
        await self.hass.services.async_call(domain, service, {"entity_id": fan_entity_id})

    async def _async_fan_entity_changed(self, event: Event) -> None:
        """Detect manual fan entity state changes (Issue #37)."""
        # Issue #361: command-only mode — entity state changes are command echoes, not physical signals.
        if not self._fan_state_feedback_enabled():
            _LOGGER.debug(
                "fan_entity state change ignored — fan_state_feedback=False"
                " (command echo only, not a physical override signal)"
            )
            return

        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state or not old_state:
            return

        if new_state.state == old_state.state:
            return

        # Skip if this change was initiated by us
        if self.automation_engine._fan_command_pending:
            return

        # Skip if fan override is already active — but a physical-state confirmation
        # event (e.g. the fan_state_entity flipping on right after fan_entity did) still
        # needs to reach the displayed status promptly, otherwise it stays stale until
        # the next scheduled coordinator poll (up to update_interval, currently 30 min).
        if self.automation_engine._fan_override_active:
            _LOGGER.info(
                "Fan/state entity changed while override already active (%s -> %s) — "
                "requesting refresh so displayed status reflects confirmed physical state",
                old_state.state,
                new_state.state,
            )
            await self.async_request_refresh()
            return

        # Skip if a fan command was issued recently (cloud thermostat echo guard)
        if self._is_recent_fan_command(threshold_seconds=30.0):
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
            self.automation_engine.handle_fan_manual_override(
                fan_before=str(old_state.state), fan_after=str(new_state.state)
            )
        elif not is_on and self.automation_engine._fan_active:
            # Fan turned off externally — route to on_fan_turned_off() to clear fan state and
            # gate nat-vent re-activation (Issue #359 Fix C).  handle_fan_manual_override() is
            # the "user turned fan ON" path and must NOT be called here.
            _LOGGER.info(
                "Fan turned off externally: %s -> %s (integration expected fan on)",
                old_state.state,
                new_state.state,
            )
            self.automation_engine.on_fan_turned_off(fan_before=str(old_state.state), fan_after=str(new_state.state))

    async def _async_reassert_setpoint_after_fan_off(self) -> None:
        """Re-assert CA's intended setpoint after an ecobee fan-off echo (Issue #359 Fix A).

        Ecobee simultaneously restores its comfort-program setpoint when the user turns the fan
        off.  We wait 5 s for the thermostat to settle, then push CA's current classification
        back so the comfort-program setpoint does not win.
        """
        await asyncio.sleep(5)
        try:
            classification = self._current_classification
            if classification is None:
                _LOGGER.warning("Setpoint re-assertion after fan-off: no current classification — skipping")
                return
            await self.automation_engine.apply_classification(
                classification,
                predicted_indoor=self._last_predicted_indoor,
                indoor_temp=self._get_indoor_temp(),
            )
            _LOGGER.info(
                "Setpoint re-asserted after fan-off echo: reasserted day_type=%s hvac_mode=%s",
                classification.day_type,
                classification.hvac_mode,
            )
        except Exception:
            _LOGGER.warning(
                "Setpoint re-assertion after fan-off failed — thermostat left as-is",
                exc_info=True,
            )

    @callback
    def _on_post_grace_fan_check(self) -> None:
        """Called by automation engine after any grace period expires (Issue #359 Fix D).

        Schedules a fan-reconciliation check so an untracked fan is caught as soon as
        grace clears rather than waiting for the next 30-min coordinator cycle.
        """
        self.hass.async_create_task(self._async_post_grace_fan_reconcile())

    async def _async_post_grace_fan_reconcile(self) -> None:
        """After grace expires, check if fan is still running and reconcile if needed (Issue #359 Fix D)."""
        # Issue #361: command-only mode — no physical-state entity to read.
        # Reset last_commanded so the next _async_update_data() cycle re-asserts the desired state.
        if not self._fan_state_feedback_enabled():
            ae = self.automation_engine
            if ae is not None:
                desired = bool(ae._fan_active)
                _LOGGER.info(
                    "Post-grace fan reconcile (command-only): asserting desired_state=%s"
                    " (feedback unavailable — will re-assert on next cycle)",
                    "on" if desired else "off",
                )
                self._last_commanded_fan_state = None
            return

        ae = self.automation_engine
        if ae is None:
            return
        _cs_pg = self.hass.states.get(self.config.get("climate_entity", ""))
        if _cs_pg is None:
            return
        fan_mode = str(_cs_pg.attributes.get("fan_mode", ""))
        hvac_action = str(_cs_pg.attributes.get("hvac_action", "")).lower()
        thermostat_fan_running = fan_mode == "on" or hvac_action == "fan"
        _LOGGER.info(
            "Post-grace fan check: fan_mode=%s hvac_action=%s fan_running=%s",
            fan_mode,
            hvac_action,
            thermostat_fan_running,
        )
        if thermostat_fan_running and hvac_action not in ("heating", "cooling"):
            await ae.reconcile_fan_on_startup(
                indoor=self._get_indoor_temp(),
                outdoor=self._last_outdoor_temp,
                thermostat_fan_running=True,
                any_sensor_open=self._any_sensor_open(),
            )

    def _fan_state_feedback_enabled(self) -> bool:
        """Return True if the fan entity provides reliable physical-state feedback (Issue #361)."""
        return bool(self.config.get(CONF_FAN_STATE_FEEDBACK, False))

    def _get_fan_physical_state(self) -> bool | None:
        """Return whether the fan is physically running, or None if feedback is disabled (Issue #361).

        When ``fan_state_feedback`` is False (command-only mode), returns None — the entity
        state only echoes the last command and cannot be used for override detection.

        When ``CONF_FAN_STATE_ENTITY`` is configured and feedback is enabled, reads that entity's
        state for physical on/off detection.  Falls back to ``CONF_FAN_ENTITY`` state if the
        state entity is unavailable or not configured.  Logs a WARNING (once per unavailability)
        when falling back.

        Returns:
            True/False if the fan is physically running (feedback mode), None if command-only.
        """
        if not self._fan_state_feedback_enabled():
            _LOGGER.debug("_get_fan_physical_state: returning None — fan_state_feedback=False (command-only mode)")
            return None
        fan_state_entity_id = self.config.get(CONF_FAN_STATE_ENTITY)
        if fan_state_entity_id:
            state = self.hass.states.get(fan_state_entity_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                self._fan_state_entity_unavailable_warned = False  # reset on success
                return state.state.lower() in ("on", "true")
            # Unavailable or missing — warn once then fall back
            if not self._fan_state_entity_unavailable_warned:
                _LOGGER.warning(
                    "Fan state entity %s is unavailable — falling back to fan command entity for physical state",
                    fan_state_entity_id,
                )
                self._fan_state_entity_unavailable_warned = True
        # Fallback: read the fan command entity
        fan_entity_id = self.config.get(CONF_FAN_ENTITY)
        if fan_entity_id:
            fan_state = self.hass.states.get(fan_entity_id)
            if fan_state is not None:
                return fan_state.state.lower() in ("on", "true")
        return False

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
            await self._start_hvac_observation(session_mode)

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

    # ------------------------------------------------------------------
    # Thermal observation pipeline v3 (multi-type obs)
    # ------------------------------------------------------------------

    def _ensure_pending_observations(self) -> None:
        """Lazily initialize _pending_observations if missing (e.g. test stubs)."""
        if not hasattr(self, "_pending_observations"):
            self._pending_observations = {}

    async def _start_hvac_observation(self, session_mode: str) -> None:
        """Begin a new HVAC thermal observation (heat or cool)."""
        self._ensure_pending_observations()
        if not self.config.get("learning_enabled", True):
            return
        if not hasattr(self, "learning"):
            return
        obs_type = OBS_TYPE_HVAC_HEAT if session_mode == "heat" else OBS_TYPE_HVAC_COOL

        # Abandon any active non-HVAC observations — HVAC start contaminates them
        for _contaminated in (
            OBS_TYPE_PASSIVE_DECAY,
            OBS_TYPE_FAN_ONLY_DECAY,
            OBS_TYPE_VENTILATED_DECAY,
            OBS_TYPE_SOLAR_GAIN,
        ):
            if _contaminated in self._pending_observations:
                self._commit_observation_if_sufficient(_contaminated, "hvac_started")

        if obs_type in self._pending_observations:
            self._abandon_observation(obs_type, "new HVAC session started")

        _LOGGER.info(
            "_start_hvac_observation: type=%s starting (prior obs=%s)",
            obs_type,
            list(self._pending_observations.keys()),
        )

        now = dt_util.now()
        pre_samples = []
        for s in self._pre_heat_sample_buffer:
            try:
                ts = dt_util.parse_datetime(s["timestamp"])
                elapsed = (now - ts).total_seconds() / 60.0 if ts else 0.0
            except Exception:
                elapsed = 0.0
            pre_samples.append(
                {
                    "timestamp": s["timestamp"],
                    "indoor_temp_f": s["indoor_temp_f"],
                    "outdoor_temp_f": s["outdoor_temp_f"],
                    "elapsed_minutes": -elapsed,
                }
            )

        indoor = self._get_indoor_temp()
        import uuid as _uuid_mod

        obs: dict = {
            "obs_type": obs_type,
            "obs_id": str(_uuid_mod.uuid4()),
            "start_time": now.isoformat(),
            "status": "monitoring",
            # NOTE: HVAC obs intentionally omit 'samples' key.  Non-HVAC (passive, fan,
            # vent, solar) use 'samples'.  HVAC obs use 'active_samples' (active phase)
            # and 'post_heat_samples' (post-heat phase).  Adding a 'samples': [] here
            # would shadow active_samples in every fallback read that uses
            # obs.get('samples', obs.get('active_samples', [])), causing n=0 in
            # rejection logs and discarding all HVAC obs on restart. (Bug 1 fix)
            "flags_at_start": {},
            "schema_version": 1,
            # HVAC-specific fields (compatible with _commit_event_from_dict HVAC path)
            "event_id": str(_uuid_mod.uuid4()),
            "created_at": now.isoformat(),
            "hvac_mode": session_mode,
            "session_mode": session_mode,
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
            "_phase": "active",
        }
        first_sample = self._get_current_sample(0.0)
        obs["active_samples"].append(first_sample)
        obs["start_outdoor_f"] = first_sample["outdoor_temp_f"]

        # Capture setpoint for diagnostic storage — not used in swing formula
        _climate_id = self.config.get("climate_entity", "")
        _cs_sw = self.hass.states.get(_climate_id) if _climate_id else None
        if _cs_sw is not None:
            _sp = _cs_sw.attributes.get("target_temperature")
            if _sp is None:
                _sp = _cs_sw.attributes.get("target_temp_low" if session_mode == "heat" else "target_temp_high")
            if _sp is not None:
                with contextlib.suppress(ValueError, TypeError):
                    obs["setpoint_f"] = round(float(_sp), 1)

        self._pending_observations[obs_type] = obs
        await self.hass.async_add_executor_job(self.learning.save_state)
        _LOGGER.info(
            "Thermal HVAC observation started: obs_id=%s mode=%s indoor=%.1f°F",
            obs["obs_id"],
            session_mode,
            indoor if indoor is not None else 0.0,
        )

    def _sample_all_observations(self) -> None:
        """Sample all active observations and check trigger conditions for new ones."""
        self._ensure_pending_observations()
        if not self.config.get("learning_enabled", True):
            return
        if not hasattr(self, "learning"):
            return

        indoor = self._get_indoor_temp()
        outdoor = getattr(self, "_last_outdoor_temp", None)

        # Issue #130 D16: Use last-known outdoor temp if current reading is unavailable.
        # Outdoor temp changes slowly; a 30-min-stale reading is accurate to ±2°F —
        # sufficient for trigger gating and OLS.  Better than skipping samples entirely.
        if outdoor is None:
            _last_known = getattr(self, "_last_known_outdoor_f", None)
            _last_known_ts = getattr(self, "_last_known_outdoor_ts", None)
            if (
                _last_known is not None
                and _last_known_ts is not None
                and (dt_util.now() - _last_known_ts).total_seconds() < 1800  # 30 min
            ):
                outdoor = _last_known
        if outdoor is not None and outdoor != getattr(self, "_last_known_outdoor_f", None):
            self._last_known_outdoor_f = outdoor
            self._last_known_outdoor_ts = dt_util.now()

        ae = self.automation_engine

        now = dt_util.now()

        # A. Sample all active observations
        for obs_type, obs in list(self._pending_observations.items()):
            if obs.get("status") != "monitoring":
                continue

            if indoor is None:
                continue

            if obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                phase = obs.get("_phase", "active")
                active_start_str = obs.get("active_start")
                try:
                    active_start = dt_util.parse_datetime(active_start_str) if active_start_str else now
                except Exception:
                    active_start = now
                elapsed = (now - active_start).total_seconds() / 60.0

                sample = self._get_current_sample(elapsed)
                if phase == "active":
                    samples = obs["active_samples"]
                    if len(samples) < THERMAL_MAX_ACTIVE_SAMPLES:
                        samples.append(sample)
                    cur_peak = obs.get("peak_indoor_f")
                    if indoor and (cur_peak is None or indoor > cur_peak):
                        obs["peak_indoor_f"] = indoor
                else:  # post_heat
                    samples = obs["post_heat_samples"]
                    if len(samples) < THERMAL_MAX_POST_HEAT_SAMPLES:
                        samples.append(sample)
            else:
                # passive/fan/vent/solar: append to samples list
                elapsed = 0.0
                start_str = obs.get("start_time")
                if start_str:
                    try:
                        start_ts = dt_util.parse_datetime(start_str)
                        if start_ts:
                            elapsed = (now - start_ts).total_seconds() / 60.0
                    except Exception:
                        pass
                sample = self._get_current_sample(elapsed)
                samples_list = obs.setdefault("samples", [])
                if len(samples_list) >= THERMAL_MAX_OBS_SAMPLES:
                    self._commit_observation_if_sufficient(obs_type, "max_samples_reached")
                else:
                    # H1: per-type decimation gate — slow phenomena at full poll rate yield noise
                    _interval_map = {
                        OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
                        OBS_TYPE_FAN_ONLY_DECAY: THERMAL_FAN_SAMPLE_INTERVAL_S,
                        OBS_TYPE_VENTILATED_DECAY: THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
                        OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_SAMPLE_INTERVAL_S,
                    }
                    _interval_s = _interval_map.get(obs_type, 0)
                    _last_s = obs.get("last_sample_time")
                    _elapsed_since_last = (
                        (now - dt_util.parse_datetime(_last_s)).total_seconds() if _last_s else _interval_s + 1
                    )
                    if _elapsed_since_last >= _interval_s:
                        if obs_type == OBS_TYPE_VENTILATED_DECAY:
                            _sf_offset = getattr(self, "_solar_phase_offset", THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT)
                            sample["solar_factor"] = _solar_factor(now.hour, _sf_offset)
                        samples_list.append(sample)
                        obs["last_sample_time"] = now.isoformat()

        # B. Check trigger conditions for new non-HVAC observations
        _hvac_active = (
            OBS_TYPE_HVAC_HEAT in self._pending_observations
            and self._pending_observations[OBS_TYPE_HVAC_HEAT].get("_phase") == "active"
        ) or (
            OBS_TYPE_HVAC_COOL in self._pending_observations
            and self._pending_observations[OBS_TYPE_HVAC_COOL].get("_phase") == "active"
        )
        # Also check live HVAC action from thermostat
        _cs = self.hass.states.get(self.config["climate_entity"])
        _hvac_action_str = _cs.attributes.get("hvac_action", "").lower() if _cs else ""
        _is_heating_cooling = _hvac_action_str in ("heating", "cooling")
        _fan_active = ae._fan_active or ae._natural_vent_active
        _sensor_open = self._any_sensor_open()

        if indoor is not None and outdoor is not None:
            _delta = abs(indoor - outdoor)

            _LOGGER.debug(
                "Thermal trigger eval: indoor=%.1f outdoor=%.1f delta=%.1f "
                "fan=%s nat_vent=%s sensor_open=%s hvac_action=%s pending=%s",
                indoor,
                outdoor,
                _delta,
                ae._fan_active,
                ae._natural_vent_active,
                _sensor_open,
                _hvac_action_str,
                list(self._pending_observations.keys()),
            )

            if (
                OBS_TYPE_PASSIVE_DECAY not in self._pending_observations
                and not _is_heating_cooling
                and not _hvac_active
                and not _fan_active
                and not _sensor_open
                and _delta >= THERMAL_PASSIVE_MIN_DELTA_F
            ):
                self._start_decay_observation(OBS_TYPE_PASSIVE_DECAY)

            _fan_only_mode = _cs.state == "fan_only" if _cs else False
            if (
                OBS_TYPE_FAN_ONLY_DECAY not in self._pending_observations
                and (_fan_only_mode or ae._fan_active)
                and not _is_heating_cooling
                and not _sensor_open
            ):
                self._start_decay_observation(OBS_TYPE_FAN_ONLY_DECAY)

            if (
                OBS_TYPE_VENTILATED_DECAY not in self._pending_observations
                and _sensor_open
                and not _is_heating_cooling
                and _delta >= THERMAL_VENTILATED_MIN_DELTA_F
            ):
                self._start_decay_observation(OBS_TYPE_VENTILATED_DECAY)

            _hour = now.hour
            if (
                OBS_TYPE_SOLAR_GAIN not in self._pending_observations
                and not _is_heating_cooling
                and not _fan_active
                and not _sensor_open
                and THERMAL_SOLAR_DAYTIME_START_H <= _hour < THERMAL_SOLAR_DAYTIME_END_H
            ):
                self._start_decay_observation(OBS_TYPE_SOLAR_GAIN)

        # C. Check commit/abandon conditions for each monitoring observation
        for obs_type in list(self._pending_observations.keys()):
            obs = self._pending_observations.get(obs_type)
            if obs is None or obs.get("status") != "monitoring":
                continue

            if obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                # HVAC stabilization is handled by _check_hvac_stabilization
                continue

            samples_list = obs.get("samples", [])

            if obs_type == OBS_TYPE_PASSIVE_DECAY:
                # Issue #137: consecutive-pair OLS replaced by chart_log endpoint estimator.
                # passive_decay observation tracks passive conditions (no HVAC/fan/sensors);
                # when it ends, trigger the chart_log fit rather than running OLS.
                if _is_heating_cooling or _hvac_active:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "hvac_started")
                elif _sensor_open:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "sensor_opened")
                elif _fan_active:
                    self._run_passive_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "fan_activated")
                elif indoor is not None and outdoor is not None and abs(indoor - outdoor) < THERMAL_PASSIVE_MIN_DELTA_F:
                    recent_temps = [s["indoor_temp_f"] for s in samples_list[-5:]] if len(samples_list) >= 5 else []
                    if recent_temps and (max(recent_temps) - min(recent_temps)) < 0.1:
                        self._run_passive_chart_log_fit(backfill=False)
                        self._abandon_observation(obs_type, "equilibrium_reached")
                else:
                    _max_samples = THERMAL_ROLLING_MAX_WINDOW_MINUTES // (THERMAL_PASSIVE_SAMPLE_INTERVAL_S // 60)
                    if len(samples_list) >= _max_samples:
                        # Hard time cap reached — trigger fit and end observation
                        self._run_passive_chart_log_fit(backfill=False)
                        self._abandon_observation(obs_type, "max_window_reached")

            elif obs_type == OBS_TYPE_FAN_ONLY_DECAY:
                # Two-threshold accumulation: signal = indoor sample range (max-min).
                # Uses indoor movement, not snapshot differential, so keep-alive fires when
                # the integer thermostat is flat even with a large indoor-outdoor gap.
                _fan_temps = [s["indoor_temp_f"] for s in samples_list if "indoor_temp_f" in s]
                _fan_signal_sufficient = (
                    (max(_fan_temps) - min(_fan_temps)) >= THERMAL_ROLLING_MIN_DELTA_T_F if _fan_temps else False
                )
                if self._evaluate_rolling_window(obs_type, obs, _fan_signal_sufficient, skip_delta_guard=True):
                    continue
                _fan_only_mode = _cs.state == "fan_only" if _cs else False
                _fan_still_on = _fan_only_mode or ae._fan_active
                if not _fan_still_on and not (_cs and _cs.state == "fan_only"):
                    self._commit_observation_if_sufficient(obs_type, "fan_stopped")
                elif _sensor_open:
                    self._abandon_observation(obs_type, "sensor_opened")
                elif _is_heating_cooling or _hvac_active:
                    self._abandon_observation(obs_type, "hvac_started")
                elif (
                    len(samples_list) >= THERMAL_FAN_MIN_SAMPLES
                    and indoor is not None
                    and outdoor is not None
                    and abs(indoor - outdoor) >= THERMAL_FAN_MIN_SIGNAL_F
                ):
                    self._commit_observation_if_sufficient(obs_type, "insufficient_signal")

            elif obs_type == OBS_TYPE_VENTILATED_DECAY:
                # Two-threshold accumulation: signal = indoor sample range (max-min).
                # Uses indoor movement, not snapshot differential, so keep-alive fires when
                # the integer thermostat is flat even with a large indoor-outdoor gap.
                _vent_temps = [s["indoor_temp_f"] for s in samples_list if "indoor_temp_f" in s]
                _vent_signal_sufficient = (
                    (max(_vent_temps) - min(_vent_temps)) >= THERMAL_ROLLING_MIN_DELTA_T_F if _vent_temps else False
                )
                # Solar accumulation guard: during daytime, suppress early commit if
                # sf_range has not yet reached the 2-param OLS threshold.  Without this,
                # obs commits at 30 min (sf_range ≈ 0.05–0.15) before 2-param can fire,
                # producing ols_wrong_sign rejections on solar-gain mornings.
                # The 240-min hard cap in _evaluate_rolling_window fires normally.
                _sf_vals_vent = [s.get("solar_factor", 0.0) for s in samples_list if "solar_factor" in s]
                _sf_range_vent = max(_sf_vals_vent) - min(_sf_vals_vent) if len(_sf_vals_vent) >= 2 else 0.0
                if 8 <= now.hour < 18 and _sf_range_vent < THERMAL_SOLAR_FACTOR_MIN_RANGE:
                    _vent_signal_sufficient = False
                if self._evaluate_rolling_window(obs_type, obs, _vent_signal_sufficient, skip_delta_guard=True):
                    continue
                if not _sensor_open:
                    # Sensors closed: run OLS commit (morning windows) AND chart_log endpoint
                    # fit (overnight windows). Natural filter in the endpoint fit auto-rejects
                    # morning windows where T_out crossed T_in.
                    self._commit_observation_if_sufficient(obs_type, "sensors_closed")
                    self._run_ventilated_chart_log_fit(backfill=False)
                elif _is_heating_cooling or _hvac_active:
                    self._run_ventilated_chart_log_fit(backfill=False)
                    self._abandon_observation(obs_type, "hvac_started")
                elif (
                    len(samples_list) >= THERMAL_VENT_MIN_SAMPLES
                    and indoor is not None
                    and outdoor is not None
                    and abs(indoor - outdoor) >= THERMAL_VENT_MIN_SIGNAL_F
                ):
                    self._commit_observation_if_sufficient(obs_type, "insufficient_signal")

            elif obs_type == OBS_TYPE_SOLAR_GAIN:
                # Two-threshold accumulation (Issue #126): signal = indoor ΔT sufficient
                _sg_temps = [s["indoor_temp_f"] for s in samples_list if "indoor_temp_f" in s]
                _sg_signal = (max(_sg_temps) - min(_sg_temps)) >= THERMAL_ROLLING_MIN_DELTA_T_F if _sg_temps else False
                if self._evaluate_rolling_window(obs_type, obs, _sg_signal, skip_delta_guard=False):
                    continue
                if _is_heating_cooling or _hvac_active:
                    self._abandon_observation(obs_type, "hvac_started")
                elif _sensor_open:
                    self._abandon_observation(obs_type, "sensor_opened")
                elif _fan_active:
                    self._abandon_observation(obs_type, "fan_activated")
                elif not (THERMAL_SOLAR_DAYTIME_START_H <= now.hour < THERMAL_SOLAR_DAYTIME_END_H):
                    self._abandon_observation(obs_type, "outside_daytime")
                elif len(samples_list) >= 5:
                    recent_indoor = [s["indoor_temp_f"] for s in samples_list[-5:]]
                    # Only abandon if 3+ consecutive samples are each lower than the previous
                    # (guards against brief cloud-pass dips triggering premature abandonment)
                    _falling_streak = sum(
                        1 for i in range(1, len(recent_indoor)) if recent_indoor[i] < recent_indoor[i - 1]
                    )
                    if _falling_streak >= 3:
                        self._commit_observation_if_sufficient(obs_type, "temperature_falling")
                    elif len(samples_list) >= THERMAL_SOLAR_MIN_SAMPLES and indoor is not None:
                        _first_ts = dt_util.parse_datetime(samples_list[0]["timestamp"])
                        elapsed_h = (now - _first_ts).total_seconds() / 3600.0 if _first_ts else 0.0
                        if elapsed_h > 0:
                            mean_rate = (
                                samples_list[-1]["indoor_temp_f"] - samples_list[0]["indoor_temp_f"]
                            ) / elapsed_h
                            if mean_rate >= THERMAL_SOLAR_MIN_RATE_F_PER_HR:
                                self._commit_observation_if_sufficient(obs_type, "insufficient_rate")

    # ------------------------------------------------------------------
    # Chart-log dual-estimator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_solar_hour(ts_str: str) -> bool:
        """Return True if the timestamp falls in local hours 08:00–19:59 (solar guard)."""
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            local_hour = dt_util.as_local(ts).hour
            return 8 <= local_hour <= 19
        except (ValueError, AttributeError):
            return False

    def _select_estimator(
        self,
        result_a: dict | None,
        result_b: dict | None,
    ) -> dict | None:
        """Choose between endpoint (A) and block-OLS (B) estimates.

        Decision table:
          A=no, B=no               → None
          A=yes, B=no              → A, grade=low
          A=no,  B=yes, R²<0.20   → None (B unreliable, A absent)
          A=no,  B=yes, R²≥0.20   → B, grade=low(R²<0.50) or medium(R²≥0.50)
          A=yes, B=yes, R²<0.20   → A, grade=low
          A=yes, B=yes, R²0.20-0.50, agree → B, grade=low
          A=yes, B=yes, R²0.20-0.50, disagree → A, grade=low
          A=yes, B=yes, R²≥0.50, agree → B, grade=medium
          A=yes, B=yes, R²≥0.50, disagree → A, grade=low
        """
        a_valid = result_a is not None and result_a.get("k") is not None
        b_valid = result_b is not None and result_b.get("k") is not None

        if not a_valid and not b_valid:
            return None

        if a_valid and not b_valid:
            chosen = dict(result_a)
            chosen["grade"] = "low"
            return chosen

        r2_b = result_b.get("r_squared") if result_b else None

        if not a_valid and b_valid:
            if r2_b is None or r2_b < THERMAL_DUAL_OLS_OK:
                return None
            chosen = dict(result_b)
            chosen["grade"] = "medium" if r2_b >= THERMAL_DUAL_OLS_GOOD else "low"
            return chosen

        # Both valid
        if r2_b is None or r2_b < THERMAL_DUAL_OLS_OK:
            chosen = dict(result_a)
            chosen["grade"] = "low"
            _LOGGER.info(
                "chart_log dual_estimator: k_A=%.4f k_B=%s R²_B=%s agree=%s → source=%s grade=%s",
                result_a["k"],
                f"{result_b['k']:.4f}" if result_b else "n/a",
                f"{r2_b:.2f}" if r2_b is not None else "n/a",
                "n/a",
                chosen["source"],
                chosen["grade"],
            )
            return chosen

        denom_agree = (abs(result_a["k"]) + abs(result_b["k"])) / 2.0
        agree = denom_agree > 0 and (abs(result_a["k"] - result_b["k"]) / denom_agree) <= THERMAL_DUAL_AGREE_REL

        if r2_b >= THERMAL_DUAL_OLS_GOOD and agree:
            chosen = dict(result_b)
            chosen["grade"] = "medium"
        elif r2_b >= THERMAL_DUAL_OLS_OK and agree:
            chosen = dict(result_b)
            chosen["grade"] = "low"
        else:
            chosen = dict(result_a)
            chosen["grade"] = "low"

        _LOGGER.info(
            "chart_log dual_estimator: k_A=%.4f k_B=%s R²_B=%s agree=%s → source=%s grade=%s",
            result_a["k"],
            f"{result_b['k']:.4f}" if result_b else "n/a",
            f"{r2_b:.2f}" if r2_b is not None else "n/a",
            agree,
            chosen["source"],
            chosen["grade"],
        )
        return chosen

    def _extract_passive_windows(self, entries: list[dict], days: int) -> list[list[dict]]:
        """Extract passive decay windows from chart_log entries.

        Regime: HVAC=off/idle, fan=off, windows=closed.
        Solar guard: rejects any window whose start OR end timestamp falls in local hours 08–19.
        """
        cutoff = dt_util.now() - timedelta(days=days)
        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush() -> None:
            if len(current) < 2:
                current.clear()
                return
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                ts1 = datetime.fromisoformat(current[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                elapsed_min = (ts1 - ts0).total_seconds() / 60.0
                # Solar guard: reject windows that start or end in daytime hours
                if (
                    elapsed_min >= THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES
                    and not self._is_solar_hour(current[0]["ts"])
                    and not self._is_solar_hour(current[-1]["ts"])
                ):
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = entry.get("hvac", "")
            fan = entry.get("fan")
            windows_open = entry.get("windows_open")

            if indoor is None or outdoor is None:
                _flush()
                continue

            hvac_idle = hvac in ("idle", "off", "", "fan") or (
                "heat" not in (hvac or "") and "cool" not in (hvac or "")
            )
            fan_off = fan is False or fan is None
            win_closed = not windows_open

            if not hvac_idle or not fan_off or not win_closed:
                _flush()
                continue

            current.append({"ts": ts_str, "indoor": float(indoor), "outdoor": float(outdoor)})

        _flush()
        return windows

    def _passive_endpoint_estimate(self, window: list[dict]) -> dict | None:
        """Compute endpoint k_passive estimate for a passive decay window.

        Returns {"k": float, "r_squared": None, "source": "endpoint", "grade": "low"} or None.
        """
        import math as _math

        t_start = window[0]["indoor"]
        t_end = window[-1]["indoor"]
        t_out_avg = sum(s["outdoor"] for s in window) / len(window)

        try:
            ts0 = datetime.fromisoformat(window[0]["ts"])
            ts1 = datetime.fromisoformat(window[-1]["ts"])
            if ts0.tzinfo is None:
                ts0 = ts0.replace(tzinfo=UTC)
            if ts1.tzinfo is None:
                ts1 = ts1.replace(tzinfo=UTC)
            dt_hours = (ts1 - ts0).total_seconds() / 3600.0
        except (ValueError, KeyError):
            return None

        if abs(t_end - t_start) < THERMAL_CHART_LOG_PASSIVE_MIN_DT_F:
            return None
        if dt_hours < THERMAL_CHART_LOG_PASSIVE_MIN_MINUTES / 60.0:
            return None

        denom = t_start - t_out_avg
        if abs(denom) < 0.01:
            return None
        ratio = (t_end - t_out_avg) / denom
        if ratio <= 0 or ratio >= 1.0:
            return None

        k = _math.log(ratio) / dt_hours
        if not (THERMAL_K_PASSIVE_MIN <= k <= THERMAL_K_PASSIVE_MAX):
            return None

        return {"k": k, "r_squared": None, "source": "endpoint", "grade": "low"}

    def _run_passive_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Estimate k_passive from chart_log passive-only windows using dual-estimator.

        Endpoint estimator (A) and block-OLS estimator (B) are both computed for each
        window. _select_estimator() picks the best result. The chosen source and grade
        are recorded in the observation.

        Solar guard: windows starting or ending in local hours 08–19 are rejected.

        If backfill=True, processes up to 30 days of history (called once at startup).
        If backfill=False, processes only the most recent complete passive window.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            return

        days = 30 if backfill else 2
        windows = self._extract_passive_windows(entries, days)
        if not windows:
            return

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        today_str = dt_util.now().strftime("%Y-%m-%d")

        for window in target_windows:
            result_a = self._passive_endpoint_estimate(window)
            b_raw = compute_k_passive_blocks(window)
            result_b = (
                {"k": b_raw[0], "r_squared": b_raw[1], "source": "block_ols", "grade": "low"}
                if b_raw is not None and b_raw[0] is not None
                else None
            )
            chosen = self._select_estimator(result_a, result_b)
            if chosen is None:
                continue

            k = chosen["k"]
            try:
                ts0 = datetime.fromisoformat(window[0]["ts"])
                ts1 = datetime.fromisoformat(window[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                dt_hours = (ts1 - ts0).total_seconds() / 3600.0
            except (ValueError, KeyError):
                continue

            t_start = window[0]["indoor"]
            t_end = window[-1]["indoor"]
            t_out_avg = sum(s["outdoor"] for s in window) / len(window)
            denom = t_start - t_out_avg
            ratio = (t_end - t_out_avg) / denom if abs(denom) >= 0.01 else None

            obs = {
                "hvac_mode": "passive",
                "k_passive": k,
                "confidence_grade": chosen["grade"],
                "date": today_str,
                "source": chosen["source"],
                "r_squared": chosen.get("r_squared"),
                "elapsed_hours": round(dt_hours, 2),
                "delta_t_f": round(t_end - t_start, 2),
                "ratio": round(ratio, 4) if ratio is not None else None,
            }
            self.learning.record_thermal_observation(obs)
            committed += 1
            _LOGGER.debug(
                "chart_log passive: k=%.4f source=%s conf=%s dt=%.1fh dT=%.1fF",
                k,
                chosen["source"],
                chosen["grade"],
                dt_hours,
                t_end - t_start,
            )

        if committed > 0:
            _LOGGER.info(
                "chart_log passive: committed %d observations%s",
                committed,
                " (backfill)" if backfill else "",
            )

    def _extract_ventilated_windows(self, entries: list[dict], days: int) -> list[list[dict]]:
        """Extract ventilated decay windows from chart_log entries.

        Regime: HVAC=off/idle, windows=open, T_out < T_in throughout.
        Solar guard: rejects any window whose start OR end timestamp falls in local hours 08–19.
        """
        cutoff = dt_util.now() - timedelta(days=days)
        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush() -> None:
            if len(current) < 2:
                current.clear()
                return
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                ts1 = datetime.fromisoformat(current[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                elapsed_min = (ts1 - ts0).total_seconds() / 60.0
                # Solar guard: reject windows that start or end in daytime hours
                if (
                    elapsed_min >= THERMAL_CHART_LOG_VENT_MIN_MINUTES
                    and all(s["outdoor"] < s["indoor"] for s in current)
                    and not self._is_solar_hour(current[0]["ts"])
                    and not self._is_solar_hour(current[-1]["ts"])
                ):
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = entry.get("hvac", "")
            windows_open = entry.get("windows_open")

            if indoor is None or outdoor is None:
                _flush()
                continue

            hvac_idle = hvac in ("idle", "off", "", "fan") or (
                "heat" not in (hvac or "") and "cool" not in (hvac or "")
            )
            win_open = bool(windows_open)

            if not hvac_idle or not win_open:
                _flush()
                continue

            current.append({"ts": ts_str, "indoor": float(indoor), "outdoor": float(outdoor)})

        _flush()
        return windows

    def _ventilated_endpoint_estimate(self, window: list[dict]) -> dict | None:
        """Compute endpoint k_vent_window estimate for a ventilated decay window.

        Returns {"k": float, "r_squared": None, "source": "endpoint", "grade": "low"} or None.
        """
        import math as _math

        t_start = window[0]["indoor"]
        t_end = window[-1]["indoor"]
        t_out_avg = sum(s["outdoor"] for s in window) / len(window)

        try:
            ts0 = datetime.fromisoformat(window[0]["ts"])
            ts1 = datetime.fromisoformat(window[-1]["ts"])
            if ts0.tzinfo is None:
                ts0 = ts0.replace(tzinfo=UTC)
            if ts1.tzinfo is None:
                ts1 = ts1.replace(tzinfo=UTC)
            dt_hours = (ts1 - ts0).total_seconds() / 3600.0
        except (ValueError, KeyError):
            return None

        if abs(t_end - t_start) < THERMAL_CHART_LOG_PASSIVE_MIN_DT_F:
            return None
        if dt_hours < THERMAL_CHART_LOG_VENT_MIN_MINUTES / 60.0:
            return None

        denom = t_start - t_out_avg
        if abs(denom) < 0.01:
            return None
        ratio = (t_end - t_out_avg) / denom
        if ratio <= 0 or ratio >= 1.0:
            return None

        k = _math.log(ratio) / dt_hours
        if not (THERMAL_K_PASSIVE_MIN <= k <= THERMAL_K_PASSIVE_MAX):
            return None

        return {"k": k, "r_squared": None, "source": "endpoint", "grade": "low"}

    def _run_ventilated_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Estimate k_vent_window from overnight ventilated chart_log windows using dual-estimator.

        Endpoint estimator (A) and block-OLS estimator (B) are both computed for each
        window. _select_estimator() picks the best result.

        Natural regime filter: only windows where T_out < T_in throughout are used
        (overnight conditions).
        Solar guard: windows starting or ending in local hours 08–19 are rejected.

        If backfill=True, processes up to 30 days of history (once on startup).
        If backfill=False, processes only the most recent ventilated window.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            return

        days = 30 if backfill else 2
        windows = self._extract_ventilated_windows(entries, days)
        if not windows:
            return

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        today_str = dt_util.now().strftime("%Y-%m-%d")

        for window in target_windows:
            result_a = self._ventilated_endpoint_estimate(window)
            b_raw = compute_k_passive_blocks(window)
            result_b = (
                {"k": b_raw[0], "r_squared": b_raw[1], "source": "block_ols", "grade": "low"}
                if b_raw is not None and b_raw[0] is not None
                else None
            )
            chosen = self._select_estimator(result_a, result_b)
            if chosen is None:
                continue

            k = chosen["k"]
            try:
                ts0 = datetime.fromisoformat(window[0]["ts"])
                ts1 = datetime.fromisoformat(window[-1]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                if ts1.tzinfo is None:
                    ts1 = ts1.replace(tzinfo=UTC)
                dt_hours = (ts1 - ts0).total_seconds() / 3600.0
            except (ValueError, KeyError):
                continue

            t_start = window[0]["indoor"]
            t_end = window[-1]["indoor"]
            t_out_avg = sum(s["outdoor"] for s in window) / len(window)
            denom = t_start - t_out_avg
            ratio = (t_end - t_out_avg) / denom if abs(denom) >= 0.01 else None

            obs = {
                "hvac_mode": "ventilated",
                "k_passive": k,
                "confidence_grade": chosen["grade"],
                "date": today_str,
                "source": chosen["source"],
                "r_squared": chosen.get("r_squared"),
                "elapsed_hours": round(dt_hours, 2),
                "delta_t_f": round(t_end - t_start, 2),
                "ratio": round(ratio, 4) if ratio is not None else None,
            }
            self.learning.record_thermal_observation(obs)
            committed += 1
            _LOGGER.debug(
                "chart_log vent: k=%.4f source=%s conf=%s dt=%.1fh dT=%.1fF",
                k,
                chosen["source"],
                chosen["grade"],
                dt_hours,
                t_end - t_start,
            )

        if committed > 0:
            _LOGGER.info(
                "chart_log vent: committed %d observations%s",
                committed,
                " (backfill)" if backfill else "",
            )

    def _maybe_run_periodic_solar_phase_fit(self) -> None:
        """Run the incremental daily solar phase re-fit if due (Issue #310).

        Fires at most once per calendar day after the one-shot startup backfill
        (_solar_phase_backfill=True). Uses backfill=False (last 2 days only).
        """
        if not self._solar_phase_backfill:
            return
        _today = dt_util.now().date()
        if self._last_solar_phase_fit_date == _today:
            return
        self._run_solar_phase_chart_log_fit(backfill=False)
        self._last_solar_phase_fit_date = _today
        _LOGGER.info("chart_log solar_phase: daily incremental re-fit complete (date=%s)", _today)

    def _run_solar_phase_chart_log_fit(self, *, backfill: bool = False) -> None:
        """Estimate solar_phase_offset_h from daytime passive chart_log windows.

        Regime: HVAC=off, fan=off, windows_open=False, daytime local hours (8–20).
        Calls _estimate_solar_phase_offset() for each qualifying window.
        On success, updates EWMA via self.learning.update_solar_phase_offset().

        If backfill=True, processes up to 30 days of history (called once at startup).
        If backfill=False, processes only the most recent qualifying window.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            _LOGGER.debug("Solar phase fit: chart_log not initialized — skipping")
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            _LOGGER.debug("Solar phase fit: chart_log empty — skipping")
            return

        days = 30 if backfill else 2
        cutoff = dt_util.now() - timedelta(days=days)

        # Structured entry log: total entries and date range for observability
        try:
            _ts_first = entries[0].get("ts", "?")
            _ts_last = entries[-1].get("ts", "?")
        except (IndexError, AttributeError):
            _ts_first = _ts_last = "?"
        _LOGGER.info(
            "Solar phase fit: %d chart_log entries available, scanning last %d day(s) (%s–%s)",
            len(entries),
            days,
            _ts_first,
            _ts_last,
        )

        windows: list[list[dict]] = []
        current: list[dict] = []

        def _flush_solar() -> None:
            if len(current) < THERMAL_SOLAR_PHASE_MIN_ENTRIES:
                current.clear()
                return
            # Only keep windows that are clearly daytime (start hour 8–20)
            try:
                ts0 = datetime.fromisoformat(current[0]["ts"])
                if ts0.tzinfo is None:
                    ts0 = ts0.replace(tzinfo=UTC)
                local0 = dt_util.as_local(ts0)
                if 8 <= local0.hour < 20:
                    windows.append(list(current))
            except (ValueError, KeyError):
                pass
            current.clear()

        for entry in entries:
            ts_str = entry.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
            if ts < cutoff:
                continue

            indoor = entry.get("indoor")
            outdoor = entry.get("outdoor")
            hvac = str(entry.get("hvac", "")).lower()
            fan = str(entry.get("fan", "")).lower()
            windows_open = entry.get("windows_open", False)

            if indoor is None or outdoor is None:
                _flush_solar()
                continue

            # Regime: HVAC off, fan off, windows closed
            _hvac_off = hvac in ("off", "idle", "")
            _fan_off = fan in ("off", "false", "")
            if not (_hvac_off and _fan_off and not windows_open):
                _flush_solar()
                continue

            # Daytime guard: entry must be in local hours 8–20
            try:
                local_ts = dt_util.as_local(ts)
            except Exception:
                _flush_solar()
                continue
            if not (8 <= local_ts.hour < 20):
                _flush_solar()
                continue

            current.append(entry)

        _flush_solar()

        if not windows:
            _current_offset = (
                getattr(self, "learning", None)
                and getattr(self.learning, "_state", None)
                and (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
            )
            _LOGGER.info(
                "Solar phase fit: 0 qualifying passive-daytime windows — offset unchanged at %.2fh",
                _current_offset if isinstance(_current_offset, (int, float)) else 0.0,
            )
            return

        _LOGGER.info(
            "Solar phase fit: %d passive-daytime windows found, evaluating%s",
            len(windows),
            " (backfill)" if backfill else "",
        )

        target_windows = windows if backfill else windows[-1:]
        committed = 0
        rejected = 0

        for window in target_windows:
            obs, reject_reason = _estimate_solar_phase_offset(window)
            if obs is None:
                _LOGGER.debug(
                    "chart_log solar_phase: rejected window (%d entries): %s",
                    len(window),
                    reject_reason,
                )
                rejected += 1
                continue
            _old_offset = (
                (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
                if hasattr(self, "learning") and hasattr(self.learning, "_state")
                else None
            )
            self.learning.update_solar_phase_offset(obs, THERMAL_SOLAR_PHASE_ALPHA)
            _new_offset = (
                (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_h")
                if hasattr(self, "learning") and hasattr(self.learning, "_state")
                else None
            )
            committed += 1
            _LOGGER.info(
                "Solar phase EWMA: observed=%.2fh old=%.2f→new=%.2fh (window %d entries)",
                obs,
                _old_offset if isinstance(_old_offset, (int, float)) else 0.0,
                _new_offset if isinstance(_new_offset, (int, float)) else obs,
                len(window),
            )
            _LOGGER.debug(
                "chart_log solar_phase: committed obs=%.2f (window %d entries)",
                obs,
                len(window),
            )

        _LOGGER.info(
            "Solar phase fit: %d/%d windows committed%s (%d rejected)",
            committed,
            len(target_windows),
            " (backfill)" if backfill else "",
            rejected,
        )

    def _run_ac_duty_solar_phase_fit(self) -> None:
        """Estimate solar phase offset from AC duty cycle pattern (Issue #312).

        Secondary estimator — only used when the primary (passive window) method has
        never produced an observation. Reads the chart_log, groups entries by local
        calendar date, applies quality filter, estimates peak-load hour, and updates
        the EWMA via learning.update_ac_duty_solar_phase_offset().

        Sets self._solar_phase_ac_backfill = True on completion.
        """
        chart_log = getattr(self, "_chart_log", None)
        if chart_log is None:
            _LOGGER.debug("AC duty solar phase fit: chart_log not initialized — skipping")
            return
        entries = list(getattr(chart_log, "_entries", []))
        if not entries:
            _LOGGER.debug("AC duty solar phase fit: chart_log empty — skipping")
            return

        # Group entries by local calendar date
        from collections import defaultdict

        days: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            h = _entry_hour(entry)
            if h is None:
                continue
            try:
                day_str = datetime.fromisoformat(entry["ts"]).strftime("%Y-%m-%d")
            except (KeyError, ValueError):
                continue
            days[day_str].append(entry)

        committed = 0
        rejected = 0
        for day_str, day_entries in sorted(days.items()):
            ok, reason = _is_ac_duty_solar_day(day_entries)
            if not ok:
                rejected += 1
                _LOGGER.debug("AC duty solar phase: skip day=%s reason=%s", day_str, reason)
                continue
            offset = _estimate_ac_duty_solar_phase(day_entries)
            if offset is None:
                rejected += 1
                continue
            self.learning.update_ac_duty_solar_phase_offset(offset, day_str)
            committed += 1

        current: float | None = None
        if hasattr(self, "learning") and hasattr(self.learning, "_state"):
            current = (self.learning._state.thermal_model_cache or {}).get("solar_phase_offset_ac_h")
        _LOGGER.info(
            "AC duty solar phase fit: committed=%d rejected=%d current_offset=%s",
            committed,
            rejected,
            f"{current:.2f}h" if current is not None else "none",
        )
        self._solar_phase_ac_backfill = True

    def _maybe_run_periodic_solar_phase_fit(self) -> None:
        """Run the incremental daily solar phase re-fit if due (Issue #310/#312).

        Called once per day from _async_send_briefing after the primary backfill has
        completed. Runs both the primary passive-window estimator (backfill=False) and
        the secondary AC duty cycle estimator to pick up any new observations since the
        last backfill.

        No-ops until the initial backfill has been run at least once
        (_solar_phase_backfill must be True).
        """
        if not self._solar_phase_backfill:
            return
        _today = dt_util.now().date()
        if getattr(self, "_last_solar_phase_fit_date", None) == _today:
            return
        self._run_solar_phase_chart_log_fit(backfill=False)
        self._run_ac_duty_solar_phase_fit()
        self._last_solar_phase_fit_date = _today
        _LOGGER.info("chart_log solar_phase: daily incremental re-fit complete (date=%s)", _today)

    def _start_decay_observation(self, obs_type: str) -> None:
        """Create a new monitoring observation for a passive/fan/vent/solar type."""
        import uuid as _uuid_mod

        now = dt_util.now()
        obs: dict = {
            "obs_type": obs_type,
            "obs_id": str(_uuid_mod.uuid4()),
            "start_time": now.isoformat(),
            "status": "monitoring",
            "samples": [],
            "last_sample_time": None,
            "flags_at_start": {
                "sensor_open": self._any_sensor_open(),
                "fan_active": self.automation_engine._fan_active,
                "nat_vent_active": self.automation_engine._natural_vent_active,
            },
            "schema_version": 1,
        }
        self._pending_observations[obs_type] = obs
        _LOGGER.debug("Thermal decay observation started: obs_id=%s type=%s", obs["obs_id"], obs_type)

    def _end_hvac_active_phase(self, obs_type: str) -> None:
        """Transition HVAC observation active → post_heat when HVAC action stops."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None or obs.get("_phase") != "active":
            return
        now = dt_util.now()

        # Capture indoor temp at the exact HVAC-off moment so swing uses the true shutoff temperature.
        _final_indoor = self._get_indoor_temp()
        if _final_indoor is not None:
            try:
                _elapsed = (
                    now - dt_util.parse_datetime(obs.get("active_start", now.isoformat()))
                ).total_seconds() / 60.0
            except Exception:
                _elapsed = 0.0
            active_samples = obs.get("active_samples", [])
            if len(active_samples) < THERMAL_MAX_ACTIVE_SAMPLES:
                active_samples.append(self._get_current_sample(_elapsed))
            _cur_peak = obs.get("peak_indoor_f")
            if obs_type == OBS_TYPE_HVAC_COOL:
                # For cooling, peak is the minimum (lowest indoor temp reached)
                if _cur_peak is None or _final_indoor < _cur_peak:
                    obs["peak_indoor_f"] = _final_indoor
            else:
                # For heating (and any other type), peak is the maximum
                if _cur_peak is None or _final_indoor > _cur_peak:
                    obs["peak_indoor_f"] = _final_indoor

        obs["_phase"] = "post_heat"
        obs["active_end"] = now.isoformat()

        active_start_str = obs.get("active_start")
        try:
            active_start = dt_util.parse_datetime(active_start_str) if active_start_str else now
        except Exception:
            active_start = now
        obs["session_minutes"] = (now - active_start).total_seconds() / 60.0

        _LOGGER.info(
            "Thermal HVAC observation active → post_heat: obs_id=%s session=%.1f min",
            obs.get("obs_id", "?"),
            obs["session_minutes"],
        )

    async def _check_hvac_stabilization(self, obs_type: str) -> None:
        """Check if post-HVAC temperature has stabilized or timed out."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None or obs.get("_phase") != "post_heat":
            return

        active_end_str = obs.get("active_end")
        try:
            active_end = dt_util.parse_datetime(active_end_str) if active_end_str else dt_util.now()
        except Exception:
            active_end = dt_util.now()

        elapsed_post = (dt_util.now() - active_end).total_seconds() / 60.0

        if elapsed_post > THERMAL_POST_HEAT_TIMEOUT_MINUTES:
            _n_active = len(obs.get("active_samples", []))
            _n_post = len(obs.get("post_heat_samples", []))
            _LOGGER.info(
                "_check_hvac_stabilization: type=%s timeout n_active=%d n_post=%d elapsed_post=%.0fmin — abandoning",
                obs_type,
                _n_active,
                _n_post,
                elapsed_post,
            )
            self._abandon_observation(obs_type, "post_heat timeout exceeded")
            await self.hass.async_add_executor_job(self.learning.save_state)
            return

        post_samples = obs.get("post_heat_samples", [])

        # Issue #130 D24: When k_vent_window proxy is available (bridge home), post-heat OLS
        # is not needed — k_passive comes from the proxy and k_active from single-point
        # timestamps.  Single-point only needs post[0] for the HVAC-off timestamp, so the
        # minimum drops from THERMAL_MIN_POST_HEAT_SAMPLES (4) to 1.  Proxy-unaware paths
        # (normal homes, fresh installs) are unchanged.
        _cache = getattr(self.learning, "_state", None)
        _cache = _cache.thermal_model_cache if _cache is not None else None
        _k_vent_window = _cache.get("k_vent_window") if isinstance(_cache, dict) else None
        _proxy_available = _k_vent_window is not None and _k_vent_window < 0
        _min_post = 1 if _proxy_available else THERMAL_MIN_POST_HEAT_SAMPLES

        if len(post_samples) < _min_post:
            return

        # Issue #130 D15: Remove stabilization-wait gate.  Once min samples are collected,
        # commit immediately via OLS — the R² already governs quality.  Waiting for ±0.3°F
        # stability (THERMAL_STABILIZATION_THRESHOLD_F) over the last 5 min is redundant
        # and systematically blocks short-cycle (5–30 min) observations from ever committing.
        obs["status"] = "stabilized"
        obs["stabilized_at"] = dt_util.now().isoformat()
        obs["end_indoor_f"] = post_samples[-1]["indoor_temp_f"]

        peak_f = obs.get("peak_indoor_f")
        end_f = obs["end_indoor_f"]

        # Issue #130 D25: Plateau guard validates post-heat decay quality for k_passive OLS.
        # When proxy is available, k_passive comes from k_vent_window — no OLS decay needed.
        # Bypass the guard so short-cycle bridge homes are not incorrectly abandoned.
        if not _proxy_available and peak_f is not None and (peak_f - end_f) < THERMAL_HVAC_MIN_DECAY_F:
            _n_active_pg = len(obs.get("active_samples", []))
            _n_post_pg = len(obs.get("post_heat_samples", []))
            _LOGGER.info(
                "_check_hvac_stabilization: type=%s plateau guard n_active=%d n_post=%d elapsed_post=%.0fmin",
                obs_type,
                _n_active_pg,
                _n_post_pg,
                elapsed_post,
            )
            _LOGGER.info(
                "Thermal HVAC plateau guard: obs_id=%s peak=%.2f end=%.2f decay=%.2f < %.2f — abandoning",
                obs.get("obs_id", "?"),
                peak_f,
                end_f,
                peak_f - end_f,
                THERMAL_HVAC_MIN_DECAY_F,
            )
            self._abandon_observation(obs_type, "plateau guard: insufficient post-heat decay")
            await self._async_save_state()
            return

        _LOGGER.info(
            "Thermal HVAC observation min-samples reached: obs_id=%s post_samples=%d — committing",
            obs.get("obs_id", "?"),
            len(post_samples),
        )
        await self._commit_observation(obs_type)

    async def _commit_observation(self, obs_type: str, force_grade: str | None = None) -> None:
        """Commit a pending observation to the learning engine."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None:
            return
        if not self.config.get("learning_enabled", True):
            self._pending_observations.pop(obs_type, None)
            await self.hass.async_add_executor_job(self.learning.save_state)
            return

        obs_result, reject_code, r_squared = await self.hass.async_add_executor_job(
            self.learning._commit_event_from_dict,
            obs,
            force_grade,
            obs_type,
        )

        if obs_result is not None:
            if self._today_record is not None and obs_type in (OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL):
                self._today_record.thermal_session_count += 1
            self._pending_observations.pop(obs_type, None)
            await self.hass.async_add_executor_job(self.learning.save_state)
        else:
            # Learning engine rejected (OLS bad fit, wrong sign, bounds, etc.).
            # Route through _abandon_observation so the rejection enters _rejection_log
            # and the health surface stays accurate.
            self._abandon_observation(
                obs_type,
                "ols_rejected",
                reason_code=reject_code or REJECT_OLS_BAD_FIT,
                r_squared=r_squared,
                n_required=THERMAL_MIN_DECAY_SAMPLES,
            )
            await self.hass.async_add_executor_job(self.learning.save_state)

    def _abandon_observation(
        self,
        obs_type: str,
        reason: str,
        *,
        reason_code: str | None = None,
        r_squared: float | None = None,
        n_required: int | None = None,
        delta_t_required: float | None = None,
        elapsed_minutes: int | None = None,
    ) -> None:
        """Discard a pending observation and emit a structured rejection event."""
        self._ensure_pending_observations()
        obs = self._pending_observations.pop(obs_type, None)
        if obs is None:
            return
        if elapsed_minutes is None and obs is not None:
            _start_str = obs.get("start_time")
            if _start_str:
                try:
                    _start_ts = dt_util.parse_datetime(_start_str)
                    if _start_ts:
                        elapsed_minutes = int((dt_util.now() - _start_ts).total_seconds() / 60)
                except Exception:
                    pass
        # Bug 1 fix: For HVAC obs, prefer active_samples (or post_heat_samples when in
        # post_heat phase) over the generic 'samples' key.  Pre-fix HVAC obs dicts had
        # 'samples': [] which shadows active_samples in the fallback chain, causing n=0
        # to be logged even when active_samples has real data.
        _obs_type_ab = obs.get("obs_type", obs_type)
        _hvac_types_ab = {OBS_TYPE_HVAC_HEAT, OBS_TYPE_HVAC_COOL}
        if _obs_type_ab in _hvac_types_ab:
            _phase_ab = obs.get("_phase", "active")
            samples = obs.get("post_heat_samples", []) if _phase_ab == "post_heat" else obs.get("active_samples", [])
            # If still empty, fall back to generic 'samples' (legacy migration path)
            if not samples:
                samples = obs.get("samples", [])
        else:
            samples = obs.get("samples", obs.get("active_samples", []))
        delta_f = 0.0
        if len(samples) >= 2:
            first = samples[0].get("indoor_temp_f", samples[0].get("indoor_f", 0))
            last = samples[-1].get("indoor_temp_f", samples[-1].get("indoor_f", 0))
            delta_f = round(abs(last - first), 2)
        _sf_vals_ab = [s.get("solar_factor", 0.0) for s in samples if "solar_factor" in s]
        _sf_range_ab = round(max(_sf_vals_ab) - min(_sf_vals_ab), 2) if len(_sf_vals_ab) >= 2 else 0.0
        _temps_ab = [s.get("indoor_temp_f", 0.0) for s in samples if "indoor_temp_f" in s]
        _dir_ab = (
            "rising"
            if len(_temps_ab) >= 2 and _temps_ab[-1] > _temps_ab[0] + 0.1
            else "falling"
            if len(_temps_ab) >= 2 and _temps_ab[-1] < _temps_ab[0] - 0.1
            else "flat"
        )
        _LOGGER.info(
            "Thermal obs abandoned [type=%s reason=%s n=%d/%s dt=%.2f°F/%s elapsed=%sm]",
            obs_type,
            reason_code or reason,
            len(samples),
            str(n_required) if n_required is not None else "?",
            delta_f,
            f"{delta_t_required:.2f}" if delta_t_required is not None else "?",
            str(elapsed_minutes) if elapsed_minutes is not None else "?",
        )
        event = {
            "obs_type": obs_type,
            "reason_code": reason_code or REJECT_ABANDONED,
            "n_samples": len(samples),
            "n_required": n_required,
            "r_squared": r_squared,
            "r_squared_required": THERMAL_MIN_R_SQUARED,
            "delta_t_f": delta_f,
            "delta_t_required": delta_t_required,
            "elapsed_minutes": elapsed_minutes,
            "sf_range": _sf_range_ab,
            "indoor_direction": _dir_ab,
            "timestamp": dt_util.now().isoformat(),
        }
        if not hasattr(self, "_rejection_log"):
            self._rejection_log = {}
        bucket = self._rejection_log.setdefault(obs_type, [])
        bucket.append(event)
        if len(bucket) > _REJECTION_LOG_CAP:
            bucket.pop(0)
        # Sync to LearningState so rejection_log is persisted by save_state()
        self.learning._state.rejection_log = self._rejection_log
        self.hass.async_create_task(self.hass.async_add_executor_job(self.learning.save_state))

    def _build_learning_health(self) -> dict:
        """Aggregate _rejection_log into a per-obs-type health dict for get_thermal_model().

        Returns a dict keyed by obs_type, each value containing:
          - attempts: committed + total rejections
          - committed: number of successfully committed observations
          - rejections: per-reason-code counts
          - last_rejection: the most recent rejection event dict, or None
        """
        all_obs_types = [
            OBS_TYPE_PASSIVE_DECAY,
            OBS_TYPE_FAN_ONLY_DECAY,
            OBS_TYPE_VENTILATED_DECAY,
            OBS_TYPE_SOLAR_GAIN,
            OBS_TYPE_HVAC_HEAT,
            OBS_TYPE_HVAC_COOL,
        ]
        all_reason_codes = [
            REJECT_TOO_FEW_SAMPLES,
            REJECT_TOO_FEW_BLOCKS,
            REJECT_SMALL_DELTA,
            REJECT_OLS_BAD_FIT,
            REJECT_OLS_WRONG_SIGN,
            REJECT_OLS_BOUNDS,
            REJECT_ABANDONED,
            REJECT_WINDOW_TOO_SHORT,
            REJECT_NO_INTERIOR_PEAK,
        ]
        _hvac_mode_to_obs_type = {
            "passive": OBS_TYPE_PASSIVE_DECAY,
            "fan_only": OBS_TYPE_FAN_ONLY_DECAY,
            "ventilated": OBS_TYPE_VENTILATED_DECAY,
            "solar": OBS_TYPE_SOLAR_GAIN,
            "heat": OBS_TYPE_HVAC_HEAT,
            "cool": OBS_TYPE_HVAC_COOL,
        }
        health = {}
        thermal_observations = getattr(self.learning._state, "thermal_observations", [])
        rejection_log = getattr(self, "_rejection_log", {})
        for obs_type in all_obs_types:
            events = rejection_log.get(obs_type, [])
            rejection_counts: dict[str, int] = {rc: 0 for rc in all_reason_codes}
            for ev in events:
                rc = ev.get("reason_code", REJECT_ABANDONED)
                if rc in rejection_counts:
                    rejection_counts[rc] += 1
            last = events[-1] if events else None
            committed = (
                sum(
                    1
                    for o in thermal_observations
                    if isinstance(o, dict) and _hvac_mode_to_obs_type.get(o.get("hvac_mode")) == obs_type
                )
                if isinstance(thermal_observations, list)
                else 0
            )
            health[obs_type] = {
                "attempts": committed + sum(rejection_counts.values()),
                "committed": committed,
                "rejections": rejection_counts,
                "last_rejection": last,
            }

        # Per-source observation counts (dual-estimator instrumentation)
        if isinstance(thermal_observations, list):
            health["source_endpoint_count"] = sum(
                1 for o in thermal_observations if isinstance(o, dict) and o.get("source") == "endpoint"
            )
            health["source_block_ols_count"] = sum(
                1 for o in thermal_observations if isinstance(o, dict) and o.get("source") == "block_ols"
            )
        else:
            health["source_endpoint_count"] = 0
            health["source_block_ols_count"] = 0

        return health

    def _commit_observation_if_sufficient(self, obs_type: str, abandon_reason: str) -> None:
        """Commit if enough samples exist, else abandon."""
        self._ensure_pending_observations()
        obs = self._pending_observations.get(obs_type)
        if obs is None:
            return
        samples = obs.get("samples", obs.get("active_samples", []))
        min_samples = {
            OBS_TYPE_PASSIVE_DECAY: THERMAL_PASSIVE_MIN_SAMPLES,
            OBS_TYPE_FAN_ONLY_DECAY: THERMAL_FAN_MIN_SAMPLES,
            OBS_TYPE_VENTILATED_DECAY: THERMAL_VENT_MIN_SAMPLES,
            OBS_TYPE_SOLAR_GAIN: THERMAL_SOLAR_MIN_SAMPLES,
            OBS_TYPE_HVAC_HEAT: THERMAL_MIN_POST_HEAT_SAMPLES,
            OBS_TYPE_HVAC_COOL: THERMAL_MIN_POST_HEAT_SAMPLES,
        }.get(obs_type, 10)
        if len(samples) >= min_samples:
            # H2: total-ΔT guard for short windows — prevent noise-fitting on near-flat data
            if len(samples) < 10:
                temps = [s["indoor_temp_f"] for s in samples]
                if temps and (max(temps) - min(temps)) < THERMAL_ROLLING_MIN_DELTA_T_F:
                    _LOGGER.debug(
                        "Abandoning %s: insufficient total delta in short window (%.3f°F < %.3f°F)",
                        obs_type,
                        max(temps) - min(temps),
                        THERMAL_ROLLING_MIN_DELTA_T_F,
                    )
                    self._pending_observations.pop(obs_type, None)
                    return
            obs["status"] = "committing"  # prevent duplicate commit on next poll
            self.hass.async_create_task(self._commit_observation(obs_type, force_grade="low"))
        else:
            self._abandon_observation(obs_type, abandon_reason)

    def _evaluate_rolling_window(
        self,
        obs_type: str,
        obs: dict,
        signal_sufficient: bool,
        skip_delta_guard: bool = False,
    ) -> bool:
        """Evaluate whether a condition-bounded observation should commit, keep alive, or abandon.

        Returns True if the observation was committed or abandoned (caller should ``continue``).
        Returns False if the observation should keep collecting samples.

        Two-threshold logic (Issue #126):
        - Before THERMAL_ROLLING_MIN_WINDOW_MINUTES AND no signal: keep collecting.
        - After THERMAL_ROLLING_MIN_WINDOW_MINUTES AND signal sufficient: commit now.
        - After THERMAL_ROLLING_MAX_WINDOW_MINUTES: commit if enough samples, else abandon.
        - Between min and max with insufficient signal: log and keep collecting.
        """
        now = dt_util.now()
        start_str = obs.get("start_time")
        elapsed = 0.0
        if start_str:
            try:
                start_ts = dt_util.parse_datetime(start_str)
                if start_ts:
                    elapsed = (now - start_ts).total_seconds() / 60.0
            except Exception:
                pass

        # Too early and no signal yet — keep accumulating
        if elapsed < THERMAL_ROLLING_MIN_WINDOW_MINUTES and not signal_sufficient:
            return False

        # Ready to commit: min window elapsed AND signal is present
        if elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES and signal_sufficient:
            self._commit_rolling_window_obs(obs_type, obs, skip_delta_guard=skip_delta_guard)
            return True

        # Hard cap reached — commit if enough samples, else abandon
        if elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES:
            samples = obs.get("samples", [])
            if len(samples) >= THERMAL_MIN_DECAY_SAMPLES + 1:
                self._commit_rolling_window_obs(obs_type, obs, skip_delta_guard=True)
            else:
                self._abandon_observation(
                    obs_type,
                    "max_window_exceeded",
                    reason_code="max_window_exceeded",
                    elapsed_minutes=int(elapsed),
                )
            return True

        # Between min and max window, signal not yet sufficient — log and keep alive
        if elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES:
            samples = obs.get("samples", [])
            temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
            delta = round(max(temps) - min(temps), 2) if temps else 0.0
            _LOGGER.info(
                "Thermal rolling window: obs_type=%s keeping alive "
                "(elapsed=%.0fmin delta=%.2f degF < %.2f degF needed, max=%dmin)",
                obs_type,
                elapsed,
                delta,
                THERMAL_ROLLING_MIN_DELTA_T_F,
                THERMAL_ROLLING_MAX_WINDOW_MINUTES,
            )
            # Trim oldest samples to prevent unbounded growth (~96 max at 5-min cadence over 4h)
            if len(samples) > 96:
                obs["samples"] = samples[-96:]
        return False

    def _commit_rolling_window_obs(self, obs_type: str, obs: dict, *, skip_delta_guard: bool = False) -> None:
        """Commit a rolling-window observation, bypassing the full min_samples threshold.

        Rolling windows are short by design (THERMAL_ROLLING_MIN_WINDOW_MINUTES = 30 min,
        THERMAL_PASSIVE_SAMPLE_INTERVAL_S = 300 s → ~6 samples). The normal min_samples
        threshold (e.g. THERMAL_PASSIVE_MIN_SAMPLES = 30) is calibrated for long overnight
        obs. For rolling windows we require ≥ THERMAL_MIN_DECAY_SAMPLES + 1 (= 5) samples
        and a ΔT ≥ THERMAL_ROLLING_MIN_DELTA_T_F to ensure the OLS regression has signal.

        ``skip_delta_guard`` should be set for vent/fan obs types where the signal
        guarantee is the indoor-outdoor differential (already checked by caller) rather
        than the indoor temperature trend.
        """
        self._ensure_pending_observations()
        samples = obs.get("samples", [])
        _start_ts = dt_util.parse_datetime(obs.get("start_time", "")) if obs.get("start_time") else None
        _elapsed = round((dt_util.now() - _start_ts).total_seconds() / 60.0, 1) if _start_ts else None
        _temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
        _outdoor = samples[-1].get("outdoor_temp_f") if samples else None
        _LOGGER.info(
            "Thermal rolling window: obs_type=%s n=%d elapsed=%.1fmin indoor=[%.1f..%.1f] (ΔT=%.2f°F) outdoor=%s",
            obs_type,
            len(samples),
            _elapsed or 0,
            min(_temps) if _temps else 0,
            max(_temps) if _temps else 0,
            (max(_temps) - min(_temps)) if _temps else 0,
            f"{_outdoor:.1f}" if _outdoor is not None else "?",
        )
        if len(samples) < THERMAL_MIN_DECAY_SAMPLES + 1:
            self._abandon_observation(obs_type, "window_elapsed_too_few_samples")
            return
        if not skip_delta_guard:
            temps = [s["indoor_temp_f"] for s in samples]
            if max(temps) - min(temps) < THERMAL_ROLLING_MIN_DELTA_T_F:
                _LOGGER.info(
                    "Abandoning rolling window %s: insufficient total ΔT (%.3f degF < %.3f degF)",
                    obs_type,
                    max(temps) - min(temps),
                    THERMAL_ROLLING_MIN_DELTA_T_F,
                )
                self._pending_observations.pop(obs_type, None)
                return
        obs["status"] = "committing"
        self.hass.async_create_task(self._commit_observation(obs_type, force_grade="low"))

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
        # Normalize alternate temp field names used by automation events
        for _src in ("indoor_temp", "indoor"):
            if _src in entry:
                entry.setdefault("indoor_f", entry[_src])
                break
        for _src in ("outdoor_temp", "outdoor"):
            if _src in entry:
                entry.setdefault("outdoor_f", entry[_src])
                break
        if getattr(self, "config", None):
            entry.setdefault("indoor_f", self._get_indoor_temp())
            entry.setdefault("outdoor_f", getattr(self, "_last_outdoor_temp", None))
        self._event_log.append(entry)
        if len(self._event_log) > EVENT_LOG_CAP:
            self._event_log.pop(0)

    def _emit_incident(self, incident_class: str, incident_id: str, extra: dict | None = None) -> None:
        """Emit an incident_detected event into the event log."""
        payload: dict = {
            "incident_class": incident_class,
            "incident_id": incident_id,
            "comfort_cool": self.config.get("comfort_cool"),
            "comfort_heat": self.config.get("comfort_heat"),
            "occupancy_mode": (self.automation_engine._occupancy_mode if self.automation_engine else None),
        }
        if extra:
            payload.update(extra)
        self._emit_event("incident_detected", payload)

    def _is_nat_vent_tolerated_deviation(self, indoor: float, comfort_heat: float, comfort_cool: float) -> bool:
        """True if this comfort-band deviation is expected nat-vent cycling tolerance, not a violation.

        WHF nat-vent cycling (see automation.py's nat_vent_temperature_check()) is designed to
        let indoor oscillate slightly past the comfort_heat/comfort_cool band edges — the fan
        cycles on/off around a midpoint using the same CONF_NAT_VENT_HYSTERESIS_F hysteresis band.
        A momentary deviation within that hysteresis tolerance, while a nat-vent session is
        actively running, is the system successfully exercising control (CLAUDE.md
        "Goal-Oriented Comfort Model", Issue #74) — not a comfort failure. Only used to gate
        false-positive detection (incident emission, violation-minute accumulation); it does not
        change the underlying >0.5°F trigger threshold or any other decision logic.
        """
        if not (self.automation_engine and self.automation_engine._natural_vent_active):
            return False
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        return (comfort_heat - hysteresis) <= indoor <= (comfort_cool + hysteresis)

    def _detect_and_emit_incidents(self) -> None:
        """Scan recent event_log and state for noteworthy production incidents.

        Emits incident_detected events for patterns that should trigger
        auto-scenario generation in the simulation feedback loop.
        """
        now = dt_util.now()
        recent_events = [
            e for e in self._event_log[-20:] if e.get("time", "") >= (now - timedelta(minutes=35)).isoformat()
        ]
        event_types = [e.get("type") for e in recent_events]

        if "occupancy_change" in event_types or any(
            t in event_types for t in ["occupancy_away", "occupancy_home", "occupancy_vacation"]
        ):
            occ_event = next(
                (
                    e
                    for e in recent_events
                    if e.get("type") in ["occupancy_change", "occupancy_away", "occupancy_home", "occupancy_vacation"]
                ),
                None,
            )
            if occ_event:
                self._emit_incident(
                    "occupancy_transition",
                    occ_event.get("time", now.isoformat()),
                    extra={
                        "occupancy_mode": self.automation_engine._occupancy_mode if self.automation_engine else None,
                        "manual_override_active": (
                            self.automation_engine._manual_override_active if self.automation_engine else None
                        ),
                    },
                )

        override_events = [e for e in recent_events if e.get("type") == "override_detected"]
        automation_event_types = {
            "classification_applied",
            "classification_suppressed_paused",
            "warm_day_setback_applied",
            "warm_day_state_confirmed",
            "ceiling_guard_fired",
            "nat_vent_ceiling_escalation",
            "nat_vent_ac_assist_armed",
        }
        for ov_event in override_events:
            ov_time_str = ov_event.get("time", "")
            if not ov_time_str:
                continue
            try:
                ov_time = datetime.fromisoformat(ov_time_str)
            except (ValueError, TypeError):
                continue
            preceding = [
                e for e in recent_events if e.get("type") in automation_event_types and e.get("time", "") < ov_time_str
            ]
            if preceding:
                last_auto = preceding[-1]
                try:
                    last_auto_time = datetime.fromisoformat(last_auto.get("time", ""))
                    gap_seconds = (ov_time - last_auto_time).total_seconds()
                    if gap_seconds < 60:
                        self._emit_incident(
                            "rapid_override_after_automation",
                            ov_time_str,
                            extra={
                                "automation_event_type": last_auto.get("type"),
                                "gap_seconds": round(gap_seconds),
                            },
                        )
                except (ValueError, TypeError):
                    pass

        current_data = self.data or {}
        indoor = current_data.get("indoor_temp")
        comfort_cool = self.config.get("comfort_cool")
        comfort_heat = self.config.get("comfort_heat")
        if (
            indoor
            and comfort_cool
            and indoor > comfort_cool + 0.5
            and not self._is_nat_vent_tolerated_deviation(indoor, comfort_heat or 0.0, comfort_cool)
        ):
            recent_violations = [
                e
                for e in self._event_log[-50:]
                if e.get("type") == "incident_detected"
                and e.get("incident_class") == "comfort_violation"
                and e.get("time", "") >= (now - timedelta(minutes=30)).isoformat()
            ]
            if not recent_violations:
                self._emit_incident(
                    "comfort_violation",
                    now.isoformat(),
                    extra={
                        "indoor_f": indoor,
                        "outdoor_f": current_data.get("outdoor_temp"),
                        "hvac_mode": current_data.get("hvac_mode"),
                        "nat_vent_active": (
                            self.automation_engine._natural_vent_active if self.automation_engine else None
                        ),
                    },
                )
        elif (
            indoor
            and comfort_heat
            and indoor < comfort_heat - 0.5
            and not self._is_nat_vent_tolerated_deviation(indoor, comfort_heat, comfort_cool or 999.0)
        ):
            recent_violations = [
                e
                for e in self._event_log[-50:]
                if e.get("type") == "incident_detected"
                and e.get("incident_class") == "comfort_undertemp"
                and e.get("time", "") >= (now - timedelta(minutes=30)).isoformat()
            ]
            if not recent_violations:
                self._emit_incident(
                    "comfort_undertemp",
                    now.isoformat(),
                    extra={
                        "indoor_f": indoor,
                        "outdoor_f": current_data.get("outdoor_temp"),
                        "hvac_mode": current_data.get("hvac_mode"),
                        "nat_vent_active": (
                            self.automation_engine._natural_vent_active if self.automation_engine else None
                        ),
                    },
                )

    def _compute_automation_status(self) -> str:
        """Compute the current automation status string."""
        if not self._automation_enabled:
            return "disabled"
        # Bug 1 (Issue #321): Surface startup coalescing window in status
        if self._startup_coalesce_active:
            # Issue #396: the coalesce check only runs once weather data is available
            # (it lives inside `if forecast:` in _async_update_data()) — if the 5-min
            # timer has already fired but classification still isn't set, the real
            # blocker is the weather entity, not coalescing itself. Without this
            # distinction the status card says "waiting for coalescing" indefinitely
            # with no clue that the actual cause is a weather integration that hasn't
            # come back after restart.
            if self._startup_timer_fired and self._current_classification is None:
                return "starting — waiting for weather data"
            return "starting — initializing"
        # Check if windows are open during a planned window period (not a pause)
        if self.automation_engine._is_within_planned_window_period() and self._any_sensor_open():
            return "windows open (as planned)"
        if self.automation_engine.natural_vent_active:
            # Bug 3 (Issue #321): surface cycling target in status for at-a-glance visibility.
            # Issue #407: was hardcoded to the flat daytime comfort-band midpoint, ignoring the
            # sleep window, and never migrated to compute_nat_vent_cycling_band() (the Issue #402
            # follow-up single source of truth for this exact value) — see that method's docstring
            # for the fix-one-duplicate-miss-the-sibling history this repeats (#374, #400, #402).
            # Issue #409: dropped the "windows open · " prefix — natural_vent_active does not
            # imply a sensor is open (it can activate on temperature/idle-HVAC conditions alone,
            # see automation.py's idle-reeval path, and door/window sensors are optional config),
            # and real window state is already shown by the dedicated Doors/Windows status card.
            # Restating it here was both potentially inaccurate and duplicative.
            _nt = self.compute_nat_vent_cycling_band()["nat_vent_target"]
            return f"nat-vent (target {_nt:.0f}°F)"
        if self.automation_engine.is_paused_by_door:
            if self._occupancy_mode == OCCUPANCY_AWAY:
                return "paused — away (setback deferred: windows open)"
            if self._occupancy_mode == OCCUPANCY_VACATION:
                return "paused — vacation (setback deferred: windows open)"
            return "paused — door/window open"
        if self.automation_engine._override_confirm_pending:
            return "override pending (confirming...)"
        # Bug 2 (Issue #321): detect stuck grace before the normal grace-active path
        _ae2 = self.automation_engine
        if _ae2._manual_override_active and not _ae2._grace_active and _ae2._grace_end_time is not None:
            _se = dt_util.parse_datetime(_ae2._grace_end_time)
            if _se is not None and dt_util.now() > _se:
                return "override (grace stuck — check logs)"
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

    @staticmethod
    def _pred_archive_key(dt: datetime) -> int:
        """Return Unix timestamp floored to nearest 30-min boundary (UTC-safe)."""
        ts = int(dt.timestamp())
        return ts - (ts % 1800)

    def _lookup_pred_archive(self, now_dt: datetime) -> float | None:
        """Return first-written ODE prediction for this 30-min slot (None on cache miss)."""
        return self._pred_archive.get(self._pred_archive_key(now_dt))

    def _read_chart_hvac_action(self) -> str:
        """Return the thermostat's current hvac_action string for chart logging.

        Applies the #109 fan→heating/cooling remap: only remaps when fan_mode is
        auto (fan is part of the HVAC cycle). When fan_mode=on, the fan is
        circulating independently — hvac_action="fan" does NOT imply active
        heating or cooling.

        Returns "" if the climate entity is unavailable.
        """
        climate_id = self.config.get("climate_entity", "")
        cs = self.hass.states.get(climate_id) if climate_id else None
        if cs is None:
            _LOGGER.debug("chart_hvac_action: climate entity unavailable, logging ''")
            return ""
        hvac_action = str(cs.attributes.get("hvac_action", "")).lower()
        hvac_mode = cs.state.lower()
        fan_mode = str(cs.attributes.get("fan_mode", "")).lower()
        fan_is_auto = not fan_mode or fan_mode.startswith("auto")
        if hvac_action == "fan" and fan_is_auto:
            if hvac_mode == "heat":
                _LOGGER.debug("chart_hvac_action: remapping fan→heating (fan_mode=%s)", fan_mode or "empty")
                return "heating"
            if hvac_mode in ("cool", "heat_cool"):
                _LOGGER.debug("chart_hvac_action: remapping fan→cooling (fan_mode=%s)", fan_mode or "empty")
                return "cooling"
        return hvac_action

    def _fan_is_running(self) -> bool:
        """Return True if the fan is running for any reason.

        Covers CA-activated, manual override, and untracked states so that
        chart_log entries correctly reflect fan activity even when CA's own
        _fan_active flag is False (e.g. post-heat blowdown still in progress).
        """
        return self._compute_fan_status() not in {"inactive", "disabled"}

    def _fan_physically_running(self) -> bool:
        """Return True iff the fan is physically spinning right now.

        Differs from _fan_is_running() by excluding the
        'nat-vent (session active, fan idle)' state — nat-vent armed but
        between cycles means the session is active but the blower is not on.

        Used for the chart_log ``fan_running`` field so the frontend can
        distinguish a spinning fan from a merely armed nat-vent session.
        """
        return self._compute_fan_status() in {
            "active",
            "running (manual override)",
            "running (untracked)",
        }

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
            if ae._fan_active:
                return "running (manual override)"
            # _fan_active=False: check physical state to distinguish
            # "user is running it" from "user turned it on then off"
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                physical_on = self._get_fan_physical_state()
                if physical_on is True:
                    return "running (manual override)"
            return "off (manual override)"
        if ae._fan_active:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                physical_on = self._get_fan_physical_state()
                if physical_on is False:
                    _LOGGER.warning(
                        "WHF _fan_active=True but physical state=off — possible stale flag after manual stop"
                    )
                    return "active (unconfirmed)"
            return "active"
        # Bug 3 (Issue #321): nat-vent session active but fan is idle between cycles
        if ae._natural_vent_active:
            return "nat-vent (session active, fan idle)"
        # WHF ground-truth fallback: reads fan_state_entity (Type 2) or fan_entity (Type 1).
        # Catches post-restart and externally-run WHF when CA's internal flags are all clear.
        if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            physical_on = self._get_fan_physical_state()
            if physical_on is True:
                return "running (untracked)"
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

    def _compute_whf_status(self) -> str | None:
        """Return WHF-specific status, or None when WHF is not configured."""
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return None
        if ae._fan_override_active:
            physical_on = self._get_fan_physical_state()
            if ae._fan_active or physical_on is True:
                return "running (manual override)"
            return "off (manual override)"
        if ae._fan_active:
            physical_on = self._get_fan_physical_state()
            if physical_on is False:
                _LOGGER.warning("WHF _fan_active=True but physical state=off — possible stale flag after manual stop")
                return "active (unconfirmed)"
            return "active"
        if ae._natural_vent_active:
            return "nat-vent (session active, fan idle)"
        physical_on = self._get_fan_physical_state()
        if physical_on is True:
            return "running (untracked)"
        return "inactive"

    def _compute_hvac_fan_status(self) -> str | None:
        """Return HVAC-fan-blower-specific status, or None when HVAC fan is not configured."""
        ae = self.automation_engine
        fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_HVAC, FAN_MODE_BOTH):
            return None
        if ae._fan_override_active:
            if ae._fan_active:
                return "running (manual override)"
            return "off (manual override)"
        if ae._fan_active:
            return "active"
        if ae._natural_vent_active:
            return "nat-vent (session active, fan idle)"
        # Ground-truth fallback via thermostat
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
        # Bug 1 (Issue #321): Surface coalescing as the next imminent action
        if self._startup_coalesce_active and self._startup_coalesce_expiry:
            # Format as a local-time label like the other branches below; the field's
            # contract is a display-ready label, never a raw ISO timestamp (Issue #324).
            coalesce_dt = dt_util.parse_datetime(self._startup_coalesce_expiry)
            time_str = dt_util.as_local(coalesce_dt).strftime("%I:%M %p").lstrip("0") if coalesce_dt else ""
            return ("Startup coalescing", time_str)

        if not c:
            return ("Waiting for classification...", "")

        now = dt_util.now()
        today = now.date()

        # Check if windows are open during planned window period
        if self.automation_engine._is_within_planned_window_period() and self._any_sensor_open():
            return ("Windows open as recommended", "")

        # Check if automation is paused
        if self.automation_engine.is_paused_by_door:
            return ("Waiting — HVAC paused (door/window open)", "")

        if self.automation_engine._grace_active:
            source = self.automation_engine._last_resume_source or "automation"
            return (f"Grace period active ({source})", "")

        # If a contact sensor debounce is pending, that is the soonest upcoming action
        if self._door_open_timers and self._door_open_timer_expiry:
            try:
                earliest_iso = min(self._door_open_timer_expiry.values())
                expiry_dt = dt_util.parse_datetime(earliest_iso)
                if expiry_dt and expiry_dt > now:
                    time_str = dt_util.as_local(expiry_dt).strftime("%I:%M:%S %p").lstrip("0")
                    return ("Evaluating door/window sensors", time_str)
            except (ValueError, AttributeError, TypeError):
                pass

        # Build list of upcoming scheduled events as (datetime, description).
        # Using full datetimes (not time objects) so cross-midnight events like
        # pre-cool (e.g. 2:30 AM tomorrow) compare correctly against now.
        wake_time = self.config.get("wake_time", "06:30:00")
        sleep_time = self.config.get("sleep_time", "22:30:00")
        briefing_time = self.config.get("briefing_time", "06:00:00")

        def _parse_time(t: str) -> time:
            parts = t.split(":")
            return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

        def _to_dt(t: time) -> datetime:
            return datetime.combine(today, t, tzinfo=now.tzinfo)

        candidates: list[tuple[datetime, str]] = []

        bt_dt = _to_dt(_parse_time(briefing_time))
        if bt_dt > now:
            candidates.append((bt_dt, "Send daily briefing"))

        wt_dt = _to_dt(_parse_time(wake_time))
        if wt_dt > now:
            if c.hvac_mode in ("heat", "cool"):
                candidates.append((wt_dt, f"Morning wake-up — restore {c.hvac_mode} comfort"))
            else:
                candidates.append((wt_dt, "Morning wake-up check"))

        st_dt = _to_dt(_parse_time(sleep_time))
        if st_dt > now:
            unit = self.config.get("temp_unit", "fahrenheit")
            if c.hvac_mode in ("heat", "cool"):
                # Use the raw configured sleep temp — matches what handle_bedtime() actually
                # sends to the thermostat via select_comfort_band(in_sleep_window=True).
                # The warming-trend modifier surfaces separately as the Pre-cool candidate below.
                from .const import CONF_SLEEP_COOL, CONF_SLEEP_HEAT, DEFAULT_SLEEP_COOL, DEFAULT_SLEEP_HEAT

                bedtime_target = float(
                    self.config.get(CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT)
                    if c.hvac_mode == "heat"
                    else self.config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL)
                )
                mode_label = "heat" if c.hvac_mode == "heat" else "cool"
                candidates.append((st_dt, f"Bedtime — {mode_label} setback to {format_temp(bedtime_target, unit)}"))
            else:
                candidates.append((st_dt, "Bedtime check"))

        # Pre-cool is scheduled for early tomorrow morning — its trigger_dt crosses
        # midnight, so only full-datetime comparison handles it correctly.
        if self._pre_cool_trigger_dt and self._pre_cool_trigger_dt > now and self._pre_cool_target is not None:
            unit = self.config.get("temp_unit", "fahrenheit")
            pc_desc = f"Pre-cool ceiling ({format_temp(self._pre_cool_target, unit)})"
            candidates.append((self._pre_cool_trigger_dt, pc_desc))

        if not candidates:
            return ("No more actions today", "")

        candidates.sort(key=lambda e: e[0])
        next_dt, next_desc = candidates[0]
        time_str = dt_util.as_local(next_dt).strftime("%I:%M %p").lstrip("0")
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
        tomorrow_class = classify_day(
            tomorrow_forecast,
            threshold_hot=self.config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT),
            threshold_warm=self.config.get(CONF_THRESHOLD_WARM, DEFAULT_THRESHOLD_WARM),
            threshold_mild=self.config.get(CONF_THRESHOLD_MILD, DEFAULT_THRESHOLD_MILD),
            threshold_cool=self.config.get(CONF_THRESHOLD_COOL, DEFAULT_THRESHOLD_COOL),
        )

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

    def get_chart_data(self, range_str: str = "24h", before_ts: float | None = None) -> dict[str, Any]:
        """Build chart data for the dashboard panel.

        Returns a dict with four series: predicted outdoor, predicted indoor,
        actual outdoor, and actual indoor temperatures over a 24-hour period,
        plus a rolling state log filtered/downsampled to the requested range.

        range_str: one of "6h", "12h", "24h", "3d", "7d", "30d", "1y"
        before_ts: optional Unix epoch *seconds* upper-bound anchor.  When
            provided the chart log is queried for [anchor - range, anchor)
            instead of [now - range, now).  Forecast and prediction series are
            suppressed for historical views (more than 1 h before now).
        """
        now = dt_util.now()
        current_hour = now.hour + now.minute / 60.0

        # Resolve the optional historical anchor.
        # Use UTC for the anchor datetime — chart log entries are stored in UTC
        # (or UTC-offset ISO strings), so UTC comparison is always correct.
        anchor_dt: datetime | None = None
        is_historical = False
        if before_ts is not None:
            anchor_dt = datetime.fromtimestamp(before_ts, tz=UTC)
            # View is "historical" when the anchor is more than 1 hour before now
            is_historical = (now - anchor_dt).total_seconds() > 3600

        thermal_model = (
            self.learning.get_thermal_model(learning_health=self._build_learning_health()) if self.learning else {}
        )
        unit = self.config.get("temp_unit", "fahrenheit")
        _LOGGER.debug(
            "Chart data: thermal_model conf_passive=%s conf_hvac=%s passive=%d fan=%d vent=%d solar=%d heat=%d cool=%d",
            thermal_model.get("confidence_k_passive", "none"),
            thermal_model.get("confidence", "none"),
            thermal_model.get("observation_count_passive", 0),
            thermal_model.get("observation_count_fan_only", 0),
            thermal_model.get("observation_count_vent", 0),
            thermal_model.get("observation_count_solar", 0),
            thermal_model.get("observation_count_heat", 0),
            thermal_model.get("observation_count_cool", 0),
        )
        log_entries = self._chart_log.get_entries(range_str, before=anchor_dt)
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

        # Historical views suppress forward-looking series (prediction + forecast).
        # They are meaningless for a window anchored in the past and would confuse
        # the chart by overlaying future data on a historical viewport.
        if is_historical:
            predicted_indoor = []
            forecast_outdoor = []
        else:
            predicted_indoor = [
                {"ts": p["ts"], "temp": _conv(p["temp"])}
                for p in _build_predicted_indoor_future(
                    self._hourly_forecast_temps,
                    self.config,
                    now,
                    current_indoor_temp=self._get_indoor_temp(),
                    thermal_model=thermal_model,
                    occupancy_mode=self._occupancy_mode,
                    classification=self._current_classification,
                )
            ]
            forecast_outdoor = [
                {"ts": p["ts"], "temp": _conv(p["temp"])}
                for p in _build_future_forecast_outdoor(
                    self._hourly_forecast_temps,
                    classification=self._current_classification,
                )
            ]

        def _conv_log_entry(e: dict) -> dict:
            e = dict(e)
            for k in ("pred_outdoor", "pred_indoor", "pred_outdoor_avg", "pred_indoor_avg"):
                if e.get(k) is not None:
                    e[k] = _conv(e[k])
            # Back-compat: old entries written before Issue #331 lack these keys.
            # Default to False so state_log always carries both fields.
            e.setdefault("fan_running", False)
            e.setdefault("nat_vent_active", False)
            return e

        log_entries = [_conv_log_entry(e) for e in log_entries]

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

        # Compute pre-cool band parameters for chart dip visualization
        _pc_trigger_h: float | None = None
        _pc_target: float | None = None
        if self._current_classification and self._current_classification.setback_modifier < 0:
            _pc_trigger_time = self._compute_pre_cool_trigger_time()
            if _pc_trigger_time is not None:
                _pc_trigger_h = _pc_trigger_time.hour + _pc_trigger_time.minute / 60.0
                from .const import CONF_SLEEP_COOL, PRE_COOL_MIN_HEADROOM_F

                _pc_sleep_cool = float(self.config.get(CONF_SLEEP_COOL) or self.config.get("sleep_cool", 78.0))
                _pc_comfort_heat = float(self.config.get("comfort_heat", 70.0))
                _pc_target = max(
                    _pc_sleep_cool + self._current_classification.setback_modifier,
                    _pc_comfort_heat + PRE_COOL_MIN_HEADROOM_F,
                )

        _raw_band = list(
            _compute_target_band_schedule(
                _band_timestamps,
                self.config,
                self._occupancy_mode,
                now,
                setback_modifier=(
                    getattr(self._current_classification, "setback_modifier", 0.0)
                    if self._current_classification is not None
                    else 0.0
                ),
                thermal_model=thermal_model,
                classification=self._current_classification,
                pre_cool_trigger_h=_pc_trigger_h,
                pre_cool_target=_pc_target,
            )
        )
        _hvac_mode = getattr(self._current_classification, "hvac_mode", None) if self._current_classification else None
        _conv_band = [{"ts": e["ts"], "lower": _conv(e["lower"]), "upper": _conv(e["upper"])} for e in _raw_band]

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
                "learning_health": thermal_model.get("learning_health", {}),
                "confidence_k_passive": thermal_model.get("confidence_k_passive", "none"),
                "k_passive": thermal_model.get("k_passive"),
                "k_vent": thermal_model.get("k_vent"),
                "k_vent_window": thermal_model.get("k_vent_window"),
                "k_solar": (
                    convert_delta(thermal_model["k_solar"], unit) if thermal_model.get("k_solar") is not None else None
                ),
                "avg_r_squared_passive": thermal_model.get("avg_r_squared_passive"),
                "last_observation_date": thermal_model.get("last_observation_date"),
                "observation_count_passive": thermal_model.get("observation_count_passive", 0),
                "observation_count_fan_only": thermal_model.get("observation_count_fan_only", 0),
                "observation_count_vent": thermal_model.get("observation_count_vent", 0),
                "observation_count_solar": thermal_model.get("observation_count_solar", 0),
                "swing_heat": round(convert_delta(thermal_model.get("swing_heat_f_display", 1.5), unit), 2),
                "swing_cool": round(convert_delta(thermal_model.get("swing_cool_f_display", 1.5), unit), 2),
                "swing_heat_measured": thermal_model.get("swing_heat_f") is not None,
                "swing_cool_measured": thermal_model.get("swing_cool_f") is not None,
                "observation_count_swing_heat": thermal_model.get("observation_count_swing_heat", 0),
                "observation_count_swing_cool": thermal_model.get("observation_count_swing_cool", 0),
                "confidence_swing_heat": thermal_model.get("confidence_swing_heat", "none"),
                "confidence_swing_cool": thermal_model.get("confidence_swing_cool", "none"),
                "solar_phase_offset_h": thermal_model.get("solar_phase_offset_h"),
            },
            "state_log": log_entries,
            "target_band": _conv_band,
            "predicted_setpoint": _derive_predicted_setpoint(_conv_band, _hvac_mode),
            "historical_setpoint": [
                {"ts": e["ts"], "setpoint": _conv(e["setpoint"])} for e in _extract_historical_setpoint(log_entries)
            ],
            "defense_lines": [] if is_historical else _compute_defense_lines(_conv_band),
            "predicted_activity": (
                []
                if is_historical
                else _compute_predicted_activity(
                    _conv_band,
                    forecast_outdoor,
                    predicted_indoor,
                    self._current_classification,
                    self.config,
                )
            ),
            "unit": unit,
        }

    def _build_thermal_pipeline_summary(self) -> dict:
        """Build a snapshot of the current thermal observation pipeline state."""
        self._ensure_pending_observations()
        now = dt_util.now()
        pending = []
        for obs_type, obs in self._pending_observations.items():
            start_str = obs.get("start_time")
            elapsed = None
            if start_str:
                try:
                    start_ts = dt_util.parse_datetime(start_str)
                    if start_ts:
                        elapsed = round((now - start_ts).total_seconds() / 60.0, 1)
                except Exception:
                    pass
            samples = obs.get("samples", obs.get("active_samples", []))
            temps = [s["indoor_temp_f"] for s in samples if "indoor_temp_f" in s]
            last_s = obs.get("last_sample_time")
            last_age = None
            if last_s:
                try:
                    last_ts = dt_util.parse_datetime(last_s)
                    if last_ts:
                        last_age = round((now - last_ts).total_seconds() / 60.0, 1)
                except Exception:
                    pass
            outdoor = samples[-1].get("outdoor_temp_f") if samples else getattr(self, "_last_outdoor_temp", None)
            pending.append(
                {
                    "obs_type": obs_type,
                    "status": obs.get("status", "unknown"),
                    "elapsed_minutes": elapsed,
                    "sample_count": len(samples),
                    "last_sample_age_minutes": last_age,
                    "indoor_range_f": [round(min(temps), 1), round(max(temps), 1)] if temps else None,
                    "indoor_delta_f": round(max(temps) - min(temps), 2) if temps else None,
                    "outdoor_f": round(outdoor, 1) if outdoor is not None else None,
                }
            )
        return {
            "pending": pending,
            "rejection_log_counts": {ot: len(evts) for ot, evts in getattr(self, "_rejection_log", {}).items()},
        }

    def compute_nat_vent_cycling_band(self) -> dict[str, float | None]:
        """Return the WHF fan's on/off cycling band (Issue #400/#402).

        Mirrors automation.py's nat_vent_temperature_check() sleep-window branch exactly,
        so the dashboard always matches the fan's actual cycling behavior. This is the
        single source of truth for the cycling target/on_threshold/off_threshold — extracted
        (Issue #402 follow-up) so get_debug_state() and the main status endpoint both call
        this instead of each recomputing the formula, which is exactly the "fix one
        duplicate implementation, miss the sibling" pattern that caused #400 and part of
        #402 in the first place.

        NOTE: despite the "target" name, these describe the WHF fan's on/off CYCLING
        midpoint — the range the fan hunts within while a nat-vent session is active. This
        is NOT a thermostat setpoint and is never written to the climate entity; do not
        confuse it with comfort_heat/comfort_cool or the armed comfort-band ceiling/floor.
        """
        ae = self.automation_engine
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        if not ae._natural_vent_active:
            return {"nat_vent_target": None, "nat_vent_on_threshold": None, "nat_vent_off_threshold": None}
        comfort_heat = float(self.config.get("comfort_heat", 70))
        comfort_cool = float(self.config.get("comfort_cool", 75))
        if _in_sleep_window(dt_util.now(), self.config):
            sleep_heat = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
            target = sleep_heat + hysteresis
        else:
            target = (comfort_heat + comfort_cool) / 2.0
        return {
            "nat_vent_target": target,
            "nat_vent_on_threshold": target + hysteresis,
            "nat_vent_off_threshold": target - hysteresis,
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

        _nat_vent_band = self.compute_nat_vent_cycling_band()

        return {
            "automation_enabled": self._automation_enabled,
            "occupancy_mode": self._occupancy_mode,
            "paused_by_door": ae.is_paused_by_door,
            "pause_suppressed_classification": (
                bool(ae.is_paused_by_door) and ae._last_classification_applied is not None
            ),
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
            "thermal_pipeline": self._build_thermal_pipeline_summary(),
            "startup_coalesce_active": self._startup_coalesce_active,
            "startup_coalesce_seconds_remaining": (
                max(
                    0.0,
                    (dt_util.parse_datetime(self._startup_coalesce_expiry) - dt_util.now()).total_seconds(),
                )
                if self._startup_coalesce_expiry and self._startup_coalesce_active
                else None
            ),
            # Issue #396: surface decision-lock holder so a stuck coalesce/decision pass is
            # diagnosable from the dashboard, not just backend logs — "waiting on X since Y"
            # instead of a generic "waiting for coalescing" with no further detail.
            "decision_lock_holder": ae._decision_lock_holder,
            "decision_lock_held_seconds": (
                (dt_util.now() - ae._decision_lock_held_since).total_seconds()
                if ae._decision_lock_held_since is not None
                else None
            ),
            # Bug 2 (Issue #321): stuck-grace detection for debug pane
            "grace_stuck": (
                ae._manual_override_active
                and not ae._grace_active
                and ae._grace_end_time is not None
                and dt_util.parse_datetime(ae._grace_end_time) is not None
                and dt_util.now() > dt_util.parse_datetime(ae._grace_end_time)
            ),
            # Bug 3 (Issue #321): nat-vent cycling visibility in debug pane
            "nat_vent_active": ae._natural_vent_active,
            # Issue #338: AC assist status — true when nat-vent is active with FAN_MODE_HVAC
            # and aggressive_savings is off (full comfort band armed, compressor may assist).
            # FAN_MODE_BOTH excluded: _activate_fan() suppresses HVAC for BOTH (same as WHOLE_HOUSE).
            "nat_vent_ac_assist": (
                bool(ae._natural_vent_active)
                and self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) == FAN_MODE_HVAC
                and not self.config.get("aggressive_savings", False)
            ),
            "nat_vent_target": _nat_vent_band["nat_vent_target"],
            "nat_vent_on_threshold": _nat_vent_band["nat_vent_on_threshold"],
            "nat_vent_off_threshold": _nat_vent_band["nat_vent_off_threshold"],
            "nat_vent_cycling_paused": ae._natural_vent_active and not ae._fan_active,
        }

    async def _persist_shutdown_diagnostics(self) -> None:
        """Persist restart-cause diagnostics (Issue #403/#413).

        Shared by async_shutdown() (fires on config-entry unload/reload) and the
        EVENT_HOMEASSISTANT_STOP listener registered in async_setup() (fires on a real
        HA restart/deploy, which does NOT call async_unload_entry). Both paths must
        write these fields for the restart-cause classifier in async_restore_state()
        to work on the restarts that actually happen in practice.
        """
        self.learning._state.clean_shutdown = True
        self.learning._state.last_shutdown_version = VERSION
        self.learning._state.user_initiated_restart = self._user_initiated_shutdown
        await self.hass.async_add_executor_job(self.learning.save_state)
        _LOGGER.info(
            "Shutdown diagnostics persisted: version=%s user_initiated=%s",
            VERSION,
            self._user_initiated_shutdown,
        )

    async def async_shutdown(self) -> None:
        """Clean up on shutdown."""
        _LOGGER.info("Climate Advisor v%s shutting down", VERSION)

        # Restart-cause diagnostics (Issue #403): mark this as a clean shutdown so the
        # next startup can distinguish a routine restart from a crash.
        await self._persist_shutdown_diagnostics()

        # Flush HVAC runtime and save state before cleanup
        self._flush_hvac_runtime()
        await self._async_save_state()

        # Cancel any pending occupancy away setback timer
        self._cancel_occupancy_away_timer()

        # Cancel any pending debounce timers
        for cancel in self._door_open_timers.values():
            cancel()
        self._door_open_timers.clear()
        self._door_open_timer_expiry.clear()

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
            "cold_diff": float,    # indoor-outdoor when outdoor < THERMAL_COLD_BUCKET_LIMIT_F
            "mild_diff": float,    # THERMAL_COLD_BUCKET_LIMIT_F <= outdoor < THERMAL_MILD_BUCKET_LIMIT_F
            "warm_diff": float,    # indoor-outdoor when outdoor >= THERMAL_MILD_BUCKET_LIMIT_F
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
        if outdoor < THERMAL_COLD_BUCKET_LIMIT_F:
            buckets["cold"].append(delta)
        elif outdoor < THERMAL_MILD_BUCKET_LIMIT_F:
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

    Linear interpolation over ±THERMAL_BUCKET_INTERP_HALF_F transition zones at bucket
    boundaries (THERMAL_COLD_BUCKET_LIMIT_F, THERMAL_MILD_BUCKET_LIMIT_F) eliminates the
    hard jump that occurs when outdoor crosses a threshold.
    """
    cold = thermal_factors.get("cold_diff", 15.0)
    mild = thermal_factors.get("mild_diff", 8.0)
    warm = thermal_factors.get("warm_diff", 0.0)

    _cold_lo = THERMAL_COLD_BUCKET_LIMIT_F - THERMAL_BUCKET_INTERP_HALF_F
    _cold_hi = THERMAL_COLD_BUCKET_LIMIT_F + THERMAL_BUCKET_INTERP_HALF_F
    _mild_lo = THERMAL_MILD_BUCKET_LIMIT_F - THERMAL_BUCKET_INTERP_HALF_F
    _mild_hi = THERMAL_MILD_BUCKET_LIMIT_F + THERMAL_BUCKET_INTERP_HALF_F

    if outdoor <= _cold_lo:
        return cold
    elif outdoor < _cold_hi:
        frac = (outdoor - _cold_lo) / (2 * THERMAL_BUCKET_INTERP_HALF_F)
        return cold + frac * (mild - cold)
    elif outdoor <= _mild_lo:
        return mild
    elif outdoor < _mild_hi:
        frac = (outdoor - _mild_lo) / (2 * THERMAL_BUCKET_INTERP_HALF_F)
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
    hvac_mode: str | None = None,
) -> float:
    """Advance indoor temperature by dt_hours using the two-parameter ODE.

    dT/dt = k_passive * (T - T_outdoor) + Q
    Q = k_active when HVAC is driving toward setpoint, 0 otherwise.

    Pass hvac_mode="heat" or "cool" for correct behavior with sleep setback setpoints
    (sleep_heat < comfort_heat). When hvac_mode is None, falls back to threshold
    inference — only valid for comfort-range setpoints.
    """
    import math

    k_p = k_passive
    q = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint:
                q = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint:
                q = -abs(k_active)
        else:
            # legacy: threshold inference — backward-compat for callers without hvac_mode
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


def _solar_factor(
    local_hour: int,
    phase_offset_h: float = THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT,
) -> float:
    """Return a 0–1 solar intensity factor for the given local hour.

    phase_offset_h shifts the effective peak: effective_hour = local_hour − offset.
    With offset=0 the peak is at local hour 13. With the default offset=2 the peak
    is at local hour 15 (3pm), matching typical thermal-mass lag.
    """
    try:
        h = int(local_hour)
    except (TypeError, ValueError):
        return 0.0
    effective_hour = h - int(round(phase_offset_h))
    if effective_hour < THERMAL_SOLAR_DAYTIME_START_H or effective_hour >= THERMAL_SOLAR_DAYTIME_END_H:
        return 0.0
    span = (THERMAL_SOLAR_DAYTIME_END_H - THERMAL_SOLAR_DAYTIME_START_H) / 2.0
    return math.sin(math.pi * (effective_hour - THERMAL_SOLAR_DAYTIME_START_H) / (span * 2))


def _estimate_solar_phase_offset(
    window_entries: list[dict],
) -> tuple[float | None, str | None]:
    """Estimate solar phase offset from a daytime passive window.

    Returns (phase_obs, None) on success, (None, reject_reason) on failure.
    phase_obs = actual_indoor_peak_hour − 13, clamped to [OFFSET_MIN, OFFSET_MAX].

    Quality gates:
      - ≥ THERMAL_SOLAR_PHASE_MIN_ENTRIES entries
      - window span ≥ THERMAL_SOLAR_PHASE_MIN_WINDOW_H hours
      - indoor ΔT ≥ THERMAL_SOLAR_PHASE_MIN_DT_F°F
      - peak is interior (not first or last entry)
    """
    if len(window_entries) < THERMAL_SOLAR_PHASE_MIN_ENTRIES:
        return None, REJECT_TOO_FEW_SAMPLES

    # Parse timestamps
    try:
        times = [datetime.fromisoformat(str(e["ts"])) for e in window_entries]
    except (KeyError, ValueError, TypeError):
        return None, REJECT_TOO_FEW_SAMPLES

    # Window span check
    span_h = (times[-1] - times[0]).total_seconds() / 3600.0
    if span_h < THERMAL_SOLAR_PHASE_MIN_WINDOW_H:
        return None, REJECT_WINDOW_TOO_SHORT

    # Extract indoor temps
    try:
        indoor_temps = [float(e["indoor"]) for e in window_entries]
    except (KeyError, ValueError, TypeError):
        return None, REJECT_SMALL_DELTA

    # Indoor ΔT check
    temp_range = max(indoor_temps) - min(indoor_temps)
    if temp_range < THERMAL_SOLAR_PHASE_MIN_DT_F:
        return None, REJECT_SMALL_DELTA

    # Peak must not be at the first entry — a first-entry peak means the window
    # captured the tail of a prior peak, not the rise. A last-entry peak is
    # acceptable: the window end may have truncated a still-rising temperature.
    peak_idx = indoor_temps.index(max(indoor_temps))
    if peak_idx == 0:
        return None, REJECT_NO_INTERIOR_PEAK

    # Peak local hour — prefer as_local(); fall back to raw UTC hour if the
    # as_local result is not a real datetime (e.g. in test stubs).
    peak_time = times[peak_idx]
    peak_local = dt_util.as_local(peak_time)
    peak_hour = peak_local.hour if isinstance(peak_local, datetime) else peak_time.hour

    # phase_obs = peak_hour − 13, clamped to [MIN, MAX]
    phase_obs = float(peak_hour - 13)
    phase_obs_clamped = max(
        float(THERMAL_SOLAR_PHASE_OFFSET_MIN),
        min(float(THERMAL_SOLAR_PHASE_OFFSET_MAX), phase_obs),
    )

    return phase_obs_clamped, None


def _entry_hour(entry: dict) -> int | None:
    """Parse local hour from a chart_log entry ts field. Returns None on failure."""
    try:
        return datetime.fromisoformat(entry["ts"]).hour
    except (KeyError, ValueError, TypeError):
        return None


def _is_ac_duty_solar_day(day_entries: list[dict]) -> tuple[bool, str]:
    """Quality filter for AC duty cycle solar phase estimation.

    Returns (True, "") if the day qualifies, or (False, reject_reason) otherwise.
    Pure function — no instance state.

    Quality gates (in order):
      1. At least one entry in 11:00-18:00 has setpoint_cool field.
      1b. Setpoint must be in [SETPOINT_MIN_F, SETPOINT_MAX_F].
      2. Setpoint spread across 11:00-18:00 < SETPOINT_STABILITY_F.
      3. >= AC_MIN_COOL_ENTRIES cool entries in 11:00-16:00.
      4. At least one 11:00-16:00 entry has indoor > median setpoint.
    """
    # Collect entries in the stability window (11:00-18:00)
    stability_entries = [
        e
        for e in day_entries
        if _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_STABILITY_WINDOW_END_H
    ]

    # Gate 1: must have setpoint_cool field in at least one stability-window entry
    setpoints = [e["setpoint_cool"] for e in stability_entries if e.get("setpoint_cool") is not None]
    if not setpoints:
        return False, REJECT_AC_NO_COOL_SETPOINTS

    # Gate 1b: setpoint must be in a reasonable range
    if min(setpoints) < THERMAL_SOLAR_PHASE_AC_SETPOINT_MIN_F or max(setpoints) > THERMAL_SOLAR_PHASE_AC_SETPOINT_MAX_F:
        return False, REJECT_AC_SETPOINT_OUT_OF_RANGE

    # Gate 2: setpoint must be stable across the stability window
    if max(setpoints) - min(setpoints) > THERMAL_SOLAR_PHASE_AC_SETPOINT_STABILITY_F:
        return False, REJECT_AC_SETPOINT_UNSTABLE

    # Gate 3: >= AC_MIN_COOL_ENTRIES cool entries in peak window (11:00-16:00)
    peak_cool_count = sum(
        1
        for e in day_entries
        if e.get("hvac") == "cool"
        and _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
    )
    if peak_cool_count < THERMAL_SOLAR_PHASE_AC_MIN_COOL_ENTRIES:
        return False, REJECT_AC_INSUFFICIENT_MIDDAY_ACTIVITY

    # Gate 4: at least one peak-window entry has indoor > median setpoint
    median_setpoint = sorted(setpoints)[len(setpoints) // 2]
    breach = any(
        e.get("indoor", 0) > median_setpoint
        for e in day_entries
        if _entry_hour(e) is not None
        and THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H <= _entry_hour(e) < THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
    )
    if not breach:
        return False, REJECT_AC_NO_SETPOINT_BREACH

    return True, ""


def _estimate_ac_duty_solar_phase(day_entries: list[dict]) -> float | None:
    """Estimate solar phase offset from AC duty cycle peak hour.

    Counts cool entries per hour in the 11:00-16:00 window, computes duty fraction,
    finds the peak-duty hour, and returns (peak_hour - 13) clamped to
    [THERMAL_SOLAR_PHASE_OFFSET_MIN, THERMAL_SOLAR_PHASE_OFFSET_MAX].

    Returns None if no cool entries exist in the window.
    Pure function — no instance state.
    """
    # Count cool and total entries per hour in 11:00-16:00 window
    cool_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for e in day_entries:
        h = _entry_hour(e)
        _start = THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_START_H
        _end = THERMAL_SOLAR_PHASE_AC_PEAK_WINDOW_END_H
        in_peak = h is not None and _start <= h < _end
        if not in_peak:
            continue
        total_counts[h] = total_counts.get(h, 0) + 1
        if e.get("hvac") == "cool":
            cool_counts[h] = cool_counts.get(h, 0) + 1

    if not cool_counts:
        return None

    # Duty fraction per hour
    duty = {h: cool_counts[h] / total_counts[h] for h in cool_counts if total_counts.get(h, 0) > 0}
    if not duty:
        return None

    peak_hour = max(duty, key=lambda h: duty[h])
    offset = float(peak_hour - 13)
    return max(float(THERMAL_SOLAR_PHASE_OFFSET_MIN), min(float(THERMAL_SOLAR_PHASE_OFFSET_MAX), offset))


def _simulate_indoor_physics_v3(
    t_start: float,
    t_outdoor: float,
    k_passive: float,
    k_active: float | None,
    dt_hours: float,
    setpoint: float | None,
    *,
    comfort_heat: float,
    comfort_cool: float,
    k_vent: float | None = None,
    k_solar: float | None = None,
    solar_factor: float = 0.0,
    ventilation_active: bool = False,
    hvac_mode: str | None = None,
) -> float:
    """Advance indoor temperature using the v3 ODE with ventilation and solar terms."""
    k_eff = k_passive + (k_vent if (ventilation_active and k_vent is not None) else 0.0)

    q_hvac = 0.0
    if setpoint is not None and k_active is not None:
        if hvac_mode == "heat":
            if t_start < setpoint:
                q_hvac = abs(k_active)
        elif hvac_mode == "cool":
            if t_start > setpoint:
                q_hvac = -abs(k_active)
        else:
            # legacy: threshold inference — backward-compat for callers without hvac_mode
            if setpoint >= comfort_heat and t_start < setpoint:
                q_hvac = abs(k_active)
            elif setpoint <= comfort_cool and t_start > setpoint:
                q_hvac = -abs(k_active)

    q_solar = (k_solar * solar_factor) if (k_solar is not None) else 0.0
    q_total = q_hvac + q_solar

    exp_keff = math.exp(k_eff * dt_hours)
    if k_eff != 0:
        t_next = t_outdoor + (t_start - t_outdoor) * exp_keff + (q_total / k_eff) * (exp_keff - 1)
    else:
        t_next = t_start + q_total * dt_hours

    if setpoint is not None:
        if q_hvac > 0:
            t_next = min(t_next, setpoint)
        elif q_hvac < 0:
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
    pre_cool_trigger_h: float | None = None,
    pre_cool_target: float | None = None,
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
    sleep_heat = float(config.get("sleep_heat", comfort_heat - DEFAULT_SETBACK_DEPTH_F))
    sleep_cool = float(config.get("sleep_cool", comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F))

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
                # Pre-wake: sleep band; apply pre-cool ceiling from trigger time onward
                lower = sleep_heat
                if pre_cool_trigger_h is not None and pre_cool_target is not None and h_n >= pre_cool_trigger_h:
                    upper = pre_cool_target
                else:
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
                # Post-sleep: sleep band; apply pre-cool ceiling from trigger time onward
                lower = sleep_heat
                if pre_cool_trigger_h is not None and pre_cool_target is not None and h_n >= pre_cool_trigger_h:
                    upper = pre_cool_target
                else:
                    upper = sleep_cool

        result.append({"ts": ts.isoformat(), "lower": round(lower, 1), "upper": round(upper, 1)})

    return result


def _find_ceiling_breach_time(
    predicted_indoor: list[dict] | None,
    comfort_cool: float,
    tolerance: float = 0.0,
) -> datetime | None:
    """Return the first timestamp in predicted_indoor where temp > comfort_cool + tolerance.

    Args:
        predicted_indoor: List of {"ts": ISO-string, "temp": float} dicts from ODE curve.
        comfort_cool: Upper comfort bound (°F).
        tolerance: Additional threshold buffer (°F). Use CEILING_BRIDGE_TOLERANCE_F for
            bridge homes where k_vent_window proxy is less accurate for closed-window phase.

    Returns:
        datetime of first breach entry, or None if no breach or empty curve.
    """
    if not predicted_indoor:
        return None
    threshold = comfort_cool + tolerance
    for entry in predicted_indoor:
        temp = entry.get("temp")
        if temp is not None and temp > threshold:
            ts_str = entry.get("ts")
            if ts_str:
                try:
                    return datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue
    return None


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
        if classification is not None:
            _LOGGER.debug("_build_predicted_indoor_future: no hourly_forecast — using cosine fallback")
            # Build synthetic hourly list from cosine model so the function can proceed normally
            now_local = dt_util.as_local(dt_util.now())
            cosine = _build_outdoor_curve(
                high=classification.today_high,
                low=classification.today_low,
                hourly_forecast=None,
            )
            synthetic = []
            for entry in cosine:
                h = entry["hour"]
                future_dt = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
                if future_dt <= now_local:
                    future_dt += timedelta(days=1)
                synthetic.append(
                    {
                        "datetime": future_dt.isoformat(),
                        "temperature": entry["temp"],
                    }
                )
            hourly_forecast = synthetic
        else:
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
    # Override today's mode with the current classification — _day_mode() only sees
    # remaining forecast entries, which in the evening are cold night temps (max<60°F
    # even on a 68°F day), causing a spurious "heat" mode that triggers the Q_hvac bug.
    _today_date = dt_util.as_local(now).date()
    if classification is not None and hasattr(classification, "hvac_mode"):
        day_modes[_today_date] = classification.hvac_mode
        _LOGGER.debug(
            "_build_predicted_indoor_future: today mode overridden from classification: %s",
            classification.hvac_mode,
        )
    _LOGGER.debug(
        "_build_predicted_indoor_future: %d days classified: %s",
        len(day_modes),
        {str(d): m for d, m in sorted(day_modes.items())},
    )

    # Decide whether to use physics simulation or setpoint-schedule fallback.
    # Physics requires: k_passive from any confident source, and a seed temp.
    _use_physics = False
    _k_passive: float | None = None
    _k_active_heat: float | None = None
    _k_active_cool: float | None = None
    _k_vent: float | None = None
    _k_solar: float | None = None
    _k_vent_window: float | None = None
    _k_passive_via_bridge: bool = False
    # _phase_offset: when model has a learned value use it; otherwise 0.0 preserves
    # pre-feature behavior for callers that do not supply solar_phase_offset_h.
    # (THERMAL_SOLAR_PHASE_OFFSET_H_DEFAULT=2 is applied by the coordinator's
    # self._solar_phase_offset instance attribute, not by this standalone function.)
    _phase_offset: float = 0.0
    if thermal_model and current_indoor_temp is not None:
        _conf = thermal_model.get("confidence", "none")
        _conf_k_passive = thermal_model.get("confidence_k_passive")
        _k_passive = thermal_model.get("k_passive")
        _k_active_heat = thermal_model.get("k_active_heat")
        _k_active_cool = thermal_model.get("k_active_cool")
        _k_vent = thermal_model.get("k_vent")
        _k_solar = thermal_model.get("k_solar")
        _k_vent_window = thermal_model.get("k_vent_window")
        _raw_phase = thermal_model.get("solar_phase_offset_h")
        _phase_offset = float(_raw_phase) if _raw_phase is not None else 0.0
        # Gate bridge: when k_passive is absent but k_vent_window is learned, use it as
        # a proxy k_passive so the ODE can activate for thermally inert homes that only
        # have ventilated observations.  k_vent_window is always ≤ 0 for valid commits
        # (inert home → k≈0 accepted by widened ventilated bounds in learning.py).
        # When k_vent_window = 0.0 exactly the ODE produces a flat prediction (T stays at
        # current_indoor_temp), which is correct for a perfectly inert home.
        _k_passive_via_bridge = False
        if (_k_passive is None or _conf_k_passive == "none") and _k_vent_window is not None and _k_vent_window <= 0:
            _k_passive = _k_vent_window
            _k_passive_via_bridge = True
            _LOGGER.debug(
                "_build_predicted_indoor_future: gate bridge — using k_vent_window=%.4f as proxy k_passive",
                _k_passive,
            )
        _physics_eligible = (
            (
                _conf != "none"
                or (_conf_k_passive is not None and _conf_k_passive not in (None, "none"))
                or _k_passive_via_bridge  # bridge-provided k needs no confidence count
            )
            and _k_passive is not None
            and (_k_passive < 0 or _k_passive_via_bridge)
        )
        if _physics_eligible:
            _use_physics = True
            _LOGGER.debug(
                "_build_predicted_indoor_future: using physics model "
                "(conf=%s conf_k_passive=%s k_passive=%.4f k_active_heat=%s k_active_cool=%s)",
                _conf,
                _conf_k_passive,
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
    # Compute pre-cool band parameters so the prediction curve tracks the pre-cool setpoint
    _ode_pc_trigger_h: float | None = None
    _ode_pc_target: float | None = None
    _setback_mod = getattr(classification, "setback_modifier", None)
    if isinstance(_setback_mod, (int, float)) and _setback_mod < 0:
        from .const import (
            CONF_SLEEP_COOL,
            PRE_COOL_MIN_HEADROOM_F,
            PRE_COOL_POST_NAT_VENT_DELAY_MINUTES,
            PRE_COOL_WAKE_OFFSET_HOURS,
        )

        _wct = getattr(classification, "window_close_time", None)
        if _wct is not None:
            _ode_pc_trigger_h = _wct.hour + _wct.minute / 60.0 + PRE_COOL_POST_NAT_VENT_DELAY_MINUTES / 60.0
        else:
            _wake_str = config.get("wake_time", "06:30")
            _wake_h_raw = int(_wake_str.split(":")[0]) + int(_wake_str.split(":")[1]) / 60.0
            _ode_pc_trigger_h = _wake_h_raw - PRE_COOL_WAKE_OFFSET_HOURS
        _ode_sc = float(config.get(CONF_SLEEP_COOL) or config.get("sleep_cool", 78.0))
        _ode_ch = float(config.get("comfort_heat", 70.0))
        _ode_pc_target = max(
            _ode_sc + classification.setback_modifier,
            _ode_ch + PRE_COOL_MIN_HEADROOM_F,
        )

    _band_schedule = _compute_target_band_schedule(
        _future_timestamps_for_band,
        _band_config,
        occupancy_mode,
        now,
        thermal_model=thermal_model,
        classification=classification,
        pre_cool_trigger_h=_ode_pc_trigger_h,
        pre_cool_target=_ode_pc_target,
    )
    _band_lookup: dict[str, dict] = {b["ts"]: b for b in _band_schedule}

    # Pre-compute window schedule for per-hour ventilation switching (Phase 2C).
    # k_vent_window is the total measured k during ventilated conditions — replacement
    # semantics, not addition. Window-open hours substitute k_vent_window for k_passive.
    _windows_recommended = bool(classification.windows_recommended) if classification else False
    _window_open_time = getattr(classification, "window_open_time", None) if classification else None
    _window_close_time = getattr(classification, "window_close_time", None) if classification else None

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

        # Per-hour window-open check (computed before bridge guard so both can reference it).
        # Guard: skip substitution when gate bridge already used k_vent_window as
        # k_passive for all hours (_k_passive_via_bridge=True).
        _hour_windows_open = (
            _windows_recommended
            and _k_vent_window is not None
            and _window_open_time is not None
            and _window_close_time is not None
            and _window_open_time <= local_ts.time() < _window_close_time
        )

        # Bridge guard: k_vent_window is measured during open-window conditions
        # (envelope k + ventilation k).  Applying it to window-closed hours overpredicts
        # decay (τ≈7h) — the true envelope τ is much longer (≈50h).  Fall back to ramp
        # only when the classification schedules windows for today but the current hour
        # falls outside the open window.  When windows are not recommended at all (no
        # window schedule), k_vent_window is the best available proxy and physics runs
        # for all hours (behaviour consistent with pre-guard bridge semantics).
        _bridge_guard_applies = (
            _k_passive_via_bridge
            and _windows_recommended  # classification has a window schedule today
            and not _hour_windows_open  # but this hour is outside the open window
        )
        _use_physics_for_hour = _use_physics and not _bridge_guard_applies
        if _bridge_guard_applies and _use_physics:
            _LOGGER.debug(
                "_build_predicted_indoor_future: bridge hour=%s windows-closed, using ramp "
                "(k_vent_window not valid for envelope-only decay)",
                local_ts.strftime("%H:%M"),
            )

        if _use_physics_for_hour and _t_current is not None and outdoor is not None:
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

            # Per-hour k selection: window-open hours use k_vent_window (total ventilated
            # rate) as a replacement for k_passive. k_vent_window is measured as the total
            # effective k during ventilated conditions — replacement semantics, not addition.
            _k_passive_for_hour = _k_vent_window if (_hour_windows_open and not _k_passive_via_bridge) else _k_passive
            if _hour_windows_open and not _k_passive_via_bridge:
                _LOGGER.debug(
                    "_build_predicted_indoor_future: hour=%s using k_vent_window=%.4f (windows open %s–%s)",
                    local_ts.strftime("%H:%M"),
                    _k_vent_window,
                    _window_open_time,
                    _window_close_time,
                )

            if _k_solar is not None or _k_vent is not None:
                _t_current = _simulate_indoor_physics_v3(
                    _t_current,
                    float(outdoor),
                    _k_passive_for_hour,  # type: ignore[arg-type]
                    k_active_for_mode,
                    dt_hours,
                    setpoint,
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                    k_vent=_k_vent,
                    k_solar=_k_solar,
                    solar_factor=_solar_factor(local_ts.hour, _phase_offset),
                    ventilation_active=False,
                    hvac_mode=mode,
                )
            else:
                _t_current = _simulate_indoor_physics(
                    _t_current,
                    float(outdoor),
                    _k_passive_for_hour,  # type: ignore[arg-type]
                    k_active_for_mode,
                    dt_hours,
                    setpoint,
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                    hvac_mode=mode,
                )
            temp = _t_current
        else:
            # Setpoint-schedule fallback
            if mode == "heat":
                temp = _band["lower"]
            elif mode == "cool":
                temp = _band["upper"]
            else:
                # Off-day ramp: anchor to current indoor when available — a stable home
                # sitting at 69°F is better predicted by its actual reading than by
                # outdoor+2°F (which would be ~58°F on a cold day).  Fall back to
                # outdoor+2°F only when no indoor seed exists.
                if _t_current is not None:
                    temp = _t_current
                elif outdoor is not None:
                    temp = max(setback_heat, float(outdoor) + 2.0)
                else:
                    temp = comfort_heat

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

    # Expand hourly ODE output to 30-min resolution via linear interpolation.
    # This gives the prediction archive 30-min granularity matching chart_log cadence
    # and eliminates the step-function artifact on the historical chart.
    _interp: list[dict] = []
    for _i, _pt in enumerate(result):
        _interp.append(_pt)
        if _i + 1 < len(result):
            _next = result[_i + 1]
            try:
                _pt_dt = datetime.fromisoformat(_pt["ts"])
                _next_dt = datetime.fromisoformat(_next["ts"])
            except (ValueError, KeyError):
                continue
            _mid_dt = _pt_dt + (_next_dt - _pt_dt) / 2
            _mid_temp = round((_pt["temp"] + _next["temp"]) / 2, 1)
            _interp.append({"ts": _mid_dt.isoformat(), "temp": _mid_temp})
    result = _interp

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
    classification: Any | None = None,
) -> list[dict]:
    """Extract future hourly outdoor temps from the weather forecast.

    Returns all entries at or after now as {"ts": ISO_string, "temp": float}.
    Covers all available forecast days (2-10+), not just today.
    Unlike _build_outdoor_curve, values are NOT normalised to daily high/low —
    the raw forecast temperatures are used directly.

    If hourly_forecast is empty or yields no future entries and classification
    is provided, falls back to a cosine curve using today's high/low so the
    chart future region is never blank on daily-only weather integrations.
    """
    now = dt_util.now()
    result = []
    if hourly_forecast:
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
    if not result and classification is not None:
        # Hourly forecast unavailable — build cosine curve for display
        cosine = _cosine_outdoor_curve(classification.today_high, classification.today_low)
        for entry in cosine:
            h = entry["hour"]
            future_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=h)
            if future_dt < now:
                future_dt += timedelta(days=1)
            result.append({"ts": future_dt.isoformat(), "temp": round(float(entry["temp"]), 1)})
    result.sort(key=lambda x: x["ts"])
    return result


def _extract_current_hour_forecast_temp(
    hourly_forecast: list[dict] | None,
    now: datetime,
) -> float | None:
    """Return the forecast temp for the entry nearest to now, within ±2 hours.

    HA's hourly forecast returns entries starting at the next full hour, so
    exact hour matching would never find the current hour. Instead, find the
    entry with minimum absolute time delta to now.
    """
    if not hourly_forecast:
        return None
    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    best_temp: float | None = None
    best_delta: float = float("inf")
    for entry in hourly_forecast:
        dt_str = entry.get("datetime") or entry.get("time")
        temp = entry.get("temperature") if entry.get("temperature") is not None else entry.get("temp")
        if dt_str is None or temp is None:
            continue
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            entry_utc = dt_obj.replace(tzinfo=UTC) if dt_obj.tzinfo is None else dt_obj.astimezone(UTC)
            delta = abs((entry_utc - now_utc).total_seconds())
            if delta < best_delta and delta <= 7200:
                best_delta = delta
                best_temp = round(float(temp), 1)
        except (ValueError, TypeError):
            continue
    return best_temp


def _derive_predicted_setpoint(
    target_band: list[dict],
    hvac_mode: str | None,
) -> list[dict]:
    """Derive predicted setpoint list from target_band entries.

    Heat mode: lower bound; cool mode: upper bound; off/None: null.
    """
    result = []
    for entry in target_band:
        ts = entry.get("ts")
        if hvac_mode == "heat":
            sp = entry.get("lower")
        elif hvac_mode == "cool":
            sp = entry.get("upper")
        else:
            sp = None
        result.append({"ts": ts, "setpoint": sp})
    return result


def _extract_historical_setpoint(log_entries: list[dict]) -> list[dict]:
    """Extract {ts, setpoint} pairs from state_log entries."""
    result = []
    for e in log_entries:
        ts = e.get("ts")
        if not ts:
            continue
        result.append({"ts": ts, "setpoint": e.get("setpoint")})
    return result


def _compute_defense_lines(target_band: list[dict]) -> list[dict]:
    """Return [{ts, heat, cool}] from target_band — always both bounds, never null.

    Unlike _derive_predicted_setpoint (single bound per hvac_mode), this always
    exposes both the heat-defense threshold (lower) and cool-defense threshold (upper)
    so the frontend can render them as always-present automation intent lines.
    """
    return [{"ts": e["ts"], "heat": e.get("lower"), "cool": e.get("upper")} for e in target_band]


def _compute_predicted_activity(
    target_band: list[dict],
    forecast_outdoor: list[dict],
    predicted_indoor: list[dict],
    classification: Any | None,
    config: dict,
) -> list[dict]:
    """Per forecast hour: hvac_mode intent, fan_active, windows_recommended.

    All temperature values must be in the same display unit; band bounds are used
    for comparisons so no separate comfort-temp conversion is needed.
    """
    outdoor_by_ts = {e["ts"]: e.get("temp") for e in forecast_outdoor if e.get("ts")}
    indoor_by_ts = {e["ts"]: e.get("temp") for e in predicted_indoor if e.get("ts")}
    hvac_mode = getattr(classification, "hvac_mode", "off") if classification is not None else "off"
    fan_mode = str(config.get("fan_mode", "auto"))
    natural_vent_delta = float(config.get("natural_vent_delta", 5.0))

    result = []
    for band_entry in target_band:
        ts = band_entry.get("ts")
        if not ts:
            continue
        band_lower = band_entry.get("lower")
        band_upper = band_entry.get("upper")
        outdoor = outdoor_by_ts.get(ts)
        indoor = indoor_by_ts.get(ts)

        if fan_mode == "on":
            fan_active = True
        elif outdoor is not None and indoor is not None and band_upper is not None:
            fan_active = bool(outdoor < indoor and outdoor < band_upper + natural_vent_delta and indoor > band_upper)
        else:
            fan_active = False

        if outdoor is not None and indoor is not None and band_lower is not None and band_upper is not None:
            windows_recommended = bool(
                outdoor >= band_lower and outdoor <= band_upper + 2.0 and outdoor < indoor and indoor > band_upper
            )
        else:
            windows_recommended = False

        result.append(
            {
                "ts": ts,
                "hvac_mode": hvac_mode,
                "fan_active": fan_active,
                "windows_recommended": windows_recommended,
            }
        )
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
