"""Automation engine for Climate Advisor.

Manages the creation and dynamic adjustment of Home Assistant automations
based on the day classification and learning state.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .classifier import DayClassification
from .const import (
    CEILING_BRIDGE_TOLERANCE_F,
    CEILING_ESCALATION_SAVINGS_MARGIN_F,
    CEILING_PRECOOL_FALLBACK_MIN,
    CONF_ADAPTIVE_PREHEAT,
    CONF_ADAPTIVE_SETBACK,
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_FAN_ENTITY,
    CONF_FAN_MIN_RUNTIME_PER_HOUR,
    CONF_FAN_MODE,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_NAT_VENT_HYSTERESIS_F,
    CONF_NAT_VENT_REACTIVATION_LOCKOUT_S,
    CONF_NATURAL_VENT_DELTA,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_WELCOME_HOME_DEBOUNCE,
    DAY_TYPE_HOT,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_FAN_MIN_RUNTIME_PER_HOUR,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_NATURAL_VENT_DELTA,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    ECONOMIZER_EVENING_END_HOUR,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_MORNING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    MIN_VIABLE_NAT_VENT_HOURS,
    NAT_VENT_HYSTERESIS_F,
    NAT_VENT_REACTIVATION_LOCKOUT_S,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
    REVISIT_DELAY_SECONDS,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    VACATION_SETBACK_EXTRA,
)
from .temperature import format_temp, format_temp_delta, from_fahrenheit, to_fahrenheit

_LOGGER = logging.getLogger(__name__)


def _parse_forecast_dt(dt_str: str | None) -> datetime | None:
    """Parse an ISO 8601 forecast datetime string; return None on failure."""
    if not dt_str:
        return None
    try:
        return dt_util.parse_datetime(dt_str)
    except Exception:  # noqa: BLE001
        return None


def compute_bedtime_setback(
    config: dict,
    thermal_model: dict | None,
    c: DayClassification,
) -> float:
    """Compute bedtime setback target temperature using thermal model if available.

    Uses learned heating/cooling rates to compute the maximum safe setback depth
    that can be recovered from by wake_time. Falls back to hardcoded defaults when
    the thermal model has insufficient data.

    Returns the setback TARGET temperature (not the depth).
    """
    from .const import (
        CONF_MAX_SETBACK_DEPTH,
        CONF_SLEEP_COOL,
        CONF_SLEEP_HEAT,
        DEFAULT_SETBACK_DEPTH_COOL_F,
        DEFAULT_SETBACK_DEPTH_F,
        MAX_SETBACK_DEPTH_F,
        SETBACK_RECOVERY_BUFFER_MINUTES,
    )

    hvac_mode = c.hvac_mode
    setback_modifier = c.setback_modifier

    if hvac_mode == "heat":
        comfort = config.get("comfort_heat", 70)
        floor = config.get("setback_heat", 60)
        rate = (thermal_model or {}).get("heating_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_F
        # Explicit sleep temp takes priority over adaptive calculation
        _explicit = config.get(CONF_SLEEP_HEAT)
        if _explicit is not None:
            return max(float(_explicit) + setback_modifier, floor)
    elif hvac_mode == "cool":
        comfort = config.get("comfort_cool", 75)
        floor = config.get("setback_cool", 80)
        rate = (thermal_model or {}).get("cooling_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_COOL_F
        setback_modifier = -setback_modifier  # cool setback goes up, not down
        # Explicit sleep temp takes priority over adaptive calculation
        _explicit = config.get(CONF_SLEEP_COOL)
        if _explicit is not None:
            return min(float(_explicit) + setback_modifier, floor)
    else:
        return config.get("comfort_heat", 70)

    if not config.get("learning_enabled", True) or not config.get(CONF_ADAPTIVE_SETBACK, True):
        _LOGGER.debug(
            "Adaptive setback disabled — using default depth %.1f°F (%s mode)",
            DEFAULT_SETBACK_DEPTH_F if hvac_mode == "heat" else DEFAULT_SETBACK_DEPTH_COOL_F,
            hvac_mode,
        )
        thermal_model = {}
        rate = None

    confidence = (thermal_model or {}).get("confidence", "none")
    if confidence == "none" or rate is None or rate <= 0:
        depth = default_depth
    else:
        # Parse wake and sleep times to compute overnight duration
        wake_str = config.get("wake_time", "06:30")
        sleep_str = config.get("sleep_time", "22:30")
        wake_parts = wake_str.split(":")
        sleep_parts = sleep_str.split(":")
        wake_minutes = int(wake_parts[0]) * 60 + int(wake_parts[1])
        sleep_minutes = int(sleep_parts[0]) * 60 + int(sleep_parts[1])
        if wake_minutes <= sleep_minutes:
            wake_minutes += 24 * 60  # crosses midnight
        overnight_minutes = wake_minutes - sleep_minutes
        available = overnight_minutes - SETBACK_RECOVERY_BUFFER_MINUTES
        max_recoverable = rate * (available / 60.0)
        max_depth = config.get(CONF_MAX_SETBACK_DEPTH, MAX_SETBACK_DEPTH_F)
        _LOGGER.debug("Max setback depth: %.1f°F (config=%s)", max_depth, CONF_MAX_SETBACK_DEPTH in config)
        depth = min(max(max_recoverable, 0.0), max_depth)
        if hvac_mode == "heat":
            _adaptive_target = max(comfort - depth + setback_modifier, floor)
        else:
            _adaptive_target = min(comfort + depth + setback_modifier, floor)
        _LOGGER.debug(
            "Adaptive setback: rate=%.2f°F/hr overnight=%.0fmin → depth=%.1f°F target=%.1f°F (%s mode)",
            rate,
            available,
            depth,
            _adaptive_target,
            hvac_mode,
        )

    if hvac_mode == "heat":
        raw = comfort - depth + setback_modifier
        return max(raw, floor)
    else:  # cool
        raw = comfort + depth + setback_modifier
        return min(raw, floor)


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

        # Dry-run mode: when True, all service calls are logged but skipped
        self.dry_run: bool = False

        # Grace period state
        self._manual_grace_cancel: Any | None = None
        self._automation_grace_cancel: Any | None = None
        self._grace_active = False
        self._last_resume_source: str | None = None
        self._grace_end_time: str | None = None
        self._grace_duration_seconds: int = 0

        # Economizer state (two-phase window cooling per Issue #27)
        # Phase "cool-down": AC runs to cool to set temp (outdoor air assists)
        # Phase "maintain": AC off, natural ventilation holds temp
        self._economizer_active: bool = False
        self._economizer_phase: str = "inactive"  # "inactive", "cool-down", "maintain"

        # Action tracking (Issue #37)
        self._last_action_time: str | None = None
        self._last_action_reason: str | None = None

        # Revisit scheduling — 5-min follow-up after any HVAC action
        self._revisit_cancel: Any | None = None
        self._revisit_callback: Any | None = None  # Set by coordinator

        # Manual override protection — prevents classification from
        # overriding user's manual thermostat changes
        self._manual_override_active: bool = False
        self._manual_override_mode: str | None = None
        self._manual_override_time: str | None = None

        # Fan state tracking (Issue #37)
        self._fan_active: bool = False
        self._fan_on_since: str | None = None  # ISO timestamp
        self._fan_override_active: bool = False
        self._fan_override_time: str | None = None
        self._fan_command_pending: bool = False  # transient: distinguishes integration vs manual changes
        self._hvac_command_pending: bool = False  # transient: distinguishes integration vs manual HVAC changes
        self._temp_command_pending: bool = False  # transient: distinguishes integration vs manual temp changes
        self._temp_command_time: datetime | None = None  # last system-initiated temp setpoint command timestamp
        self._hvac_command_time: datetime | None = None  # last system-initiated HVAC command timestamp
        self._last_commanded_hvac_mode: str | None = None  # expected-state tracking: last mode automation commanded
        self._last_commanded_hvac_time: datetime | None = None  # expected-state tracking: when it was commanded

        # Natural ventilation mode (Issue #73)
        self._natural_vent_active: bool = False
        self._last_outdoor_temp: float | None = None
        # Timestamp of last outdoor-warm exit (outdoor ≥ indoor → pause).
        # Used for hysteresis lockout. Not serialized — resets on HA restart (acceptable for 5-min window).
        self._nat_vent_outdoor_exit_time: datetime | None = None

        # Override confirmation period (Issue #76) — pending window before override is formally accepted
        self._override_confirm_pending: bool = False
        self._override_confirm_cancel: Any | None = None
        self._override_confirm_time: str | None = None
        self._override_confirm_mode: str | None = None
        self._override_confirm_source: str | None = None  # "setpoint" or "normal"

        # Minimum fan runtime per hour — rolling cycle (Issue #77)
        self._fan_min_runtime_active: bool = False  # True if THIS feature activated the fan
        self._fan_min_cycle_cancel: Any | None = None  # cancel token for pending on/off timer

        # Event log callback — set by coordinator after construction
        self._emit_event_callback: Any | None = None

        # Today's DailyRecord — set by coordinator; used for bedtime setback tracking
        self._today_record: Any | None = None

        # Issue #96: classification event dedup — track last emitted (day_type, hvac_mode) pair
        self._last_classification_applied: tuple[str, str] | None = None
        # Issue #96: override event dedup — track last emission time
        self._last_override_detected_time: datetime | None = None

        # Resume-from-pause tracking (Issue #47)
        self._resumed_from_pause: bool = False
        self._sensor_check_callback: Any | None = None  # Set by coordinator: returns True if any sensor open

        # Welcome home notification debounce (Issue #59)
        self._last_welcome_home_notified: datetime | None = None

        # Thermal model — set by coordinator before apply_classification()
        self._thermal_model: dict = {}

        # Hourly forecast temps — injected by coordinator on each 30-min poll
        self._hourly_forecast_temps: list[dict] = []

        # Occupancy mode — synced by coordinator (Issue #85)
        self._occupancy_mode: str = OCCUPANCY_HOME

    async def _notify(self, message: str, title: str, notification_type: str) -> None:
        """Send a notification via configured channels, filtered by per-event preferences."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would send notification: %s — %s", title, message)
            return
        push_key = f"push_{notification_type}"
        email_key = f"email_{notification_type}"
        service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
        if self.config.get(push_key, True):
            await self.hass.services.async_call("notify", service_name, {"message": message, "title": title})
        if self.config.get(email_key, True):
            await self.hass.services.async_call("notify", "send_email", {"message": message, "title": title})

    @property
    def is_paused_by_door(self) -> bool:
        """Whether HVAC is currently paused due to an open door/window."""
        return self._paused_by_door

    @property
    def natural_vent_active(self) -> bool:
        """Whether natural ventilation mode is currently active."""
        return self._natural_vent_active

    _VALID_OCCUPANCY_MODES = {OCCUPANCY_HOME, OCCUPANCY_AWAY, OCCUPANCY_VACATION, OCCUPANCY_GUEST}

    def set_occupancy_mode(self, mode: str) -> None:
        """Update the engine's occupancy mode (synced by coordinator)."""
        if mode not in self._VALID_OCCUPANCY_MODES:
            _LOGGER.warning("Invalid occupancy mode %r — defaulting to home", mode)
            mode = OCCUPANCY_HOME
        if mode != self._occupancy_mode:
            _LOGGER.info("Occupancy mode changed: %s → %s", self._occupancy_mode, mode)
        self._occupancy_mode = mode

    def update_outdoor_temp(self, temp: float | None) -> None:
        """Update the cached outdoor temperature used for natural vent decisions."""
        self._last_outdoor_temp = temp

    def _is_within_planned_window_period(self) -> bool:
        """Check if windows are recommended AND we're within the window period.

        Returns True when ALL conditions hold:
        1. Classification exists with windows_recommended=True
        2. HVAC mode is "off" (no active heating/cooling to protect)
        3. Current time is between window_open_time and window_close_time

        When True, door/window sensor events should NOT trigger pause,
        grace periods, or notifications — the user is following the plan.
        """
        c = self._current_classification
        if not c or not c.windows_recommended:
            return False
        if c.hvac_mode != "off":
            return False
        if not c.window_open_time or not c.window_close_time:
            return False
        now_time = dt_util.now().time()
        return c.window_open_time <= now_time <= c.window_close_time

    def _record_action(self, action: str, reason: str) -> None:
        """Record an HVAC action with timestamp and reason, and schedule a revisit."""
        self._last_action_time = dt_util.now().isoformat()
        self._last_action_reason = f"{action} — {reason}"
        _LOGGER.warning("Action recorded: %s", self._last_action_reason)
        self._schedule_revisit()

    def _schedule_revisit(self) -> None:
        """Schedule a follow-up re-evaluation after an HVAC action."""
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None

        if not self._revisit_callback:
            return

        revisit_cb = self._revisit_callback

        @callback
        def _revisit_fired(_now: Any) -> None:
            self._revisit_cancel = None
            _LOGGER.info("Revisit check triggered (5-min follow-up after action)")
            self.hass.async_create_task(revisit_cb())

        self._revisit_cancel = async_call_later(self.hass, REVISIT_DELAY_SECONDS, _revisit_fired)

    def clear_manual_override(self, reason: str = "grace_expired") -> None:
        """Clear the manual override flag (called at transition points)."""
        if self._override_confirm_pending:
            if self._override_confirm_cancel:
                self._override_confirm_cancel()
                self._override_confirm_cancel = None
            self._override_confirm_pending = False
            self._override_confirm_time = None
            self._override_confirm_mode = None
            self._override_confirm_source = None
        if self._manual_override_active:
            if self._emit_event_callback:
                _cs = self.hass.states.get(self.climate_entity) if self.hass else None
                _old_setpoint_raw = _cs.attributes.get("temperature") if _cs else None
                self._emit_event_callback(
                    "override_cleared",
                    {
                        "was_mode": self._manual_override_mode,
                        "active_since": self._manual_override_time,
                        "old_setpoint_f": _old_setpoint_raw,
                    },
                )
            _LOGGER.info(
                "Clearing manual override — reason=%s (was %s since %s)",
                reason,
                self._manual_override_mode,
                self._manual_override_time,
            )
            self._manual_override_active = False
            self._manual_override_mode = None
            self._manual_override_time = None
        self._resumed_from_pause = False
        self.clear_fan_override()

    def _get_fan_runtime_minutes(self) -> float:
        """Return how many minutes the fan has been running, or 0.0 if inactive."""
        if not self._fan_active or not self._fan_on_since:
            return 0.0
        try:
            from datetime import datetime as _dt_cls

            on_since = _dt_cls.fromisoformat(self._fan_on_since)
            if on_since.tzinfo is None:
                on_since = on_since.replace(tzinfo=UTC)
            now = dt_util.now()
            if not isinstance(now, _dt_cls):
                return 0.0
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            delta = (now - on_since).total_seconds() / 60.0
            return max(0.0, delta)
        except Exception:
            return 0.0

    def handle_fan_manual_override(self) -> None:
        """Handle a manual fan state change — sets fan override flag + grace."""
        self._stop_fan_min_runtime_cycles()
        self._fan_override_active = True
        self._fan_override_time = dt_util.now().isoformat()
        _LOGGER.warning(
            "Fan manual override activated at %s",
            self._fan_override_time,
        )
        self._start_grace_period("manual", trigger="fan_manual_override")

    def clear_fan_override(self) -> None:
        """Clear the fan override flag (called at transition points)."""
        if self._fan_override_active:
            _LOGGER.info(
                "Clearing fan manual override (since %s)",
                self._fan_override_time,
            )
            self._fan_override_active = False
            self._fan_override_time = None
            # Restart the min-runtime cycle that was suspended when override was set
            self.hass.async_create_task(self.start_min_fan_runtime_cycles())

    async def start_min_fan_runtime_cycles(self) -> None:
        """Start rolling minimum fan runtime cycles (not clock-aligned).

        Called once at coordinator startup and when fan override is cleared.
        Cancels any existing cycle before starting a new one. The cycle
        start time is offset from the clock hour by however many seconds
        into the hour HA happened to start, so no two installs fire together.
        """
        self._stop_fan_min_runtime_cycles()
        min_runtime = self.config.get(CONF_FAN_MIN_RUNTIME_PER_HOUR, DEFAULT_FAN_MIN_RUNTIME_PER_HOUR)
        if min_runtime <= 0 or self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) == FAN_MODE_DISABLED:
            return
        await self._fan_cycle_on()

    def _stop_fan_min_runtime_cycles(self) -> None:
        """Cancel any pending min-runtime cycle timer and clear active flag."""
        if self._fan_min_cycle_cancel:
            self._fan_min_cycle_cancel()
            self._fan_min_cycle_cancel = None
        self._fan_min_runtime_active = False

    async def _fan_cycle_on(self) -> None:
        """Fan 'on' phase: activate fan, schedule off after min_runtime minutes."""
        min_runtime = self.config.get(CONF_FAN_MIN_RUNTIME_PER_HOUR, DEFAULT_FAN_MIN_RUNTIME_PER_HOUR)
        if min_runtime <= 0 or self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) == FAN_MODE_DISABLED:
            return  # Feature disabled — stop cycling

        if self._fan_override_active:
            return  # User has control; cycle suspended until override cleared

        if not self._fan_active:
            await self._activate_fan(reason="min_runtime_cycle")
            self._fan_min_runtime_active = True

            if min_runtime >= 60:
                return  # Always-on: fan stays on, no further scheduling

            @callback
            def _turn_off(_now: Any) -> None:
                self._fan_min_cycle_cancel = None
                self.hass.async_create_task(self._fan_cycle_off())

            self._fan_min_cycle_cancel = async_call_later(self.hass, min_runtime * 60, _turn_off)
        else:
            # Fan already running for another reason — skip, retry in 60 minutes
            @callback
            def _retry(_now: Any) -> None:
                self._fan_min_cycle_cancel = None
                self.hass.async_create_task(self._fan_cycle_on())

            self._fan_min_cycle_cancel = async_call_later(self.hass, 60 * 60, _retry)

    async def _fan_cycle_off(self) -> None:
        """Fan 'off' phase: deactivate fan, schedule next on after wait period."""
        min_runtime = self.config.get(CONF_FAN_MIN_RUNTIME_PER_HOUR, DEFAULT_FAN_MIN_RUNTIME_PER_HOUR)

        if self._fan_min_runtime_active:
            self._fan_min_runtime_active = False
            await self._deactivate_fan(reason="min_runtime_cycle_complete")

        wait_sec = max(0, (60 - min_runtime) * 60)

        @callback
        def _turn_on(_now: Any) -> None:
            self._fan_min_cycle_cancel = None
            self.hass.async_create_task(self._fan_cycle_on())

        self._fan_min_cycle_cancel = async_call_later(self.hass, wait_sec, _turn_on)

    def handle_manual_override(
        self,
        *,
        source: str = "normal",
        old_mode: str | None = None,
        new_mode: str | None = None,
        classification_mode: str | None = None,
    ) -> None:
        """Handle a manual thermostat change (outside of door/window pause).

        Starts the confirmation period (Issue #76). If the thermostat state
        still differs from classification after the confirmation delay, the
        override is formally accepted and the grace period begins. Transient
        events (thermostat restart, fan cycles) that resolve within the window
        are silently ignored.

        Args:
            source: "normal" for mode-change overrides, "setpoint" for
                    temperature-only changes where mode still matches classification.
            old_mode: Previous hvac_mode (from coordinator for enriched event payload).
            new_mode: New hvac_mode detected.
            classification_mode: What classification expects (for event payload).
        """
        self.start_override_confirmation(
            source=source,
            old_mode=old_mode,
            new_mode=new_mode,
            classification_mode=classification_mode,
        )

    def start_override_confirmation(
        self,
        source: str,
        *,
        old_mode: str | None = None,
        new_mode: str | None = None,
        classification_mode: str | None = None,
    ) -> None:
        """Begin the override confirmation window (Issue #76).

        Args:
            source: "normal" for regular operation overrides,
                    "pause" for overrides detected during a door/window pause.
            old_mode: Previous hvac_mode (for enriched event payload).
            new_mode: New hvac_mode detected.
            classification_mode: What classification expects (for event payload).
        """
        state = self.hass.states.get(self.climate_entity)
        detected_mode = state.state if state else "unknown"
        confirm_seconds = int(self.config.get(CONF_OVERRIDE_CONFIRM_PERIOD, DEFAULT_OVERRIDE_CONFIRM_SECONDS))

        if confirm_seconds <= 0:
            # Confirmation disabled — accept override immediately (legacy behaviour)
            self._confirm_override(detected_mode)
            return

        # Cancel any existing pending confirmation (restart the window)
        if self._override_confirm_cancel:
            self._override_confirm_cancel()
            self._override_confirm_cancel = None

        self._override_confirm_pending = True
        self._override_confirm_time = dt_util.now().isoformat()
        self._override_confirm_mode = detected_mode
        self._override_confirm_source = source
        _LOGGER.info(
            "Potential %s override detected (mode=%s) — confirming in %d minutes",
            source,
            detected_mode,
            confirm_seconds // 60,
        )

        _dedup_window = timedelta(minutes=5)
        _now = dt_util.now()
        if self._last_override_detected_time is None or (_now - self._last_override_detected_time) >= _dedup_window:
            self._last_override_detected_time = _now
            if self._emit_event_callback:
                self._emit_event_callback(
                    "override_detected",
                    {
                        "detected_mode": detected_mode,
                        "source": source,
                        "confirm_delay_seconds": confirm_seconds,
                        "old_mode": old_mode,
                        "new_mode": new_mode,
                        "classification_mode": classification_mode,
                    },
                )
        else:
            _LOGGER.debug(
                "override_detected suppressed — within 5-minute dedup window (last=%s)",
                self._last_override_detected_time.isoformat(),
            )

        @callback
        def _confirm_override_expired(_now: Any) -> None:
            self._override_confirm_cancel = None
            if not self._override_confirm_pending:
                return
            current_state = self.hass.states.get(self.climate_entity)
            current_mode = current_state.state if current_state else "unknown"
            cls_mode = self._current_classification.hvac_mode if self._current_classification else None
            # For setpoint overrides: mode may still match classification, but
            # the user deliberately moved the setpoint — always take PATH A.
            _setpoint_override = source == "setpoint"
            if _setpoint_override or (current_mode not in ("unavailable", "unknown") and current_mode != cls_mode):
                # PATH A: Still divergent (or deliberate setpoint override) — formally confirm
                _LOGGER.warning(
                    "Override confirmed after %d minutes (mode=%s, classification wants %s)",
                    confirm_seconds // 60,
                    current_mode,
                    cls_mode,
                )
                self._override_confirm_pending = False
                self._override_confirm_time = None
                self._override_confirm_mode = None
                self._override_confirm_source = None
                self._confirm_override(current_mode)
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "override_confirmed",
                        {"mode": current_mode, "confirm_delay_seconds": confirm_seconds},
                    )
            else:
                # PATH B: State resolved — transient event, no override
                _LOGGER.info(
                    "Potential override self-resolved (detected=%s, current=%s) — no action taken",
                    self._override_confirm_mode,
                    current_mode,
                )
                self._override_confirm_pending = False
                self._override_confirm_time = None
                self._override_confirm_mode = None
                self._override_confirm_source = None
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "override_self_resolved",
                        {"detected_mode": detected_mode, "current_mode": current_mode},
                    )

        self._override_confirm_cancel = async_call_later(self.hass, confirm_seconds, _confirm_override_expired)

    def _confirm_override(self, mode: str) -> None:
        """Formally accept a manual override and start the grace period."""
        self._manual_override_active = True
        self._manual_override_mode = mode
        self._manual_override_time = dt_util.now().isoformat()
        _LOGGER.warning(
            "Manual override activated: mode=%s",
            self._manual_override_mode,
        )
        self._start_grace_period("manual", trigger="override_confirmed")

    async def apply_classification(
        self,
        classification: DayClassification,
        predicted_indoor: list[dict] | None = None,
    ) -> None:
        """Apply a new day classification — adjust HVAC behavior accordingly.

        This is called once in the morning and can be called again if
        conditions change significantly mid-day.

        Args:
            classification: The day classification to apply.
            predicted_indoor: Optional ODE-predicted indoor temperature curve
                (list of {"ts": ISO str, "temp": float} entries). When provided
                and the model is calibrated, the ceiling guard evaluates whether
                to pre-cool before comfort_cool is breached.
        """
        self._current_classification = classification

        if self._manual_override_active:
            _LOGGER.info(
                "Manual override active (mode=%s since %s) — skipping HVAC mode change",
                self._manual_override_mode,
                self._manual_override_time,
            )
            return

        if self._override_confirm_pending:
            _LOGGER.info(
                "Override confirmation pending (detected=%s at %s) — skipping HVAC mode change",
                self._override_confirm_mode,
                self._override_confirm_time,
            )
            return

        # Issue #85: respect occupancy mode — don't overwrite setback with comfort
        if self._occupancy_mode == OCCUPANCY_VACATION:
            _LOGGER.info("Vacation mode — skipping classification temp change (deep setback preserved)")
            return
        if self._occupancy_mode == OCCUPANCY_AWAY:
            _LOGGER.info("Away mode — reapplying setback instead of comfort temps")
            await self.handle_occupancy_away()
            return

        _cs = self.hass.states.get(self.climate_entity)
        _LOGGER.debug(
            "apply_classification: wants=%r, thermostat=%r",
            classification.hvac_mode,
            _cs.state if _cs else "unavailable",
        )

        unit = self.config.get("temp_unit", "fahrenheit")
        _LOGGER.warning(
            "Applying classification: %s (trend: %s %s)",
            classification.day_type,
            classification.trend_direction,
            format_temp_delta(classification.trend_magnitude, unit),
        )
        _old_mode_cls = _cs.state if _cs else None
        _cls_key = (classification.day_type, classification.hvac_mode)
        if _cls_key != self._last_classification_applied:
            self._last_classification_applied = _cls_key
            if self._emit_event_callback:
                self._emit_event_callback(
                    "classification_applied",
                    {
                        "day_type": classification.day_type,
                        "hvac_mode": classification.hvac_mode,
                        "trend": classification.trend_direction,
                        "old_hvac_mode": _old_mode_cls,
                    },
                )
        else:
            _LOGGER.debug(
                "classification_applied suppressed — same as last (%s/%s)",
                classification.day_type,
                classification.hvac_mode,
            )

        # Set the base HVAC mode
        cls_reason = (
            f"daily classification — {classification.day_type} day,"
            f" trend {classification.trend_direction} {format_temp_delta(classification.trend_magnitude, unit)}"
        )
        if classification.hvac_mode in ("heat", "cool"):
            await self._set_hvac_mode(classification.hvac_mode, reason=cls_reason)
            await self._set_temperature_for_mode(classification, reason=cls_reason)
        elif classification.hvac_mode == "off":
            indoor_temp = self._get_indoor_temp_f()
            comfort_heat = self.config.get("comfort_heat")
            if indoor_temp is not None and comfort_heat is not None and indoor_temp < comfort_heat:
                # Indoor below comfort floor despite warm forecast — heat to comfort first.
                # apply_classification() is called every 30 min; once indoor reaches comfort_heat
                # the guard stops firing and HVAC goes off naturally.
                _LOGGER.warning(
                    "Warm-day off deferred: indoor %.1f°F below comfort floor %.1f°F — heating to comfort first",
                    indoor_temp,
                    comfort_heat,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "warm_day_comfort_gap",
                        {
                            "day_type": classification.day_type,
                            "indoor_temp": indoor_temp,
                            "comfort_heat": comfort_heat,
                        },
                    )
                await self._set_hvac_mode(
                    "heat",
                    reason=(
                        f"indoor {format_temp(indoor_temp, unit)} below comfort floor"
                        f" {format_temp(comfort_heat, unit)}"
                        f" — heating before {classification.day_type} day shutoff"
                    ),
                )
                await self._set_temperature(
                    comfort_heat,
                    reason=f"comfort floor recovery before {classification.day_type} day HVAC off",
                )
            else:
                # Issue #96 Root Cause C: Replace hard off with mode-aware setback to avoid Ecobee side-effects.
                # No mode change = no fan_mode/temperature side-effects on Ecobee.
                # heat:      setback_heat — heating won't fire (house is above floor)
                # cool:      setback_cool — cooling won't fire unless truly extreme (safety ceiling preserved)
                # heat_cool/auto: both setbacks via target_temp_low/high — both sides suppressed with safety bounds
                # unknown:   fall back to hard off (last resort)
                _cs_state = self.hass.states.get(self.climate_entity)
                _current_mode = _cs_state.state if _cs_state else "unknown"
                _setback_heat = self.config.get("setback_heat")
                _setback_cool = self.config.get("setback_cool")
                _setpoint_changed = False
                _old_setpoint_f: float | None = None

                if _current_mode == "off":
                    # Thermostat already off — heartbeat, no service call needed.
                    pass
                elif _current_mode == "heat":
                    await self._set_temperature(
                        _setback_heat,
                        reason=f"warm-day setback: {classification.day_type} day, heating suppressed at setback_heat",
                    )
                    _setpoint_changed = True
                elif _current_mode == "cool":
                    # Capture old setpoint before adjustment
                    _cs = self.hass.states.get(self.climate_entity)
                    _old_setpoint_raw = _cs.attributes.get("temperature") if _cs else None
                    unit = self.config.get("temp_unit", "fahrenheit")
                    _old_setpoint_f = to_fahrenheit(_old_setpoint_raw, unit) if _old_setpoint_raw is not None else None
                    # Guard: skip service call if setpoint is already at setback_cool
                    if _old_setpoint_f is not None and abs(_old_setpoint_f - _setback_cool) <= 0.5:
                        pass  # Already at setback — heartbeat only
                    else:
                        await self._set_temperature(
                            _setback_cool,
                            reason=(
                                f"warm-day setback: {classification.day_type} day, cooling suppressed at setback_cool"
                            ),
                        )
                        _setpoint_changed = True
                elif _current_mode in ("heat_cool", "auto"):
                    await self._set_temperature_dual(
                        _setback_heat,
                        _setback_cool,
                        reason=f"warm-day setback: {classification.day_type} day, dual setpoints prevent HVAC running",
                    )
                    _setpoint_changed = True
                else:
                    _LOGGER.warning(
                        "warm-day setback: thermostat in unknown mode %r -- falling back to hard off",
                        _current_mode,
                    )
                    await self._set_hvac_mode(
                        "off",
                        reason=(
                            f"daily classification -- {classification.day_type} day,"
                            f" HVAC not needed (unknown mode={_current_mode})"
                        ),
                    )
                    _setpoint_changed = True

                if self._emit_event_callback:
                    if _setpoint_changed:
                        self._emit_event_callback(
                            "warm_day_setback_applied",
                            {
                                "day_type": classification.day_type,
                                "thermostat_mode": _current_mode,
                                **(
                                    {
                                        "old_setpoint_f": _old_setpoint_f,
                                        "new_setpoint_f": _setback_cool,
                                    }
                                    if _current_mode == "cool"
                                    else {}
                                ),
                            },
                        )
                    else:
                        self._emit_event_callback(
                            "warm_day_state_confirmed",
                            {
                                "day_type": classification.day_type,
                                "thermostat_mode": _current_mode,
                            },
                        )

        # ODE ceiling guard (Issue #136): if thermal model predicts indoor will breach
        # comfort_cool within lead_time AND outdoor is already warmer than indoor
        # (nat-vent unavailable), set HVAC to cool proactively.
        # Re-evaluated on every 30-min cycle — no flag needed; adapts to forecast changes.
        if predicted_indoor and classification.hvac_mode == "off":
            _thermal = self._thermal_model or {}
            _k_passive = _thermal.get("k_passive")
            _conf = _thermal.get("confidence_k_passive") or _thermal.get("confidence", "none")
            _k_via_bridge = bool(_thermal.get("k_passive_via_bridge"))
            _k_active_cool = _thermal.get("k_active_cool")
            _comfort_cool_cg = self.config.get("comfort_cool")
            _tolerance = CEILING_BRIDGE_TOLERANCE_F if _k_via_bridge else 0.0

            _model_eligible = (
                _k_passive is not None
                and _k_passive < 0
                and (_conf != "none" or _k_via_bridge)
                and _comfort_cool_cg is not None
            )

            _outdoor = self._last_outdoor_temp
            _indoor_cg = self._get_indoor_temp_f()

            _LOGGER.debug(
                "ODE ceiling guard eval: %d points, comfort_cool=%s, outdoor=%s, indoor=%s,"
                " k_passive=%s, conf=%s, bridge=%s",
                len(predicted_indoor),
                _comfort_cool_cg,
                _outdoor,
                _indoor_cg,
                _k_passive,
                _conf,
                _k_via_bridge,
            )

            # Issue #247: dormancy is THREE conditions (the change #218 specified but its commit
            # omitted — only the escalation-on-fire half landed). Defer to free cooling ONLY when
            # outdoor is cooler AND nat-vent is actually running AND indoor is still within band.
            # If indoor has breached the ceiling (free cooling losing to solar/internal gains), or
            # nat-vent is not actually running (sensors closed / fan override — #215), the guard must
            # evaluate and escalate to AC even though outdoor <= indoor. aggressive_savings widens the
            # in-band threshold so savings homes tolerate a small overshoot before paying for cooling.
            _aggressive = bool(self.config.get("aggressive_savings", False))
            _ceiling_threshold = (
                _comfort_cool_cg + CEILING_ESCALATION_SAVINGS_MARGIN_F
                if (_aggressive and _comfort_cool_cg is not None)
                else _comfort_cool_cg
            )
            if not _model_eligible:
                _LOGGER.debug("ODE ceiling guard: skipped — k_passive=%s, conf=%s", _k_passive, _conf)
            elif _outdoor is None or _indoor_cg is None:
                _LOGGER.debug("ODE ceiling guard: skipped — missing outdoor/indoor temps")
            elif _outdoor <= _indoor_cg and self._natural_vent_active and _indoor_cg <= _ceiling_threshold:
                _LOGGER.debug(
                    "ODE ceiling guard: dormant — outdoor %.1f <= indoor %.1f, nat-vent running,"
                    " indoor <= ceiling threshold %.1f (free cooling viable)",
                    _outdoor,
                    _indoor_cg,
                    _ceiling_threshold,
                )
            else:
                # Dormancy lifted (outdoor rose above indoor, OR nat-vent is not running, OR indoor
                # breached the ceiling under active nat-vent — Issue #247). Scan the predicted curve
                # for a ceiling breach.
                # Inline equivalent of _find_ceiling_breach_time() — avoids circular import.
                _breach_ts: datetime | None = None
                _threshold = _comfort_cool_cg + _tolerance
                for _entry in predicted_indoor:
                    _t = _entry.get("temp")
                    if _t is not None and _t > _threshold:
                        with contextlib.suppress(KeyError, ValueError, TypeError):
                            _breach_ts = datetime.fromisoformat(_entry["ts"])
                        break

                if _breach_ts is None:
                    _LOGGER.debug(
                        "ODE ceiling guard: dormant — no breach above %.1f°F predicted",
                        _threshold,
                    )
                else:
                    _now_cg = dt_util.now()
                    # Normalize both to UTC for reliable subtraction
                    _now_utc = (
                        _now_cg.astimezone(UTC)
                        if getattr(_now_cg, "tzinfo", None) is not None
                        else _now_cg.replace(tzinfo=UTC)
                    )
                    _breach_utc = (
                        _breach_ts.astimezone(UTC) if _breach_ts.tzinfo is not None else _breach_ts.replace(tzinfo=UTC)
                    )
                    _hours_to_breach = (_breach_utc - _now_utc).total_seconds() / 3600

                    if _k_active_cool is not None and abs(_k_active_cool) > 0:
                        _lead_min = ((_comfort_cool_cg - _indoor_cg) / abs(_k_active_cool)) * 60 * 1.3
                    else:
                        _lead_min = float(CEILING_PRECOOL_FALLBACK_MIN)
                    _lead_min = max(30.0, min(240.0, _lead_min))

                    _LOGGER.info(
                        "ODE ceiling guard: breach predicted at %s (%.1fh away), outdoor=%.1f, indoor=%.1f,"
                        " nat_vent=%s",
                        _breach_ts.strftime("%H:%M"),
                        _hours_to_breach,
                        _outdoor,
                        _indoor_cg,
                        self._natural_vent_active,
                    )

                    if _hours_to_breach <= _lead_min / 60:
                        _LOGGER.info(
                            "ODE ceiling guard: active — setting HVAC cool, target=%.1f"
                            " (breach %.1fh, lead=%.0fmin, k_cool=%s)",
                            _comfort_cool_cg,
                            _hours_to_breach,
                            _lead_min,
                            _k_active_cool,
                        )
                        if self._natural_vent_active:
                            await self._deactivate_fan(reason="ceiling guard override -- nat-vent to active cooling")
                            self._natural_vent_active = False
                            if self._emit_event_callback:
                                self._emit_event_callback(
                                    "nat_vent_ceiling_escalation",
                                    {
                                        "indoor": _indoor_cg,
                                        "outdoor": _outdoor,
                                        "comfort_cool": _comfort_cool_cg,
                                    },
                                )
                        _cs_cg = self.hass.states.get(self.climate_entity)
                        _old_mode_cg = _cs_cg.state if _cs_cg else None
                        _old_setpoint_raw_cg = _cs_cg.attributes.get("temperature") if _cs_cg else None
                        _old_setpoint_f_cg = (
                            to_fahrenheit(_old_setpoint_raw_cg, unit) if _old_setpoint_raw_cg is not None else None
                        )
                        await self._set_hvac_mode(
                            "cool",
                            reason=(f"ODE ceiling guard — breach predicted at {_breach_ts.strftime('%H:%M')}"),
                        )
                        await self._set_temperature(
                            _comfort_cool_cg,
                            reason="ODE ceiling guard — target comfort_cool",
                        )
                        if self._emit_event_callback:
                            self._emit_event_callback(
                                "ceiling_guard_fired",
                                {
                                    "breach_time": _breach_ts.isoformat(),
                                    "hours_to_breach": round(_hours_to_breach, 1),
                                    "lead_time_min": round(_lead_min),
                                    "old_hvac_mode": _old_mode_cg,
                                    "new_hvac_mode": "cool",
                                    "new_setpoint_f": _comfort_cool_cg,
                                    "old_setpoint_f": _old_setpoint_f_cg,
                                },
                            )
                    else:
                        _LOGGER.debug(
                            "ODE ceiling guard: standing by — breach %.1fh away, need %.0fmin lead time",
                            _hours_to_breach,
                            _lead_min,
                        )

        # Handle pre-conditioning
        if classification.pre_condition and classification.pre_condition_target:
            await self._schedule_pre_condition(classification)

        # Issue #96 Root Cause E: apply_classification() runs on every coordinator refresh
        # (30-min scheduled AND 5-min revisits). Cancel any revisit _record_action() scheduled —
        # the 30-min cycle provides sufficient re-evaluation frequency.
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None
        _LOGGER.debug("apply_classification: revisit canceled — 30-min cycle handles re-evaluation")

    async def _set_hvac_mode(self, mode: str, *, reason: str) -> None:
        """Set the thermostat HVAC mode."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would set HVAC mode to %s — %s", mode, reason)
            return
        self._hvac_command_pending = True
        self._hvac_command_time = dt_util.now()
        self._last_commanded_hvac_mode = mode
        self._last_commanded_hvac_time = dt_util.now()
        _cs_reaffirm = self.hass.states.get(self.climate_entity)
        if _cs_reaffirm and _cs_reaffirm.state == mode:
            _LOGGER.debug("_set_hvac_mode: thermostat already %r — re-affirming", mode)
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.climate_entity, "hvac_mode": mode},
            )
            _LOGGER.warning("Set HVAC mode to %s — %s", mode, reason)
            self._record_action(f"Set HVAC to {mode}", reason)
            # When taking HVAC offline, assert fan_mode=auto to clear any post-heat
            # blowdown state. Skip if nat-vent is active — clobbering fan_mode=on
            # while nat-vent is running silently stops cooling (Issue #134).
            if mode == "off" and not self._natural_vent_active:
                _fan_cfg = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
                if _fan_cfg in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                    try:
                        await self.hass.services.async_call(
                            "climate",
                            "set_fan_mode",
                            {"entity_id": self.climate_entity, "fan_mode": "auto"},
                        )
                        _LOGGER.debug("Asserted fan_mode=auto alongside hvac_mode=off")
                    except Exception:
                        _LOGGER.debug("Could not assert fan_mode=auto — non-critical", exc_info=True)
        finally:
            self._hvac_command_pending = False

    async def _set_temperature(self, temperature: float, *, reason: str) -> None:
        """Set the thermostat target temperature.

        Args:
            temperature: Target temperature in internal Fahrenheit.
            reason: Human-readable reason for logging.
        """
        unit = self.config.get("temp_unit", "fahrenheit")
        # Convert internal °F to user's unit before sending to HA climate entity
        service_temp = from_fahrenheit(temperature, unit)
        if self.dry_run:
            _LOGGER.info(
                "[DRY RUN] Would set temperature to %s — %s",
                format_temp(temperature, unit),
                reason,
            )
            return
        # Check setpoint is appropriate for current HVAC mode
        _current_mode_state = self.hass.states.get(self.climate_entity)
        _current_mode = _current_mode_state.state if _current_mode_state else None
        if _current_mode == "cool" and temperature < (self.config.get("comfort_heat", 70) - 1.0):
            _LOGGER.error(
                "SETPOINT INCONSISTENCY: cool mode but target %.1fF is below comfort_heat threshold",
                temperature,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "incident_detected",
                    {
                        "incident_class": "setpoint_mode_inconsistency",
                        "incident_id": dt_util.now().isoformat(),
                        "hvac_mode": _current_mode,
                        "setpoint_f": temperature,
                        "comfort_heat": self.config.get("comfort_heat", 70),
                        "comfort_cool": self.config.get("comfort_cool", 76),
                    },
                )
        elif _current_mode == "heat" and temperature > (self.config.get("comfort_cool", 76) + 1.0):
            _LOGGER.error(
                "SETPOINT INCONSISTENCY: heat mode but target %.1fF is above comfort_cool threshold",
                temperature,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "incident_detected",
                    {
                        "incident_class": "setpoint_mode_inconsistency",
                        "incident_id": dt_util.now().isoformat(),
                        "hvac_mode": _current_mode,
                        "setpoint_f": temperature,
                        "comfort_heat": self.config.get("comfort_heat", 70),
                        "comfort_cool": self.config.get("comfort_cool", 76),
                    },
                )
        self._temp_command_pending = True
        self._temp_command_time = dt_util.now()
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self.climate_entity, "temperature": service_temp},
            )
        finally:
            self._temp_command_pending = False
        _LOGGER.warning(
            "Set temperature to %s — %s",
            format_temp(temperature, unit),
            reason,
        )
        self._record_action(f"Set temp to {format_temp(temperature, unit)}", reason)

    async def _set_temperature_dual(self, low: float, high: float, *, reason: str) -> None:
        """Set target_temp_low and target_temp_high for heat_cool/auto thermostat modes.

        Uses the same flag/logging/record_action pattern as _set_temperature().
        low/high are internal Fahrenheit values; converted to user unit before service call.
        """
        unit = self.config.get("temp_unit", "fahrenheit")
        service_low = from_fahrenheit(low, unit)
        service_high = from_fahrenheit(high, unit)
        if self.dry_run:
            _LOGGER.info(
                "[DRY RUN] Would set dual temperature low=%s high=%s — %s",
                format_temp(low, unit),
                format_temp(high, unit),
                reason,
            )
            return
        self._temp_command_pending = True
        self._temp_command_time = dt_util.now()
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.climate_entity,
                    "target_temp_low": service_low,
                    "target_temp_high": service_high,
                },
            )
        finally:
            self._temp_command_pending = False
        _LOGGER.warning(
            "Set dual temperature [%s / %s] — %s",
            format_temp(low, unit),
            format_temp(high, unit),
            reason,
        )
        self._record_action(f"Set dual temp [{format_temp(low, unit)}/{format_temp(high, unit)}]", reason)

    async def _set_temperature_for_mode(self, c: DayClassification, *, reason: str) -> None:
        """Set temperature based on the classification and current period.

        Safety net: redirects to setback handlers when occupancy is away/vacation
        so that any code path calling this function respects occupancy mode (Issue #85).
        """
        # Issue #85: redirect to setback when not home/guest
        if self._occupancy_mode == OCCUPANCY_AWAY:
            _LOGGER.info("Away mode — redirecting to setback instead of comfort (%s)", reason)
            await self.handle_occupancy_away()
            return
        if self._occupancy_mode == OCCUPANCY_VACATION:
            _LOGGER.info("Vacation mode — redirecting to deep setback instead of comfort (%s)", reason)
            await self.handle_occupancy_vacation()
            return

        unit = self.config.get("temp_unit", "fahrenheit")
        if c.hvac_mode == "heat":
            target = self.config["comfort_heat"]
        elif c.hvac_mode == "cool":
            target = self.config["comfort_cool"]
            if c.pre_condition and c.pre_condition_target and c.pre_condition_target < 0:
                # Pre-cool: target is below comfort
                target = target + c.pre_condition_target
                reason = f"{reason} (pre-cool offset {format_temp_delta(abs(c.pre_condition_target), unit)})"
        else:
            return

        await self._set_temperature(target, reason=reason)

    async def _schedule_pre_condition(self, c: DayClassification) -> None:
        """Schedule pre-heating or pre-cooling based on trend.

        For warming trends: more aggressive setback (handled by setback_modifier)
        For cooling trends: pre-heat in the evening
        """
        unit = self.config.get("temp_unit", "fahrenheit")
        if c.trend_direction == "cooling" and c.pre_condition_target and c.pre_condition_target > 0:
            # Pre-heat: schedule a bump relative to sleep_time using adaptive timing
            from .const import (
                CONF_DEFAULT_PREHEAT_MINUTES,
                CONF_MAX_PREHEAT_MINUTES,
                CONF_MIN_PREHEAT_MINUTES,
                CONF_PREHEAT_SAFETY_MARGIN,
                DEFAULT_PREHEAT_MINUTES,
                MAX_PREHEAT_MINUTES,
                MIN_PREHEAT_MINUTES,
                PREHEAT_SAFETY_MARGIN,
            )

            preheat_target = self.config["comfort_heat"] + c.pre_condition_target

            # Compute adaptive pre-heat start time
            thermal_model = self._thermal_model or {}
            if not self.config.get("learning_enabled", True) or not self.config.get(CONF_ADAPTIVE_PREHEAT, True):
                _LOGGER.debug(
                    "Adaptive pre-heat disabled — using default %d min",
                    self.config.get(CONF_DEFAULT_PREHEAT_MINUTES, DEFAULT_PREHEAT_MINUTES),
                )
                thermal_model = {}

            confidence = thermal_model.get("confidence", "none")
            heating_rate = thermal_model.get("heating_rate_f_per_hour")

            # pre_condition_target is the degrees to raise (positive for heating)
            temp_rise = getattr(c, "pre_condition_target", 2.0) or 2.0

            min_min = self.config.get(CONF_MIN_PREHEAT_MINUTES, MIN_PREHEAT_MINUTES)
            max_min = self.config.get(CONF_MAX_PREHEAT_MINUTES, MAX_PREHEAT_MINUTES)
            default_min = self.config.get(CONF_DEFAULT_PREHEAT_MINUTES, DEFAULT_PREHEAT_MINUTES)
            safety = self.config.get(CONF_PREHEAT_SAFETY_MARGIN, PREHEAT_SAFETY_MARGIN)
            _LOGGER.debug(
                "Pre-heat thresholds: min=%d max=%d default=%d safety=%.2f (from config)",
                min_min,
                max_min,
                default_min,
                safety,
            )
            _adaptive_preheat_active = False
            if confidence == "none" or heating_rate is None or heating_rate <= 0:
                minutes_needed = max(min_min, min(max_min, default_min))
            else:
                minutes_needed = (temp_rise / heating_rate) * 60.0 * safety
                minutes_needed = max(min_min, min(max_min, minutes_needed))
                _adaptive_preheat_active = True

            # Compute preheat start time relative to sleep_time
            sleep_str = self.config.get("sleep_time", "22:30")
            sleep_parts = sleep_str.split(":")
            sleep_total_minutes = int(sleep_parts[0]) * 60 + int(sleep_parts[1])
            preheat_total_minutes = sleep_total_minutes - int(minutes_needed)
            if preheat_total_minutes < 0:
                preheat_total_minutes += 24 * 60
            preheat_hour = preheat_total_minutes // 60
            preheat_minute = preheat_total_minutes % 60
            preheat_time_str = f"{preheat_hour:02d}:{preheat_minute:02d}"

            if _adaptive_preheat_active:
                _LOGGER.debug(
                    "Adaptive pre-heat: rate=%.2f°F/hr delta=%.1f°F → %d min (safety ×%.1f), start=%s",
                    heating_rate,
                    temp_rise,
                    int(minutes_needed),
                    safety,
                    preheat_time_str,
                )

            _LOGGER.info(
                "Scheduling pre-heat to %s at %s (cold front coming)",
                format_temp(preheat_target, unit),
                preheat_time_str,
            )
            # In a full implementation, this would register a time-based listener
            # For now, store the intent for the coordinator to act on
            self.config["_pending_preheat"] = {
                "time": preheat_time_str,
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
            outdoor = self._last_outdoor_temp
            comfort_cool = float(self.config.get("comfort_cool", 75))
            nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
            nat_vent_threshold = comfort_cool + nat_vent_delta
            if outdoor is not None and outdoor < nat_vent_threshold:
                pass  # outdoor cool enough — fall through to nat-vent check below
            else:
                _LOGGER.info(
                    "Door/window open (%s) but %s grace period active — not pausing",
                    entity_id,
                    self._last_resume_source,
                )
                return

        if self._is_within_planned_window_period():
            _LOGGER.info(
                "Door/window open (%s) during planned window period — not pausing "
                "(windows recommended, HVAC off, day_type=%s)",
                entity_id,
                self._current_classification.day_type if self._current_classification else "unknown",
            )
            return

        # Check for natural ventilation opportunity before falling through to pause
        outdoor = self._last_outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
        nat_vent_threshold = comfort_cool + nat_vent_delta
        indoor = self._get_indoor_temp_f()
        comfort_heat = float(self.config.get("comfort_heat", 70))
        if (
            outdoor is not None
            and indoor is not None
            and outdoor < indoor  # outdoor must be cooler than indoor
            and indoor > comfort_heat  # indoor must be above comfort floor
            and outdoor < nat_vent_threshold
        ):
            _skip_nat_vent = False

            # Phase 2 Guard 1: rising outdoor forecast
            hourly = self._hourly_forecast_temps or []
            if hourly:
                now_dt = dt_util.now()
                # Ensure timezone-aware for comparison with forecast datetimes
                if now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=UTC)
                lookahead_temps = [
                    h["temperature"]
                    for h in hourly
                    if h.get("temperature") is not None
                    and (parsed := _parse_forecast_dt(h.get("datetime"))) is not None
                    and now_dt < parsed <= now_dt + timedelta(hours=2)
                ]
                if lookahead_temps and max(lookahead_temps) > nat_vent_threshold:
                    _skip_nat_vent = True
                    _LOGGER.info(
                        "Nat vent skipped: forecast peak %.1f°F > threshold %.1f°F within 2 hr",
                        max(lookahead_temps),
                        nat_vent_threshold,
                    )
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_forecast_skip",
                            {"forecast_peak": max(lookahead_temps), "threshold": nat_vent_threshold},
                        )

            # Phase 2 Guard 2: thermal model floor imminence
            if not _skip_nat_vent:
                thermal = self._thermal_model or {}
                confidence = thermal.get("confidence", "none")
                if confidence in ("medium", "high"):
                    k_passive = thermal.get("k_passive")
                    if k_passive is not None and k_passive < 0:
                        passive_rate = k_passive * (indoor - outdoor)  # °F/hr, negative
                        if passive_rate < 0:
                            time_to_floor = (indoor - comfort_heat) / abs(passive_rate)
                            if time_to_floor < MIN_VIABLE_NAT_VENT_HOURS:
                                _skip_nat_vent = True
                                _LOGGER.info(
                                    "Nat vent skipped: floor predicted in %.2f hr < %.1f hr threshold (k_passive=%.3f)",
                                    time_to_floor,
                                    MIN_VIABLE_NAT_VENT_HOURS,
                                    k_passive,
                                )
                                if self._emit_event_callback:
                                    self._emit_event_callback(
                                        "nat_vent_floor_imminent_skip",
                                        {"time_to_floor_hr": round(time_to_floor, 2)},
                                    )

            if not _skip_nat_vent:
                # Capture mode before nat_vent changes
                _old_mode_nv = self.hass.states.get(self.climate_entity)
                _old_mode_nv = _old_mode_nv.state if _old_mode_nv else "unknown"

                nat_vent_reason = (
                    f"natural ventilation: outdoor {outdoor:.1f}F < indoor {indoor:.1f}F,"
                    f" outdoor {outdoor:.1f}F <= {nat_vent_threshold:.1f}F"
                )
                await self._set_hvac_mode("off", reason=nat_vent_reason + ", HVAC off, fan on")
                await self._activate_fan(reason=nat_vent_reason)
                self._natural_vent_active = True
                _LOGGER.info(
                    "Natural ventilation mode: outdoor %.1f°F < indoor %.1f°F,"
                    " outdoor ≤ target %.1f°F — fan on, HVAC off",
                    outdoor,
                    indoor,
                    nat_vent_threshold,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "sensor_opened",
                        {
                            "entity": entity_id,
                            "result": "natural_ventilation",
                            "hvac_mode_change": f"{_old_mode_nv}→off",
                            "fan_mode_change": "auto→on",
                        },
                    )
                return

        # Get current mode before pausing
        state = self.hass.states.get(self.climate_entity)
        if state:
            self._pre_pause_mode = state.state

        if self._pre_pause_mode and self._pre_pause_mode != "off":
            self._paused_by_door = True
            if self._emit_event_callback:
                self._emit_event_callback(
                    "sensor_opened",
                    {
                        "entity": entity_id,
                        "result": "paused",
                        "hvac_mode_change": f"{self._pre_pause_mode}→off",
                    },
                )
            await self._set_hvac_mode(
                "off",
                reason=f"door/window open — {entity_id}, was {self._pre_pause_mode} mode",
            )

            # Notify
            debounce_minutes = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS) // 60
            friendly_name = entity_id.split(".")[-1].replace("_", " ").title()
            await self._notify(
                f"🚪 HVAC paused — {friendly_name} has been open for "
                f"{debounce_minutes} minutes. "
                f"Heating/cooling will resume when it's closed.",
                "Climate Advisor",
                notification_type="door_window_pause",
            )

    async def handle_all_doors_windows_closed(self) -> None:
        """Resume HVAC after all monitored doors/windows are closed."""
        was_nat_vent = self._natural_vent_active
        was_paused = self._paused_by_door
        if self._emit_event_callback:
            self._emit_event_callback(
                "sensor_all_closed",
                {"was_paused": was_paused, "was_nat_vent": was_nat_vent},
            )

        # Handle natural ventilation mode cleanup (sensors closed while in nat vent)
        if self._natural_vent_active:
            self._natural_vent_active = False
            await self._deactivate_fan(reason="door/window closed — ending natural ventilation mode")
            # Resume normal classification if we have one
            if self._current_classification:
                c = self._current_classification
                if c.hvac_mode in ("heat", "cool"):
                    await self._set_hvac_mode(
                        c.hvac_mode,
                        reason="door/window closed — restoring mode after natural ventilation",
                    )
                    await self._set_temperature_for_mode(
                        c,
                        reason="door/window closed — restoring comfort after natural ventilation",
                    )
                    self._start_grace_period("automation", trigger="sensor_closed_resume")
            return

        if not self._paused_by_door:
            return

        self._paused_by_door = False
        if self._pre_pause_mode:
            await self._set_hvac_mode(
                self._pre_pause_mode,
                reason=f"door/window closed — restoring {self._pre_pause_mode} mode",
            )
            if self._current_classification:
                await self._set_temperature_for_mode(
                    self._current_classification,
                    reason="door/window closed — restoring comfort",
                )
            self._start_grace_period("automation", trigger="sensor_closed_resume")
        self._pre_pause_mode = None

    async def check_natural_vent_conditions(self) -> None:
        """Re-evaluate natural ventilation vs pause when temperatures change.

        Called by coordinator on each _async_update_data when sensors are open.
        Mirrors the monitoring logic in tools/simulate.py ClimateSimulator.
        """
        if not (self._paused_by_door or self._natural_vent_active):
            # Comfort-ceiling override (Issue #134): if grace is active and indoor has
            # risen above comfort_cool, allow re-evaluation so nat-vent can engage.
            # Grace still blocks rapid door-open/close cycling below the comfort ceiling.
            _indoor = self._get_indoor_temp_f()
            _cool = float(self.config.get("comfort_cool", 75))
            # Issue #244: a contact sensor open while HVAC is idle (door opened with
            # nothing to pause) must still be re-evaluated so nat-vent can engage when
            # outdoor later cools below indoor — otherwise the occupant misses free
            # evening cooling. Restricted to HVAC-off so we never fight active heating/cooling.
            _hvac_state_244 = self.hass.states.get(self.climate_entity)
            _hvac_off_244 = (_hvac_state_244 is None) or (getattr(_hvac_state_244, "state", "off") == "off")
            _idle_open = bool(self._sensor_check_callback and self._sensor_check_callback()) and _hvac_off_244
            if not ((self._grace_active and _indoor is not None and _indoor > _cool) or _idle_open):
                return

        outdoor = self._last_outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
        threshold = comfort_cool + nat_vent_delta

        # Issue #134: comfort-ceiling re-entry during grace — neither flag is True but
        # indoor has risen above comfort_cool. Check nat-vent conditions directly.
        if not (self._paused_by_door or self._natural_vent_active):
            _indoor = self._get_indoor_temp_f()
            _comfort_heat = float(self.config.get("comfort_heat", 70))
            _hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            if (
                outdoor is not None
                and _indoor is not None
                and outdoor < _indoor - _hysteresis
                and _indoor > _comfort_heat
                and outdoor < threshold
            ):
                # Turn HVAC off first (may be in heat mode from comfort-floor exit) before
                # running the fan. _natural_vent_active is still False here, so Fix 1's guard
                # allows fan_mode=auto — _activate_fan immediately overrides to fan_mode=on.
                await self._set_hvac_mode(
                    "off",
                    reason=f"nat vent grace re-entry: indoor {_indoor:.1f}°F > comfort_cool",
                )
                await self._activate_fan(
                    reason=(
                        f"nat vent grace re-entry: indoor {_indoor:.1f}°F"
                        f" > comfort_cool {comfort_cool:.1f}°F,"
                        f" outdoor {outdoor:.1f}°F cool"
                    )
                )
                self._natural_vent_active = True
                # Issue #244: emit so the re-evaluation activation is visible in the
                # event log / timeline / AI report (previously this path was silent).
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "sensor_opened",
                        {
                            "entity": "natural_vent_reeval",
                            "result": "natural_ventilation",
                            "trigger": "open_door_reeval",
                        },
                    )
            return

        # Issue #99: Comfort-floor exit — check BEFORE outdoor warmth to avoid conflicting
        # transitions. If indoor drops to comfort_heat, stop fan and restore heat.
        # Do NOT enter pause — the house needs to warm up, not wait for nat vent re-evaluation.
        if self._natural_vent_active:
            comfort_heat = float(self.config.get("comfort_heat", 70))
            indoor = self._get_indoor_temp_f()
            if self._natural_vent_active and indoor is not None and comfort_cool is not None and indoor > comfort_cool:
                # Issue #247: the ODE ceiling guard now ESCALATES to AC on the classification cycle
                # when indoor breaches the ceiling under active nat-vent (its three-condition dormancy
                # lifts), so this is an informational heads-up, not a stuck state.
                _LOGGER.info(
                    "Nat-vent active but indoor %.1fF > comfort_cool %.1fF --"
                    " ceiling guard will escalate to AC this classification cycle",
                    indoor,
                    comfort_cool,
                )
            if indoor is not None and indoor <= comfort_heat:
                self._natural_vent_active = False
                await self._deactivate_fan(
                    reason=(
                        f"natural vent exit: indoor {indoor:.1f}\u00b0F \u2264 comfort floor {comfort_heat:.1f}\u00b0F"
                    )
                )
                _LOGGER.info(
                    "Natural vent exit (comfort floor): indoor %.1f\u00b0F"
                    " \u2264 comfort_heat %.1f\u00b0F \u2014 restoring heat",
                    indoor,
                    comfort_heat,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_comfort_floor_exit",
                        {
                            "indoor_temp": indoor,
                            "comfort_heat": comfort_heat,
                            "fan_mode_change": "on→auto",
                            "hvac_mode_restored": (
                                self._current_classification.hvac_mode if self._current_classification else "unknown"
                            ),
                        },
                    )
                if self._current_classification:
                    c = self._current_classification
                    if c.hvac_mode in ("heat", "cool"):
                        await self._set_hvac_mode(
                            c.hvac_mode,
                            reason=f"natural vent comfort-floor exit \u2014 restoring {c.hvac_mode} mode",
                        )
                        await self._set_temperature_for_mode(
                            c,
                            reason="natural vent comfort-floor exit \u2014 restoring comfort",
                        )
                        self._start_grace_period("automation", trigger="nat_vent_exit_resume")
                return

        # Priority 2b: Away mode ceiling exit — nat-vent exits at home comfort ceiling while away
        # (away setback is higher than comfort_cool, but nat-vent only free-cools within home band)
        if self._natural_vent_active and self._occupancy_mode == OCCUPANCY_AWAY:
            _indoor_away = self._get_indoor_temp_f()
            if _indoor_away is not None and comfort_cool is not None and _indoor_away >= comfort_cool:
                _LOGGER.info(
                    "Nat-vent away-mode ceiling exit: indoor %.1fF >= comfort_cool %.1fF while away",
                    _indoor_away,
                    comfort_cool,
                )
                self._natural_vent_active = False
                await self._deactivate_fan(reason="nat-vent ceiling exit (away mode)")
                # Do NOT pause — just let away setback handle HVAC
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_away_ceiling_exit",
                        {"indoor": _indoor_away, "comfort_cool": comfort_cool},
                    )
                return

        # Phase 2: proactive floor exit — predict floor crossing before it happens
        if self._natural_vent_active:
            thermal = self._thermal_model or {}
            if thermal.get("confidence", "none") in ("medium", "high"):
                k_passive = thermal.get("k_passive")
                _indoor_now = self._get_indoor_temp_f()
                if (
                    k_passive is not None
                    and k_passive < 0
                    and _indoor_now is not None
                    and outdoor is not None
                    and outdoor < _indoor_now
                ):
                    passive_rate = k_passive * (_indoor_now - outdoor)  # °F/hr, negative
                    if passive_rate < 0:
                        comfort_heat_now = float(self.config.get("comfort_heat", 70))
                        time_to_floor = (_indoor_now - comfort_heat_now) / abs(passive_rate)
                        if time_to_floor < MIN_VIABLE_NAT_VENT_HOURS:
                            self._natural_vent_active = False
                            await self._deactivate_fan(
                                reason=(f"nat vent proactive floor exit: floor in {time_to_floor:.2f} hr")
                            )
                            _LOGGER.info(
                                "Natural vent proactive exit: floor predicted in %.2f hr"
                                " < %.1f hr threshold — restoring heat",
                                time_to_floor,
                                MIN_VIABLE_NAT_VENT_HOURS,
                            )
                            if self._emit_event_callback:
                                self._emit_event_callback(
                                    "nat_vent_predicted_floor_exit",
                                    {
                                        "time_to_floor_hr": round(time_to_floor, 2),
                                        "fan_mode_change": "on→auto",
                                        "hvac_mode_restored": (
                                            self._current_classification.hvac_mode
                                            if self._current_classification
                                            else "unknown"
                                        ),
                                    },
                                )
                            if self._current_classification:
                                c = self._current_classification
                                if c.hvac_mode in ("heat", "cool"):
                                    await self._set_hvac_mode(
                                        c.hvac_mode,
                                        reason="nat vent proactive floor exit — restoring HVAC",
                                    )
                                    await self._set_temperature_for_mode(
                                        c,
                                        reason="nat vent proactive floor exit — restoring comfort",
                                    )
                                    self._start_grace_period("automation", trigger="nat_vent_exit_resume")
                            return

        # NEW (Issue #115): exit if outdoor ≥ indoor — airflow now heating not cooling
        indoor = self._get_indoor_temp_f()
        if self._natural_vent_active and outdoor is not None and indoor is not None and outdoor >= indoor:
            self._natural_vent_active = False
            self._paused_by_door = True
            self._nat_vent_outdoor_exit_time = dt_util.now()
            await self._deactivate_fan(
                reason=(
                    f"nat vent exit: outdoor {outdoor:.1f}\u00b0F \u2265 indoor {indoor:.1f}\u00b0F"
                    " \u2014 airflow reversed"
                )
            )
            _LOGGER.info(
                "Natural vent exit: outdoor %.1f\u00b0F \u2265 indoor %.1f\u00b0F"
                " \u2014 airflow reversed, entering pause",
                outdoor,
                indoor,
            )
            if self._emit_event_callback:
                self._emit_event_callback("nat_vent_outdoor_rise_exit", {"outdoor": outdoor, "indoor": indoor})
            return

        if self._natural_vent_active and outdoor is not None and outdoor > threshold:
            # Outdoor got too warm — exit nat vent, enter pause
            self._natural_vent_active = False
            self._paused_by_door = True
            await self._deactivate_fan(
                reason=f"natural vent exit: outdoor {outdoor:.1f}\u00b0F > threshold {threshold:.1f}\u00b0F"
            )
            _LOGGER.info(
                "Natural vent exit: outdoor %.1f\u00b0F > threshold %.1f\u00b0F \u2014 entering pause",
                outdoor,
                threshold,
            )
            return

        if self._paused_by_door and outdoor is not None and indoor is not None:
            hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            lockout_s = float(self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S))
            comfort_heat = float(self.config.get("comfort_heat", 70))

            # Enforce lockout after outdoor-warm exit
            if self._nat_vent_outdoor_exit_time is not None:
                elapsed = (dt_util.now() - self._nat_vent_outdoor_exit_time).total_seconds()
                if elapsed < lockout_s:
                    return  # still within lockout window

            if (
                outdoor < indoor - hysteresis  # outdoor must be at least 1°F below indoor
                and indoor > comfort_heat  # indoor above comfort floor
                and outdoor < threshold  # within acceptable ceiling
            ):
                # Outdoor cooled down — activate natural vent
                await self._activate_fan(
                    reason=(
                        f"natural vent activated: outdoor {outdoor:.1f}\u00b0F"
                        f" < indoor {indoor:.1f}\u00b0F \u2212 {hysteresis:.1f}\u00b0F hysteresis,"
                        f" outdoor \u2264 threshold {threshold:.1f}\u00b0F"
                    )
                )
                self._natural_vent_active = True
                self._paused_by_door = False
                _LOGGER.info(
                    "Natural vent activated: outdoor %.1f\u00b0F < indoor %.1f\u00b0F \u2212 %.1f\u00b0F hysteresis,"
                    " outdoor \u2264 threshold %.1f\u00b0F while paused",
                    outdoor,
                    indoor,
                    hysteresis,
                    threshold,
                )

    async def handle_manual_override_during_pause(
        self,
        *,
        old_mode: str | None = None,
        new_mode: str | None = None,
        classification_mode: str | None = None,
    ) -> None:
        """Handle when user manually turns HVAC on during a sensor pause.

        Called by the coordinator when it detects a thermostat mode change
        from 'off' to something else while paused_by_door is True.
        """
        if not self._paused_by_door:
            return
        _LOGGER.info("Manual HVAC override detected during door/window pause")
        self._paused_by_door = False
        self._pre_pause_mode = None
        # Start confirmation period — wait before formally accepting the override
        self.start_override_confirmation(
            source="pause",
            old_mode=old_mode,
            new_mode=new_mode,
            classification_mode=classification_mode,
        )

    async def resume_from_pause(self) -> str | None:
        """Resume HVAC from contact sensor pause (user-initiated via dashboard).

        Clears the pause, restores the current classification's HVAC mode
        (not pre_pause_mode, since classification may have changed), and
        starts a manual override grace period to prevent immediate re-pause.

        Returns the restored mode string, or None if not currently paused.
        """
        if not self._paused_by_door:
            return None

        _LOGGER.info("User resumed HVAC from door/window pause via dashboard")
        self._paused_by_door = False
        self._pre_pause_mode = None
        self._resumed_from_pause = True

        restore_mode = None
        if self._current_classification:
            restore_mode = self._current_classification.hvac_mode
            if restore_mode and restore_mode != "off":
                await self._set_hvac_mode(
                    restore_mode,
                    reason="user resumed from door/window pause",
                )
                await self._set_temperature_for_mode(
                    self._current_classification,
                    reason="user resumed from door/window pause",
                )

        self._start_grace_period("manual", trigger="dashboard_resume")
        return restore_mode

    def _start_grace_period(self, source: str, trigger: str = "") -> None:
        """Start a grace period after HVAC is resumed.

        Args:
            source: "manual" for user-initiated overrides,
                    "automation" for Climate Advisor resumptions.
        """
        self._cancel_grace_timers()

        if source == "manual":
            duration = self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
            should_notify = self.config.get(CONF_MANUAL_GRACE_NOTIFY, False)
        else:
            duration = self.config.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
            should_notify = self.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True)

        if duration <= 0:
            return  # Grace period disabled

        self._grace_active = True
        self._last_resume_source = source
        self._grace_duration_seconds = duration
        self._grace_end_time = (dt_util.now() + timedelta(seconds=duration)).isoformat()

        @callback
        def _grace_expired(_now: Any) -> None:
            """Grace period has elapsed — re-check sensors before clearing."""
            self._on_grace_expired(source, duration, should_notify)

            # Converge to correct scheduled state (bedtime setback or current classification)
            self.hass.async_create_task(self._apply_current_scheduled_state())

        cancel = async_call_later(self.hass, duration, _grace_expired)
        if source == "manual":
            self._manual_grace_cancel = cancel
        else:
            self._automation_grace_cancel = cancel

        _LOGGER.info("Started %s grace period (%d seconds)", source, duration)
        if self._emit_event_callback:
            self._emit_event_callback(
                "grace_started",
                {"source": source, "duration_seconds": duration, "trigger": trigger},
            )

    def _on_grace_expired(self, source: str, duration: int, should_notify: bool) -> None:
        """Handle grace period expiry — re-check sensors then clear state.

        Extracted from the inner callback in ``_start_grace_period`` so it can
        also be invoked from ``_reschedule_grace_timer`` after an HA restart.
        """
        # If within planned window period, sensors open is expected — just clear grace
        if self._is_within_planned_window_period():
            _LOGGER.info(
                "%s grace expired during planned window period — sensors open as expected, clearing grace",
                source,
            )
            self._grace_active = False
            self._last_resume_source = None
            self._grace_end_time = None
            self._manual_grace_cancel = None
            self._automation_grace_cancel = None
            self.clear_manual_override(reason="grace_expired")
            return

        # If any contact sensor is still open, re-pause instead of clearing
        if self._sensor_check_callback and self._sensor_check_callback():
            _LOGGER.info(
                "%s grace expired but sensor(s) still open — re-pausing HVAC",
                source,
            )
            self._grace_active = False
            self._last_resume_source = None
            self._grace_end_time = None
            self._manual_grace_cancel = None
            self._automation_grace_cancel = None
            self.clear_manual_override(reason="grace_expired")
            if self._emit_event_callback:
                self._emit_event_callback("grace_expired", {"source": source, "re_paused": True})
            self.hass.async_create_task(self._re_pause_for_open_sensor())
            return

        self._grace_active = False
        self._last_resume_source = None
        self._grace_end_time = None
        self._manual_grace_cancel = None
        self._automation_grace_cancel = None
        self.clear_manual_override(reason="grace_expired")
        _LOGGER.info("%s grace period expired (%d seconds)", source, duration)
        if self._emit_event_callback:
            self._emit_event_callback("grace_expired", {"source": source, "re_paused": False})

        if should_notify:
            self.hass.async_create_task(
                self._notify(
                    f"⏱️ {source.capitalize()} grace period expired "
                    f"({duration // 60} minutes). HVAC will now respond "
                    f"normally to door/window sensor changes.",
                    "Climate Advisor",
                    notification_type="grace_expired",
                )
            )

    def _reschedule_grace_timer(self, remaining_seconds: float) -> None:
        """Re-create the grace expiry callback after an HA restart.

        Called by the coordinator's ``async_restore_state`` when persisted state
        shows an active grace period that still has time remaining.
        """
        source = self._last_resume_source or "manual"
        duration = int(self._grace_duration_seconds)
        should_notify = False  # Don't notify on re-scheduled expiry after restart

        @callback
        def _grace_expired_restored(_now: Any) -> None:
            self._on_grace_expired(source, duration, should_notify)

        cancel = async_call_later(self.hass, remaining_seconds, _grace_expired_restored)
        if source == "manual":
            self._manual_grace_cancel = cancel
        else:
            self._automation_grace_cancel = cancel
        _LOGGER.info(
            "Grace timer re-created after restart: %s grace, %.0f seconds remaining",
            source,
            remaining_seconds,
        )

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

    async def _re_pause_for_open_sensor(self) -> None:
        """Re-pause HVAC because a sensor is still open when grace expired."""
        if self._is_within_planned_window_period():
            _LOGGER.info(
                "Skipping re-pause — within planned window period (windows recommended)",
            )
            return
        # Check nat-vent conditions before blindly re-pausing
        outdoor = self._last_outdoor_temp
        comfort_cool = float(self.config.get("comfort_cool", 75))
        nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
        indoor = self._get_indoor_temp_f()
        comfort_heat = float(self.config.get("comfort_heat", 70))
        if (
            outdoor is not None
            and indoor is not None
            and outdoor < indoor  # outdoor must be cooler than indoor
            and indoor > comfort_heat  # indoor must be above comfort floor
            and outdoor < comfort_cool + nat_vent_delta
        ):
            nat_vent_threshold = comfort_cool + nat_vent_delta
            nat_vent_reason = (
                f"grace expired — nat-vent: outdoor {outdoor:.1f}°F < indoor {indoor:.1f}°F,"
                f" outdoor {outdoor:.1f}°F ≤ {nat_vent_threshold:.1f}°F"
            )
            await self._set_hvac_mode("off", reason=nat_vent_reason)
            await self._activate_fan(reason=nat_vent_reason)
            self._natural_vent_active = True
            _LOGGER.info(
                "Re-check after grace: nat-vent conditions met — outdoor %.1f°F < indoor %.1f°F, outdoor ≤ %.1f°F",
                outdoor,
                indoor,
                nat_vent_threshold,
            )
            if self._emit_event_callback:
                self._emit_event_callback("sensor_opened", {"entity": "re-check", "result": "natural_ventilation"})
            return
        state = self.hass.states.get(self.climate_entity)
        if state and state.state not in ("off", "unavailable", "unknown"):
            self._pre_pause_mode = state.state
            self._paused_by_door = True
            await self._set_hvac_mode(
                "off",
                reason="grace expired — door/window still open, re-pausing",
            )
            await self._notify(
                "Grace period expired but a door/window is still open. HVAC has been paused again.",
                "Climate Advisor",
                notification_type="grace_repause",
            )
        elif state and state.state == "off":
            # HVAC already off, just set the pause flag
            self._paused_by_door = True

    async def _apply_current_scheduled_state(self, reason: str = "grace_expired") -> None:
        """After override clears, converge to the scheduled automation state.

        Determines what state automation would be in right now if no manual override
        had occurred, and applies it. Ensures automation always converges back to the
        correct state after a grace period expires.
        """
        from homeassistant.util import dt as dt_util  # noqa: PLC0415

        now = dt_util.now()

        # Determine if we're in a bedtime window (after sleep_time OR before wake_time)
        sleep_time = self.config.get("sleep_time")  # e.g. "22:30"
        wake_time = self.config.get("wake_time")  # e.g. "07:00"

        if sleep_time and wake_time:
            try:
                from datetime import time as dt_time  # noqa: PLC0415

                sleep_h, sleep_m = map(int, sleep_time.split(":"))
                wake_h, wake_m = map(int, wake_time.split(":"))
                now_time = now.time().replace(second=0, microsecond=0)
                sleep_t = dt_time(sleep_h, sleep_m)
                wake_t = dt_time(wake_h, wake_m)

                # Bedtime window: after sleep_time OR before wake_time
                in_bedtime_window = now_time >= sleep_t or now_time < wake_t
                if in_bedtime_window:
                    _LOGGER.info(
                        "Grace expired: in bedtime window (%s–%s) — applying bedtime setback",
                        sleep_time,
                        wake_time,
                    )
                    await self.handle_bedtime()
                    return
            except (ValueError, AttributeError):
                pass  # malformed config — fall through to classification

        # Otherwise apply current classification
        if self._current_classification:
            _LOGGER.info("Grace expired: applying current classification")
            await self.apply_classification(self._current_classification)

    async def handle_occupancy_away(self) -> None:
        """Handle everyone leaving — apply setback."""
        self._occupancy_mode = OCCUPANCY_AWAY
        if self._manual_override_active:
            _LOGGER.info(
                "Occupancy transition to away — clearing manual override (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            self.clear_manual_override(reason="occupancy_away")
        c = self._current_classification
        if not c:
            _LOGGER.warning("Occupancy away handler skipped — no day classification available")
            return

        # Use actual thermostat mode as ground truth — classification mode may differ
        # if the day started in one mode but the nightly reclassification switched it
        # (e.g., started hot → cool mode, reclassified to heat at night; without this
        # guard, we would apply setback_heat while the AC is still in cool mode)
        actual_state = self.hass.states.get(self.climate_entity)
        actual_mode = actual_state.state if actual_state else c.hvac_mode

        unit = self.config.get("temp_unit", "fahrenheit")
        if actual_mode == "cool":
            setback = self.config["setback_cool"] - c.setback_modifier
            await self._set_temperature(
                setback,
                reason=(
                    f"occupancy away — cool setback"
                    f" (base {format_temp(self.config['setback_cool'], unit)}"
                    f" - modifier {format_temp_delta(c.setback_modifier, unit)})"
                ),
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback",
                    {"mode": "cool", "target_f": setback, "occupancy": "away"},
                )
        elif actual_mode == "heat":
            setback = self.config["setback_heat"] + c.setback_modifier
            await self._set_temperature(
                setback,
                reason=(
                    f"occupancy away — heat setback"
                    f" (base {format_temp(self.config['setback_heat'], unit)}"
                    f" + modifier {format_temp_delta(c.setback_modifier, unit)})"
                ),
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback",
                    {"mode": "heat", "target_f": setback, "occupancy": "away"},
                )
        else:
            _LOGGER.info(
                "Occupancy away — HVAC mode is %r, no setback needed",
                actual_mode,
            )

    async def handle_occupancy_home(self) -> None:
        """Handle someone returning — restore comfort."""
        self._occupancy_mode = OCCUPANCY_HOME
        c = self._current_classification
        if not c:
            return

        if c.hvac_mode in ("heat", "cool"):
            await self._set_temperature_for_mode(c, reason=f"occupancy home — restoring {c.hvac_mode} comfort")
            comfort = self.config["comfort_heat"] if c.hvac_mode == "heat" else self.config["comfort_cool"]
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_comfort_restored",
                    {"mode": c.hvac_mode, "target_f": comfort},
                )

        # Check 1: Temperature proximity — skip notification if house already near comfort.
        indoor_temp = self._get_indoor_temp_f()
        if indoor_temp is not None and c.hvac_mode in ("heat", "cool"):
            comfort = self.config["comfort_heat"] if c.hvac_mode == "heat" else self.config["comfort_cool"]
            setback = self.config["setback_heat"] if c.hvac_mode == "heat" else self.config["setback_cool"]
            if abs(indoor_temp - comfort) < abs(indoor_temp - setback):
                _LOGGER.info(
                    "Welcome home notification suppressed — indoor %.1f\u00b0F already near comfort %.1f\u00b0F"
                    " (dist_comfort=%.1f < dist_setback=%.1f)",
                    indoor_temp,
                    comfort,
                    abs(indoor_temp - comfort),
                    abs(indoor_temp - setback),
                )
                self._last_welcome_home_notified = dt_util.now()
                return

        # Check 2: Debounce — skip notification if one was sent recently.
        debounce_seconds = self.config.get(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS)
        if debounce_seconds > 0 and self._last_welcome_home_notified is not None:
            elapsed = (dt_util.now() - self._last_welcome_home_notified).total_seconds()
            if elapsed < debounce_seconds:
                _LOGGER.info(
                    "Welcome home notification suppressed — debounce active (%.0fs elapsed, window=%ds)",
                    elapsed,
                    debounce_seconds,
                )
                return

        self._last_welcome_home_notified = dt_util.now()
        await self._notify(
            "🏠 Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes.",
            "Climate Advisor",
            notification_type="occupancy_home",
        )

    async def handle_occupancy_vacation(self) -> None:
        """Handle vacation mode — apply deeper setback for extended away."""
        self._occupancy_mode = OCCUPANCY_VACATION
        if self._manual_override_active:
            _LOGGER.info(
                "Occupancy transition to vacation — clearing manual override (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            self.clear_manual_override(reason="occupancy_vacation")
        c = self._current_classification
        if not c:
            return

        # Use actual thermostat mode as ground truth — same guard as handle_occupancy_away()
        actual_state = self.hass.states.get(self.climate_entity)
        actual_mode = actual_state.state if actual_state else c.hvac_mode

        unit = self.config.get("temp_unit", "fahrenheit")
        if actual_mode == "cool":
            setback = self.config["setback_cool"] - c.setback_modifier + VACATION_SETBACK_EXTRA
            await self._set_temperature(
                setback,
                reason=(
                    f"vacation mode — deep cool setback"
                    f" (base {format_temp(self.config['setback_cool'], unit)}"
                    f" - modifier {format_temp_delta(c.setback_modifier, unit)}"
                    f" + vacation {format_temp_delta(VACATION_SETBACK_EXTRA, unit)})"
                ),
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback",
                    {"mode": "cool", "target_f": setback, "occupancy": "vacation"},
                )
        elif actual_mode == "heat":
            setback = self.config["setback_heat"] + c.setback_modifier - VACATION_SETBACK_EXTRA
            await self._set_temperature(
                setback,
                reason=(
                    f"vacation mode — deep heat setback"
                    f" (base {format_temp(self.config['setback_heat'], unit)}"
                    f" + modifier {format_temp_delta(c.setback_modifier, unit)}"
                    f" - vacation {format_temp_delta(VACATION_SETBACK_EXTRA, unit)})"
                ),
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback",
                    {"mode": "heat", "target_f": setback, "occupancy": "vacation"},
                )
        else:
            _LOGGER.info(
                "Occupancy vacation — HVAC mode is %r, no setback needed",
                actual_mode,
            )

    async def handle_bedtime(self) -> None:
        """Apply bedtime setback."""
        # Issue #85: vacation/away already has a setback — don't override it with sleep temps
        if self._occupancy_mode in (OCCUPANCY_VACATION, OCCUPANCY_AWAY):
            _LOGGER.info("Bedtime skipped — %s mode (setback already active)", self._occupancy_mode)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "bedtime_setback_skipped",
                    {"reason": "occupancy", "occupancy": self._occupancy_mode},
                )
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "occupancy"
            return

        if self._manual_override_active:
            _LOGGER.info(
                "Bedtime setback skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            if self._emit_event_callback:
                self._emit_event_callback("bedtime_setback_skipped", {"reason": "manual_override"})
            return

        _LOGGER.warning("Bedtime setback: clearing any pending override state before applying sleep setback")
        self.clear_manual_override(reason="bedtime")

        # Deactivate fan at bedtime (fan running overnight is noisy/wasteful)
        if self._fan_active and not self._fan_override_active:
            await self._deactivate_fan(reason="bedtime — fan off for night")
        if self._economizer_active:
            await self._deactivate_economizer(outdoor_temp=0)

        c = self._current_classification
        if not c:
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "no_classification"
            return

        if c.hvac_mode not in ("heat", "cool"):
            _LOGGER.info("Bedtime — HVAC mode is %s, no setback needed", c.hvac_mode)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "bedtime_setback_skipped",
                    {"reason": "hvac_off", "occupancy": self._occupancy_mode},
                )
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "hvac_off"
            return

        unit = self.config.get("temp_unit", "fahrenheit")
        if c.hvac_mode == "heat":
            bedtime_target = compute_bedtime_setback(self.config, self._thermal_model, c)
            depth_f = abs(self.config["comfort_heat"] - bedtime_target)
            was_adaptive = (
                self._thermal_model is not None
                and self._thermal_model.get("confidence", "none") != "none"
                and (self._thermal_model.get("k_active_heat") or 0) > 0
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "bedtime_setback",
                    {
                        "mode": "heat",
                        "target_f": bedtime_target,
                        "depth_f": depth_f,
                        "adaptive": was_adaptive,
                        "modifier": c.setback_modifier,
                    },
                )
            if self._today_record is not None:
                self._today_record.setback_heat_applied_f = bedtime_target
                self._today_record.setback_depth_f = depth_f
                self._today_record.setback_was_adaptive = was_adaptive
            await self._set_temperature(
                bedtime_target,
                reason=(
                    f"bedtime — heat setback"
                    f" (comfort {format_temp(self.config['comfort_heat'], unit)}"
                    f" + modifier {format_temp_delta(c.setback_modifier, unit)})"
                ),
            )
        elif c.hvac_mode == "cool":
            bedtime_target = compute_bedtime_setback(self.config, self._thermal_model, c)
            depth_f = abs(self.config["comfort_cool"] - bedtime_target)
            was_adaptive = (
                self._thermal_model is not None
                and self._thermal_model.get("confidence", "none") != "none"
                and (self._thermal_model.get("k_active_cool") or 0) > 0
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "bedtime_setback",
                    {
                        "mode": "cool",
                        "target_f": bedtime_target,
                        "depth_f": depth_f,
                        "adaptive": was_adaptive,
                        "modifier": c.setback_modifier,
                    },
                )
            if self._today_record is not None:
                self._today_record.setback_cool_applied_f = bedtime_target
                self._today_record.setback_depth_f = depth_f
                self._today_record.setback_was_adaptive = was_adaptive
            await self._set_temperature(
                bedtime_target,
                reason=(
                    f"bedtime — cool setback"
                    f" (comfort {format_temp(self.config['comfort_cool'], unit)}"
                    f" + modifier {format_temp_delta(c.setback_modifier, unit)})"
                ),
            )

    async def handle_morning_wakeup(self) -> None:
        """Restore comfort for morning wake-up."""
        # Issue #85: skip comfort restore when nobody is home
        if self._occupancy_mode not in (OCCUPANCY_HOME, OCCUPANCY_GUEST):
            _LOGGER.info(
                "Morning wakeup skipped — occupancy mode is '%s'",
                self._occupancy_mode,
            )
            return

        if self._manual_override_active:
            _LOGGER.info(
                "Morning wakeup skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            if self._emit_event_callback:
                self._emit_event_callback("morning_wakeup_skipped", {"reason": "manual_override"})
            return

        _LOGGER.warning("Morning wakeup: clearing any pending override state before restoring comfort")
        self.clear_manual_override(reason="morning_wakeup")

        # Deactivate fan if still running from overnight
        if self._fan_active:
            await self._deactivate_fan(reason="morning wakeup — resetting fan state")

        c = self._current_classification
        if not c:
            return

        if c.hvac_mode == "heat":
            comfort_heat = self.config["comfort_heat"]
            await self._set_temperature(
                comfort_heat,
                reason="morning wake-up — restoring heat comfort",
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "morning_wakeup",
                    {"mode": "heat", "target_f": comfort_heat},
                )
        elif c.hvac_mode == "cool":
            comfort_cool = self.config["comfort_cool"]
            await self._set_temperature(
                comfort_cool,
                reason="morning wake-up — restoring cool comfort",
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "morning_wakeup",
                    {"mode": "cool", "target_f": comfort_cool},
                )

    async def _activate_fan(self, *, reason: str) -> None:
        """Activate fan based on configured fan_mode."""
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan activation")
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would activate fan — %s", reason)
            return

        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]  # "fan" or "switch"
                    await self.hass.services.async_call(domain, "turn_on", {"entity_id": fan_entity})
                    _LOGGER.warning("Activated %s fan (%s) — %s", domain, fan_entity, reason)

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                hvac_state = self.hass.states.get(self.climate_entity)
                hvac_mode = hvac_state.state if hvac_state else "unknown"
                if hvac_mode == "off":
                    _LOGGER.debug(
                        "Activating HVAC fan-only mode while HVAC is 'off' — "
                        "this is intentional (economizer maintain phase); "
                        "most thermostats support fan circulation independent of heating/cooling"
                    )
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "on"},
                )
                _LOGGER.warning("Activated HVAC fan — %s", reason)

            self._fan_active = True
            self._fan_on_since = dt_util.now().isoformat()
            self._record_action("Fan activated", reason)
        finally:
            self._fan_command_pending = False

    async def _deactivate_fan(self, *, reason: str) -> None:
        """Deactivate fan based on configured fan_mode."""
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan deactivation")
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would deactivate fan — %s", reason)
            return

        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]
                    await self.hass.services.async_call(domain, "turn_off", {"entity_id": fan_entity})
                    _LOGGER.warning("Deactivated %s fan (%s) — %s", domain, fan_entity, reason)

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "auto"},
                )
                _LOGGER.warning("Deactivated HVAC fan — %s", reason)

            self._fan_active = False
            self._fan_on_since = None
            self._record_action("Fan deactivated", reason)
        finally:
            self._fan_command_pending = False

    async def check_window_cooling_opportunity(
        self,
        outdoor_temp: float,
        indoor_temp: float | None,
        windows_physically_open: bool,
        current_hour: int = -1,
    ) -> bool:
        """Two-phase window cooling strategy (Issue #27).

        Phase 1 — cool-down: When windows are open and outdoor temp has dropped
        near comfort, run AC to cool to set temp. Outdoor air assists, making
        AC more efficient.

        Phase 2 — maintain: Once indoor reaches comfort (or below), pause AC
        and let natural ventilation hold the temperature.

        Time-bounded to morning (6-9 AM) and evening (5 PM - midnight) hours.
        Respects aggressive_savings: when True, skip AC assist and rely on
        ventilation only.

        Returns True if economizer is active (either phase), False otherwise.
        """
        c = self._current_classification
        if not c or c.day_type != DAY_TYPE_HOT:
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp)
            return False

        # If natural ventilation is active, don't override it with economizer
        if self._natural_vent_active:
            return False

        unit = self.config.get("temp_unit", "fahrenheit")
        comfort_cool = self.config.get("comfort_cool", 75)
        delta = self.config.get("economizer_temp_delta", ECONOMIZER_TEMP_DELTA)
        aggressive_savings = self.config.get("aggressive_savings", False)

        # Time-bound check: only during morning (6-9) and evening (17-24) hours
        if current_hour < 0:
            # Default: allow (caller didn't pass hour, skip time gate)
            in_window = True
        else:
            in_window = (ECONOMIZER_MORNING_START_HOUR <= current_hour < ECONOMIZER_MORNING_END_HOUR) or (
                ECONOMIZER_EVENING_START_HOUR <= current_hour < ECONOMIZER_EVENING_END_HOUR
            )

        # Conditions for economizer eligibility
        eligible = windows_physically_open and outdoor_temp <= comfort_cool + delta and in_window

        if not eligible:
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp)
            return False

        # --- Economizer is eligible ---
        self._economizer_active = True

        if aggressive_savings:
            # Savings mode: skip AC, rely purely on ventilation
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._set_hvac_mode(
                    "off",
                    reason=f"economizer (savings) — outdoor {format_temp(outdoor_temp, unit)}, ventilation only",
                )
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer (savings): ventilation only, outdoor=%s",
                    format_temp(outdoor_temp, unit),
                )
            return True

        # Comfort mode: two-phase strategy
        if indoor_temp is not None and indoor_temp > comfort_cool:
            # Phase 1: cool-down — run AC, outdoor air assists efficiency
            if self._economizer_phase != "cool-down":
                self._economizer_phase = "cool-down"
                await self._set_hvac_mode(
                    "cool",
                    reason=(
                        f"economizer cool-down — indoor {format_temp(indoor_temp, unit)}"
                        f" > comfort {format_temp(comfort_cool, unit)},"
                        f" outdoor {format_temp(outdoor_temp, unit)} assisting"
                    ),
                )
                await self._set_temperature(
                    comfort_cool,
                    reason=f"economizer cool-down — target comfort {format_temp(comfort_cool, unit)}",
                )
                _LOGGER.info(
                    "Economizer phase=cool-down: indoor=%s, target=%s, outdoor=%s",
                    format_temp(indoor_temp, unit),
                    format_temp(comfort_cool, unit),
                    format_temp(outdoor_temp, unit),
                )
            return True
        else:
            # Phase 2: maintain — indoor at or below comfort, AC off
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._set_hvac_mode(
                    "off",
                    reason=(
                        f"economizer maintain — indoor"
                        f" {format_temp(indoor_temp if indoor_temp is not None else 0, unit)}"
                        " at comfort, ventilation holding"
                    ),
                )
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer phase=maintain: indoor=%s, AC off",
                    format_temp(indoor_temp if indoor_temp is not None else 0, unit),
                )
            return True

    async def _deactivate_economizer(self, outdoor_temp: float) -> None:
        """Deactivate economizer and resume normal AC operation."""
        unit = self.config.get("temp_unit", "fahrenheit")
        c = self._current_classification
        self._economizer_active = False
        self._economizer_phase = "inactive"
        await self._deactivate_fan(reason="economizer off — fan no longer needed")
        if c and c.hvac_mode == "cool":
            await self._set_hvac_mode(
                "cool",
                reason=f"economizer off — resuming normal AC (outdoor {format_temp(outdoor_temp, unit)})",
            )
            await self._set_temperature_for_mode(
                c,
                reason="economizer off — restoring comfort cooling",
            )
        _LOGGER.info("Economizer deactivated: outdoor=%s", format_temp(outdoor_temp, unit))

    def _get_indoor_temp_f(self) -> float | None:
        """Read indoor temperature in °F from the configured source."""
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
                            "Indoor temp entity %s has non-numeric state %r; skipping proximity check",
                            entity_id,
                            state.state,
                        )
            return None
        climate_state = self.hass.states.get(self.climate_entity)
        if climate_state:
            temp = climate_state.attributes.get("current_temperature")
            return to_fahrenheit(float(temp), unit) if temp is not None else None
        return None

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore automation state from persisted data.

        Grace timers are cleared on restart (natural reset point).
        Only pause state is restored so HVAC resumes correctly.
        """
        self._paused_by_door = state.get("paused_by_door", False)
        self._pre_pause_mode = state.get("pre_pause_mode")
        self._economizer_active = state.get("economizer_active", False)
        self._economizer_phase = state.get("economizer_phase", "inactive")
        self._last_action_time = state.get("last_action_time")
        self._last_action_reason = state.get("last_action_reason")
        self._manual_override_active = state.get("manual_override_active", False)
        self._manual_override_mode = state.get("manual_override_mode")
        self._manual_override_time = state.get("manual_override_time")
        self._fan_active = state.get("fan_active", False)
        self._fan_on_since = state.get("fan_on_since")
        self._fan_override_active = state.get("fan_override_active", False)
        self._fan_override_time = state.get("fan_override_time")
        self._fan_min_runtime_active = state.get("fan_min_runtime_active", False)
        # _fan_min_cycle_cancel is not serializable; cycle restarts fresh from coordinator startup
        last_notified = state.get("last_welcome_home_notified")
        if last_notified:
            try:
                self._last_welcome_home_notified = datetime.fromisoformat(last_notified)
            except (ValueError, TypeError):
                self._last_welcome_home_notified = None
        else:
            self._last_welcome_home_notified = None
        # Restore grace period state so coordinator can decide whether to
        # re-schedule the timer or clear it (Issue #227).
        self._grace_active = state.get("grace_active", False)
        self._last_resume_source = state.get("last_resume_source")
        self._grace_end_time = state.get("grace_end_time")
        self._grace_duration_seconds = int(state.get("grace_duration_seconds", 0))
        _LOGGER.info(
            "Restored automation state: paused=%s, pre_pause_mode=%s, "
            "last_action=%s, manual_override=%s, fan_active=%s, fan_override=%s",
            self._paused_by_door,
            self._pre_pause_mode,
            self._last_action_reason,
            self._manual_override_active,
            self._fan_active,
            self._fan_override_active,
        )

    def get_serializable_state(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the engine's internal state."""
        return {
            "paused_by_door": self._paused_by_door,
            "pre_pause_mode": self._pre_pause_mode,
            "grace_active": self._grace_active,
            "last_resume_source": self._last_resume_source,
            "grace_end_time": self._grace_end_time,
            "grace_duration_seconds": self._grace_duration_seconds,
            "dry_run": self.dry_run,
            "economizer_active": self._economizer_active,
            "economizer_phase": self._economizer_phase,
            "last_action_time": self._last_action_time,
            "last_action_reason": self._last_action_reason,
            "manual_override_active": self._manual_override_active,
            "manual_override_mode": self._manual_override_mode,
            "manual_override_time": self._manual_override_time,
            "override_confirm_pending": self._override_confirm_pending,
            "override_confirm_time": self._override_confirm_time,
            "fan_active": self._fan_active,
            "fan_on_since": self._fan_on_since,
            "fan_override_active": self._fan_override_active,
            "fan_override_time": self._fan_override_time,
            "fan_min_runtime_active": self._fan_min_runtime_active,
            "last_welcome_home_notified": (
                self._last_welcome_home_notified.isoformat() if self._last_welcome_home_notified else None
            ),
            "current_classification": (
                {
                    "day_type": self._current_classification.day_type,
                    "hvac_mode": self._current_classification.hvac_mode,
                    "trend_direction": self._current_classification.trend_direction,
                }
                if self._current_classification
                else None
            ),
        }

    def cleanup(self) -> None:
        """Remove all active listeners and cancel pending timers."""
        self._cancel_grace_timers()
        self._stop_fan_min_runtime_cycles()
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None
        if self._override_confirm_cancel:
            self._override_confirm_cancel()
            self._override_confirm_cancel = None
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
