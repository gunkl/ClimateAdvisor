"""Automation engine for Climate Advisor.

Manages the creation and dynamic adjustment of Home Assistant automations
based on the day classification and learning state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
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
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
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
    CONF_SLEEP_COOL,
    CONF_SLEEP_HEAT,
    CONF_WELCOME_HOME_DEBOUNCE,
    DAY_TYPE_HOT,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_FAN_MIN_RUNTIME_PER_HOUR,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_NATURAL_VENT_DELTA,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SLEEP_COOL,
    DEFAULT_SLEEP_HEAT,
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
from .temperature import convert_delta, format_temp, format_temp_delta, from_fahrenheit, to_fahrenheit

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThermostatCapabilities:
    """What modes and setpoint shapes a thermostat advertises (Issue #249).

    Derived from the climate entity's ``hvac_modes`` list and ``supported_features`` bitmask so the
    program-selection logic can choose how to arm the comfort band — single-mode (``cool``/``heat``)
    vs a ``heat_cool`` dual-setpoint band — based on what the hardware actually supports. An unknown
    or unavailable thermostat yields all-False capabilities and callers fall back to current behavior.
    """

    modes: tuple[str, ...]
    supports_heat: bool
    supports_cool: bool
    supports_heat_cool: bool  # a band mode (heat_cool or auto) is offered
    supports_dual_setpoint: bool  # band mode AND target_temp_low/high accepted
    raw_supported_features: int


def parse_thermostat_capabilities(hvac_modes: Any, supported_features: Any) -> ThermostatCapabilities:
    """Compute :class:`ThermostatCapabilities` from advertised modes + feature bitmask.

    Pure function (no HA state access) so it is trivially unit-testable. Defensive against
    missing/None/malformed inputs: a non-list ``hvac_modes`` or non-int ``supported_features``
    degrades to empty/zero, yielding all-False capabilities rather than raising.

    ``supports_dual_setpoint`` requires BOTH a band mode (``heat_cool``/``auto``) in ``hvac_modes``
    AND the ``TARGET_TEMPERATURE_RANGE`` feature bit, because Home Assistant only accepts
    ``target_temp_low``/``target_temp_high`` when that feature is present.
    """
    modes: tuple[str, ...] = tuple(str(m) for m in hvac_modes) if isinstance(hvac_modes, (list, tuple)) else ()

    try:
        features = int(supported_features)
    except (TypeError, ValueError):
        features = 0

    supports_heat_cool = "heat_cool" in modes or "auto" in modes
    supports_dual_setpoint = supports_heat_cool and bool(features & CLIMATE_FEATURE_TARGET_TEMP_RANGE)

    return ThermostatCapabilities(
        modes=modes,
        supports_heat="heat" in modes,
        supports_cool="cool" in modes,
        supports_heat_cool=supports_heat_cool,
        supports_dual_setpoint=supports_dual_setpoint,
        raw_supported_features=features,
    )


@dataclass(frozen=True)
class ComfortBand:
    """The comfort band the thermostat should hold (Issue #249 P3).

    Capability-free: the band expresses *what* we want (floor, ceiling, active edge) with no
    reference to thermostat modes. The actuation primitive :meth:`AutomationEngine._apply_comfort_band`
    reads capabilities and emits the appropriate command shape.

    ``active`` identifies the edge that the day primarily threatens:
    - ``"ceiling"`` — warm/hot day, afternoon; compressor defends the upper bound.
    - ``"floor"`` — cold/cool day, morning; heater defends the lower bound.
    ``active`` is used by single-mode actuation to pick which edge to arm; dual-setpoint devices
    always arm both.
    """

    floor: float
    ceiling: float
    active: str  # "ceiling" | "floor"
    reason: str


def select_comfort_band(
    classification: DayClassification,
    config: dict,
    *,
    occupancy_mode: str,
    in_sleep_window: bool,
    aggressive_savings: bool,
    pre_condition_achieved: bool = False,
) -> ComfortBand:
    """Compute the comfort band for the current plan — pure, no HA state access.

    Derives ``[floor, ceiling]`` and the active edge from the classification, occupancy,
    sleep-window state, and savings posture. **No capability mapping** — that is the
    actuation primitive's job.

    Band logic:
    - vacation: deep setback on both edges; ``active="ceiling"`` (a cool-capable unit defends
      the wide ceiling, the dominant concern in an empty home).
    - away: standard setback on both edges; ``active="ceiling"`` for the same reason.
    - sleep: ``sleep_heat``/``sleep_cool`` band; active follows day type.
    - occupied + awake (home/guest), ANY day type: the "lazy" comfort band
      ``[comfort_heat, comfort_cool]`` — the thermostat pre-heats the morning to comfort_heat and
      cools the afternoon to comfort_cool. Suppression to a setback edge applies ONLY when
      away/asleep. ``active`` marks the day's dominant edge for single-mode devices (floor on a heat
      day, ceiling otherwise); pre-cool lowers the ceiling on a hot day.
    ``aggressive_savings`` widens BOTH comfort edges by ``CEILING_ESCALATION_SAVINGS_MARGIN_F``
    (floor down, ceiling up) so the system runs less; setback/sleep bands are unaffected.
    """
    comfort_heat = float(config.get("comfort_heat", 70))
    comfort_cool = float(config.get("comfort_cool", 75))
    setback_heat = float(config.get("setback_heat", 60))
    setback_cool = float(config.get("setback_cool", 80))
    sleep_heat = float(config.get(CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT))
    sleep_cool = float(config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL))
    margin = CEILING_ESCALATION_SAVINGS_MARGIN_F if aggressive_savings else 0.0

    if occupancy_mode == OCCUPANCY_VACATION:
        floor = setback_heat - VACATION_SETBACK_EXTRA
        ceiling = setback_cool + VACATION_SETBACK_EXTRA
        active = "ceiling"
        ctx = "vacation"
    elif occupancy_mode == OCCUPANCY_AWAY:
        floor, ceiling, active, ctx = setback_heat, setback_cool, "ceiling", "away"
    elif in_sleep_window:
        active = "floor" if classification.hvac_mode == "heat" else "ceiling"
        floor, ceiling, ctx = sleep_heat, sleep_cool, "sleep"
    else:
        # Occupied + awake (home/guest): the "lazy" comfort band — hold BOTH edges at comfort so the
        # thermostat pre-heats the morning to comfort_heat and cools the afternoon to comfort_cool.
        # (Suppression to a setback edge happens only away/asleep.) `active` is the day's dominant
        # edge for single-mode devices: floor on a heat day, ceiling otherwise.
        active = "floor" if classification.hvac_mode == "heat" else "ceiling"
        floor = comfort_heat - margin
        ceiling = comfort_cool + margin
        if (
            classification.hvac_mode == "cool"
            and classification.pre_condition_target is not None
            and classification.pre_condition_target < 0
            and not pre_condition_achieved
        ):
            ceiling += float(classification.pre_condition_target)  # pre-cool lowers the ceiling
        ctx = "comfort"

    reason = (
        f"{ctx} band [{floor:.0f}/{ceiling:.0f}] (day={classification.day_type}, active={active}"
        f"{', aggressive' if aggressive_savings else ''})"
    )
    return ComfortBand(floor=floor, ceiling=ceiling, active=active, reason=reason)


def _in_sleep_window(now: datetime, config: dict) -> bool:
    """Return True if ``now`` falls in the configured sleep window (Issue #249).

    The window runs ``sleep_time`` → ``wake_time`` with midnight wraparound (the common night-owl
    case where ``sleep_time > wake_time``, e.g. 22:30 → 07:00): in-window iff
    ``now >= sleep_time OR now < wake_time``. Returns False when either time is unset or malformed —
    callers treat "unknown" as awake (apply the daytime program), matching the prior inline behavior.
    """
    from datetime import time as dt_time  # noqa: PLC0415

    sleep_time = config.get("sleep_time")
    wake_time = config.get("wake_time")
    if not sleep_time or not wake_time:
        return False
    try:
        _sp = str(sleep_time).split(":")
        sleep_h, sleep_m = int(_sp[0]), int(_sp[1])
        _wp = str(wake_time).split(":")
        wake_h, wake_m = int(_wp[0]), int(_wp[1])
        now_time = now.time().replace(second=0, microsecond=0)
        sleep_t = dt_time(sleep_h, sleep_m)
        wake_t = dt_time(wake_h, wake_m)
    except (ValueError, AttributeError):
        return False
    return now_time >= sleep_t or now_time < wake_t


def _fan_device_label(config: dict) -> str:
    """Return a human-readable device label for the active fan type."""
    mode = config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    if mode == FAN_MODE_WHOLE_HOUSE:
        return "whf"
    if mode == FAN_MODE_HVAC:
        return "hvac_fan"
    if mode == FAN_MODE_BOTH:
        return "both"
    return "none"


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

    if hvac_mode == "heat":
        comfort = config.get("comfort_heat", 70)
        floor = config.get("setback_heat", 60)
        rate = (thermal_model or {}).get("heating_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_F
        # Explicit sleep temp takes priority over adaptive calculation
        _explicit = config.get(CONF_SLEEP_HEAT)
        if _explicit is not None:
            return max(float(_explicit), floor)
    elif hvac_mode == "cool":
        comfort = config.get("comfort_cool", 75)
        floor = config.get("setback_cool", 80)
        rate = (thermal_model or {}).get("cooling_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_COOL_F
        # Explicit sleep temp takes priority over adaptive calculation
        # Note: warming-trend mid-night adjustment is handled separately by handle_pre_cool(),
        # not here. compute_bedtime_setback() always returns the raw configured sleep temp.
        _explicit = config.get(CONF_SLEEP_COOL)
        if _explicit is not None:
            return min(float(_explicit), floor)
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
        _adaptive_target = max(comfort - depth, floor) if hvac_mode == "heat" else min(comfort + depth, floor)
        _LOGGER.debug(
            "Adaptive setback: rate=%.2f°F/hr overnight=%.0fmin → depth=%.1f°F target=%.1f°F (%s mode)",
            rate,
            available,
            depth,
            _adaptive_target,
            hvac_mode,
        )

    if hvac_mode == "heat":
        raw = comfort - depth
        return max(raw, floor)
    else:  # cool
        raw = comfort + depth
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

        # Issue #392 Fix 3: serialize the six automation decision-pass entry points
        # (apply_classification, handle_door_window_open, handle_all_doors_windows_closed,
        # check_natural_vent_conditions, _re_pause_for_open_sensor, nat_vent_temperature_check)
        # against each other. asyncio is single-threaded but not atomic across awaits — without
        # this lock, two triggers firing close together (e.g. a sensor-open debounce callback and
        # a thermostat temperature-tick callback) can interleave and race on shared engine state
        # (_natural_vent_active, _fan_active, _pre_fan_hvac_mode, _paused_by_door). See
        # docs/08-COMPUTATION-REFERENCE.md §9g for the deadlock-avoidance analysis (none of the
        # six methods calls another of the six directly in the same stack, so a plain
        # `async with self._decision_lock:` wrap is safe — no `_impl` extraction needed).
        self._decision_lock = asyncio.Lock()
        # Issue #396: holder tracking so a stuck/slow lock is diagnosable from logs alone —
        # the #392 lock shipped with WARNING-level logging for the contended-and-blocked case
        # (hvac_write_blocked_whf_active) but nothing for "a method is waiting on this lock and
        # it isn't coming back," which is the failure mode that actually occurred. Set
        # immediately after acquiring, cleared in a finally immediately before release.
        self._decision_lock_holder: str | None = None
        self._decision_lock_held_since: datetime | None = None

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
        # HVAC mode captured before whole-house fan activation (Issue #277 Fix C).
        # Restored when the whole-house fan deactivates so AC/heat resumes.
        self._pre_fan_hvac_mode: str | None = None
        self._hvac_command_pending: bool = False  # transient: distinguishes integration vs manual HVAC changes
        self._temp_command_pending: bool = False  # transient: distinguishes integration vs manual temp changes
        self._temp_command_time: datetime | None = None  # last system-initiated temp setpoint command timestamp
        self._pending_setpoint_single: float | None = None  # single setpoint validation: commanded temp (service units)
        self._pending_setpoint_mode: str | None = None  # single setpoint validation: commanded mode ("cool"|"heat")
        # Issue #411: consecutive setpoint_rejected count for the current commanded value.
        # Reset to 0 whenever a setpoint is confirmed/accepted; incremented on each rejection.
        # On the 2nd+ consecutive rejection, the retry nudges the setpoint by ±1°F first to
        # force the device to recognize a real change before sending the actual target.
        self._setpoint_reject_streak: int = 0
        self._write_seq: int = 0  # monotonic counter: validation callbacks skip if a newer write has superseded them
        self._hvac_command_time: datetime | None = None  # last system-initiated HVAC command timestamp
        self._fan_command_time: datetime | None = None  # last system-initiated fan command timestamp (race guard)
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

        # Thermostatic fan backstop timer (Issue #327): self-rescheduling timer started in
        # _activate_fan, cancelled in _deactivate_fan + cleanup. Ensures fan_thermostat_check
        # fires even when temperature sensors update slowly.
        self._fan_thermo_cancel: Any | None = None

        # Event log callback — set by coordinator after construction
        self._emit_event_callback: Any | None = None

        # Coordinator refresh callback — called after grace expiry so HA sensor
        # state updates immediately rather than waiting for the next 30-min poll
        # (Issue #290 Fix 1).  Set by coordinator after construction.
        self._request_refresh_callback: Any | None = None

        # Post-grace fan-check callback — called at every exit path of _on_grace_expired()
        # after clear_manual_override() so coordinator can re-evaluate whether nat-vent
        # should be adopted from the current fan state (Issue #359).
        # Set by coordinator after construction.
        self._post_grace_fan_check_callback: Callable[[], None] | None = None

        # Today's DailyRecord — set by coordinator; used for bedtime setback tracking
        self._today_record: Any | None = None

        # Issue #96: classification event dedup — track last emitted (day_type, hvac_mode) pair
        self._last_classification_applied: tuple[str, str] | None = None
        # Issue #96: override event dedup — track last emission time
        self._last_override_detected_time: datetime | None = None

        # Resume-from-pause tracking (Issue #47)
        self._resumed_from_pause: bool = False
        self._sensor_check_callback: Any | None = None  # Set by coordinator: returns True if any sensor open

        # Issue #423: physical fan ground-truth callbacks — set by coordinator after
        # construction, mirroring _sensor_check_callback/_emit_event_callback above.
        # Used by _reconcile_fan_physical_drift() to self-correct a stale _fan_active.
        self._get_fan_physical_state_callback: Any | None = None
        self._is_recent_fan_command_callback: Any | None = None
        self._fan_drift_tick_count: int = 0

        # Welcome home notification debounce (Issue #59)
        self._last_welcome_home_notified: datetime | None = None

        # Thermal model — set by coordinator before apply_classification()
        self._thermal_model: dict = {}

        # Hourly forecast temps — injected by coordinator on each 30-min poll
        self._hourly_forecast_temps: list[dict] = []

        # Occupancy mode — synced by coordinator (Issue #85)
        self._occupancy_mode: str = OCCUPANCY_HOME

        # Pre-cool achievement gate (Issue #295) — once the home reaches the pre-cool
        # target temperature for the day, revert to comfort_cool ceiling for the rest
        # of the day rather than holding the lower pre-cool setpoint all afternoon.
        self._pre_condition_achieved: bool = False
        self._pre_condition_achieved_date: str | None = None

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

    @property
    def _fan_running(self) -> bool:
        """Whether any CA-owned fan (HVAC blower or whole-house) is currently running.

        Collapses the recurring ``self._fan_active or self._natural_vent_active`` OR
        pattern into a single derived property (Issue #392 Fix 1e) — the two flags are
        one concept ("is CA's fan on") fractured into two names. Stepping stone toward a
        future ``FanSession`` extraction (see Issue #392 shaping analysis).
        """
        return self._fan_active or self._natural_vent_active

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

    def handle_fan_manual_override(self, fan_before: str = "", fan_after: str = "") -> None:
        """Handle a manual fan state change — sets fan override flag + grace (Issue #327).

        Idempotent: safe to call even if an override is already active (re-stamps the
        time and restarts the grace period so the timer is always fresh).

        Args:
            fan_before: Fan state before the manual change (e.g. "on", "auto").
            fan_after: Fan state after the manual change.
        """
        self._stop_fan_min_runtime_cycles()
        self._fan_override_active = True
        self._fan_override_time = dt_util.now().isoformat()
        _LOGGER.info(
            "Fan override: set — manual fan change detected %s->%s, override active since %s, grace period starting",
            fan_before or "?",
            fan_after or "?",
            self._fan_override_time,
        )
        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_manual_override",
                {
                    "fan_before": fan_before,
                    "fan_after": fan_after,
                    "override_active_since": self._fan_override_time,
                    "fan_device": _fan_device_label(self.config),
                },
            )
        self._start_grace_period("manual", trigger="fan_manual_override")

    def on_fan_turned_off(self, fan_before: str = "", fan_after: str = "") -> None:
        """Handle the user turning the fan OFF — clears fan state and gates nat-vent re-activation (Issue #359).

        Unlike ``handle_fan_manual_override`` (which is for fan-ON and sets ``_fan_override_active``),
        this method DOES NOT set ``_fan_override_active``: that flag means "user turned fan on, CA backs
        off".  Fan-off instead starts a grace period so nat-vent is not immediately re-activated before
        conditions are verified.

        Args:
            fan_before: Fan state before the change (e.g. "on", "auto").
            fan_after: Fan state after the change (e.g. "off", "auto").
        """
        _LOGGER.info(
            "Fan turned off by user: fan=%s->%s, trigger=fan_off",
            fan_before or "?",
            fan_after or "?",
        )

        # _fan_override_active must NOT be set here (it is the "user turned fan ON" flag).
        # If it is somehow already True at this point that indicates a missed transition
        # elsewhere — clear it and warn so the inconsistency is visible in logs.
        if self._fan_override_active:
            _LOGGER.warning(
                "Fan turned off but _fan_override_active was True (stale override) — clearing. "
                "fan_before=%s fan_after=%s",
                fan_before or "?",
                fan_after or "?",
            )
            self._fan_override_active = False
            self._fan_override_time = None

        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_cancel",
                {
                    "fan_before": fan_before,
                    "fan_after": fan_after,
                    "trigger": "fan_off",
                    "fan_device": _fan_device_label(self.config),
                },
            )

        self._clear_fan_flags_and_start_grace(
            reason=f"fan={fan_before or '?'}->{fan_after or '?'}",
            trigger_label="fan_off",
            preserve_nat_vent_session=False,
        )

    def _clear_fan_flags_and_start_grace(
        self,
        *,
        reason: str,
        trigger_label: str = "fan_off",
        preserve_nat_vent_session: bool = False,
    ) -> None:
        """Shared "fan confirmed off" flag-clearing + grace-period logic.

        Extracted so both a genuine user fan-off (``on_fan_turned_off()``, always ends the
        nat-vent session — the user made a real decision) and a physical-drift
        self-correction (``_reconcile_fan_physical_drift()``, Issue #423 — CA's own belief was
        wrong, the session should survive so cycling-on logic can immediately re-evaluate) can
        share the mechanics without the drift-correction path silently killing a nat-vent
        session it never should have.

        Callers emit their own specific event (with a literal event-type string) before
        calling this — mirroring the established `_exit_nat_vent()` pattern — so the static
        event-coverage check (`tests/test_activity_renderers.py`) can still find the literal,
        and so each caller's payload shape stays under its own control.

        Args:
            reason: Human-readable reason logged alongside the correction.
            trigger_label: `trigger` field on the grace period, for log/event correlation.
            preserve_nat_vent_session: When True, `_natural_vent_active` is left untouched so
                the session survives the correction (Issue #423 drift-correction case). When
                False (the default, matching the original `on_fan_turned_off()` behavior), the
                session ends — a genuine fan-off is a real end-of-session signal.
        """
        self._fan_active = False
        self._fan_on_since = None
        if not preserve_nat_vent_session:
            self._natural_vent_active = False

        _LOGGER.info(
            "Fan flags cleared (%s): _fan_active/_fan_on_since cleared, _natural_vent_active %s;"
            " starting %s grace period",
            reason,
            "preserved" if preserve_nat_vent_session else "cleared",
            trigger_label,
        )

        # Restart min-runtime cycle scheduling (same as clear_fan_override does)
        self.hass.async_create_task(self.start_min_fan_runtime_cycles())

        # Start grace period to gate nat-vent re-activation — same duration as manual grace
        # but with a distinct trigger string so logs/events are distinguishable.
        self._start_grace_period("manual", trigger=trigger_label)

    def clear_fan_override(self) -> None:
        """Clear the fan override flag (called at transition points, Issue #327).

        Idempotent: no-op if no override is currently active.
        After clearing, restarts the min-runtime cycle that was suspended when the
        override was set.
        """
        if self._fan_override_active:
            _LOGGER.info(
                "Fan override: cleared — override active since %s, resuming CA fan control",
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
        old_setpoint_f: float | None = None,
        new_setpoint_f: float | None = None,
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
            old_setpoint_f: Previous thermostat setpoint in degrees F (setpoint overrides only).
            new_setpoint_f: New thermostat setpoint in degrees F (setpoint overrides only).
        """
        self.start_override_confirmation(
            source=source,
            old_mode=old_mode,
            new_mode=new_mode,
            classification_mode=classification_mode,
            old_setpoint_f=old_setpoint_f,
            new_setpoint_f=new_setpoint_f,
        )

    def start_override_confirmation(
        self,
        source: str,
        *,
        old_mode: str | None = None,
        new_mode: str | None = None,
        classification_mode: str | None = None,
        old_setpoint_f: float | None = None,
        new_setpoint_f: float | None = None,
    ) -> None:
        """Begin the override confirmation window (Issue #76).

        Args:
            source: "normal" for regular operation overrides,
                    "pause" for overrides detected during a door/window pause.
            old_mode: Previous hvac_mode (for enriched event payload).
            new_mode: New hvac_mode detected.
            classification_mode: What classification expects (for event payload).
            old_setpoint_f: Previous thermostat setpoint in degrees F (setpoint overrides only).
            new_setpoint_f: New thermostat setpoint in degrees F (setpoint overrides only).
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
                        "old_setpoint_f": old_setpoint_f,
                        "new_setpoint_f": new_setpoint_f,
                        "indoor_f": self._indoor_f_for_event(),
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
                if self.config.get(CONF_MANUAL_GRACE_NOTIFY, True):
                    self.hass.async_create_task(
                        self._notify(
                            "Brief thermostat adjustment detected — treated as transient "
                            "(reverted within confirmation window). "
                            "Climate Advisor continues normal operation.",
                            "Climate Advisor",
                            notification_type="override_self_resolved",
                        )
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

    @contextlib.asynccontextmanager
    async def _decision_pass(self, method_name: str):
        """Acquire ``self._decision_lock`` with wait/hold instrumentation (Issue #396).

        Logs when a method starts waiting on the lock and how long it waited once
        acquired, and tracks who currently holds it (`_decision_lock_holder` /
        `_decision_lock_held_since`) so a stuck or slow lock is diagnosable from logs
        alone instead of requiring another multi-hour investigation.
        """
        _wait_start = dt_util.now()
        if self._decision_lock.locked():
            _LOGGER.debug(
                "[decision-lock] %s: waiting — currently held by %s since %s",
                method_name,
                self._decision_lock_holder,
                self._decision_lock_held_since,
            )
        async with self._decision_lock:
            _wait_seconds = (dt_util.now() - _wait_start).total_seconds()
            self._decision_lock_holder = method_name
            self._decision_lock_held_since = dt_util.now()
            _LOGGER.debug(
                "[decision-lock] %s: acquired (waited %.3fs)",
                method_name,
                _wait_seconds,
            )
            try:
                yield
            finally:
                _held_seconds = (dt_util.now() - self._decision_lock_held_since).total_seconds()
                _LOGGER.debug(
                    "[decision-lock] %s: releasing (held %.3fs)",
                    method_name,
                    _held_seconds,
                )
                self._decision_lock_holder = None
                self._decision_lock_held_since = None

    async def apply_classification(
        self,
        classification: DayClassification,
        predicted_indoor: list[dict] | None = None,
        indoor_temp: float | None = None,
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
            indoor_temp: Current indoor temperature in °F. When provided, used
                to evaluate the pre-cool achievement gate (Issue #295). When
                None the achievement check is skipped for this cycle.
        """
        async with self._decision_pass("apply_classification"):
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

            # Issue #337: while paused by open door/window, suppress the band and hold HVAC off.
            if self._paused_by_door:
                _LOGGER.warning(
                    "apply_classification: door/window open (_paused_by_door=True) — "
                    "suppressing band, ensuring HVAC off; day_type=%s",
                    classification.day_type,
                )
                _cs_paused = self.hass.states.get(self.climate_entity)
                if _cs_paused is not None and _cs_paused.state != "off":
                    _LOGGER.info(
                        "apply_classification: thermostat in state=%r — forcing off (windows open)",
                        _cs_paused.state,
                    )
                    await self._set_hvac_mode(
                        "off",
                        reason="classification cycle: door/window open — HVAC suppressed while paused",
                    )
                else:
                    _LOGGER.info("apply_classification: thermostat already off — no mode change needed (windows open)")
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "classification_suppressed_paused",
                        {"day_type": classification.day_type, "hvac_mode": classification.hvac_mode},
                    )
                return

            # Issue #338: while nat-vent is active with savings mode, enforce floor-only HVAC so the
            # 30-minute cycle cannot re-arm the ceiling (compressor) through open windows.
            # With savings off, call the helper to keep the full band current, then continue so the
            # ODE ceiling guard can still fire as a safety backstop if a breach is predicted.
            if self._natural_vent_active:
                _aggressive = bool(self.config.get("aggressive_savings", False))
                _LOGGER.info(
                    "apply_classification: nat-vent active — enforcing nat-vent band ac_assist=%s day_type=%s",
                    not _aggressive,
                    classification.day_type,
                )
                await self._apply_nat_vent_hvac_state()
                if _aggressive:
                    # Savings mode: no compressor through open windows — skip ceiling guard
                    return
                # Issue #392 Fix 1b: WHF/BOTH is mutually exclusive with the compressor by
                # design (_activate_fan suppresses HVAC) — skip select_comfort_band()/the ODE
                # ceiling guard entirely rather than letting the choke-point guard silently drop
                # the write. FAN_MODE_HVAC keeps falling through (fan/AC coexist safely).
                _fan_cfg_cls = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
                if _fan_cfg_cls in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
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
                            "indoor_f": indoor_temp,
                        },
                    )
            else:
                _LOGGER.debug(
                    "classification_applied suppressed — same as last (%s/%s)",
                    classification.day_type,
                    classification.hvac_mode,
                )

            # Issue #295: pre-cool achievement gate — daily reset + detection.
            # Reset the flag at the start of each new calendar day so the pre-cool
            # ceiling offset re-arms each morning.  Check whether the home has
            # already reached the pre-cool target temperature; if so, set the flag
            # so select_comfort_band() reverts to comfort_cool for the rest of the day.
            _today = dt_util.now().strftime("%Y-%m-%d")
            if self._pre_condition_achieved_date != _today:
                self._pre_condition_achieved = False
                self._pre_condition_achieved_date = _today

            if (
                not self._pre_condition_achieved
                and classification.pre_condition
                and classification.pre_condition_target is not None
                and classification.pre_condition_target < 0
                and indoor_temp is not None
            ):
                _absolute_target = float(self.config.get("comfort_cool", 75)) + float(
                    classification.pre_condition_target
                )
                if indoor_temp <= _absolute_target:
                    self._pre_condition_achieved = True
                    _LOGGER.info(
                        "Pre-cool achieved (indoor=%.1f°F ≤ target=%.1f°F) — reverting to comfort"
                        " ceiling for rest of day",
                        indoor_temp,
                        _absolute_target,
                    )

            # Arm the comfort band — the thermostat holds the house; no mode-specific dispatch needed.
            cls_reason = (
                f"daily classification — {classification.day_type} day,"
                f" trend {classification.trend_direction} {format_temp_delta(classification.trend_magnitude, unit)}"
            )
            _band = select_comfort_band(
                classification,
                self.config,
                occupancy_mode=self._occupancy_mode,
                in_sleep_window=_in_sleep_window(dt_util.now(), self.config),
                aggressive_savings=bool(self.config.get("aggressive_savings", False)),
                pre_condition_achieved=self._pre_condition_achieved,
            )
            await self._apply_comfort_band(_band, reason=cls_reason)

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
                # Issue #392 Fix 1: the ceiling threshold is archetype-aware — None for whole-house-fan
                # mode (direction-only, no ceiling handoff; see _ceiling_threshold() docstring).
                _ceiling_threshold_val = self._ceiling_threshold(_comfort_cool_cg)
                if not _model_eligible:
                    _LOGGER.debug("ODE ceiling guard: skipped — k_passive=%s, conf=%s", _k_passive, _conf)
                elif _outdoor is None or _indoor_cg is None:
                    _LOGGER.debug("ODE ceiling guard: skipped — missing outdoor/indoor temps")
                elif _ceiling_threshold_val is None:
                    # Issue #402: archetypes with no ceiling-based compressor handoff
                    # (FAN_MODE_WHOLE_HOUSE/BOTH — see _ceiling_threshold() docstring) must never
                    # escalate to AC here at all, not just stay dormant while nat-vent happens to
                    # be active. Previously, a brief transient where _natural_vent_active flipped
                    # False (e.g. a pause/reactivate cycle) let dormancy lift and this guard arm
                    # 'cool' mode + a setpoint — which the reactivation gate then immediately
                    # undid (that gate also has no ceiling threshold for this archetype, so
                    # nothing blocked it from reactivating nat-vent right away) — producing a
                    # rapid escalate/reactivate oscillation with redundant thermostat writes
                    # every time this function re-evaluated, instead of a single clean decision.
                    _LOGGER.debug(
                        "ODE ceiling guard: dormant — no ceiling-based compressor handoff for this"
                        " fan archetype (WHOLE_HOUSE/BOTH); free cooling is direction-only"
                    )
                elif _outdoor <= _indoor_cg and self._natural_vent_active and _indoor_cg <= _ceiling_threshold_val:
                    _LOGGER.debug(
                        "ODE ceiling guard: dormant — outdoor %.1f <= indoor %.1f, nat-vent running,"
                        " indoor <= ceiling threshold %s (free cooling viable)",
                        _outdoor,
                        _indoor_cg,
                        _ceiling_threshold_val,
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
                            _breach_ts.astimezone(UTC)
                            if _breach_ts.tzinfo is not None
                            else _breach_ts.replace(tzinfo=UTC)
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
                                await self._deactivate_fan(
                                    reason=(
                                        f"ceiling guard override — indoor {_indoor_cg:.1f}°F approaching"
                                        f" comfort_cool {_comfort_cool_cg:.1f}°F, breach predicted in"
                                        f" {_hours_to_breach:.1f}h — switching to active cooling"
                                    )
                                )
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
                                mode="cool",
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

    async def _apply_comfort_band(self, band: ComfortBand, *, reason: str) -> None:
        """Arm the thermostat with the comfort band (always single-setpoint).

        Reads live thermostat capabilities and emits ONE ``set_temperature`` call with
        ``hvac_mode`` included so the thermostat is in the right mode and HA deduplication
        is bypassed:
        - ``active="ceiling"``, cool-capable → ``set_temperature`` with ``hvac_mode="cool"``
          and ``temperature=band.ceiling``.
        - ``active="floor"``, heat-capable → ``set_temperature`` with ``hvac_mode="heat"``
          and ``temperature=band.floor``.
        - device cannot serve the active edge or state unavailable → log and return (defensive
          no-op).

        Emits ``comfort_band_applied`` event so the harness/scenarios can assert on band decisions.
        """
        caps = self._get_thermostat_capabilities()

        if band.active == "ceiling" and caps.supports_cool:
            await self._set_temperature(band.ceiling, reason=reason, mode="cool")
            _cmd_shape = "cool"
        elif band.active == "floor" and caps.supports_heat:
            await self._set_temperature(band.floor, reason=reason, mode="heat")
            _cmd_shape = "heat"
        else:
            # The thermostat advertises no mode that can defend the active edge (e.g. a heat-only
            # unit on a warm day, or an unavailable entity). Surface this at INFO in real operation
            # so a silently-unarmed home is observable; stay quiet in dry-run.
            _log = _LOGGER.debug if self.dry_run else _LOGGER.info
            _log(
                "_apply_comfort_band: no capable mode for active=%r (modes=%s) — band not armed this cycle",
                band.active,
                list(caps.modes),
            )
            return

        if self._emit_event_callback:
            self._emit_event_callback(
                "comfort_band_applied",
                {
                    "floor": band.floor,
                    "ceiling": band.ceiling,
                    "active": band.active,
                    "mode": _cmd_shape,
                    "reason": band.reason,
                    "indoor_f": self._indoor_f_for_event(),
                },
            )

    async def _set_hvac_mode(self, mode: str, *, reason: str) -> None:
        """Set the thermostat HVAC mode."""
        # Issue #392 Fix 1b: structural choke-point guard — WHF/AC mutual exclusion is
        # enforced here rather than by convention at every one of the ~13 call sites.
        if mode != "off" and self._whf_owns_hvac():
            _LOGGER.warning("HVAC write blocked — whole-house fan owns thermostat (%s)", reason)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "hvac_write_blocked_whf_active",
                    {"attempted_mode": mode, "reason": reason},
                )
            return
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
                    # Stamp _fan_command_time BEFORE the service call so the race guard
                    # suppresses the cloud-thermostat echo that arrives >30 s later
                    # (Issue #277 Fix A1).
                    self._fan_command_time = dt_util.now()
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

    async def _set_temperature(self, temperature: float, *, reason: str, mode: str = "cool") -> None:
        """Set the thermostat target temperature with hvac_mode in a single call.

        Args:
            temperature: Target temperature in internal Fahrenheit.
            reason: Human-readable reason for logging.
            mode: "cool" (ceiling setpoint) or "heat" (floor setpoint).  Sent as
                ``hvac_mode`` in the service call so the thermostat is always in the
                correct mode and HA deduplication is bypassed (the mode key makes
                every call distinct even when temperature hasn't changed).
        """
        # Issue #392 Fix 1b: structural choke-point guard — WHF/AC mutual exclusion is
        # enforced here rather than by convention at every call site.
        if mode != "off" and self._whf_owns_hvac():
            _LOGGER.warning("HVAC write blocked — whole-house fan owns thermostat (%s)", reason)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "hvac_write_blocked_whf_active",
                    {"attempted_mode": mode, "reason": reason},
                )
            return
        unit = self.config.get("temp_unit", "fahrenheit")
        # Convert internal °F to user's unit before sending to HA climate entity
        service_temp = from_fahrenheit(temperature, unit)
        if self.dry_run:
            _LOGGER.info(
                "[DRY RUN] Would set temperature to %s (%s mode) — %s",
                format_temp(temperature, unit),
                mode,
                reason,
            )
            return
        # Check setpoint is appropriate for commanded mode
        if mode == "cool" and temperature < (self.config.get("comfort_heat", 70) - 1.0):
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
                        "hvac_mode": mode,
                        "setpoint_f": temperature,
                        "comfort_heat": self.config.get("comfort_heat", 70),
                        "comfort_cool": self.config.get("comfort_cool", 76),
                    },
                )
        elif mode == "heat" and temperature > (self.config.get("comfort_cool", 76) + 1.0):
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
                        "hvac_mode": mode,
                        "setpoint_f": temperature,
                        "comfort_heat": self.config.get("comfort_heat", 70),
                        "comfort_cool": self.config.get("comfort_cool", 76),
                    },
                )
        # Set state tracking BEFORE the write so the validation callback always
        # compares against the intended final setpoint.
        _now = dt_util.now()
        self._pending_setpoint_single = service_temp
        self._pending_setpoint_mode = mode
        self._write_seq += 1
        _my_seq = self._write_seq
        self._temp_command_time = _now
        self._temp_command_pending = True
        # hvac_mode is embedded in the set_temperature call, so register it as a
        # commanded mode change — coordinator uses these to suppress mode-change
        # echoes from CA's own writes (mirrors what _set_hvac_mode used to do).
        self._last_commanded_hvac_mode = mode
        self._last_commanded_hvac_time = _now
        self._hvac_command_time = _now
        try:
            # Single call: hvac_mode + temperature together.  Including hvac_mode in every
            # call bypasses HA deduplication (the mode key makes each call distinct) and
            # ensures the thermostat is always in the correct mode.
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": self.climate_entity,
                    "hvac_mode": mode,
                    "temperature": service_temp,
                },
            )
        finally:
            self._temp_command_pending = False

        async def _check_single_setpoint_accepted() -> None:
            if self._write_seq != _my_seq:
                return
            state = self.hass.states.get(self.climate_entity)
            if state is None:
                return
            reported = state.attributes.get("temperature")
            if reported is None:
                return
            _TOLERANCE = 0.6
            if abs(float(reported) - self._pending_setpoint_single) > _TOLERANCE:
                self._setpoint_reject_streak += 1
                _LOGGER.error(
                    "Setpoint validation FAILED: commanded=%.1f (%s mode), "
                    "thermostat reports=%.1f — reject streak=%d — scheduling retry in 15 minutes",
                    self._pending_setpoint_single,
                    self._pending_setpoint_mode,
                    reported,
                    self._setpoint_reject_streak,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "setpoint_rejected",
                        {
                            "commanded": self._pending_setpoint_single,
                            "reported": float(reported),
                        },
                    )
                # Retry after 15 minutes if no newer command has superseded this one.
                _retry_seq = _my_seq
                _retry_temp = service_temp
                _retry_mode = mode
                # Issue #411: on the 2nd+ consecutive rejection for this commanded value,
                # nudge the setpoint by ±1°F first — some thermostat integrations dedup a
                # repeated identical set_temperature payload, so retrying with the exact
                # same value can never succeed. A brief nudge forces the device to
                # recognize a real change before the actual target is sent 30s later.
                _do_nudge = self._setpoint_reject_streak >= 2

                async def _retry_callback(_now: Any) -> None:
                    if self._write_seq != _retry_seq:
                        return  # newer command superseded; skip retry
                    if _do_nudge:
                        _nudge_delta = convert_delta(1.0, unit)
                        _nudge_temp = (
                            _retry_temp + _nudge_delta if _retry_mode == "cool" else _retry_temp - _nudge_delta
                        )
                        _LOGGER.warning(
                            "Retrying setpoint write after repeated rejection (streak=%d):"
                            " nudging to %.1f %s before real target %.1f %s",
                            self._setpoint_reject_streak,
                            _nudge_temp,
                            _retry_mode,
                            _retry_temp,
                            _retry_mode,
                        )
                        if self._emit_event_callback:
                            self._emit_event_callback(
                                "setpoint_nudge",
                                {
                                    "nudge_value": _nudge_temp,
                                    "real_target": _retry_temp,
                                    "mode": _retry_mode,
                                },
                            )
                        await self.hass.services.async_call(
                            "climate",
                            "set_temperature",
                            {
                                "entity_id": self.climate_entity,
                                "hvac_mode": _retry_mode,
                                "temperature": _nudge_temp,
                            },
                        )

                        async def _send_real_target(_later: Any) -> None:
                            if self._write_seq != _retry_seq:
                                return  # newer command superseded; skip
                            _LOGGER.info(
                                "Sending real target after nudge: %.1f %s",
                                _retry_temp,
                                _retry_mode,
                            )
                            await self._set_temperature(_retry_temp, reason="retry/setpoint_nudge", mode=_retry_mode)

                        @callback
                        def _schedule_real_target(_later: Any) -> None:
                            self.hass.async_create_task(_send_real_target(_later))

                        async_call_later(self.hass, 30, _schedule_real_target)
                    else:
                        _LOGGER.warning(
                            "Retrying setpoint write after rejection: %.0f°F %s",
                            _retry_temp,
                            _retry_mode,
                        )
                        await self._set_temperature(_retry_temp, reason="retry/setpoint_rejected", mode=_retry_mode)

                @callback
                def _schedule_retry(_now: Any) -> None:
                    self.hass.async_create_task(_retry_callback(_now))

                async_call_later(self.hass, 900, _schedule_retry)
            else:
                self._setpoint_reject_streak = 0
                _LOGGER.info(
                    "Setpoint confirmed by thermostat: temperature=%.1f (%s mode)",
                    reported,
                    self._pending_setpoint_mode,
                )

        @callback
        def _schedule_check(_now: Any) -> None:
            self.hass.async_create_task(_check_single_setpoint_accepted())

        async_call_later(self.hass, 10, _schedule_check)
        _LOGGER.warning(
            "Set temperature to %s — %s",
            format_temp(temperature, unit),
            reason,
        )
        self._record_action(f"Set temp to {format_temp(temperature, unit)}", reason)

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
            floor_target = float(self.config["comfort_heat"])
            await self._set_temperature(floor_target, reason=reason, mode="heat")
            return
        elif c.hvac_mode == "cool":
            ceiling_target = float(self.config["comfort_cool"])
            if c.pre_condition and c.pre_condition_target and c.pre_condition_target < 0:
                # Pre-cool: target is below comfort
                ceiling_target = ceiling_target + c.pre_condition_target
                reason = f"{reason} (pre-cool offset {format_temp_delta(abs(c.pre_condition_target), unit)})"
            await self._set_temperature(ceiling_target, reason=reason, mode="cool")
            return
        else:
            return

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
        async with self._decision_pass("handle_door_window_open"):
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
            comfort_heat = self._nat_vent_reactivation_floor()
            _LOGGER.debug(
                "Nat vent gate check (%s): outdoor=%s indoor=%s comfort_heat=%.1f threshold=%.1f | "
                "dir=%s floor=%s ceiling=%s",
                entity_id,
                f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                f"{indoor:.1f}" if indoor is not None else "unavailable",
                comfort_heat,
                nat_vent_threshold,
                outdoor is not None and indoor is not None and outdoor < indoor,
                indoor is not None and indoor > comfort_heat,
                outdoor is not None and outdoor < nat_vent_threshold,
            )
            # Issue #411 (Pass 4): shared reactivation gate, previously hand-copied here as
            # "Issue #392 Fix 1: mirror the ODE ceiling guard's dormancy condition on
            # reactivation." No hysteresis applied at this call site (default 0.0).
            _nat_vent_gate_entered = self._nat_vent_may_reactivate(
                outdoor=outdoor,
                indoor=indoor,
                comfort_heat=comfort_heat,
                comfort_cool=comfort_cool,
                threshold=nat_vent_threshold,
            )
            if _nat_vent_gate_entered:
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
                                {
                                    "forecast_peak": max(lookahead_temps),
                                    "threshold": nat_vent_threshold,
                                    "fan_device": _fan_device_label(self.config),
                                },
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
                                        "Nat vent skipped: floor predicted in %.2f hr < %.1f hr"
                                        " threshold (k_passive=%.3f)",
                                        time_to_floor,
                                        MIN_VIABLE_NAT_VENT_HOURS,
                                        k_passive,
                                    )
                                    if self._emit_event_callback:
                                        self._emit_event_callback(
                                            "nat_vent_floor_imminent_skip",
                                            {
                                                "time_to_floor_hr": round(time_to_floor, 2),
                                                "fan_device": _fan_device_label(self.config),
                                            },
                                        )

                if not _skip_nat_vent:
                    # Capture mode before nat_vent changes
                    _old_mode_nv = self.hass.states.get(self.climate_entity)
                    _old_mode_nv = _old_mode_nv.state if _old_mode_nv else "unknown"

                    nat_vent_reason = (
                        f"natural ventilation: outdoor {outdoor:.1f}F < indoor {indoor:.1f}F,"
                        f" outdoor {outdoor:.1f}F <= {nat_vent_threshold:.1f}F"
                    )
                    await self._activate_fan(reason=nat_vent_reason)
                    self._natural_vent_active = True
                    _LOGGER.info(
                        "Natural ventilation mode: outdoor %.1f°F < indoor %.1f°F,"
                        " outdoor ≤ target %.1f°F — fan on, applying nat-vent HVAC state",
                        outdoor,
                        indoor,
                        nat_vent_threshold,
                    )
                    await self._apply_nat_vent_hvac_state()
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "sensor_opened",
                            {
                                "entity": entity_id,
                                "result": "natural_ventilation",
                                "hvac_mode_change": f"{_old_mode_nv}→band-armed",
                                "fan_mode_change": "auto→on",
                            },
                        )
                    return

            if not _nat_vent_gate_entered:
                _LOGGER.info(
                    "Nat vent not started (%s): outdoor=%s indoor=%s — "
                    "primary gates failed (dir=%s floor=%s ceiling=%s) — proceeding to HVAC pause check",
                    entity_id,
                    f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                    f"{indoor:.1f}" if indoor is not None else "unavailable",
                    outdoor is not None and indoor is not None and outdoor < indoor,
                    indoor is not None and indoor > comfort_heat,
                    outdoor is not None and outdoor < nat_vent_threshold,
                )

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
        async with self._decision_pass("handle_all_doors_windows_closed"):
            was_nat_vent = self._natural_vent_active
            was_paused = self._paused_by_door
            if self._emit_event_callback:
                self._emit_event_callback(
                    "sensor_all_closed",
                    {"was_paused": was_paused, "was_nat_vent": was_nat_vent},
                )

            # Handle natural ventilation mode cleanup (sensors closed while in nat vent).
            # Issue #418: routed through the canonical _exit_nat_vent() choke point (Issue
            # #411) instead of hand-rolling the pause/grace decision here — this was one of
            # 2 remaining sites bypassing it. _exit_nat_vent() restores the pre-fan HVAC mode
            # and starts a grace period; the classification-aware re-arm this branch used to
            # do inline now happens when that grace period expires, via
            # _apply_current_scheduled_state() -> apply_classification() (up to
            # DEFAULT_AUTOMATION_GRACE_SECONDS later, not instantly — an accepted tradeoff for
            # unification, see #418). The "sensor_all_closed" event emitted above already
            # satisfies _exit_nat_vent()'s "caller emits its own specific event" contract.
            if self._natural_vent_active:
                await self._exit_nat_vent(reason="door/window closed — ending natural ventilation mode")
                return

            # Fix D (Issue #277): whole-house fan running outside nat-vent must stop
            # when all sensors close — otherwise it draws outdoor air through a closed
            # envelope, counteracting HVAC and wasting energy for the occupant.
            _fan_cfg_d = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            if (
                self._fan_active
                and _fan_cfg_d in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH)
                and not self._natural_vent_active
            ):
                _LOGGER.info("All sensors closed — stopping whole-house fan (was running outside nat-vent)")
                # emit_event=False: this transition is reported via sensor_all_closed above.
                await self._deactivate_fan(reason="all sensors closed — stopping whole-house fan", emit_event=False)

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
        async with self._decision_pass("check_natural_vent_conditions"):
            if not (self._paused_by_door or self._natural_vent_active):
                # Comfort-ceiling override (Issue #134): if grace is active and indoor has
                # risen above comfort_cool, allow re-evaluation so nat-vent can engage.
                # Grace still blocks rapid door-open/close cycling below the comfort ceiling.
                _indoor = self._get_indoor_temp_f()
                _cool = float(self.config.get("comfort_cool", 75))
                # Issue #244: a contact sensor open while HVAC is idle (door opened with
                # nothing to pause) must still be re-evaluated so nat-vent can engage when
                # outdoor later cools below indoor — otherwise the occupant misses free
                # evening cooling. Restricted to HVAC-not-actively-calling so we never fight
                # active heating/cooling.
                #
                # Issue #402 fix: originally this required the thermostat's armed MODE to be
                # literally "off". But _apply_comfort_band() legitimately arms "cool" mode as a
                # ceiling backstop once nat-vent releases HVAC ownership (so the compressor can
                # save the day if the breeze alone can't hold the ceiling) — and that backstop
                # arming was permanently blocking this reactivation path even though the
                # compressor was never actually running (hvac_action stayed "idle" because
                # indoor never reached the armed ceiling). Check hvac_action instead of the
                # armed mode: as long as the compressor isn't ACTIVELY calling for heat/cool,
                # passive/free WHF re-evaluation should still be allowed to resume.
                _hvac_state_244 = self.hass.states.get(self.climate_entity)
                _hvac_action_244 = (
                    str(_hvac_state_244.attributes.get("hvac_action", "")).lower() if _hvac_state_244 else ""
                )
                _hvac_off_244 = (
                    _hvac_state_244 is None
                    or getattr(_hvac_state_244, "state", "off") == "off"
                    or _hvac_action_244 in ("", "off", "idle")
                )
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
                _comfort_heat = self._nat_vent_reactivation_floor()
                _hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
                # Issue #411 Pass 4: this was a 4th hand-copied instance of the shared
                # reactivation gate (found after the initial 3-site extraction) — folded
                # into _nat_vent_may_reactivate() for consistency, not left as a copy.
                if self._nat_vent_may_reactivate(
                    outdoor=outdoor,
                    indoor=_indoor,
                    comfort_heat=_comfort_heat,
                    comfort_cool=comfort_cool,
                    threshold=threshold,
                    hysteresis=_hysteresis,
                ):
                    # Band stays armed — just activate the fan; the compressor self-arbitrates.
                    #
                    # Issue #402 follow-up: this reason text used to always claim
                    # "indoor > comfort_cool" regardless of which of the two conditions in
                    # the outer guard actually let execution reach here — grace_active with
                    # indoor above the ceiling (the original Issue #134 case), OR idle_open
                    # (a contact sensor open + HVAC not actively calling, widened in #402)
                    # — the latter has nothing to do with comfort_cool. The condition
                    # actually evaluated just above (outdoor cooler than indoor by more than
                    # hysteresis, indoor above the comfort floor, outdoor below the ceiling
                    # threshold) is what the message must describe instead.
                    await self._activate_fan(
                        reason=(
                            f"nat-vent re-engaged: outdoor {outdoor:.1f}°F < indoor {_indoor:.1f}°F"
                            f" − {_hysteresis:.1f}°F hysteresis, indoor > comfort_heat {_comfort_heat:.1f}°F,"
                            f" outdoor ≤ threshold {threshold:.1f}°F — free cooling still favorable"
                        )
                    )
                    self._natural_vent_active = True
                    await self._apply_nat_vent_hvac_state()
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
                if (
                    self._natural_vent_active
                    and indoor is not None
                    and comfort_cool is not None
                    and indoor > comfort_cool
                ):
                    # Issue #247: the ODE ceiling guard now ESCALATES to AC on the classification cycle
                    # when indoor breaches the ceiling under active nat-vent (its three-condition dormancy
                    # lifts), so this is an informational heads-up, not a stuck state.
                    _LOGGER.info(
                        "Nat-vent active but indoor %.1fF > comfort_cool %.1fF --"
                        " ceiling guard will escalate to AC this classification cycle",
                        indoor,
                        comfort_cool,
                    )
                # During sleep window, lower the hard exit floor to sleep_heat - hysteresis so the
                # cycling logic can gracefully pause the fan at sleep_heat before the session terminates.
                _hysteresis_cv = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
                if _in_sleep_window(dt_util.now(), self.config):
                    _sleep_heat_cv = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
                    _vent_floor = _sleep_heat_cv - _hysteresis_cv
                else:
                    _vent_floor = comfort_heat
                if indoor is not None and indoor <= _vent_floor:
                    self._natural_vent_active = False
                    await self._deactivate_fan(
                        reason=(
                            f"natural vent exit: indoor {indoor:.1f}\u00b0F \u2264 comfort floor"
                            f" {_vent_floor:.1f}\u00b0F"
                        )
                    )
                    _LOGGER.info(
                        "Natural vent exit (comfort floor): indoor %.1f\u00b0F"
                        " \u2264 floor %.1f\u00b0F \u2014 restoring heat",
                        indoor,
                        _vent_floor,
                    )
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_comfort_floor_exit",
                            {
                                "indoor_temp": indoor,
                                "comfort_heat": _vent_floor,
                                "fan_mode_change": "on→auto",
                                "fan_device": _fan_device_label(self.config),
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
                            {
                                "indoor": _indoor_away,
                                "comfort_cool": comfort_cool,
                                "fan_device": _fan_device_label(self.config),
                            },
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
                                _LOGGER.info(
                                    "Natural vent proactive exit: floor predicted in %.2f hr"
                                    " < %.1f hr threshold — exiting nat-vent session",
                                    time_to_floor,
                                    MIN_VIABLE_NAT_VENT_HOURS,
                                )
                                if self._emit_event_callback:
                                    self._emit_event_callback(
                                        "nat_vent_predicted_floor_exit",
                                        {
                                            "time_to_floor_hr": round(time_to_floor, 2),
                                            "fan_mode_change": "on→auto",
                                            "fan_device": _fan_device_label(self.config),
                                            "hvac_mode_restored": (
                                                self._current_classification.hvac_mode
                                                if self._current_classification
                                                else "unknown"
                                            ),
                                        },
                                    )
                                await self._exit_nat_vent(
                                    reason=(
                                        f"nat-vent proactive floor exit: indoor {_indoor_now:.1f}°F"
                                        f" predicted to reach comfort_heat {comfort_heat_now:.1f}°F"
                                        f" in {time_to_floor:.2f}h"
                                    )
                                )
                                return

            # NEW (Issue #115): exit if outdoor > indoor — airflow now strictly heating
            indoor = self._get_indoor_temp_f()
            if self._natural_vent_active and outdoor is not None and indoor is not None and outdoor > indoor:
                _LOGGER.info(
                    "Natural vent exit: outdoor %.1f°F > indoor %.1f°F — airflow reversed",
                    outdoor,
                    indoor,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_outdoor_rise_exit",
                        {"outdoor": outdoor, "indoor": indoor, "fan_device": _fan_device_label(self.config)},
                    )
                # set_outdoor_exit_time=True: this is the only exit path whose
                # _nat_vent_outdoor_exit_time is consumed by the reactivation lockout below.
                await self._exit_nat_vent(
                    reason=(f"nat vent exit: outdoor {outdoor:.1f}°F > indoor {indoor:.1f}°F — airflow reversed"),
                    set_outdoor_exit_time=True,
                )
                return

            if self._natural_vent_active and outdoor is not None and outdoor > threshold:
                # Outdoor got too warm — exit nat vent.
                # Issue #411: routing this through _exit_nat_vent() is an intentional behavior
                # change, not a no-op refactor — this path previously never captured
                # _pre_pause_mode before pausing (unlike the door-open pause path). It now does,
                # via _exit_nat_vent()'s sensor-open branch.
                _LOGGER.info(
                    "Natural vent exit: outdoor %.1f°F > threshold %.1f°F",
                    outdoor,
                    threshold,
                )
                await self._exit_nat_vent(
                    reason=f"natural vent exit: outdoor {outdoor:.1f}°F > threshold {threshold:.1f}°F"
                )
                return

            if self._paused_by_door and outdoor is not None and indoor is not None:
                hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
                lockout_s = float(
                    self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S)
                )
                comfort_heat = self._nat_vent_reactivation_floor()

                # Enforce lockout after outdoor-warm exit
                if self._nat_vent_outdoor_exit_time is not None:
                    elapsed = (dt_util.now() - self._nat_vent_outdoor_exit_time).total_seconds()
                    if elapsed < lockout_s:
                        _LOGGER.debug(
                            "Nat vent paused-by-door: lockout active — %.0fs elapsed of %.0fs (%.0fs remaining)",
                            elapsed,
                            lockout_s,
                            lockout_s - elapsed,
                        )
                        return

                _floor_ok = indoor > comfort_heat
                _ceiling_ok = outdoor < threshold
                # Issue #411 (Pass 4): shared reactivation gate, previously hand-copied here.
                _may_reactivate = self._nat_vent_may_reactivate(
                    outdoor=outdoor,
                    indoor=indoor,
                    comfort_heat=comfort_heat,
                    comfort_cool=comfort_cool,
                    threshold=threshold,
                    hysteresis=hysteresis,
                )
                if _may_reactivate:
                    # Outdoor cooled down — activate natural vent
                    await self._activate_fan(
                        reason=(
                            f"natural vent activated: outdoor {outdoor:.1f}°F"
                            f" < indoor {indoor:.1f}°F − {hysteresis:.1f}°F hysteresis,"
                            f" outdoor ≤ threshold {threshold:.1f}°F"
                        )
                    )
                    self._natural_vent_active = True
                    self._paused_by_door = False
                    _LOGGER.info(
                        "Natural vent activated: outdoor %.1f°F < indoor %.1f°F − %.1f°F hysteresis,"
                        " outdoor ≤ threshold %.1f°F while paused",
                        outdoor,
                        indoor,
                        hysteresis,
                        threshold,
                    )
                    await self._apply_nat_vent_hvac_state()
                else:
                    _LOGGER.debug(
                        "Nat vent paused-by-door: conditions not met — "
                        "outdoor=%.1f°F indoor=%.1f°F delta=%.1f°F (need>%.1f°F) "
                        "floor_ok=%s ceiling_ok=%s",
                        outdoor,
                        indoor,
                        indoor - outdoor,
                        hysteresis,
                        _floor_ok,
                        _ceiling_ok,
                    )

    async def nat_vent_temperature_check(self, current_temp: float) -> None:
        """Thermostat-style cycling: keep indoor near the comfort midpoint during a nat-vent session.

        Called on every thermostat temperature tick via coordinator._async_thermostat_changed.
        Also called as a 30-minute backstop from check_natural_vent_conditions().

        When indoor drops to (midpoint - hysteresis) the fan turns off temporarily — the
        nat-vent SESSION stays active (_natural_vent_active=True) and HVAC suppression is
        maintained (restore_hvac=False). When indoor warms back to (midpoint + hysteresis)
        the fan re-engages, subject to the outdoor-warm guard.
        """
        async with self._decision_pass("nat_vent_temperature_check"):
            if not self._natural_vent_active:
                return

            comfort_heat = float(self.config.get("comfort_heat", 70))
            comfort_cool = float(self.config.get("comfort_cool", 75))
            hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            if _in_sleep_window(dt_util.now(), self.config):
                sleep_heat = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
                nat_vent_target = sleep_heat + hysteresis  # e.g. 65+1=66; off at 65, on at 67
                # Hard exit floor is one hysteresis step below the cycling-off threshold so the
                # fan can cycle off gracefully at sleep_heat before the session ends.
                _hard_floor = sleep_heat - hysteresis  # e.g. 64°F
                _context = "sleep"
            else:
                nat_vent_target = (comfort_heat + comfort_cool) / 2.0
                _hard_floor = comfort_heat
                _context = "daytime"
            off_threshold = nat_vent_target - hysteresis
            on_threshold = nat_vent_target + hysteresis

            # Hard floor exit takes priority over cycling.
            # Sleep window: _hard_floor = sleep_heat - hysteresis (one step below cycling-off threshold),
            # allowing the fan to cycle off gracefully at sleep_heat before the session terminates.
            # Daytime: _hard_floor = comfort_heat (unchanged behaviour).
            if current_temp <= _hard_floor:
                _LOGGER.info(
                    "Nat-vent hard exit [%s] via temp-check: indoor %.1f°F ≤ floor %.1f°F — ending session",
                    _context,
                    current_temp,
                    _hard_floor,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_comfort_floor_exit",
                        {
                            "indoor_temp": current_temp,
                            "comfort_heat": _hard_floor,
                            "source": "temp_check",
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                await self._exit_nat_vent(
                    reason=(
                        f"nat-vent hard floor exit [{_context}]: indoor {current_temp:.1f}°F"
                        f" ≤ floor {_hard_floor:.1f}°F"
                    )
                )
                return

            if self._fan_active and current_temp <= off_threshold:
                _LOGGER.info(
                    "Nat-vent cycling [%s]: target=%.1f°F, off=%.1f°F, on=%.1f°F (fan_device=%s)"
                    " — indoor %.1f°F ≤ off_threshold, cycling fan off, session remains active",
                    _context,
                    nat_vent_target,
                    off_threshold,
                    on_threshold,
                    _fan_device_label(self.config),
                    current_temp,
                )
                # Deactivate the fan without restoring HVAC — session stays alive.
                # emit_event=False: this transition is reported via nat_vent_fan_off below.
                await self._deactivate_fan(reason="nat_vent_cycling_off", restore_hvac=False, emit_event=False)
                # _natural_vent_active intentionally left True — session continues
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_fan_off",
                        {
                            "indoor_temp": current_temp,
                            "off_threshold": off_threshold,
                            "target": nat_vent_target,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                return

            if not self._fan_active and current_temp >= on_threshold:
                outdoor = self._last_outdoor_temp
                if outdoor is not None and outdoor >= current_temp:
                    _LOGGER.info(
                        "Nat-vent cycling: indoor %.1f°F ≥ on_threshold %.1f°F"
                        " but outdoor %.1f°F ≥ indoor — skipping re-activation"
                        " (outdoor-warm exit condition active)",
                        current_temp,
                        on_threshold,
                        outdoor,
                    )
                    return
                _LOGGER.info(
                    "Nat-vent cycling [%s]: target=%.1f°F, off=%.1f°F, on=%.1f°F (fan_device=%s)"
                    " — indoor %.1f°F ≥ on_threshold, outdoor=%.1f°F, cycling fan on",
                    _context,
                    nat_vent_target,
                    off_threshold,
                    on_threshold,
                    _fan_device_label(self.config),
                    current_temp,
                    outdoor if outdoor is not None else 0.0,
                )
                # emit_event=False: this transition is reported via nat_vent_fan_on below.
                await self._activate_fan(reason="nat_vent_cycling_on", emit_event=False)
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_fan_on",
                        {
                            "indoor_temp": current_temp,
                            "on_threshold": on_threshold,
                            "target": nat_vent_target,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )

    async def fan_thermostat_check(
        self,
        *,
        indoor: float | None,
        outdoor: float | None,
        trigger: str,
    ) -> None:
        """Thermostatic safety check for any CA-owned running fan (Issue #327).

        Called on every indoor OR outdoor temperature change and by the 5-minute backstop
        timer.  Deactivates the fan when free-cooling is gone (outdoor >= indoor with the
        configured hysteresis margin) or when the comfort target has been reached.

        Design: idempotent, cheap, safe to call at high frequency.  No-op when no CA fan
        is active or when the user has a manual override in effect.

        Args:
            indoor:  Current indoor temperature in °F (None = unavailable).
            outdoor: Current outdoor temperature in °F (None = unavailable).
            trigger: Caller label for the DEBUG log ("indoor", "outdoor", "timer", etc.).
        """
        ca_fan_active = self._fan_running
        if not ca_fan_active:
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=keep",
                trigger,
                f"{indoor:.1f}" if indoor is not None else "unavailable",
                f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                False,
            )
            return

        if self._fan_override_active:
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=keep",
                trigger,
                f"{indoor:.1f}" if indoor is not None else "unavailable",
                f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                True,
            )
            return

        comfort_heat = float(self.config.get("comfort_heat", 70))

        # --- Check 1: free-cooling direction guard ---
        # Free cooling requires outdoor cooler than indoor. Once outdoor >= indoor the
        # airflow no longer cools (neutral or reversed) — stop. NOTE: NO hysteresis on the
        # STOP side; the anti-flap hysteresis lives on nat-vent RE-activation
        # (check_natural_vent_conditions). Subtracting it here would kill free cooling ~1°F
        # early — e.g. stop at outdoor 71 / indoor 72 while a favorable gradient remains.
        if outdoor is not None and indoor is not None and outdoor >= indoor:
            if self._natural_vent_active:
                # Issue #418: actually routed through the canonical nat-vent outdoor-rise
                # exit now (previously this comment claimed it did, but the code hand-rolled
                # _natural_vent_active/_paused_by_door/_deactivate_fan itself — which set
                # _paused_by_door=True while still restoring HVAC via _deactivate_fan()'s
                # default restore_hvac=True, contradicting the pause semantics, and never
                # captured _pre_pause_mode or checked whether a sensor was genuinely still
                # open). _exit_nat_vent() gets all three right, mirroring
                # check_natural_vent_conditions()'s equivalent outdoor-rise-exit call site.
                stop_reason = f"outdoor {outdoor:.1f}°F >= indoor {indoor:.1f}°F — airflow reversed"
                _LOGGER.debug(
                    "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=%s",
                    trigger,
                    f"{indoor:.1f}",
                    f"{outdoor:.1f}",
                    True,
                    f"stop:{stop_reason}",
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_outdoor_rise_exit",
                        {"outdoor": outdoor, "indoor": indoor, "fan_device": _fan_device_label(self.config)},
                    )
                await self._exit_nat_vent(
                    reason=f"nat vent exit (fast loop): {stop_reason}",
                    set_outdoor_exit_time=True,
                )
                return
            stop_reason = f"outdoor {outdoor:.1f}°F >= indoor {indoor:.1f}°F (free cooling gone)"
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=%s",
                trigger,
                f"{indoor:.1f}",
                f"{outdoor:.1f}",
                True,
                f"stop:{stop_reason}",
            )
            await self._deactivate_fan(reason=f"fan thermostat check — {stop_reason}")
            return

        # --- Check 2: cooled to target ---
        # A CA fan only runs while outdoor < indoor (Check 1 stops it otherwise), so it is ALWAYS
        # cooling. Stop once indoor has cooled to the comfort floor, to avoid overcooling. Do NOT
        # stop when indoor >= comfort_cool: for a cooling fan, being above the ceiling means "keep
        # cooling" — the inverse would shut the fan off exactly when the home is too warm and needs
        # it most (Issue #327 — caught by the fan_fast_stop_on_outdoor_rise scenario).
        #
        # Issue #402: this floor must be sleep-aware, mirroring the fix #374 already applied to
        # check_natural_vent_conditions() (line ~2182). This tick-level check fires on every
        # thermostat temperature change — far more often than the 30-min classification cycle —
        # so if it used the flat daytime comfort_heat floor during the sleep window, it would
        # always preempt nat_vent_temperature_check()'s correct sleep-window cycling (off at
        # sleep_heat, on at sleep_heat+2*hysteresis) before that logic ever got a chance to run,
        # permanently ending the nat-vent session at comfort_heat instead of letting it cycle.
        _hysteresis_ftc = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        if _in_sleep_window(dt_util.now(), self.config):
            _sleep_heat_ftc = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
            _vent_floor_ftc = _sleep_heat_ftc - _hysteresis_ftc
        else:
            _vent_floor_ftc = comfort_heat
        if indoor is not None and indoor <= _vent_floor_ftc:
            stop_reason = f"indoor {indoor:.1f}°F ≤ comfort floor {_vent_floor_ftc:.1f}°F (cooled to floor)"
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=%s",
                trigger,
                f"{indoor:.1f}",
                f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                True,
                f"stop:{stop_reason}",
            )
            if self._natural_vent_active:
                self._natural_vent_active = False
            await self._deactivate_fan(reason=f"fan thermostat check — {stop_reason}")
            return

        _LOGGER.debug(
            "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=keep",
            trigger,
            f"{indoor:.1f}" if indoor is not None else "unavailable",
            f"{outdoor:.1f}" if outdoor is not None else "unavailable",
            True,
        )

    async def reconcile_fan_on_startup(
        self,
        *,
        indoor: float | None,
        outdoor: float | None,
        thermostat_fan_running: bool,
        any_sensor_open: bool,
    ) -> None:
        """Reconcile fan state on HA startup / coalesce window (Issue #327).

        Called by the coordinator's _do_startup_coalesce after classification runs.
        Ensures a running fan always has an explicit owner — never silent limbo.

        Decision logic:
        - thermostat_fan_running False → ``no-fan``: ensure all fan flags are clean.
        - nat-vent eligible (any_sensor_open AND outdoor < indoor AND nat-vent gate passes)
          → ``adopt-on``: set _fan_active=True, _natural_vent_active=True, start backstop.
        - else fan is running but not warranted → ``turn-off``: deactivate per archetype.

        Args:
            indoor:               Current indoor temperature in °F (None = unavailable).
            outdoor:              Current outdoor temperature in °F (None = unavailable).
            thermostat_fan_running: The archetype-appropriate "is a fan physically running"
                                    ground-truth signal (Issue #423). Despite the name, this is
                                    NOT always the thermostat's own fan_mode/hvac_action —
                                    callers resolve it via
                                    coordinator._derive_thermostat_fan_running_for_reconcile(),
                                    which uses the thermostat's attributes for FAN_MODE_HVAC but
                                    the real configured WHF entity's physical state
                                    (_get_fan_physical_state()) for FAN_MODE_WHOLE_HOUSE, since
                                    those are physically separate devices. A prior version of
                                    every caller here always used the thermostat's attributes
                                    regardless of archetype, which could "adopt" a whole-house
                                    fan session based on an unrelated thermostat-internal fan
                                    blip while the real WHF was off (Issue #423).
            any_sensor_open:      True when at least one door/window sensor is open.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        archetype = fan_mode

        if not thermostat_fan_running:
            # Fan is off — ensure CA flags are clean (defence in depth), and release any
            # stranded WHF HVAC suppression left over from a session that ended without
            # a matching _deactivate_fan() call (Issue #405). Without this, a nat-vent
            # cycling-off (restore_hvac=False, by design) followed by a coalesce boundary
            # that observes the fan already off would clear _natural_vent_active here but
            # leave _pre_fan_hvac_mode stranded non-None forever, permanently blocking
            # every future HVAC write via _whf_owns_hvac() with no recovery path.
            self._fan_active = False
            self._fan_on_since = None
            self._natural_vent_active = False
            decision = "no-fan"
            _LOGGER.info(
                "Fan reconcile: thermostat_fan_running=%s nat_vent_eligible=%s decision=%s archetype=%s",
                thermostat_fan_running,
                False,
                decision,
                archetype,
            )
            await self._deactivate_fan(
                reason="startup reconcile — fan confirmed off, releasing any stranded HVAC suppression",
                restore_hvac=True,
            )
            return

        # Evaluate nat-vent eligibility
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
        comfort_cool = float(self.config.get("comfort_cool", 75))
        comfort_heat = self._nat_vent_reactivation_floor()
        nat_vent_threshold = comfort_cool + nat_vent_delta

        # Issue #417: folded into the shared _nat_vent_may_reactivate() gate instead of a
        # 5th hand-rolled copy — this hand-rolled version was also missing the sleep-aware
        # floor and the ceiling dormancy check the shared gate already accounts for.
        nat_vent_eligible = (
            fan_mode != FAN_MODE_DISABLED
            and any_sensor_open
            and self._nat_vent_may_reactivate(
                outdoor=outdoor,
                indoor=indoor,
                comfort_heat=comfort_heat,
                comfort_cool=comfort_cool,
                threshold=nat_vent_threshold,
                hysteresis=hysteresis,
            )
        )

        if nat_vent_eligible:
            # Adopt the running fan as CA-owned nat-vent
            decision = "adopt-on"
            self._fan_active = True
            self._fan_on_since = dt_util.now().isoformat()
            self._natural_vent_active = True
            # Start the thermostatic backstop now that CA owns this fan session
            self._start_fan_thermo_backstop()
            _LOGGER.info(
                "Fan reconcile: thermostat_fan_running=%s nat_vent_eligible=%s decision=%s archetype=%s",
                thermostat_fan_running,
                nat_vent_eligible,
                decision,
                archetype,
            )
            # Issue #402 follow-up: this branch previously left zero activity-log trace of
            # the fan being adopted as CA-owned at startup — the fan silently starts being
            # managed with no record of why, unlike the turn-off branch below which does
            # emit a fan_deactivated event. Record the adoption the same way.
            _adopt_reason = (
                f"startup reconcile — fan already running, indoor {indoor:.1f}°F,"
                f" outdoor {outdoor:.1f}°F, nat-vent conditions met — adopting as CA-owned"
            )
            self._record_action("Fan activated", _adopt_reason)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "fan_activated",
                    {
                        "reason": _adopt_reason,
                        "fan_mode": fan_mode,
                        "fan_device": _fan_device_label(self.config),
                    },
                )
        else:
            # Fan running but nat-vent not warranted — turn it off
            decision = "turn-off"
            _LOGGER.info(
                "Fan reconcile: thermostat_fan_running=%s nat_vent_eligible=%s decision=%s archetype=%s",
                thermostat_fan_running,
                nat_vent_eligible,
                decision,
                archetype,
            )
            _turn_off_reason = "startup reconcile — fan running without CA warrant"
            # Ensure flags are correct before deactivating — _exit_nat_vent()'s internal
            # _deactivate_fan() call is a no-op unless _fan_active reads True.
            self._fan_active = True  # let _deactivate_fan see an owned fan
            # Issue #417: route through the canonical _exit_nat_vent() choke point (Issue
            # #411) instead of hand-rolling the pause/grace decision here — this makes a
            # genuine reconcile-driven turn-off behave identically to the other nat-vent
            # exit sites (sets _paused_by_door + _pre_pause_mode when the window is still
            # open, starts a grace period otherwise). Emit a specific event first since
            # _exit_nat_vent() always passes emit_event=False to _deactivate_fan(),
            # assuming the caller already recorded one — this call site didn't before.
            if self._emit_event_callback:
                self._emit_event_callback(
                    "nat_vent_reconcile_exit",
                    {
                        "reason": _turn_off_reason,
                        "fan_device": _fan_device_label(self.config),
                    },
                )
            await self._exit_nat_vent(reason=_turn_off_reason)

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
            should_notify = self.config.get(CONF_MANUAL_GRACE_NOTIFY, True)
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
            if self._request_refresh_callback:
                self._request_refresh_callback()
            if self._post_grace_fan_check_callback:
                self._post_grace_fan_check_callback()
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
            if self._request_refresh_callback:
                self._request_refresh_callback()
            if self._post_grace_fan_check_callback:
                self._post_grace_fan_check_callback()
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
        if self._request_refresh_callback:
            self._request_refresh_callback()
        if self._post_grace_fan_check_callback:
            self._post_grace_fan_check_callback()
        _LOGGER.info("%s grace period expired (%d seconds)", source, duration)
        if self._emit_event_callback:
            self._emit_event_callback("grace_expired", {"source": source, "re_paused": False})

        if should_notify:
            if source == "manual":
                message = "Your manual thermostat override has expired. Climate Advisor has resumed automated control."
            else:
                message = (
                    f"Automation grace period expired ({duration // 60} minutes). "
                    "HVAC will now respond normally to door/window sensor changes."
                )
            self.hass.async_create_task(
                self._notify(
                    message,
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
        self._grace_end_time = None  # Bug 2 fix (Issue #321): prevent stuck-at-0 display
        self._last_resume_source = None

    async def _re_pause_for_open_sensor(self) -> None:
        """Re-pause HVAC because a sensor is still open when grace expired."""
        async with self._decision_pass("_re_pause_for_open_sensor"):
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
            comfort_heat = self._nat_vent_reactivation_floor()
            # Issue #411 (Pass 4): shared reactivation gate, previously hand-copied here as
            # "Issue #392 Fix 1: mirror the ODE ceiling guard's dormancy condition." No
            # hysteresis applied at this call site (default 0.0).
            nat_vent_threshold = comfort_cool + nat_vent_delta
            if self._nat_vent_may_reactivate(
                outdoor=outdoor,
                indoor=indoor,
                comfort_heat=comfort_heat,
                comfort_cool=comfort_cool,
                threshold=nat_vent_threshold,
            ):
                nat_vent_reason = (
                    f"grace expired — nat-vent: outdoor {outdoor:.1f}°F < indoor {indoor:.1f}°F,"
                    f" outdoor {outdoor:.1f}°F ≤ {nat_vent_threshold:.1f}°F"
                )
                await self._activate_fan(reason=nat_vent_reason)
                self._natural_vent_active = True
                await self._apply_nat_vent_hvac_state()
                _LOGGER.info(
                    "Re-check after grace: nat-vent conditions met — outdoor %.1f°F < indoor %.1f°F,"
                    " outdoor ≤ %.1f°F, band stays armed",
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

        # Determine if we're in a bedtime window (after sleep_time OR before wake_time).
        # Issue #249: extracted to the shared module-level _in_sleep_window() helper.
        if _in_sleep_window(now, self.config):
            _LOGGER.info(
                "Grace expired: in bedtime window (%s–%s) — applying bedtime setback",
                self.config.get("sleep_time"),
                self.config.get("wake_time"),
            )
            await self.handle_bedtime()
            return

        # Otherwise apply current classification
        if self._current_classification:
            _LOGGER.info("Grace expired: applying current classification")
            await self.apply_classification(self._current_classification)

    async def handle_occupancy_away(self) -> None:
        """Handle everyone leaving — apply setback."""
        self._occupancy_mode = OCCUPANCY_AWAY
        if self._paused_by_door:
            _LOGGER.info(
                "Occupancy away — door/window open (_paused_by_door=True), "
                "skipping setback band; occupancy recorded, HVAC remains off"
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback_suppressed_paused",
                    {"occupancy": "away", "reason": "paused_by_door"},
                )
            return
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

        # Arm the away setback band — covers both edges; thermostat self-arbitrates.
        _away_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=OCCUPANCY_AWAY,
            in_sleep_window=False,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        if self._emit_event_callback:
            self._emit_event_callback(
                "occupancy_setback",
                {
                    "mode": "away",
                    "floor": _away_band.floor,
                    "ceiling": _away_band.ceiling,
                    "occupancy": "away",
                    "indoor_f": self._indoor_f_for_event(),
                },
            )
        await self._apply_comfort_band(_away_band, reason="occupancy away — setback band")

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
                    {"mode": c.hvac_mode, "target_f": comfort, "indoor_f": self._indoor_f_for_event()},
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
        if self._paused_by_door:
            _LOGGER.info(
                "Occupancy vacation — door/window open (_paused_by_door=True), "
                "skipping setback band; occupancy recorded, HVAC remains off"
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_setback_suppressed_paused",
                    {"occupancy": "vacation", "reason": "paused_by_door"},
                )
            return
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

        # Arm the vacation deep-setback band — both edges widened; thermostat self-arbitrates.
        _vac_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=OCCUPANCY_VACATION,
            in_sleep_window=False,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        if self._emit_event_callback:
            self._emit_event_callback(
                "occupancy_setback",
                {
                    "mode": "vacation",
                    "floor": _vac_band.floor,
                    "ceiling": _vac_band.ceiling,
                    "occupancy": "vacation",
                    "indoor_f": self._indoor_f_for_event(),
                },
            )
        await self._apply_comfort_band(_vac_band, reason="vacation mode — deep setback band")

    async def handle_bedtime(self) -> None:
        """Apply bedtime setback."""
        # Issue #299: guard against double-write when this task is fired via async_create_task
        # in _check_startup_override() while apply_classification() runs in the same coordinator
        # cycle.  apply_classification() runs first (it is awaited), sets _temp_command_time, and
        # clears _temp_command_pending in its finally block.  By the time handle_bedtime() runs,
        # the flag is already clear — a time-based cooldown is required.
        if self._temp_command_time is not None and (dt_util.now() - self._temp_command_time).total_seconds() < 30:
            _LOGGER.debug("handle_bedtime: skipping — setpoint write within last 30s (startup dedup guard)")
            return

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
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "manual_override"
            return

        _LOGGER.warning("Bedtime setback: clearing any pending override state before applying sleep setback")
        self.clear_manual_override(reason="bedtime")

        c = self._current_classification
        if not c:
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "no_classification"
            # No sleep target available — deactivate fan unconditionally
            if self._fan_active and not self._fan_override_active:
                await self._deactivate_fan(reason="bedtime — no classification")
                self._natural_vent_active = False
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp=0)
            return

        # Compute sleep band first — needed for the nat-vent continuation gate below.
        _sleep_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=True,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )

        # Issue #370: Nat-vent continuation gate — if nat-vent is actively running and
        # outdoor air is below the sleep ceiling, allow free cooling to continue to the
        # sleep target instead of deactivating the fan and handing off to the compressor.
        # Applies to all fan archetypes (WHF, HVAC fan, BOTH).
        _outdoor_370 = self._last_outdoor_temp
        _nat_vent_can_reach = (
            self._natural_vent_active
            and self._fan_active
            and not self._fan_override_active
            and _outdoor_370 is not None
            and _outdoor_370 < _sleep_band.ceiling
        )
        if _nat_vent_can_reach:
            _LOGGER.info(
                "Nat-vent continues at bedtime: outdoor=%.1f°F below sleep_cool=%.1f°F — fan runs to sleep target",
                _outdoor_370,
                _sleep_band.ceiling,
            )
        else:
            if self._fan_active and not self._fan_override_active:
                await self._deactivate_fan(reason="bedtime — nat-vent not favorable for continuation")
                self._natural_vent_active = False
        if self._economizer_active:
            await self._deactivate_economizer(outdoor_temp=0)
        if self._emit_event_callback:
            self._emit_event_callback(
                "bedtime_setback",
                {
                    "mode": c.hvac_mode,
                    "floor": _sleep_band.floor,
                    "ceiling": _sleep_band.ceiling,
                    "active": _sleep_band.active,
                    "modifier": c.setback_modifier,
                },
            )
        if _nat_vent_can_reach and self._emit_event_callback:
            self._emit_event_callback(
                "nat_vent_bedtime_continue",
                {
                    "outdoor_temp": _outdoor_370,
                    "sleep_cool": _sleep_band.ceiling,
                    "fan_device": _fan_device_label(self.config),
                },
            )
        if self._today_record is not None:
            # Issue #402: key off _sleep_band.active (the edge _apply_comfort_band() actually
            # arms below), not c.hvac_mode. On a warm/mild day c.hvac_mode is "off", but the
            # sleep band's ceiling is still armed as a single-setpoint cool backstop — the
            # setback WAS applied, but the original `if hvac_mode == "heat"/"cool"` check had
            # no branch for "off", so DailyRecord never recorded it (and neither Applied nor
            # Skipped ever got populated on the majority of nights in a mild climate).
            if _sleep_band.active == "floor":
                self._today_record.setback_heat_applied_f = _sleep_band.floor
                self._today_record.setback_depth_f = abs(self.config.get("comfort_heat", 70) - _sleep_band.floor)
                self._today_record.setback_was_adaptive = False
            elif _sleep_band.active == "ceiling":
                self._today_record.setback_cool_applied_f = _sleep_band.ceiling
                self._today_record.setback_depth_f = abs(self.config.get("comfort_cool", 75) - _sleep_band.ceiling)
                self._today_record.setback_was_adaptive = False
        await self._apply_comfort_band(
            _sleep_band,
            reason=f"bedtime — sleep band [{_sleep_band.floor:.0f}/{_sleep_band.ceiling:.0f}]",
        )

    async def handle_pre_cool(self, indoor_temp: float | None, nat_vent_just_closed: bool) -> str:
        """Apply overnight pre-cool setpoint to bank cold thermal mass before a hot day.

        Fires at the pre-cool trigger time (nat-vent close + delay, or wake_time - 4h).
        Suppressed when nat-vent already brought indoor to or below the target.
        Returns a short status string for logging.
        """
        from .const import CONF_SLEEP_COOL, PRE_COOL_MIN_HEADROOM_F

        c = self._current_classification
        if not c or c.setback_modifier >= 0:
            _LOGGER.info(
                "Pre-cool trigger fired: skipped — no warming trend (modifier=%s)",
                getattr(c, "setback_modifier", "n/a"),
            )
            return "skipped: no warming trend"

        if self._occupancy_mode in (OCCUPANCY_VACATION, OCCUPANCY_AWAY):
            _LOGGER.info("Pre-cool skipped — %s mode (setback already active)", self._occupancy_mode)
            return f"skipped: {self._occupancy_mode}"

        if self._manual_override_active:
            _LOGGER.info(
                "Pre-cool skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            return "skipped: manual override"

        sleep_cool = float(self.config.get(CONF_SLEEP_COOL) or self.config.get("sleep_cool", 78.0))
        comfort_heat = float(self.config.get("comfort_heat", 70.0))
        raw_target = sleep_cool + c.setback_modifier  # setback_modifier is negative for warming trend
        floor = comfort_heat + PRE_COOL_MIN_HEADROOM_F
        pre_cool_target = max(raw_target, floor)

        _LOGGER.info(
            "Pre-cool trigger fired: indoor=%s°F, target=%.1f°F, modifier=%.1f (sleep_cool=%.1f, floor=%.1f)",
            f"{indoor_temp:.1f}" if indoor_temp is not None else "unknown",
            pre_cool_target,
            c.setback_modifier,
            sleep_cool,
            floor,
        )

        if raw_target < floor:
            _LOGGER.warning(
                "Pre-cool target %.1f°F below floor %.1f°F (comfort_heat=%.1f + headroom=%.1f); clamped to %.1f°F",
                raw_target,
                floor,
                comfort_heat,
                PRE_COOL_MIN_HEADROOM_F,
                pre_cool_target,
            )

        # If nat-vent just closed and already achieved target: suppress AC
        if nat_vent_just_closed and indoor_temp is not None and indoor_temp <= pre_cool_target:
            _LOGGER.info(
                "Pre-cool suppressed: nat-vent brought indoor to %.1f°F (target %.1f°F) — no AC needed",
                indoor_temp,
                pre_cool_target,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "pre_cool_suppressed_nat_vent",
                    {"indoor": indoor_temp, "target": pre_cool_target, "modifier": c.setback_modifier},
                )
            return f"suppressed: nat-vent achieved {indoor_temp:.1f}°F (target {pre_cool_target:.1f}°F)"

        # Get sleep heat floor from current sleep band so we preserve it
        _sleep_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=True,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        _pre_cool_band = ComfortBand(
            floor=_sleep_band.floor,
            ceiling=pre_cool_target,
            active="ceiling",
            reason=f"pre-cool — warming trend thermal mass banking (target {pre_cool_target:.0f}°F)",
        )

        if self._emit_event_callback:
            self._emit_event_callback(
                "pre_cool_applied",
                {
                    "target": pre_cool_target,
                    "modifier": c.setback_modifier,
                    "sleep_cool": sleep_cool,
                    "floor": floor,
                    "indoor": indoor_temp,
                    "nat_vent_suppressed": False,
                },
            )

        _LOGGER.info(
            "Pre-cool setpoint applied: cool ceiling %.1f°F (heat floor unchanged at %.1f°F)",
            pre_cool_target,
            _sleep_band.floor,
        )
        await self._apply_comfort_band(
            _pre_cool_band,
            reason=f"pre-cool — warming trend [{_sleep_band.floor:.0f}/{pre_cool_target:.0f}]",
        )
        return f"applied: {pre_cool_target:.1f}°F"

    async def handle_morning_wakeup(self, indoor_temp: float | None = None) -> None:
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

        # Morning pre-cool overshoot guard: warn if indoor is below comfort_heat (heat may fire)
        _current_indoor = indoor_temp
        _comfort_heat = float(self.config.get("comfort_heat", 70.0))
        if _current_indoor is not None:
            if _current_indoor < _comfort_heat:
                _LOGGER.warning(
                    "Morning check: indoor %.1f°F below comfort_heat %.1f°F — pre-cool overshoot; heat may fire",
                    _current_indoor,
                    _comfort_heat,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "pre_cool_overshoot",
                        {"indoor": _current_indoor, "comfort_heat": _comfort_heat},
                    )
            else:
                _LOGGER.info(
                    "Morning check: indoor %.1f°F ≥ comfort_heat %.1f°F — within guard",
                    _current_indoor,
                    _comfort_heat,
                )

        # Arm the daytime comfort band — waking up exits the sleep window.
        _wakeup_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=False,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        if self._emit_event_callback:
            self._emit_event_callback(
                "morning_wakeup",
                {
                    "mode": c.hvac_mode,
                    "floor": _wakeup_band.floor,
                    "ceiling": _wakeup_band.ceiling,
                    "active": _wakeup_band.active,
                },
            )
        await self._apply_comfort_band(
            _wakeup_band,
            reason=f"morning wake-up — comfort band [{_wakeup_band.floor:.0f}/{_wakeup_band.ceiling:.0f}]",
        )

    async def _exit_nat_vent(self, *, reason: str, set_outdoor_exit_time: bool = False) -> None:
        """Single choke point for ending a nat-vent session (Issue #411).

        Unifies the handoff previously hand-rolled at 4 separate call sites (Phase 2
        proactive floor exit, the reactive hard-floor exit, the outdoor-reversal exit,
        and the outdoor-too-warm exit) so every path checks the monitored sensor state
        before deciding whether to restore HVAC or pause, instead of each site
        re-deciding independently. Away-mode ceiling exit is intentionally NOT routed
        through this function — it is a different concept with no pause/grace state
        machine.

        Args:
            reason: Human-readable reason passed through to ``_deactivate_fan``.
            set_outdoor_exit_time: Only the outdoor-reversal exit passes True. Records
                ``_nat_vent_outdoor_exit_time`` for the paused-by-door reactivation
                lockout. Other exit paths must not set this as a side effect of this
                refactor (Issue #411 blast-radius finding).
        """
        self._natural_vent_active = False
        if set_outdoor_exit_time:
            self._nat_vent_outdoor_exit_time = dt_util.now()
        sensor_open = bool(self._sensor_check_callback and self._sensor_check_callback())
        # emit_event=False on both branches: every call site already emits its own more
        # specific exit event (nat_vent_predicted_floor_exit, nat_vent_comfort_floor_exit,
        # nat_vent_outdoor_rise_exit, etc.) before calling this method. Letting
        # _deactivate_fan() also emit a generic fan_deactivated event here would land at
        # the same timestamp and shadow the specific event in outcome-ordering consumers
        # (Issue #411 — found during test verification).
        if sensor_open:
            # Don't restore active HVAC into an open window — pause instead. The
            # existing pause/grace machinery (_re_pause_for_open_sensor) re-evaluates
            # nat-vent reactivation on the next grace-expiry cycle.
            await self._deactivate_fan(reason=reason, restore_hvac=False, emit_event=False)
            self._paused_by_door = True
            state = self.hass.states.get(self.climate_entity)
            self._pre_pause_mode = state.state if state and state.state != "off" else None
            _LOGGER.info(
                "Nat-vent exit (%s): monitored sensor still open — pausing HVAC (pre_pause_mode=%s)",
                reason,
                self._pre_pause_mode,
            )
        else:
            await self._deactivate_fan(reason=reason, emit_event=False)
            self._start_grace_period("automation", trigger="nat_vent_exit_resume")
            _LOGGER.info("Nat-vent exit (%s): sensors closed — restoring HVAC and starting grace period", reason)

    def _nat_vent_reactivation_floor(self) -> float:
        """Sleep-aware comfort floor for nat-vent reactivation/eligibility gates (Issue #417).

        Mirrors the sleep-window branch already used correctly by
        ``nat_vent_temperature_check()`` and ``fan_thermostat_check()``'s comfort-floor
        check. Every reactivation-gate call site (``_nat_vent_may_reactivate()`` and its
        4 callers, plus ``reconcile_fan_on_startup``) previously hardcoded the flat
        daytime ``comfort_heat`` floor with no sleep-window branch — during the sleep
        window, indoor temp sitting between ``sleep_heat`` and ``comfort_heat`` would
        read as "below the floor" and repeatedly reject reactivation, even though the
        session should stay armed until the (lower) sleep floor. This is the same
        failure mode already fixed once for the cycling functions under Issue #402;
        this closes the gap on the reactivation-gate side.
        """
        comfort_heat = float(self.config.get("comfort_heat", 70))
        if _in_sleep_window(dt_util.now(), self.config):
            return float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
        return comfort_heat

    def _nat_vent_may_reactivate(
        self,
        *,
        outdoor: float | None,
        indoor: float | None,
        comfort_heat: float,
        comfort_cool: float,
        threshold: float,
        hysteresis: float = 0.0,
    ) -> bool:
        """Shared 4-part reactivation gate for nat-vent (Issue #411, Pass 4).

        Extracted from 4 call sites (``handle_door_window_open``, the paused-by-door
        reactivation block, ``_re_pause_for_open_sensor``, and the Issue #134
        comfort-ceiling re-entry check in ``check_natural_vent_conditions``) that each
        hand-copied this identical condition — a documented prior production bug
        (#402) came from exactly this duplication drifting out of sync. Callers keep
        their own
        additional guards (e.g. the door-open path's rising-forecast check) and their
        own post-gate actions (starting the fan, clearing ``_paused_by_door``,
        applying nat-vent HVAC state) — this function returns only the shared boolean
        gate, mirroring how ``_ceiling_threshold()`` is scoped as a value helper.

        Args:
            outdoor: Current outdoor temperature (°F), or None if unavailable.
            indoor: Current indoor temperature (°F), or None if unavailable.
            comfort_heat: Comfort floor (°F) — indoor must be above this.
            comfort_cool: Comfort ceiling (°F) — used for the archetype-aware ceiling check.
            threshold: Outdoor must be below this (typically comfort_cool + nat_vent_delta).
                Passed in rather than recomputed here so this stays a pure boolean gate.
            hysteresis: Subtracted from indoor in the outdoor/indoor delta check. Callers
                that don't apply hysteresis (handle_door_window_open, _re_pause_for_open_sensor)
                pass 0.0 (the default); the paused-by-door reactivation block passes the
                configured nat-vent hysteresis.
        """
        if outdoor is None or indoor is None:
            return False
        ceiling_threshold = self._ceiling_threshold(comfort_cool)
        ceiling_ok = ceiling_threshold is None or indoor <= ceiling_threshold
        return outdoor < indoor - hysteresis and indoor > comfort_heat and outdoor < threshold and ceiling_ok

    def _ceiling_threshold(self, comfort_cool: float | None) -> float | None:
        """Ceiling above which the compressor should take over from fan-assisted cooling.

        Returns None for whole-house-fan mode (FAN_MODE_WHOLE_HOUSE / FAN_MODE_BOTH): a WHF
        is guaranteed to keep converging toward outdoor temperature as long as outdoor stays
        below indoor, so there is no ceiling-based handoff point — only the outdoor/indoor
        direction matters (Issue #392 Fix 1). HVAC fan mode coexists with the compressor (band
        stays armed per Issue #249), so the ceiling is a valid handoff signal there.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return None
        if comfort_cool is None:
            return None
        aggressive = bool(self.config.get("aggressive_savings", False))
        return comfort_cool + CEILING_ESCALATION_SAVINGS_MARGIN_F if aggressive else comfort_cool

    def _whf_owns_hvac(self) -> bool:
        """Whether a whole-house-fan session currently owns (suppresses) the thermostat.

        True when fan_mode is WHOLE_HOUSE/BOTH AND a suppression session is active
        (``_pre_fan_hvac_mode is not None`` — the same flag ``_activate_fan``/
        ``_deactivate_fan`` use to track an active suppression, not ``_natural_vent_active``,
        which also covers HVAC-fan-mode nat-vent where HVAC is NOT suppressed).

        Issue #392 Fix 1b: this is the seed of a future ``FanSession.may_run_hvac()`` object
        (see Issue #392 shaping analysis) — a single choke-point check standing in for the
        deferred `FanSession` extraction, not a permanent standalone guard.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        return fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and self._pre_fan_hvac_mode is not None

    async def _apply_nat_vent_hvac_state(self) -> None:
        """Apply the correct HVAC state when nat-vent is active.

        Called immediately after nat-vent activates (all paths) and on every
        30-minute apply_classification() cycle while nat-vent is active.

        FAN_MODE_WHOLE_HOUSE: no-op — HVAC is already suppressed by _activate_fan().
        FAN_MODE_HVAC + aggressive_savings=False: re-arm the full comfort band so the
            thermostat self-arbitrates and the compressor can assist if the breeze alone
            cannot hold the comfort ceiling.
        FAN_MODE_HVAC + aggressive_savings=True: arm the floor only (heat @ comfort_heat)
            so the compressor cannot run for cooling through open windows.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode in (FAN_MODE_DISABLED, FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            # WHOLE_HOUSE and BOTH: _activate_fan() already called _set_hvac_mode("off") to prevent
            # fighting the exhaust fan — re-arming a band here would immediately contradict that.
            _LOGGER.info(
                "_apply_nat_vent_hvac_state: no-op (fan_mode=%s — HVAC suppressed by _activate_fan or disabled)",
                fan_mode,
            )
            return

        aggressive_savings = bool(self.config.get("aggressive_savings", False))
        comfort_heat = float(self.config.get("comfort_heat", 70))
        comfort_cool = float(self.config.get("comfort_cool", 75))

        if not aggressive_savings:
            # Sleep window: skip the full-band setpoint call — apply_classification() will arm
            # the sleep band immediately after, so a prior full-band write would be overwritten
            # and would cause redundant thermostat calls all night.  Emit the status event so
            # the status card and activity report still show nat-vent as active.
            _in_sleep = _in_sleep_window(dt_util.now(), self.config)
            if _in_sleep:
                _LOGGER.info(
                    "_apply_nat_vent_hvac_state: sleep window in effect — skipping full-band setpoint"
                    " (deferring to sleep band); nat_vent_ac_assist_armed emitted comfort_heat=%.1f comfort_cool=%.1f",
                    comfort_heat,
                    comfort_cool,
                )
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_ac_assist_armed",
                        {
                            "comfort_heat": comfort_heat,
                            "comfort_cool": comfort_cool,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                return

            # Full comfort band — compressor may assist if breeze cannot hold the ceiling.
            _LOGGER.info(
                "_apply_nat_vent_hvac_state: AC assist armed — full band comfort_heat=%.1f comfort_cool=%.1f"
                " (aggressive_savings=off)",
                comfort_heat,
                comfort_cool,
            )
            _nat_vent_band = ComfortBand(
                floor=comfort_heat,
                ceiling=comfort_cool,
                active="ceiling",
                reason="nat-vent AC assist — full comfort band",
            )
            await self._apply_comfort_band(
                _nat_vent_band,
                reason="nat-vent AC assist: full band armed (aggressive_savings=off)",
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "nat_vent_ac_assist_armed",
                    {
                        "comfort_heat": comfort_heat,
                        "comfort_cool": comfort_cool,
                        "fan_device": _fan_device_label(self.config),
                    },
                )
        else:
            # Savings mode — floor guard only; ceiling disarmed so compressor cannot run
            # for cooling through open windows.
            _LOGGER.info(
                "_apply_nat_vent_hvac_state: savings mode — floor-only at comfort_heat=%.1f"
                " (aggressive_savings=on — ceiling disarmed)",
                comfort_heat,
            )
            await self._set_hvac_mode("heat", reason="nat-vent savings mode — floor guard only, ceiling disarmed")
            await self._set_temperature(
                comfort_heat, reason="nat-vent savings mode — protecting comfort floor", mode="heat"
            )

    async def _activate_fan(self, *, reason: str, emit_event: bool = True) -> None:
        """Activate fan based on configured fan_mode.

        Args:
            reason: Human-readable trigger source (logged + surfaced in the Activity Report).
            emit_event: When True (default), emit a ``fan_activated`` event to the event log
                so the Activity Report shows every CA fan command with its source. Callers
                that already emit a more specific event for the same transition (the nat-vent
                cycler / exit paths) pass False to avoid a duplicate row (Issue #331 follow-up).
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan activation")
            return

        # Issue #392 Fix 1c: idempotency guard — collapse redundant re-decisions from
        # multiple gate sites into a single real state transition.
        if self._fan_active:
            _LOGGER.debug("_activate_fan: already active — no-op (%s)", reason)
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would activate fan — %s", reason)
            return

        self._fan_command_time = dt_util.now()
        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                # Capture current HVAC mode before suppressing it (Issue #277 Fix C).
                # Whole-house fan exchanges outdoor air directly — running AC/heat
                # simultaneously fights the fan and wastes energy.
                _cs = self.hass.states.get(self.climate_entity)
                self._pre_fan_hvac_mode = _cs.state if _cs else None
                await self._set_hvac_mode(
                    "off",
                    reason="whole-house fan active — suppressing HVAC to prevent fighting outdoor air exchange",
                )

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
            if emit_event and self._emit_event_callback:
                self._emit_event_callback(
                    "fan_activated",
                    {"reason": reason, "fan_mode": fan_mode, "fan_device": _fan_device_label(self.config)},
                )

            # Post-fan setpoint verify: Ecobee may revert to comfort program after a fan command.
            # Re-assert our setpoint within 30s so the coordinator's _is_recent_temp_command guard
            # covers any delayed state report.
            _verify_seq = self._write_seq
            _expected_temp = self._pending_setpoint_single
            _expected_mode = self._last_commanded_hvac_mode

            async def _do_verify_after_fan_on() -> None:
                if self._write_seq != _verify_seq:
                    return  # newer command issued — skip
                if _expected_temp is None or _expected_mode is None:
                    return  # no active setpoint
                if self._manual_override_active:
                    return  # genuine confirmed override — don't fight it
                current_state = self.hass.states.get(self.climate_entity)
                if current_state is None:
                    return
                actual = current_state.attributes.get("temperature")
                if actual is None:
                    return
                try:
                    if (
                        abs(float(actual) - _expected_temp) > 0.6
                    ):  # 0.6°F — same tolerance as _check_single_setpoint_accepted
                        _LOGGER.info(
                            "Post-fan setpoint verify: thermostat %.1f°F != expected %.1f°F — re-asserting %s mode",
                            float(actual),
                            _expected_temp,
                            _expected_mode,
                        )
                        await self._set_temperature(
                            _expected_temp, reason="post-fan-verify/repair", mode=_expected_mode
                        )
                except (ValueError, TypeError):
                    pass

            @callback
            def _verify_setpoint_after_fan_on(_now: Any) -> None:
                self.hass.async_create_task(_do_verify_after_fan_on())

            async_call_later(self.hass, 30.0, _verify_setpoint_after_fan_on)

            # Issue #327: thermostatic backstop timer — fires every 5 min while the fan
            # is CA-owned; calls fan_thermostat_check so a slow-updating outdoor sensor
            # cannot leave the fan running indefinitely between state-listener events.
            self._start_fan_thermo_backstop()
        finally:
            self._fan_command_pending = False

    def _start_fan_thermo_backstop(self) -> None:
        """Start (or restart) the 5-minute thermostatic backstop timer (Issue #327).

        The timer is self-rescheduling: each fire re-schedules the next tick before
        calling fan_thermostat_check, so it runs continuously while the fan is active.
        Cancelled by _deactivate_fan and cleanup.
        """
        if self._fan_thermo_cancel:
            self._fan_thermo_cancel()
            self._fan_thermo_cancel = None

        @callback
        def _thermo_tick(_now: Any) -> None:
            self._fan_thermo_cancel = None
            self.hass.async_create_task(self._thermo_backstop_task())

        self._fan_thermo_cancel = async_call_later(self.hass, 5 * 60, _thermo_tick)

    async def _thermo_backstop_task(self) -> None:
        """Execute a thermostatic check and reschedule the backstop (Issue #327)."""
        indoor = self._get_indoor_temp_f()
        outdoor = self._last_outdoor_temp
        # Issue #423: self-healing physical-state check runs first — if _fan_active is stale
        # (e.g. from a reconcile that "adopted" a fan that was never actually turned on), correct
        # it here so fan_thermostat_check()/nat_vent_temperature_check() below see the corrected
        # state instead of stale-True on this same tick.
        self._reconcile_fan_physical_drift()
        await self.fan_thermostat_check(indoor=indoor, outdoor=outdoor, trigger="timer")
        # Issue #402 follow-up: nat_vent_temperature_check() (the function that owns the
        # cycling on/off-threshold decision) is otherwise invoked ONLY when the coordinator
        # detects the thermostat's current_temperature attribute change — it has no timer
        # of its own. fan_thermostat_check()'s backstop above only protects the coarser
        # hard floor (comfort_heat), not the cycling off-threshold, so indoor could sit
        # below the cycling off-threshold for minutes with nothing re-checking until a
        # genuine new temperature-changed event arrived. Piggyback the existing 5-minute
        # timer to also re-evaluate cycling while nat-vent is active.
        if self._natural_vent_active and indoor is not None:
            await self.nat_vent_temperature_check(indoor)
        # Re-arm only if the fan is still active after the check
        if self._fan_running:
            self._start_fan_thermo_backstop()

    def _cancel_fan_thermo_backstop(self) -> None:
        """Cancel the thermostatic backstop timer (Issue #327)."""
        if self._fan_thermo_cancel:
            self._fan_thermo_cancel()
            self._fan_thermo_cancel = None

    def _reconcile_fan_physical_drift(self) -> None:
        """Detect and self-correct a stale _fan_active=True with no matching physical fan (Issue #423).

        Closes the gap that let the reported incident persist for 3.5+ hours: nothing
        previously compared `_fan_active`'s belief against the real configured fan entity's
        physical state and corrected it — `_compute_fan_status()`/`_compute_whf_status()`
        already do this comparison, but only to render "active (unconfirmed)" in the UI.

        Only applies to FAN_MODE_WHOLE_HOUSE/FAN_MODE_BOTH with fan_state_feedback enabled —
        those are the only archetypes with an independent physical ground-truth read
        (`_get_fan_physical_state_callback`). FAN_MODE_HVAC has no separate physical entity to
        drift from (the thermostat's own attributes ARE the fan) and command-only mode
        (`_get_fan_physical_state_callback()` returns None) has no ground truth to compare
        against — both are no-ops here by construction.

        Guards against two false-positive sources:
        - Recent CA command echo/lag: skip if a fan command was issued in the last 30s
          (`_is_recent_fan_command_callback`, the same guard `_async_fan_entity_changed()`
          already uses for this exact purpose).
        - Single-tick transient: requires the drift to persist across 2 consecutive backstop
          ticks (5 min apart) before correcting, so a momentary sensor flap doesn't trigger a
          correct-then-immediately-re-adopt cycle every 5 minutes.

        On confirmed drift, clears the stale flags via `_clear_fan_flags_and_start_grace()`
        with `preserve_nat_vent_session=True` — the nat-vent session survives so the
        immediately-following `nat_vent_temperature_check()` call in `_thermo_backstop_task()`
        can re-fire `_activate_fan()` on the same tick if conditions still warrant it.
        """
        if not self._fan_active:
            self._fan_drift_tick_count = 0
            return

        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return

        if self._is_recent_fan_command_callback and self._is_recent_fan_command_callback(threshold_seconds=30.0):
            self._fan_drift_tick_count = 0
            return

        if not self._get_fan_physical_state_callback:
            return
        physical_on = self._get_fan_physical_state_callback()
        if physical_on is None:
            return  # command-only mode — no ground truth, nothing to reconcile
        if physical_on:
            self._fan_drift_tick_count = 0  # agrees — reset
            return

        # physical_on is False but _fan_active is True — disagreement.
        self._fan_drift_tick_count += 1
        if self._fan_drift_tick_count < 2:
            _LOGGER.info(
                "Fan physical-state drift detected (tick %d/2): _fan_active=True but physical"
                " state=off — awaiting confirmation tick before correcting",
                self._fan_drift_tick_count,
            )
            return

        _LOGGER.warning(
            "Fan physical-state drift confirmed over 2 backstop ticks: _fan_active=True but"
            " physical state=off — self-correcting stale flag (Issue #423)"
        )
        self._fan_drift_tick_count = 0
        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_cancel",
                {
                    "trigger": "physical_drift_correction",
                    "reason": "physical-state drift confirmed over 2 backstop ticks",
                    "fan_device": _fan_device_label(self.config),
                },
            )
        self._clear_fan_flags_and_start_grace(
            reason="physical-state drift confirmed over 2 backstop ticks",
            trigger_label="physical_drift_correction",
            preserve_nat_vent_session=True,
        )

    async def _deactivate_fan(self, *, reason: str, restore_hvac: bool = True, emit_event: bool = True) -> None:
        """Deactivate fan based on configured fan_mode.

        Args:
            reason: Human-readable reason for deactivation (logged + surfaced in the report).
            restore_hvac: When True (default), restores the HVAC mode that was suppressed
                when the whole-house fan activated. Pass False during nat-vent cycling
                (Bug 3 / Issue #321) so the session can continue without re-engaging HVAC
                between cycles — the fan turns off temporarily, but HVAC stays suppressed.
            emit_event: When True (default), emit a ``fan_deactivated`` event so the Activity
                Report shows every CA fan-off with its source. Callers that already emit a
                more specific event for the same transition (nat-vent cycler / exit paths)
                pass False to avoid a duplicate row (Issue #331 follow-up).
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan deactivation")
            return

        # Issue #392 Fix 1c: idempotency guard — collapse redundant re-decisions from
        # multiple gate sites into a single real state transition.
        #
        # Issue #402 follow-up: the fan can already be physically off from a nat-vent
        # cycling-off (nat_vent_temperature_check calls _deactivate_fan(restore_hvac=False),
        # which intentionally leaves _pre_fan_hvac_mode set so the session survives the
        # cycle). If a later caller asks to restore_hvac=True while the fan is already
        # inactive, a bare early-return here would skip the HVAC-suppression release too —
        # permanently stranding _pre_fan_hvac_mode set and blocking all future HVAC writes
        # via _whf_owns_hvac(). Only skip the physical "turn fan off" step; still honor a
        # pending HVAC restore.
        if not self._fan_active:
            if restore_hvac and self._pre_fan_hvac_mode is not None:
                _restore_mode = self._pre_fan_hvac_mode
                self._pre_fan_hvac_mode = None
                _LOGGER.debug(
                    "_deactivate_fan: fan already inactive but restoring stranded HVAC suppression (%s)", reason
                )
                await self._set_hvac_mode(
                    _restore_mode,
                    reason=f"whole-house fan already stopped — restoring HVAC mode ({_restore_mode})",
                )
            else:
                _LOGGER.debug("_deactivate_fan: already inactive — no-op (%s)", reason)
            return

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would deactivate fan — %s", reason)
            return

        self._fan_command_time = dt_util.now()
        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]
                    await self.hass.services.async_call(domain, "turn_off", {"entity_id": fan_entity})
                    _LOGGER.warning("Deactivated %s fan (%s) — %s", domain, fan_entity, reason)

                # Restore prior HVAC mode that was suppressed when the fan activated
                # (Issue #277 Fix C). Only restore if we have a stored mode to go back to.
                # Skipped during nat-vent cycling (restore_hvac=False) so HVAC stays
                # suppressed between fan-on and fan-off cycles within the same session.
                if restore_hvac and self._pre_fan_hvac_mode is not None:
                    _restore_mode = self._pre_fan_hvac_mode
                    # Issue #392: clear _pre_fan_hvac_mode BEFORE issuing the restore write, not
                    # after. _whf_owns_hvac() (the Fix 1b choke-point guard in _set_hvac_mode())
                    # treats "_pre_fan_hvac_mode is not None" as "WHF still owns the thermostat" —
                    # the restore write itself ends the suppression session, so ownership must be
                    # released before the write, or the guard self-blocks the very call that is
                    # supposed to un-suppress HVAC.
                    self._pre_fan_hvac_mode = None
                    await self._set_hvac_mode(
                        _restore_mode,
                        reason=f"whole-house fan stopped — restoring HVAC mode ({_restore_mode})",
                    )

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "auto"},
                )
                _LOGGER.warning("Deactivated HVAC fan — %s", reason)

            self._fan_active = False
            self._fan_on_since = None
            # Issue #327: cancel the thermostatic backstop timer when fan deactivates.
            self._cancel_fan_thermo_backstop()
            self._record_action("Fan deactivated", reason)
            if emit_event and self._emit_event_callback:
                self._emit_event_callback(
                    "fan_deactivated",
                    {"reason": reason, "fan_mode": fan_mode, "fan_device": _fan_device_label(self.config)},
                )

            # Post-fan setpoint verify: Ecobee may revert to comfort program after a fan command.
            # Re-assert our setpoint within 30s so the coordinator's _is_recent_temp_command guard
            # covers any delayed state report.
            _verify_seq = self._write_seq
            _expected_temp = self._pending_setpoint_single
            _expected_mode = self._last_commanded_hvac_mode

            async def _do_verify_after_fan_off() -> None:
                if self._write_seq != _verify_seq:
                    return  # newer command issued — skip
                if _expected_temp is None or _expected_mode is None:
                    return  # no active setpoint
                if self._manual_override_active:
                    return  # genuine confirmed override — don't fight it
                current_state = self.hass.states.get(self.climate_entity)
                if current_state is None:
                    return
                actual = current_state.attributes.get("temperature")
                if actual is None:
                    return
                try:
                    if (
                        abs(float(actual) - _expected_temp) > 0.6
                    ):  # 0.6°F — same tolerance as _check_single_setpoint_accepted
                        _LOGGER.info(
                            "Post-fan setpoint verify: thermostat %.1f°F != expected %.1f°F — re-asserting %s mode",
                            float(actual),
                            _expected_temp,
                            _expected_mode,
                        )
                        await self._set_temperature(
                            _expected_temp, reason="post-fan-verify/repair", mode=_expected_mode
                        )
                except (ValueError, TypeError):
                    pass

            @callback
            def _verify_setpoint_after_fan_off(_now: Any) -> None:
                self.hass.async_create_task(_do_verify_after_fan_off())

            async_call_later(self.hass, 30.0, _verify_setpoint_after_fan_off)
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

        # Conditions for economizer eligibility.
        # Issue #327: added outdoor_temp < indoor_temp free-cooling-direction guard, mirroring
        # nat-vent's gate at check_natural_vent_conditions. Without this guard the economizer
        # could start the fan while it is warmer outside than in (e.g. on a hot evening), pulling
        # warmer outdoor air into the house and working against comfort.
        direction_ok = indoor_temp is None or outdoor_temp < indoor_temp
        if not direction_ok:
            _LOGGER.debug(
                "Economizer gate: direction rejected — outdoor %.1f°F >= indoor %.1f°F"
                " (free-cooling direction required)",
                outdoor_temp,
                indoor_temp if indoor_temp is not None else 0.0,
            )
        eligible = windows_physically_open and outdoor_temp <= comfort_cool + delta and in_window and direction_ok

        if not eligible:
            if self._economizer_active:
                await self._deactivate_economizer(outdoor_temp)
            return False

        # --- Economizer is eligible ---
        self._economizer_active = True

        if aggressive_savings:
            # Savings mode: rely purely on ventilation; band stays armed (compressor self-arbitrates)
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer (savings): ventilation only, outdoor=%s, band stays armed",
                    format_temp(outdoor_temp, unit),
                )
            return True

        # Comfort mode: two-phase strategy
        if indoor_temp is not None and indoor_temp > comfort_cool:
            # Phase 1: cool-down. Issue #264: the #249 comfort band already holds comfort_cool, so the
            # economizer no longer sets the HVAC mode/setpoint — doing so would flip the heat_cool band
            # to single `cool` and fight the band (two controllers). It now only assists with the fan,
            # pulling cool outdoor air through the open window to make the band's cooling more efficient.
            if self._economizer_phase != "cool-down":
                self._economizer_phase = "cool-down"
                await self._activate_fan(
                    reason=(
                        f"economizer cool-down — fan assists the band's cooling: indoor"
                        f" {format_temp(indoor_temp, unit)} > comfort {format_temp(comfort_cool, unit)},"
                        f" outdoor {format_temp(outdoor_temp, unit)} assisting"
                    )
                )
                _LOGGER.info(
                    "Economizer phase=cool-down: indoor=%s, outdoor=%s — band holds comfort_cool=%s, fan assists",
                    format_temp(indoor_temp, unit),
                    format_temp(outdoor_temp, unit),
                    format_temp(comfort_cool, unit),
                )
            return True
        else:
            # Phase 2: maintain — indoor at or below comfort; band stays armed (compressor
            # self-arbitrates — if ventilation is enough the compressor stays idle for free).
            if self._economizer_phase != "maintain":
                self._economizer_phase = "maintain"
                await self._activate_fan(reason="economizer maintain — fan assists ventilation")
                _LOGGER.info(
                    "Economizer phase=maintain: indoor=%s, band armed, ventilation holding",
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

    def _indoor_f_for_event(self) -> float | None:
        """Read current indoor temp from climate entity for event enrichment."""
        try:
            state = self.hass.states.get(self.climate_entity)
            if state is not None:
                return float(state.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
        return None

    def _get_thermostat_capabilities(self) -> ThermostatCapabilities:
        """Read the configured thermostat's advertised capabilities (Issue #249).

        Reads ``hvac_modes`` and ``supported_features`` from the climate entity's state and
        delegates to :func:`parse_thermostat_capabilities`. If the entity is missing or
        unavailable, returns all-False capabilities so callers degrade gracefully to their
        current behavior rather than assuming a band-capable thermostat.
        """
        state = self.hass.states.get(self.climate_entity)
        if state is None:
            return parse_thermostat_capabilities(None, None)
        attrs = getattr(state, "attributes", None) or {}
        return parse_thermostat_capabilities(attrs.get("hvac_modes"), attrs.get("supported_features"))

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore automation state from persisted data.

        Design decision: HA restart = clean slate for override, grace, pause, AND fan
        override state (Issue #327).
        - Manual overrides and grace periods are user-interactive; restoring them would
          silently suppress CA automation without the user knowing the system restarted.
        - Pause state (_paused_by_door / _pre_pause_mode) is also cleared: the
          door/window state-change listener re-detects any open sensors quickly after
          startup (None → "on" transition), re-pausing after the configured debounce
          (default 5 min). A brief HVAC re-arm is preferable to sitting paused
          indefinitely if cloud weather/thermostat services are slow to reconnect
          (Issue #263/#306).
        - Fan override state (_fan_override_active / _fan_override_time) is NOW cleared
          on restart (Issue #327): restoring it with no grace-timer reschedule left the
          fan in indefinite limbo — both _activate_fan and _deactivate_fan skipped forever.
          Restart reclaims fan control; reconcile_fan_on_startup() then decides whether to
          adopt (nat-vent) or turn the fan off.
        - _fan_active / _fan_on_since / _pre_fan_hvac_mode are still restored as hints
          for reconcile_fan_on_startup(); the coordinator's startup coalesce makes the
          final decision.
        - _natural_vent_active is NOT persisted and resets to False on restart; the
          reconcile step re-evaluates whether nat-vent conditions still hold.
        """
        # _paused_by_door and _pre_pause_mode are intentionally NOT restored here.
        # __init__ already sets both to their clean defaults (False / None).
        # The door/window listener re-detects open sensors on startup.
        self._economizer_active = state.get("economizer_active", False)
        self._economizer_phase = state.get("economizer_phase", "inactive")
        self._last_action_time = state.get("last_action_time")
        self._last_action_reason = state.get("last_action_reason")
        self._fan_active = state.get("fan_active", False)
        self._fan_on_since = state.get("fan_on_since")
        self._fan_min_runtime_active = state.get("fan_min_runtime_active", False)
        self._pre_fan_hvac_mode = state.get("pre_fan_hvac_mode")
        # _fan_min_cycle_cancel / _fan_thermo_cancel are not serializable; timers restart
        # fresh from coordinator startup / reconcile_fan_on_startup().
        last_notified = state.get("last_welcome_home_notified")
        if last_notified:
            try:
                self._last_welcome_home_notified = datetime.fromisoformat(last_notified)
            except (ValueError, TypeError):
                self._last_welcome_home_notified = None
        else:
            self._last_welcome_home_notified = None
        # Clean slate on restart: override, grace, and fan-override state are all cleared.
        # The user is back in front of a fresh system — carry-over would mean CA silently
        # blocks automation without any visible sign of an override.
        self._manual_override_active = False
        self._manual_override_mode = None
        self._manual_override_time = None
        self._override_confirm_pending = False
        self._override_confirm_time = None
        self._override_confirm_mode = None
        self._override_confirm_source = None
        self._grace_active = False
        self._grace_end_time = None
        self._grace_duration_seconds = None
        self._last_resume_source = None
        # Issue #327: fan override cleared on restart — no grace timer to reschedule.
        # reconcile_fan_on_startup() runs shortly after and decides adopt-on / turn-off.
        self._fan_override_active = False
        self._fan_override_time = None
        _LOGGER.info(
            "Fan override: restart clean-slate — _fan_override_active and _fan_override_time "
            "cleared (Issue #327); reconcile will decide fan disposition"
        )
        # Issue #295: pre-cool achievement gate — restored so a restart mid-day after
        # the home reached the pre-cool target does not re-arm the lower ceiling offset.
        self._pre_condition_achieved = state.get("pre_condition_achieved", False)
        self._pre_condition_achieved_date = state.get("pre_condition_achieved_date")
        _LOGGER.info(
            "Restored automation state: last_action=%s, fan_active=%s, fan_override=%s, "
            "precool_achieved=%s "
            "(override/grace/pause/fan-override state cleared — clean slate on restart per Issue #263/#327)",
            self._last_action_reason,
            self._fan_active,
            self._fan_override_active,
            self._pre_condition_achieved,
        )

    def get_serializable_state(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the engine's internal state.

        Override and grace state are intentionally omitted: they are always
        cleared on restore (clean-slate policy), so saving them provides no
        benefit and would only clutter the persisted JSON.
        """
        return {
            "paused_by_door": self._paused_by_door,
            "pre_pause_mode": self._pre_pause_mode,
            "dry_run": self.dry_run,
            "economizer_active": self._economizer_active,
            "economizer_phase": self._economizer_phase,
            "last_action_time": self._last_action_time,
            "last_action_reason": self._last_action_reason,
            "fan_active": self._fan_active,
            "fan_on_since": self._fan_on_since,
            "fan_override_active": self._fan_override_active,
            "fan_override_time": self._fan_override_time,
            "fan_min_runtime_active": self._fan_min_runtime_active,
            "pre_fan_hvac_mode": self._pre_fan_hvac_mode,
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
            # Issue #295: pre-cool achievement gate — persisted so an HA restart after
            # the home reached the pre-cool target does not re-arm the lower ceiling.
            "pre_condition_achieved": self._pre_condition_achieved,
            "pre_condition_achieved_date": self._pre_condition_achieved_date,
        }

    def cleanup(self) -> None:
        """Remove all active listeners and cancel pending timers."""
        self._cancel_grace_timers()
        self._stop_fan_min_runtime_cycles()
        self._cancel_fan_thermo_backstop()  # Issue #327
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None
        if self._override_confirm_cancel:
            self._override_confirm_cancel()
            self._override_confirm_cancel = None
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
