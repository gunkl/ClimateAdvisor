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
from enum import Enum
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .classifier import DayClassification
from .const import (
    CEILING_BRIDGE_TOLERANCE_F,
    CEILING_ESCALATION_SAVINGS_MARGIN_F,
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
    CONF_NAT_VENT_SOFT_START_ENABLED,
    CONF_NATURAL_VENT_DELTA,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SLEEP_COOL,
    CONF_SLEEP_HEAT,
    CONF_THRESHOLD_HOT,
    CONF_WELCOME_HOME_DEBOUNCE,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_FAN_MIN_RUNTIME_PER_HOUR,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_NAT_VENT_SOFT_START_ENABLED,
    DEFAULT_NATURAL_VENT_DELTA,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    DEFAULT_SLEEP_COOL,
    DEFAULT_SLEEP_HEAT,
    DEFAULT_THRESHOLD_HOT,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    ECONOMIZER_EVENING_END_HOUR,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_MORNING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
    FAN_MIN_TOGGLE_INTERVAL_S,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    GRACE_TRIGGERS_PROTECTING_OVERRIDE,
    HOT_DAY_PRE_COOL_MODIFIER,
    MIN_VIABLE_NAT_VENT_HOURS,
    NAT_VENT_HYSTERESIS_F,
    NAT_VENT_REACTIVATION_LOCKOUT_S,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
    OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
    PEAK_DECLINE_MARGIN_F,
    REVISIT_DELAY_SECONDS,
    TEMP_SOURCE_CLIMATE_FALLBACK,
    TIMER_BOUNDARY_SETTLE_SECONDS,
    VACATION_SETBACK_EXTRA,
)
from .desired_state import (
    FanCycleOutcome,
    ScheduledBandGate,
    SetpointRetryAction,
    decide_grace_start,
    decide_override_confirm,
    decide_revisit,
    decide_scheduled_band_gate,
    decide_scheduled_write_seq_current,
    decide_setpoint_retry_action,
)

if TYPE_CHECKING:
    from .classification_fsm import ClassificationDecision
    from .door_window_fsm import DoorWindowFsmEventKind
    from .economizer_fsm import EconomizerFsmInputs, EconomizerTransition
    from .fan_fsm import FanFsmEventKind, FanFsmInputs, FanTransition
    from .override_grace_fsm import OverrideGraceFsmEventKind

from .classification_fsm import (
    ClassificationFsmEvent,
    ClassificationFsmEventKind,
    ClassificationFsmInputs,
)
from .classification_fsm import transition as _classification_fsm_transition
from .door_window_lifecycle import (
    DoorWindowLifecycleInputs,
    DoorWindowLifecycleState,
    derive_door_window_lifecycle_state,
)
from .economizer_lifecycle import (
    EconomizerLifecycleInputs,
    EconomizerLifecycleState,
    derive_economizer_lifecycle_state,
)
from .fan_drift_reconciliation import FanDriftInputs, FanDriftOutcome, decide_fan_drift_reconciliation
from .fan_lifecycle import (
    FanCyclingState,
    FanLifecycleInputs,
    FanLifecycleState,
    FanOverrideState,
    FanPhysicalState,
    WhfHvacOwnership,
    derive_fan_lifecycle_state,
)
from .fan_thermostat_decision import (
    FanThermostatInputs,
    FanThermostatOutcome,
    _resolve_vent_floor,
    resolve_hard_exit_floor,
)
from .fan_toggle_rate_limit import (
    FanToggleRateLimitInputs,
    FanToggleRateLimitOutcome,
    decide_fan_toggle_rate_limit,
)
from .indoor_temp import resolve_indoor_temp_f
from .lifecycle_dispatcher import LifecycleDispatcher
from .lifecycle_events import LifecycleEvent, LifecycleEventType
from .nat_vent_cycling import NatVentCyclingInputs, compute_nat_vent_target, decide_nat_vent_cycling
from .nat_vent_exit import (
    NatVentExitInputs,
    NatVentExitReason,
    decide_nat_vent_exit,
)
from .nat_vent_gate import (
    NatVentGateInputs,
    NatVentSoftStartGateInputs,
    decide_nat_vent_gate,
    decide_nat_vent_soft_start_gate,
)
from .nat_vent_lifecycle import (
    NatVentLifecycleInputs,
    NatVentLifecycleState,
    derive_nat_vent_lifecycle_state,
)
from .nat_vent_reactivation_lockout import is_reactivation_locked_out
from .occupancy_fsm import (
    AwayVacationDecision,
    AwayVacationInputs,
    AwayVacationOutcome,
    HomeDecision,
    HomeInputs,
    HomeNotifyOutcome,
    decide_away_vacation_dispatch,
    decide_home_dispatch,
)
from .ode_ceiling_guard import OdeCeilingGuardOutcome
from .override_grace_lifecycle import (
    GraceState,
    OverrideConfirmState,
    OverrideGraceLifecycleState,
)
from .setpoint_verify_decision import SetpointVerifyOutcome, decide_setpoint_verify
from .temperature import (
    convert_delta,
    format_temp,
    format_temp_delta,
    from_fahrenheit,
    to_fahrenheit,
)
from .thermal_lead_time import compute_lead_minutes_from_rate

_LOGGER = logging.getLogger(__name__)


class FanCommandResult(Enum):
    """Outcome of an `_activate_fan()`/`_deactivate_fan()` call (Issue #649).

    Every early-return guard inside those two functions previously returned bare
    `None`, giving callers no way to tell "a real command was issued" apart from any
    of the several no-op reasons. That distinction matters once a command can be
    silently deferred by the Issue #641 rate limiter for up to 5 minutes: callers that
    build their own Activity Report event (the nat-vent exit branches) need it to avoid
    reporting a state transition that hasn't happened yet.
    """

    EXECUTED = "executed"
    ALREADY_IN_STATE = "already_in_state"
    RATE_LIMITED_NEW = "rate_limited_new"
    RATE_LIMITED_DUP = "rate_limited_dup"
    OVERRIDDEN = "overridden"
    DISABLED = "disabled"


# Issue #664: moved to const.py (GRACE_TRIGGERS_PROTECTING_OVERRIDE) as the single source of
# truth — override_grace_start.py's decide_grace_protects_override() previously hand-duplicated
# this same frozenset as its own default, with no import connecting the two.
_GRACE_TRIGGERS_PROTECTING_OVERRIDE = GRACE_TRIGGERS_PROTECTING_OVERRIDE


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
) -> ComfortBand:
    """Compute the comfort band for the current plan — pure, no HA state access.

    Derives ``[floor, ceiling]`` and the active edge from the classification, occupancy,
    sleep-window state, and savings posture. **No capability mapping** — that is the
    actuation primitive's job.

    Band logic:
    - vacation: deep setback on both edges; ``active="ceiling"`` (a cool-capable unit defends
      the wide ceiling, the dominant concern in an empty home).
    - away: standard setback on both edges; ``active="ceiling"`` for the same reason.
    - sleep: ``sleep_heat``/``sleep_cool`` band; active follows day type. Overnight pre-cool
      banking (below sleep_cool) is applied separately by ``handle_pre_cool()`` — see
      ``compute_pre_cool_target()`` — not by this function.
    - occupied + awake (home/guest), ANY day type: the "lazy" comfort band
      ``[comfort_heat, comfort_cool]`` — the thermostat pre-heats the morning to comfort_heat and
      cools the afternoon to comfort_cool. Suppression to a setback edge applies ONLY when
      away/asleep. ``active`` marks the day's dominant edge for single-mode devices (floor on a heat
      day, ceiling otherwise).
    ``aggressive_savings`` widens BOTH comfort edges by ``CEILING_ESCALATION_SAVINGS_MARGIN_F``
    (floor down, ceiling up) so the system runs less; setback/sleep bands are unaffected.
    """
    comfort_heat = float(config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
    comfort_cool = float(config.get("comfort_cool", DEFAULT_COMFORT_COOL))
    setback_heat = float(config.get("setback_heat", DEFAULT_SETBACK_HEAT))
    setback_cool = float(config.get("setback_cool", DEFAULT_SETBACK_COOL))
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
        ctx = "comfort"

    reason = (
        f"{ctx} band [{floor:.0f}/{ceiling:.0f}] (day={classification.day_type}, active={active}"
        f"{', aggressive' if aggressive_savings else ''})"
    )
    return ComfortBand(floor=floor, ceiling=ceiling, active=active, reason=reason)


def compute_pre_cool_target(config: dict, setback_modifier: float) -> float:
    """Compute the overnight pre-cool AC ceiling that banks cold thermal mass on a warming-trend
    night (Issue #258, floor formula revised — architecture-reset session).

    ``raw_target = sleep_cool + setback_modifier`` (modifier is negative on a warming trend, so
    this lowers the ceiling below the normal sleep target). The result is floored at
    ``sleep_heat + hysteresis`` — the same "+1 above the floor" convention
    ``nat_vent_temperature_check()`` already uses for sleep-window fan cycling — so pre-cool can
    travel the full ``[sleep_heat, sleep_cool]`` range instead of being clamped near daytime
    ``comfort_heat`` (the original formula's floor, which left little to no headroom once
    ``sleep_cool`` was reformatted to a flat, cooler-than-daytime default).

    This is the single source of truth for the pre-cool target — every one of its 5 call sites
    (the real AC trigger in ``handle_pre_cool()``, trigger-time scheduling, the chart's target-band
    dip, and the ODE predicted-indoor curve) must call this, not re-derive the formula, so the
    chart and the control path can never diverge (Issue #436).
    """
    sleep_cool = float(config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL))
    sleep_heat = float(config.get(CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT))
    hysteresis = float(config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
    raw_target = sleep_cool + setback_modifier
    floor = sleep_heat + hysteresis
    return max(raw_target, floor)


def resolve_pre_cool_modifier(classification: DayClassification, config: dict) -> float | None:
    """Decide whether overnight pre-cool should run tonight, and with what modifier (Issue #558).

    Returns ``None`` when neither gate is satisfied (no pre-cool tonight). Otherwise returns the
    modifier to pass to ``compute_pre_cool_target()``:

    - If tonight qualifies via a warming trend (``setback_modifier < 0``, set by the classifier
      on a significant/moderate warming trend), that modifier is used as-is — unchanged from the
      original Issue #258 behavior.
    - Else, if tomorrow is independently forecast to be a hot day (``tomorrow_high >=
      threshold_hot``), a flat fallback of ``HOT_DAY_PRE_COOL_MODIFIER`` is used instead. Without
      this fallback, a plateaued stretch of hot days (each day's forecast high roughly matching
      the last, so ``setback_modifier`` stays 0) would get zero overnight banking beyond the flat
      ``sleep_cool`` floor, indefinitely — this closes that gap using the same nightly, patient
      mechanism rather than reintroducing a daytime catch-up.

    This is the single source of truth for "should/how should tonight pre-cool" — call sites in
    ``handle_pre_cool()``, the trigger-time scheduler, and the briefing narrative must all use
    this rather than re-deriving the gate condition. Defensive against loosely-typed test doubles
    (e.g. a ``MagicMock`` classification with no explicit ``setback_modifier``/``tomorrow_high``
    set) — treated as "not eligible" rather than raising.
    """
    _setback_mod = getattr(classification, "setback_modifier", None)
    if isinstance(_setback_mod, (int, float)) and _setback_mod < 0:
        return _setback_mod
    _tomorrow_high = getattr(classification, "tomorrow_high", None)
    if isinstance(_tomorrow_high, (int, float)):
        threshold_hot = float(config.get(CONF_THRESHOLD_HOT, DEFAULT_THRESHOLD_HOT))
        if _tomorrow_high >= threshold_hot:
            return HOT_DAY_PRE_COOL_MODIFIER
    return None


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


def should_defer_to_occupancy_setback(occupancy_mode: str) -> bool:
    """Return True if `occupancy_mode` means comfort/setback logic should defer to
    the away/vacation setback handlers instead of running normally (Issue #460).

    Single source of truth for a gate previously phrased 3 different (but
    logically equivalent) ways across this module's setpoint paths — the same
    "sibling formulation drift" risk as #400/#402/#417/#456/#458. Equivalence
    relies on occupancy_mode always being one of exactly the 4 values in
    const.py (OCCUPANCY_HOME, OCCUPANCY_AWAY, OCCUPANCY_VACATION, OCCUPANCY_GUEST):
    `occupancy_mode in (OCCUPANCY_AWAY, OCCUPANCY_VACATION)` is equivalent to
    `occupancy_mode not in (OCCUPANCY_HOME, OCCUPANCY_GUEST)`, the inverted form
    handle_morning_wakeup() used.
    """
    return occupancy_mode in (OCCUPANCY_AWAY, OCCUPANCY_VACATION)


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
        comfort = config.get("comfort_heat", DEFAULT_COMFORT_HEAT)
        floor = config.get("setback_heat", DEFAULT_SETBACK_HEAT)
        rate = (thermal_model or {}).get("heating_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_F
        # Explicit sleep temp takes priority over adaptive calculation
        _explicit = config.get(CONF_SLEEP_HEAT)
        if _explicit is not None:
            return max(float(_explicit), floor)
    elif hvac_mode == "cool":
        comfort = config.get("comfort_cool", DEFAULT_COMFORT_COOL)
        floor = config.get("setback_cool", DEFAULT_SETBACK_COOL)
        rate = (thermal_model or {}).get("cooling_rate_f_per_hour")
        default_depth = DEFAULT_SETBACK_DEPTH_COOL_F
        # Explicit sleep temp takes priority over adaptive calculation
        # Note: warming-trend mid-night adjustment is handled separately by handle_pre_cool(),
        # not here. compute_bedtime_setback() always returns the raw configured sleep temp.
        _explicit = config.get(CONF_SLEEP_COOL)
        if _explicit is not None:
            return min(float(_explicit), floor)
    else:
        return config.get("comfort_heat", DEFAULT_COMFORT_HEAT)

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


@dataclass
class AutomationEngineCallbacks:
    """Bundle of the 9 callbacks the coordinator wires onto an AutomationEngine.

    Issue #604 (Block 5, subtask N2): named-bundle form of the callback wiring the
    coordinator has always done post-construction, so a future second ("shadow")
    engine instance can be given its own dedicated set of callables instead of
    reusing the production coordinator's real bound methods. Passing this bundle
    doesn't change behavior for the existing single (production) engine — it is
    the same 9 attributes, assigned in one step instead of nine.
    """

    revisit: Callable[[], Any] | None = None
    sensor_check: Callable[[], bool] | None = None
    sensor_debounce_pending: Callable[[], bool] | None = None
    emit_event: Callable[..., None] | None = None
    request_refresh: Callable[[], None] | None = None
    post_grace_fan_check: Callable[[], None] | None = None
    get_fan_physical_state: Callable[[], Any] | None = None
    is_recent_fan_command: Callable[[], bool] | None = None
    reclassify: Callable[[], None] | None = None


class AutomationEngine:
    """Manages HVAC automations based on daily classification."""

    # Issue #729: class-level default so a test fixture that partially constructs an
    # engine via object.__new__(AutomationEngine) (skipping __init__ — an established
    # pattern across many existing tests, see CLAUDE.md) still has a value for the
    # role= tag now interpolated into the real-command log lines, instead of an
    # AttributeError. Normal construction always overwrites this with an instance
    # attribute in __init__ below.
    role: str = "production"

    def __init__(
        self,
        hass: HomeAssistant,
        climate_entity: str,
        weather_entity: str,
        door_window_sensors: list[str],
        notify_service: str,
        config: dict[str, Any],
        sensor_polarity_inverted: bool = False,
        *,
        callbacks: AutomationEngineCallbacks | None = None,
        role: str = "production",
    ) -> None:
        """Initialize the automation engine."""
        self.hass = hass
        self.climate_entity = climate_entity
        self.weather_entity = weather_entity
        self.door_window_sensors = door_window_sensors
        self.notify_service = notify_service
        self.config = config
        self.sensor_polarity_inverted = sensor_polarity_inverted
        # Issue #604: label only, for logging/future observability — never branched on
        # inside this class or the 9 coordinator callback methods. Isolation between a
        # production and a future shadow engine is structural (which callables get wired
        # in, via AutomationEngineCallbacks), not a runtime role check.
        self.role = role
        self._active_listeners: list[Any] = []
        self._current_classification: DayClassification | None = None
        self._paused_by_door = False
        self._pre_pause_mode: str | None = None
        # Issue #523: distinguishes "_paused_by_door=True with HVAC already off" (nothing
        # was interrupted, no grace/resume timer exists) from a genuine mode-change pause —
        # check_natural_vent_conditions()'s idle-open re-evaluation loop (Issue #244/#402/
        # #504) must keep running every cycle in the former case, since nothing else will
        # ever re-trigger it otherwise. Only _pause_for_door_window() sets this True.
        self._paused_with_hvac_already_off = False
        # Issue #592: which entity triggered the current door/window pause, and when it
        # started — threaded into classification_suppressed_paused so the Activity Record
        # can say *which* sensor and *how long*, not just that a pause is in effect.
        self._paused_entity: str | None = None
        self._paused_since: datetime | None = None

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
        # Issue #625: the `trigger` string _start_grace_period() already computes was
        # previously used only for logging/event-payload correlation and thrown away
        # otherwise. Retaining it lets the Status card show a short cause label (e.g.
        # "WHF override", "thermostat override") without re-narrating a full sentence.
        self._last_grace_trigger: str | None = None
        self._grace_end_time: str | None = None
        self._grace_duration_seconds: int = 0
        # Issue #530: whether the CURRENTLY active grace exists to protect a real override
        # (see _GRACE_TRIGGERS_PROTECTING_OVERRIDE) — set fresh by _start_grace_period() on
        # every start, read by coordinator._check_orphaned_grace().
        self._grace_protects_override: bool = False

        # Comfort-band event dedup (Issue #444): tracks the last-announced band so
        # overlapping triggers (startup coalesce + its own refresh, grace-expiry
        # re-application) don't each re-announce an identical band as a fresh event.
        self._last_comfort_band_signature: tuple[str, str, float] | None = None
        self._last_comfort_band_event_at: datetime | None = None

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
        # Issue #483: carries the confirming _override_confirm_source ("normal"/"pause"/
        # "setpoint") forward onto the *active* override so the adopt-on-match check can
        # exclude setpoint-only overrides (mode matching classification is not sufficient
        # evidence of convergence when the user's real intent was a setpoint change).
        self._manual_override_source: str | None = None

        # Fan state tracking (Issue #37)
        self._fan_active: bool = False
        self._fan_on_since: str | None = None  # ISO timestamp
        self._fan_override_active: bool = False
        self._fan_override_time: str | None = None
        # RF remote timer selection in hours, for observability only (Issue #486).
        # None when the active override wasn't started by a remote timer, or the
        # remote selected "no timer" (falls back to configured manual_grace_seconds).
        self._fan_remote_timer_hours: float | None = None
        # RF remote speed selection, for observability only (Issue #519). Set by both
        # handle_fan_manual_override() (an override-classified speed press) and
        # handle_fan_speed_observed() (a comfort-only speed press) — either way, this is
        # "the last speed the remote reported," not an indicator of which path set it.
        self._fan_remote_speed: str | None = None
        self._fan_command_pending: bool = False  # transient: distinguishes integration vs manual changes
        # Issue #530: when a manual grace tied to an RF-remote timer expires, the timer's
        # physical hardware side typically finishes within seconds of CA's software clock —
        # not always before. This deadline marks a short coalescing window during which a
        # fan-off report is treated as the tail of that SAME timer boundary (not a fresh,
        # unexpected event) — see on_fan_turned_off() and _on_grace_expired().
        self._timer_boundary_settle_until: datetime | None = None
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
        # Issue #641: last time _activate_fan()/_deactivate_fan() issued a REAL fan
        # state-changing command — deliberately separate from _fan_command_time above.
        # _fan_command_time is also stamped by bookkeeping-only echo-tracking commands
        # that don't represent a real physical toggle (e.g. _reconcile_fan_physical_drift()'s
        # corrective "sync the stuck control entity" off-command, Issue #449/#482 — the
        # physical fan was already off, only the entity's stale belief changes), and that
        # method's docstring explicitly documents an immediately-following same-tick
        # recycle-on as correct, expected behavior. Rate-limiting against the shared field
        # would misfire on that documented sequence. Only _activate_fan/_deactivate_fan's
        # own command sites touch this field.
        self._fan_toggle_command_time: datetime | None = None
        # Issue #641: set whenever a fan toggle is suppressed by the rate-limit backstop
        # (_fan_toggle_rate_limited) — the timestamp the suppression will lift. None when
        # no toggle is currently suppressed. Status-tab surfacing only, not read by any
        # decision path — mirrors the pattern of other diagnostic-only fields.
        self._fan_rate_limited_until: datetime | None = None
        # Issue #649: the direction ("activate"/"deactivate") the pending deferral above
        # is for — lets the status card and dedup guard describe what's actually pending
        # instead of just "something is rate-limited". None whenever
        # _fan_rate_limited_until is None.
        self._fan_rate_limited_direction: str | None = None
        # Issue #482: id of the HA Context CA attached to its most recent outgoing WHF
        # fan-entity service call (see _command_whf_control_entity). When the resulting
        # state-changed Event's own event.context.id (or parent_id) matches this value,
        # that is an authoritative "this transition was CA-issued" signal — used by
        # coordinator._async_fan_entity_changed() as an additional provenance check
        # alongside the existing _fan_command_pending/timing-heuristic guards. Real HA
        # context propagation through third-party fan/switch integrations (especially
        # one-way RF transmitter entities) is not guaranteed reliable, so this is treated
        # as a corroborating signal, not a replacement for the existing checks.
        # Issue #561: bounded recency list, not a single last-write-wins id — see
        # _record_fan_command_context()'s docstring for why a scalar isn't safe.
        self._recent_fan_command_context_ids: list[tuple[str, datetime]] = []
        self._last_commanded_hvac_mode: str | None = None  # expected-state tracking: last mode automation commanded
        self._last_commanded_hvac_time: datetime | None = None  # expected-state tracking: when it was commanded

        # Natural ventilation mode (Issue #73)
        self._natural_vent_active: bool = False
        self._last_outdoor_temp: float | None = None
        # Reentrancy guard for reconcile_fan_on_startup() (Issue #561): that method is
        # called from 4 independent sites (startup coalesce, 30-min untracked-fan backstop,
        # thermostat state-change, post-grace-expiry) with no coordination between them.
        # Two overlapping calls previously each independently "adopted" the same physically-
        # running fan and each started their own self-rescheduling backstop timer
        # (_start_fan_thermo_backstop() only tracks the single most-recent one via
        # self._fan_thermo_cancel), leaving the earlier one permanently uncancellable and
        # ticking forever in parallel. A plain bool (not the shared self._decision_lock,
        # which is not reentrant and some call sites may already hold) is enough: a second
        # concurrent call simply skips this tick rather than blocking.
        self._reconcile_fan_in_progress: bool = False
        # Timestamp of last outdoor-warm exit (outdoor ≥ indoor → pause).
        # Used for hysteresis lockout. Not serialized — resets on HA restart (acceptable for 5-min window).
        self._nat_vent_outdoor_exit_time: datetime | None = None

        # Nat-vent soft-start sub-mode (Issue #540, scoped from #533): qualifies WHY an
        # active nat-vent session was entered — True when entered via the parity/
        # past-peak soft-start gate rather than the full bulk-cooling gate. Coexists with
        # _natural_vent_active the same way _grace_protects_override coexists with
        # _grace_active: same top-level state, a distinct qualifying sub-flag. Cleared
        # alongside every _natural_vent_active = False assignment (see _exit_nat_vent()
        # and its documented bypass sites).
        self._nat_vent_soft_start: bool = False
        # Today's observed outdoor peak-so-far and sample count, mirrored from the
        # coordinator's _outdoor_temp_history (single source of truth — see
        # coordinator._apply_outdoor_temp()). Not serialized — rebuilt from the
        # coordinator's own persisted/restored history within one update cycle.
        self._outdoor_temp_today_peak: float | None = None
        self._outdoor_temp_today_sample_count: int = 0
        # Issue #757 Phase 6 Step 5 correction: the nat-vent lifecycle FSM
        # (nat_vent_fsm.py, Issue #594 Phase R) is now unconditionally authoritative
        # for all 10 real nat-vent trigger sites — _natvent_fsm_authoritative has
        # been removed. It was permanently True in production for weeks and proven
        # behavior-equivalent via an offline differential comparator (zero divergence
        # across the golden+pending corpus) before the legacy inline branches were
        # deleted.
        # Issue #757 Phase 6 Step 4 correction: the door/window lifecycle FSM
        # (door_window_fsm.py, Issue #660) is now unconditionally authoritative for
        # all 8 real door/window trigger sites via the shared
        # AutomationEngine._resolve_door_window_pause_flags() dispatcher —
        # _doorwindow_fsm_authoritative (Issue #594 Phase R, Step 8) has been removed.
        # It was permanently True in production for weeks and proven behavior-equivalent
        # via an offline differential comparator (zero divergence across the
        # golden+pending corpus) before the legacy inline flag-write branch was deleted.

        # Issue #757 Phase 6 Step 3 correction: the override/grace lifecycle FSM
        # (override_grace_fsm.py, Issue #639) is now unconditionally authoritative for
        # _override_confirm_pending/_grace_active/_grace_protects_override —
        # _override_grace_fsm_authoritative (Issue #664) has been removed. It was
        # permanently True in production for weeks and proven behavior-equivalent via
        # an offline differential comparator (zero divergence across the golden+pending
        # corpus) before the legacy inline flag-write branch was deleted.

        # Issue #757 Phase 6 Step 7 (final subsystem — graduation complete): the
        # classification decision FSM (classification_fsm.py, Issue #742) is now
        # unconditionally authoritative for apply_classification()'s ODE ceiling
        # guard block — _classification_fsm_authoritative has been removed. It was
        # permanently True in production for weeks and proven behavior-equivalent via
        # an offline differential comparator (zero divergence across the golden+pending
        # corpus) before the legacy inline ~190-line eligibility/dormancy/breach-scan/
        # lead-time computation was deleted. Like occupancy_fsm.py, classification_fsm.py
        # is deliberately STATELESS (see its own module docstring's five-whys) — there
        # was never a persisted lifecycle this graduation touches, only which code path
        # computed the ceiling-guard decision each cycle. This is the last of the 5
        # ``_*_fsm_authoritative`` flags to graduate — as of this step, neither engine
        # instance carries any such flag anymore.

        # Issue #757 Phase 6 Step 6 correction: the occupancy dispatch FSM
        # (occupancy_fsm.py, Issue #744) is now unconditionally authoritative for
        # handle_occupancy_away()/handle_occupancy_home()/handle_occupancy_vacation()'s
        # branch structure — _occupancy_fsm_authoritative has been removed. It was
        # permanently True in production for weeks and proven behavior-equivalent via
        # an offline differential comparator (zero divergence across the golden+pending
        # corpus) before the legacy inline branches were deleted. Like
        # classification_fsm.py, occupancy_fsm.py is deliberately STATELESS (see its
        # own module docstring's five-whys) — self._occupancy_mode is data (set by
        # set_occupancy_mode()), not a persisted lifecycle this graduation touches.

        # Issue #746 (strangler-fig completion program, Phase 5 — the final
        # subsystem extraction): whether the economizer lifecycle FSM
        # Issue #717 (Block 5, epic #594): wire the previously-dormant
        # lifecycle_dispatcher.py pub/sub router (Issue #633) into production so the
        # three lifecycle FSMs stop cross-reading each other's raw booleans directly.
        # Each AutomationEngine instance owns its own dispatcher — never shared with
        # any diagnostic/shadow-only dispatcher — following the same structural
        # isolation precedent as AutomationEngineCallbacks (Issue #604): nothing
        # should be able to let a shadow-side consumer register on the registry
        # production writes into. This engine registers as the sole controller of
        # all six real event types today (it still owns all three lifecycles'
        # state), so this is currently a same-instance emit/consume round-trip —
        # the seam that lets a future genuinely separate controller take over one
        # lifecycle without every consumer changing, not a behavior change today.
        self._lifecycle_dispatcher = LifecycleDispatcher()
        # Dispatcher-synced mirrors — populated ONLY by _on_lifecycle_event(), never
        # written directly elsewhere. Observability/diagnostics only: an earlier
        # version of this change also routed _build_nat_vent_fsm_inputs()/
        # _build_door_window_fsm_inputs() through these instead of the canonical
        # _paused_by_door/_grace_active/_manual_override_active/_natural_vent_active
        # attributes, on the theory that the cross-read should "genuinely flow
        # through the dispatcher." That was the wrong design for a same-instance
        # emit/consume round-trip: this engine both emits and consumes every event
        # today, so the canonical attributes can never actually go stale relative to
        # a same-object mirror the way a genuine cross-instance mirror could (cf.
        # coordinator.py's _sync_shadow_inputs(), which exists because production and
        # shadow ARE separate instances). Routing the FSM builders through a
        # dispatcher-only mirror broke the established direct-attribute-assignment
        # test-fixture convention used across 40+ existing test files, for no real
        # safety benefit — reverted. The FSM builders read the canonical attributes;
        # these mirrors exist so a test (or a future diagnostic) can assert the
        # dispatcher's own round-trip actually works, independent of production's
        # real decision inputs.
        self._dispatched_paused_by_door: bool = False
        self._dispatched_grace_active: bool = False
        self._dispatched_manual_override_active: bool = False
        self._dispatched_natural_vent_active: bool = False
        # Issue #722: reintroduced now that the write-site gap is fixed. The prior
        # exclusion (see the removed comment this replaces) objected to deriving
        # this from the NAT_VENT_SESSION_* diff — the wrong signal. This mirror is
        # instead sourced from _pre_fan_hvac_mode's own before/after diff, via the
        # new _resolve_whf_hvac_suppression() chokepoint covering all 4 real
        # writers of that field (_suppress_hvac_for_whf(), _release_whf_and_
        # reclassify(), and _deactivate_fan()'s two release branches — the latter
        # two were not named in #722's original text; found during investigation).
        self._dispatched_whf_owns_hvac: bool = False
        self._lifecycle_dispatcher.register(
            "automation_engine",
            emits=frozenset(
                {
                    LifecycleEventType.DOOR_PAUSE_STARTED,
                    LifecycleEventType.DOOR_PAUSE_ENDED,
                    LifecycleEventType.GRACE_STARTED,
                    LifecycleEventType.GRACE_ENDED,
                    LifecycleEventType.OVERRIDE_CONFIRMED,
                    LifecycleEventType.OVERRIDE_CLEARED,
                    LifecycleEventType.NAT_VENT_SESSION_STARTED,
                    LifecycleEventType.NAT_VENT_SESSION_ENDED,
                    LifecycleEventType.WHF_HVAC_SUPPRESSED,
                    LifecycleEventType.WHF_HVAC_RELEASED,
                }
            ),
            consumes=frozenset(
                {
                    LifecycleEventType.DOOR_PAUSE_STARTED,
                    LifecycleEventType.DOOR_PAUSE_ENDED,
                    LifecycleEventType.GRACE_STARTED,
                    LifecycleEventType.GRACE_ENDED,
                    LifecycleEventType.OVERRIDE_CONFIRMED,
                    LifecycleEventType.OVERRIDE_CLEARED,
                    LifecycleEventType.NAT_VENT_SESSION_STARTED,
                    LifecycleEventType.NAT_VENT_SESSION_ENDED,
                    LifecycleEventType.WHF_HVAC_SUPPRESSED,
                    LifecycleEventType.WHF_HVAC_RELEASED,
                }
            ),
            on_event=self._on_lifecycle_event,
        )

        # Override confirmation period (Issue #76) — pending window before override is formally accepted
        self._override_confirm_pending: bool = False
        self._override_confirm_cancel: Any | None = None
        self._override_confirm_time: str | None = None
        self._override_confirm_mode: str | None = None
        self._override_confirm_source: str | None = None  # "setpoint" or "normal"

        # Issue #729: cancel handles for the 3 timer chains inside _set_temperature()'s
        # rejection-handling path (_schedule_check -> _schedule_retry ->
        # _schedule_real_target) and the two post-fan setpoint-verify timers — found
        # during the reload-based-promotion redesign to be scheduled via
        # async_call_later() but never tracked anywhere, so cleanup() had no way to
        # reach them. One shared attribute for the 3-stage chain (only one stage is
        # ever outstanding at a time — each stage replaces the prior); two separate
        # attributes for the fan-on/fan-off verify timers since those run in
        # independent contexts and could in principle overlap.
        self._setpoint_retry_cancel: Any | None = None
        self._fan_on_verify_cancel: Any | None = None
        self._fan_off_verify_cancel: Any | None = None

        # Minimum fan runtime per hour — rolling cycle (Issue #77)
        self._fan_min_runtime_active: bool = False  # True if THIS feature activated the fan
        self._fan_min_cycle_cancel: Any | None = None  # cancel token for pending on/off timer

        # Thermostatic fan backstop timer (Issue #327): self-rescheduling timer started in
        # _activate_fan, cancelled in _deactivate_fan + cleanup. Ensures fan_thermostat_check
        # fires even when temperature sensors update slowly.
        self._fan_thermo_cancel: Any | None = None
        # Generation counter (Issue #561, defense-in-depth): self._fan_thermo_cancel can only
        # ever hold one live cancel handle, so if two chains are ever started concurrently
        # (e.g. a future reentrancy gap elsewhere), whichever started first becomes
        # permanently uncancellable the moment the second starts, and both tick forever in
        # parallel. Each _start_fan_thermo_backstop() call bumps this counter and stamps its
        # own tick with the value at schedule time; a tick whose stamped generation no longer
        # matches the current counter belongs to a superseded chain and self-terminates
        # instead of rescheduling, so at most one chain survives past its next tick.
        self._fan_thermo_generation: int = 0

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

        # Reclassify callback — called by _release_whf_and_reclassify() when a manual/remote
        # WHF session ends, so the coordinator re-runs apply_classification() with its own
        # current classification/predicted-indoor state (Issue #495). AutomationEngine has no
        # direct handle on that coordinator-owned state, so this mirrors the existing
        # callback-injection pattern above rather than reaching into the coordinator directly.
        # Set by coordinator after construction.
        self._reclassify_callback: Callable[[], None] | None = None

        # Today's DailyRecord — set by coordinator; used for bedtime setback tracking
        self._today_record: Any | None = None

        # Issue #96: classification event dedup — track last emitted (day_type, hvac_mode) pair
        self._last_classification_applied: tuple[str, str] | None = None
        # Issue #96: override event dedup — track last emission time
        self._last_override_detected_time: datetime | None = None
        # Issue #446: unwarranted-fan reconcile-correction dedup — track last correction time
        self._last_unwarranted_fan_correction_at: datetime | None = None

        # Resume-from-pause tracking (Issue #47)
        self._resumed_from_pause: bool = False
        self._sensor_check_callback: Any | None = None  # Set by coordinator: returns True if any sensor open
        # Issue #504: Set by coordinator: returns True if any currently-open monitored sensor
        # is still within its CONF_SENSOR_DEBOUNCE settle window. None/unset (e.g. tests that
        # don't wire a coordinator) is treated as "not pending", matching pre-#504 behavior.
        self._sensor_debounce_pending_callback: Any | None = None

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

        # Issue #604: seed the 9 callback attributes from the bundle, if one was given.
        # Omitted (None) → every _x_callback attribute stays None, exactly as before this
        # dataclass existed. Applied last so it can't be shadowed by any of the plain
        # `self._x_callback: Any | None = None` assignments above.
        if callbacks is not None:
            self.set_callbacks(callbacks)

    def set_callbacks(self, callbacks: AutomationEngineCallbacks) -> None:
        """(Re-)wire the 9 coordinator callback bundle onto this engine.

        Extracted from ``__init__``'s original inline unpacking (Issue #604) so
        it can also be called after construction — Issue #727's shadow-engine-
        primary promotion swaps a production and shadow engine's callback
        bundles (production's real callbacks must follow whichever physical
        engine is currently primary; the demoted engine gets the isolated
        shadow bundle instead), without duplicating this unpacking logic.
        """
        self._revisit_callback = callbacks.revisit
        self._sensor_check_callback = callbacks.sensor_check
        self._sensor_debounce_pending_callback = callbacks.sensor_debounce_pending
        self._emit_event_callback = callbacks.emit_event
        self._request_refresh_callback = callbacks.request_refresh
        self._post_grace_fan_check_callback = callbacks.post_grace_fan_check
        self._get_fan_physical_state_callback = callbacks.get_fan_physical_state
        self._is_recent_fan_command_callback = callbacks.is_recent_fan_command
        self._reclassify_callback = callbacks.reclassify

    async def _notify(self, message: str, title: str, notification_type: str) -> None:
        """Send a notification via configured channels, filtered by per-event preferences."""
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would send notification: %s — %s role=%s", title, message, self.role)
            return
        push_key = f"push_{notification_type}"
        email_key = f"email_{notification_type}"
        service_name = self.notify_service.split(".")[-1] if "." in self.notify_service else self.notify_service
        if self.config.get(push_key, True):
            await self.hass.services.async_call("notify", service_name, {"message": message, "title": title})
            _LOGGER.info("Notification sent: %s — %s role=%s", title, message, self.role)
        if self.config.get(email_key, True):
            await self.hass.services.async_call("notify", "send_email", {"message": message, "title": title})
            _LOGGER.info("Email notification sent: %s — %s role=%s", title, message, self.role)

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

        Issue #629 investigation note: condition #2 was considered for a live-thermostat-mode
        check instead of ``classification.hvac_mode``, but that would invert the deliberate
        Issue #51/#53 design — this exemption is intentionally keyed on the day's *recommended*
        mode so a thermostat left in an active mode (manual override, or any other reason)
        during a windows-recommended period doesn't get fought by the door-pause guard. Left
        as-is; Issue #629's actual fix is the independent choke-point guard in
        ``_apply_comfort_band()`` below, which stops CA's own comfort-band arm from writing an
        active mode through open windows in the first place — it does not rely on this
        function's behavior.
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
        _LOGGER.info("Action recorded: %s", self._last_action_reason)
        self._schedule_revisit()

    def _schedule_revisit(self) -> None:
        """Schedule a follow-up re-evaluation after an HVAC action.

        Architecture-reset Step 2 (session state machine slice): the
        decision (should a revisit be scheduled, and when) now lives in
        ``desired_state.decide_revisit()``. This method still owns cancelling
        any prior timer and scheduling the real ``async_call_later``.
        """
        if self._revisit_cancel:
            self._revisit_cancel()
            self._revisit_cancel = None

        revisit = decide_revisit(
            has_revisit_callback=bool(self._revisit_callback),
            delay_seconds=REVISIT_DELAY_SECONDS,
            now=dt_util.now(),
        )
        if revisit is None:
            return

        revisit_cb = self._revisit_callback

        @callback
        def _revisit_fired(_now: Any) -> None:
            self._revisit_cancel = None
            _LOGGER.info("Revisit check triggered (5-min follow-up after action)")
            self.hass.async_create_task(revisit_cb())

        self._revisit_cancel = async_call_later(self.hass, REVISIT_DELAY_SECONDS, _revisit_fired)

    def _legacy_clear_confirm_flag(self) -> None:
        """The 1-line flag-clear ``clear_manual_override()`` always used to perform
        inline (Issue #664). Issue #757 Phase 6 Step 3: no longer composed into a
        ``legacy`` closure for ``_resolve_override_grace_fsm_state()`` — that dispatcher
        is now unconditionally FSM-authoritative and never calls this method. Still used
        directly, unchanged, by ``clear_manual_override()`` for every caller whose event
        isn't override/grace FSM-modeled (``handle_occupancy_away/vacation``,
        ``handle_bedtime``, ``handle_morning_wakeup``, the stuck-grace-recovery
        watchdog) — kept as its own method (rather than inlined) for that reuse.
        """
        self._override_confirm_pending = False

    def _clear_override_confirm_action(self) -> None:
        """Real side effect only: cancel the confirm-expiry timer handle (if any) and
        clear the non-FSM-derived confirm bookkeeping (Issue #664). Deliberately does
        NOT write ``_override_confirm_pending`` — see ``_legacy_clear_confirm_flag()``.
        """
        if self._override_confirm_cancel:
            self._override_confirm_cancel()
            self._override_confirm_cancel = None
        self._override_confirm_time = None
        self._override_confirm_mode = None
        self._override_confirm_source = None

    def clear_manual_override(self, reason: str = "grace_expired") -> None:
        """Clear the manual override flag (called at transition points).

        Issue #664: the confirm-clear half is a thin wrapper over
        ``_clear_override_confirm_action()`` + ``_legacy_clear_confirm_flag()`` —
        real work / flag-write split, same shape as ``_start_grace_period()``'s split.
        Used unconditionally by every caller except the 3 real override/grace call
        sites named in ``_legacy_clear_confirm_flag()``'s docstring, which call the
        action directly and route the flag-write through the dispatcher instead,
        atomically alongside the grace flags for the SAME event (see
        ``cancel_override()``).
        """
        if self._override_confirm_pending:
            self._clear_override_confirm_action()
            self._legacy_clear_confirm_flag()
        self._clear_manual_override_active(reason)

    def _clear_manual_override_active(self, reason: str) -> None:
        """The ``_manual_override_active``-and-below half of ``clear_manual_override()``
        (Issue #664, extracted for reuse). None of these fields are part of the
        override/grace FSM's ``(OverrideConfirmState, GraceState)`` 2-tuple derivation
        (same exclusion list ``_apply_override_grace_fsm_state()``'s docstring
        documents) — always runs directly, unconditionally, regardless of which
        computation determined the confirm/grace flags for this event.
        """
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
            self._manual_override_source = None
            self._manual_override_time = None
            # Issue #717: single real site, reusing this method's own existing
            # idempotent "did it actually change" guard rather than duplicating it.
            try:
                self._lifecycle_dispatcher.emit(
                    LifecycleEvent(
                        event_type=LifecycleEventType.OVERRIDE_CLEARED,
                        source="automation_engine",
                        at=dt_util.now(),
                        detail=reason,
                    )
                )
            except Exception:  # noqa: BLE001 — a dispatcher bug must never affect the real clear
                _LOGGER.exception("_clear_manual_override_active: lifecycle event emit failed (isolated)")
        self._resumed_from_pause = False
        self.clear_fan_override()

    def cancel_override(self, reason: str = "user_cancel") -> bool:
        """User/system deliberately ends an override and its grace protection right now (Issue #508).

        Mirrors the post-processing ``_on_grace_expired()`` performs at natural expiry (fan
        reconcile, coordinator refresh) but on-demand instead of waiting for the grace timer.
        ``clear_manual_override()`` unconditionally clears the fan-override flag too, so this one
        method is the entry point for both dashboard "Cancel..." buttons — there is no separate
        "kind" of cancellation, just one operation with two entry points.

        Issue #664: the confirm/grace flag write is a SINGLE atomic dispatch call for the
        OVERRIDE_CANCELLED event (not two separate calls into ``clear_manual_override()``'s
        and ``_cancel_grace_timers()``'s own dispatch — the FSM's ``transition()`` computes
        both flags from one ``(confirm, grace)`` state pair per event, so this must be one
        dispatcher call, not two racing against each other's ``origin_state`` read).

        Returns:
            True if an override and/or grace period was actually cancelled; False if there was
            nothing active (safe no-op — callers can use this to choose their response message).
        """
        had_manual = self._manual_override_active
        had_fan = self._fan_override_active
        had_grace = self._grace_active
        if not (had_manual or had_fan or had_grace):
            return False

        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        if self._override_confirm_pending:
            self._clear_override_confirm_action()
        self._cancel_grace_timers_action()

        self._resolve_override_grace_fsm_state(kind=_OGFEventKind.OVERRIDE_CANCELLED)
        self._clear_manual_override_active(reason)

        # clear_manual_override() only emits "override_cleared" when a thermostat-mode override
        # was active (guarded on _manual_override_active); a fan-only override cleared here would
        # otherwise leave zero trace in the Activity Report.
        if had_fan and not had_manual and self._emit_event_callback:
            self._emit_event_callback("override_cleared", {"was_mode": None, "old_setpoint_f": None, "reason": reason})

        # Force the same reconciliation a natural grace expiry always performs, instead of
        # relying on the next incidental event or the next 30-min classification cycle.
        if self._post_grace_fan_check_callback:
            self._post_grace_fan_check_callback()
        if self._request_refresh_callback:
            self._request_refresh_callback()
        return True

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

    def handle_fan_manual_override(
        self,
        fan_before: str = "",
        fan_after: str = "",
        event_context_id: str | None = None,
        duration_override: float | None = None,
        remote_timer_hours: float | None = None,
        remote_speed: str | None = None,
        is_remote_event: bool = False,
    ) -> None:
        """Handle a manual fan state change — sets fan override flag + grace (Issue #327).

        Idempotent: safe to call even if an override is already active (re-stamps the
        time and restarts the grace period so the timer is always fresh).

        This is also the single entry point for QuietCool RF remote timer selections
        (Issue #486, coordinator._async_fan_remote_changed) — a remote timer press is a
        manual fan override just like any other, it just supplies its own grace
        duration instead of using the configured default. Deliberately NOT a separate
        method: the two callers must share the same override/grace bookkeeping so a
        future change to one path can't silently stop covering the other (the
        "sibling threshold drift" failure mode from #400/#402/#417/#456/#458).

        Args:
            fan_before: Fan state before the manual change (e.g. "on", "auto").
            fan_after: Fan state after the manual change.
            event_context_id: The HA Context id of the triggering state-changed event, if
                any (Issue #482) — surfaced in the Activity Report payload as diagnostic
                provenance data so a future investigation of a genuinely-external fan
                event doesn't need cross-source timestamp archaeology.
            duration_override: When set (seconds), the grace period lasts exactly this
                long instead of the configured manual_grace_seconds. Supplied by an RF
                remote timer selection; None means "use the configured default"
                (also the case for a plain physical fan-on with no timer info).
            remote_timer_hours: The RF remote's selected timer duration in hours, for
                observability only (dashboard/status display + serialized state). None
                for a non-remote-triggered override, or when the remote picked "no timer".
            is_remote_event: True when this call originates from the RF remote (Issue #495)
                — disambiguates a genuine "no timer" remote selection (``remote_timer_hours=
                None`` from a real ``timer_none`` press, which SHOULD revert the stored value
                to None) from a plain non-remote fan-on re-detection also passing
                ``remote_timer_hours=None`` (which should NOT clobber an already-active
                remote timer — see the preserve-on-restamp comment below).
            remote_speed: The RF remote's selected speed (``"low"``/``"medium"``/``"high"``),
                for observability only (Issue #519) — same guarded-overwrite treatment as
                ``remote_timer_hours`` below. None for a non-remote-triggered override, or a
                remote press with no speed component (e.g. timer alone).
        """
        # Issue #731 Phase 5: origin_state captured before any of this method's writes —
        # routed through _resolve_fan_fsm_state(). See the resolve call below (after all
        # the real direct writes) for the "group 1" wiring shape.
        from .fan_fsm import FanFsmEventKind

        _override_origin_state = self.fan_lifecycle_state

        was_override_active = self._fan_override_active
        self._stop_fan_min_runtime_cycles()
        self._fan_override_active = True
        self._fan_override_time = dt_util.now().isoformat()
        # Issue #495: only overwrite the remote-timer value when the caller has a genuine
        # opinion about it. This method is the SHARED entry point for both remote-timer
        # presses and plain (non-remote) fan-on detections; a plain re-stamp of an
        # already-active override (e.g. the WHF fan entity re-reporting "on" after a brief
        # unavailable flap) must not clobber an active remote timer to None. Confirmed live:
        # the status API returned fan_remote_timer_hours=None seconds after an 8h remote
        # press while the persisted state still held 8.0 — the fan entity's own
        # re-detection had nulled it. `is_remote_event` covers the one case that would
        # otherwise be indistinguishable from that non-remote re-detection: a deliberate
        # `timer_none` remote press, which also passes remote_timer_hours=None but SHOULD
        # clear the stored value (revert to the configured default duration).
        if is_remote_event or remote_timer_hours is not None or not was_override_active:
            self._fan_remote_timer_hours = remote_timer_hours
        # Issue #519: same guarded-overwrite treatment as remote_timer_hours above, so a
        # plain re-stamp doesn't clobber an already-known remote speed to None.
        if is_remote_event or remote_speed is not None or not was_override_active:
            self._fan_remote_speed = remote_speed

        # Issue #731 Phase 5: real side effect (the writes above) already happened —
        # this dispatch call is purely the group-1 mirror-sync/audit-trail step.
        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.MANUAL_OVERRIDE_DETECTED,
            origin_state=_override_origin_state,
        )

        _LOGGER.info(
            "Fan override: set — manual fan change detected %s->%s, override active since %s,"
            " grace period starting%s%s",
            fan_before or "?",
            fan_after or "?",
            self._fan_override_time,
            f" (RF remote timer: {self._fan_remote_timer_hours}h)" if self._fan_remote_timer_hours is not None else "",
            f" (RF remote speed: {self._fan_remote_speed})" if self._fan_remote_speed is not None else "",
        )
        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_manual_override",
                {
                    "fan_before": fan_before,
                    "fan_after": fan_after,
                    "override_active_since": self._fan_override_time,
                    "fan_device": _fan_device_label(self.config),
                    "event_context_id": event_context_id,
                    "remote_timer_hours": remote_timer_hours,
                    "remote_speed": remote_speed,
                },
            )
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        _trigger = "fan_manual_override"
        if self._start_grace_period_action("manual", trigger=_trigger, duration_override=duration_override):
            self._resolve_override_grace_fsm_state(kind=_OGFEventKind.FAN_OVERRIDE_DETECTED)

        # Issue #495: whole-house fan and HVAC are mutually exclusive — this was previously
        # only enforced for CA-initiated activation (_activate_fan()). A manually/remotely
        # detected WHF-on left the AC armed for the life of the override. Scoped to
        # WHOLE_HOUSE/BOTH; FAN_MODE_HVAC coexists with the compressor by design.
        if self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            self.hass.async_create_task(
                self._suppress_hvac_for_whf(
                    reason="whole-house fan manually turned on — suppressing HVAC to prevent AC/fan fighting"
                )
            )

    def handle_fan_speed_observed(self, speed: str, is_remote_event: bool = True) -> None:
        """Record a remote-reported speed CHANGE that does not constitute a manual override
        (Issue #519).

        The coordinator's burst-classification logic (coordinator._flush_fan_remote_burst())
        routes a bare speed press here — instead of through handle_fan_manual_override() —
        specifically when the fan was ALREADY running before this interaction and no timer
        was also selected: the user adjusted speed only, which is a comfort preference, not
        "taking manual control" of an off fan. Deliberately a separate, small function rather
        than a branch/flag inside handle_fan_manual_override(): that function's entire
        contract is "arm an override" (sets _fan_override_active, starts grace, suppresses
        HVAC) — smearing a "don't actually override" path through it via a flag would mix two
        different outcomes into one already-complex function instead of keeping each
        single-purpose and independently testable.

        Does NOT touch _fan_override_active, grace, or HVAC suppression — display/
        observability only. This is the seam a future CA-initiated speed-comfort feature
        (adjusting WHF speed for comfort/energy reasons) would build on; no such feature
        exists yet.
        """
        self._fan_remote_speed = speed
        _LOGGER.info("Fan remote speed observed (comfort-only, no override): %s", speed)
        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_speed_observed",
                {
                    "speed": speed,
                    "fan_device": _fan_device_label(self.config),
                    "is_remote_event": is_remote_event,
                },
            )

    def on_fan_turned_off(self, fan_before: str = "", fan_after: str = "", event_context_id: str | None = None) -> None:
        """Handle the user turning the fan OFF — clears fan state and gates nat-vent re-activation (Issue #359).

        Unlike ``handle_fan_manual_override`` (which is for fan-ON and sets ``_fan_override_active``),
        this method DOES NOT set ``_fan_override_active``: that flag means "user turned fan on, CA backs
        off".  Fan-off instead starts a grace period so nat-vent is not immediately re-activated before
        conditions are verified.

        Args:
            fan_before: Fan state before the change (e.g. "on", "auto").
            fan_after: Fan state after the change (e.g. "off", "auto").
            event_context_id: The HA Context id of the triggering state-changed event, if
                any (Issue #482) — surfaced in the Activity Report payload as diagnostic
                provenance data.
        """
        # Issue #530: a fan-off arriving inside the settle window of a just-expired
        # RF-timer-linked grace is the tail of that SAME timer boundary, not a fresh
        # event — coalesce into whatever the post-grace reconcile already decided instead
        # of starting a brand-new fan-off grace (which the orphaned-grace watchdog cannot
        # distinguish from a genuinely stuck one) and re-litigating the whole decision
        # from scratch.
        _settle_until = getattr(self, "_timer_boundary_settle_until", None)
        if _settle_until is not None and dt_util.now() <= _settle_until:
            self._timer_boundary_settle_until = None  # one-shot — consumed
            _LOGGER.info(
                "Fan turned off within the RF-timer boundary settle window (fan=%s->%s) —"
                " treating as the same timer session ending, not a new event",
                fan_before or "?",
                fan_after or "?",
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "fan_cancel",
                    {
                        "fan_before": fan_before,
                        "fan_after": fan_after,
                        "trigger": "timer_boundary_settle",
                        "fan_device": _fan_device_label(self.config),
                        "event_context_id": event_context_id,
                    },
                )
            # Issue #731 Phase 5: origin_state captured before this block's writes —
            # routed through _resolve_fan_fsm_state(). Group-1 wiring, kept distinct
            # from USER_FAN_OFF per fan_fsm.py's own docstring (this settle branch
            # deliberately does NOT start a fresh fan-off grace and may route through
            # _exit_nat_vent() instead of _clear_fan_flags_and_start_grace()). The
            # nat-vent-active branch's real flag change happens asynchronously inside
            # the scheduled _exit_nat_vent() task (not captured synchronously here —
            # this dispatch documents that the settle event itself fired); the else
            # branch's _fan_active/_fan_on_since clear IS synchronous and is reflected
            # in the post-state re-derivation below.
            from .fan_fsm import FanFsmEventKind

            _settle_origin_state = self.fan_lifecycle_state

            if self._natural_vent_active:
                # The post-grace reconcile adopted this fan as a nat-vent session moments
                # ago (it was still physically on when the grace's software clock
                # expired) — let the single choke point for ending a session decide
                # pause-vs-restore against live sensor state, instead of starting a fresh
                # fan-off grace that only gets fought by the orphaned-grace watchdog.
                self.hass.async_create_task(
                    self._exit_nat_vent(reason=f"fan={fan_before or '?'}->{fan_after or '?'} (RF timer boundary)")
                )
            else:
                # Reconcile didn't adopt the fan (or it was already off) — nothing further
                # to reconcile; just make sure the flags reflect reality.
                self._fan_active = False
                self._fan_on_since = None

            self._resolve_fan_fsm_state(
                kind=FanFsmEventKind.TIMER_BOUNDARY_SETTLE,
                origin_state=_settle_origin_state,
            )
            return

        _LOGGER.info(
            "Fan turned off by user: fan=%s->%s, trigger=fan_off",
            fan_before or "?",
            fan_after or "?",
        )

        # _fan_override_active must NOT be set here (it is the "user turned fan ON" flag).
        # If it is somehow already True at this point that indicates a missed transition
        # elsewhere — clear it and warn so the inconsistency is visible in logs.
        if self._fan_override_active:
            # Issue #731 Phase 5: origin_state captured before this block's writes —
            # routed through _resolve_fan_fsm_state(). Mirrors
            # handle_fan_manual_override()/clear_fan_override()'s group-1 wiring shape.
            # Only this stale-override-clear branch of on_fan_turned_off() is wired
            # here — the settle/grace logic elsewhere in this method
            # (USER_FAN_OFF/TIMER_BOUNDARY_SETTLE kinds) is left for a later sub-phase.
            from .fan_fsm import FanFsmEventKind

            _override_origin_state = self.fan_lifecycle_state

            _LOGGER.warning(
                "Fan turned off but _fan_override_active was True (stale override) — clearing. "
                "fan_before=%s fan_after=%s",
                fan_before or "?",
                fan_after or "?",
            )
            self._fan_override_active = False
            self._fan_override_time = None
            self._fan_remote_timer_hours = None
            self._fan_remote_speed = None

            self._resolve_fan_fsm_state(
                kind=FanFsmEventKind.OVERRIDE_CLEARED,
                origin_state=_override_origin_state,
            )

        if self._emit_event_callback:
            self._emit_event_callback(
                "fan_cancel",
                {
                    "fan_before": fan_before,
                    "fan_after": fan_after,
                    "trigger": "fan_off",
                    "fan_device": _fan_device_label(self.config),
                    "event_context_id": event_context_id,
                },
            )

        self._clear_fan_flags_and_start_grace(
            reason=f"fan={fan_before or '?'}->{fan_after or '?'}",
            trigger_label="fan_off",
            preserve_nat_vent_session=False,
        )
        # Issue #495: release any WHF HVAC suppression this session was holding (manual
        # override OR a nat-vent session the user stopped physically) and reclassify —
        # the fan is confirmed off by the very event that triggered this method.
        self._release_whf_and_reclassify(reason=f"fan turned off by user ({fan_before or '?'}->{fan_after or '?'})")

    def _clear_fan_flags_and_start_grace(
        self,
        *,
        reason: str,
        trigger_label: str = "fan_off",
        preserve_nat_vent_session: bool = False,
        source: str = "manual",
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
            source: `source` field on the grace period — "manual" for a genuine user action,
                "automation" for a CA-internal correction (Issue #446: the drift-correction
                caller was previously hardcoded to "manual" here, making the Activity Report
                claim the user turned the fan off when CA corrected itself).

        Issue #731 Phase 5: the SINGLE dispatch site for ``FanFsmEventKind.FLAGS_CLEARED_FOR_GRACE``
        — both real callers (``on_fan_turned_off()``'s genuine fan-off and
        ``_reconcile_fan_physical_drift()``'s CORRECT branch) share this one dispatch rather
        than each dispatching their own. ``on_fan_turned_off()``'s normal fan-off path is
        deliberately left without its own ``USER_FAN_OFF`` dispatch for the same reason — its
        entire flag-clearing effect IS this method, so a second dispatch there would just
        report the same net state change twice for one logical event (see fan_fsm.py's own
        USER_FAN_OFF/FLAGS_CLEARED_FOR_GRACE docstring split for why they're distinct kinds
        in the first place).
        """
        # routed through _resolve_fan_fsm_state(). Group-1 wiring: the real work below
        # (flag clear + grace-period start) is unchanged; the dispatch call is purely
        # the mirror-sync/audit-trail step.
        from .fan_fsm import FanFsmEventKind

        _clear_origin_state = self.fan_lifecycle_state

        self._fan_active = False
        self._fan_on_since = None
        if not preserve_nat_vent_session:
            self._natural_vent_active = False
            self._nat_vent_soft_start = False

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
        self._start_grace_period(source, trigger=trigger_label)

        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.FLAGS_CLEARED_FOR_GRACE,
            origin_state=_clear_origin_state,
        )

    def clear_fan_override(self) -> None:
        """Clear the fan override flag (called at transition points, Issue #327).

        Idempotent: no-op if no override is currently active.
        After clearing, restarts the min-runtime cycle that was suspended when the
        override was set.
        """
        if self._fan_override_active:
            # Issue #731 Phase 5: origin_state captured before this block's writes —
            # routed through _resolve_fan_fsm_state(). Mirrors
            # handle_fan_manual_override()'s group-1 wiring shape.
            from .fan_fsm import FanFsmEventKind

            _override_origin_state = self.fan_lifecycle_state

            _LOGGER.info(
                "Fan override: cleared — override active since %s, resuming CA fan control",
                self._fan_override_time,
            )
            self._fan_override_active = False
            self._fan_override_time = None
            self._fan_remote_timer_hours = None
            self._fan_remote_speed = None

            self._resolve_fan_fsm_state(
                kind=FanFsmEventKind.OVERRIDE_CLEARED,
                origin_state=_override_origin_state,
            )

            # Restart the min-runtime cycle that was suspended when override was set
            self.hass.async_create_task(self.start_min_fan_runtime_cycles())
            # Issue #495: release any WHF HVAC suppression this override was holding.
            # _release_whf_and_reclassify() itself checks physical fan state and no-ops
            # if the WHF is still running (e.g. grace expired but the timer hasn't) —
            # the post-grace fan reconcile owns that case instead.
            self._release_whf_and_reclassify(reason="fan override cleared")

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

        # Issue #731 Phase 5: origin_state captured before this method's write —
        # routed through _resolve_fan_fsm_state(). Group-1 wiring (caller-already-
        # decided): the real write (below) is unchanged; the dispatch call is purely
        # the mirror-sync/audit-trail step.
        from .fan_fsm import FanFsmEventKind

        _stop_origin_state = self.fan_lifecycle_state
        self._fan_min_runtime_active = False

        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.MIN_RUNTIME_CYCLE_STOPPED,
            origin_state=_stop_origin_state,
        )

    async def _fan_cycle_on(self) -> None:
        """Fan 'on' phase: activate fan, schedule off after min_runtime minutes.

        Architecture-reset Step 2: the decision now lives in
        desired_state.decide_fan_cycle_on() — this method owns actually calling
        _activate_fan() and scheduling the real async_call_later.
        """
        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state(). The FSM's
        # MIN_RUNTIME_CYCLE_ON transition computes the same outcome/delay
        # decide_fan_cycle_on() used to (fan_fsm.py wraps that same pure decision);
        # the actual _activate_fan() await and the fan_min_runtime_active/timer writes
        # stay in the shell below since transitions are synchronous.
        #
        # Issue #757 Step 2 fix: fan_fsm.py's _transition_on_min_runtime_cycle_on()
        # projects fan_active/fan_min_runtime_active=True onto to_state for the two
        # ACTIVATE outcomes (modeling what _activate_fan() below is ABOUT to do) —
        # _apply_fan_fsm_state() then writes that projection into self._fan_active
        # immediately, before _activate_fan() itself runs. That pre-empts
        # _activate_fan()'s own idempotency guard (`if self._fan_active: return
        # ALREADY_IN_STATE`), so the real command below silently no-ops (confirmed
        # via tests/test_fan_control.py::TestMinFanRuntime once this dispatch became
        # unconditional). This dispatch is kept only for its outcome/delay return
        # value and mirror-sync bookkeeping — the two fields it might have written
        # are restored to their pre-dispatch values so the real writes below (via
        # _activate_fan() and the explicit _fan_min_runtime_active= True lines) are
        # the sole source of truth for this call, matching every other "Group 1"
        # call site's real-write-owns-state discipline.
        from .fan_fsm import FanFsmEventKind

        _cycle_on_origin_state = self.fan_lifecycle_state
        _fan_active_before = self._fan_active
        _fan_min_runtime_active_before = self._fan_min_runtime_active

        _cycle_on_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.MIN_RUNTIME_CYCLE_ON,
            origin_state=_cycle_on_origin_state,
        )
        self._fan_active = _fan_active_before
        self._fan_min_runtime_active = _fan_min_runtime_active_before
        outcome = _cycle_on_transition.cycle_outcome
        delay = _cycle_on_transition.cycle_delay_seconds

        if outcome in (FanCycleOutcome.DISABLED, FanCycleOutcome.OVERRIDE_SUSPENDED):
            return

        if outcome is FanCycleOutcome.ACTIVATE_ALWAYS_ON:
            await self._activate_fan(reason="min_runtime_cycle")
            self._fan_min_runtime_active = True
            return

        if outcome is FanCycleOutcome.ACTIVATE_WITH_OFF_TIMER:
            await self._activate_fan(reason="min_runtime_cycle")
            self._fan_min_runtime_active = True

            @callback
            def _turn_off(_now: Any) -> None:
                self._fan_min_cycle_cancel = None
                self.hass.async_create_task(self._fan_cycle_off())

            self._fan_min_cycle_cancel = async_call_later(self.hass, delay, _turn_off)
            return

        # outcome is RETRY_LATER — fan already running for another reason
        @callback
        def _retry(_now: Any) -> None:
            self._fan_min_cycle_cancel = None
            self.hass.async_create_task(self._fan_cycle_on())

        self._fan_min_cycle_cancel = async_call_later(self.hass, delay, _retry)

    async def _fan_cycle_off(self) -> None:
        """Fan 'off' phase: deactivate fan, schedule next on after wait period.

        Architecture-reset Step 2: the decision now lives in
        desired_state.decide_fan_cycle_off().
        """
        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state(). Same shape as
        # _fan_cycle_on() above — the FSM's MIN_RUNTIME_CYCLE_OFF transition computes
        # the same should_deactivate/wait_seconds decide_fan_cycle_off() used to; the
        # actual _deactivate_fan() await and timer scheduling stay in the shell.
        #
        # Issue #757 Step 2 fix: same premature-projection hazard as _fan_cycle_on()
        # above — fan_fsm.py's _transition_on_min_runtime_cycle_off() projects
        # fan_active/fan_min_runtime_active=False onto to_state when
        # should_deactivate=True, before _deactivate_fan() below has actually run.
        # Restore both fields to their pre-dispatch values so the real writes
        # (_deactivate_fan()'s own ground-truth write and the explicit
        # _fan_min_runtime_active=False line) are the sole source of truth.
        from .fan_fsm import FanFsmEventKind

        _cycle_off_origin_state = self.fan_lifecycle_state
        _fan_active_before = self._fan_active
        _fan_min_runtime_active_before = self._fan_min_runtime_active

        _cycle_off_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.MIN_RUNTIME_CYCLE_OFF,
            origin_state=_cycle_off_origin_state,
        )
        self._fan_active = _fan_active_before
        self._fan_min_runtime_active = _fan_min_runtime_active_before
        should_deactivate = _cycle_off_transition.cycle_should_deactivate
        wait_sec = _cycle_off_transition.cycle_delay_seconds

        if should_deactivate:
            self._fan_min_runtime_active = False
            await self._deactivate_fan(reason="min_runtime_cycle_complete")

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
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        self.start_override_confirmation(
            source=source,
            event_kind=_OGFEventKind.OVERRIDE_DETECTED,
            old_mode=old_mode,
            new_mode=new_mode,
            classification_mode=classification_mode,
            old_setpoint_f=old_setpoint_f,
            new_setpoint_f=new_setpoint_f,
        )

    async def _stand_down_whf_for_override_conflict(
        self,
        *,
        mode: str | None,
        indoor_temp: float | None,
        event_source: str | None,
    ) -> None:
        """End an active WHF/nat-vent session because a manual HVAC-mode override just
        physically conflicts with it (Issue #714/#748).

        This is the single, shared implementation of the "manual override to an active mode
        detected while WHF owns HVAC — stand the session down immediately" rule. It used to be
        copy-pasted at four call sites (immediate override detection, nat-vent-exit tick,
        nat-vent-temp-check tick, fan-thermostat-check tick) — Issue #748's investigation
        found that duplication was the reason a later, unrelated fix (Issue #486's RF-remote-
        timer-absolute guard) could silently defeat this rule at some call sites without being
        caught: there was no single place to audit both behaviors together. Do not re-duplicate
        this at a new call site — call this method instead.

        Deliberately does NOT call ``_exit_nat_vent()``: that function's sensors-closed branch
        restores ``_pre_fan_hvac_mode`` (the mode captured BEFORE nat-vent started) via
        ``_set_hvac_mode()``, which has no override awareness of its own and would silently
        overwrite the user's just-set mode right back — the exact bug this rule closes.

        Passes ``bypass_absolute_override=True`` to ``_deactivate_fan()``: this is a hard
        AC/WHF mutex, not routine automation second-guessing an RF remote timer — last setting
        placed wins, unconditionally, no matter what mechanism is currently protecting the
        losing device.
        """
        self._natural_vent_active = False
        self._nat_vent_soft_start = False
        await self._deactivate_fan(
            reason=f"manual override to {mode} — ending free cooling",
            restore_hvac=False,
            release_suppression=True,
            emit_event=False,
            bypass_absolute_override=True,
        )
        if self._emit_event_callback:
            payload = {
                "indoor_temp": indoor_temp,
                "override_mode": mode,
                "fan_device": _fan_device_label(self.config),
            }
            if event_source is not None:
                payload["source"] = event_source
            self._emit_event_callback("nat_vent_manual_override_exit", payload)

    def start_override_confirmation(
        self,
        source: str,
        *,
        event_kind: OverrideGraceFsmEventKind,
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
            event_kind: Issue #664 — which ``OverrideGraceFsmEventKind`` this call
                corresponds to (``OVERRIDE_DETECTED`` for ``handle_manual_override()``,
                ``MANUAL_OVERRIDE_DURING_PAUSE`` for
                ``handle_manual_override_during_pause()``) — the two callers land on the
                same shape of transition but are distinct real production entry points.
            old_mode: Previous hvac_mode (for enriched event payload).
            new_mode: New hvac_mode detected.
            classification_mode: What classification expects (for event payload).
            old_setpoint_f: Previous thermostat setpoint in degrees F (setpoint overrides only).
            new_setpoint_f: New thermostat setpoint in degrees F (setpoint overrides only).
        """
        state = self.hass.states.get(self.climate_entity)
        detected_mode = state.state if state else "unknown"
        confirm_seconds = int(self.config.get(CONF_OVERRIDE_CONFIRM_PERIOD, DEFAULT_OVERRIDE_CONFIRM_SECONDS))

        # Issue #714: a manual override to an active HVAC mode structurally conflicts
        # with WHF/nat-vent (Issue #392's whole premise) — stand the session down the
        # instant the mode change is observed, independent of whether the confirmation
        # window below later accepts it as a durable override or it self-resolves as a
        # transient glitch. The physical fact "the thermostat currently reads an active
        # mode" is real right now, the same way a sensor-close event is real right now —
        # mirrors handle_all_doors_windows_closed()'s immediate, event-driven reaction
        # rather than waiting for nat-vent's own next tick-based re-evaluation to notice.
        # Deliberately does NOT call _exit_nat_vent(): its sensors-closed branch restores
        # _pre_fan_hvac_mode (captured before nat-vent started) via _set_hvac_mode(),
        # which has no override awareness of its own and would silently overwrite the
        # user's just-set mode right back — the same bug this fix closes elsewhere.
        if detected_mode not in ("off", "unavailable", "unknown") and self._whf_owns_hvac():
            _LOGGER.info(
                "Manual override to %s detected while WHF owns HVAC — ending free cooling session immediately",
                detected_mode,
            )
            # Set synchronously (not inside the scheduled task below) so any code that reads
            # these flags immediately after this call — still within the same tick — observes
            # the session as already standing down. hass.async_create_task() defers the
            # coroutine's body to the next event loop iteration; it does not run any of it
            # synchronously the way a direct `await` would.
            self._natural_vent_active = False
            self._nat_vent_soft_start = False
            self.hass.async_create_task(
                self._stand_down_whf_for_override_conflict(
                    mode=detected_mode,
                    indoor_temp=self._indoor_f_for_event(),
                    event_source="override_detected",
                )
            )

        # Architecture-reset Step 2 (session state machine slice): the disabled-vs-pending
        # branch now lives in desired_state.decide_override_confirm() — this method still
        # owns the immediate-accept side effect and the real async_call_later scheduling.
        # Issue #664: this decision itself is never dispatcher-branched — it's the exact
        # same decide_override_confirm() call override_grace_fsm.py's _land_after_detection()
        # makes, so legacy and FSM can never disagree on which branch to take.
        _override_pending = decide_override_confirm(
            confirm_seconds=confirm_seconds, detected_mode=detected_mode, now=dt_util.now()
        )
        if _override_pending is None:
            # Confirmation disabled — accept override immediately (legacy behaviour)
            self._confirm_override_action(detected_mode, source=source)
            self._resolve_override_grace_fsm_state(kind=event_kind)
            return

        # Cancel any existing pending confirmation (restart the window)
        if self._override_confirm_cancel:
            self._override_confirm_cancel()
            self._override_confirm_cancel = None

        self._override_confirm_time = dt_util.now().isoformat()
        self._override_confirm_mode = detected_mode
        self._override_confirm_source = source
        self._resolve_override_grace_fsm_state(kind=event_kind)
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

        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

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
                _LOGGER.info(
                    "Override confirmed after %d minutes (mode=%s, classification wants %s)",
                    confirm_seconds // 60,
                    current_mode,
                    cls_mode,
                )
                self._clear_override_confirm_action()
                self._confirm_override_action(current_mode, source=source)
                self._resolve_override_grace_fsm_state(kind=_OGFEventKind.OVERRIDE_CONFIRM_EXPIRED)
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "override_confirmed",
                        {
                            "mode": current_mode,
                            "confirm_delay_seconds": confirm_seconds,
                            "cls_mode": cls_mode,
                            "source": source,
                        },
                    )
            else:
                # PATH B: State resolved — transient event, no override
                _LOGGER.info(
                    "Potential override self-resolved (detected=%s, current=%s) — no action taken",
                    self._override_confirm_mode,
                    current_mode,
                )
                self._clear_override_confirm_action()
                self._resolve_override_grace_fsm_state(kind=_OGFEventKind.OVERRIDE_CONFIRM_EXPIRED)
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

    def _override_matches_current_decision(self, classification: DayClassification | None) -> bool:
        """Return True if the active manual override already matches what automation wants now.

        Issue #483 ("adopt matching decision instead of continuing a grace period"): if
        automation's current decision independently arrives at the same HVAC mode the
        user's override already produced, the override and its grace period no longer
        represent a real disagreement — the system should adopt the converged state
        rather than continue treating it as an active override.

        Scope (deliberately conservative — see docs/grace-periods-spec.md and Issue #483):
        - Only HVAC *mode* overrides are eligible (the ones that flow through
          ``_confirm_override()`` and set ``_manual_override_mode``). Fan-on overrides
          (``_fan_override_active``), fan-off grace (``on_fan_turned_off``), and
          door/window pause/resume grace do not set ``_manual_override_mode`` and are
          therefore never eligible here — there is no automation-decision comparison of
          the same shape for those triggers (fan eligibility requires re-evaluating live
          nat-vent conditions through an async, state-mutating path; door/window grace is
          about sensor state, not a "decision" at all).
        - Setpoint-only overrides (``_manual_override_source == "setpoint"``) are
          excluded outright. The HVAC mode may already match classification for these
          (the user only changed the target temperature, not the mode) — matching mode
          alone is not evidence the user's setpoint intent has converged with
          automation's, and falsely adopting would silently drop the user's chosen
          temperature.
        - Mode-changing overrides (``source`` "normal"/"pause") still require the
          thermostat's *current setpoint* to be within
          ``OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F`` of the setpoint ``select_comfort_band()``
          would arm right now for that mode. A compound override — the user both changed
          the mode AND deliberately chose a different temperature (e.g. "heat" at 74°F
          when automation's comfort_heat is 70°F) — must not be adopted just because the
          *mode* happens to agree; the temperature disagreement is still a real, live
          override of the user's intent. This mirrors the exact single-setpoint math
          ``_apply_comfort_band()`` uses (``band.floor`` for heat, ``band.ceiling`` for
          cool) so the comparison never drifts from what would actually be commanded.
        """
        if not self._manual_override_active or self._manual_override_mode is None:
            return False
        if self._manual_override_source == "setpoint":
            return False
        if classification is None:
            return False
        if self._manual_override_mode != classification.hvac_mode:
            return False
        if classification.hvac_mode not in ("heat", "cool"):
            # "off" (or any other non-setpoint mode): no setpoint to compare — mode match
            # is sufficient evidence of convergence.
            return True

        band = select_comfort_band(
            classification,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=_in_sleep_window(dt_util.now(), self.config),
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        target_f = band.floor if classification.hvac_mode == "heat" else band.ceiling

        state = self.hass.states.get(self.climate_entity) if self.hass else None
        raw_setpoint = state.attributes.get("temperature") if state else None
        if raw_setpoint is None:
            # No live setpoint to compare against — nothing more precise available;
            # mode match is the best evidence we have.
            return True
        unit = self.config.get("temp_unit", "fahrenheit")
        try:
            current_f = to_fahrenheit(float(raw_setpoint), unit)
        except (TypeError, ValueError):
            return True
        return abs(current_f - target_f) <= OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F

    def _confirm_override_action(self, mode: str, source: str | None = None) -> bool:
        """Real side effect only (Issue #664): the non-FSM-derived manual-override
        fields (always direct writes, outside the 2-tuple derivation) plus the
        grace-start action. Deliberately does NOT write
        ``_grace_active``/``_grace_protects_override`` — callers
        (``start_override_confirmation()``, the confirm-expiry timer closure) must
        follow with exactly one ``_resolve_override_grace_fsm_state()`` call. Its
        return value doesn't need to be threaded through to that call — the FSM
        independently re-derives whether grace would start via
        ``_build_override_grace_fsm_inputs()``'s ``grace_would_start`` field (same
        ``decide_grace_start()``-backed check), so it never wrongly claims
        ``ACTIVE_PROTECTING_OVERRIDE`` when manual grace is disabled by config
        (``manual_grace_seconds=0``) even though this method already ran.

        Args:
            mode: The confirmed HVAC mode string.
            source: The originating ``_override_confirm_source`` ("normal", "pause",
                or "setpoint"), carried forward so later adopt-on-match logic
                (Issue #483) can exclude setpoint-only overrides from adoption.

        Returns:
            True if grace actually started (a real timer now exists), False if manual
            grace is disabled by config.
        """
        self._manual_override_active = True
        self._manual_override_mode = mode
        self._manual_override_source = source
        self._manual_override_time = dt_util.now().isoformat()
        _LOGGER.info(
            "Manual override activated: mode=%s source=%s",
            self._manual_override_mode,
            self._manual_override_source or "unknown",
        )
        # Issue #717: single real, unconditional site — see _on_lifecycle_event()'s
        # docstring for why this must never be able to delay the grace-start action
        # that follows.
        try:
            self._lifecycle_dispatcher.emit(
                LifecycleEvent(
                    event_type=LifecycleEventType.OVERRIDE_CONFIRMED,
                    source="automation_engine",
                    at=dt_util.now(),
                    detail=mode,
                )
            )
        except Exception:  # noqa: BLE001 — a dispatcher bug must never affect the real override
            _LOGGER.exception("_confirm_override_action: lifecycle event emit failed (isolated)")
        return self._start_grace_period_action("manual", trigger="override_confirmed")

    def _confirm_override(self, mode: str, source: str | None = None) -> None:
        """Formally accept a manual override and start the grace period.

        Issue #664: thin wrapper — real work lives in ``_confirm_override_action()``.
        Not called from anywhere post-refactor (both former call sites now call the
        action directly and route the flag-write through the dispatcher instead — see
        ``start_override_confirmation()`` and its internal confirm-expiry closure), kept
        as the reference "legacy" flag computation and for any future direct caller.
        """
        if self._confirm_override_action(mode, source=source):
            self._legacy_set_grace_flags("override_confirmed")

    def _on_lifecycle_event(self, event: LifecycleEvent) -> None:
        """Update the dispatcher-synced mirror attributes (Issue #717).

        Trivial attribute assignment only — no I/O, no awaiting, no HA service
        calls (Decision Point 5 of the approved plan): a slow or buggy handler
        must never be able to delay a real HVAC command. Runs synchronously,
        inline, at ``emit()`` time, so the mirror is always correct by the time
        the same tick's ``_build_nat_vent_fsm_inputs()``/
        ``_build_door_window_fsm_inputs()`` call reads it — deferred emission
        would reopen exactly the "state not synced when read" gap Issue
        #615/#631 each hit independently, just relocated to this new seam.
        """
        if event.event_type is LifecycleEventType.DOOR_PAUSE_STARTED:
            self._dispatched_paused_by_door = True
        elif event.event_type is LifecycleEventType.DOOR_PAUSE_ENDED:
            self._dispatched_paused_by_door = False
        elif event.event_type is LifecycleEventType.GRACE_STARTED:
            self._dispatched_grace_active = True
        elif event.event_type is LifecycleEventType.GRACE_ENDED:
            self._dispatched_grace_active = False
        elif event.event_type is LifecycleEventType.OVERRIDE_CONFIRMED:
            self._dispatched_manual_override_active = True
        elif event.event_type is LifecycleEventType.OVERRIDE_CLEARED:
            self._dispatched_manual_override_active = False
        elif event.event_type is LifecycleEventType.NAT_VENT_SESSION_STARTED:
            self._dispatched_natural_vent_active = True
        elif event.event_type is LifecycleEventType.NAT_VENT_SESSION_ENDED:
            self._dispatched_natural_vent_active = False
        elif event.event_type is LifecycleEventType.WHF_HVAC_SUPPRESSED:
            self._dispatched_whf_owns_hvac = True
        elif event.event_type is LifecycleEventType.WHF_HVAC_RELEASED:
            self._dispatched_whf_owns_hvac = False

    @contextlib.asynccontextmanager
    async def _decision_pass(self, method_name: str):
        """Acquire ``self._decision_lock`` with wait/hold instrumentation (Issue #396).

        Logs when a method starts waiting on the lock and how long it waited once
        acquired, and tracks who currently holds it (`_decision_lock_holder` /
        `_decision_lock_held_since`) so a stuck or slow lock is diagnosable from logs
        alone instead of requiring another multi-hour investigation.

        Issue #717: also the single before/after diff point for
        NAT_VENT_SESSION_STARTED/ENDED — nat-vent has no single real activation
        chokepoint the way door/window and override/grace do (``_natural_vent_active``
        is written at ~18 scattered call sites across the six methods that all funnel
        through this context manager under ``_decision_lock``), so a before/after diff
        wrapped around the SAME serialization point every one of those call sites
        already passes through catches every write site in one change, rather than
        eighteen.
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
            _nat_vent_before = bool(self._natural_vent_active)
            try:
                yield
            finally:
                try:
                    _nat_vent_after = bool(self._natural_vent_active)
                    if _nat_vent_after and not _nat_vent_before:
                        self._lifecycle_dispatcher.emit(
                            LifecycleEvent(
                                event_type=LifecycleEventType.NAT_VENT_SESSION_STARTED,
                                source="automation_engine",
                                at=dt_util.now(),
                                detail=method_name,
                            )
                        )
                    elif _nat_vent_before and not _nat_vent_after:
                        self._lifecycle_dispatcher.emit(
                            LifecycleEvent(
                                event_type=LifecycleEventType.NAT_VENT_SESSION_ENDED,
                                source="automation_engine",
                                at=dt_util.now(),
                                detail=method_name,
                            )
                        )
                except Exception:  # noqa: BLE001 — a dispatcher bug must never affect a real decision pass
                    _LOGGER.exception("[decision-lock] %s: nat-vent session event emit failed (isolated)", method_name)
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
                if self._override_matches_current_decision(classification):
                    # Issue #483: automation's current decision already matches the
                    # override the user set — adopt it now instead of waiting out the
                    # rest of the grace period. Cancel the pending timer (no double-fire
                    # later), clear override/grace flags, log+emit at INFO, then FALL
                    # THROUGH to apply the (now-agreeing) classification normally below.
                    _LOGGER.info(
                        "Manual override adopted — automation decision now matches "
                        "(pre-expiry): mode=%s source=%s override_since=%s",
                        self._manual_override_mode,
                        self._manual_override_source or "normal",
                        self._manual_override_time,
                    )
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "override_adopted",
                            {
                                "mode": self._manual_override_mode,
                                "source": self._manual_override_source or "normal",
                                "pre_expiry": True,
                            },
                        )
                    self.cancel_override(reason="adopted_matching_decision")
                    if self._post_grace_fan_check_callback:
                        self._post_grace_fan_check_callback()
                    # continue below — classification is applied normally this cycle
                else:
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

            # Issue #498: occupancy/paused/nat-vent checks below now route through the single
            # shared gate (desired_state.decide_scheduled_band_gate()) also used by
            # handle_bedtime()/handle_morning_wakeup()/handle_pre_cool() — pure extraction of
            # this exact check order, no behavior change for this call site.
            # Issue #620: reconcile _paused_by_door with live sensor state first — a sensor
            # open since before any event-driven pause path ran would otherwise never be seen.
            await self._sync_paused_by_door_with_live_sensors()
            _gate = decide_scheduled_band_gate(
                occupancy_mode=self._occupancy_mode,
                manual_override_active=self._manual_override_active,
                paused_by_door=self._paused_by_door,
                natural_vent_active=self._natural_vent_active,
                whf_owns_hvac=self._whf_owns_hvac(),
            )

            # Issue #85: respect occupancy mode — don't overwrite setback with comfort
            # Issue #505: vacation must actively reapply the deep setback the same way away
            # does — "deep setback preserved" was never actually guaranteed once a manual
            # override (or anything else) has moved the thermostat off the setback value.
            if _gate == ScheduledBandGate.DEFER_OCCUPANCY:
                if self._occupancy_mode == OCCUPANCY_VACATION:
                    _LOGGER.info("Vacation mode — reapplying deep setback instead of comfort temps")
                    await self.handle_occupancy_vacation()
                    return
                _LOGGER.info("Away mode — reapplying setback instead of comfort temps")
                await self.handle_occupancy_away()
                return

            # Issue #337: while paused by open door/window, suppress the band and hold HVAC off.
            if _gate == ScheduledBandGate.DEFER_PAUSED:
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
                _paused_key = (classification.day_type, classification.hvac_mode)
                if self._emit_event_callback and not self._recent_duplicate(
                    "classification_suppressed_paused", _paused_key
                ):
                    _pause_minutes = (
                        (dt_util.now() - self._paused_since).total_seconds() / 60.0
                        if self._paused_since is not None
                        else None
                    )
                    self._emit_event_callback(
                        "classification_suppressed_paused",
                        {
                            "day_type": classification.day_type,
                            "hvac_mode": classification.hvac_mode,
                            "paused_entity": self._paused_entity,
                            "paused_minutes": round(_pause_minutes) if _pause_minutes is not None else None,
                        },
                    )
                return

            # Issue #338: while nat-vent is active with savings mode, enforce floor-only HVAC so the
            # 30-minute cycle cannot re-arm the ceiling (compressor) through open windows.
            # With savings off, call the helper to keep the full band current, then continue so the
            # ODE ceiling guard can still fire as a safety backstop if a breach is predicted.
            #
            # Issue #495: whf_owns_hvac is OR'd with natural_vent_active inside the shared gate
            # (decide_scheduled_band_gate) so a manually/remotely-detected WHF session (which sets
            # _pre_fan_hvac_mode via _suppress_hvac_for_whf() but does NOT set _natural_vent_active
            # — a manual override isn't a nat-vent decision) is also gated here, not just left to
            # the low-level _set_hvac_mode()/_set_temperature() write-guard. The write-guard alone
            # would still block the write, but silently — this catches the same condition apply_
            # classification already checked for the CA-initiated case so a manual session doesn't
            # compute select_comfort_band()/run the ODE ceiling guard just to have the result
            # dropped (Issue #392 Fix 1b's own rationale).
            if _gate == ScheduledBandGate.DEFER_NAT_VENT:
                _aggressive = bool(self.config.get("aggressive_savings", False))
                # Issue #618: DEFER_NAT_VENT is natural_vent_active OR whf_owns_hvac() (see the
                # comment above this block) — printing both disjuncts separately makes it
                # possible to tell "genuine nat-vent session" apart from "stranded WHF
                # ownership" directly from logs, without reading code, next time this fires for
                # much longer than expected.
                _LOGGER.info(
                    "apply_classification: nat-vent active — enforcing nat-vent band ac_assist=%s"
                    " day_type=%s natural_vent_active=%s whf_owns_hvac=%s",
                    not _aggressive,
                    classification.day_type,
                    self._natural_vent_active,
                    self._whf_owns_hvac(),
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
            _LOGGER.info(
                "Applying classification: %s (trend: %s %s)",
                classification.day_type,
                classification.trend_direction,
                format_temp_delta(classification.trend_magnitude, unit),
            )
            _old_mode_cls = _cs.state if _cs else None
            _cls_key = (classification.day_type, classification.hvac_mode)
            # _last_classification_applied is also read directly by coordinator.py/api.py as
            # a "has a classification ever been applied" marker (independent of event dedup) —
            # keep it updated to the latest key regardless of what _recent_duplicate() decides.
            self._last_classification_applied = _cls_key
            if not self._recent_duplicate("classification_applied", _cls_key):
                if self._emit_event_callback:
                    self._emit_event_callback(
                        "classification_applied",
                        {
                            "day_type": classification.day_type,
                            "hvac_mode": classification.hvac_mode,
                            "trend": classification.trend_direction,
                            "trend_magnitude": classification.trend_magnitude,
                            "old_hvac_mode": _old_mode_cls,
                            "indoor_f": indoor_temp,
                            "today_high": classification.today_high,
                            "applied_threshold_f": classification.applied_threshold_f,
                            "threshold_margin_f": classification.threshold_margin_f,
                        },
                    )
            else:
                _LOGGER.debug(
                    "classification_applied suppressed — same as last (%s/%s)",
                    classification.day_type,
                    classification.hvac_mode,
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
            )
            await self._apply_comfort_band(_band, reason=cls_reason)

            # ODE ceiling guard (Issue #136): if thermal model predicts indoor will breach
            # comfort_cool within lead_time AND outdoor is already warmer than indoor
            # (nat-vent unavailable), set HVAC to cool proactively.
            # Re-evaluated on every 30-min cycle — no flag needed; adapts to forecast changes.
            #
            # Issue #742/#757 Phase 7: the eligibility/dormancy/breach-scan/lead-time
            # computation is delegated to the pure classification_fsm.py/ode_ceiling_guard.py
            # pair — same logging, same events, same HVAC writes, driven by the returned
            # decision instead of re-derived inline. The legacy inline implementation was
            # graduated (removed) in Phase 7 after weeks of zero-divergence shadow comparison —
            # see _apply_ode_ceiling_guard_decision()'s own docstring for the parity contract.
            _cls_decision = self._resolve_classification_fsm_state(
                classification=classification, predicted_indoor=predicted_indoor
            )
            await self._apply_ode_ceiling_guard_decision(classification, predicted_indoor, _cls_decision)

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

        Issue #629: structural choke-point guard, mirroring the WHF/AC mutex guard in
        ``_set_hvac_mode()`` (Issue #392 Fix 1b) — enforced here, at the single write point
        all comfort-band callers funnel through, rather than by convention at each of the
        7 call sites. Upstream flag bookkeeping (``_paused_by_door``, set by
        ``_sync_paused_by_door_with_live_sensors()``) is supposed to stop this call from ever
        being reached while a monitored window is open, but that bookkeeping can lag or be
        exempted (``_is_within_planned_window_period()``) in ways that leave this the last line
        of defense. Confirmed production incident: a routine band re-affirmation right after
        nat-vent released ownership silently commanded ``cool`` mode through windows that had
        been open for over an hour.

        Exempt while nat-vent/WHF genuinely owns HVAC (``_natural_vent_active``/
        ``_whf_owns_hvac()``) — ``decide_scheduled_band_gate()`` checks occupancy *before*
        nat-vent (Issue #498), so ``handle_occupancy_away()``/``handle_occupancy_vacation()``
        legitimately arm a (usually wide, inert) setback band while an active nat-vent session
        continues to own the real HVAC behavior; nat-vent's own exit logic decides when to stop,
        independent of this choke-point. This guard exists for the case nat-vent/WHF does
        *not* own HVAC yet the window is still open.
        """
        if (
            not self._natural_vent_active
            and not self._whf_owns_hvac()
            and not self._sensor_debounce_pending
            and self._any_monitored_sensor_open()
        ):
            _LOGGER.warning(
                "_apply_comfort_band: monitored door/window open — refusing to arm active mode "
                "(would have been %s), pausing instead",
                band.active,
            )
            await self._pause_for_door_window(
                entity_label="monitored door/window",
                reason="door/window open at comfort-band arm time",
                notify_message=(
                    "🚪 HVAC paused — a monitored door/window is open. Heating/cooling will resume when it's closed."
                ),
                notify_type="door_window_pause",
            )
            return

        caps = self._get_thermostat_capabilities()

        if band.active == "ceiling" and caps.supports_cool:
            await self._set_temperature(band.ceiling, reason=reason, mode="cool")
            _cmd_shape = "cool"
            _target = band.ceiling
        elif band.active == "floor" and caps.supports_heat:
            await self._set_temperature(band.floor, reason=reason, mode="heat")
            _cmd_shape = "heat"
            _target = band.floor
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

        # Issue #444, revised by Issue #591/#590 Finding D: _set_temperature() above is always
        # called (unconditional thermostat re-assertion) — only the human-facing ANNOUNCEMENT
        # is deduped here, via the shared _recent_duplicate() helper. Permanent
        # (content-keyed, no fixed window) rather than #444's original 10-minute window — a
        # real 11-minute production gap (an uncancelled revisit timer armed by a
        # non-apply_classification() _apply_comfort_band() caller) slipped past that window
        # and re-announced an identical band. Owner-confirmed decision (#590 Finding D): no
        # periodic re-announcement heartbeat — the visible confirmation is silent after the
        # first announcement until the band actually changes; the underlying thermostat
        # command is unaffected and still fires every cycle.
        _signature = (band.active, _cmd_shape, round(_target, 2))
        if self._recent_duplicate("comfort_band_applied", _signature):
            _LOGGER.debug(
                "comfort_band_applied event suppressed — identical band (%s %.1f°F) already announced",
                _cmd_shape,
                _target,
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
            # Issue #591: WINDOWED (not permanent) dedup. Permanent content-keyed dedup was
            # tried first and reverted — it silently swallowed the second, semantically
            # distinct guard firing at wake-up in golden/pending scenario
            # wakeup_preserves_whf_manual_override, since each occurrence (bedtime, then
            # wake-up, hours apart) is its own decision point, not noise. But leaving this
            # site completely unguarded reopens the literal #584 bug shape — an overlapping
            # trigger pair (e.g. startup coalesce + its own follow-on refresh) firing this
            # SAME guard seconds apart. A short window catches that accidental near-duplicate
            # with wide margin below the hours-apart gaps real decision points have.
            _whf_block_sig = (mode,)
            if self._emit_event_callback and not self._recent_duplicate(
                "hvac_write_blocked_whf_active", _whf_block_sig, window_seconds=600
            ):
                self._emit_event_callback(
                    "hvac_write_blocked_whf_active",
                    {"attempted_mode": mode, "reason": reason},
                )
            return
        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would set HVAC mode to %s — %s role=%s", mode, reason, self.role)
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
            _LOGGER.info("Set HVAC mode to %s — %s role=%s", mode, reason, self.role)
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

    async def _set_temperature(
        self,
        temperature: float,
        *,
        reason: str,
        mode: str = "cool",
        skip_setpoint_sanity_check: bool = False,
    ) -> None:
        """Set the thermostat target temperature with hvac_mode in a single call.

        Args:
            temperature: Target temperature in internal Fahrenheit.
            reason: Human-readable reason for logging.
            mode: "cool" (ceiling setpoint) or "heat" (floor setpoint).  Sent as
                ``hvac_mode`` in the service call so the thermostat is always in the
                correct mode and HA deduplication is bypassed (the mode key makes
                every call distinct even when temperature hasn't changed).
            skip_setpoint_sanity_check: Issue #786 post-implementation audit, Fix 3.
                When True, skips the comfort_heat/comfort_cool sanity bounds check
                below. Only the TOU pre-conditioning call site
                (``apply_tou_precondition()``) passes True — by design it intentionally
                banks a cool-mode setpoint down toward (or below) ``comfort_heat`` (e.g.
                a sleep_heat-derived target), which is not a bug for that call path.
                Every other caller (``_apply_comfort_band()`` and all its other callers)
                keeps the default False and still gets the full sanity check.
        """
        # Issue #392 Fix 1b: structural choke-point guard — WHF/AC mutual exclusion is
        # enforced here rather than by convention at every call site.
        if mode != "off" and self._whf_owns_hvac():
            _LOGGER.warning("HVAC write blocked — whole-house fan owns thermostat (%s)", reason)
            # Issue #591: WINDOWED (not permanent) dedup. Permanent content-keyed dedup was
            # tried first and reverted — it silently swallowed the second, semantically
            # distinct guard firing at wake-up in golden/pending scenario
            # wakeup_preserves_whf_manual_override, since each occurrence (bedtime, then
            # wake-up, hours apart) is its own decision point, not noise. But leaving this
            # site completely unguarded reopens the literal #584 bug shape — an overlapping
            # trigger pair (e.g. startup coalesce + its own follow-on refresh) firing this
            # SAME guard seconds apart. A short window catches that accidental near-duplicate
            # with wide margin below the hours-apart gaps real decision points have.
            _whf_block_sig = (mode,)
            if self._emit_event_callback and not self._recent_duplicate(
                "hvac_write_blocked_whf_active", _whf_block_sig, window_seconds=600
            ):
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
                "[DRY RUN] Would set temperature to %s (%s mode) — %s role=%s",
                format_temp(temperature, unit),
                mode,
                reason,
                self.role,
            )
            return
        # Check setpoint is appropriate for commanded mode. Skipped for intentional
        # target-override writes (Issue #786 post-implementation audit, Fix 3) — TOU
        # pre-conditioning banks a cool-mode setpoint down toward comfort_heat (or below
        # it, to a sleep_heat-derived target) by design; that is not the bug this check
        # exists to catch in normal comfort-band writes.
        if skip_setpoint_sanity_check:
            pass
        elif mode == "cool" and temperature < (self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT) - 1.0):
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
                        "comfort_heat": self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT),
                        "comfort_cool": self.config.get("comfort_cool", DEFAULT_COMFORT_COOL),
                    },
                )
        elif mode == "heat" and temperature > (self.config.get("comfort_cool", DEFAULT_COMFORT_COOL) + 1.0):
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
                        "comfort_heat": self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT),
                        "comfort_cool": self.config.get("comfort_cool", DEFAULT_COMFORT_COOL),
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
            # Architecture-reset Step 2: reuses the SAME pure decision already built for
            # the post-fan setpoint verify (setpoint_verify_decision.decide_setpoint_verify) —
            # this is structurally the identical stale/no-reading/tolerance check (0.6°F),
            # just without the no-setpoint/override-active branches, which never apply here
            # (pending_setpoint_single/_mode are always set by this point, and this check
            # doesn't consider manual override).
            state = self.hass.states.get(self.climate_entity)
            reported: float | None = None
            if state is not None:
                _reported_raw = state.attributes.get("temperature")
                if _reported_raw is not None:
                    try:
                        reported = float(_reported_raw)
                    except (ValueError, TypeError):
                        reported = None
            outcome = decide_setpoint_verify(
                current_write_seq=self._write_seq,
                verify_write_seq=_my_seq,
                expected_temp=self._pending_setpoint_single,
                expected_mode=self._pending_setpoint_mode,
                manual_override_active=False,
                actual_temp=reported,
            )
            if outcome in (SetpointVerifyOutcome.STALE, SetpointVerifyOutcome.NO_READING):
                return
            if outcome is SetpointVerifyOutcome.REASSERT:
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
                            "reported": reported,
                            "reject_streak": self._setpoint_reject_streak,
                        },
                    )
                # Retry after 15 minutes if no newer command has superseded this one.
                # Architecture-reset Step 3 (last of the 9 DesiredState mechanisms): the
                # write_seq/nudge BRANCHING decision is now pure (decide_setpoint_retry_action
                # below). The delay itself stays a literal constant, not derived from
                # dt_util.now() arithmetic — this code path has never depended on the wall
                # clock (unlike grace/revisit), and introducing that dependency here would
                # repeat the exact decide_fan_cycle_on/off pitfall from earlier this session:
                # dt_util is a bare, unpatched MagicMock in these tests, so a mocked
                # `(at - now).total_seconds()` returns a MagicMock, not a real float.
                _retry_seq = _my_seq
                _retry_temp = service_temp
                _retry_mode = mode

                async def _retry_callback(_now: Any) -> None:
                    # Issue #411: on the 2nd+ consecutive rejection for this commanded value,
                    # nudge the setpoint by ±1°F first — some thermostat integrations dedup a
                    # repeated identical set_temperature payload, so retrying with the exact
                    # same value can never succeed. A brief nudge forces the device to
                    # recognize a real change before the actual target is sent 30s later.
                    _action = decide_setpoint_retry_action(
                        current_write_seq=self._write_seq,
                        retry_write_seq=_retry_seq,
                        reject_streak=self._setpoint_reject_streak,
                    )
                    if _action is SetpointRetryAction.SUPERSEDED:
                        return  # newer command superseded; skip retry
                    if _action is SetpointRetryAction.NUDGE_THEN_TARGET:
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
                                    "reject_streak": self._setpoint_reject_streak,
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
                            if not decide_scheduled_write_seq_current(
                                current_write_seq=self._write_seq, target_write_seq=_retry_seq
                            ):
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

                        self._setpoint_retry_cancel = async_call_later(self.hass, 30, _schedule_real_target)
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

                self._setpoint_retry_cancel = async_call_later(self.hass, 900, _schedule_retry)
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

        self._setpoint_retry_cancel = async_call_later(self.hass, 10, _schedule_check)
        _LOGGER.info(
            "Set temperature to %s (mode=%s) — %s role=%s",
            format_temp(temperature, unit),
            mode,
            reason,
            self.role,
        )
        self._record_action(f"Set temp to {format_temp(temperature, unit)} (mode={mode})", reason)

    async def _set_temperature_for_mode(
        self,
        c: DayClassification,
        *,
        reason: str,
        target_override: float | None = None,
        skip_setpoint_sanity_check: bool = False,
    ) -> None:
        """Set temperature based on the classification and current period.

        Safety net: redirects to setback handlers when occupancy is away/vacation
        so that any code path calling this function respects occupancy mode (Issue #85).

        ``target_override`` (Issue #786): use this value instead of
        ``self.config["comfort_heat"/"comfort_cool"]`` — the TOU scheduler's
        pre-conditioning banks toward a resolved target that may differ from the plain
        comfort value (e.g. the sleep-window edge, via ``scheduler.resolve_tou_phase()``).
        The occupancy safety net above still applies unconditionally regardless.

        ``skip_setpoint_sanity_check`` (Issue #786 post-implementation audit, Fix 3):
        forwarded to ``_set_temperature()`` unchanged. Only TOU pre-conditioning passes
        ``True`` — its whole design intentionally banks a cool-mode setpoint down toward
        (or below) ``comfort_heat`` (e.g. to a sleep_heat-derived target), which the
        sanity check would otherwise flag as a false ``SETPOINT INCONSISTENCY``. Every
        other caller keeps the default ``False`` and is unaffected.
        """
        # Issue #85: redirect to setback when not home/guest. Gate unified via
        # should_defer_to_occupancy_setback() (Issue #460); which handler to redirect
        # to still depends on which of the two deferring modes this is.
        if should_defer_to_occupancy_setback(self._occupancy_mode):
            if self._occupancy_mode == OCCUPANCY_AWAY:
                _LOGGER.info("Away mode — redirecting to setback instead of comfort (%s)", reason)
                await self.handle_occupancy_away()
            else:
                _LOGGER.info("Vacation mode — redirecting to deep setback instead of comfort (%s)", reason)
                await self.handle_occupancy_vacation()
            return

        if c.hvac_mode == "heat":
            floor_target = target_override if target_override is not None else float(self.config["comfort_heat"])
            await self._set_temperature(
                floor_target, reason=reason, mode="heat", skip_setpoint_sanity_check=skip_setpoint_sanity_check
            )
            return
        elif c.hvac_mode == "cool":
            ceiling_target = target_override if target_override is not None else float(self.config["comfort_cool"])
            await self._set_temperature(
                ceiling_target, reason=reason, mode="cool", skip_setpoint_sanity_check=skip_setpoint_sanity_check
            )
            return
        else:
            return

    async def apply_tou_precondition(self, classification: DayClassification, target: float, schedule_id: str) -> None:
        """Drive the setpoint toward the resolved TOU banking target ahead of a scheduled
        high-cost window (Issue #786).

        Routes through ``_set_temperature_for_mode()`` for the Issue #85 occupancy safety
        net. ``mode`` is not passed explicitly — ``classification.hvac_mode`` already
        matches the day's anticipated HVAC need, which is also the direction
        ``scheduler.resolve_tou_phase()`` banks in (see its docstring): pre-*cool* on a
        cooling day, pre-*heat* on a heating day.

        Defers (no HVAC command) via the same shared gate ``handle_bedtime()``/
        ``handle_morning_wakeup()``/``handle_pre_cool()`` already use
        (``desired_state.decide_scheduled_band_gate()``) — a manual override, a paused
        door/window, or an active nat-vent/WHF session all take precedence over TOU
        banking, exactly as they do for every other scheduled-trigger action. This
        replaced a bespoke door/window-only check that mirrored only
        ``_apply_comfort_band()``'s own guard and never checked override/paused state at
        all (Issue #786 post-implementation audit, Fix 1) — silently overwriting a
        protected manual override was the confirmed bug. ``DEFER_OCCUPANCY`` and
        ``PROCEED`` both fall through unchanged to ``_set_temperature_for_mode()`` below,
        whose existing Issue #85 occupancy safety net already redirects
        ``DEFER_OCCUPANCY`` correctly — a second redirect here would duplicate that
        logic.
        """
        # Issue #786 post-implementation audit, Fix 2: `decide_scheduled_band_gate()`
        # only sees `_manual_override_active`, which is set only after the confirm
        # window (`DEFAULT_OVERRIDE_CONFIRM_SECONDS`) elapses via
        # `_confirm_override_action()`. During that window `_override_confirm_pending`
        # is True but `_manual_override_active` is still False, so the gate above would
        # pass through and let TOU banking overwrite a not-yet-confirmed manual change.
        # `apply_classification()` guards this exact window separately (see its own
        # `_override_confirm_pending` check above) — mirror that here.
        if self._override_confirm_pending:
            _LOGGER.info(
                "Override confirmation pending (detected=%s at %s) — skipping TOU pre-conditioning (schedule=%s)",
                self._override_confirm_mode,
                self._override_confirm_time,
                schedule_id,
            )
            return

        await self._sync_paused_by_door_with_live_sensors()
        _gate = decide_scheduled_band_gate(
            occupancy_mode=self._occupancy_mode,
            manual_override_active=self._manual_override_active,
            paused_by_door=self._paused_by_door,
            natural_vent_active=self._natural_vent_active,
            whf_owns_hvac=self._whf_owns_hvac(),
        )
        if _gate in (
            ScheduledBandGate.DEFER_OVERRIDE,
            ScheduledBandGate.DEFER_PAUSED,
            ScheduledBandGate.DEFER_NAT_VENT,
        ):
            _LOGGER.info(
                "TOU pre-conditioning skipped this cycle — %s (schedule=%s)",
                _gate.value,
                schedule_id,
            )
            return

        _LOGGER.info(
            "TOU pre-conditioning: banking to %.1f°F ahead of schedule %s (mode=%s)",
            target,
            schedule_id,
            classification.hvac_mode,
        )
        await self._set_temperature_for_mode(
            classification,
            reason=f"tou_precondition schedule={schedule_id}",
            target_override=target,
            skip_setpoint_sanity_check=True,
        )

        # Emit only when the write above actually banked toward `target` — not when
        # _set_temperature_for_mode() redirected to away/vacation setback (Issue #85),
        # which would make a "tou_precondition_applied" event misleading. Dedup the
        # ANNOUNCEMENT only (not the underlying write, which _set_temperature_for_mode()
        # always issues every cycle this phase is active) — same shape as
        # _apply_comfort_band()'s own comfort_band_applied dedup.
        if not should_defer_to_occupancy_setback(self._occupancy_mode):
            _signature = (schedule_id, classification.hvac_mode, round(target, 2))
            if self._emit_event_callback and not self._recent_duplicate("tou_precondition_applied", _signature):
                self._emit_event_callback(
                    "tou_precondition_applied",
                    {
                        "schedule_id": schedule_id,
                        "target": target,
                        "mode": classification.hvac_mode,
                    },
                )

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
            _rate_usable = heating_rate if (confidence != "none" and heating_rate and heating_rate > 0) else None
            minutes_needed = compute_lead_minutes_from_rate(
                delta_t=temp_rise,
                rate=_rate_usable,
                min_minutes=min_min,
                max_minutes=max_min,
                safety_multiplier=safety,
                fallback_minutes=max(min_min, min(max_min, default_min)),
            )
            _adaptive_preheat_active = _rate_usable is not None

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

    @property
    def _sensor_debounce_pending(self) -> bool:
        """True if any monitored sensor is still inside its open/close debounce settle window.

        Single source of truth for "momentary/transient vs. genuinely settled" sensor state —
        reused by ``_idle_open`` (the reactivation-gate widening from Issue #244/#402/#504) and
        ``_sync_paused_by_door_with_live_sensors()`` (Issue #620) so the two callers can never
        define "settled" differently.
        """
        return bool(self._sensor_debounce_pending_callback and self._sensor_debounce_pending_callback())

    async def _sync_paused_by_door_with_live_sensors(self) -> None:
        """Reconcile ``_paused_by_door`` with live, debounce-settled sensor state (Issue #620).

        ``_paused_by_door`` is normally only set by event-driven paths
        (``handle_door_window_open()`` on a fresh open, ``_exit_nat_vent()``'s sensor-open
        branch). A sensor that was already open *before* either of those ever ran leaves it
        ``False`` forever — so ``decide_scheduled_band_gate()``'s four callers
        (``apply_classification``/``handle_bedtime``/``handle_morning_wakeup``/``handle_pre_cool``)
        could write an active HVAC mode into an open window with no live check at all. Confirmed
        live: 2026-08-11 incident, a sensor open since bedtime with no fresh open event ever
        firing. Call this at the top of each of those four, before they build the gate's inputs.
        """
        if self._paused_by_door or self._natural_vent_active or self._whf_owns_hvac():
            return  # already paused, or nat-vent/WHF already legitimately owns this
        if self._is_within_planned_window_period():
            return  # windows intentionally open — same exception handle_door_window_open() honors
        if self._sensor_debounce_pending or not self._any_monitored_sensor_open():
            return  # not open, or only momentarily/transiently open — do not pause on a blip
        await self._pause_for_door_window(
            entity_label="monitored door/window",
            reason="door/window still open at comfort-restore time",
            notify_message=(
                "🚪 HVAC paused — a monitored door/window is open. Heating/cooling will resume when it's closed."
            ),
            notify_type="door_window_pause",
        )

    def _set_door_window_pause_fields(self, *, entity_label: str, hvac_already_off: bool) -> None:
        """Write the door/window pause-state fields shared across every pause entry point.

        One definition of "what fields a door/window pause writes" for
        ``_pause_for_door_window()`` and ``_exit_nat_vent()``'s sensor-still-open
        branch — the latter used to hand-roll only ``_paused_by_door`` (plus its own
        ``_pre_pause_mode``), silently omitting ``_paused_with_hvac_already_off``/
        ``_paused_entity``/``_paused_since``. ``_paused_with_hvac_already_off`` feeds
        real control flow (``derive_door_window_lifecycle_state()`` uses it to tell
        ``PAUSED_ACTIVE`` from ``PAUSED_IDLE``); the other two are diagnostic-only
        (Activity Report "Settings" text via ``ai_skills_context.py``'s
        ``_render_paused_entity_settings()``).

        ``_pre_pause_mode`` is intentionally NOT written here — each caller derives
        it with its own state-read logic and writes it separately before calling
        this helper.
        """
        self._paused_by_door = True
        self._paused_with_hvac_already_off = hvac_already_off
        self._paused_entity = entity_label
        self._paused_since = dt_util.now()

    async def _pause_for_door_window_action(
        self, *, entity_label: str, reason: str, notify_message: str, notify_type: str
    ) -> bool | None:
        """The HVAC-affecting half of a door/window pause (Issue #660 Step 6):
        thermostat mode capture, turning HVAC off (or noting it's already off),
        notifying, and emitting the "sensor_opened" event. Split out of the former
        monolithic ``_pause_for_door_window()`` so Group B callers
        (``handle_door_window_open()``, ``_re_pause_for_open_sensor()``) can run this
        action half unconditionally while deriving their resulting pause *flags*
        through the shared FSM dispatcher under their own event kind.

        Returns ``hvac_already_off``: ``False`` if HVAC was actively turned off (a
        real mode transition happened), ``True`` if HVAC was already off (pause flag
        only). Returns ``None`` if the thermostat state was unavailable/unknown — no
        pause happens at all in that case (disambiguated from ``hvac_already_off``,
        which only makes sense once a pause is actually occurring; the original
        monolithic method's ``return False`` for this case conflated the two).
        """
        state = self.hass.states.get(self.climate_entity)
        mode = state.state if state else None
        if mode and mode not in ("off", "unavailable", "unknown"):
            self._pre_pause_mode = mode
            await self._set_hvac_mode("off", reason=f"{reason}, was {mode} mode")
            await self._notify(notify_message, "Climate Advisor", notification_type=notify_type)
            if self._emit_event_callback:
                self._emit_event_callback(
                    "sensor_opened",
                    {"entity": entity_label, "result": "paused", "hvac_mode_change": f"{mode}→off"},
                )
            return False
        if mode == "off":
            _LOGGER.info(
                "Door/window pause (%s): HVAC already off — pause flag set, no mode change needed",
                entity_label,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "sensor_opened",
                    {"entity": entity_label, "result": "paused"},
                )
            return True
        return None

    async def _pause_for_door_window(
        self, *, entity_label: str, reason: str, notify_message: str, notify_type: str
    ) -> bool:
        """Pause HVAC for an open door/window, or mark paused if HVAC was already off.

        Issue #523: handle_door_window_open() and _re_pause_for_open_sensor() each used to
        hand-roll this off-vs-not-off branch separately, and drifted out of sync — the older
        handle_door_window_open() never set _paused_by_door when HVAC was already off,
        silently leaving the next apply_classification() cycle unguarded. Single source of
        truth for both call sites now.

        Issue #660 Step 6: thin wrapper over the split action half
        (``_pause_for_door_window_action()``) and flags half (the shared
        ``_resolve_door_window_pause_flags()`` dispatcher, kind=SENSOR_OPENED — the
        same event kind ``handle_door_window_open()`` uses under Step 7, since this
        is semantically the same "a door/window is open, pause" outcome). Kept
        callable for its own generic callers (``_apply_comfort_band()``'s guard,
        ``_sync_paused_by_door_with_live_sensors()``'s SYNC_RECONCILE-adjacent
        direct-pause path) — both automatically become FSM-authoritative-capable
        through this one shared wrapper rather than needing individual treatment.

        Issue #757 Phase 6 Step 4 fix: passes ``nat_vent_gate_ruled_out=True`` — both
        real callers already independently confirmed nat-vent/WHF does NOT currently
        own HVAC before ever calling this method (see each caller's own guard), for
        reasons SENSOR_OPENED's own ``decide_nat_vent_gate()`` recomputation cannot see
        (see ``DoorWindowFsmInputs.nat_vent_gate_ruled_out``'s docstring for the full
        incident this closes — golden scenario
        ``issue_629_comfort_band_arm_through_open_window``).

        Returns True if HVAC was actively turned off (a real mode transition happened),
        False if HVAC was already off and only the pause flag was set (or if the
        thermostat state was unavailable/unknown and no pause happened at all).
        """
        hvac_already_off = await self._pause_for_door_window_action(
            entity_label=entity_label, reason=reason, notify_message=notify_message, notify_type=notify_type
        )
        if hvac_already_off is None:
            return False

        from .door_window_fsm import DoorWindowFsmEventKind

        self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.SENSOR_OPENED, nat_vent_gate_ruled_out=True)
        # _paused_entity/_paused_since aren't part of _apply_door_window_fsm_state()'s
        # 2-field derivation (see its own docstring) — direct writes here, matching
        # what _set_door_window_pause_fields() would have written on the removed
        # legacy path.
        self._paused_entity = entity_label
        self._paused_since = dt_util.now()
        return not hvac_already_off

    async def handle_door_window_open(self, entity_id: str) -> None:
        """Handle a door/window being opened for longer than the debounce period.

        Called by the coordinator after the debounce period.
        """
        from .door_window_fsm import DoorWindowFsmEventKind

        async with self._decision_pass("handle_door_window_open"):
            if self._paused_by_door:
                return  # Already paused

            if self._grace_active:
                # Issue #655: this used to short-circuit on a coarse outdoor-only
                # proxy (outdoor < comfort_cool + nat_vent_delta) instead of the real
                # 4-variable reactivation gate computed a few lines below — the two
                # could disagree, letting a "cool enough" outdoor reading fall through
                # the grace suppression only to still hit a pause moments later when
                # the real gate (which also needs indoor/comfort_heat) said no. Reuse
                # the same shared gate here instead of a hand-copied proxy.
                _outdoor_g = self._last_outdoor_temp
                _comfort_cool_g = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
                _nat_vent_delta_g = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
                _grace_gate_entered = self._nat_vent_may_reactivate(
                    outdoor=_outdoor_g,
                    indoor=self._get_indoor_temp_f(),
                    comfort_heat=self._nat_vent_reactivation_floor(),
                    comfort_cool=_comfort_cool_g,
                    nat_vent_delta=_nat_vent_delta_g,
                )
                if not _grace_gate_entered:
                    _LOGGER.info(
                        "Door/window open (%s) but %s grace period active — not pausing",
                        entity_id,
                        self._last_resume_source,
                    )
                    return
                # else: real gate says reactivation is viable — fall through to the
                # nat-vent-vs-pause decision below, same as before.

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
            comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
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
            #
            # Issue #757 Phase 6 Step 5: this boolean is decided by
            # nat_vent_fsm.transition() instead of the legacy direct call, restricted to
            # its ACTIVE_FULL_GATE outcome only. This call site has never modeled
            # soft-start entry (no _nat_vent_may_soft_start() call here) — the FSM wiring
            # here is wiring-only, not new decision authority — so an FSM-produced
            # ACTIVE_SOFT_START result is treated the same as "not entered" here, matching
            # this site's pre-existing scope.
            from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
            from .nat_vent_fsm import transition as _nat_vent_transition

            # The FSM's current_state is deliberately forced to INACTIVE rather than
            # read from self.nat_vent_lifecycle_state: this call site fires on ANY
            # window opening, including a second opening during an already-active
            # nat-vent session, but the question asked here is a pure entry-gate
            # question ("should nat-vent start now"), not an exit question. Reading
            # the live lifecycle state would route an in-flight active session
            # through the FSM's exit chain (decide_nat_vent_exit()) instead of the
            # entry gate, killing sessions that should have kept running. Same
            # always-fresh-compute semantics as _nat_vent_may_reactivate() and the
            # same INACTIVE-forcing already used in reconcile_fan_on_startup (see
            # that method's rationale comment above its transition() call).
            _fsm_current_state = NatVentLifecycleState.INACTIVE
            # hysteresis=0.0: this call site is one of the 2 (of 5)
            # _nat_vent_may_reactivate() callers that deliberately omits hysteresis
            # (see that method's docstring) — _build_nat_vent_fsm_inputs()'s default
            # would otherwise read the configured value, diverging from legacy here.
            #
            # grace_active=False: Issue #757 Phase 6 Step 5 fix — the grace-active
            # pre-check above (Fix 2/#249) already resolved whether grace should
            # block nat-vent here (temperature-based bypass); this FSM call must not
            # re-block on the still-True self._grace_active via the unrelated
            # overheat-exception rule. See _build_nat_vent_fsm_inputs()'s
            # grace_active docstring for the full incident.
            _fsm_inputs = self._build_nat_vent_fsm_inputs(
                now=dt_util.now(), indoor=indoor, outdoor=outdoor, hysteresis=0.0, grace_active=False
            )
            _fsm_result = _nat_vent_transition(
                _fsm_current_state, NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs)
            )
            _nat_vent_gate_entered = _fsm_result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE
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
                                                "indoor_temp": round(indoor, 1),
                                                "comfort_heat": round(comfort_heat, 1),
                                                "k_passive": round(k_passive, 4),
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
                    _activation_result = await self._activate_fan(reason=nat_vent_reason)
                    # Project onto the FSM state that matches legacy's writes at this
                    # site exactly: legacy only ever set _natural_vent_active = True
                    # here, leaving _nat_vent_soft_start unchanged and _paused_by_door
                    # untouched (guaranteed False already — this method returns early
                    # at the top if _paused_by_door is True). Hardcoding
                    # ACTIVE_FULL_GATE regardless of _fsm_result.to_state would
                    # silently demote an in-flight soft-start session to full-gate on
                    # a second window opening, which legacy never did.
                    #
                    # Issue #706 (Bug F): routed through
                    # _apply_nat_vent_fsm_state_after_activation() rather than applying
                    # this pre-await decision directly — a manual override arriving
                    # during the await above must not be silently overwritten.
                    self._apply_nat_vent_fsm_state_after_activation(
                        NatVentLifecycleState.ACTIVE_SOFT_START
                        if self._nat_vent_soft_start
                        else NatVentLifecycleState.ACTIVE_FULL_GATE,
                        _activation_result,
                    )
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

            debounce_minutes = self.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS) // 60
            friendly_name = entity_id.split(".")[-1].replace("_", " ").title()
            # Issue #660 Step 7: calls the action half directly (instead of going
            # through the _pause_for_door_window() wrapper) and derives its own flags
            # under its own event kind (SENSOR_OPENED — same kind the wrapper already
            # used for this outcome, no change in FSM behavior, just explicit routing
            # matching this method's Group B role). No change to any decision logic
            # above this point — both Phase 2 guards, the grace real-gate, and the
            # planned-window check stay byte-for-byte unchanged.
            hvac_already_off = await self._pause_for_door_window_action(
                entity_label=entity_id,
                reason=f"door/window open — {entity_id}",
                notify_message=(
                    f"🚪 HVAC paused — {friendly_name} has been open for "
                    f"{debounce_minutes} minutes. "
                    f"Heating/cooling will resume when it's closed."
                ),
                notify_type="door_window_pause",
            )
            if hvac_already_off is not None:
                self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.SENSOR_OPENED)
                self._paused_entity = entity_id
                self._paused_since = dt_util.now()

    async def handle_all_doors_windows_closed(self) -> None:
        """Resume HVAC after all monitored doors/windows are closed."""
        async with self._decision_pass("handle_all_doors_windows_closed"):
            was_nat_vent = self._natural_vent_active
            was_paused = self._paused_by_door
            if self._emit_event_callback:
                self._emit_event_callback(
                    "sensor_all_closed",
                    {
                        "was_paused": was_paused,
                        "was_nat_vent": was_nat_vent,
                        # Issue #504: lets the Activity Report show the whf:on->off transition
                        # on this row — _exit_nat_vent() suppresses _deactivate_fan()'s own
                        # event (Issue #411) since this event is its "caller emits its own
                        # specific event" contract, but it never carried the fan device label.
                        "fan_device": _fan_device_label(self.config),
                    },
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

            from .door_window_fsm import DoorWindowFsmEventKind

            # Issue #660 Step 4 / #757 Phase 6 Step 4: routed through the shared,
            # unconditionally FSM-authoritative dispatcher. The restore-or-clear action
            # below is driven by self._pre_pause_mode's own truthiness, the same input
            # the FSM's ALL_SENSORS_CLOSED transition consults (pre_pause_mode_active,
            # Step 2's fix), so they agree on when to act.
            self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.ALL_SENSORS_CLOSED)
            self._paused_entity = None
            self._paused_since = None
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
            # Issue #523: _paused_by_door alone is no longer a reliable "HVAC was actively
            # interrupted" signal — handle_door_window_open()/_pause_for_door_window() now
            # correctly set it even when HVAC was already off (nothing to interrupt, no
            # grace/resume timer ever starts). Without _actively_paused, that flag-only case
            # would permanently block this idle-open re-evaluation loop below (Issue #244/
            # #402/#504) since nothing else would ever re-trigger it.
            _actively_paused = self._paused_by_door and not self._paused_with_hvac_already_off
            if not (_actively_paused or self._natural_vent_active):
                # Comfort-ceiling override (Issue #134): if grace is active and indoor has
                # risen above comfort_cool, allow re-evaluation so nat-vent can engage.
                # Grace still blocks rapid door-open/close cycling below the comfort ceiling.
                _indoor = self._get_indoor_temp_f()
                _cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
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
                # Issue #504: re-read of #244 confirmed it was about a sensor open ALL DAY
                # never getting re-evaluated once outdoor cools later — not about reacting
                # within milliseconds of a fresh open. Gate on the same CONF_SENSOR_DEBOUNCE
                # settle window that already governs the pause decision (handle_door_window_open),
                # instead of the raw instantaneous sensor state. A sensor that's been open long
                # enough for #244's scenario to matter has long since cleared its debounce timer,
                # so this is a no-op for #244 — it only blocks reacting to a sensor still
                # bouncing/settling (was previously indistinguishable from a real, stable open).
                # Issue #620: _idle_open must not bypass an active grace period — grace exists
                # specifically to gate nat-vent re-activation (docs/grace-periods-spec.md:79,95),
                # and this widening (Issue #244/#402/#504) was the one path that never checked
                # it. The comfort-ceiling exception beside it (Issue #134) is untouched — genuine
                # overheating during grace still re-engages nat-vent.
                #
                # Issue #757 Phase 6 Step 5 fix: _idle_open must also not bypass a fresh
                # reactivation lockout. Before Step 5, nat_vent_temperature_check()'s fast
                # per-tick exit chain only recognized comfort-floor exits, so an AWAY_CEILING
                # exit could only ever happen via THIS function's own exit-chain below — and
                # entry/exit are mutually exclusive within one call (this precondition block
                # only runs when nat-vent is NOT active), so there was no way to reach here
                # moments after an away-ceiling exit in the same tick. Step 5 made the fast
                # per-tick exit chain recognize AWAY_CEILING too, so a real production tick can
                # now exit via that fast path and then, in the SAME event, reach this idle-open
                # re-entry gate with sensors still open — decide_nat_vent_gate() has no
                # occupancy awareness, and an away-ceiling exit changes none of the outdoor/
                # indoor/comfort_heat inputs it checks (unlike every other exit reason, whose
                # own trigger condition inherently makes immediate re-entry impossible).
                # Reusing the general-purpose reactivation-lockout mechanism (now armed by
                # AWAY_CEILING exits too, see that branch's own comment) rather than adding a
                # new occupancy-specific entry guard — nat-vent activation is deliberately NOT
                # occupancy-gated in general (see golden scenario
                # away_natvent_activates_free_cooling's own regression-proof notes); this is a
                # generic "don't immediately re-enter right after any recent exit" cooldown,
                # the same protection paused-by-door reactivation already has.
                _idle_open = (
                    self._any_monitored_sensor_open()
                    and _hvac_off_244
                    and not self._sensor_debounce_pending
                    and not self._grace_active
                    and not is_reactivation_locked_out(
                        outdoor_exit_time=self._nat_vent_outdoor_exit_time,
                        now=dt_util.now(),
                        lockout_seconds=float(
                            self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S)
                        ),
                    )
                )
                if not ((self._grace_active and _indoor is not None and _indoor > _cool) or _idle_open):
                    return

            outdoor = self._last_outdoor_temp
            # Issue #608: computed unconditionally (not only inside the
            # `if self._natural_vent_active:` exit-chain block below) because the
            # later paused-reactivation-lockout check also reads it, matching the
            # original code's own unconditional read at its outdoor-rise-exit site.
            indoor = self._get_indoor_temp_f()
            comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
            nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
            threshold = comfort_cool + nat_vent_delta

            # Issue #134: comfort-ceiling re-entry during grace — neither flag is True but
            # indoor has risen above comfort_cool. Check nat-vent conditions directly.
            # (_actively_paused computed above — same value, condition unchanged in between.)
            if not (_actively_paused or self._natural_vent_active):
                _indoor = self._get_indoor_temp_f()
                _comfort_heat = self._nat_vent_reactivation_floor()
                _hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
                # Issue #411 Pass 4: this was a 4th hand-copied instance of the shared
                # reactivation gate (found after the initial 3-site extraction) — folded
                # into _nat_vent_may_reactivate() for consistency, not left as a copy.
                #
                # Issue #757 Phase 6 Step 5: both the full-gate reactivation and the
                # soft-start sub-gate below are decided by a single
                # nat_vent_fsm.transition() call instead of the two hand-sequenced legacy
                # calls — same priority order (decide_nat_vent_gate() first,
                # decide_nat_vent_soft_start_gate() only if that fails), same pure
                # functions underneath. The former legacy ``elif`` chain (a direct
                # ``_paused_by_door``/``is_reactivation_locked_out()`` check, then
                # ``_nat_vent_may_reactivate()``, then ``_nat_vent_may_soft_start()``) is
                # provably subsumed by this FSM branch's own ``to_state`` handling —
                # the FSM's ``_transition_from_inactive()`` already applies the identical
                # reactivation-lockout check (``PAUSED_REACTIVATION_LOCKOUT``) before ever
                # considering ``ACTIVE_FULL_GATE``/``ACTIVE_SOFT_START``, so a locked-out
                # or non-qualifying case simply falls through with no matching ``if/elif``
                # below and this block does nothing — the same externally-observable
                # outcome as the deleted legacy branches.
                from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
                from .nat_vent_fsm import transition as _nat_vent_transition

                _fsm_current_state = self.nat_vent_lifecycle_state
                # Issue #696: previously hardcoded paused_by_door=False here, on the
                # premise that this idle-open re-entry site only ever runs when
                # _actively_paused is False and so could never legitimately have
                # _paused_by_door=True. That premise missed the
                # _paused_with_hvac_already_off=True case Issue #523 deliberately made
                # reachable here: _actively_paused (= paused_by_door AND NOT
                # hvac_already_off) is False even while the real _paused_by_door flag
                # is True (sensor still open, HVAC was already idle at exit) — exactly
                # what a COMFORT_FLOOR exit with an open sensor produces. Passing the
                # real flag lets the FSM's existing PAUSED_REACTIVATION_LOCKOUT branch
                # (_transition_from_inactive(), nat_vent_fsm.py) apply here the same way
                # it already does at the paused-by-door reactivation call site below —
                # outdoor_exit_time/lockout_seconds are unaffected by this parameter and
                # were already correctly populated either way.
                _fsm_inputs = self._build_nat_vent_fsm_inputs(
                    now=dt_util.now(), indoor=_indoor, outdoor=outdoor, apply_reactivation_floor=True
                )
                _fsm_result = _nat_vent_transition(
                    _fsm_current_state, NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs)
                )
                _to_state = _fsm_result.to_state
                if _to_state == NatVentLifecycleState.ACTIVE_SOFT_START and not self.config.get(
                    CONF_NAT_VENT_SOFT_START_ENABLED, DEFAULT_NAT_VENT_SOFT_START_ENABLED
                ):
                    # CONF_NAT_VENT_SOFT_START_ENABLED has no FSM-side field —
                    # decide_nat_vent_soft_start_gate() always evaluates the condition;
                    # same caller-side-guard treatment as the Phase 2c forecast/thermal
                    # guards (nat_vent_fsm.py does not gain a new field for this).
                    _to_state = NatVentLifecycleState.INACTIVE

                # Issue #757 Phase 6 Step 5: the former legacy ``elif`` chain here (a
                # direct ``_paused_by_door``/``is_reactivation_locked_out()`` check, then
                # ``_nat_vent_may_reactivate()``, then ``_nat_vent_may_soft_start()``) is
                # provably subsumed by ``_to_state`` above — ``_transition_from_inactive()``
                # (nat_vent_fsm.py) already applies the identical reactivation-lockout
                # check (``PAUSED_REACTIVATION_LOCKOUT``) before ever considering
                # ``ACTIVE_FULL_GATE``/``ACTIVE_SOFT_START``, so a locked-out or
                # non-qualifying case leaves ``_to_state`` at neither branch below and
                # this block does nothing — the same externally-observable outcome the
                # deleted legacy branches produced.
                if _to_state == NatVentLifecycleState.ACTIVE_FULL_GATE:
                    # Band stays armed — just activate the fan; the compressor self-arbitrates.
                    _activation_result = await self._activate_fan(
                        reason=(
                            f"nat-vent re-engaged: outdoor {outdoor:.1f}°F < indoor {_indoor:.1f}°F"
                            f" − {_hysteresis:.1f}°F hysteresis, indoor > comfort_heat {_comfort_heat:.1f}°F,"
                            f" outdoor ≤ threshold {threshold:.1f}°F — free cooling still favorable"
                        )
                    )
                    # Preserve _paused_by_door across the apply: legacy's
                    # `self._natural_vent_active = True` write at this site never
                    # touched _paused_by_door, but _apply_nat_vent_fsm_state()'s
                    # projection unconditionally clears it (it only reads True from
                    # PAUSED_REACTIVATION_LOCKOUT). Without this, a caller that enters
                    # here with _paused_by_door=True (e.g. also
                    # _paused_with_hvac_already_off=True) would flip to
                    # _paused_by_door=False, an incoherent pair legacy never produced.
                    #
                    # Issue #706 (Bug F): routed through
                    # _apply_nat_vent_fsm_state_after_activation() — an override
                    # arriving during the await above must not be silently overwritten.
                    _pre_paused_by_door = self._paused_by_door
                    self._apply_nat_vent_fsm_state_after_activation(_to_state, _activation_result)
                    self._paused_by_door = _pre_paused_by_door
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
                elif _to_state == NatVentLifecycleState.ACTIVE_SOFT_START:
                    _LOGGER.info(
                        "Nat-vent soft-start entered: outdoor %.1f°F <= indoor %.1f°F,"
                        " past today's peak %.1f°F by >= %.1f°F, indoor > comfort_heat %.1f°F",
                        outdoor,
                        _indoor,
                        self._outdoor_temp_today_peak or 0.0,
                        PEAK_DECLINE_MARGIN_F,
                        _comfort_heat,
                    )
                    _activation_result = await self._activate_fan(
                        reason=(
                            f"nat-vent soft-start: outdoor {outdoor:.1f}°F at/below indoor {_indoor:.1f}°F"
                            " parity, past today's peak and declining — purge/comfort air movement"
                        )
                    )
                    # Preserve _paused_by_door across the apply — see the matching
                    # comment on the ACTIVE_FULL_GATE branch above for the rationale.
                    # Issue #706 (Bug F): same override-race guard as the
                    # ACTIVE_FULL_GATE branch above.
                    _pre_paused_by_door = self._paused_by_door
                    self._apply_nat_vent_fsm_state_after_activation(_to_state, _activation_result)
                    self._paused_by_door = _pre_paused_by_door
                    await self._apply_nat_vent_hvac_state()
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_soft_start_entered",
                            {
                                "outdoor": outdoor,
                                "indoor": _indoor,
                                "outdoor_today_peak": self._outdoor_temp_today_peak,
                                "comfort_heat": _comfort_heat,
                                "decline_margin_f": PEAK_DECLINE_MARGIN_F,
                            },
                        )
                return

            # Issue #540: soft-start → full nat-vent upgrade. Once an active soft-start
            # session's outdoor/indoor delta independently clears the full bulk-cooling
            # gate, drop the qualifier — the session itself (fan, HVAC suppression, exit
            # hierarchy) is already running unchanged; only the status label changes.
            #
            # Issue #757 Phase 6 Step 5: this upgrade check is computed once, together
            # with the exit-chain decision immediately below, via a single
            # ``nat_vent_fsm.transition()`` call — the same pure escalation check
            # (Issue #540, mirrored into ``nat_vent_fsm.py``), read from one shared
            # place. The former legacy twin here (a direct ``_nat_vent_may_reactivate()``
            # call, run only while non-authoritative) has been removed — see the
            # ``self._nat_vent_soft_start:`` block below (formerly gated on
            # ``_natvent_fsm_authoritative`` too) for the surviving single path.

            # Issue #608 (Block 5 Phase 2): the 5-check priority-ordered exit chain
            # (comfort-floor, away-ceiling, proactive-floor, outdoor-rise,
            # ceiling-threshold) previously inline here now lives in
            # nat_vent_exit.decide_nat_vent_exit() — same conditions, same
            # priority order, differentially validated (tests/test_nat_vent_exit.py
            # + unchanged golden/pending assertions on these exact exit events).
            # Side effects (fan/HVAC calls, event emission, logging) stay here;
            # only the branching condition moved.
            if self._natural_vent_active:
                comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
                indoor = self._get_indoor_temp_f()
                if indoor is not None and comfort_cool is not None and indoor > comfort_cool:
                    # Issue #247: the ODE ceiling guard now ESCALATES to AC on the classification cycle
                    # when indoor breaches the ceiling under active nat-vent (its three-condition dormancy
                    # lifts), so this is an informational heads-up, not a stuck state.
                    _LOGGER.info(
                        "Nat-vent active but indoor %.1fF > comfort_cool %.1fF --"
                        " ceiling guard will escalate to AC this classification cycle",
                        indoor,
                        comfort_cool,
                    )

                thermal = self._thermal_model or {}

                # Issue #757 Phase 6 Step 5: the soft-start escalation check (skipped
                # above) is computed here via nat_vent_fsm.transition() instead of the
                # hand-duplicated inline copy (now removed). The exit-chain decision
                # immediately below is unchanged either way — it already called this
                # same pure decide_nat_vent_exit() function directly, so there is
                # nothing to swap there; the FSM's own transition() calls the identical
                # function with the identical inputs (see tools/sim_harness's
                # nat_vent_fsm_decision_compare.py for the equivalence proof).
                if self._nat_vent_soft_start:
                    from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
                    from .nat_vent_fsm import transition as _nat_vent_transition

                    _fsm_current_state = self.nat_vent_lifecycle_state
                    _fsm_inputs = self._build_nat_vent_fsm_inputs(now=dt_util.now(), indoor=indoor, outdoor=outdoor)
                    _fsm_result = _nat_vent_transition(
                        _fsm_current_state, NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs)
                    )
                    if (
                        _fsm_current_state == NatVentLifecycleState.ACTIVE_SOFT_START
                        and _fsm_result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE
                    ):
                        self._nat_vent_soft_start = False
                        _LOGGER.info(
                            "Nat-vent soft-start upgraded to full free-cooling (FSM-authoritative):"
                            " outdoor %.1f°F, indoor %.1f°F",
                            outdoor if outdoor is not None else 0.0,
                            indoor if indoor is not None else 0.0,
                        )

                exit_decision = decide_nat_vent_exit(
                    NatVentExitInputs(
                        indoor=indoor,
                        outdoor=outdoor,
                        comfort_heat_raw=comfort_heat,
                        sleep_heat=float(self.config.get(CONF_SLEEP_HEAT, comfort_heat)),
                        in_sleep_window=_in_sleep_window(dt_util.now(), self.config),
                        hysteresis=float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F)),
                        comfort_cool=comfort_cool,
                        nat_vent_delta=nat_vent_delta,
                        occupancy_mode=self._occupancy_mode,
                        thermal_confidence=thermal.get("confidence", "none"),
                        k_passive=thermal.get("k_passive"),
                        manual_override_active=self._manual_override_active,
                        manual_override_mode=self._manual_override_mode,
                    )
                )

                if exit_decision.reason == NatVentExitReason.MANUAL_OVERRIDE_CONFLICT:
                    # Issue #714: a manual override to an active HVAC mode structurally
                    # conflicts with WHF/nat-vent — end the session immediately. Deliberately
                    # does NOT call _exit_nat_vent() here: that function's sensors-closed
                    # branch restores _pre_fan_hvac_mode (the mode captured BEFORE nat-vent
                    # started) via _set_hvac_mode(), which has no override awareness of its
                    # own and would silently overwrite the user's just-set mode right back —
                    # the exact bug this fix closes. Release suppression without writing any
                    # mode; the thermostat already reads the user's chosen mode and that's
                    # exactly where it should stay.
                    _LOGGER.info(
                        "Nat-vent exit: manual override to %s conflicts with WHF — ending free"
                        " cooling session (indoor %.1f°F)",
                        self._manual_override_mode,
                        indoor if indoor is not None else 0.0,
                    )
                    await self._stand_down_whf_for_override_conflict(
                        mode=self._manual_override_mode,
                        indoor_temp=indoor,
                        event_source=None,
                    )
                    return

                if exit_decision.reason == NatVentExitReason.COMFORT_FLOOR:
                    # Note (Issue #620 investigation): like fan_thermostat_check()'s
                    # STOP_COOLED_TO_FLOOR (now fixed via _exit_nat_vent()), this branch calls
                    # _deactivate_fan() with default restore_hvac=True and no live sensor check
                    # — the same bug class. NOT migrated to _exit_nat_vent() here: unlike the
                    # fast-path branches, this one explicitly restores to
                    # _current_classification.hvac_mode (live intent) via the _set_hvac_mode()/
                    # _set_temperature_for_mode() calls below, not _exit_nat_vent()'s
                    # _pre_fan_hvac_mode snapshot — those are different restore-target semantics
                    # that need their own decision, not a mechanical swap. Flagged as a known
                    # follow-up, out of scope for #620.
                    #
                    # Issue #739: this branch bypasses _exit_nat_vent() entirely, so it never
                    # armed the reactivation lockout the way every sibling exit reason in this
                    # method (and, since #696/#755, both COMFORT_FLOOR-class fast-path twins)
                    # already does — the exact "self-complementary at a fixed reading" gap #696
                    # disproved, just unnoticed here because this branch's own bypass makes it
                    # invisible to the _exit_nat_vent()-only AST coverage scan. Confirmed live:
                    # a comfort-floor exit here on 2026-08-22 left indoor still below the
                    # 70-72F daytime band, and the WHF reactivated within a minute on a
                    # ~1F uptick, immediately re-breaching the floor it had just exited to
                    # protect. Set the exit time directly (this branch has no
                    # set_outdoor_exit_time kwarg to pass, same as the AWAY_CEILING branch
                    # below).
                    _vent_floor = exit_decision.vent_floor
                    self._natural_vent_active = False
                    self._nat_vent_outdoor_exit_time = dt_util.now()
                    await self._deactivate_fan(
                        reason=(f"natural vent exit: indoor {indoor:.1f}°F ≤ comfort floor {_vent_floor:.1f}°F")
                    )
                    _LOGGER.info(
                        "Natural vent exit (comfort floor): indoor %.1f°F ≤ floor %.1f°F — restoring %s",
                        indoor,
                        _vent_floor,
                        self._current_classification.hvac_mode if self._current_classification else "unknown",
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
                                reason=f"natural vent comfort-floor exit — restoring {c.hvac_mode} mode",
                            )
                            await self._set_temperature_for_mode(
                                c,
                                reason="natural vent comfort-floor exit — restoring comfort",
                            )
                            self._start_grace_period("automation", trigger="nat_vent_exit_resume")
                    return

                if exit_decision.reason == NatVentExitReason.AWAY_CEILING:
                    _LOGGER.info(
                        "Nat-vent away-mode ceiling exit: indoor %.1fF >= comfort_cool %.1fF while away",
                        indoor,
                        comfort_cool,
                    )
                    self._natural_vent_active = False
                    self._nat_vent_soft_start = False
                    # Issue #757 Phase 6 Step 5 fix: arms the same reactivation-lockout
                    # timer every other exit reason already arms (via _exit_nat_vent()'s
                    # set_outdoor_exit_time — this branch bypasses that choke point, so
                    # it must set the field directly). Without this, the idle-open
                    # reactivation gate (Issue #244/#402/#504) could immediately
                    # re-activate nat-vent moments later with indoor still at/above the
                    # ceiling — decide_nat_vent_gate() has no occupancy awareness and the
                    # ceiling exit changes none of the outdoor/indoor/comfort_heat inputs
                    # it checks, unlike every other exit reason, whose own trigger
                    # condition inherently makes immediate re-entry impossible. Confirmed
                    # live via golden scenario away_natvent_exits_at_comfort_ceiling:
                    # nat-vent never actually stopped, flip-flopping right at the ceiling
                    # it was supposed to protect.
                    self._nat_vent_outdoor_exit_time = dt_util.now()
                    _away_ceiling_result = await self._deactivate_fan(reason="nat-vent ceiling exit (away mode)")
                    # Do NOT pause -- just let away setback handle HVAC
                    # Issue #649: skip emitting a duplicate report for a repeat block within
                    # an already-reported deferral window (same reasoning as _exit_nat_vent()'s
                    # centralized handling — this branch doesn't route through that function,
                    # since away-mode ceiling exit intentionally has no pause/grace machinery).
                    if self._emit_event_callback and _away_ceiling_result is not FanCommandResult.RATE_LIMITED_DUP:
                        self._emit_event_callback(
                            "nat_vent_away_ceiling_exit",
                            {
                                "indoor": indoor,
                                "comfort_cool": comfort_cool,
                                "fan_device": _fan_device_label(self.config),
                            },
                        )
                    return

                if exit_decision.reason == NatVentExitReason.PROACTIVE_FLOOR:
                    time_to_floor = exit_decision.time_to_floor_hr
                    comfort_heat_now = exit_decision.comfort_heat_now
                    k_passive = thermal.get("k_passive")
                    _LOGGER.info(
                        "Natural vent proactive exit: floor predicted in %.2f hr"
                        " < %.1f hr threshold — exiting nat-vent session",
                        time_to_floor,
                        MIN_VIABLE_NAT_VENT_HOURS,
                    )
                    # Issue #641: set_outdoor_exit_time=True — without this, a monitored
                    # sensor left open hands this exit straight into the _paused_by_door
                    # reactivation block below with no lockout armed. Since the instant
                    # reactivation gate (outdoor < indoor - hysteresis) is a *different,
                    # non-predictive* condition than this exit's own time-to-floor
                    # prediction, indoor/outdoor barely move tick-to-tick, so the gate is
                    # almost always still satisfied on the very next tick — guaranteeing
                    # immediate reactivation, another proactive exit, and a repeating
                    # on/off flip-flop (the WHF fast-cycling incident). Arming the same
                    # lockout the outdoor-rise exit already uses closes this gap.
                    #
                    # Issue #649: event emission moved into _exit_nat_vent() itself (via
                    # event_type/event_payload) instead of firing here unconditionally —
                    # centralizes the "was this actually executed or deferred by the #641
                    # rate limiter" check in one place instead of repeating it at every
                    # exit-reason branch.
                    await self._exit_nat_vent(
                        reason=(
                            f"nat-vent proactive floor exit: indoor {indoor:.1f}°F"
                            f" predicted to reach comfort_heat {comfort_heat_now:.1f}°F"
                            f" in {time_to_floor:.2f}h"
                        ),
                        set_outdoor_exit_time=True,
                        event_type="nat_vent_predicted_floor_exit",
                        event_payload={
                            "time_to_floor_hr": round(time_to_floor, 2),
                            "indoor_temp": round(indoor, 1),
                            "comfort_heat": round(comfort_heat_now, 1),
                            "k_passive": round(k_passive, 4),
                            "fan_mode_change": "on→auto",
                            "fan_device": _fan_device_label(self.config),
                            "hvac_mode_restored": (
                                self._current_classification.hvac_mode if self._current_classification else "unknown"
                            ),
                        },
                    )
                    return

                if exit_decision.reason == NatVentExitReason.OUTDOOR_RISE:
                    _LOGGER.info(
                        "Natural vent exit: outdoor %.1f°F >= indoor %.1f°F — airflow reversed",
                        outdoor,
                        indoor,
                    )
                    # set_outdoor_exit_time=True: the original exit reason this lockout was
                    # built for (Issue #115/#411). Issue #641 later extended the same
                    # treatment to PROACTIVE_FLOOR and CEILING_THRESHOLD above/below, once
                    # both were found to hand off into the identical paused-reactivation race.
                    # Issue #690: boundary is now non-strict (>=, via is_outdoor_rise_exit()) —
                    # this text is user-visible on the Debug tab via _last_action_reason.
                    await self._exit_nat_vent(
                        reason=(f"nat vent exit: outdoor {outdoor:.1f}°F >= indoor {indoor:.1f}°F — airflow reversed"),
                        set_outdoor_exit_time=True,
                        event_type="nat_vent_outdoor_rise_exit",
                        event_payload={
                            "outdoor": outdoor,
                            "indoor": indoor,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                    return

                if exit_decision.reason == NatVentExitReason.CEILING_THRESHOLD:
                    # Issue #411: routing this through _exit_nat_vent() is an intentional behavior
                    # change, not a no-op refactor -- this path previously never captured
                    # _pre_pause_mode before pausing (unlike the door-open pause path). It now does,
                    # via _exit_nat_vent()'s sensor-open branch.
                    _LOGGER.info(
                        "Natural vent exit: outdoor %.1f°F > threshold %.1f°F",
                        outdoor,
                        threshold,
                    )
                    # Issue #641: same lockout gap and same fix as the proactive-floor exit
                    # above — the reactivation gate's own ceiling_ok = outdoor < threshold
                    # check is the exact complementary boundary, so outdoor hovering near
                    # threshold can flip-flop this exit against reactivation identically.
                    # Issue #666: event_type was missing here — every sibling exit branch in
                    # this function (OUTDOOR_RISE above, PROACTIVE_FLOOR before it) and
                    # fan_thermostat_check()'s own equivalent outdoor-exit branch all pass
                    # "nat_vent_outdoor_rise_exit" (added project-wide by Issue #649, add1b8f
                    # — this call site was the one sibling it missed). event_type is kept
                    # here for the general event_log/notification machinery even though the
                    # two shadow-diagnostic event registries this originally fed (Issue #647's
                    # nat-vent one, Issue #647's door/window one) have both since been removed
                    # (Phase 6 Steps 4-5, graduation).
                    await self._exit_nat_vent(
                        reason=f"natural vent exit: outdoor {outdoor:.1f}°F > threshold {threshold:.1f}°F",
                        set_outdoor_exit_time=True,
                        event_type="nat_vent_outdoor_rise_exit",
                        event_payload={
                            "outdoor": outdoor,
                            "indoor": indoor,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                    return

            if self._paused_by_door and outdoor is not None and indoor is not None:
                hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
                lockout_s = float(
                    self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S)
                )
                comfort_heat = self._nat_vent_reactivation_floor()

                # Issue #757 Phase 6 Step 5: the lockout check, full-gate reactivation,
                # and soft-start sub-gate below are all decided by a single
                # nat_vent_fsm.transition() call — the FSM's PAUSED_REACTIVATION_LOCKOUT
                # branch re-implements the same is_reactivation_locked_out() check this
                # block used to call directly. The former legacy twin here (a direct
                # is_reactivation_locked_out() call, then _nat_vent_may_reactivate(), then
                # _nat_vent_may_soft_start()) is provably subsumed by the FSM's own
                # PAUSED_REACTIVATION_LOCKOUT transition and has been removed.
                from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
                from .nat_vent_fsm import transition as _nat_vent_transition

                _fsm_current_state = self.nat_vent_lifecycle_state
                _fsm_inputs = self._build_nat_vent_fsm_inputs(
                    now=dt_util.now(), indoor=indoor, outdoor=outdoor, apply_reactivation_floor=True
                )
                _fsm_result = _nat_vent_transition(
                    _fsm_current_state, NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs)
                )
                _to_state = _fsm_result.to_state

                if _to_state == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT:
                    _lockout_check_now = dt_util.now()
                    if self._nat_vent_outdoor_exit_time is not None:
                        elapsed = (_lockout_check_now - self._nat_vent_outdoor_exit_time).total_seconds()
                        _LOGGER.debug(
                            "Nat vent paused-by-door: lockout active — %.0fs elapsed of %.0fs (%.0fs remaining)",
                            elapsed,
                            lockout_s,
                            lockout_s - elapsed,
                        )
                    return

                if _to_state == NatVentLifecycleState.ACTIVE_SOFT_START and not self.config.get(
                    CONF_NAT_VENT_SOFT_START_ENABLED, DEFAULT_NAT_VENT_SOFT_START_ENABLED
                ):
                    # CONF_NAT_VENT_SOFT_START_ENABLED has no FSM-side field — see the
                    # identical caller-side-guard treatment at the idle-open re-entry
                    # site above.
                    _to_state = NatVentLifecycleState.INACTIVE

                if _to_state == NatVentLifecycleState.ACTIVE_FULL_GATE:
                    # Issue #706 (Bug F): route through
                    # _apply_nat_vent_fsm_state_after_activation() — an override
                    # arriving during the await below must not be silently
                    # overwritten by this pre-await _to_state decision.
                    _activation_result = await self._activate_fan(
                        reason=(
                            f"natural vent activated: outdoor {outdoor:.1f}°F"
                            f" < indoor {indoor:.1f}°F − {hysteresis:.1f}°F hysteresis,"
                            f" outdoor ≤ threshold {threshold:.1f}°F"
                        )
                    )
                    self._apply_nat_vent_fsm_state_after_activation(_to_state, _activation_result)

                    from .door_window_fsm import DoorWindowFsmEventKind

                    self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED)
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_reactivated_while_paused",
                            {"outdoor": outdoor, "indoor": indoor, "threshold": threshold},
                        )
                    self._paused_entity = None
                    self._paused_since = None
                    _LOGGER.info(
                        "Natural vent activated: outdoor %.1f°F < indoor %.1f°F − %.1f°F hysteresis,"
                        " outdoor ≤ threshold %.1f°F while paused",
                        outdoor,
                        indoor,
                        hysteresis,
                        threshold,
                    )
                    await self._apply_nat_vent_hvac_state()
                elif _to_state == NatVentLifecycleState.ACTIVE_SOFT_START:
                    # Issue #706 (Bug F): same override-race guard as the
                    # ACTIVE_FULL_GATE branch above.
                    _activation_result = await self._activate_fan(
                        reason=(
                            f"nat-vent soft-start while paused: outdoor {outdoor:.1f}°F at/below"
                            f" indoor {indoor:.1f}°F parity, past today's peak and declining"
                        )
                    )
                    self._apply_nat_vent_fsm_state_after_activation(_to_state, _activation_result)

                    from .door_window_fsm import DoorWindowFsmEventKind

                    self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED)
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_reactivated_while_paused",
                            {"outdoor": outdoor, "indoor": indoor, "threshold": threshold},
                        )
                    self._paused_entity = None
                    self._paused_since = None
                    _LOGGER.info(
                        "Nat-vent soft-start activated while paused: outdoor %.1f°F <= indoor %.1f°F,"
                        " past today's peak %.1f°F by >= %.1f°F",
                        outdoor,
                        indoor,
                        self._outdoor_temp_today_peak or 0.0,
                        PEAK_DECLINE_MARGIN_F,
                    )
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "nat_vent_soft_start_entered",
                            {
                                "outdoor": outdoor,
                                "indoor": indoor,
                                "outdoor_today_peak": self._outdoor_temp_today_peak,
                                "comfort_heat": comfort_heat,
                                "decline_margin_f": PEAK_DECLINE_MARGIN_F,
                            },
                        )
                    await self._apply_nat_vent_hvac_state()
                else:
                    _floor_ok = indoor > comfort_heat
                    _ceiling_ok = outdoor < threshold
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
                return

    async def nat_vent_temperature_check(self, current_temp: float, *, outdoor: float | None) -> None:
        """Thermostat-style cycling: keep indoor near the comfort midpoint during a nat-vent session.

        Called on every thermostat temperature tick via coordinator._async_thermostat_changed.
        Also called as a 5-minute backstop from _thermo_backstop_task().

        When indoor drops to (midpoint - hysteresis) the fan turns off temporarily — the
        nat-vent SESSION stays active (_natural_vent_active=True) and HVAC suppression is
        maintained (restore_hvac=False). When indoor warms back to (midpoint + hysteresis)
        the fan re-engages, subject to the outdoor-warm guard.

        Args:
            current_temp: Current indoor temperature in °F.
            outdoor: Current outdoor temperature in °F, sourced fresh by the caller (Issue
                #561) — mirrors fan_thermostat_check()'s existing convention rather than
                reading self._last_outdoor_temp internally. That cached attribute is only
                refreshed by coordinator._apply_outdoor_temp(), and this cycling check is
                the one place that previously read it directly instead of receiving a
                caller-sourced value, letting it silently go stale for hours (Issue #561
                root cause A) while every other nat-vent gate in this module reads a live
                value from its own caller.
        """
        async with self._decision_pass("nat_vent_temperature_check"):
            if not self._natural_vent_active:
                return

            # Issue #561: a WHF session can be left believing it's still open (e.g. by
            # _reconcile_fan_physical_drift()'s preserve-session path, or by the
            # duplicate-timer-chain defect fixed alongside this) well after the monitored
            # sensors actually closed. Nothing about "cycle the fan back on" is safe to do
            # with every window/door closed — running a whole-house exhaust fan against a
            # sealed building has no cooling benefit and can depressurize the home. Scoped
            # to FAN_MODE_WHOLE_HOUSE/FAN_MODE_BOTH only: FAN_MODE_HVAC's fan-only mode has
            # no separate physical-exterior-airflow requirement (it's the thermostat's own
            # blower, not an exhaust fan) and its reactivation paths are intentionally
            # allowed to re-engage without an open sensor (Issue #134's grace/ceiling path).
            _fan_mode_nvtc = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            if _fan_mode_nvtc in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH) and not self._any_monitored_sensor_open():
                _LOGGER.warning(
                    "Nat-vent session force-closed: _natural_vent_active was True but no"
                    " monitored sensor is open — ending session instead of cycling fan",
                )
                await self._exit_nat_vent(
                    reason="nat-vent session force-closed: session flag was stale, sensors are closed"
                )
                return

            comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
            comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
            hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            sleep_heat = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
            in_sleep_window = _in_sleep_window(dt_util.now(), self.config)
            nat_vent_target = compute_nat_vent_target(
                sleep_heat=sleep_heat,
                in_sleep_window=in_sleep_window,
                comfort_heat_raw=comfort_heat,
                comfort_cool=comfort_cool,
                hysteresis=hysteresis,
            )
            _context = "sleep" if in_sleep_window else "daytime"
            # Hard exit floor — single source of truth in resolve_hard_exit_floor() (Issue #456),
            # also used by fan_thermostat_check()/check_natural_vent_conditions(). In the sleep
            # branch this is one hysteresis step below the cycling-off threshold above, so the
            # fan can cycle off gracefully at sleep_heat before the session ends.
            _hard_floor = resolve_hard_exit_floor(
                comfort_heat_raw=comfort_heat,
                sleep_heat=sleep_heat,
                in_sleep_window=in_sleep_window,
                hysteresis=hysteresis,
            )
            off_threshold = nat_vent_target - hysteresis
            on_threshold = nat_vent_target + hysteresis

            # Issue #757 Phase 6 Step 5: the fast per-tick hard-exit check calls
            # decide_nat_vent_exit() — the SAME 5-check priority chain (comfort-floor,
            # away-ceiling, proactive-floor, outdoor-rise, ceiling-threshold)
            # check_natural_vent_conditions()'s slow loop already uses. A session can end
            # via any of the 5 reasons within this fast loop's own tick cadence, instead of
            # waiting up to 30 min for the slow loop to catch it. The former legacy branch
            # (comfort-floor only, via resolve_hard_exit_floor()/_hard_floor) has been
            # removed.
            thermal_nvtc = self._thermal_model or {}
            nat_vent_delta_nvtc = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
            exit_decision = decide_nat_vent_exit(
                NatVentExitInputs(
                    indoor=current_temp,
                    outdoor=outdoor,
                    comfort_heat_raw=comfort_heat,
                    sleep_heat=sleep_heat,
                    in_sleep_window=in_sleep_window,
                    hysteresis=hysteresis,
                    comfort_cool=comfort_cool,
                    nat_vent_delta=nat_vent_delta_nvtc,
                    occupancy_mode=self._occupancy_mode,
                    thermal_confidence=thermal_nvtc.get("confidence", "none"),
                    k_passive=thermal_nvtc.get("k_passive"),
                    manual_override_active=self._manual_override_active,
                    manual_override_mode=self._manual_override_mode,
                )
            )
            _exit_reason = exit_decision.reason

            # Hard floor (or any of the 5 exit reasons) takes priority over cycling. Sleep
            # window: _hard_floor = sleep_heat - hysteresis (one step below cycling-off
            # threshold), allowing the fan to cycle off gracefully at sleep_heat before the
            # session terminates. Daytime: _hard_floor = comfort_heat (unchanged behaviour).
            if _exit_reason != NatVentExitReason.NONE:
                # Away-mode ceiling exit is intentionally NOT routed through _exit_nat_vent()
                # here either — same documented exclusion as check_natural_vent_conditions()'s
                # own AWAY_CEILING branch and _exit_nat_vent()'s own docstring ("a different
                # concept with no pause/grace state machine"). Mirrors that branch's exact
                # side effects at this new call site.
                if _exit_reason == NatVentExitReason.AWAY_CEILING:
                    _LOGGER.info(
                        "Nat-vent away-mode ceiling exit via temp-check: indoor %.1f°F >= comfort_cool %.1f°F"
                        " while away",
                        current_temp,
                        comfort_cool,
                    )
                    self._natural_vent_active = False
                    self._nat_vent_soft_start = False
                    # Issue #757 Phase 6 Step 5 fix: see check_natural_vent_conditions()'s
                    # matching AWAY_CEILING branch for the full incident this closes —
                    # arms the same reactivation lockout every other exit reason already
                    # arms, since this branch bypasses _exit_nat_vent()'s choke point too.
                    self._nat_vent_outdoor_exit_time = dt_util.now()
                    _away_result = await self._deactivate_fan(reason="nat-vent ceiling exit (away mode) via temp_check")
                    if self._emit_event_callback and _away_result is not FanCommandResult.RATE_LIMITED_DUP:
                        self._emit_event_callback(
                            "nat_vent_away_ceiling_exit",
                            {
                                "indoor": current_temp,
                                "comfort_cool": comfort_cool,
                                "fan_device": _fan_device_label(self.config),
                                "source": "temp_check",
                            },
                        )
                    return

                if _exit_reason == NatVentExitReason.MANUAL_OVERRIDE_CONFLICT:
                    # Issue #714: same bypass as check_natural_vent_conditions()'s own branch —
                    # deliberately not routed through _exit_nat_vent(), whose sensors-closed
                    # path would restore _pre_fan_hvac_mode (captured before nat-vent started)
                    # via _set_hvac_mode(), overwriting the user's just-set mode right back.
                    _LOGGER.info(
                        "Nat-vent exit: manual override to %s conflicts with WHF via temp-check —"
                        " ending free cooling session (indoor %.1f°F)",
                        self._manual_override_mode,
                        current_temp,
                    )
                    await self._stand_down_whf_for_override_conflict(
                        mode=self._manual_override_mode,
                        indoor_temp=current_temp,
                        event_source="temp_check",
                    )
                    return

                # Remaining 4 reasons all route through the canonical _exit_nat_vent() choke
                # point (Issue #418), same as their slow-loop siblings — event_type/payload
                # shape and set_outdoor_exit_time match check_natural_vent_conditions()'s own
                # branches exactly (Issue #641 lockout-arming included for the 3 reasons that
                # can hand off into a sensor-still-open pause).
                if _exit_reason == NatVentExitReason.COMFORT_FLOOR:
                    _floor_for_log = (
                        exit_decision.vent_floor
                        if exit_decision is not None and exit_decision.vent_floor is not None
                        else _hard_floor
                    )
                    _log_detail = f"indoor {current_temp:.1f}°F ≤ floor {_floor_for_log:.1f}°F"
                    _event_type = "nat_vent_comfort_floor_exit"
                    _payload: dict[str, Any] = {
                        "indoor_temp": current_temp,
                        "comfort_heat": _floor_for_log,
                        "source": "temp_check",
                        "fan_device": _fan_device_label(self.config),
                        "fan_mode_change": "on→auto",
                        "hvac_mode_restored": (
                            self._current_classification.hvac_mode if self._current_classification else "unknown"
                        ),
                    }
                    # Issue #696: previously False on the (now-disproven) assumption that
                    # exit and re-entry always check the identical comfort_heat quantity at
                    # a fixed reading, so they could never both be satisfied by the same
                    # indoor temperature. That holds only within a single tick — indoor
                    # legitimately drifts across the floor between ticks in production (a
                    # real 68->69F rise over 5 real minutes triggered this exact gap on
                    # 2026-08-23), so this exit needs the same anti-flap protection as the
                    # other three exit reasons below.
                    _set_outdoor_exit_time = True
                elif _exit_reason == NatVentExitReason.PROACTIVE_FLOOR:
                    _ttf = (exit_decision.time_to_floor_hr if exit_decision is not None else None) or 0.0
                    _cf_now = (exit_decision.comfort_heat_now if exit_decision is not None else None) or _hard_floor
                    _log_detail = (
                        f"floor predicted in {_ttf:.2f}h — indoor {current_temp:.1f}°F -> comfort_heat {_cf_now:.1f}°F"
                    )
                    _event_type = "nat_vent_predicted_floor_exit"
                    _k_passive_nvtc = thermal_nvtc.get("k_passive")
                    _payload = {
                        "time_to_floor_hr": round(_ttf, 2),
                        "indoor_temp": round(current_temp, 1),
                        "comfort_heat": round(_cf_now, 1),
                        "k_passive": round(_k_passive_nvtc, 4) if _k_passive_nvtc is not None else None,
                        "source": "temp_check",
                        "fan_mode_change": "on→auto",
                        "fan_device": _fan_device_label(self.config),
                        "hvac_mode_restored": (
                            self._current_classification.hvac_mode if self._current_classification else "unknown"
                        ),
                    }
                    _set_outdoor_exit_time = True
                else:
                    # OUTDOOR_RISE / CEILING_THRESHOLD — both mapped to the same
                    # nat_vent_outdoor_rise_exit event type as their slow-loop siblings
                    # (see check_natural_vent_conditions()'s own CEILING_THRESHOLD branch
                    # comment, Issue #666, for why CEILING_THRESHOLD reuses this type).
                    _outdoor_log = outdoor if outdoor is not None else 0.0
                    _log_detail = (
                        f"outdoor {_outdoor_log:.1f}°F >= indoor {current_temp:.1f}°F — airflow reversed"
                        if _exit_reason == NatVentExitReason.OUTDOOR_RISE
                        else f"outdoor {_outdoor_log:.1f}°F > ceiling threshold"
                    )
                    _event_type = "nat_vent_outdoor_rise_exit"
                    _payload = {
                        "outdoor": outdoor,
                        "indoor": current_temp,
                        "source": "temp_check",
                        "fan_device": _fan_device_label(self.config),
                    }
                    _set_outdoor_exit_time = True

                _LOGGER.info(
                    "Nat-vent hard exit [%s] via temp-check (%s): %s — ending session",
                    _context,
                    _exit_reason.value,
                    _log_detail,
                )
                await self._exit_nat_vent(
                    reason=f"nat-vent {_exit_reason.value} exit [{_context}] via temp-check: {_log_detail}",
                    set_outdoor_exit_time=_set_outdoor_exit_time,
                    event_type=_event_type,
                    event_payload=_payload,
                )
                return

            # Issue #698 (Decision 3): the on-threshold outdoor-warm reactivation guard
            # delegates to is_outdoor_rise_exit() — the single source of truth already
            # shared by nat_vent_exit.py's OUTDOOR_RISE check and this module's other
            # outdoor-warm comparisons — instead of a hand-duplicated `outdoor >=
            # current_temp` copy. This is a de-duplication bug fix, not new FSM-only
            # behavior — the boundary semantics (non-strict >=) are unchanged.
            #
            # Issue #757 Phase 6 Step 5: the former legacy branch here (the same cycling
            # decision computed inline instead of via decide_nat_vent_cycling()) has been
            # removed.
            cycling_decision = decide_nat_vent_cycling(
                NatVentCyclingInputs(
                    indoor=current_temp,
                    outdoor=outdoor,
                    comfort_heat_raw=comfort_heat,
                    sleep_heat=sleep_heat,
                    in_sleep_window=in_sleep_window,
                    comfort_cool=comfort_cool,
                    hysteresis=hysteresis,
                    fan_hardware_active=self._fan_active,
                )
            )
            _should_be_active = cycling_decision.fan_should_be_active
            _outdoor_rise_blocked = cycling_decision.outdoor_rise_blocked

            if self._fan_active and not _should_be_active:
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
                await self._deactivate_fan(reason="nat_vent_cycling_off", restore_hvac=False, emit_event=False)
                if self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) != FAN_MODE_DISABLED and self._emit_event_callback:
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

            if not self._fan_active and _should_be_active:
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
                await self._activate_fan(reason="nat_vent_cycling_on", emit_event=False)
                if self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED) != FAN_MODE_DISABLED and self._emit_event_callback:
                    self._emit_event_callback(
                        "nat_vent_fan_on",
                        {
                            "indoor_temp": current_temp,
                            "outdoor_temp": outdoor,
                            "on_threshold": on_threshold,
                            "target": nat_vent_target,
                            "fan_device": _fan_device_label(self.config),
                        },
                    )
                return

            if not self._fan_active and not _should_be_active and _outdoor_rise_blocked:
                _LOGGER.info(
                    "Nat-vent cycling: indoor %.1f°F ≥ on_threshold %.1f°F"
                    " but outdoor %.1f°F ≥ indoor — skipping re-activation"
                    " (outdoor-warm exit condition active)",
                    current_temp,
                    on_threshold,
                    outdoor if outdoor is not None else 0.0,
                )
            return

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
            if self._fan_remote_timer_hours is not None:
                # Issue #486: this is the second suppression choke point (fan_thermostat_check
                # never reaches _deactivate_fan() when overridden — it returns "keep" directly),
                # so it needs its own log line mirroring the one in _deactivate_fan() for the
                # absolute-timer behavior to be observable regardless of which guard fires.
                # Issue #585: INFO, not WARNING — the RF-timer mutex is working correctly here,
                # not malfunctioning; WARNING is reserved for actual anomalies/clamps/guard-fires.
                _LOGGER.info(
                    "Fan thermostat cycle-off suppressed by active RF remote timer (%sh):"
                    " trigger=%s indoor=%s outdoor=%s",
                    self._fan_remote_timer_hours,
                    trigger,
                    f"{indoor:.1f}" if indoor is not None else "unavailable",
                    f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                )
            else:
                _LOGGER.debug(
                    "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=keep",
                    trigger,
                    f"{indoor:.1f}" if indoor is not None else "unavailable",
                    f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                    True,
                )
            return

        comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        hysteresis_ftc = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        in_sleep_window = _in_sleep_window(dt_util.now(), self.config)
        sleep_heat_ftc = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))

        # Issue #435 follow-up (architecture-reset Step 2): Check 1 + Check 2's decision
        # logic lives in one pure, independently-tested function
        # (fan_thermostat_decision.decide_fan_thermostat_check). As of Issue #757 Step 2
        # this method no longer calls it directly — it's called from fan_fsm.py's own
        # _transition_on_thermostat_check_tick(), reached via _resolve_fan_fsm_state()
        # below. This method only reconstructs the same reason strings/log lines/side
        # effects the pre-extraction inline code produced — no decision logic here.
        inputs = FanThermostatInputs(
            indoor=indoor,
            outdoor=outdoor,
            comfort_heat_raw=comfort_heat,
            sleep_heat=sleep_heat_ftc,
            in_sleep_window=in_sleep_window,
            hysteresis=hysteresis_ftc,
            natural_vent_active=self._natural_vent_active,
            manual_override_active=self._manual_override_active,
            manual_override_mode=self._manual_override_mode,
        )

        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state(). Per
        # fan_fsm.py's own _transition_on_thermostat_check_tick() docstring, this kind
        # deliberately never projects onto to_state (the real routing below —
        # _exit_nat_vent()/_deactivate_fan() event-type selection, HVAC-restore-vs-pause
        # decisions — stays entirely in the shell); this reads thermostat_outcome off
        # the returned transition.
        from .fan_fsm import FanFsmEventKind

        _thermostat_check_origin_state = self.fan_lifecycle_state

        _thermostat_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.THERMOSTAT_CHECK_TICK,
            origin_state=_thermostat_check_origin_state,
            indoor=indoor,
            outdoor=outdoor,
            hysteresis=hysteresis_ftc,
            in_sleep_window=in_sleep_window,
        )
        outcome = _thermostat_transition.thermostat_outcome

        if outcome is FanThermostatOutcome.KEEP:
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=keep",
                trigger,
                f"{indoor:.1f}" if indoor is not None else "unavailable",
                f"{outdoor:.1f}" if outdoor is not None else "unavailable",
                True,
            )
            return

        if outcome is FanThermostatOutcome.STOP_MANUAL_OVERRIDE_CONFLICT:
            # Issue #714: deliberately not routed through _exit_nat_vent() — its
            # sensors-closed branch restores _pre_fan_hvac_mode (captured before nat-vent
            # started) via _set_hvac_mode(), which has no override awareness of its own
            # and would silently overwrite the user's just-set mode right back.
            _LOGGER.info(
                "Fan thermostat check: manual override to %s conflicts with WHF —"
                " ending free cooling session (indoor %.1f°F)",
                self._manual_override_mode,
                indoor if indoor is not None else 0.0,
            )
            await self._stand_down_whf_for_override_conflict(
                mode=self._manual_override_mode,
                indoor_temp=indoor,
                event_source="fan_thermostat_check",
            )
            return

        if outcome is FanThermostatOutcome.STOP_VIA_NAT_VENT_EXIT:
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
            await self._exit_nat_vent(
                reason=f"nat vent exit (fast loop): {stop_reason}",
                set_outdoor_exit_time=True,
                event_type="nat_vent_outdoor_rise_exit",
                event_payload={"outdoor": outdoor, "indoor": indoor, "fan_device": _fan_device_label(self.config)},
            )
            return

        if outcome is FanThermostatOutcome.STOP_DEACTIVATE:
            # --- Check 1's non-nat-vent stop branch: free-cooling direction guard ---
            # Free cooling requires outdoor cooler than indoor. Once outdoor >= indoor the
            # airflow no longer cools (neutral or reversed) — stop. NOTE: NO hysteresis on
            # the STOP side; the anti-flap hysteresis lives on nat-vent RE-activation
            # (check_natural_vent_conditions). Subtracting it here would kill free cooling
            # ~1°F early — e.g. stop at outdoor 71 / indoor 72 while a favorable gradient
            # remains.
            stop_reason = f"outdoor {outdoor:.1f}°F >= indoor {indoor:.1f}°F (free cooling gone)"
            _LOGGER.debug(
                "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=%s",
                trigger,
                f"{indoor:.1f}",
                f"{outdoor:.1f}",
                True,
                f"stop:{stop_reason}",
            )
            # Issue #620: previously a bare _deactivate_fan() call — restore_hvac defaulted to
            # True with no check of live sensor state, the exact pre-#418 pattern documented
            # above at STOP_VIA_NAT_VENT_EXIT. Route through the same single choke point so a
            # monitored sensor still open at this stop is paused, not silently restored into.
            # This branch fires for ANY CA-owned fan (nat-vent session or not, e.g. a
            # min-runtime-cycle fan) — unlike STOP_VIA_NAT_VENT_EXIT, which only fires when a
            # nat-vent session is active. Only label the event nat-vent-specific when one
            # genuinely was; otherwise emit the same generic event _deactivate_fan() itself
            # would have (test_stops_non_natvent_fan_without_natvent_event).
            _was_nat_vent = self._natural_vent_active
            if _was_nat_vent:
                _event_type = "nat_vent_outdoor_rise_exit"
                _event_payload: dict[str, Any] = {
                    "outdoor": outdoor,
                    "indoor": indoor,
                    "fan_device": _fan_device_label(self.config),
                }
            else:
                _event_type = "fan_deactivated"
                _event_payload = {
                    "reason": f"fan thermostat check — {stop_reason}",
                    "fan_mode": self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
                    "fan_device": _fan_device_label(self.config),
                }
            # Issue #641: same treatment as STOP_VIA_NAT_VENT_EXIT above and
            # check_natural_vent_conditions()'s OUTDOOR_RISE exit — this branch's own
            # docstring already documents it as "the exact same boundary condition,"
            # just reached via the tick-level Check 1 path instead of the 30-min cycle.
            # Arm the same lockout so outdoor hovering near the indoor boundary can't
            # flip-flop this exit against reactivation any more than its sibling can.
            await self._exit_nat_vent(
                reason=f"fan thermostat check — {stop_reason}",
                set_outdoor_exit_time=True,
                event_type=_event_type,
                event_payload=_event_payload,
            )
            return

        # outcome is STOP_COOLED_TO_FLOOR
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
        vent_floor_ftc = _resolve_vent_floor(inputs)
        stop_reason = f"indoor {indoor:.1f}°F ≤ comfort floor {vent_floor_ftc:.1f}°F (cooled to floor)"
        _LOGGER.debug(
            "Fan thermostat check: trigger=%s indoor=%s outdoor=%s active=%s decision=%s",
            trigger,
            f"{indoor:.1f}",
            f"{outdoor:.1f}" if outdoor is not None else "unavailable",
            True,
            f"stop:{stop_reason}",
        )
        # Issue #620: previously a bare _deactivate_fan() call after manually clearing
        # _natural_vent_active — restore_hvac defaulted to True with no check of live sensor
        # state (the exact incident this issue is about: WHF stops here, and the pre-suppression
        # HVAC mode gets restored into a window that's still open). _exit_nat_vent() already
        # clears _natural_vent_active/_nat_vent_soft_start itself and checks the sensor before
        # deciding whether to restore or pause — same single choke point as the two branches
        # above. This branch fires for ANY CA-owned fan (nat-vent or not, e.g. min-runtime
        # cycling) — only label the event nat-vent-specific when one genuinely was active,
        # mirroring the same distinction made for STOP_DEACTIVATE above.
        _was_nat_vent_floor = self._natural_vent_active
        if _was_nat_vent_floor:
            _event_type = "nat_vent_comfort_floor_exit"
            _event_payload: dict[str, Any] = {
                "indoor_temp": indoor,
                "comfort_heat": vent_floor_ftc,
                "fan_mode_change": "on→auto",
                "fan_device": _fan_device_label(self.config),
                "hvac_mode_restored": (
                    self._current_classification.hvac_mode if self._current_classification else "unknown"
                ),
            }
        else:
            _event_type = "fan_deactivated"
            _event_payload = {
                "reason": f"fan thermostat check — {stop_reason}",
                "fan_mode": self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
                "fan_device": _fan_device_label(self.config),
            }
        # Issue #755: previously omitted set_outdoor_exit_time here on the same
        # "self-complementary at a fixed reading" reasoning already disproven for
        # nat_vent_temperature_check()'s COMFORT_FLOOR exit (Issue #696) — indoor
        # legitimately drifts across vent_floor_ftc between ticks in production, so exit
        # and re-entry are not actually self-complementary. This tick-level check runs on
        # every sensor update (not just the 30-min cycle #696 hit), so the exposure window
        # is at least as wide. Arm the same 300s lockout as this method's other two exit
        # branches above, so a sensor-still-open pause here gets the same anti-flap
        # protection at the idle-open re-entry path check_natural_vent_conditions() already
        # consults (fixed by #696).
        await self._exit_nat_vent(
            reason=f"fan thermostat check — {stop_reason}",
            set_outdoor_exit_time=True,
            event_type=_event_type,
            event_payload=_event_payload,
        )

    async def reconcile_fan_on_startup(
        self,
        *,
        indoor: float | None,
        outdoor: float | None,
        thermostat_fan_running: bool,
        any_sensor_open: bool,
        trigger: str = "startup",
        recent_hvac_session_ended: bool = False,
        remote_timer_provenance: tuple[float, float] | None = None,
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
            trigger:               Which of the 4 call sites invoked this reconcile — used only
                                    in log/reason strings (Issue #530). This method is called from
                                    a genuine HA restart (``"ha_restart"``), the periodic 30-min
                                    untracked-fan backstop (``"backstop_30min"``), a live
                                    thermostat hvac_action transition (``"thermostat_state_change"``),
                                    and every grace-period expiry (``"post_grace_expiry"``) — the
                                    reason string previously hardcoded "startup reconcile" for all
                                    four, which read as a phantom HA restart when none of the other
                                    three triggers fired. Defaults to ``"startup"`` for any caller
                                    that doesn't pass one explicitly.
            recent_hvac_session_ended: True when this reconcile was triggered by a thermostat
                                    ``hvac_action`` transition directly out of ``cooling``/
                                    ``heating`` into ``fan`` — the normal internal post-compressor
                                    blower phase, not an out-of-band fan appearance. When True,
                                    the "no-fan" branch below releases any stranded WHF
                                    suppression but never force-writes an HVAC mode change,
                                    since a legitimate active/just-finished cooling or heating
                                    session must never be interrupted by this reconcile
                                    (Issue #618).
            remote_timer_provenance: ``(remaining_seconds, token_hours)`` from the coordinator's
                                    live read of the RF remote entity's still-unexpired
                                    re-announced timer token (Issue #677), or None. Pre-filtered
                                    to "still valid" by the caller — this method does not
                                    re-check expiry. None (the default, and the case for every
                                    pre-#677 caller) leaves this method's behavior byte-for-byte
                                    identical to before Issue #677.
        """
        # Issue #561: reentrancy guard — this method is called from 4 independent sites
        # with no coordination between them. Two overlapping calls previously each
        # independently reached the "adopt-on" branch below and each started their own
        # self-rescheduling backstop timer, leaving one permanently uncancellable and
        # ticking forever in parallel with the other (root cause of the WHF activating
        # while all windows were closed, hours after the legitimate session had ended).
        # A concurrent call simply skips this tick — the caller that lost the race gets
        # another chance on its own next trigger.
        if self._reconcile_fan_in_progress:
            _LOGGER.debug("reconcile_fan_on_startup: skipping — already in progress (trigger=%s)", trigger)
            return
        self._reconcile_fan_in_progress = True
        try:
            await self._reconcile_fan_on_startup_locked(
                indoor=indoor,
                outdoor=outdoor,
                thermostat_fan_running=thermostat_fan_running,
                any_sensor_open=any_sensor_open,
                trigger=trigger,
                recent_hvac_session_ended=recent_hvac_session_ended,
                remote_timer_provenance=remote_timer_provenance,
            )
        finally:
            self._reconcile_fan_in_progress = False

    def _nat_vent_lockout_status(self, now: datetime) -> tuple[float, float] | None:
        """Elapsed/remaining seconds of the reactivation lockout, or ``None`` if not armed.

        Shared by every call site that needs to log lockout timing (Issue #790) —
        one place to compute ``(elapsed, remaining)`` from ``_nat_vent_outdoor_exit_time``
        instead of each site re-deriving the same subtraction.
        """
        if self._nat_vent_outdoor_exit_time is None:
            return None
        lockout_s = float(self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S))
        elapsed = (now - self._nat_vent_outdoor_exit_time).total_seconds()
        return elapsed, lockout_s

    async def _reconcile_fan_on_startup_locked(
        self,
        *,
        indoor: float | None,
        outdoor: float | None,
        thermostat_fan_running: bool,
        any_sensor_open: bool,
        trigger: str,
        recent_hvac_session_ended: bool = False,
        remote_timer_provenance: tuple[float, float] | None = None,
    ) -> None:
        """Body of reconcile_fan_on_startup(), run under its reentrancy guard (Issue #561)."""
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        archetype = fan_mode

        # Issue #677: a live-re-announced, still-unexpired RF remote timer takes priority
        # over the adopt-on/turn-off nat-vent logic below. Without this, the natural
        # hardware shutoff hours later is read as a fresh, unexplained manual action by
        # on_fan_turned_off() (a brand-new 3-hour "manual" grace, blocking nat-vent
        # reactivation despite favorable outdoor air) — this restores the SAME grace CA
        # would have already been honoring had the restart not wiped its in-memory
        # bookkeeping, sized to the timer's remaining duration. Only acts when the fan is
        # still physically running; if it already read off, provenance is provided but
        # deliberately ignored here — falls through to the unchanged turn-off/no-op path.
        # When remote_timer_provenance is None (every pre-#677 caller, and every restart
        # with no live remote timer token), this branch is skipped entirely and the rest of
        # this method is byte-for-byte unchanged from before Issue #677.
        if remote_timer_provenance is not None and thermostat_fan_running:
            _remaining_seconds, _token_hours = remote_timer_provenance
            _LOGGER.info(
                "Fan reconcile: live RF remote timer still valid at restart (%sh token,"
                " %.0fs remaining) and fan is still running — re-arming manual override"
                " instead of treating this as an unexplained fan state (archetype=%s)",
                _token_hours,
                _remaining_seconds,
                archetype,
            )
            self.handle_fan_manual_override(
                fan_before="on",
                fan_after="on",
                duration_override=_remaining_seconds,
                remote_timer_hours=_token_hours,
                is_remote_event=True,
            )
            return

        if not thermostat_fan_running:
            # Issue #733: thermostat_fan_running is a live hass.states.get() read taken
            # by the caller — if CA itself issued a fan command in the last 30s (e.g.
            # _do_startup_coalesce() activating nat-vent for an open window a moment
            # before calling reconcile in the same pass), the physical entity may not
            # have reported the new state back to HA yet. Without this guard, that stale
            # read looked identical to "the fan is genuinely off" and clobbered the
            # just-made decision — the WHF kept running the physical relay while CA's own
            # flags said inactive, so nothing thermostatically managed it afterward.
            # Reuses the exact guard _reconcile_fan_physical_drift() already applies to
            # the same class of stale-read-vs-fresh-command race (automation.py ~9319).
            if self._is_recent_fan_command_callback and self._is_recent_fan_command_callback(threshold_seconds=30.0):
                _LOGGER.info(
                    "Fan reconcile: thermostat_fan_running=False but a fan command was issued"
                    " in the last 30s — deferring to that fresh command instead of this stale"
                    " physical read (archetype=%s, trigger=%s)",
                    archetype,
                    trigger,
                )
                return

            # Fan is off — ensure CA flags are clean (defence in depth), and release any
            # stranded WHF HVAC suppression left over from a session that ended without
            # a matching _deactivate_fan() call (Issue #405). Without this, a nat-vent
            # cycling-off (restore_hvac=False, by design) followed by a coalesce boundary
            # that observes the fan already off would clear _natural_vent_active here but
            # leave _pre_fan_hvac_mode stranded non-None forever, permanently blocking
            # every future HVAC write via _whf_owns_hvac() with no recovery path.
            #
            # Issue #731 Phase 5: deliberately NOT routed through _resolve_fan_fsm_state()
            # (fan_fsm.py's STARTUP_RECONCILE event kind), even though this call site is
            # STARTUP_RECONCILE's real production origin per fan_fsm.py's own docstring.
            # This write group spans TWO independent lifecycles: _fan_active/_fan_on_since
            # are fan-lifecycle fields, but _natural_vent_active/_nat_vent_soft_start belong
            # to nat-vent's own lifecycle (fan_fsm.py's module docstring explicitly notes
            # natural_vent_active is read here only as a cross-lifecycle boolean, not a
            # field this FSM derives). _apply_fan_fsm_state() only ever writes
            # _fan_active/_fan_override_active/_fan_min_runtime_active/_pre_fan_hvac_mode
            # (None-clear) — it does not and should not own _natural_vent_active/
            # _nat_vent_soft_start. Routing this write group through the dispatcher would
            # silently drop the nat-vent-side flag changes once _fan_fsm_authoritative
            # flips True (the FSM branch would apply only the fan-side quarter of this
            # reconcile decision), a correctness regression waiting for a future phase.
            # Stays a direct write.
            self._fan_active = False
            self._fan_on_since = None
            self._natural_vent_active = False
            self._nat_vent_soft_start = False
            decision = "no-fan"
            _LOGGER.info(
                "Fan reconcile: thermostat_fan_running=%s nat_vent_eligible=%s decision=%s archetype=%s",
                thermostat_fan_running,
                False,
                decision,
                archetype,
            )
            # Issue #523: never restore a suppressed HVAC mode while paused for an open
            # door/window — matches the invariant #418 already established for the
            # nat-vent-exit path ("_paused_by_door=True while _deactivate_fan()'s default
            # restore_hvac=True restores HVAC anyway" contradicts pause semantics). Without
            # this guard, a WHF session left _pre_fan_hvac_mode stranded from before a
            # restart could silently re-command HVAC on right after the classification block
            # above correctly paused it for an open window.
            #
            # Issue #618: this branch is exactly where #405's fix and #523's pause guard could
            # combine to re-strand the snapshot — when _paused_by_door is True at the moment
            # this fires, restore_hvac=False, and without release_suppression=True the old code
            # left _pre_fan_hvac_mode stranded non-None again, indistinguishable from the #405
            # case this branch was written to fix. release_suppression=True always releases
            # ownership here (this IS a genuine session end — thermostat_fan_running is False),
            # while restore_hvac still correctly gates whether a mode gets written right now.
            #
            # Issue #618 (part 2): recent_hvac_session_ended additionally blocks the write when
            # this reconcile was triggered by a normal cooling/heating -> fan hvac_action
            # transition (the thermostat's own post-compressor blower phase) — never force HVAC
            # off because of that transition. The 2026-08-10 incident: this exact branch force-
            # set HVAC to "off" 5 minutes after the AC had legitimately started cooling, because
            # it misread the blower phase as "the WHF stopped."
            await self._deactivate_fan(
                reason=f"{trigger} reconcile — fan confirmed off, releasing any stranded HVAC suppression",
                restore_hvac=not self._paused_by_door and not recent_hvac_session_ended,
                release_suppression=True,
            )
            return

        # Evaluate nat-vent eligibility
        # Issue #417: folded into the shared _nat_vent_may_reactivate() gate instead of a
        # 5th hand-rolled copy — this hand-rolled version was also missing the sleep-aware
        # floor and the ceiling dormancy check the shared gate already accounts for.
        #
        # Issue #757 Phase 6 Step 5: this eligibility question is decided by
        # nat_vent_fsm.transition() instead of a direct legacy call (removed), restricted
        # to its ACTIVE_FULL_GATE outcome only — this call site (like
        # handle_door_window_open()) has never modeled soft-start adoption, so an
        # FSM-produced ACTIVE_SOFT_START result is treated the same as "not eligible" here.
        # The FSM's current_state is deliberately forced to INACTIVE rather than read from
        # self.nat_vent_lifecycle_state: this call site is a pure entry-gate question
        # ("should today's already-physically-running fan be trusted as CA-owned nat-vent"),
        # independent of whatever _natural_vent_active/_paused_by_door happen to read at
        # reconcile time (e.g. restored-but-stale post-restart state) — exactly the same
        # always-fresh-compute semantics _nat_vent_may_reactivate() has. Forcing INACTIVE
        # routes transition() through _transition_from_inactive(), the FSM branch that calls
        # the same two pure functions (decide_nat_vent_gate(), decide_nat_vent_soft_start_gate())
        # in the same priority order the legacy call here always used.
        from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
        from .nat_vent_fsm import transition as _nat_vent_transition

        # Issue #790: previously hardcoded paused_by_door=False here, on the claim
        # (nat_vent_reactivation_lockout.py's docstring) that this method "runs at
        # most once per restart/30-min backstop, structurally incapable of sub-minute
        # repeats" — false for 2 of its 4 real triggers (thermostat_state_change,
        # post_grace_expiry are event-driven, not cadence-bound, and can fire
        # sub-minute). Passing the real value is safe uniformly across all 4 triggers:
        # _nat_vent_outdoor_exit_time is never persisted across restarts (state.py),
        # so is_reactivation_locked_out() can only fire when a real exit was armed
        # earlier in this same running process — no restart-staleness hazard.
        _fsm_inputs = self._build_nat_vent_fsm_inputs(
            now=dt_util.now(), indoor=indoor, outdoor=outdoor, paused_by_door=self._paused_by_door
        )
        _fsm_result = _nat_vent_transition(
            NatVentLifecycleState.INACTIVE, NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs)
        )
        if _fsm_result.to_state == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT:
            _lockout_status = self._nat_vent_lockout_status(dt_util.now())
            _elapsed, _lockout_s = _lockout_status if _lockout_status is not None else (0.0, 0.0)
            _LOGGER.warning(
                "Fan reconcile (%s): reactivation lockout suppressed adoption — %.0fs remaining of %.0fs",
                trigger,
                _lockout_s - _elapsed,
                _lockout_s,
            )
        nat_vent_eligible = (
            fan_mode != FAN_MODE_DISABLED
            and any_sensor_open
            and _fsm_result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE
        )

        if nat_vent_eligible:
            # Adopt the running fan as CA-owned nat-vent
            decision = "adopt-on"
            # Issue #600: this method has 4 independent callers (ha_restart,
            # backstop_30min, post_grace_expiry, thermostat_state_change) with no
            # coordination between sequential (non-overlapping) triggers — the Issue #561
            # mutex only blocks concurrent re-entry and explicitly lets a second sequential
            # trigger through ("the caller that lost the race gets another chance on its
            # own next trigger"). Without this guard, two triggers landing minutes apart
            # each redundantly re-adopt an already-owned session: duplicate "Fan activated"
            # Activity Record entries for one real event, and _fan_on_since silently
            # jumping forward each time, understating the displayed session duration.
            # Issue #731 Phase 5: deliberately NOT routed through _resolve_fan_fsm_state() —
            # same cross-lifecycle reasoning as the no-fan branch above (this write group
            # also sets _natural_vent_active, which _apply_fan_fsm_state() does not own).
            # Stays a direct write.
            _already_adopted = self._natural_vent_active
            self._fan_active = True
            if self._fan_on_since is None:
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
            if _already_adopted:
                _LOGGER.debug(
                    "%s reconcile re-confirmed an already-adopted nat-vent session —"
                    " skipping duplicate Activity Record entry",
                    trigger,
                )
                return
            # Issue #402 follow-up: this branch previously left zero activity-log trace of
            # the fan being adopted as CA-owned at startup — the fan silently starts being
            # managed with no record of why, unlike the turn-off branch below which does
            # emit a fan_deactivated event. Record the adoption the same way.
            _adopt_reason = (
                f"{trigger} reconcile — fan already running, indoor {indoor:.1f}°F,"
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
            _turn_off_reason = f"{trigger} reconcile — fan running without CA warrant"

            # Issue #446: reconcile_fan_on_startup() is called from 4 different sites
            # (startup coalesce, 30-min backstop, thermostat state-change, post-grace-expiry)
            # with no rate limit — a fan that keeps re-appearing as "unwarranted" (e.g. a
            # thermostat's own circulation schedule CA cannot durably override with one
            # command) previously triggered a full correction every single call, producing
            # repeated near-identical Activity Report spam every few minutes. Cooldown
            # mirrors the existing _last_override_detected_time dedup pattern above.
            _cooldown_window = timedelta(minutes=5)
            _now = dt_util.now()
            _last_correction = self._last_unwarranted_fan_correction_at
            if _last_correction is not None and (_now - _last_correction) < _cooldown_window:
                _LOGGER.info(
                    "Unwarranted-fan correction suppressed — already corrected at %s,"
                    " within the 5-minute cooldown (likely a recurring condition CA cannot"
                    " durably override with one command, e.g. a thermostat circulation schedule)",
                    _last_correction.isoformat(),
                )
                return
            self._last_unwarranted_fan_correction_at = _now

            # Ensure flags are correct before deactivating — _exit_nat_vent()'s internal
            # _deactivate_fan() call is a no-op unless _fan_active reads True.
            #
            # Issue #731 Phase 5: deliberately NOT routed through _resolve_fan_fsm_state().
            # Unlike the two branches above, this IS a single raw fan-lifecycle flag (no
            # cross-lifecycle field alongside it) — but it exists purely to make the
            # immediately-following _exit_nat_vent()/_deactivate_fan() call see an owned
            # fan; _deactivate_fan() itself already runs its own ACTIVATE/DEACTIVATE_REQUESTED
            # dispatch (Site A) and owns the real hardware-confirmed _fan_active write (Site
            # B). Wiring this transient pre-flag through the dispatcher too would just be a
            # second, throwaway derivation of the same axis moments before the real one.
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
            # Issue #790: arms the same reactivation lockout every other _exit_nat_vent()
            # call site arms — previously exempted on the same debunked "runs at most
            # once per restart/30-min backstop" cadence claim as the check-side bypass
            # fixed above, but 2 of this method's 4 triggers are event-driven and can
            # fire sub-minute. Without this, a turn-off issued from THIS call site left
            # no lockout timer for a subsequent reconcile call to check.
            await self._exit_nat_vent(reason=_turn_off_reason, set_outdoor_exit_time=True)

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
        # Issue #721: investigated re-sourcing this to _dispatched_paused_by_door.
        # Rejected — every real production writer keeps the mirror in lockstep
        # (Finding 1's write-site audit), but 14+ existing tests
        # (test_resume_from_pause.py, test_manual_override_respect.py, etc.) set
        # engine._paused_by_door = True directly, bypassing the dispatcher, then
        # call this method immediately — the same direct-attribute-assignment
        # fixture convention whose incompatibility with dispatcher-mirror reads
        # was already discovered and reverted once for the FSM input builders
        # (see _dispatched_paused_by_door's declaration comment in __init__).
        # Re-sourcing here would reproduce that exact regression. Stays canonical.
        if not self._paused_by_door:
            return
        _LOGGER.info("Manual HVAC override detected during door/window pause")

        from .door_window_fsm import DoorWindowFsmEventKind

        # Issue #594 Phase R, Step 0 / #757 Phase 6 Step 4: routed through the shared,
        # unconditionally FSM-authoritative dispatcher — MANUAL_OVERRIDE_DURING_PAUSE
        # lands on NORMAL from PAUSED_ACTIVE/PAUSED_IDLE, or GRACE from
        # PAUSED_DURING_GRACE (grace left running).
        self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE)
        self._paused_entity = None
        self._paused_since = None
        self._pre_pause_mode = None
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        # Start confirmation period — wait before formally accepting the override
        self.start_override_confirmation(
            source="pause",
            event_kind=_OGFEventKind.MANUAL_OVERRIDE_DURING_PAUSE,
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
        # Issue #721: see handle_manual_override_during_pause()'s matching comment —
        # same test-fixture-breaking reason to keep this canonical, not dispatched.
        if not self._paused_by_door:
            return None

        _LOGGER.info("User resumed HVAC from door/window pause via dashboard")

        from .door_window_fsm import DoorWindowFsmEventKind

        # Issue #594 Phase R, Step 0 / #757 Phase 6 Step 4: routed through the shared,
        # unconditionally FSM-authoritative dispatcher — same shape as
        # handle_manual_override_during_pause()'s call. DASHBOARD_RESUME lands
        # unconditionally on GRACE from either origin state, matching the unconditional
        # _start_grace_period() call below regardless of origin.
        self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.DASHBOARD_RESUME)
        self._paused_entity = None
        self._paused_since = None
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

        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        _trigger = "dashboard_resume"
        if self._start_grace_period_action("manual", trigger=_trigger):
            self._resolve_override_grace_fsm_state(kind=_OGFEventKind.DASHBOARD_RESUME)
        return restore_mode

    def _legacy_set_grace_flags(self, trigger: str) -> None:
        """The 2-line flag computation ``_start_grace_period()`` always used to perform
        inline (Issue #664). Issue #757 Phase 6 Step 3: no longer passed as a ``legacy``
        closure to ``_resolve_override_grace_fsm_state()`` — that dispatcher is now
        unconditionally FSM-authoritative and never calls this method. Its only
        remaining caller is the dead-but-kept-for-reference ``_confirm_override()``
        (see its own docstring) — not called from any real production call site.
        """
        self._grace_active = True
        self._grace_protects_override = trigger in _GRACE_TRIGGERS_PROTECTING_OVERRIDE

    def _start_grace_period_action(
        self, source: str, trigger: str = "", duration_override: float | None = None
    ) -> bool:
        """Real side effect only: cancel any prior timer, resolve duration/should_notify,
        schedule the real grace-expiry ``async_call_later``, and write every
        non-FSM-derived bookkeeping field (Issue #664). Deliberately does NOT write
        ``_grace_active``/``_grace_protects_override`` — the two flags
        ``override_grace_fsm.py`` models — so a caller can genuinely choose which
        computation determines those two, exclusively, via
        ``_resolve_override_grace_fsm_state()``, instead of this method silently
        overwriting whatever the dispatcher just decided.

        Returns True if grace actually started (a real timer now exists), False if grace
        is disabled (``decide_grace_start()`` returned None) — callers must only proceed
        to write the 2 derived flags (via legacy or the dispatcher) when this is True;
        a disabled grace period must never claim ``_grace_active=True``.
        """
        self._cancel_grace_timers()

        now = dt_util.now()
        manual_duration = (
            duration_override
            if (source == "manual" and duration_override is not None)
            else self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        )
        grace = decide_grace_start(
            source=source,
            manual_duration_seconds=manual_duration,
            manual_should_notify=self.config.get(CONF_MANUAL_GRACE_NOTIFY, True),
            automation_duration_seconds=self.config.get(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS),
            automation_should_notify=self.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True),
            now=now,
        )
        if grace is None:
            return False  # Grace period disabled

        duration = int((grace.at - now).total_seconds())
        should_notify = grace.should_notify

        self._last_resume_source = source
        self._last_grace_trigger = trigger
        self._grace_duration_seconds = duration
        self._grace_end_time = grace.at.isoformat()

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
        return True

    def _start_grace_period(self, source: str, trigger: str = "", duration_override: float | None = None) -> None:
        """Start a grace period after HVAC is resumed.

        Args:
            source: "manual" for user-initiated overrides,
                    "automation" for Climate Advisor resumptions.
            trigger: Distinct per-callsite label (e.g. "fan_manual_override", "fan_off",
                "dashboard_resume") — logged/emitted for observability, and, as of Issue
                #530, also determines whether this grace protects a real override:
                membership in ``_GRACE_TRIGGERS_PROTECTING_OVERRIDE`` sets
                ``self._grace_protects_override``, which ``coordinator._check_orphaned_grace()``
                reads to decide whether an override-less grace is a bug (a real override
                grace whose flag vanished without going through ``cancel_override()``) or
                expected (fan-off cooldown, window-close resume, etc. never set an override
                flag in the first place). Every callsite must pass an explicit trigger.
            duration_override: When set (seconds), bypasses the configured manual
                grace duration and uses this value instead. Used by RF-remote timer
                selections (Issue #486) to make the grace period last exactly as
                long as the user asked at the physical remote. Only meaningful when
                source == "manual"; ignored otherwise.

        Issue #664: thin wrapper — real work lives in ``_start_grace_period_action()``.
        This wrapper is for every caller whose ``trigger`` was historically NOT a modeled
        ``OverrideGraceFsmEventKind`` (fan-off, window-close, nat-vent-exit,
        drift-correction, etc.). The ~3 callers whose trigger IS modeled
        (``fan_manual_override``, ``dashboard_resume``, ``override_confirmed``) call
        ``_start_grace_period_action()`` directly instead and route the flag-write
        through ``_resolve_override_grace_fsm_state()`` — see ``handle_fan_manual_override()``,
        ``resume_from_pause()``, ``_confirm_override()``.

        Issue #672: this wrapper's own "every other trigger" callers now ALSO route
        through the dispatcher, under ``UNPROTECTED_GRACE_STARTED`` — closing the gap
        where those triggers set real production state (``_grace_active=True``,
        never protecting) but the FSM had no way to ever find out. One shared edit here
        covers all of them (fan-off, window-close, nat-vent-exit, drift-correction),
        instead of touching each of their individual call sites.
        """
        if self._start_grace_period_action(source, trigger, duration_override):
            from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

            self._resolve_override_grace_fsm_state(kind=_OGFEventKind.UNPROTECTED_GRACE_STARTED)
            # Issue #672: a second, DISTINCT event from _start_grace_period_action()'s own
            # "grace_started" (which also fires for the 3 protecting triggers, via their
            # own direct dispatcher call sites — reusing it here would wrongly feed
            # UNPROTECTED_GRACE_STARTED for those too). This wrapper is, by construction
            # per its own docstring, called only for the non-protecting triggers, so this
            # event type is exactly and only ever "an unprotected grace genuinely started"
            # — feeds coordinator.py's diagnostic FSM tracker via
            # _OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP, closing the live production=idle/
            # active_unprotected vs fsm=idle/none gap this whole fix targets.
            if self._emit_event_callback:
                self._emit_event_callback(
                    "unprotected_grace_started",
                    {"source": source, "trigger": trigger},
                )

    def _on_grace_expired(self, source: str, duration: int, should_notify: bool) -> None:
        """Handle grace period expiry — re-check sensors then clear state.

        Extracted from the inner callback in ``_start_grace_period`` so it can
        also be invoked from ``_reschedule_grace_timer`` after an HA restart.
        """
        # Issue #530: snapshot whether this grace was tied to an RF-remote timer BEFORE
        # clear_manual_override() (called in every branch below) wipes
        # _fan_remote_timer_hours. If so, arm a short settle window: a fan-off report
        # arriving shortly after is the tail of this SAME timer boundary — the timer's
        # own hardware side completing a few seconds after CA's software clock — not a
        # fresh, independent event. See on_fan_turned_off().
        if source == "manual" and getattr(self, "_fan_remote_timer_hours", None) is not None:
            self._timer_boundary_settle_until = dt_util.now() + timedelta(seconds=TIMER_BOUNDARY_SETTLE_SECONDS)
            _LOGGER.debug(
                "RF-timer-linked grace expiring — arming %ss timer-boundary settle window",
                TIMER_BOUNDARY_SETTLE_SECONDS,
            )

        # Issue #660 Step 8: captured once, before any branch's _cancel_grace_timers()
        # runs (that call clears _grace_active, which door_window_lifecycle_state
        # reads) — nothing between here and each branch's own _cancel_grace_timers()
        # call touches door/window pause/grace state, so one capture at the top
        # covers all 3 branches. The shared dispatcher always reads
        # self.door_window_lifecycle_state *live* by default, which would be wrong
        # here specifically since grace will already be cleared by the time it runs
        # — this is the one call site that needs the origin_state override.
        from .door_window_fsm import DoorWindowFsmEventKind

        _origin_state = self.door_window_lifecycle_state

        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        def _dispatch_override_grace_expired() -> None:
            """Real action + single dispatcher call for GRACE_TIMER_EXPIRED (Issue #664),
            shared by all 3 branches below. Origin state doesn't need explicit capture
            (unlike the door/window dispatcher above) — the action calls never touch
            _override_confirm_pending/_grace_active/_grace_protects_override themselves,
            so a live read after them still reflects the correct pre-transition state.
            """
            if self._override_confirm_pending:
                self._clear_override_confirm_action()
            self._cancel_grace_timers_action()
            self._resolve_override_grace_fsm_state(kind=_OGFEventKind.GRACE_TIMER_EXPIRED)

        # If within planned window period, sensors open is expected — just clear grace
        if self._is_within_planned_window_period():
            _LOGGER.info(
                "%s grace expired during planned window period — sensors open as expected, clearing grace",
                source,
            )
            _dispatch_override_grace_expired()
            self._resolve_door_window_pause_flags(
                kind=DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                origin_state=_origin_state,
            )
            self._clear_manual_override_active("grace_expired")
            if self._request_refresh_callback:
                self._request_refresh_callback()
            if self._post_grace_fan_check_callback:
                self._post_grace_fan_check_callback()
            # Shadow-feed completion (door/window Step 1b's documented residual): this
            # was the one GRACE_TIMER_EXPIRED outcome with no event emit at all, so it
            # never fed the shadow door/window FSM. Reuses the same "grace_expired"
            # event type the sibling branches below already emit — no wiring changes
            # to _DOOR_WINDOW_GRACE_EXPIRY_EVENT_TYPES needed — with a payload key
            # distinguishing this outcome from a real sensor re-check.
            if self._emit_event_callback:
                self._emit_event_callback(
                    "grace_expired", {"source": source, "within_planned_window": True, "re_paused": False}
                )
            return

        # If any contact sensor is still open, re-pause instead of clearing
        if self._any_monitored_sensor_open():
            _LOGGER.info(
                "%s grace expired but sensor(s) still open — re-pausing HVAC",
                source,
            )
            _dispatch_override_grace_expired()
            # Issue #660 Step 8 / #757 Phase 6 Step 4: this applies the FSM's RE_PAUSE
            # outcome (including its own nested nat-vent reactivation gate check) to
            # _paused_by_door/_paused_with_hvac_already_off/_grace_active BEFORE
            # _re_pause_for_open_sensor() is scheduled below, so that task can select
            # its action by reading the already-applied flag instead of independently
            # recomputing the gate and re-writing them.
            self._resolve_door_window_pause_flags(
                kind=DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
                origin_state=_origin_state,
            )
            self._clear_manual_override_active("grace_expired")
            if self._request_refresh_callback:
                self._request_refresh_callback()
            if self._post_grace_fan_check_callback:
                self._post_grace_fan_check_callback()
            if self._emit_event_callback:
                self._emit_event_callback("grace_expired", {"source": source, "re_paused": True})
            self.hass.async_create_task(self._re_pause_for_open_sensor())
            return

        # Issue #483: at natural expiry, check whether automation's current decision has
        # independently converged on the same HVAC mode the override already produced.
        # If so, adopt it — this is the safe, minimum-bar location for this feature (see
        # docs/grace-periods-spec.md and Issue #483's scope notes for why apply_classification()
        # additionally carries the pre-expiry version of this same check). Adoption skips the
        # "your override has expired" notification, which would otherwise misleadingly imply
        # the user's setting was reverted when it in fact matches what automation wants anyway.
        _adopted = source == "manual" and self._override_matches_current_decision(self._current_classification)
        _adopted_mode: str | None = None
        _adopted_source: str = "normal"
        if _adopted:
            # Capture before clear_manual_override() nulls these out below. getattr:
            # some tests construct AutomationEngine via object.__new__() and populate
            # only the attributes they need (mirrors the existing _apply_comfort_band
            # defensive-read pattern) — _manual_override_source may not exist on older
            # partial stubs that predate Issue #483.
            _adopted_mode = self._manual_override_mode
            _adopted_source = getattr(self, "_manual_override_source", None) or "normal"
            _LOGGER.info(
                "Manual override adopted — automation decision now matches (grace period ended "
                "cleanly): mode=%s source=%s duration_seconds=%d",
                _adopted_mode,
                _adopted_source,
                duration,
            )

        _dispatch_override_grace_expired()
        self._resolve_door_window_pause_flags(
            kind=DoorWindowFsmEventKind.GRACE_TIMER_EXPIRED,
            origin_state=_origin_state,
        )
        self._clear_manual_override_active("adopted_matching_decision" if _adopted else "grace_expired")
        if self._request_refresh_callback:
            self._request_refresh_callback()
        if self._post_grace_fan_check_callback:
            self._post_grace_fan_check_callback()

        if _adopted:
            if self._emit_event_callback:
                self._emit_event_callback(
                    "override_adopted",
                    {"mode": _adopted_mode, "source": _adopted_source, "duration_seconds": duration},
                )
            return

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

    def _legacy_clear_grace_flags(self) -> None:
        """The 2-line flag-clear ``_cancel_grace_timers()`` always used to perform inline
        (Issue #664). Issue #757 Phase 6 Step 3: no longer passed as a ``legacy`` closure
        to ``_resolve_override_grace_fsm_state()`` — that dispatcher is now unconditionally
        FSM-authoritative and never calls this method. Still used directly, unchanged, by
        ``_cancel_grace_timers()`` itself for every caller whose event isn't override/grace
        FSM-modeled (``cleanup()``, the internal cancel-prior-timer call inside
        ``_start_grace_period_action()``, and every door/window call site).

        **Issue #709: door/window's dispatcher no longer writes ``_grace_active`` at all.**
        Prior to #709 this method's docstring claimed the redundant door/window write was
        "proven safe" as a same-value coexistence — that claim turned out to be false: the
        two writers could disagree for real, transiently, whenever ``resume_from_pause()``/
        ``handle_all_doors_windows_closed()`` ran the door/window dispatch before the real
        grace-start action, with genuine ``await`` points in between. ``_apply_door_window_fsm_state()``
        was fixed to stop writing this flag; ``_resolve_override_grace_fsm_state()``'s FSM
        write (or this method, for the non-FSM-modeled callers above) is the flag's sole
        writer everywhere.
        """
        self._grace_active = False
        self._grace_protects_override = False

    def _cancel_grace_timers_action(self) -> None:
        """Real side effect only: cancel any pending timer handles and clear the
        non-FSM-derived bookkeeping fields (Issue #664). Deliberately does NOT write
        ``_grace_active``/``_grace_protects_override`` — see ``_legacy_clear_grace_flags()``.
        """
        if self._manual_grace_cancel:
            self._manual_grace_cancel()
            self._manual_grace_cancel = None
        if self._automation_grace_cancel:
            self._automation_grace_cancel()
            self._automation_grace_cancel = None
        self._grace_end_time = None  # Bug 2 fix (Issue #321): prevent stuck-at-0 display
        self._last_resume_source = None
        self._last_grace_trigger = None

    def _cancel_grace_timers(self) -> None:
        """Cancel any active grace period timers.

        Issue #664: thin wrapper — real work lives in ``_cancel_grace_timers_action()``.
        Used unconditionally by every caller EXCEPT the real override/grace
        GRACE_TIMER_EXPIRED/OVERRIDE_CANCELLED/OVERRIDE_SUPERSEDED call sites, which call
        ``_cancel_grace_timers_action()`` directly and route the flag-clear through
        ``_resolve_override_grace_fsm_state()`` instead — see ``_on_grace_expired()``,
        ``coordinator._check_orphaned_grace()``.
        """
        self._cancel_grace_timers_action()
        self._legacy_clear_grace_flags()

    async def _re_pause_for_open_sensor(self) -> None:
        """Re-pause HVAC because a sensor is still open when grace expired."""
        async with self._decision_pass("_re_pause_for_open_sensor"):
            if self._is_within_planned_window_period():
                _LOGGER.info(
                    "Skipping re-pause — within planned window period (windows recommended)",
                )
                return
            outdoor = self._last_outdoor_temp
            comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
            nat_vent_delta = float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA))
            indoor = self._get_indoor_temp_f()
            nat_vent_threshold = comfort_cool + nat_vent_delta

            # Issue #708 / Issue #757 Phase 6 Step 5: this reactivation decision is
            # checked independent of door/window's authority, matching every other wired
            # nat-vent decision site (handle_door_window_open's idle-open re-entry,
            # check_natural_vent_conditions's comfort-ceiling re-entry and paused-by-door
            # reactivation, reconcile_fan_on_startup's adopt gate) — none of those gate
            # on door/window's authority either. This keeps the two concerns
            # independent: door/window's authority only governs how its own pause/grace
            # flags get derived (via _resolve_door_window_pause_flags() below, which
            # is correct either way — PAUSED_NAT_VENT_REACTIVATED always transitions
            # to NORMAL from any origin state, so it doesn't matter whether the flags
            # it's dispatched against already agree with this decision); nat-vent's
            # own FSM governs its own reactivation question.
            from .nat_vent_fsm import NatVentFsmEvent, NatVentFsmEventKind
            from .nat_vent_fsm import transition as _nat_vent_transition

            # The FSM's current_state is forced to INACTIVE, matching the other
            # two "pure entry-gate question" sites (handle_door_window_open,
            # reconcile_fan_on_startup) rather than read from
            # self.nat_vent_lifecycle_state: this legacy call never consulted the
            # reactivation lockout (unlike the paused-by-door reactivation site in
            # check_natural_vent_conditions), and never modeled soft-start entry
            # (no _nat_vent_may_soft_start() call at this site) — an
            # FSM-produced ACTIVE_SOFT_START result is treated the same as "not
            # eligible," matching this site's pre-existing scope exactly.
            # hysteresis=0.0 and paused_by_door=False mirror the legacy call's own
            # omissions (see _nat_vent_may_reactivate()'s docstring: this is one of the
            # 2-of-5 callers that never applied hysteresis, and this site never
            # consulted _paused_by_door either).
            _fsm_inputs = self._build_nat_vent_fsm_inputs(
                now=dt_util.now(),
                indoor=indoor,
                outdoor=outdoor,
                hysteresis=0.0,
                paused_by_door=False,
                apply_reactivation_floor=True,
            )
            _fsm_result = _nat_vent_transition(
                NatVentLifecycleState.INACTIVE,
                NatVentFsmEvent(kind=NatVentFsmEventKind.TICK, inputs=_fsm_inputs),
            )
            _reactivates = _fsm_result.to_state == NatVentLifecycleState.ACTIVE_FULL_GATE

            if _reactivates:
                nat_vent_reason = (
                    f"grace expired — nat-vent: outdoor {outdoor:.1f}°F < indoor {indoor:.1f}°F,"
                    f" outdoor {outdoor:.1f}°F ≤ {nat_vent_threshold:.1f}°F"
                )
                await self._activate_fan(reason=nat_vent_reason)
                self._natural_vent_active = True

                from .door_window_fsm import DoorWindowFsmEventKind

                # Issue #676: this branch is structurally identical to
                # check_natural_vent_conditions()'s own paused-reactivation branch
                # (automation.py ~3860-3953), which already routes through
                # _resolve_door_window_pause_flags() and emits
                # "nat_vent_reactivated_while_paused" — the only event type wired to the
                # shadow door/window FSM's PAUSED_NAT_VENT_REACTIVATED transition
                # (coordinator.py's _DOOR_WINDOW_NAT_VENT_REACTIVATED_EVENT_TYPES). This
                # call site was never updated to match when that mechanism was built
                # (Issues #647/#660/#668), so the shadow FSM never left paused_idle when
                # grace expired into this specific reactivation path — production
                # correctly reactivated nat-vent and cleared its own pause flags below,
                # but the shadow FSM had no event telling it to do the same, sticking at
                # paused_idle indefinitely (confirmed live, 2026-08-18: 20+ minutes of
                # stuck "production=normal fsm=paused_idle" disagreement).
                self._resolve_door_window_pause_flags(
                    kind=DoorWindowFsmEventKind.PAUSED_NAT_VENT_REACTIVATED,
                )
                # Issue #637 (Phase R Step 1, violation #3) / #657 / #757 Phase 6 Step 4
                # correction: _paused_entity/_paused_since are NOT part of
                # _apply_door_window_fsm_state()'s 2-field derivation (see its own
                # docstring), so the dispatcher call above never clears them regardless
                # of origin state — direct writes here, matching the sibling
                # reactivation branches in check_natural_vent_conditions(). (A prior
                # version of this comment claimed the dispatcher call made this
                # redundant — restored after tests/test_resume_from_pause.py's
                # test_repause_clears_paused_by_door_on_nat_vent_reactivation caught the
                # gap live.)
                self._paused_entity = None
                self._paused_since = None
                await self._apply_nat_vent_hvac_state()
                _LOGGER.info(
                    "Re-check after grace: nat-vent conditions met — outdoor %.1f°F < indoor %.1f°F,"
                    " outdoor ≤ %.1f°F, band stays armed",
                    outdoor,
                    indoor,
                    nat_vent_threshold,
                )
                if self._emit_event_callback:
                    # Issue #676: emitted BEFORE "sensor_opened" below, deliberately —
                    # feeds the shadow door/window FSM's PAUSED_NAT_VENT_REACTIVATED
                    # transition; unrelated to and consumed independently of
                    # "sensor_opened" (Activity Report rendering, etc.). Ordered first
                    # so "sensor_opened" — the event the golden-scenario harness's
                    # production_outcome_at() already maps to this branch's
                    # "natural_ventilation" outcome — remains the last-emitted event at
                    # this instant, unchanged from pre-fix behavior; this new event type
                    # has no outcome mapping of its own (diagnostic-only) and would
                    # otherwise shadow it.
                    self._emit_event_callback(
                        "nat_vent_reactivated_while_paused",
                        {"outdoor": outdoor, "indoor": indoor, "threshold": nat_vent_threshold},
                    )
                    self._emit_event_callback(
                        "sensor_opened",
                        {
                            "entity": "re-check",
                            "result": "natural_ventilation",
                            "outdoor_temp": outdoor,
                            "indoor_temp": indoor,
                            "threshold": nat_vent_threshold,
                        },
                    )
                return

            # Issue #757 Phase 6 Step 4 correction: this briefly called only the
            # action half (_pause_for_door_window_action()) here, on the theory that
            # _on_grace_expired()'s dispatcher call had already derived the pause
            # flags before scheduling this task, making a second SENSOR_OPENED
            # dispatch redundant. That theory holds for the real production call
            # chain (_on_grace_expired() -> this task), but this method is also
            # called directly in tests (and, in principle, could be reached without
            # that precondition) — calling the full _pause_for_door_window() wrapper
            # instead is safe either way: from an already-paused origin its own
            # SENSOR_OPENED dispatch just no-ops (see door_window_fsm.py's
            # _transition_from_paused: "guards on paused_by_door already being True
            # and no-ops"), and it independently derives the correct flags when they
            # weren't already set. Caught live by
            # tests/test_resume_from_pause.py's test_re_pause_when_hvac_already_off.
            await self._pause_for_door_window(
                entity_label="re-check",
                reason="grace expired — door/window still open, re-pausing",
                notify_message=("Grace period expired but a door/window is still open. HVAC has been paused again."),
                notify_type="grace_repause",
            )

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
        # Issue #744 / Phase 6 Step 6: routes through the pure occupancy_fsm.py pair —
        # same logging, same events, same HVAC writes, driven by the returned decision.
        # The legacy inline branch this replaced is gone (graduated) — see
        # _resolve_occupancy_away_vacation_fsm_state()/_apply_occupancy_away_vacation_decision().
        _av_decision = self._resolve_occupancy_away_vacation_fsm_state(mode="away")
        await self._apply_occupancy_away_vacation_decision("away", _av_decision)

    async def handle_occupancy_home(self) -> None:
        """Handle someone returning — restore comfort."""
        self._occupancy_mode = OCCUPANCY_HOME
        # Issue #744 / Phase 6 Step 6: see handle_occupancy_away()'s matching comment.
        _home_decision = self._resolve_occupancy_home_fsm_state()
        await self._apply_occupancy_home_decision(_home_decision)

    async def handle_occupancy_vacation(self) -> None:
        """Handle vacation mode — apply deeper setback for extended away."""
        self._occupancy_mode = OCCUPANCY_VACATION
        # Issue #744 / Phase 6 Step 6: see handle_occupancy_away()'s matching comment.
        _vac_decision = self._resolve_occupancy_away_vacation_fsm_state(mode="vacation")
        await self._apply_occupancy_away_vacation_decision("vacation", _vac_decision)

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

        # Issue #498: occupancy/override/paused/nat-vent checks below now route through the
        # single shared gate (desired_state.decide_scheduled_band_gate()) also used by
        # apply_classification()/handle_morning_wakeup()/handle_pre_cool() — see that
        # function's docstring for the full rationale. Two behavior corrections land with
        # this refactor: (1) bedtime no longer has its own outdoor-vs-sleep_cool comparison
        # to decide whether nat-vent should keep running — that comparison could hand off to
        # AC prematurely even while outdoor was still well below indoor and the fan was doing
        # useful, cheaper work (finding #7); the engine's own per-tick
        # check_natural_vent_conditions() already manages the session correctly (outdoor-
        # reversal exit, sleep-aware cycling target), so bedtime just defers entirely while
        # nat-vent/WHF is active. (2) bedtime now also defers when paused by an open door/
        # window, which it never checked before (finding #11).
        # Issue #620: reconcile _paused_by_door with live sensor state first — a sensor open
        # since before any event-driven pause path ran would otherwise never be seen.
        await self._sync_paused_by_door_with_live_sensors()
        _gate = decide_scheduled_band_gate(
            occupancy_mode=self._occupancy_mode,
            manual_override_active=self._manual_override_active,
            paused_by_door=self._paused_by_door,
            natural_vent_active=self._natural_vent_active,
            whf_owns_hvac=self._whf_owns_hvac(),
        )

        # Issue #85: vacation/away already has a setback — don't override it with sleep temps.
        # Issue #505: bedtime-specific sleep temps are still skipped, but the setback itself
        # must be actively reapplied here too — grace expiry landing inside the sleep window
        # routes here instead of apply_classification(), and the same "already active"
        # assumption can be false (e.g. a manual override cleared moments earlier).
        if _gate == ScheduledBandGate.DEFER_OCCUPANCY:
            _LOGGER.info(
                "Bedtime skipped — %s mode (reapplying setback instead of sleep temps)",
                self._occupancy_mode,
            )
            if self._emit_event_callback and not self._recent_duplicate(
                "bedtime_setback_skipped", ("occupancy", self._occupancy_mode)
            ):
                self._emit_event_callback(
                    "bedtime_setback_skipped",
                    {"reason": "occupancy", "occupancy": self._occupancy_mode},
                )
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "occupancy"
            if self._occupancy_mode == OCCUPANCY_VACATION:
                await self.handle_occupancy_vacation()
            else:
                await self.handle_occupancy_away()
            return

        if _gate == ScheduledBandGate.DEFER_OVERRIDE:
            _LOGGER.info(
                "Bedtime setback skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            _skip_dup = self._recent_duplicate("bedtime_setback_skipped", ("manual_override",))
            if self._emit_event_callback and not _skip_dup:
                self._emit_event_callback("bedtime_setback_skipped", {"reason": "manual_override"})
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "manual_override"
            return

        if _gate == ScheduledBandGate.DEFER_PAUSED:
            _LOGGER.info("Bedtime setback skipped — paused by open door/window")
            if self._emit_event_callback and not self._recent_duplicate("bedtime_setback_skipped", ("paused_by_door",)):
                self._emit_event_callback("bedtime_setback_skipped", {"reason": "paused_by_door"})
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "paused_by_door"
            return

        # Issue #498: capture whether the fan is user-overridden BEFORE clear_manual_
        # override() runs. clear_manual_override() unconditionally calls
        # clear_fan_override(), which resets _fan_override_active to False as a side
        # effect — reading self._fan_override_active AFTER that call always sees it
        # already cleared, silently defeating the "not self._fan_override_active" guard
        # below (this was true of handle_bedtime()'s guard even before this fix; it was
        # never actually exercised because it read a flag clear_manual_override() had
        # just zeroed). Same capture-before-clear pattern already used at :3648 for
        # _manual_override_mode/_manual_override_source.
        _fan_was_overridden = self._fan_override_active

        _LOGGER.info("Bedtime setback: clearing any pending override state before applying sleep setback")
        self.clear_manual_override(reason="bedtime")

        c = self._current_classification
        if not c:
            if self._today_record is not None:
                self._today_record.setback_skipped_reason = "no_classification"
            # No sleep target available — deactivate fan/economizer unless nat-vent/WHF owns it.
            if _gate != ScheduledBandGate.DEFER_NAT_VENT:
                if self._fan_active and not _fan_was_overridden:
                    await self._deactivate_fan(reason="bedtime — no classification")
                    self._natural_vent_active = False
                    self._nat_vent_soft_start = False
                if self._economizer_active:
                    await self._deactivate_economizer(outdoor_temp=0)
            return

        _sleep_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=True,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )

        # Issue #498: bedtime no longer decides for itself whether nat-vent/WHF "can still
        # reach" the sleep target — it just leaves an active session alone (skips fan
        # deactivation) and always still computes/emits the sleep band exactly as before.
        # The low-level _whf_owns_hvac() choke-point guard inside _set_temperature() (used by
        # _apply_comfort_band() below) continues to silently no-op the actual write for
        # WHF/BOTH — same as it always has; FAN_MODE_HVAC coexists with the compressor so its
        # write goes through normally. This is deliberately NOT a second copy of that
        # archetype decision — bedtime only decides "touch the fan or not."
        if _gate == ScheduledBandGate.DEFER_NAT_VENT:
            _LOGGER.info("Bedtime: nat-vent/WHF session active — leaving fan alone")
        else:
            if self._fan_active and not _fan_was_overridden:
                await self._deactivate_fan(reason="bedtime — nat-vent not active")
                self._natural_vent_active = False
                self._nat_vent_soft_start = False
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
        if (
            _gate == ScheduledBandGate.DEFER_NAT_VENT
            and self._emit_event_callback
            and not self._recent_duplicate("nat_vent_bedtime_continue", (_fan_device_label(self.config),))
        ):
            self._emit_event_callback(
                "nat_vent_bedtime_continue",
                {
                    "fan_device": _fan_device_label(self.config),
                    "outdoor_temp": self._last_outdoor_temp,
                    "sleep_cool": _sleep_band.ceiling,
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
                _comfort_heat_f = self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT)
                self._today_record.setback_depth_f = abs(_comfort_heat_f - _sleep_band.floor)
                self._today_record.setback_was_adaptive = False
            elif _sleep_band.active == "ceiling":
                self._today_record.setback_cool_applied_f = _sleep_band.ceiling
                _comfort_cool_f = self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)
                self._today_record.setback_depth_f = abs(_comfort_cool_f - _sleep_band.ceiling)
                self._today_record.setback_was_adaptive = False
        await self._apply_comfort_band(
            _sleep_band,
            reason=f"bedtime — sleep band [{_sleep_band.floor:.0f}/{_sleep_band.ceiling:.0f}]",
        )

    async def handle_pre_cool(self, indoor_temp: float | None, nat_vent_just_closed: bool) -> str:
        """Apply overnight pre-cool setpoint to bank cold thermal mass before a hot day.

        Fires at the pre-cool trigger time (nat-vent close + delay, or wake_time - 4h), gated by
        ``resolve_pre_cool_modifier()`` — either a warming trend tonight, or tomorrow's forecast
        classifying hot on its own (Issue #558, closing the plateau gap where consecutive hot
        days with no further warming got no overnight banking at all).
        Suppressed when nat-vent already brought indoor to or below the target.
        Returns a short status string for logging.
        """
        c = self._current_classification
        _modifier = resolve_pre_cool_modifier(c, self.config) if c else None
        if _modifier is None:
            _LOGGER.info(
                "Pre-cool trigger fired: skipped — no warming trend and tomorrow not hot (modifier=%s)",
                getattr(c, "setback_modifier", "n/a"),
            )
            return "skipped: not eligible"

        # Issue #593: computed up front (pure config + modifier, no gate/indoor dependency)
        # so the DEFER_NAT_VENT "active_session" branch below can show the target it's
        # deferring to, not just a generic "already achieved" label that never applied here.
        pre_cool_target = compute_pre_cool_target(self.config, _modifier)

        # Issue #498: occupancy/override/paused/nat-vent checks below now route through the
        # single shared gate (desired_state.decide_scheduled_band_gate()) also used by
        # apply_classification()/handle_bedtime()/handle_morning_wakeup() — see that
        # function's docstring for the full rationale. Pre-cool previously had ZERO nat-vent/
        # WHF awareness of its own, relying entirely on the low-level _whf_owns_hvac() choke-
        # point guard inside _set_temperature() to silently no-op the write — this makes that
        # deferral explicit and observable, and also adds a paused-by-door check it never had
        # (finding #11). Deferring to an active nat-vent/WHF session loses nothing: pre-cool's
        # own compute_pre_cool_target() floors at sleep_heat + hysteresis, the exact anchor
        # nat_vent_temperature_check() already cycles the fan around during the sleep window —
        # free cooling is already doing at least as much work as pre-cool's own AC ceiling
        # would (Issue #498 finding #8a).
        # Issue #620: reconcile _paused_by_door with live sensor state first — a sensor open
        # since before any event-driven pause path ran would otherwise never be seen.
        await self._sync_paused_by_door_with_live_sensors()
        _gate = decide_scheduled_band_gate(
            occupancy_mode=self._occupancy_mode,
            manual_override_active=self._manual_override_active,
            paused_by_door=self._paused_by_door,
            natural_vent_active=self._natural_vent_active,
            whf_owns_hvac=self._whf_owns_hvac(),
        )

        # Issue #505: reapply the setback here too, same rationale as apply_classification()/
        # handle_bedtime() — "already active" is not guaranteed once an override has cleared.
        if _gate == ScheduledBandGate.DEFER_OCCUPANCY:
            _LOGGER.info(
                "Pre-cool skipped — %s mode (reapplying setback instead of pre-cool)",
                self._occupancy_mode,
            )
            if self._occupancy_mode == OCCUPANCY_VACATION:
                await self.handle_occupancy_vacation()
            else:
                await self.handle_occupancy_away()
            return f"skipped: {self._occupancy_mode}"

        if _gate == ScheduledBandGate.DEFER_OVERRIDE:
            _LOGGER.info(
                "Pre-cool skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            return "skipped: manual override"

        if _gate == ScheduledBandGate.DEFER_PAUSED:
            _LOGGER.info("Pre-cool skipped — paused by open door/window")
            return "skipped: paused_by_door"

        if _gate == ScheduledBandGate.DEFER_NAT_VENT:
            _fan_cfg_pc = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            _LOGGER.info(
                "Pre-cool deferred — nat-vent/WHF session active (fan_mode=%s); free cooling already"
                " chasing at least as cold a target",
                _fan_cfg_pc,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "pre_cool_suppressed_nat_vent",
                    {
                        "modifier": _modifier,
                        "reason": "active_session",
                        "target": pre_cool_target,
                        "indoor": indoor_temp,
                    },
                )
            if _fan_cfg_pc in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                return "suppressed: nat-vent/WHF session active"
            # FAN_MODE_HVAC: fall through and arm the pre-cool ceiling, fan keeps running.

        sleep_cool = float(self.config.get(CONF_SLEEP_COOL, DEFAULT_SLEEP_COOL))
        sleep_heat_floor = float(self.config.get(CONF_SLEEP_HEAT, DEFAULT_SLEEP_HEAT))
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        raw_target = sleep_cool + _modifier  # negative modifier lowers the ceiling below sleep_cool
        floor = sleep_heat_floor + hysteresis

        _LOGGER.info(
            "Pre-cool trigger fired: indoor=%s°F, target=%.1f°F, modifier=%.1f (sleep_cool=%.1f, floor=%.1f)",
            f"{indoor_temp:.1f}" if indoor_temp is not None else "unknown",
            pre_cool_target,
            _modifier,
            sleep_cool,
            floor,
        )

        if raw_target < floor:
            _LOGGER.warning(
                "Pre-cool target %.1f°F below floor %.1f°F (sleep_heat=%.1f + hysteresis=%.1f); clamped to %.1f°F",
                raw_target,
                floor,
                sleep_heat_floor,
                hysteresis,
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
                    {
                        "indoor": indoor_temp,
                        "target": pre_cool_target,
                        "modifier": _modifier,
                        "reason": "achieved",
                    },
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
            reason=f"pre-cool — thermal mass banking (target {pre_cool_target:.0f}°F)",
        )

        if self._emit_event_callback:
            self._emit_event_callback(
                "pre_cool_applied",
                {
                    "target": pre_cool_target,
                    "modifier": _modifier,
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
            reason=f"pre-cool [{_sleep_band.floor:.0f}/{pre_cool_target:.0f}]",
        )
        return f"applied: {pre_cool_target:.1f}°F"

    async def handle_morning_wakeup(self, indoor_temp: float | None = None) -> None:
        """Restore comfort for morning wake-up."""
        # Issue #498: occupancy/override/paused/nat-vent checks below now route through the
        # single shared gate (desired_state.decide_scheduled_band_gate()) also used by
        # apply_classification()/handle_bedtime()/handle_pre_cool() — see that function's
        # docstring for the full rationale. This closes the 06:30 wake-up bug directly: the
        # fan-deactivation call below now skips whenever nat-vent/WHF owns HVAC (previously
        # unconditional — confirmed live: "Fan deactivated -- morning wakeup" -> "Comfort
        # band applied (cool)" -> nat-vent had to re-engage a minute later to correct it) and
        # skips whenever the user is overriding the fan (previously missing that check
        # entirely, unlike handle_bedtime()). It also now defers when paused by an open door/
        # window, which it never checked before (finding #11).
        # Issue #620: reconcile _paused_by_door with live sensor state first — a sensor open
        # since before any event-driven pause path ran would otherwise never be seen.
        await self._sync_paused_by_door_with_live_sensors()
        _gate = decide_scheduled_band_gate(
            occupancy_mode=self._occupancy_mode,
            manual_override_active=self._manual_override_active,
            paused_by_door=self._paused_by_door,
            natural_vent_active=self._natural_vent_active,
            whf_owns_hvac=self._whf_owns_hvac(),
        )

        # Issue #85: skip comfort restore when nobody is home.
        if _gate == ScheduledBandGate.DEFER_OCCUPANCY:
            _LOGGER.info(
                "Morning wakeup skipped — occupancy mode is '%s'",
                self._occupancy_mode,
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "morning_wakeup_skipped",
                    {"reason": "occupancy", "occupancy": self._occupancy_mode},
                )
            return

        if _gate == ScheduledBandGate.DEFER_OVERRIDE:
            _LOGGER.info(
                "Morning wakeup skipped — manual override active (mode=%s since %s)",
                self._manual_override_mode,
                self._manual_override_time,
            )
            if self._emit_event_callback:
                self._emit_event_callback("morning_wakeup_skipped", {"reason": "manual_override"})
            return

        if _gate == ScheduledBandGate.DEFER_PAUSED:
            _LOGGER.info("Morning wakeup skipped — paused by open door/window")
            if self._emit_event_callback:
                self._emit_event_callback("morning_wakeup_skipped", {"reason": "paused_by_door"})
            return

        # Issue #498: capture whether the fan is user-overridden BEFORE clear_manual_
        # override() runs — it unconditionally calls clear_fan_override(), which resets
        # _fan_override_active to False as a side effect. Reading self._fan_override_active
        # after that call would always see it already cleared, silently defeating the "not
        # overridden" guard below (this is precisely how the reported 06:30 bug's fix could
        # have looked complete while still doing nothing — same capture-before-clear
        # pattern already used at :3648 and now in handle_bedtime()).
        _fan_was_overridden = self._fan_override_active

        _LOGGER.info("Morning wakeup: clearing any pending override state before restoring comfort")
        self.clear_manual_override(reason="morning_wakeup")

        # Deactivate fan if still running from overnight — unless the user is overriding it
        # or nat-vent/WHF currently owns HVAC (Issue #498 fix — see docstring note above).
        if _gate != ScheduledBandGate.DEFER_NAT_VENT and self._fan_active and not _fan_was_overridden:
            await self._deactivate_fan(reason="morning wakeup — resetting fan state")

        c = self._current_classification
        if not c:
            return

        # Morning pre-cool overshoot guard: warn if indoor is below comfort_heat (heat may fire)
        _current_indoor = indoor_temp
        _comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
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

        # Issue #498: wakeup doesn't decide the WHF-vs-HVAC-fan archetype question itself —
        # it just leaves an active nat-vent/WHF session's fan alone (handled above) and
        # always still computes/emits the daytime band exactly as it did before this fix.
        # The low-level _whf_owns_hvac() choke-point guard inside _set_temperature() (used
        # by _apply_comfort_band() below) silently no-ops the actual write for WHF/BOTH;
        # FAN_MODE_HVAC coexists with the compressor so its write goes through normally.
        #
        # Issue #711: "leaving fan alone" used to mean literally nothing re-checked the
        # session until the next periodic/temp-change tick (up to 5 min later, via the
        # backstop timer) — by which point indoor could already have drifted past the new,
        # tighter daytime thresholds (this function has just armed a *new* comfort band
        # above). A session that was healthily cycling under the looser sleep-window band
        # could coast straight through the graceful daytime cycle-off point and hit the hard
        # exit floor before daytime rules ever got a look at it (confirmed live, 2026-08-21).
        # nat_vent_temperature_check() already contains the correct sleep-aware cycling/exit
        # logic for exactly this situation — call it now, at the moment the new band takes
        # effect, instead of waiting for an unrelated later tick. Guarded on indoor_temp not
        # being None: that function's signature requires a real reading, and this same "guard
        # not available" case already causes the morning pre-cool overshoot check above to
        # skip entirely.
        if _gate == ScheduledBandGate.DEFER_NAT_VENT:
            _LOGGER.info("Morning wakeup: nat-vent/WHF session active — re-checking against daytime band")
            if indoor_temp is not None:
                await self.nat_vent_temperature_check(indoor_temp, outdoor=self._last_outdoor_temp)

        # Arm the daytime comfort band — waking up exits the sleep window.
        _wakeup_band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=self._occupancy_mode,
            in_sleep_window=False,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        # Issue #591 (found via further investigation of wakeup_preserves_whf_manual_override):
        # handle_morning_wakeup() itself is reachable from multiple overlapping trigger paths,
        # same as apply_classification()/handle_bedtime() — that scenario has it invoked twice
        # within the same wake-up. Without a dedup guard here, the second call's unconditional
        # morning_wakeup marker survived even after hvac_write_blocked_whf_active (below) was
        # deduped, becoming the new trailing "last observable decision" and masking the correct
        # outcome. Windowed (not permanent) — this event legitimately fires once per real
        # wake-up, hours apart.
        _wakeup_sig = (c.hvac_mode, round(_wakeup_band.floor, 2), round(_wakeup_band.ceiling, 2), _wakeup_band.active)
        if self._emit_event_callback and not self._recent_duplicate("morning_wakeup", _wakeup_sig, window_seconds=600):
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

    def _any_monitored_sensor_open(self) -> bool:
        """Return True if any monitored door/window sensor is currently open.

        Single choke point for this check (Issue #561) — previously re-derived inline at
        each call site (``_sensor_check_callback and _sensor_check_callback()``). Callers
        that need to know whether a nat-vent session is still legitimately justified by an
        open sensor, rather than only by the ``_natural_vent_active``/``_fan_active`` flags,
        should read this directly instead of trusting those flags as a proxy — flags can
        outlive the sensor state that justified them (see ``nat_vent_temperature_check()``'s
        reactivation branch and ``_reconcile_fan_physical_drift()``'s preserve-session branch).
        """
        return bool(self._sensor_check_callback and self._sensor_check_callback())

    async def _exit_nat_vent(
        self,
        *,
        reason: str,
        set_outdoor_exit_time: bool = False,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> FanCommandResult:
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
            set_outdoor_exit_time: Records ``_nat_vent_outdoor_exit_time`` for the
                paused-by-door reactivation lockout. Originally only the outdoor-reversal
                exit passed True (Issue #411); Issue #641 extended this to the
                proactive-floor and ceiling-threshold exits too, after both were found to
                exhibit the identical flip-flop against the instant reactivation gate
                whenever they hand off into a sensor-still-open pause. Any exit reason
                that can route through this pause branch should arm the lockout unless
                it's independently proven immune to immediate re-satisfaction by the
                instant gate (comfort-floor and away-ceiling exits don't route through
                this function at all, so they're exempt by construction, not by omission).
            event_type: Issue #649 — the caller's own specific Activity Report event type
                (e.g. ``nat_vent_predicted_floor_exit``). Centralizing emission here (instead
                of each of the 7+ call sites emitting before calling this method, as before
                #649) lets the payload accurately reflect whether the underlying fan command
                actually executed or was deferred by the Issue #641 rate limiter, in exactly
                one place. None (the historical default) skips event emission entirely,
                preserving the small number of call sites that build their own event some
                other way.
            event_payload: Payload for ``event_type``. Its ``fan_mode_change`` key is
                overwritten based on the real outcome — left as given when the toggle
                executed, replaced with a "deferred" description when newly rate-limited.
                Ignored if ``event_type`` is None.

        Returns:
            The ``FanCommandResult`` from the underlying ``_deactivate_fan()`` call.
        """
        self._natural_vent_active = False
        self._nat_vent_soft_start = False
        if set_outdoor_exit_time:
            self._nat_vent_outdoor_exit_time = dt_util.now()
        sensor_open = self._any_monitored_sensor_open()
        # emit_event=False on both branches: the caller's own specific exit event
        # (nat_vent_predicted_floor_exit, nat_vent_comfort_floor_exit,
        # nat_vent_outdoor_rise_exit, etc.) is emitted below, by this method, once the
        # real outcome is known (Issue #649) — letting _deactivate_fan() also emit a
        # generic fan_deactivated event here would land at the same timestamp and shadow
        # the specific event in outcome-ordering consumers (Issue #411 — found during
        # test verification).
        if sensor_open:
            # Don't restore active HVAC into an open window — pause instead. The
            # existing pause/grace machinery (_re_pause_for_open_sensor) re-evaluates
            # nat-vent reactivation on the next grace-expiry cycle.
            #
            # Issue #618: release_suppression=True — this IS the session ending (we already
            # cleared _natural_vent_active above), even though we're not writing a restored
            # mode right now. Without this, _pre_fan_hvac_mode stays stranded non-None for as
            # long as the window stays open, and _whf_owns_hvac() keeps reporting the WHF as
            # still owning the thermostat long after the session actually ended — which is
            # exactly what happened in the 2026-08-10 incident: the window later closed and
            # handle_all_doors_windows_closed() ran, but apply_classification()'s DEFER_NAT_VENT
            # gate kept deferring the HVAC-mode restore for hours because this flag was never
            # released.
            result = await self._deactivate_fan(
                reason=reason, restore_hvac=False, release_suppression=True, emit_event=False
            )
            state = self.hass.states.get(self.climate_entity)
            self._pre_pause_mode = state.state if state and state.state != "off" else None
            # Write-shape divergence fix (found while scoping #637 Step 3): this branch
            # used to set only _paused_by_door, unlike _pause_for_door_window()'s full
            # field set. _pre_pause_mode's own truthiness (nothing to restore means the
            # FSM-visible state is equivalent to "already off"/PAUSED_IDLE) is what the
            # FSM's own inputs read, matching decide_door_close_response()'s own
            # truthiness test on pre_pause_mode.
            #
            # Issue #660 Step 4 / #757 Phase 6 Step 4: routed through the shared,
            # unconditionally FSM-authoritative dispatcher for the 2 fields it derives
            # (_paused_by_door/_paused_with_hvac_already_off — Issue #709 removed
            # _grace_active from this derivation, see that method's docstring).
            # _paused_entity/_paused_since aren't part of that derivation (see
            # _apply_door_window_fsm_state()'s own docstring), so they stay direct
            # writes below.
            from .door_window_fsm import DoorWindowFsmEventKind

            self._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.NAT_VENT_EXITED_SENSOR_STILL_OPEN)
            self._paused_entity = "nat-vent-exit"
            self._paused_since = dt_util.now()
            # Issue #649: a repeat of an already-reported deferral logs at DEBUG, not INFO —
            # this is the exact "one retry tick after another while still blocked" pattern
            # that produced unbounded duplicate INFO lines before this fix.
            _log = _LOGGER.debug if result is FanCommandResult.RATE_LIMITED_DUP else _LOGGER.info
            _log(
                "Nat-vent exit (%s): monitored sensor still open — pausing HVAC (pre_pause_mode=%s)",
                reason,
                self._pre_pause_mode,
            )
        else:
            result = await self._deactivate_fan(reason=reason, emit_event=False)
            self._start_grace_period("automation", trigger="nat_vent_exit_resume")
            _log = _LOGGER.debug if result is FanCommandResult.RATE_LIMITED_DUP else _LOGGER.info
            _log("Nat-vent exit (%s): sensors closed — restoring HVAC and starting grace period", reason)

        if event_type and self._emit_event_callback and result is not FanCommandResult.RATE_LIMITED_DUP:
            payload = dict(event_payload or {})
            if result is FanCommandResult.RATE_LIMITED_NEW:
                applies_at = self._fan_rate_limited_until
                payload["fan_mode_change"] = (
                    f"deferred (5-min floor, applies {applies_at.strftime('%H:%M:%S')})"
                    if isinstance(applies_at, datetime)
                    else "deferred (5-min floor)"
                )
            # EXECUTED / ALREADY_IN_STATE / OVERRIDDEN / DISABLED: payload's own
            # fan_mode_change (if any) is left exactly as the caller built it — matches
            # pre-#649 behavior for every outcome that isn't a fresh deferral.
            self._emit_event_callback(event_type, payload)

        return result

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

        Issue #775: the daytime branch previously returned raw ``comfort_heat``, which
        sits below the cycling-off threshold (``nat_vent_target - hysteresis``) a *live*
        session already uses in ``nat_vent_temperature_check()``. That let a re-armed
        session push indoor below where a continuously-running session would have
        stopped cycling. The daytime branch now returns that same off_threshold so a
        stopped session can't restart any lower than a live one would have cycled.
        The sleep-window branch is unchanged: ``sleep_heat`` already equals the live
        session's sleep-window off_threshold (``sleep_heat + hysteresis - hysteresis``),
        which is why this gap only showed up during the day.
        """
        comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        if _in_sleep_window(dt_util.now(), self.config):
            return float(self.config.get(CONF_SLEEP_HEAT, comfort_heat))
        comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))
        hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        nat_vent_target = compute_nat_vent_target(
            sleep_heat=0.0,
            in_sleep_window=False,
            comfort_heat_raw=comfort_heat,
            comfort_cool=comfort_cool,
            hysteresis=hysteresis,
        )
        return nat_vent_target - hysteresis

    def _build_nat_vent_fsm_inputs(
        self,
        *,
        now: datetime,
        indoor: float | None,
        outdoor: float | None,
        hysteresis: float | None = None,
        paused_by_door: bool | None = None,
        fan_hardware_active: bool | None = None,
        grace_active: bool | None = None,
        apply_reactivation_floor: bool = False,
    ):
        """Build the FSM's input snapshot from current engine state (Issue #594
        Phase R, Step 2). Issue #757 Phase 6 Step 5: this was previously also shared
        with ``coordinator._evaluate_nat_vent_fsm()``'s shadow-diagnostic
        construction — that method (a third, independent replica used only for
        comparison) has been deleted along with nat-vent's own shadow-diagnostic
        axes, so this is now the sole builder.

        Args:
            grace_active: Overrides ``self._grace_active`` when provided. Issue #757
                Phase 6 Step 5 fix: ``handle_door_window_open()``'s own grace-period
                bypass (Fix 2/#249 — "is outdoor cool enough that grace shouldn't
                block nat-vent") is a DIFFERENT criterion than
                ``_grace_blocks_natvent()``'s overheat exception (Issue #134/#706 —
                "is indoor hot enough that grace shouldn't block nat-vent"). When
                ``handle_door_window_open()``'s own pre-check (calling
                ``_nat_vent_may_reactivate()`` directly, temperature-only, no grace
                awareness) already decided reactivation is favorable and fell
                through, the immediately-following FSM gate call must not re-block
                on the STILL-True ``self._grace_active`` via the unrelated
                overheat-exception rule — that silently broke the Fix 2 bypass for
                any door-open-during-grace scenario where indoor didn't happen to
                exceed comfort_cool too, live in production since the FSM path went
                authoritative (found when Step 5 made it unconditional).
                ``None`` (every other call site) keeps reading ``self._grace_active``
                live, per Issue #706 Bug D's own fix — this override is narrowly
                scoped to the one call site whose own preceding logic has already
                answered the grace question via a different, already-executed rule.
                (``handle_door_window_open``, ``_re_pause_for_open_sensor``) pass
                ``hysteresis=0.0`` explicitly rather than the configured value — this
                parameter lets a caller preserve that same per-site distinction when
                building FSM inputs instead of always reading the configured value.
                ``None`` (every pre-#694 call site) keeps this method's prior
                behavior unchanged.
            paused_by_door: Overrides ``self._paused_by_door`` when provided. Issue #694
                (Phase 2b): ``check_natural_vent_conditions()``'s idle-open re-entry site
                only ever runs when ``_actively_paused`` is False (see that method's own
                guard) — a real ``_paused_by_door=True and _paused_with_hvac_already_off=
                True`` combination is possible there (the window was never actively
                interrupting HVAC), and the legacy call at that site has never consulted
                the reactivation lockout (only the separate paused-reactivation call site
                does). Feeding the FSM ``self._paused_by_door``'s real value at the
                idle-open site would apply the lockout somewhere legacy never has —
                new decision authority, out of this wiring-only issue's scope. ``False``
                is passed explicitly at that one call site to preserve the exact legacy
                gap; ``None`` (every other caller) keeps this method's prior behavior
                (``self._paused_by_door``) unchanged.
            fan_hardware_active: Overrides ``self._fan_active`` when provided. Issue #698
                (Phase 2d): only ``nat_vent_temperature_check()``'s FSM-authoritative
                branch needs this — it's the sole caller whose ``transition()`` result
                is read for ``fan_should_be_active`` (mid-session cycling). ``None``
                (every other caller) keeps this method's prior behavior
                (``self._fan_active``) unchanged — harmless either way, since none of
                those other call sites read ``NatVentTransition.fan_should_be_active``.
            apply_reactivation_floor: Issue #775. ``False`` (default) keeps
                ``comfort_heat_raw``/``in_sleep_window`` genuinely raw/live, matching
                every call site's behavior before this issue — correct for a pure
                first-activation/adopt-gate question (``handle_door_window_open()``'s
                dormancy-mirror check, ``reconcile_fan_on_startup()``'s adopt gate),
                where there is no live session's own cycling band to hold a restart to.
                ``True`` pre-resolves ``comfort_heat_raw`` through
                ``self._nat_vent_reactivation_floor()`` (the same daytime
                cycling-off-threshold fix ``_nat_vent_may_reactivate()``'s 4 legacy
                callers already apply) and forces ``in_sleep_window=False`` as a
                pass-through, so the gate's own sleep-window branch doesn't re-resolve
                an already-resolved value — same convention documented on
                ``_nat_vent_may_reactivate()``. Set at the 3 call sites that are
                genuinely re-arming a session that already ran and exited: the
                idle-open re-entry check, the paused-by-door reactivation block, and
                ``_re_pause_for_open_sensor``'s reactivation check.

        Issue #706 (Bug D): ``override_active``/``grace_active`` are now always read
        live from engine state — ``bool(self._fan_override_active or
        self._manual_override_active)`` and ``bool(self._grace_active)``. Before this
        fix, every real production caller of this method left both fields at their
        dataclass default (``False``, added for Issue #687/Phase 2a but never wired
        here), so the FSM was blind to a real override/grace window and could
        disagree with what ``_activate_fan()``'s own override guard actually did.

        Issue #717: ``paused_by_door``/``grace_active``/``manual_override_active`` are
        still read directly off the canonical attributes here — see
        ``_dispatched_paused_by_door``'s declaration comment in ``__init__`` for why a
        same-instance mirror sourced *only* from dispatcher events turned out to be
        the wrong design (it broke the established direct-attribute-assignment test
        fixture convention across 40+ test files, for no real staleness benefit: this
        engine both emits and consumes, so same-object attribute access can never go
        stale the way a genuine cross-instance mirror could). The dispatcher's value
        today is the real, tested emit/audit-trail wiring at every genuine transition
        — proven by ``check_registry_completeness()`` and the dispatcher's own
        ``event_log`` — not as the sole gate on these reads.
        """
        from .nat_vent_fsm import NatVentFsmInputs

        comfort_heat_raw = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        thermal_model = self._thermal_model or {}
        _configured_hysteresis = float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
        _hysteresis = hysteresis if hysteresis is not None else _configured_hysteresis
        _paused_by_door = paused_by_door if paused_by_door is not None else bool(self._paused_by_door)
        _fan_hardware_active = fan_hardware_active if fan_hardware_active is not None else bool(self._fan_active)
        _override_active = bool(self._fan_override_active or self._manual_override_active)
        _grace_active = grace_active if grace_active is not None else bool(self._grace_active)
        _sleep_heat = float(self.config.get(CONF_SLEEP_HEAT, comfort_heat_raw))
        _in_sleep_window_val = _in_sleep_window(now, self.config)
        if apply_reactivation_floor:
            # Issue #775: pre-resolve through the same daytime cycling-off-threshold
            # formula _nat_vent_reactivation_floor() already applies for every other
            # reactivation call site, then force in_sleep_window=False so the gate's
            # own sleep-window branch treats this as already-resolved (same
            # pass-through convention documented on _nat_vent_may_reactivate()). Note
            # this reassigns comfort_heat_raw AFTER _sleep_heat's own raw-comfort_heat
            # fallback above is computed, so _sleep_heat is never accidentally
            # derived from the already-tightened value.
            comfort_heat_raw = self._nat_vent_reactivation_floor()
            _in_sleep_window_val = False
        return NatVentFsmInputs(
            indoor=indoor,
            outdoor=outdoor,
            comfort_heat_raw=comfort_heat_raw,
            sleep_heat=_sleep_heat,
            in_sleep_window=_in_sleep_window_val,
            comfort_cool=float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)),
            nat_vent_delta=float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA)),
            hysteresis=_hysteresis,
            # Issue #757 Phase 6 Step 4 fix: same "whole_house_fan" wrong-default bug
            # found and fixed in _build_door_window_fsm_inputs() (this method's own
            # sibling builder, and the original source of the copy-paste — see that
            # method's comment for the full incident). Fixed here too for consistency —
            # this was a live latent bug for any caller of this method with no
            # fan_mode configured, now unconditionally reachable since Step 5.
            fan_mode=str(self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)),
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
            occupancy_mode=self._occupancy_mode,
            thermal_confidence=thermal_model.get("confidence", "none"),
            k_passive=thermal_model.get("k_passive"),
            outdoor_today_peak=self._outdoor_temp_today_peak,
            outdoor_sample_count=self._outdoor_temp_today_sample_count,
            peak_decline_margin=PEAK_DECLINE_MARGIN_F,
            paused_by_door=_paused_by_door,
            outdoor_exit_time=self._nat_vent_outdoor_exit_time,
            lockout_seconds=float(
                self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S)
            ),
            now=now,
            fan_hardware_active=_fan_hardware_active,
            override_active=_override_active,
            grace_active=_grace_active,
            manual_override_active=bool(self._manual_override_active),
            manual_override_mode=self._manual_override_mode,
        )

    @property
    def nat_vent_lifecycle_state(self) -> NatVentLifecycleState:
        """Current nat-vent session state, derived from existing flags (Issue #606).

        Purely a computed view of ``_natural_vent_active``/``_nat_vent_soft_start``/
        ``_paused_by_door``/``_nat_vent_outdoor_exit_time``, so it cannot desync from
        the flags it reads. See ``nat_vent_lifecycle.py`` for the pure derivation.

        As of Issue #757 Phase 6 Step 5, this is a production decision input, not
        read-only observability: it unconditionally supplies the FSM's starting state
        at 3 of the 4 wired call sites (the idle-open re-entry site and both
        paused-reactivation branches) — ``handle_door_window_open()``'s entry-gate site
        and ``reconcile_fan_on_startup()`` deliberately force ``INACTIVE`` instead,
        since those are pure entry-gate questions independent of live session state
        (see their own rationale comments).
        """
        lockout_s = float(self.config.get(CONF_NAT_VENT_REACTIVATION_LOCKOUT_S, NAT_VENT_REACTIVATION_LOCKOUT_S))
        return derive_nat_vent_lifecycle_state(
            NatVentLifecycleInputs(
                natural_vent_active=self._natural_vent_active,
                nat_vent_soft_start=self._nat_vent_soft_start,
                paused_by_door=self._paused_by_door,
                outdoor_exit_time=self._nat_vent_outdoor_exit_time,
                now=dt_util.now(),
                lockout_seconds=lockout_s,
            )
        )

    def _apply_nat_vent_fsm_state(self, state: NatVentLifecycleState) -> None:
        """Write ``_natural_vent_active``/``_nat_vent_soft_start``/``_paused_by_door``
        from a ``nat_vent_fsm.transition()`` result (Issue #594 Phase R, Phase 2f).

        Inverse of ``nat_vent_lifecycle_state``'s derivation — see
        ``nat_vent_lifecycle.py``'s ``derive_nat_vent_lifecycle_state()``.
        Deliberately does NOT touch ``_nat_vent_outdoor_exit_time`` — the FSM's
        ``to_state`` alone cannot distinguish "just entered lockout" from
        "already mid-lockout," and only the outdoor-reversal exit path
        (``_exit_nat_vent(set_outdoor_exit_time=True)``) has the information
        needed to arm it, same exclusion-list treatment as door/window's
        ``_grace_end_time``.

        Called from production at 5 sites (``handle_door_window_open()``,
        ``check_natural_vent_conditions()``'s idle-open re-entry and
        reactivation-while-paused branches, and ``reconcile_fan_on_startup()``) —
        unconditionally as of Issue #757 Phase 6 Step 5.

        Issue #717: this is a SECOND real writer of ``_paused_by_door``, outside
        ``_resolve_door_window_pause_flags()``'s own before/after diff — the nat-vent
        FSM can set this door/window-owned field directly. Needs its own before/after
        diff so DOOR_PAUSE_STARTED/ENDED still fires correctly on this path — found the
        same way Phase 1 found ``coordinator.py:5088``: a second real writer of a
        field this issue is already instrumenting.
        """
        _paused_before = bool(self._paused_by_door)
        self._natural_vent_active = state in (
            NatVentLifecycleState.ACTIVE_FULL_GATE,
            NatVentLifecycleState.ACTIVE_SOFT_START,
        )
        self._nat_vent_soft_start = state == NatVentLifecycleState.ACTIVE_SOFT_START
        self._paused_by_door = state == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT
        self._emit_boolean_transition(
            before=_paused_before,
            after=bool(self._paused_by_door),
            started=LifecycleEventType.DOOR_PAUSE_STARTED,
            ended=LifecycleEventType.DOOR_PAUSE_ENDED,
            detail="nat_vent_fsm_authoritative",
            caller="_apply_nat_vent_fsm_state",
        )

    def _apply_nat_vent_fsm_state_after_activation(
        self, to_state: NatVentLifecycleState, activation_result: FanCommandResult
    ) -> None:
        """Apply an FSM decision computed BEFORE an ``await self._activate_fan(...)``
        call, guarding against the Issue #706 Bug F race.

        All 5 production call sites for ``_apply_nat_vent_fsm_state()`` share the
        same shape: compute a ``to_state`` decision, then ``await
        self._activate_fan(...)`` — a real event-loop yield point — under
        ``_decision_lock``/``_decision_pass``, then apply that pre-await decision.
        ``handle_fan_manual_override()``/``coordinator._async_fan_entity_changed()``
        are NOT lock-protected and can run to completion during that await window,
        setting ``_fan_override_active``/starting grace. When that happens,
        ``_activate_fan()``'s own override guard rejects the real fan command and
        returns ``FanCommandResult.OVERRIDDEN`` — the definitive, race-free signal
        that the pre-await ``to_state`` is now stale. In that case, apply
        ``INACTIVE`` instead of the stale decision so ``_natural_vent_active`` never
        disagrees with the fact the real command was rejected. Every other
        ``FanCommandResult`` (``EXECUTED``, ``ALREADY_IN_STATE``,
        ``RATE_LIMITED_NEW``/``DUP``, ``DISABLED``) means no override intervened
        mid-await, so the pre-await ``to_state`` is still correct to apply.
        """
        if activation_result is FanCommandResult.OVERRIDDEN:
            _LOGGER.warning(
                "Nat-vent FSM state application skipped: fan override became active"
                " during activation — applying INACTIVE instead of stale %s decision",
                to_state,
            )
            self._apply_nat_vent_fsm_state(NatVentLifecycleState.INACTIVE)
        else:
            self._apply_nat_vent_fsm_state(to_state)

    def _build_door_window_fsm_inputs(self, *, now: datetime, nat_vent_gate_ruled_out: bool = False):
        """Build the door/window FSM's input snapshot from current engine state
        (Issue #594 Phase R, Step 2).

        Issue #594 Phase R, Step 0: this is now the SOLE builder for
        ``DoorWindowFsmInputs`` — ``coordinator._door_window_fsm_inputs()`` was
        deleted because both of its call sites always ran against
        ``self.automation_engine`` (production, never the shadow engine), so it
        was the same computation written twice, not two builders that happened
        to agree. Both coordinator call sites now call this method directly
        (``ae._build_door_window_fsm_inputs(now=now)``).

        Issue #717/#722: ``natural_vent_active``/``grace_active``/``whf_owns_hvac``
        all still read the canonical attributes/method directly here — see
        ``_dispatched_paused_by_door``'s declaration comment in ``__init__`` for why a
        same-instance dispatcher-only mirror was the wrong design for these reads.
        ``_dispatched_whf_owns_hvac`` (added for #722, sourced from
        ``_resolve_whf_hvac_suppression()``'s before/after diff) is deliberately NOT
        read here for the same reason: tests across `test_fan_control.py`,
        `test_whole_house_fan_hvac_suppression.py`, etc. set
        ``engine._pre_fan_hvac_mode`` directly, bypassing the dispatcher — routing
        this input through the mirror would reproduce the exact regression #717's
        own FSM-builder wiring hit and reverted. The dispatcher's value here is the
        emit/audit-trail wiring at every genuine transition, proven by
        ``check_registry_completeness()`` and its own ``event_log`` — not gating
        these already-fresh same-object reads.
        """
        from .door_window_fsm import DoorWindowFsmInputs

        state = self.hass.states.get(self.climate_entity)
        hvac_mode = state.state if state else None
        return DoorWindowFsmInputs(
            hvac_mode=hvac_mode,
            outdoor=self._last_outdoor_temp,
            indoor=self._get_indoor_temp_f(),
            comfort_heat=self._nat_vent_reactivation_floor(),
            comfort_cool=float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)),
            nat_vent_delta=float(self.config.get(CONF_NATURAL_VENT_DELTA, DEFAULT_NATURAL_VENT_DELTA)),
            # Issue #757 Phase 6 Step 4 fix: every other fan_mode config read in this
            # file (25+ call sites, e.g. _nat_vent_may_reactivate() at line ~8073)
            # defaults to FAN_MODE_DISABLED when unset — this builder used
            # "whole_house_fan" instead (introduced #637, copy-pasted from the
            # nat-vent FSM builder's own identical mistake, #633). Dormant while
            # door/window's dispatcher was flag-gated (this builder was never called
            # for a config with no fan_mode key in real production), surfaced as a
            # real production divergence once Step 4 made the dispatcher
            # unconditional: a home with no whole-house fan configured, HVAC-only
            # ceiling exceeded, would have its own real gate correctly block nat-vent
            # while this FSM builder's inputs silently claimed a whole-house-fan
            # archetype, entering DoorOpenResponse.NAT_VENT_ELIGIBLE and skipping the
            # pause entirely — see tests/test_nat_vent_activation.py's site3/site4
            # FAN_MODE_HVAC cases (both use a config with no fan_mode key), which
            # caught this live.
            fan_mode=str(self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)),
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
            within_planned_window=self._is_within_planned_window_period(),
            any_sensor_open=self._any_monitored_sensor_open(),
            sensor_debounce_pending=bool(self._sensor_debounce_pending),
            override_matches_current_decision=self._override_matches_current_decision(self._current_classification),
            grace_source=self._last_resume_source or "automation",
            natural_vent_active=bool(self._natural_vent_active),
            whf_owns_hvac=bool(self._whf_owns_hvac()),
            grace_active=bool(self._grace_active),
            pre_pause_mode_active=bool(self._pre_pause_mode),
            manual_grace_would_start=self._grace_would_start("manual", now),
            automation_grace_would_start=self._grace_would_start("automation", now),
            now=now,
            nat_vent_gate_ruled_out=nat_vent_gate_ruled_out,
        )

    @property
    def door_window_lifecycle_state(self) -> DoorWindowLifecycleState:
        """Current door/window pause/grace session state, derived from existing
        flags (Issue #637).

        Read-only observability by default — the value this property computes is
        also fed as the ``current_state`` argument to ``door_window_fsm.transition()``
        by ``AutomationEngine._resolve_door_window_pause_flags()``, the shared
        dispatcher every one of the 8 real door/window trigger methods calls,
        unconditionally authoritative as of Issue #757 Phase 6 Step 4 (formerly gated
        by ``_doorwindow_fsm_authoritative``, Issue #660 Phase R Step 8 — now removed).
        ``_on_grace_expired()`` is the one call site that passes an explicitly
        captured ``origin_state`` instead of relying on this property's live read,
        since grace is already cleared by the time it would otherwise read it. Purely
        a computed view of ``_paused_by_door``/``_paused_with_hvac_already_off``/
        ``_grace_active``, so it cannot desync from the flags it reads. See
        ``door_window_lifecycle.py`` for the pure derivation.
        """
        return derive_door_window_lifecycle_state(
            DoorWindowLifecycleInputs(
                paused_by_door=bool(self._paused_by_door),
                paused_with_hvac_already_off=bool(self._paused_with_hvac_already_off),
                grace_active=bool(self._grace_active),
            )
        )

    def _apply_door_window_fsm_state(self, state: DoorWindowLifecycleState) -> None:
        """Write ``_paused_by_door``/``_paused_with_hvac_already_off`` from a
        ``door_window_fsm.transition()`` result (Issue #594 Phase R, Step 2).

        The inverse of ``door_window_lifecycle_state``'s derivation — see
        ``door_window_lifecycle.py``'s state-to-flags table. Deliberately does NOT
        touch ``_paused_entity``/``_paused_since``/``_pre_pause_mode``/
        ``_last_resume_source``/``_last_grace_trigger``/``_grace_end_time``/
        ``_grace_protects_override`` — those aren't part of the 5-state derivation
        (the FSM's ``outcome``/``at`` fields don't carry entity labels or trigger
        names), so every caller keeps writing those itself, same as before this
        method existed.

        **Issue #709: does NOT write ``_grace_active``.** Prior to #709 this method
        also wrote ``_grace_active`` from the GRACE/PAUSED_DURING_GRACE members of
        ``state`` — a second, independent writer of a flag ``override_grace_fsm.py``'s
        own docstring already claims exclusive ownership of (see
        ``_resolve_override_grace_fsm_state()``'s docstring: "Genuinely mutually
        exclusive — exactly one of the FSM path or ``legacy()`` ever writes
        ``_grace_active``"— a claim this method's now-removed write silently
        falsified). The dual-write was reachable as a real race, not just a
        theoretical layering violation: ``resume_from_pause()`` and
        ``handle_all_doors_windows_closed()`` both call
        ``_resolve_door_window_pause_flags()`` (which used to land here) BEFORE the
        real grace-start action (``_start_grace_period_action()``), separated by
        genuine ``await`` points (``_set_hvac_mode()``/``_set_temperature_for_mode()``)
        that yield control back to the event loop. During that window, a concurrently
        scheduled task (e.g. ``coordinator._check_orphaned_grace()``) could observe
        ``_grace_active=True`` with no real timer scheduled and no override
        protecting it — a phantom grace period this method's write created and only
        the subsequent real action call (which unconditionally cancels any prior
        timer via ``_cancel_grace_timers()``) happened to wash out. Now that this
        method leaves ``_grace_active`` untouched, the ONLY writer is
        ``_apply_override_grace_fsm_state()`` (FSM path) or the paired ``legacy()``
        closures passed to ``_resolve_override_grace_fsm_state()`` — both already
        correctly gated on ``_start_grace_period_action()``'s real return value, so
        a phantom "grace active, no timer" combination can no longer occur. Door/
        window's own FSM continues to READ ``_grace_active`` as a cross-lifecycle
        input (``DoorWindowFsmInputs.grace_active``) — every call site re-derives
        ``current_state`` from a live read of ``door_window_lifecycle_state`` (which
        itself reads the live flag) rather than carrying a stale copy, so this
        state machine self-heals against whatever the real flag is on every
        subsequent transition; see ``door_window_fsm.py``'s own docstring for the
        ``manual_grace_would_start``/``automation_grace_would_start`` inputs added in
        the same fix, which keep the FSM's *own* ``to_state``/``outcome`` audit
        trail accurate for the 3 transitions that used to unconditionally assume a
        new grace period would start.
        """
        self._paused_by_door = state in (
            DoorWindowLifecycleState.PAUSED_ACTIVE,
            DoorWindowLifecycleState.PAUSED_IDLE,
            DoorWindowLifecycleState.PAUSED_DURING_GRACE,
        )
        self._paused_with_hvac_already_off = state == DoorWindowLifecycleState.PAUSED_IDLE

    def _emit_boolean_transition(
        self,
        *,
        before: bool,
        after: bool,
        started: LifecycleEventType,
        ended: LifecycleEventType,
        detail: str | None,
        caller: str,
    ) -> None:
        """Shared before/after-diff-emit shape (Issue #721/#722 DRY finding).

        Extracted from 3 call sites that each hand-rolled this identical
        try/except + if/elif block (``_apply_nat_vent_fsm_state()``,
        ``_resolve_door_window_pause_flags()``, ``_resolve_override_grace_fsm_state()``)
        before a 4th copy was about to be added for WHF-suppression tracking
        (``_resolve_whf_hvac_suppression()``). Pure extraction — same emitted
        events, same exception isolation — so it must not change any of the 3
        existing sites' observable behavior.
        """
        try:
            if after and not before:
                self._lifecycle_dispatcher.emit(
                    LifecycleEvent(event_type=started, source="automation_engine", at=dt_util.now(), detail=detail)
                )
            elif before and not after:
                self._lifecycle_dispatcher.emit(
                    LifecycleEvent(event_type=ended, source="automation_engine", at=dt_util.now(), detail=detail)
                )
        except Exception:  # noqa: BLE001 — a dispatcher bug must never affect the real decision
            _LOGGER.exception("%s: lifecycle event emit failed (isolated)", caller)

    def _resolve_door_window_pause_flags(
        self,
        *,
        kind: DoorWindowFsmEventKind,
        origin_state: DoorWindowLifecycleState | None = None,
        nat_vent_gate_ruled_out: bool = False,
    ) -> None:
        """Single dispatch point for every door/window flag-derivation call site
        (Issue #594 Phase R / #660). Each of the 8 real trigger methods supplies its own
        event kind; this function owns the FSM transition exactly once, instead of once
        per call site. Issue #757 Phase 6 Step 4: the legacy inline flag-write branch
        (and the ``legacy`` closure parameter each call site used to build) has been
        removed — this dispatcher is now unconditionally FSM-authoritative.

        ``origin_state``: defaults to a live read of ``self.door_window_lifecycle_state``
        — correct for every site except ``_on_grace_expired()``, which must capture
        state *before* ``_cancel_grace_timers()`` clears ``_grace_active`` and pass it
        explicitly here (grace has already been cleared by the time this function
        would otherwise read it live).

        ``nat_vent_gate_ruled_out``: forwarded to ``_build_door_window_fsm_inputs()`` —
        see ``DoorWindowFsmInputs.nat_vent_gate_ruled_out``'s own docstring (Issue #757
        Phase 6 Step 4 fix). ``_pause_for_door_window()`` is the only real caller that
        passes ``True``.

        Issue #717: also the single emit point for DOOR_PAUSE_STARTED/ENDED — every
        real door/window pause transition already funnels through here regardless of
        which branch below actually decides the flag, so a before/after diff of
        ``_paused_by_door`` around the branch catches every real occasion (the 8 kinds
        this method is called with) without needing per-``kind`` special-casing, which
        would have to guess at a static kind→direction mapping that doesn't hold for
        compound kinds like ``PAUSED_NAT_VENT_REACTIVATED``/``GRACE_TIMER_EXPIRED``
        (each can resolve to either direction depending on the real outcome).
        """
        _paused_before = bool(self._paused_by_door)
        from .door_window_fsm import DoorWindowFsmEvent
        from .door_window_fsm import transition as _door_window_transition

        state = origin_state if origin_state is not None else self.door_window_lifecycle_state
        result = _door_window_transition(
            state,
            DoorWindowFsmEvent(
                kind=kind,
                inputs=self._build_door_window_fsm_inputs(
                    now=dt_util.now(), nat_vent_gate_ruled_out=nat_vent_gate_ruled_out
                ),
            ),
        )
        self._apply_door_window_fsm_state(result.to_state)
        self._emit_boolean_transition(
            before=_paused_before,
            after=bool(self._paused_by_door),
            started=LifecycleEventType.DOOR_PAUSE_STARTED,
            ended=LifecycleEventType.DOOR_PAUSE_ENDED,
            detail=kind.value,
            caller="_resolve_door_window_pause_flags",
        )

    def _build_override_grace_fsm_inputs(self):
        """Build the override/grace FSM's input snapshot from current engine state
        (Issue #664).

        Sole builder for ``OverrideGraceFsmInputs`` — mirrors
        ``coordinator._evaluate_override_grace_fsm()``'s pre-existing computation
        exactly (same ``current_setpoint_f``/``target_setpoint_f`` resolution via
        ``select_comfort_band()`` + ``to_fahrenheit()``, wrapped in the same
        defensive try/except a missing/unparseable live setpoint tolerates), now
        called from inside ``AutomationEngine`` itself so the dispatcher can build
        the same snapshot the shadow-mirror path already builds, without a second,
        coordinator-side copy of this computation.
        """
        from .override_grace_fsm import OverrideGraceFsmInputs

        now = dt_util.now()
        classification = self._current_classification
        classification_mode = classification.hvac_mode if classification else None

        current_setpoint_f: float | None = None
        target_setpoint_f: float | None = None
        try:
            if classification is not None and classification_mode in ("heat", "cool"):
                band = select_comfort_band(
                    classification,
                    self.config,
                    occupancy_mode=self._occupancy_mode,
                    in_sleep_window=_in_sleep_window(now, self.config),
                    aggressive_savings=bool(self.config.get("aggressive_savings", False)),
                )
                target_setpoint_f = band.floor if classification_mode == "heat" else band.ceiling
                state = self.hass.states.get(self.climate_entity)
                raw_setpoint = state.attributes.get("temperature") if state else None
                if raw_setpoint is not None:
                    unit = self.config.get("temp_unit", "fahrenheit")
                    current_setpoint_f = to_fahrenheit(float(raw_setpoint), unit)
        except (TypeError, ValueError, AttributeError):
            current_setpoint_f = None
            target_setpoint_f = None

        current_mode_state = self.hass.states.get(self.climate_entity)
        return OverrideGraceFsmInputs(
            confirm_seconds=float(self.config.get(CONF_OVERRIDE_CONFIRM_PERIOD, DEFAULT_OVERRIDE_CONFIRM_SECONDS)),
            setpoint_override=bool(self._override_confirm_source == "setpoint"),
            current_mode=current_mode_state.state if current_mode_state else None,
            classification_mode=classification_mode,
            manual_override_active=bool(self._manual_override_active),
            manual_override_mode=self._manual_override_mode,
            manual_override_source=self._manual_override_source,
            fan_override_active=bool(self._fan_override_active),
            current_setpoint_f=current_setpoint_f,
            target_setpoint_f=target_setpoint_f,
            tolerance_f=OVERRIDE_ADOPT_SETPOINT_TOLERANCE_F,
            within_planned_window=self._is_within_planned_window_period(),
            any_sensor_open=self._any_monitored_sensor_open(),
            # Issue #757 Phase 6 Step 3 correction: for GRACE_TIMER_EXPIRED specifically,
            # _cancel_grace_timers_action() already set _last_resume_source=None before
            # this dispatch, so this read is always "automation" for that kind. Confirmed
            # INERT — it only feeds the FSM transition's audit-only outcome string, which
            # transition() never reads, and GRACE_TIMER_EXPIRED always lands on
            # (IDLE, NONE) regardless of it. Both production and the (now-deleted) shadow
            # comparator shared this exact staleness with zero divergence, so it's left
            # as-is rather than "fixed."
            grace_source=self._last_resume_source or "automation",
            now=now,
            grace_would_start=self._manual_grace_would_start(now),
        )

    def _grace_would_start(self, source: str, now: datetime) -> bool:
        """Whether a grace period for ``source`` ("manual" or "automation") is
        currently enabled by config — i.e. whether ``_start_grace_period_action()``
        would actually schedule a real timer for that source right now, without
        actually starting one (Issue #664, generalized in #709).

        Same ``decide_grace_start()`` call ``_start_grace_period_action()`` itself
        makes. Both FSMs that model grace (``override_grace_fsm.py``,
        ``door_window_fsm.py``) consult this — the SOLE computation of "would grace
        start", never duplicated — since ``decide_grace_start()`` resolves duration/
        should_notify for BOTH sources from a single call regardless of which one
        is asked about, this passes the other source's live config values through
        unconditionally; only the returned duration for ``source`` determines the
        boolean result.
        """
        manual_duration = self.config.get(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        return (
            decide_grace_start(
                source=source,
                manual_duration_seconds=manual_duration,
                manual_should_notify=self.config.get(CONF_MANUAL_GRACE_NOTIFY, True),
                automation_duration_seconds=self.config.get(
                    CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS
                ),
                automation_should_notify=self.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True),
                now=now,
            )
            is not None
        )

    def _manual_grace_would_start(self, now: datetime) -> bool:
        """Whether manual grace is currently enabled (``CONF_MANUAL_GRACE_PERIOD`` > 0)
        (Issue #664). Thin ``source="manual"`` wrapper over ``_grace_would_start()``
        (Issue #709) — kept as a named method since ``override_grace_fsm.py``'s own
        docstring documents "every override/grace-modeled event that starts grace
        uses ``source='manual'``", so every real call site building
        ``OverrideGraceFsmInputs`` always wants this specific source.
        """
        return self._grace_would_start("manual", now)

    @property
    def override_grace_lifecycle_state(self) -> OverrideGraceLifecycleState:
        """Current override/grace session state, derived from existing flags
        (Issue #639, promoted from shadow-only to authoritative-capable in #664).

        Read-only observability by default — the value this property computes is
        also fed as the ``current_state`` argument to ``override_grace_fsm.transition()``
        by ``AutomationEngine._resolve_override_grace_fsm_state()``, the shared
        dispatcher every real override/grace trigger site calls (unconditionally
        authoritative as of Issue #757 Phase 6 Step 3). Purely a computed view of
        ``_override_confirm_pending``/``_grace_active``/``_grace_protects_override``,
        so it cannot desync from the flags it reads. See ``override_grace_lifecycle.py``
        for the pure derivation.
        """
        from .override_grace_lifecycle import (
            OverrideGraceLifecycleInputs,
            derive_override_grace_lifecycle_state,
        )

        return derive_override_grace_lifecycle_state(
            OverrideGraceLifecycleInputs(
                override_confirm_pending=bool(self._override_confirm_pending),
                grace_active=bool(self._grace_active),
                grace_protects_override=bool(self._grace_protects_override),
            )
        )

    def _apply_override_grace_fsm_state(self, state: OverrideGraceLifecycleState) -> None:
        """Write ``_override_confirm_pending``/``_grace_active``/``_grace_protects_override``
        from an ``override_grace_fsm.transition()`` result (Issue #664).

        The inverse of ``override_grace_lifecycle_state``'s derivation. Deliberately does
        NOT touch ``_override_confirm_time``/``_mode``/``_source``, ``_grace_end_time``,
        ``_last_resume_source``, ``_last_grace_trigger``, ``_grace_duration_seconds``, or
        any ``_manual_override_*``/``_fan_override_active`` field — none of those are part
        of the 2-tuple's derivation (same "outside the N-field derivation stays a direct
        per-caller write" rule ``_apply_door_window_fsm_state()``'s own docstring
        documents).

        Called only from ``_resolve_override_grace_fsm_state()`` — the real
        timer/confirm-window primitives (``_start_grace_period_action()``,
        ``_start_override_confirmation_action()``, ``_cancel_grace_timers_action()``) never
        write these 3 flags themselves; this method is their sole writer (Issue #757
        Phase 6 Step 3 — the prior non-authoritative ``legacy()`` closure branch was
        proven behaviorally equivalent by an offline differential comparator across the
        golden+pending corpus, then deleted).
        """
        confirm, grace = state
        self._override_confirm_pending = confirm == OverrideConfirmState.PENDING
        self._grace_active = grace != GraceState.NONE
        self._grace_protects_override = grace == GraceState.ACTIVE_PROTECTING_OVERRIDE

    def _resolve_override_grace_fsm_state(
        self,
        *,
        kind: OverrideGraceFsmEventKind,
        origin_state: OverrideGraceLifecycleState | None = None,
    ) -> None:
        """Single dispatch point for every override/grace flag-derivation call site
        (Issue #664). Unconditionally authoritative as of Issue #757 Phase 6 Step 3 —
        ``override_grace_fsm.transition()`` is the sole computation of
        ``_override_confirm_pending``/``_grace_active``/``_grace_protects_override``; the
        prior non-authoritative ``legacy()`` closure branch (behind the now-removed
        ``_override_grace_fsm_authoritative`` flag) was deleted after weeks of production
        parity confirmed zero divergence via an offline differential comparator across
        the golden+pending corpus.

        Real timer/confirm-window scheduling is NOT owned here — callers must already
        have run the real side-effecting "_action" half of whichever primitive
        (``_start_grace_period_action()``, ``_start_override_confirmation_action()``,
        ``_cancel_grace_timers_action()``) *before* calling this dispatcher; this method
        only ever decides the 3 flags, never timers.

        ``origin_state`` defaults to a live read of ``self.override_grace_lifecycle_state``
        — correct for every site except ``_on_grace_expired()``'s branches and
        ``_check_orphaned_grace()``, both of which must capture state *before* the real
        cancel action clears the flags being transitioned, and pass it explicitly here.

        Issue #717: also the single emit point for GRACE_STARTED/ENDED, via a
        before/after diff of ``_grace_active`` around the transition — same rationale as
        the equivalent diff in ``_resolve_door_window_pause_flags()``. Deliberately
        does NOT emit OVERRIDE_CONFIRMED/CLEARED here — those aren't part of this
        dispatcher's 3-flag derivation (see ``_apply_override_grace_fsm_state()``'s own
        docstring); they're emitted from ``_confirm_override_action()`` and
        ``_clear_manual_override_active()``, each a single real unconditional site.
        """
        _grace_before = bool(self._grace_active)
        from .override_grace_fsm import OverrideGraceFsmEvent
        from .override_grace_fsm import transition as _override_grace_transition

        state = origin_state if origin_state is not None else self.override_grace_lifecycle_state
        result = _override_grace_transition(
            state,
            OverrideGraceFsmEvent(kind=kind, inputs=self._build_override_grace_fsm_inputs()),
        )
        self._apply_override_grace_fsm_state(result.to_state)
        self._emit_boolean_transition(
            before=_grace_before,
            after=bool(self._grace_active),
            started=LifecycleEventType.GRACE_STARTED,
            ended=LifecycleEventType.GRACE_ENDED,
            detail=kind.value,
            caller="_resolve_override_grace_fsm_state",
        )

    def _nat_vent_may_reactivate(
        self,
        *,
        outdoor: float | None,
        indoor: float | None,
        comfort_heat: float,
        comfort_cool: float,
        nat_vent_delta: float,
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

        Architecture-reset Step 2 follow-up: the decision itself now lives in
        ``nat_vent_gate.decide_nat_vent_gate()`` — differential + substitution
        tested against this exact call (see tools/nat_vent_gate_diff.py,
        tools/nat_vent_gate_substitution_diff.py). This method only reconstructs
        the ``NatVentGateInputs`` from already-caller-resolved values (``comfort_heat``
        is already sleep-window-resolved by every caller via
        ``_nat_vent_reactivation_floor()``, so ``in_sleep_window=False`` here is not a
        second resolution — it's a pass-through that keeps the pure core's own
        sleep-window branch a no-op, since that resolution already happened once).

        Args:
            outdoor: Current outdoor temperature (°F), or None if unavailable.
            indoor: Current indoor temperature (°F), or None if unavailable.
            comfort_heat: Comfort floor (°F) — indoor must be above this. Already
                sleep-window-resolved by the caller (via ``_nat_vent_reactivation_floor()``).
            comfort_cool: Comfort ceiling (°F) — used for the archetype-aware ceiling check.
            nat_vent_delta: Added to comfort_cool to form the outdoor threshold —
                every real call site already holds this as a separate local before
                summing it into a threshold, so passing it through unchanged (instead
                of the pre-summed threshold) avoids re-deriving it via subtraction.
            hysteresis: Subtracted from indoor in the outdoor/indoor delta check. Callers
                that don't apply hysteresis (handle_door_window_open, _re_pause_for_open_sensor)
                pass 0.0 (the default); the paused-by-door reactivation block passes the
                configured nat-vent hysteresis.
        """
        inputs = NatVentGateInputs(
            outdoor=outdoor,
            indoor=indoor,
            comfort_heat_raw=comfort_heat,
            sleep_heat=comfort_heat,
            in_sleep_window=False,
            comfort_cool=comfort_cool,
            nat_vent_delta=nat_vent_delta,
            hysteresis=hysteresis,
            fan_mode=self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        return decide_nat_vent_gate(inputs)

    def _nat_vent_may_soft_start(
        self,
        *,
        outdoor: float | None,
        indoor: float | None,
        comfort_heat: float,
        comfort_cool: float,
        full_gate_active: bool,
    ) -> bool:
        """Shared soft-start sub-gate for nat-vent (Issue #540, scoped from #533).

        Distinct from ``_nat_vent_may_reactivate()``/``decide_nat_vent_gate()`` — allows
        WHF purge/comfort activation at outdoor/indoor parity once today's outdoor temp
        is confirmed past its peak and declining, without waiting for the full
        bulk-cooling gate's hysteresis-cleared delta. Opt-out via
        ``CONF_NAT_VENT_SOFT_START_ENABLED`` (default on — no humidity/dew-point sensor
        guards this today, so disable it if you only want the fan to run once outdoor is
        measurably cooler than indoor). See ``nat_vent_gate.NatVentSoftStartGateInputs``
        for full field rationale.

        Args:
            outdoor: Current outdoor temperature (°F), or None if unavailable.
            indoor: Current indoor temperature (°F), or None if unavailable.
            comfort_heat: Comfort floor (°F) — already sleep-window-resolved by the
                caller, same as ``_nat_vent_may_reactivate()``'s ``comfort_heat`` arg.
            comfort_cool: Comfort ceiling (°F).
            full_gate_active: The caller's own ``_nat_vent_may_reactivate(...)`` result
                for the same inputs — soft-start stands down once the full gate would
                already apply, so the two gates never compete for the same activation.
        """
        if not self.config.get(CONF_NAT_VENT_SOFT_START_ENABLED, DEFAULT_NAT_VENT_SOFT_START_ENABLED):
            return False
        inputs = NatVentSoftStartGateInputs(
            outdoor=outdoor,
            indoor=indoor,
            comfort_heat=comfort_heat,
            comfort_cool=comfort_cool,
            fan_mode=self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
            outdoor_today_peak=self._outdoor_temp_today_peak,
            outdoor_sample_count=self._outdoor_temp_today_sample_count,
            peak_decline_margin=PEAK_DECLINE_MARGIN_F,
            full_gate_active=full_gate_active,
        )
        return decide_nat_vent_soft_start_gate(inputs)

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

    def _resolve_classification_fsm_state(
        self,
        *,
        classification: DayClassification,
        predicted_indoor: list[dict] | None,
    ) -> ClassificationDecision:
        """Single dispatch point for apply_classification()'s classification FSM
        (Issue #742). Builds one ``ClassificationFsmInputs`` snapshot from live
        engine/config state and returns the composed decision (gate +
        ceiling-guard outcome).

        Read-only — this resolver never mutates engine state. The caller
        (``apply_classification()``, unconditional as of Issue #757 Phase 7 —
        see this module's ``__init__`` for the graduation note) is responsible
        for acting on the returned ``ClassificationDecision`` — see
        ``_apply_ode_ceiling_guard_decision()`` for the side-effecting half.

        Recomputes the same ``decide_scheduled_band_gate()`` result
        ``apply_classification()`` already computed earlier in the same call
        (via its own unconditional, non-flag-gated ``_gate = decide_scheduled_band_gate(...)``
        line) — harmless redundancy, not a behavior risk: nothing between that
        earlier call and this one mutates any of the 5 flags the gate reads, so
        the result is guaranteed identical. Kept this way (rather than
        threading the earlier `_gate` value through as a parameter) so this
        resolver's calling convention matches every other ``_resolve_*_fsm_state()``
        dispatcher's shape: one snapshot in, one full decision out.
        """
        _thermal = self._thermal_model or {}
        comfort_cool = self.config.get("comfort_cool")
        inputs = ClassificationFsmInputs(
            occupancy_mode=self._occupancy_mode,
            manual_override_active=self._manual_override_active,
            paused_by_door=self._paused_by_door,
            natural_vent_active=self._natural_vent_active,
            whf_owns_hvac=self._whf_owns_hvac(),
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
            fan_mode=self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED),
            predicted_indoor=predicted_indoor,
            hvac_mode=classification.hvac_mode,
            k_passive=_thermal.get("k_passive"),
            confidence_k_passive=_thermal.get("confidence_k_passive") or _thermal.get("confidence", "none"),
            k_passive_via_bridge=bool(_thermal.get("k_passive_via_bridge")),
            k_active_cool=_thermal.get("k_active_cool"),
            comfort_cool=comfort_cool,
            outdoor=self._last_outdoor_temp,
            indoor=self._get_indoor_temp_f(),
            ceiling_threshold=self._ceiling_threshold(comfort_cool),
            now=dt_util.now(),
        )
        event = ClassificationFsmEvent(kind=ClassificationFsmEventKind.CYCLE_EVALUATED, inputs=inputs)
        return _classification_fsm_transition(event)

    async def _apply_ode_ceiling_guard_decision(
        self,
        classification: DayClassification,
        predicted_indoor: list[dict] | None,
        decision: ClassificationDecision,
    ) -> None:
        """Side-effecting shell for the ODE ceiling guard's FSM-authoritative
        branch (Issue #742). Byte-identical logging/event/HVAC-write behavior
        to the legacy inline block that used to live in ``apply_classification()``
        (removed at graduation, Issue #757 Phase 7), driven by
        ``decision.ceiling_decision`` instead of re-deriving it inline. Called
        unconditionally from ``apply_classification()``'s own call site.

        ``decision.ceiling_eligibility`` is not consulted directly here (only
        ``decision.ceiling_decision``) — by construction, this method is only
        ever called from the same code path production's own
        ``if predicted_indoor and classification.hvac_mode == "off":`` line
        guards (the DEFER_OCCUPANCY/DEFER_PAUSED/DEFER_NAT_VENT-with-savings/
        DEFER_NAT_VENT-with-WHF-archetype short-circuits all return from
        ``apply_classification()`` before this line is ever reached — see
        ``classification_fsm.py``'s own module docstring), so
        ``ceiling_eligibility`` is always ``EVALUATED`` here in practice; the
        one remaining internal branch (``OdeCeilingGuardOutcome.NOT_APPLICABLE``,
        for predicted_indoor/hvac_mode edge cases the pure function itself
        checks) is handled by the early-return below, exactly mirroring
        production's own outer `if` guard.
        """
        outcome_obj = decision.ceiling_decision
        if outcome_obj is None or outcome_obj.outcome is OdeCeilingGuardOutcome.NOT_APPLICABLE:
            return

        _thermal = self._thermal_model or {}
        _k_passive = _thermal.get("k_passive")
        _conf = _thermal.get("confidence_k_passive") or _thermal.get("confidence", "none")
        _k_via_bridge = bool(_thermal.get("k_passive_via_bridge"))
        _k_active_cool = _thermal.get("k_active_cool")
        _comfort_cool_cg = self.config.get("comfort_cool")
        _outdoor = self._last_outdoor_temp
        _indoor_cg = self._get_indoor_temp_f()
        _unit = self.config.get("temp_unit", "fahrenheit")

        _LOGGER.debug(
            "ODE ceiling guard eval: %d points, comfort_cool=%s, outdoor=%s, indoor=%s,"
            " k_passive=%s, conf=%s, bridge=%s",
            len(predicted_indoor) if predicted_indoor else 0,
            _comfort_cool_cg,
            _outdoor,
            _indoor_cg,
            _k_passive,
            _conf,
            _k_via_bridge,
        )

        outcome = outcome_obj.outcome
        if outcome is OdeCeilingGuardOutcome.MODEL_INELIGIBLE:
            _LOGGER.debug("ODE ceiling guard: skipped — k_passive=%s, conf=%s", _k_passive, _conf)
            return
        if outcome is OdeCeilingGuardOutcome.MISSING_TEMPS:
            _LOGGER.debug("ODE ceiling guard: skipped — missing outdoor/indoor temps")
            return
        if outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD:
            _LOGGER.debug(
                "ODE ceiling guard: dormant — no ceiling-based compressor handoff for this"
                " fan archetype (WHOLE_HOUSE/BOTH); free cooling is direction-only"
            )
            return
        if outcome is OdeCeilingGuardOutcome.DORMANT:
            _ceiling_threshold_val = self._ceiling_threshold(_comfort_cool_cg)
            _LOGGER.debug(
                "ODE ceiling guard: dormant — outdoor %.1f <= indoor %.1f, nat-vent running,"
                " indoor <= ceiling threshold %s (free cooling viable)",
                _outdoor,
                _indoor_cg,
                _ceiling_threshold_val,
            )
            return
        if outcome is OdeCeilingGuardOutcome.NO_BREACH_PREDICTED:
            _tolerance = CEILING_BRIDGE_TOLERANCE_F if _k_via_bridge else 0.0
            _threshold = _comfort_cool_cg + _tolerance
            _LOGGER.debug("ODE ceiling guard: dormant — no breach above %.1f°F predicted", _threshold)
            return

        # STANDING_BY or ESCALATE — both logged a breach-predicted INFO line first.
        _breach_ts = outcome_obj.breach_ts
        _hours_to_breach = outcome_obj.hours_to_breach
        _lead_min = outcome_obj.lead_min
        _LOGGER.info(
            "ODE ceiling guard: breach predicted at %s (%.1fh away), outdoor=%.1f, indoor=%.1f, nat_vent=%s",
            _breach_ts.strftime("%H:%M"),
            _hours_to_breach,
            _outdoor,
            _indoor_cg,
            self._natural_vent_active,
        )

        if outcome is OdeCeilingGuardOutcome.STANDING_BY:
            _LOGGER.debug(
                "ODE ceiling guard: standing by — breach %.1fh away, need %.0fmin lead time",
                _hours_to_breach,
                _lead_min,
            )
            return

        # ESCALATE
        _LOGGER.info(
            "ODE ceiling guard: active — setting HVAC cool, target=%.1f (breach %.1fh, lead=%.0fmin, k_cool=%s)",
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
            self._nat_vent_soft_start = False
            if self._emit_event_callback:
                self._emit_event_callback(
                    "nat_vent_ceiling_escalation",
                    {
                        "indoor": _indoor_cg,
                        "outdoor": _outdoor,
                        "comfort_cool": _comfort_cool_cg,
                        "hours_to_breach": round(_hours_to_breach, 2),
                        "lead_min": round(_lead_min),
                        "k_active_cool": _k_active_cool,
                    },
                )
        _cs_cg = self.hass.states.get(self.climate_entity)
        _old_mode_cg = _cs_cg.state if _cs_cg else None
        _old_setpoint_raw_cg = _cs_cg.attributes.get("temperature") if _cs_cg else None
        _old_setpoint_f_cg = to_fahrenheit(_old_setpoint_raw_cg, _unit) if _old_setpoint_raw_cg is not None else None
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

    def _resolve_occupancy_away_vacation_fsm_state(self, *, mode: str) -> AwayVacationDecision:
        """Single dispatch point for handle_occupancy_away()'s/
        handle_occupancy_vacation()'s occupancy FSM (Issue #744). Builds one
        ``AwayVacationInputs`` snapshot from live engine state and returns the
        composed decision.

        Read-only — this resolver never mutates engine state. The caller (either
        handler) is responsible for acting on the returned ``AwayVacationDecision``
        via ``_apply_occupancy_away_vacation_decision()``. ``mode`` ("away" or
        "vacation") does not affect the pure decision itself (see
        ``occupancy_fsm.py``'s module docstring) — it is threaded through only for
        the apply-shell's logging/event-payload text.
        """
        inputs = AwayVacationInputs(
            paused_by_door=self._paused_by_door,
            manual_override_active=self._manual_override_active,
            has_classification=self._current_classification is not None,
        )
        return decide_away_vacation_dispatch(inputs)

    async def _apply_occupancy_away_vacation_decision(self, mode: str, decision: AwayVacationDecision) -> None:
        """Side-effecting shell for the occupancy FSM's away/vacation-authoritative
        branch (Issue #744). Byte-identical logging/event/HVAC-write behavior to the
        legacy inline blocks in ``handle_occupancy_away()``/``handle_occupancy_vacation()``,
        driven by ``decision`` instead of re-deriving the branch inline. Called
        unconditionally — see each handler's own call site. ``mode`` is
        ``"away"`` or ``"vacation"``.
        """
        if decision.outcome is AwayVacationOutcome.SUPPRESSED_PAUSED:
            _LOGGER.info(
                "Occupancy %s — door/window open (_paused_by_door=True), "
                "skipping setback band; occupancy recorded, HVAC remains off",
                mode,
            )
            if self._emit_event_callback and not self._recent_duplicate("occupancy_setback_suppressed_paused", (mode,)):
                _pause_minutes = (
                    (dt_util.now() - self._paused_since).total_seconds() / 60.0
                    if self._paused_since is not None
                    else None
                )
                self._emit_event_callback(
                    "occupancy_setback_suppressed_paused",
                    {
                        "occupancy": mode,
                        "reason": "paused_by_door",
                        "paused_entity": self._paused_entity,
                        "paused_minutes": round(_pause_minutes) if _pause_minutes is not None else None,
                    },
                )
            return

        if decision.clear_override:
            _LOGGER.info(
                "Occupancy transition to %s — clearing manual override (mode=%s since %s)",
                mode,
                self._manual_override_mode,
                self._manual_override_time,
            )
            self.clear_manual_override(reason=f"occupancy_{mode}")

        if decision.outcome is AwayVacationOutcome.NO_CLASSIFICATION:
            if mode == "away":
                _LOGGER.warning("Occupancy away handler skipped — no day classification available")
            return

        c = self._current_classification
        _occ_mode_const = OCCUPANCY_AWAY if mode == "away" else OCCUPANCY_VACATION
        _band = select_comfort_band(
            c,
            self.config,
            occupancy_mode=_occ_mode_const,
            in_sleep_window=False,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
        )
        # Issue #591: WINDOWED dedup — see handle_occupancy_away()'s original comment for
        # the full rationale (repeated bands are often intentional re-confirmations, but
        # an unguarded site reopens the #584 double-emit shape within seconds).
        _sig = (mode, round(_band.floor, 2), round(_band.ceiling, 2))
        if self._emit_event_callback and not self._recent_duplicate("occupancy_setback", _sig, window_seconds=600):
            self._emit_event_callback(
                "occupancy_setback",
                {
                    "mode": mode,
                    "floor": _band.floor,
                    "ceiling": _band.ceiling,
                    "occupancy": mode,
                    "indoor_f": self._indoor_f_for_event(),
                },
            )
        await self._apply_comfort_band(_band, reason=f"occupancy {mode} — setback band")

    def _resolve_occupancy_home_fsm_state(self) -> HomeDecision:
        """Single dispatch point for handle_occupancy_home()'s occupancy FSM
        (Issue #744). Builds one ``HomeInputs`` snapshot from live engine/config
        state and returns the composed decision. Read-only — see
        ``_resolve_occupancy_away_vacation_fsm_state()``'s matching docstring.
        """
        c = self._current_classification
        indoor_temp = self._get_indoor_temp_f()
        comfort_f: float | None = None
        setback_f: float | None = None
        if c is not None and c.hvac_mode in ("heat", "cool"):
            comfort_f = self.config["comfort_heat"] if c.hvac_mode == "heat" else self.config["comfort_cool"]
            setback_f = self.config["setback_heat"] if c.hvac_mode == "heat" else self.config["setback_cool"]
        debounce_seconds = self.config.get(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS)
        seconds_since: float | None = None
        if self._last_welcome_home_notified is not None:
            seconds_since = (dt_util.now() - self._last_welcome_home_notified).total_seconds()
        inputs = HomeInputs(
            has_classification=c is not None,
            hvac_mode=c.hvac_mode if c is not None else None,
            indoor_temp_f=indoor_temp,
            comfort_f=comfort_f,
            setback_f=setback_f,
            debounce_seconds=float(debounce_seconds),
            seconds_since_last_notified=seconds_since,
        )
        return decide_home_dispatch(inputs)

    async def _apply_occupancy_home_decision(self, decision: HomeDecision) -> None:
        """Side-effecting shell for the occupancy FSM's home-authoritative branch
        (Issue #744). Byte-identical logging/event/HVAC-write/notify behavior to the
        legacy inline block in ``handle_occupancy_home()``, driven by ``decision``
        instead of re-deriving the branch inline. Called unconditionally — see
        ``handle_occupancy_home()``'s own call site.
        """
        if not decision.restore and decision.notify is HomeNotifyOutcome.NONE:
            return  # mirrors legacy's `if not c: return`

        c = self._current_classification
        if decision.restore:
            await self._set_temperature_for_mode(c, reason=f"occupancy home — restoring {c.hvac_mode} comfort")
            comfort = self.config["comfort_heat"] if c.hvac_mode == "heat" else self.config["comfort_cool"]
            if self._emit_event_callback:
                self._emit_event_callback(
                    "occupancy_comfort_restored",
                    {"mode": c.hvac_mode, "target_f": comfort, "indoor_f": self._indoor_f_for_event()},
                )

        if decision.notify is HomeNotifyOutcome.SUPPRESSED_NEAR_COMFORT:
            indoor_temp = self._get_indoor_temp_f()
            comfort = self.config["comfort_heat"] if c.hvac_mode == "heat" else self.config["comfort_cool"]
            setback = self.config["setback_heat"] if c.hvac_mode == "heat" else self.config["setback_cool"]
            _LOGGER.info(
                "Welcome home notification suppressed — indoor %.1f°F already near comfort %.1f°F"
                " (dist_comfort=%.1f < dist_setback=%.1f)",
                indoor_temp,
                comfort,
                abs(indoor_temp - comfort),
                abs(indoor_temp - setback),
            )
            self._last_welcome_home_notified = dt_util.now()
            return

        if decision.notify is HomeNotifyOutcome.SUPPRESSED_DEBOUNCE:
            debounce_seconds = self.config.get(CONF_WELCOME_HOME_DEBOUNCE, DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS)
            elapsed = (
                (dt_util.now() - self._last_welcome_home_notified).total_seconds()
                if self._last_welcome_home_notified is not None
                else 0.0
            )
            _LOGGER.info(
                "Welcome home notification suppressed — debounce active (%.0fs elapsed, window=%ds)",
                elapsed,
                debounce_seconds,
            )
            return

        if decision.notify is HomeNotifyOutcome.SEND:
            self._last_welcome_home_notified = dt_util.now()
            await self._notify(
                "🏠 Welcome home! Restoring comfort temperature. Should feel normal in about 20–30 minutes.",
                "Climate Advisor",
                notification_type="occupancy_home",
            )

    def _build_fan_fsm_inputs(
        self,
        *,
        indoor: float | None = None,
        outdoor: float | None = None,
        hysteresis: float | None = None,
        in_sleep_window: bool | None = None,
        fan_min_runtime_minutes: float | None = None,
        natural_vent_active: bool | None = None,
        fan_drift_tick_count: int | None = None,
    ) -> FanFsmInputs:
        """Build the fan/WHF FSM's input snapshot from current engine state (Issue #731,
        Phase 4).

        Sole builder for ``FanFsmInputs`` — every field is read directly off ``self.*``,
        never via a ``_dispatched_*`` mirror, same #717 revert rationale the other three
        lifecycle builders (``_build_nat_vent_fsm_inputs()``, ``_build_door_window_fsm_inputs()``,
        ``_build_override_grace_fsm_inputs()``) already follow.

        The fields that are NOT attributes on ``self`` (``indoor``/``outdoor``/``hysteresis``/
        ``in_sleep_window``/``fan_min_runtime_minutes``/``natural_vent_active``/
        ``fan_drift_tick_count``) are accepted as optional keyword overrides — same
        "``None`` means fall back to a live-resolved default" convention
        ``_build_nat_vent_fsm_inputs()`` documents for its own per-call-site parameters
        (``hysteresis``, ``paused_by_door``, ``fan_hardware_active``).

        ``fan_drift_tick_count`` exists because ``_reconcile_fan_physical_drift()``
        (Issue #757 Step 2 fix) mutates ``self._fan_drift_tick_count`` to the real
        pure function's ``next_tick_count`` BEFORE dispatching ``DRIFT_TICK`` for
        mirror-sync — reading ``self._fan_drift_tick_count`` live here would feed
        ``_transition_on_drift_tick()``'s own independent call to
        ``decide_fan_drift_reconciliation()`` the already-advanced count, causing it
        to silently re-progress a second time and confirm drift one real tick early
        (found via a 2-consecutive-tick test failing after only one tick). The
        ``DRIFT_TICK`` call site passes the pre-increment count explicitly so the
        FSM's re-derivation reaches the exact same outcome the real code just did,
        instead of advancing past it.

        ``physical_on`` is read LAZILY, replicating ``_reconcile_fan_physical_drift()``'s own
        laziness guard exactly (fan active, WHF/BOTH archetype, no recent CA command echo,
        physical-state callback configured) — this avoids an unconditional live state read on
        every FSM build for the 14 of 16 event kinds that never consult it.
        """
        from .fan_fsm import FanFsmInputs

        fan_mode = str(self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED))
        now = dt_util.now()

        # Mirrors _reconcile_fan_physical_drift()'s own laziness guard exactly — only
        # actually read live physical state once every cheaper guard has already passed.
        recent_fan_command = bool(
            self._is_recent_fan_command_callback and self._is_recent_fan_command_callback(threshold_seconds=30.0)
        )
        physical_state_available = bool(self._get_fan_physical_state_callback)
        physical_on: bool | None = None
        if (
            self._fan_active
            and fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH)
            and not recent_fan_command
            and physical_state_available
        ):
            physical_on = self._get_fan_physical_state_callback()

        comfort_heat_raw = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))

        return FanFsmInputs(
            fan_active=self._fan_active,
            fan_drift_tick_count=(
                fan_drift_tick_count if fan_drift_tick_count is not None else self._fan_drift_tick_count
            ),
            fan_override_active=self._fan_override_active,
            fan_remote_timer_hours=self._fan_remote_timer_hours,
            fan_min_runtime_active=self._fan_min_runtime_active,
            fan_mode=fan_mode,
            pre_fan_hvac_mode=self._pre_fan_hvac_mode,
            fan_rate_limited_until=self._fan_rate_limited_until,
            fan_rate_limited_direction=self._fan_rate_limited_direction,
            now=now,
            natural_vent_active=(
                natural_vent_active if natural_vent_active is not None else bool(self._natural_vent_active)
            ),
            last_toggle_command_time=self._fan_toggle_command_time,
            toggle_min_interval_s=float(FAN_MIN_TOGGLE_INTERVAL_S),
            recent_fan_command=recent_fan_command,
            physical_state_available=physical_state_available,
            physical_on=physical_on,
            fan_min_runtime_minutes=(
                fan_min_runtime_minutes
                if fan_min_runtime_minutes is not None
                else float(self.config.get(CONF_FAN_MIN_RUNTIME_PER_HOUR, 0.0))
            ),
            fan_running=bool(self._fan_running),
            indoor=indoor,
            outdoor=outdoor,
            comfort_heat_raw=comfort_heat_raw,
            sleep_heat=float(self.config.get(CONF_SLEEP_HEAT, comfort_heat_raw)),
            in_sleep_window=(in_sleep_window if in_sleep_window is not None else _in_sleep_window(now, self.config)),
            hysteresis=(
                hysteresis
                if hysteresis is not None
                else float(self.config.get(CONF_NAT_VENT_HYSTERESIS_F, NAT_VENT_HYSTERESIS_F))
            ),
            manual_override_active=bool(self._manual_override_active),
            manual_override_mode=self._manual_override_mode,
        )

    @property
    def fan_lifecycle_state(self) -> FanLifecycleState:
        """Current fan/WHF composed state, derived from existing flags (Issue #731,
        Phase 4).

        A lightweight, read-only counterpart to ``_build_fan_fsm_inputs()`` — this
        property only needs the 5-axis-relevant subset of fields (no per-call-site
        overrides), so it is callable at any time, from any context, without needing
        a live ``indoor``/``outdoor``/``hysteresis`` reading the way the full FSM
        builder does. Purely a computed view of ``_fan_active``/``_fan_drift_tick_count``/
        ``_fan_override_active``/``_fan_remote_timer_hours``/``_fan_min_runtime_active``/
        ``_pre_fan_hvac_mode``/``_fan_rate_limited_until``/``_fan_rate_limited_direction``,
        so it cannot desync from the flags it reads. See ``fan_lifecycle.py`` for the pure
        derivation.
        """
        fan_mode = str(self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED))
        return derive_fan_lifecycle_state(
            FanLifecycleInputs(
                fan_active=self._fan_active,
                fan_drift_tick_count=self._fan_drift_tick_count,
                fan_override_active=self._fan_override_active,
                fan_remote_timer_hours=self._fan_remote_timer_hours,
                fan_min_runtime_active=self._fan_min_runtime_active,
                fan_mode=fan_mode,
                pre_fan_hvac_mode=self._pre_fan_hvac_mode,
                fan_rate_limited_until=self._fan_rate_limited_until,
                fan_rate_limited_direction=self._fan_rate_limited_direction,
                now=dt_util.now(),
            )
        )

    def _whf_owns_hvac(self) -> bool:
        """Whether a whole-house-fan session currently owns (suppresses) the thermostat.

        True when fan_mode is WHOLE_HOUSE/BOTH AND a suppression session is active
        (``_pre_fan_hvac_mode is not None`` — the same flag ``_activate_fan``/
        ``_deactivate_fan`` use to track an active suppression, not ``_natural_vent_active``,
        which also covers HVAC-fan-mode nat-vent where HVAC is NOT suppressed).

        Issue #392 Fix 1b: this is the seed of a future ``FanSession.may_run_hvac()`` object
        (see Issue #392 shaping analysis) — a single choke-point check standing in for the
        deferred `FanSession` extraction, not a permanent standalone guard.

        Issue #731 (Phase 4): re-expressed in terms of ``fan_lifecycle_state`` — proven
        exactly equivalent to the prior direct-flag-read body (both check the same two
        conditions, ``fan_mode in (WHOLE_HOUSE, BOTH)`` and ``pre_fan_hvac_mode is not
        None``; see ``fan_lifecycle._derive_hvac_ownership()``), so this is a pure
        re-expression, not a behavior change.
        """
        return self.fan_lifecycle_state.hvac_ownership is WhfHvacOwnership.SUPPRESSING

    def _apply_fan_fsm_state(self, state: FanLifecycleState) -> None:
        """Write ``_fan_active``/``_fan_override_active``/``_fan_min_runtime_active``/
        ``_pre_fan_hvac_mode`` (None-clear only) from a ``fan_fsm.transition()`` result
        (Issue #731, Phase 4).

        The inverse of ``fan_lifecycle_state``'s derivation, for the 4 fields that
        derivation composes. Deliberately does NOT touch — and Phase 5's real call
        sites remain the sole owners of — every other fan/WHF-adjacent field:
        ``_fan_on_since``, ``_fan_override_time``, ``_fan_remote_timer_hours``,
        ``_fan_remote_speed``, ``_pre_fan_hvac_mode``'s string VALUE (only the
        None-clear below is owned here — the string it's set TO on suppression entry
        is a payload write owned by the real ``_suppress_hvac_for_whf()`` caller, not
        this derivation), ``_fan_toggle_command_time``, ``_fan_rate_limited_until``,
        ``_fan_rate_limited_direction``, ``_fan_drift_tick_count``, ``_fan_thermo_cancel``,
        ``_fan_thermo_generation``, ``_fan_min_cycle_cancel``, ``_fan_command_time``,
        ``_fan_command_pending``, ``_recent_fan_command_context_ids`` — none of those are
        part of the 4-field derivation ``fan_lifecycle.derive_fan_lifecycle_state()``
        composes (same "outside the N-field derivation stays a direct per-caller write"
        rule ``_apply_override_grace_fsm_state()``'s own docstring documents). This is
        safe because every excluded field is either a timestamp/payload/cancel-token
        the real side-effecting method already sets before or alongside calling the
        dispatcher, or (for ``_fan_rate_limited_until``/``_fan_rate_limited_direction``/
        ``_fan_drift_tick_count``) a field this FSM's own ``rate_limit``/physical-drift
        axes read but do not themselves persist — the real ``_resolve_fan_fsm_state()``
        dispatcher call sites for ``ACTIVATE_REQUESTED``/``DEACTIVATE_REQUESTED``/
        ``DRIFT_TICK`` are Phase 5's job to wire those specific writes alongside this
        method's 4-field write, not this method's own responsibility.

        Called only from the authoritative branch of ``_resolve_fan_fsm_state()`` —
        the real command/override/cycling primitives never write these 4 flags
        themselves when the switch is on; only this method (FSM path) or the paired
        legacy closure (non-authoritative path) does, exclusively, so the switch
        genuinely governs which computation determines these flags rather than one
        silently overwriting the other.
        """
        self._fan_active = state.physical in (FanPhysicalState.ON, FanPhysicalState.ON_DRIFT_SUSPECTED)
        self._fan_override_active = state.override is not FanOverrideState.NONE
        self._fan_min_runtime_active = state.cycling is FanCyclingState.ACTIVE
        if state.hvac_ownership is WhfHvacOwnership.NONE:
            self._pre_fan_hvac_mode = None
        # deliberately NEVER sets _pre_fan_hvac_mode to a string value here — that's a
        # payload write owned by the real _suppress_hvac_for_whf() caller, not this
        # derivation (see docstring above for the full exclusion list and why it's safe)

    def _resolve_fan_fsm_state(
        self,
        *,
        kind: FanFsmEventKind,
        origin_state: FanLifecycleState | None = None,
        **input_overrides: Any,
    ) -> FanTransition:
        """Single dispatch point for every fan/WHF flag-derivation call site (Issue #731).

        Graduated to FSM-only in Issue #757 Step 2, once ``_fan_fsm_authoritative``
        had been permanently True in production since Phase 5 and was proven
        behavior-equivalent to the deleted legacy path via an offline differential
        comparator (zero divergence across the golden+pending corpus). There is no
        legacy branch left — this always writes ``_fan_active``/``_fan_override_active``/
        ``_fan_min_runtime_active``/``_pre_fan_hvac_mode`` (None-clear) for a given call
        via the FSM transition. Real side effects — hardware commands, timer scheduling,
        grace periods, event emission the 6 imported pure decision functions'
        ``fan_fsm.py`` module docstring documents as still shell-owned — are NOT owned
        here; callers must already have run (or still must run, reading this
        transition's shell-directive fields) the real side-effecting half of whichever
        handler this call site belongs to.

        Unlike the other 3 dispatchers (which return ``None``), this one returns the
        full ``FanTransition`` — the fan/WHF FSM's decision-bearing event kinds
        (``ACTIVATE_REQUESTED``/``DEACTIVATE_REQUESTED``/``DRIFT_TICK``/
        ``MIN_RUNTIME_CYCLE_ON``/``MIN_RUNTIME_CYCLE_OFF``/``THERMO_BACKSTOP_TICK``/
        ``THERMOSTAT_CHECK_TICK``) carry shell-directive fields (e.g.
        ``rate_limit_outcome``, ``drift_outcome``, ``cycle_should_deactivate``,
        ``thermostat_outcome``) callers need to act on — a bare ``None`` return would
        discard exactly the information those callers exist to read.

        ``origin_state`` defaults to a live read of ``self.fan_lifecycle_state`` —
        correct for every site except one whose real primitive already mutates a
        flag this FSM composes before the dispatcher would otherwise read it live
        (mirrors ``_resolve_override_grace_fsm_state()``'s own ``origin_state``
        carve-out); the caller passes it explicitly in that case.

        ``**input_overrides`` is forwarded verbatim to ``_build_fan_fsm_inputs()`` —
        the per-call-site keyword overrides (``indoor``/``outdoor``/``hysteresis``/
        ``in_sleep_window``/``fan_min_runtime_minutes``/``natural_vent_active``) that
        builder documents.

        Also the single emit point for WHF_HVAC_SUPPRESSED/RELEASED — a before/after
        diff of ``_whf_owns_hvac()`` around the ``transition()`` call, absorbing the
        responsibility the now-deleted ``_resolve_whf_hvac_suppression()`` used to own
        ("this is purely the mirror-sync half of the pattern... `_whf_owns_hvac()` has
        no companion FSM to switch to" — this dispatcher IS that companion now). Phase 5
        re-pointed all 4 of ``_resolve_whf_hvac_suppression()``'s real call sites at
        this dispatcher and deleted it — it no longer exists in this module.
        """
        from .fan_fsm import FanFsmEvent
        from .fan_fsm import transition as _fan_transition

        state = origin_state if origin_state is not None else self.fan_lifecycle_state
        _whf_before = self._whf_owns_hvac()
        result = _fan_transition(
            state,
            FanFsmEvent(kind=kind, inputs=self._build_fan_fsm_inputs(**input_overrides)),
        )
        self._apply_fan_fsm_state(result.to_state)
        self._emit_boolean_transition(
            before=_whf_before,
            after=self._whf_owns_hvac(),
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail=kind.value,
            caller="_resolve_fan_fsm_state",
        )
        return result

    async def _suppress_hvac_for_whf(self, *, reason: str) -> None:
        """Capture current HVAC mode and turn it off for a whole-house-fan session (Issue #495).

        Scoped to WHOLE_HOUSE/BOTH only — FAN_MODE_HVAC coexists with the compressor by
        design (that fan IS the thermostat's own blower). Idempotent: a session that already
        owns HVAC (``_pre_fan_hvac_mode is not None``) is not re-captured, so a repeated call
        (e.g. two manual-override re-stamps in quick succession — the live incident showed a
        physical fan-on at 20:45:32 followed by an RF remote press at 20:48:40) never clobbers
        the captured mode with "off".

        Shared by ``_activate_fan()`` (CA-initiated) and ``handle_fan_manual_override()``
        (manual/remote-detected) so the WHF/HVAC mutual-exclusion rule has exactly one
        suppression path — see the #400/#402/#417/#456/#458 "sibling threshold drift" history
        for why a second copy of this rule is the wrong move.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return
        if self._pre_fan_hvac_mode is not None:
            return
        _cs = self.hass.states.get(self.climate_entity)
        prior_mode = _cs.state if _cs else None

        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
        #
        # Issue #731 Phase 7 fix: WHF_SUPPRESSION_REQUESTED/WHF_RELEASE_REQUESTED are
        # Group-1 "caller-already-decided" kinds (fan_fsm.py's own module docstring:
        # "re-derive composed state against the already-post-change snapshot the
        # caller supplied"). That means the real ``_pre_fan_hvac_mode`` write MUST
        # happen before the dispatch call — mirroring _stop_fan_min_runtime_cycles()'s
        # reference pattern (write the real flag directly, then dispatch is purely
        # mirror-sync). before/after diff capture is likewise hoisted so it observes
        # the real transition.
        from .fan_fsm import FanFsmEventKind

        _whf_origin_state = self.fan_lifecycle_state
        _whf_before = self._whf_owns_hvac()
        self._pre_fan_hvac_mode = prior_mode

        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.WHF_SUPPRESSION_REQUESTED,
            origin_state=_whf_origin_state,
        )
        self._emit_boolean_transition(
            before=_whf_before,
            after=self._whf_owns_hvac(),
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail=None,
            caller="_suppress_hvac_for_whf",
        )
        await self._set_hvac_mode("off", reason=reason)
        if self._emit_event_callback:
            self._emit_event_callback("whf_hvac_suppressed", {"prior_mode": prior_mode, "reason": reason})

    def _release_whf_and_reclassify(self, *, reason: str) -> None:
        """End a WHF HVAC-suppression session: release ownership, then reclassify (Issue #495).

        A manual/remote WHF session can run for hours (an 8h QuietCool RF remote timer
        selection), so the HVAC mode captured in ``_pre_fan_hvac_mode`` at activation time is
        often stale by exit — the session can span a sleep-setback transition. Rather than
        blindly restoring that captured mode (what ``_deactivate_fan()`` does for the short
        CA-initiated nat-vent cycle), this releases ownership and lets classification compute
        the correct current mode AND setpoint in one shot, via the coordinator's existing
        fan-off reassert path (``_async_reassert_setpoint_after_fan_off``, Issue #359 Fix A).

        Guard: no-op if the WHF is still physically running, checked via the same ground-truth
        callback ``_reconcile_fan_physical_drift()`` already uses
        (``_get_fan_physical_state_callback`` — returns True/False when feedback is available,
        None in command-only mode). The post-grace fan reconcile
        (``_on_post_grace_fan_check`` -> ``reconcile_fan_on_startup``) owns the "still running"
        case; releasing here too would race it.

        Guard (Issue #530): also no-op if ``_natural_vent_active`` is still True. Physical
        fan state alone is not sufficient — ``clear_fan_override()`` calls this method
        whenever an override is cleared, including cases where the fan happens to be
        physically off at that instant but CA still considers a nat-vent session live (e.g.
        wake-up clearing a leftover fan override while nat-vent is mid-session, confirmed
        live: ``handle_morning_wakeup()`` decided ``DEFER_NAT_VENT`` — "leaving fan alone" —
        moments before this exact call released suppression anyway, letting the very next
        comfort-band write arm active HVAC with windows still open). As long as
        ``_natural_vent_active`` is True, the nat-vent session itself owns when suppression
        ends — via ``_exit_nat_vent()`` — not this override-clear side effect.
        """
        if self._pre_fan_hvac_mode is None:
            return  # no suppression session to release
        if self._natural_vent_active:
            _LOGGER.debug(
                "WHF release-and-reclassify skipped (%s) — nat-vent session still active,"
                " _exit_nat_vent() owns suppression release",
                reason,
            )
            return
        if self._get_fan_physical_state_callback and self._get_fan_physical_state_callback():
            _LOGGER.debug("WHF release-and-reclassify skipped (%s) — fan still physically on", reason)
            return

        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
        #
        # Issue #731 Phase 7 fix: see _suppress_hvac_for_whf()'s matching comment —
        # WHF_RELEASE_REQUESTED is also a Group-1 "caller-already-decided" kind, so the
        # real _pre_fan_hvac_mode write (and the before/after diff it feeds) is hoisted
        # above the dispatch call.
        from .fan_fsm import FanFsmEventKind

        _whf_origin_state = self.fan_lifecycle_state
        _whf_before = self._whf_owns_hvac()
        self._pre_fan_hvac_mode = None

        # release _whf_owns_hvac() BEFORE the reclassify write
        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.WHF_RELEASE_REQUESTED,
            origin_state=_whf_origin_state,
        )
        self._emit_boolean_transition(
            before=_whf_before,
            after=self._whf_owns_hvac(),
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail=None,
            caller="_release_whf_and_reclassify",
        )
        _LOGGER.info("WHF session ended (%s) — releasing HVAC suppression, reclassifying current state", reason)
        if self._emit_event_callback:
            self._emit_event_callback("whf_hvac_released", {"reason": reason})
        if self._reclassify_callback:
            self._reclassify_callback()

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
        comfort_heat = float(self.config.get("comfort_heat", DEFAULT_COMFORT_HEAT))
        comfort_cool = float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL))

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
                _assist_sig = ("sleep_window", round(comfort_heat, 2), round(comfort_cool, 2))
                if self._emit_event_callback and not self._recent_duplicate("nat_vent_ac_assist_armed", _assist_sig):
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
            _assist_sig = ("full_band", round(comfort_heat, 2), round(comfort_cool, 2))
            if self._emit_event_callback and not self._recent_duplicate("nat_vent_ac_assist_armed", _assist_sig):
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

    async def _command_whf_control_entity(self, desired_on: bool, *, reason: str) -> bool:
        """Command the WHF control/transmitter entity, guarding against HA's belief-based
        command dedup in dual-entity (split control/detect) setups (Issue #449).

        Scope: WHOLE_HOUSE_FAN/BOTH archetypes only, and only when dual-entity feedback
        ground truth is available (``_get_fan_physical_state_callback`` returns non-None).
        Single-entity/command-only WHF setups and FAN_MODE_HVAC are entirely untouched —
        a single-entity control can be trusted, and the separate HVAC fan-mode command
        path is unaffected.

        A one-way transmitter entity has no feedback of its own; its HA-reported state
        can silently drift from physical reality. Confirmed via real HA history (Issue
        #449): ~14 repeated ``turn_on`` calls produced zero state transitions once the
        entity's HA-reported state already read "on", because the physical fan had been
        turned off by something outside HA's command path. When the control entity's
        belief already matches the desired state AND ground truth disagrees, force a
        real transition by commanding the OPPOSITE state first, waiting 5 seconds, then
        the desired state. When both signals already agree, do nothing — no redundant
        command.

        Returns:
            True if an HA service call was issued, False if the desired state was
            already confirmed correct and nothing needed to change.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode not in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
            return False
        fan_entity = self.config.get(CONF_FAN_ENTITY)
        if not fan_entity:
            return False
        domain = fan_entity.split(".")[0]  # "fan" or "switch"
        desired_service = "turn_on" if desired_on else "turn_off"

        ground_truth = self._get_fan_physical_state_callback() if self._get_fan_physical_state_callback else None

        if ground_truth is not None:
            control_state = self.hass.states.get(fan_entity)
            control_matches_desired = bool(control_state and (control_state.state == "on") == desired_on)
            if control_matches_desired and ground_truth == desired_on:
                _LOGGER.debug(
                    "WHF control entity and detected physical state already agree (%s) — no command needed (%s)",
                    "on" if desired_on else "off",
                    reason,
                )
                return False
            if control_matches_desired and ground_truth != desired_on:
                _LOGGER.warning(
                    "WHF control entity already reads %s but detected physical state"
                    " disagrees — forcing a real transition before reasserting (%s)",
                    "on" if desired_on else "off",
                    reason,
                )
                opposite_service = "turn_off" if desired_on else "turn_on"
                await self._call_fan_service_with_context(domain, opposite_service, fan_entity)
                await asyncio.sleep(5)
                await self._call_fan_service_with_context(domain, desired_service, fan_entity)
                return True

        # Not dual-entity mode, ground truth unavailable this tick, or control entity
        # doesn't yet match the desired state — a plain command is a genuine transition.
        await self._call_fan_service_with_context(domain, desired_service, fan_entity)
        return True

    async def _call_fan_service_with_context(self, domain: str, service: str, entity_id: str) -> None:
        """Issue a fan/switch service call carrying a fresh HA Context (Issue #482).

        HA attaches the originating service call's ``Context`` to the resulting
        state-changed ``Event``. Recording our own context here lets
        ``coordinator._async_fan_entity_changed()`` check ``event.context`` against
        recently-issued CA command contexts as an additional CA-attribution signal,
        alongside the existing ``_fan_command_pending``/timing-heuristic guards.

        Scope note: context propagation through third-party fan/switch integrations
        (particularly a one-way RF transmitter entity with no feedback of its own) is
        not guaranteed by HA core — some integrations do not carry the calling
        context through to their own state write. This is why the context check is
        additive/corroborating rather than a replacement for the existing checks.
        """
        cmd_context = Context()
        self._record_fan_command_context(cmd_context.id)
        await self.hass.services.async_call(domain, service, {"entity_id": entity_id}, context=cmd_context)

    def _record_fan_command_context(self, context_id: str) -> None:
        """Record a just-issued CA fan-command context id (Issue #561).

        Replaces a single last-write-wins ``_fan_command_context_id`` attribute, which
        could be overwritten by a second overlapping fan command before the first
        command's resulting state-changed event was evaluated against it — causing CA's
        own action to be misattributed as a manual override (the "whole-house fan
        manually turned on" message this issue's report disputed). Keeping a short-lived
        set instead means either command's context can still be matched, regardless of
        which one the coordinator's state-change listener happens to see first.
        """
        now = dt_util.now()
        cutoff = now - timedelta(seconds=30)
        self._recent_fan_command_context_ids = [
            (cid, ts) for cid, ts in self._recent_fan_command_context_ids if ts >= cutoff
        ]
        self._recent_fan_command_context_ids.append((context_id, now))

    def fan_command_context_matches(self, event_context_id: str | None, event_context_parent_id: str | None) -> bool:
        """Return True if either id matches a CA fan command issued in the last 30s (Issue #561).

        Checked by ``coordinator._async_fan_entity_changed()`` in place of the old
        single-id equality check.
        """
        if event_context_id is None and event_context_parent_id is None:
            return False
        cutoff = dt_util.now() - timedelta(seconds=30)
        return any(
            ts >= cutoff and cid in (event_context_id, event_context_parent_id)
            for cid, ts in self._recent_fan_command_context_ids
        )

    def _fan_toggle_rate_limited(self, *, action: str, reason: str) -> FanCommandResult | None:
        """Hard safety backstop against rapid fan on/off/on cycling (Issue #641).

        Returns ``None`` if this toggle is NOT rate-limited (caller should proceed).
        Otherwise returns ``FanCommandResult.RATE_LIMITED_NEW`` the first time a given
        deferral window is reported, or ``RATE_LIMITED_DUP`` for every later call that
        lands in the *same* window (Issue #649) — e.g. two independent decision paths
        noticing the same condition in one tick, or `fan_thermostat_check()` re-running
        on every subsequent temperature-change tick while still blocked. Before #649
        every such call logged its own WARNING and fired its own
        ``incident_detected``/``fan_rapid_cycling`` event, producing an unbounded string
        of duplicate, misleadingly-framed Activity Report rows for what is physically
        one ongoing blocked state — the floor doing its job, not an anomaly, so it is no
        longer reported as an incident at all.

        Defense-in-depth: independent of any specific root cause, protects the physical
        WHF/HVAC-fan relay from rapid cycling regardless of which upstream decision logic
        produced the reversal (the WHF fast-cycling incident that motivated this — a
        proactive-floor exit immediately followed by reactivation — is fixed at its own
        root cause elsewhere, but this backstop holds even if a different, future gap
        produces the same physical symptom).

        Only ever compares against ``self._fan_toggle_command_time`` — stamped
        exclusively by ``_activate_fan``/``_deactivate_fan``'s own command sites, not
        the broader ``_fan_command_time`` echo-tracking field (see that attribute's
        declaration for why the two must stay separate: an internal bookkeeping-only
        stamp with no real physical toggle must not poison this guard). A genuine
        user/RF-remote fan action never reaches this check either way, since both
        callers already return early when ``self._fan_override_active`` is set.

        isinstance guard: several call sites across the codebase (e.g. the fan-mode
        assertion inside ``_set_hvac_mode``) stamp fan-related timestamps from
        ``dt_util.now()`` without the caller having patched a real clock in tests,
        leaving a ``MagicMock`` rather than a ``datetime`` — matching the defensive
        pattern this codebase already uses elsewhere for mocked-engine timestamp reads
        (see ``_format_grace_remaining()``). Treat anything that isn't a real timestamp
        as "no prior command", never raise.
        """
        outcome, applies_at = decide_fan_toggle_rate_limit(
            FanToggleRateLimitInputs(
                last_toggle_command_time=self._fan_toggle_command_time,
                action=action,
                rate_limited_until=self._fan_rate_limited_until,
                rate_limited_direction=self._fan_rate_limited_direction,
                min_interval_s=FAN_MIN_TOGGLE_INTERVAL_S,
                now=dt_util.now(),
            )
        )

        if outcome is FanToggleRateLimitOutcome.ALLOW:
            return None

        if outcome is FanToggleRateLimitOutcome.DEFER_DUPLICATE:
            # Issue #649: a deferral window already has a WARNING/report on record if
            # _fan_rate_limited_until already points at this exact applies_at moment for
            # the same direction — every later call in that same window is a silent
            # duplicate of already-known information.
            _LOGGER.debug(
                "Fan toggle already deferred until %s — skipping duplicate report (%s)",
                applies_at.strftime("%H:%M:%S"),
                reason,
            )
            return FanCommandResult.RATE_LIMITED_DUP

        elapsed = (dt_util.now() - self._fan_toggle_command_time).total_seconds()
        _LOGGER.info(
            "Fan toggle deferred: rate limit (%.0fs since last change, min %ds) — %s (%s) — applies at %s",
            elapsed,
            FAN_MIN_TOGGLE_INTERVAL_S,
            action,
            reason,
            applies_at.strftime("%H:%M:%S"),
        )
        self._fan_rate_limited_until = applies_at
        self._fan_rate_limited_direction = action
        return FanCommandResult.RATE_LIMITED_NEW

    async def _activate_fan(self, *, reason: str, emit_event: bool = True) -> FanCommandResult:
        """Activate fan based on configured fan_mode.

        Args:
            reason: Human-readable trigger source (logged + surfaced in the Activity Report).
            emit_event: When True (default), emit a ``fan_activated`` event to the event log
                so the Activity Report shows every CA fan command with its source. Callers
                that already emit a more specific event for the same transition (the nat-vent
                cycler / exit paths) pass False to avoid a duplicate row (Issue #331 follow-up).

        Returns:
            A ``FanCommandResult`` describing what actually happened (Issue #649) — callers
            that build their own Activity Report event for this same transition use this to
            avoid reporting a state change that didn't happen (rate-limited) or reporting the
            same deferral twice (a duplicate block within the same window).
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return FanCommandResult.DISABLED

        if self._fan_override_active:
            _LOGGER.info("Fan override active — skipping fan activation")
            return FanCommandResult.OVERRIDDEN

        # Issue #714: a manual override to an active HVAC mode (heat/cool/heat_cool)
        # structurally conflicts with WHF — starting the fan would call
        # _suppress_hvac_for_whf() below and force the thermostat straight back to "off",
        # silently reverting the user's manual choice with no override check at all (the
        # entry-side half of the #705 bug; the exit-side half is handled by the new
        # MANUAL_OVERRIDE_CONFLICT checks in nat_vent_exit.py/fan_thermostat_decision.py).
        # Mirrors the _fan_override_active guard immediately above.
        if self._manual_override_active and self._manual_override_mode not in (None, "off"):
            _LOGGER.info(
                "Manual override active (mode=%s) — skipping fan activation (%s)",
                self._manual_override_mode,
                reason,
            )
            return FanCommandResult.OVERRIDDEN

        # Issue #392 Fix 1c: idempotency guard — collapse redundant re-decisions from
        # multiple gate sites into a single real state transition.
        if self._fan_active:
            _LOGGER.debug("_activate_fan: already active — no-op (%s)", reason)
            return FanCommandResult.ALREADY_IN_STATE

        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
        from .fan_fsm import FanFsmEventKind

        _rl_from_state = self.fan_lifecycle_state

        _rl_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.ACTIVATE_REQUESTED,
            origin_state=_rl_from_state,
        )
        # Issue #731 Phase 7 fix: the FSM's own computed rate_limit_applies_at
        # (fan_fsm.py's _transition_on_activate_requested() derives it) must be
        # written back here — mirrors the legacy _fan_toggle_rate_limited()'s own
        # DEFER_NEW-only write (ALLOW/DEFER_DUPLICATE leave these fields untouched).
        if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW:
            self._fan_rate_limited_until = _rl_transition.rate_limit_applies_at
            self._fan_rate_limited_direction = "activate"
        # Issue #757 Step 2 correction: the deleted legacy closure's own
        # _fan_toggle_rate_limited() carried the ONLY logging for this guard — the
        # FSM-authoritative branch never had its own, so this INFO/DEBUG pair (an
        # Observability Requirements-mandated decision-outcome log, per CLAUDE.md)
        # had been silently missing from real production since Phase Fan/WHF (#731)
        # went authoritative, not something this step introduced. Restored here,
        # same text/level split as the deleted method.
        if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_DUPLICATE:
            _LOGGER.debug(
                "Fan toggle already deferred until %s — skipping duplicate report (%s)",
                _rl_transition.rate_limit_applies_at.strftime("%H:%M:%S"),
                reason,
            )
        elif _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW:
            _elapsed = (dt_util.now() - self._fan_toggle_command_time).total_seconds()
            _LOGGER.info(
                "Fan toggle deferred: rate limit (%.0fs since last change, min %ds) — %s (%s) — applies at %s",
                _elapsed,
                FAN_MIN_TOGGLE_INTERVAL_S,
                "activate",
                reason,
                _rl_transition.rate_limit_applies_at.strftime("%H:%M:%S"),
            )
        if _rl_transition.rate_limit_outcome is not FanToggleRateLimitOutcome.ALLOW:
            return (
                FanCommandResult.RATE_LIMITED_NEW
                if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW
                else FanCommandResult.RATE_LIMITED_DUP
            )

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would activate fan — %s role=%s", reason, self.role)
            return FanCommandResult.EXECUTED

        _was_deferred = (
            isinstance(self._fan_rate_limited_until, datetime) and self._fan_rate_limited_direction == "activate"
        )
        if _was_deferred:
            _LOGGER.info(
                "5-minute floor expired — applying deferred activation (original reason: %s)",
                reason,
            )
            self._fan_rate_limited_until = None
            self._fan_rate_limited_direction = None

        self._fan_command_time = dt_util.now()
        self._fan_toggle_command_time = self._fan_command_time
        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                # Whole-house fan exchanges outdoor air directly — running AC/heat
                # simultaneously fights the fan and wastes energy (Issue #277 Fix C).
                await self._suppress_hvac_for_whf(
                    reason="whole-house fan active — suppressing HVAC to prevent fighting outdoor air exchange"
                )

                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]  # "fan" or "switch"
                    _commanded = await self._command_whf_control_entity(True, reason=reason)
                    if _commanded:
                        _LOGGER.info("Activated %s fan (%s) — %s role=%s", domain, fan_entity, reason, self.role)

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
                _LOGGER.info("Activated HVAC fan — %s role=%s", reason, self.role)

            # Issue #731 Phase 5: deliberately NOT routed through _resolve_fan_fsm_state().
            # This is the raw hardware-activation write itself — the FSM's physical axis
            # (fan_lifecycle.py's _derive_physical()) is DERIVED FROM _fan_active, so
            # deciding what to write here based on a state that itself reads what's about
            # to be written would be circular. The rate-limit decision immediately above
            # (which genuinely IS FSM-dispatched) already gated whether we reach this line
            # at all; by the time we're here the real command has already been issued and
            # this write simply records ground truth. Stays a direct write in both
            # _activate_fan() and _deactivate_fan(), same reasoning both places.
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
                # Architecture-reset Step 2: the decision now lives in
                # setpoint_verify_decision.decide_setpoint_verify() — shared with
                # _do_verify_after_fan_off(), which had byte-for-byte identical logic
                # before this consolidation (found during the #429 dedup sweep).
                current_state = self.hass.states.get(self.climate_entity)
                actual_temp: float | None = None
                if current_state is not None:
                    _actual_raw = current_state.attributes.get("temperature")
                    if _actual_raw is not None:
                        try:
                            actual_temp = float(_actual_raw)
                        except (ValueError, TypeError):
                            actual_temp = None
                outcome = decide_setpoint_verify(
                    current_write_seq=self._write_seq,
                    verify_write_seq=_verify_seq,
                    expected_temp=_expected_temp,
                    expected_mode=_expected_mode,
                    manual_override_active=self._manual_override_active,
                    actual_temp=actual_temp,
                )
                if outcome is SetpointVerifyOutcome.REASSERT:
                    _LOGGER.info(
                        "Post-fan setpoint verify: thermostat %.1f°F != expected %.1f°F — re-asserting %s mode",
                        actual_temp,
                        _expected_temp,
                        _expected_mode,
                    )
                    await self._set_temperature(_expected_temp, reason="post-fan-verify/repair", mode=_expected_mode)

            @callback
            def _verify_setpoint_after_fan_on(_now: Any) -> None:
                self.hass.async_create_task(_do_verify_after_fan_on())

            self._fan_on_verify_cancel = async_call_later(self.hass, 30.0, _verify_setpoint_after_fan_on)

            # Issue #327: thermostatic backstop timer — fires every 5 min while the fan
            # is CA-owned; calls fan_thermostat_check so a slow-updating outdoor sensor
            # cannot leave the fan running indefinitely between state-listener events.
            self._start_fan_thermo_backstop()
        finally:
            self._fan_command_pending = False
        return FanCommandResult.EXECUTED

    def _start_fan_thermo_backstop(self) -> None:
        """Start (or restart) the 5-minute thermostatic backstop timer (Issue #327).

        The timer is self-rescheduling: each fire re-schedules the next tick before
        calling fan_thermostat_check, so it runs continuously while the fan is active.
        Cancelled by _deactivate_fan and cleanup.
        """
        if self._fan_thermo_cancel:
            self._fan_thermo_cancel()
            self._fan_thermo_cancel = None

        # Issue #561: stamp this chain with the current generation before scheduling —
        # see self._fan_thermo_generation's declaration for why.
        self._fan_thermo_generation += 1
        _my_generation = self._fan_thermo_generation

        @callback
        def _thermo_tick(_now: Any) -> None:
            self._fan_thermo_cancel = None
            if _my_generation != self._fan_thermo_generation:
                _LOGGER.debug(
                    "Fan thermo backstop tick (generation %d) superseded by generation %d —"
                    " a newer chain already started; this stale chain self-terminates",
                    _my_generation,
                    self._fan_thermo_generation,
                )
                return
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
            await self.nat_vent_temperature_check(indoor, outdoor=outdoor)
        # Re-arm only if the fan is still active after the check. Architecture-reset
        # Step 2: the decision now lives in desired_state.decide_fan_thermo_backstop() —
        # this method still owns actually calling _start_fan_thermo_backstop() to
        # schedule the real timer.
        #
        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state(). Per fan_fsm.py's
        # own _transition_on_thermo_backstop_tick() docstring, this kind never changes
        # to_state (the timer-arm decision is not one of the 5 composed axes); the
        # real _start_fan_thermo_backstop() call stays in the shell below, reading
        # thermo_backstop_should_be_armed off the returned transition.
        from .fan_fsm import FanFsmEventKind

        _backstop_origin_state = self.fan_lifecycle_state

        _backstop_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.THERMO_BACKSTOP_TICK,
            origin_state=_backstop_origin_state,
        )
        if _backstop_transition.thermo_backstop_should_be_armed:
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

        Architecture-reset Step 2 (session state machine slice): the decision itself now
        lives in `fan_drift_reconciliation.decide_fan_drift_reconciliation()` — this method
        only reconstructs the pure inputs and applies the returned outcome's side effects.
        """
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        recent_fan_command = bool(
            self._is_recent_fan_command_callback and self._is_recent_fan_command_callback(threshold_seconds=30.0)
        )
        physical_state_available = bool(self._get_fan_physical_state_callback)
        # Mirrors the original code's laziness: only actually read live physical state
        # once every cheaper guard (fan active, applicable archetype, no recent CA
        # command echo) has already passed — avoids an unnecessary state read otherwise.
        physical_on = None
        if (
            self._fan_active
            and fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH)
            and not recent_fan_command
            and physical_state_available
        ):
            physical_on = self._get_fan_physical_state_callback()

        # Issue #446 instrumentation: log the raw inputs on EVERY tick, not just on confirmed
        # drift, so a future recurrence has real evidence instead of inference. `physical_on`
        # already reflects whatever entity the coordinator treats as ground truth (fan_state_entity
        # when configured, else the command entity) — logged alongside the command entity's own
        # reported state for direct comparison.
        _command_state = self.hass.states.get(self.config.get(CONF_FAN_ENTITY, ""))
        _LOGGER.debug(
            "Fan drift tick: fan_active=%s fan_mode=%s recent_command=%s physical_available=%s"
            " physical_on=%s command_entity_state=%s tick_count=%d",
            self._fan_active,
            fan_mode,
            recent_fan_command,
            physical_state_available,
            physical_on,
            _command_state.state if _command_state else "unavailable",
            self._fan_drift_tick_count,
        )

        # Issue #757 Step 2 correction: the deleted legacy closure that used to gate
        # this block contained real, non-mirror-sync side effects beyond the FSM's
        # own flag-composition — decide_fan_drift_reconciliation() itself,
        # _fan_drift_tick_count bookkeeping, _emit_event_callback("fan_cancel", ...),
        # _clear_fan_flags_and_start_grace() (grace-period start),
        # _release_whf_and_reclassify() (WHF suppression release + reclassify), and
        # scheduling the real hardware off-command (_command_whf_control_entity via
        # _do_drift_reconciliation_off_command()) on FanDriftOutcome.CORRECT.
        # fan_fsm.py's _transition_on_drift_tick() only composes the fan_active flag
        # for this outcome — none of those other side effects have an FSM-branch
        # equivalent. tests/test_fan_control.py's TestReconcileFanPhysicalDrift/
        # TestReconcileFanDriftIntegrationLoadBearing/TestCommandWhfControlEntityWiring/
        # TestReconcileFanDriftBookkeeping suites confirmed this is live, expected
        # behavior (not dead code) — deleting it broke 10 tests. Restored here as an
        # unconditional shell, matching the Group-1 "real write first, dispatch
        # purely for mirror-sync" pattern already used by the WHF_SUPPRESSION_
        # REQUESTED/WHF_RELEASE_REQUESTED call sites.
        from .fan_fsm import FanFsmEventKind

        _drift_origin_state = self.fan_lifecycle_state

        inputs = FanDriftInputs(
            fan_active=self._fan_active,
            fan_mode=fan_mode,
            recent_fan_command=recent_fan_command,
            physical_state_available=physical_state_available,
            physical_on=physical_on,
            tick_count=self._fan_drift_tick_count,
        )
        outcome, next_tick_count = decide_fan_drift_reconciliation(inputs)
        self._fan_drift_tick_count = next_tick_count

        if outcome is FanDriftOutcome.AWAITING:
            _LOGGER.info(
                "Fan physical-state drift detected (tick %d/2): _fan_active=True but physical"
                " state=off — awaiting confirmation tick before correcting",
                next_tick_count,
            )
        elif outcome is FanDriftOutcome.CORRECT:
            _LOGGER.warning(
                "Fan physical-state drift confirmed over 2 backstop ticks: _fan_active=True but"
                " physical state=off — self-correcting stale flag (Issue #423)"
            )
            if self._emit_event_callback:
                self._emit_event_callback(
                    "fan_cancel",
                    {
                        "trigger": "physical_drift_correction",
                        "reason": "physical-state drift confirmed over 2 backstop ticks",
                        "fan_device": _fan_device_label(self.config),
                    },
                )
            # Issue #561: preserving the nat-vent session across this correction (so
            # nat_vent_temperature_check() can immediately re-fire below) is only correct if
            # the session is still legitimately justified by an open sensor. Without this
            # check, a session whose windows already closed while the physical-drift-confirm
            # was pending (2 backstop ticks = ~10 min) would be preserved indefinitely — with
            # no further log line — until temperature happened to cross a cycling threshold
            # again, at which point the fan would reactivate against a sealed house. If the
            # session is no longer justified, end it for real (preserve=False) and release any
            # WHF HVAC suppression it was holding, mirroring on_fan_turned_off()'s genuine
            # fan-off sequence.
            _was_nat_vent_active = self._natural_vent_active
            _preserve_session = _was_nat_vent_active and self._any_monitored_sensor_open()
            if _was_nat_vent_active and not _preserve_session:
                _LOGGER.warning(
                    "Nat-vent session force-closed during physical-drift correction — no"
                    " monitored sensor is open, so the session cannot legitimately still be"
                    " open (Issue #561)"
                )
            self._clear_fan_flags_and_start_grace(
                reason="physical-state drift confirmed over 2 backstop ticks",
                trigger_label="physical_drift_correction",
                preserve_nat_vent_session=_preserve_session,
                source="automation",
            )
            if _was_nat_vent_active and not _preserve_session:
                self._release_whf_and_reclassify(
                    reason="physical-drift correction found the nat-vent session's sensors closed"
                )
            # Issue #449: the control entity's own HA-reported state can silently stay
            # stuck "on" (a one-way transmitter has no feedback of its own) even though
            # ground truth has just confirmed the fan is physically off — reconcile it now
            # so the very next reactivation attempt (nat_vent_temperature_check()'s
            # immediate same-tick re-fire, when the session was preserved above)
            # starts from a control entity that genuinely reads "off".
            #
            # Issue #482: this off-command must set the same _fan_command_pending/
            # _fan_command_time bookkeeping every other command site sets, so
            # _async_fan_entity_changed() (coordinator.py) can suppress the resulting
            # state-change event as CA-initiated instead of misclassifying it as manual
            # (which would start a spurious grace period). The bookkeeping is stamped
            # HERE, synchronously, before the task is scheduled — not inside the task body
            # — so there is no window where the entity-changed listener could observe a
            # stale/unset _fan_command_pending before the task actually runs.
            self._fan_command_time = dt_util.now()
            self._fan_command_pending = True

            async def _do_drift_reconciliation_off_command() -> None:
                try:
                    await self._command_whf_control_entity(
                        False, reason="physical-state drift confirmed over 2 backstop ticks"
                    )
                finally:
                    self._fan_command_pending = False

            self.hass.async_create_task(_do_drift_reconciliation_off_command())

        # Real writes above already own _fan_active for the CORRECT outcome (via
        # _clear_fan_flags_and_start_grace()) and leave it untouched otherwise; this
        # dispatch is purely the mirror-sync/audit-trail step, matching every other
        # Group-1 call site — fan_fsm.py's own to_state projection for DRIFT_TICK
        # (fan_fsm.py's _transition_on_drift_tick()) reads self._fan_active fresh via
        # _build_fan_fsm_inputs() at dispatch time and round-trips it unchanged for
        # every outcome except CORRECT, where it independently derives the same
        # False the real write above already applied — so no restore is needed.
        self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.DRIFT_TICK,
            origin_state=_drift_origin_state,
            fan_drift_tick_count=inputs.tick_count,
        )

    async def _deactivate_fan(
        self,
        *,
        reason: str,
        restore_hvac: bool = True,
        release_suppression: bool | None = None,
        emit_event: bool = True,
        bypass_absolute_override: bool = False,
    ) -> FanCommandResult:
        """Deactivate fan based on configured fan_mode.

        Args:
            reason: Human-readable reason for deactivation (logged + surfaced in the report).
            restore_hvac: When True (default), restores the HVAC mode that was suppressed
                when the whole-house fan activated. Pass False during nat-vent cycling
                (Bug 3 / Issue #321) so the session can continue without re-engaging HVAC
                between cycles — the fan turns off temporarily, but HVAC stays suppressed.
            release_suppression: Whether this call ends WHF ownership of the thermostat
                (clears ``_pre_fan_hvac_mode``) independent of whether it also *writes* a
                restored mode right now. Defaults to ``restore_hvac`` when not given, matching
                historical behavior for existing callers. Genuine session-end callers
                (``_exit_nat_vent()``'s sensor-open branch, the reconcile "no-fan" branch) pass
                ``True`` explicitly even when ``restore_hvac=False`` (don't write into an open
                window, but don't strand the snapshot either) — see Issue #618. Mid-session
                cycling-off (``nat_vent_temperature_check()``) leaves this at its
                ``restore_hvac``-tracking default (``False``) since that stranding is
                intentional — the session is still ongoing and expected to resume.
            emit_event: When True (default), emit a ``fan_deactivated`` event so the Activity
                Report shows every CA fan-off with its source. Callers that already emit a
                more specific event for the same transition (nat-vent cycler / exit paths)
                pass False to avoid a duplicate row (Issue #331 follow-up).
            bypass_absolute_override: Issue #748 — when True, skip the Issue #486
                RF-remote-timer-absolute guard below entirely. Reserved for the single hard
                AC/WHF mutex-enforcement path (``_stand_down_whf_for_override_conflict()``):
                a manual HVAC-mode override physically conflicts with an active WHF session
                right now, which is a hard invariant, not an automation preference Issue #486
                was designed to protect against. Every other caller must leave this False so
                Issue #486's "an RF remote timer is absolute against routine automation
                second-guessing" behavior is unchanged.

        Returns:
            A ``FanCommandResult`` describing what actually happened (Issue #649) — see
            ``_activate_fan()``'s matching docstring note.
        """
        if release_suppression is None:
            release_suppression = restore_hvac
        fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
        if fan_mode == FAN_MODE_DISABLED:
            return FanCommandResult.DISABLED

        if self._fan_override_active and not bypass_absolute_override:
            if self._fan_remote_timer_hours is not None:
                # Issue #486: the override is absolute while an RF remote timer is active —
                # this shutoff (nat-vent exit, comfort-floor breach, cycle-off, etc.) is
                # suppressed and logged (not silently dropped) so the "log-only" absolute-timer
                # behavior is observable. Issue #585: INFO, not WARNING — the mutex/timer logic
                # is working correctly here, not malfunctioning.
                _LOGGER.info(
                    "Fan deactivation suppressed by active RF remote timer (%sh) — reason: %s",
                    self._fan_remote_timer_hours,
                    reason,
                )
            else:
                _LOGGER.info("Fan override active — skipping fan deactivation")
            return FanCommandResult.OVERRIDDEN

        if self._fan_override_active and bypass_absolute_override and self._fan_remote_timer_hours is not None:
            # Issue #748: the hard AC/WHF mutex overrides even an active RF remote timer —
            # last setting placed wins. Logged (not silently proceeding) so the
            # remote-timer-overridden outcome stays observable, matching Issue #486's own
            # observability bar for the opposite (suppressed) outcome above. Issue #585:
            # INFO, not WARNING — the mutex is working correctly, not malfunctioning.
            _LOGGER.info(
                "Fan deactivation forced despite active RF remote timer (%sh) — hard AC/WHF"
                " mutex overrides remote-timer protection — reason: %s",
                self._fan_remote_timer_hours,
                reason,
            )

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
        #
        # Issue #618: a genuine session-end (release_suppression=True) must clear
        # _pre_fan_hvac_mode even when restore_hvac=False (don't write HVAC into an open
        # window, but don't strand the snapshot either — a stranded snapshot kept
        # _whf_owns_hvac() reporting True for hours after the session actually ended,
        # blocking apply_classification()'s DEFER_NAT_VENT gate from ever clearing).
        if not self._fan_active:
            if release_suppression and self._pre_fan_hvac_mode is not None:
                _restore_mode = self._pre_fan_hvac_mode

                # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
                #
                # Issue #731 Phase 7 fix: see _suppress_hvac_for_whf()'s matching
                # comment — WHF_RELEASE_REQUESTED is a Group-1 "caller-already-decided"
                # kind, so the real _pre_fan_hvac_mode write (and the before/after diff
                # it feeds) is hoisted above the dispatch call.
                from .fan_fsm import FanFsmEventKind

                _whf_origin_state = self.fan_lifecycle_state
                _whf_before = self._whf_owns_hvac()
                self._pre_fan_hvac_mode = None

                self._resolve_fan_fsm_state(
                    kind=FanFsmEventKind.WHF_RELEASE_REQUESTED,
                    origin_state=_whf_origin_state,
                )
                self._emit_boolean_transition(
                    before=_whf_before,
                    after=self._whf_owns_hvac(),
                    started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
                    ended=LifecycleEventType.WHF_HVAC_RELEASED,
                    detail=None,
                    caller="_deactivate_fan",
                )
                if restore_hvac:
                    _LOGGER.debug(
                        "_deactivate_fan: fan already inactive but restoring stranded HVAC suppression (%s)", reason
                    )
                    await self._set_hvac_mode(
                        _restore_mode,
                        reason=f"whole-house fan already stopped — restoring HVAC mode ({_restore_mode})",
                    )
                    # Issue #618: this is the "physical fan was already off, but a snapshot
                    # from an earlier session was still stranded" path — the exact case that
                    # was completely invisible in the dashboard Activity Record in the
                    # 2026-08-10 incident (only a DEBUG log line, no event, no record). The
                    # normal "fan was actively running and gets deactivated now" path a few
                    # lines below already emits fan_deactivated when emit_event=True; this is
                    # the one sibling branch that historically emitted nothing at all, so it's
                    # the only new event added anywhere in this function.
                    self._record_action(
                        "Restored HVAC mode from stranded fan suppression", f"{reason} (restored={_restore_mode})"
                    )
                    if self._emit_event_callback:
                        self._emit_event_callback(
                            "stranded_hvac_suppression_restored",
                            {"reason": reason, "restore_mode": _restore_mode},
                        )
                else:
                    _LOGGER.debug(
                        "_deactivate_fan: fan already inactive — releasing stranded HVAC suppression"
                        " without restoring mode (%s, prior_mode=%s)",
                        reason,
                        _restore_mode,
                    )
            else:
                _LOGGER.debug("_deactivate_fan: already inactive — no-op (%s)", reason)
            # Issue #733: a backstop timer should never outlive _fan_active reading False —
            # if something cleared _fan_active without going through the full deactivation
            # path below (e.g. a reconcile branch's direct write), the self-rescheduling
            # thermostatic backstop armed by the earlier _activate_fan() call is left
            # running but orphaned, pointing at flags that now say nothing is active. Its
            # one tick then no-ops (nat_vent_temperature_check exits immediately) and never
            # reschedules again, silently ending thermostatic oversight for the rest of the
            # session. Safe no-op when no timer is scheduled.
            self._cancel_fan_thermo_backstop()
            return FanCommandResult.ALREADY_IN_STATE

        # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
        from .fan_fsm import FanFsmEventKind

        _rl_from_state = self.fan_lifecycle_state

        _rl_transition = self._resolve_fan_fsm_state(
            kind=FanFsmEventKind.DEACTIVATE_REQUESTED,
            origin_state=_rl_from_state,
        )
        # Issue #731 Phase 7 fix: see _activate_fan()'s matching comment — the FSM's
        # own computed rate_limit_applies_at must be written back here, mirroring the
        # legacy _fan_toggle_rate_limited()'s own DEFER_NEW-only side effect on
        # _fan_rate_limited_until/_direction.
        if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW:
            self._fan_rate_limited_until = _rl_transition.rate_limit_applies_at
            self._fan_rate_limited_direction = "deactivate"
        # Issue #757 Step 2 correction: see _activate_fan()'s matching comment — this
        # log had been silently missing from real production since Phase Fan/WHF
        # (#731) went authoritative, not something this step introduced.
        if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_DUPLICATE:
            _LOGGER.debug(
                "Fan toggle already deferred until %s — skipping duplicate report (%s)",
                _rl_transition.rate_limit_applies_at.strftime("%H:%M:%S"),
                reason,
            )
        elif _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW:
            _elapsed = (dt_util.now() - self._fan_toggle_command_time).total_seconds()
            _LOGGER.info(
                "Fan toggle deferred: rate limit (%.0fs since last change, min %ds) — %s (%s) — applies at %s",
                _elapsed,
                FAN_MIN_TOGGLE_INTERVAL_S,
                "deactivate",
                reason,
                _rl_transition.rate_limit_applies_at.strftime("%H:%M:%S"),
            )
        if _rl_transition.rate_limit_outcome is not FanToggleRateLimitOutcome.ALLOW:
            return (
                FanCommandResult.RATE_LIMITED_NEW
                if _rl_transition.rate_limit_outcome is FanToggleRateLimitOutcome.DEFER_NEW
                else FanCommandResult.RATE_LIMITED_DUP
            )

        if self.dry_run:
            _LOGGER.info("[DRY RUN] Would deactivate fan — %s role=%s", reason, self.role)
            return FanCommandResult.EXECUTED

        _was_deferred = (
            isinstance(self._fan_rate_limited_until, datetime) and self._fan_rate_limited_direction == "deactivate"
        )
        if _was_deferred:
            _LOGGER.info(
                "5-minute floor expired — applying deferred exit (original reason: %s)",
                reason,
            )
            self._fan_rate_limited_until = None
            self._fan_rate_limited_direction = None

        self._fan_command_time = dt_util.now()
        self._fan_toggle_command_time = self._fan_command_time
        self._fan_command_pending = True
        try:
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                fan_entity = self.config.get(CONF_FAN_ENTITY)
                if fan_entity:
                    domain = fan_entity.split(".")[0]
                    _commanded = await self._command_whf_control_entity(False, reason=reason)
                    if _commanded:
                        _LOGGER.info("Deactivated %s fan (%s) — %s role=%s", domain, fan_entity, reason, self.role)

                # Restore prior HVAC mode that was suppressed when the fan activated
                # (Issue #277 Fix C). Only restore if we have a stored mode to go back to.
                # Skipped during nat-vent cycling (restore_hvac=False) so HVAC stays
                # suppressed between fan-on and fan-off cycles within the same session.
                #
                # Issue #618: release_suppression (independent of restore_hvac) still clears
                # _pre_fan_hvac_mode at a genuine session end even when the caller doesn't want
                # a mode written right now (e.g. a window is still open) — otherwise the stale
                # snapshot strands _whf_owns_hvac()==True indefinitely, the same bug fixed in
                # the already-inactive branch above.
                if release_suppression and self._pre_fan_hvac_mode is not None:
                    _restore_mode = self._pre_fan_hvac_mode

                    # Issue #392: clear _pre_fan_hvac_mode BEFORE issuing the restore write, not
                    # after. _whf_owns_hvac() (the Fix 1b choke-point guard in _set_hvac_mode())
                    # treats "_pre_fan_hvac_mode is not None" as "WHF still owns the thermostat" —
                    # the restore write itself ends the suppression session, so ownership must be
                    # released before the write, or the guard self-blocks the very call that is
                    # supposed to un-suppress HVAC.
                    # Issue #731 Phase 5: routed through _resolve_fan_fsm_state().
                    #
                    # Issue #731 Phase 7 fix: see _suppress_hvac_for_whf()'s matching
                    # comment — WHF_RELEASE_REQUESTED is a Group-1 "caller-already-
                    # decided" kind, so the real _pre_fan_hvac_mode write (and the
                    # before/after diff it feeds) is hoisted above the dispatch call.
                    # FanFsmEventKind already imported earlier in this method (see the
                    # rate-limit section above).
                    _whf_origin_state_release = self.fan_lifecycle_state
                    _whf_before_release = self._whf_owns_hvac()
                    self._pre_fan_hvac_mode = None

                    self._resolve_fan_fsm_state(
                        kind=FanFsmEventKind.WHF_RELEASE_REQUESTED,
                        origin_state=_whf_origin_state_release,
                    )
                    self._emit_boolean_transition(
                        before=_whf_before_release,
                        after=self._whf_owns_hvac(),
                        started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
                        ended=LifecycleEventType.WHF_HVAC_RELEASED,
                        detail=None,
                        caller="_deactivate_fan",
                    )
                    if restore_hvac:
                        await self._set_hvac_mode(
                            _restore_mode,
                            reason=f"whole-house fan stopped — restoring HVAC mode ({_restore_mode})",
                        )
                    else:
                        _LOGGER.debug(
                            "_deactivate_fan: releasing stranded HVAC suppression without"
                            " restoring mode (%s, prior_mode=%s)",
                            reason,
                            _restore_mode,
                        )

            if fan_mode in (FAN_MODE_HVAC, FAN_MODE_BOTH):
                await self.hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": "auto"},
                )
                _LOGGER.info("Deactivated HVAC fan — %s role=%s", reason, self.role)

            # Issue #731 Phase 5: deliberately NOT routed through _resolve_fan_fsm_state() —
            # see _activate_fan()'s matching comment (raw hardware-deactivation write; the
            # FSM's physical axis is derived FROM this flag, so wiring it here would be
            # circular).
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
                # Architecture-reset Step 2: the decision now lives in
                # setpoint_verify_decision.decide_setpoint_verify() — shared with
                # _do_verify_after_fan_on(), which had byte-for-byte identical logic
                # before this consolidation (found during the #429 dedup sweep).
                current_state = self.hass.states.get(self.climate_entity)
                actual_temp: float | None = None
                if current_state is not None:
                    _actual_raw = current_state.attributes.get("temperature")
                    if _actual_raw is not None:
                        try:
                            actual_temp = float(_actual_raw)
                        except (ValueError, TypeError):
                            actual_temp = None
                outcome = decide_setpoint_verify(
                    current_write_seq=self._write_seq,
                    verify_write_seq=_verify_seq,
                    expected_temp=_expected_temp,
                    expected_mode=_expected_mode,
                    manual_override_active=self._manual_override_active,
                    actual_temp=actual_temp,
                )
                if outcome is SetpointVerifyOutcome.REASSERT:
                    _LOGGER.info(
                        "Post-fan setpoint verify: thermostat %.1f°F != expected %.1f°F — re-asserting %s mode",
                        actual_temp,
                        _expected_temp,
                        _expected_mode,
                    )
                    await self._set_temperature(_expected_temp, reason="post-fan-verify/repair", mode=_expected_mode)

            @callback
            def _verify_setpoint_after_fan_off(_now: Any) -> None:
                self.hass.async_create_task(_do_verify_after_fan_off())

            self._fan_off_verify_cancel = async_call_later(self.hass, 30.0, _verify_setpoint_after_fan_off)
        finally:
            self._fan_command_pending = False
        return FanCommandResult.EXECUTED

    @property
    def economizer_lifecycle_state(self) -> EconomizerLifecycleState:
        """Current economizer session state, derived from existing flags (Issue #746).

        Purely a computed view of ``_economizer_active``/``_economizer_phase``, so it
        cannot desync from the flags it reads. See ``economizer_lifecycle.py`` for the
        pure derivation.
        """
        return derive_economizer_lifecycle_state(
            EconomizerLifecycleInputs(
                economizer_active=self._economizer_active,
                economizer_phase=self._economizer_phase,
            )
        )

    def _build_economizer_fsm_inputs(
        self,
        *,
        outdoor_temp: float,
        indoor_temp: float | None,
        windows_physically_open: bool,
        current_hour: int,
    ) -> EconomizerFsmInputs:
        """Build the FSM's input snapshot from current engine state + this call's
        parameters (Issue #746). Mirrors ``_build_nat_vent_fsm_inputs()``'s role."""
        from .economizer_fsm import EconomizerFsmInputs

        c = self._current_classification
        day_type = c.day_type if c else None

        if current_hour < 0:
            in_window = True
        else:
            in_window = (ECONOMIZER_MORNING_START_HOUR <= current_hour < ECONOMIZER_MORNING_END_HOUR) or (
                ECONOMIZER_EVENING_START_HOUR <= current_hour < ECONOMIZER_EVENING_END_HOUR
            )

        return EconomizerFsmInputs(
            day_type=day_type,
            natural_vent_active=bool(self._natural_vent_active),
            outdoor=outdoor_temp,
            indoor=indoor_temp,
            comfort_cool=float(self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)),
            delta=float(self.config.get("economizer_temp_delta", ECONOMIZER_TEMP_DELTA)),
            windows_physically_open=windows_physically_open,
            in_window=in_window,
            aggressive_savings=bool(self.config.get("aggressive_savings", False)),
            now=dt_util.now(),
        )

    def _apply_economizer_fsm_state(self, state: EconomizerLifecycleState) -> None:
        """Write ``_economizer_active``/``_economizer_phase`` from an
        ``economizer_fsm.transition()`` result (Issue #746).

        Inverse of ``economizer_lifecycle_state``'s derivation — see
        ``economizer_lifecycle.py``'s ``derive_economizer_lifecycle_state()``. Since
        the two underlying flags are always set in lockstep in production (see that
        module's docstring), this is a straight, unconditional write — matches legacy
        setting both fields together on every real write site.
        """
        self._economizer_active = state != EconomizerLifecycleState.INACTIVE
        self._economizer_phase = state.value

    async def _check_window_cooling_opportunity_fsm(
        self,
        outdoor_temp: float,
        indoor_temp: float | None,
        windows_physically_open: bool,
        current_hour: int,
    ) -> bool:
        """Sole implementation of ``check_window_cooling_opportunity()`` (Issue #757,
        strangler-fig graduation Phase 6 Step 1 — the legacy two-phase branch was
        removed once ``_economizer_fsm_authoritative`` had been permanently True in
        production for weeks with zero corpus divergence, per Issue #746/Phase 5).

        Computes the decision via ``economizer_fsm.transition()``, then applies
        the side effects (fan activate/deactivate, HVAC resume, logging).
        """
        from .economizer_fsm import EconomizerFsmEvent, EconomizerFsmEventKind
        from .economizer_fsm import transition as _economizer_fsm_transition

        unit = self.config.get("temp_unit", "fahrenheit")
        comfort_cool = self.config.get("comfort_cool", DEFAULT_COMFORT_COOL)
        aggressive_savings = self.config.get("aggressive_savings", False)

        inputs = self._build_economizer_fsm_inputs(
            outdoor_temp=outdoor_temp,
            indoor_temp=indoor_temp,
            windows_physically_open=windows_physically_open,
            current_hour=current_hour,
        )
        current_state = self.economizer_lifecycle_state
        event = EconomizerFsmEvent(kind=EconomizerFsmEventKind.TICK, inputs=inputs)
        result: EconomizerTransition = _economizer_fsm_transition(current_state, event)

        # Same direction-rejected debug log production emits before its eligibility
        # check — only ever populated once the gate has actually been reached (not
        # on the not-hot-day short-circuit), same reachability as legacy.
        if result.direction_ok is False:
            _LOGGER.debug(
                "Economizer gate: direction rejected — outdoor %.1f°F >= indoor %.1f°F"
                " (free-cooling direction required)",
                outdoor_temp,
                indoor_temp if indoor_temp is not None else 0.0,
            )

        # Nat-vent-active short-circuit: return False without touching state at all —
        # the same asymmetry legacy's own `if self._natural_vent_active: return False`
        # branch preserves (deliberately NOT deactivating even if already active).
        if result.deferred:
            return False

        if result.to_state == EconomizerLifecycleState.INACTIVE:
            if result.changed:
                await self._deactivate_economizer(outdoor_temp)
            return False

        self._apply_economizer_fsm_state(result.to_state)

        if not result.changed:
            return True

        if result.to_state == EconomizerLifecycleState.MAINTAIN:
            await self._activate_fan(reason="economizer maintain — fan assists ventilation")
            if aggressive_savings:
                _LOGGER.info(
                    "Economizer (savings): ventilation only, outdoor=%s, band stays armed",
                    format_temp(outdoor_temp, unit),
                )
            else:
                _LOGGER.info(
                    "Economizer phase=maintain: indoor=%s, band armed, ventilation holding",
                    format_temp(indoor_temp if indoor_temp is not None else 0, unit),
                )
        else:  # COOL_DOWN
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

        Issue #757 (strangler-fig graduation Phase 6 Step 1): the legacy two-phase
        body that used to live here was removed once ``_economizer_fsm_authoritative``
        had been permanently True in production for weeks with zero corpus divergence
        (Issue #746/Phase 5). This now always delegates to the FSM-based
        ``_check_window_cooling_opportunity_fsm()`` — same logging, same events, same
        fan/HVAC writes.
        """
        return await self._check_window_cooling_opportunity_fsm(
            outdoor_temp, indoor_temp, windows_physically_open, current_hour
        )

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
        """Read indoor temperature in °F from the configured source.

        Delegates to the shared ``indoor_temp.resolve_indoor_temp_f()`` helper
        (Issue #796, Step 10) so this cannot drift out of sync with the
        coordinator's identically-shaped ``_get_indoor_temp()`` again — that
        drift previously meant this method's readings were never checked
        against the plausible-indoor-range guard the coordinator already had,
        and a non-numeric ``current_temperature`` on the climate_fallback path
        could raise uncaught instead of being treated as unavailable. Reads
        ``self.config``/``self.hass`` fresh on every call — no caching.
        """
        return resolve_indoor_temp_f(
            hass=self.hass,
            source=self.config.get("indoor_temp_source", TEMP_SOURCE_CLIMATE_FALLBACK),
            unit=self.config.get("temp_unit", "fahrenheit"),
            indoor_temp_entity=self.config.get("indoor_temp_entity"),
            climate_entity=self.climate_entity,
        )

    def _indoor_f_for_event(self) -> float | None:
        """Read current indoor temp from climate entity for event enrichment."""
        try:
            state = self.hass.states.get(self.climate_entity)
            if state is not None:
                return float(state.attributes["current_temperature"])
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
        return None

    def _recent_duplicate(
        self,
        key: str,
        signature: tuple[Any, ...],
        *,
        window_seconds: float | None = None,
    ) -> bool:
        """Shared decision-record dedup check (Issue #591 — generalizes Issue #444's pattern).

        Content-keyed: returns True (and does NOT update the record) only when the exact
        same ``signature`` was already the last one recorded for ``key`` — and, if
        ``window_seconds`` is given, only within that many seconds of the prior record. A
        signature change always updates the record and returns False, regardless of timing.
        ``window_seconds=None`` (the default) means permanent-until-changed: a repeated
        identical signature is suppressed no matter how much time has passed, which is the
        only shape immune to the recurring bug class this helper closes (Issue #96, #444,
        #584) — a function reachable from multiple independent trigger paths, where a fixed
        time window can be slipped past by an untraced trigger delay (confirmed in
        production: an 11-minute gap slipped past #444's original 10-minute window).

        This gates ONLY the caller's event/log emission — callers MUST perform any real
        HVAC/fan action unconditionally, before consulting this helper. See Issue #591/#590
        Finding C for the per-call-site audit confirming this holds for every current caller.

        getattr/dict-create-on-first-use: mirrors the pre-existing ``_last_comfort_band_*``
        defensive-read pattern — some tests construct ``AutomationEngine`` via
        ``object.__new__()`` and bypass ``__init__``.
        """
        now = dt_util.now()
        sigs: dict[str, tuple[Any, ...]] = getattr(self, "_dedup_signatures", None) or {}
        ats: dict[str, datetime] = getattr(self, "_dedup_timestamps", None) or {}
        self._dedup_signatures = sigs
        self._dedup_timestamps = ats

        last_signature = sigs.get(key)
        last_at = ats.get(key)
        if window_seconds is None:
            _within_window = True
        else:
            _within_window = (
                isinstance(last_at, datetime)
                and isinstance(now, datetime)
                and (now - last_at).total_seconds() < window_seconds
            )
        is_duplicate = last_signature == signature and _within_window
        sigs[key] = signature
        ats[key] = now
        return is_duplicate

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
        self._manual_override_source = None
        self._manual_override_time = None
        self._override_confirm_time = None
        self._override_confirm_mode = None
        self._override_confirm_source = None
        self._grace_end_time = None
        self._grace_duration_seconds = None
        self._last_resume_source = None
        self._last_grace_trigger = None

        # Issue #680: route the 3 FSM-modeled flags (_override_confirm_pending /
        # _grace_active / _grace_protects_override) through
        # _resolve_override_grace_fsm_state() instead of assigning them directly. That
        # dispatcher is now (Issue #757 Phase 6 Step 3) unconditionally the single writer
        # of these 3 flags across every real override/grace call site — a direct
        # assignment here would be a second, ungoverned writer, bypassing it. Closest
        # semantic match is GRACE_TIMER_EXPIRED (grace ending with nothing left to
        # protect) — same reasoning coordinator._check_orphaned_grace() already uses for
        # this event kind.
        # origin_state is passed explicitly (unlike most real call sites, which default to
        # a live read) because there is no real prior FSM-tracked transition to read here —
        # nothing survives a restart — so the origin modeled is the synthetic "nothing was
        # running" state restart itself guarantees, not whichever residual value these
        # fields happened to carry from __init__.
        #
        # transition((IDLE, NONE), GRACE_TIMER_EXPIRED) hits
        # _transition_from_no_grace()'s fallthrough ("unreachable_no_grace" — grace can't
        # expire with none active) and returns the origin state unchanged, i.e. (IDLE,
        # NONE) — which _apply_override_grace_fsm_state() writes back as
        # confirm_pending=False, grace_active=False, grace_protects_override=False. This
        # is the same clean-slate restart POLICY the legacy direct-assignment used to
        # produce — only the writer changed.
        from .override_grace_fsm import OverrideGraceFsmEventKind as _OGFEventKind

        self._resolve_override_grace_fsm_state(
            kind=_OGFEventKind.GRACE_TIMER_EXPIRED,
            origin_state=(OverrideConfirmState.IDLE, GraceState.NONE),
        )
        # Issue #327: fan override cleared on restart — no grace timer to reschedule.
        # reconcile_fan_on_startup() runs shortly after and decides adopt-on / turn-off.
        self._fan_override_active = False
        self._fan_override_time = None
        # Issue #486: an RF remote timer selection is part of the override it started —
        # not persisted/restored, same as the rest of the override/grace clean slate above.
        self._fan_remote_timer_hours = None
        # Issue #519: same clean-slate treatment — a comfort-only speed observation isn't
        # tied to an override at all, but it's still transient, ambient, restart-scoped
        # observability, not state worth preserving across a restart.
        self._fan_remote_speed = None
        _LOGGER.info(
            "Fan override: restart clean-slate — _fan_override_active and _fan_override_time "
            "cleared (Issue #327); reconcile will decide fan disposition"
        )
        _LOGGER.info(
            "Restored automation state: last_action=%s, fan_active=%s, fan_override=%s "
            "(override/grace/pause/fan-override state cleared — clean slate on restart per Issue #263/#327)",
            self._last_action_reason,
            self._fan_active,
            self._fan_override_active,
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
            # Issue #486: RF remote timer hours, for observability only — like the two
            # fields above, always cleared on restore() (clean-slate policy), not restored.
            "fan_remote_timer_hours": self._fan_remote_timer_hours,
            # Issue #519: RF remote speed, for observability only — same clean-slate policy.
            "fan_remote_speed": self._fan_remote_speed,
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
        # Issue #729: found during the reload-based-promotion redesign — these 3 were
        # scheduled via async_call_later() but never tracked/cancelled anywhere, so a
        # config-entry reload (or any other cleanup()) left them running against a
        # stale engine instance. _setpoint_retry_cancel's chain notably includes a
        # real _set_temperature() call up to 15 minutes out.
        if self._setpoint_retry_cancel:
            self._setpoint_retry_cancel()
            self._setpoint_retry_cancel = None
        if self._fan_on_verify_cancel:
            self._fan_on_verify_cancel()
            self._fan_on_verify_cancel = None
        if self._fan_off_verify_cancel:
            self._fan_off_verify_cancel()
            self._fan_off_verify_cancel = None
        for unsub in self._active_listeners:
            unsub()
        self._active_listeners.clear()
