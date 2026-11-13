"""Climate platform for CA Dev Thermostat Sim.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Reuses the real Climate Advisor ODE step function (_simulate_indoor_physics)
so this simulator can never drift from production thermal-model behavior
(DRY rule, CLAUDE.md). That function lives in
custom_components/climate_advisor/coordinator.py as a pure, module-level
function with no instance-state dependency, so it can be imported directly.
While the compressor runs, this simulator asks that step for full capacity
(an unbounded drive target, Issue #1387), and its own deadband state machine
decides when the compressor stops — like a real thermostat, whose equipment
heats or cools at full power until the thermostat cuts it.

NOTE ON HA VERSION: ClimateEntity/RestoreEntity's API shape used here
(hvac_modes, target_temperature, current_temperature, async_get_last_state,
async_write_ha_state) has been stable since well before hacs.json's pinned
minimum homeassistant version (2026.3.0), but this was NOT verified against
a locally-installed `homeassistant` package — none exists in this repo/venv.
Test on a real Home Assistant instance before relying on it.
"""

from __future__ import annotations

import contextlib
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import FAN_AUTO, FAN_ON
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BLOWER_LEAD_IN_COOL_MIN,
    CONF_BLOWER_LEAD_IN_HEAT_MIN,
    CONF_BLOWER_OVERRUN_COOL_MIN,
    CONF_BLOWER_OVERRUN_HEAT_MIN,
    CONF_COAST_COOL_RATE,
    CONF_COAST_COOL_TAU_MIN,
    CONF_COAST_HEAT_RATE,
    CONF_COAST_HEAT_TAU_MIN,
    CONF_COMFORT_COOL,
    CONF_COMFORT_HEAT,
    CONF_COMPANION_SENSOR,
    CONF_COOL_RATE_PRESET,
    CONF_DEADBAND_COOL_F,
    CONF_DEADBAND_HEAT_F,
    CONF_EDGE_REPORT_DELAY_S,
    CONF_HEAT_RATE_PRESET,
    CONF_IDLE_BEFORE_OVERRUN_S,
    CONF_INITIAL_TEMP_F,
    CONF_K_ACTIVE_COOL,
    CONF_K_ACTIVE_HEAT,
    CONF_K_PASSIVE,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_RUN_SECONDS,
    CONF_OFF_OFFSET_COOL_F,
    CONF_OFF_OFFSET_HEAT_F,
    CONF_OUTDOOR_SOURCE,
    CONF_PRECISION,
    CONF_REPORT_HVAC_ACTION,
    CONF_SENSOR_LAG_MIN,
    CONF_TICK_SECONDS,
    DEFAULT_DEADBAND_COOL_F,
    DEFAULT_DEADBAND_HEAT_F,
    DEFAULT_MIN_OFF_SECONDS,
    DEFAULT_MIN_RUN_SECONDS,
    DEFAULT_OFF_OFFSET_COOL_F,
    DEFAULT_OFF_OFFSET_HEAT_F,
    DOMAIN,
)
from .hvac_profile import (
    DEFAULT_EDGE_REPORT_DELAY_S,
    DEFAULT_IDLE_BEFORE_OVERRUN_S,
    DEFAULT_PRECISION,
    DEFAULT_RATE_PRESET,
    DEFAULT_SENSOR_LAG_MIN,
    EDGE_REPORT_DELAY_MAX_S,
    IDLE_BEFORE_OVERRUN_MAX_S,
    PHASE_LEAD_IN,
    PHASE_OVERRUN,
    PHASE_RUNNING,
    PRECISION_DEFAULT,
    RATE_PRESET_CUSTOM,
    RATE_PRESETS,
    BlowerConfig,
    EquipmentState,
    advance_equipment,
    coast_average_f_per_h,
    lag_step,
    precision_value,
    preset_for_rate,
    reported_action,
    resolve_rate,
    stop_equipment,
)

_LOGGER = logging.getLogger(__name__)


def sim_device_info(entry: ConfigEntry) -> DeviceInfo:
    """The device a zone's thermostat and its companion sensor share (only created when the companion is on)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get("name") or entry.title,
        manufacturer="Climate Advisor dev tools",
        model="Simulated thermostat",
    )


def _decide_compressor_state(
    *,
    mode: str | None,
    current_temp: float,
    target_temp: float | None,
    deadband: float | None,
    compressor_on: bool,
    last_on_ts: datetime | None,
    last_off_ts: datetime | None,
    min_run_seconds: float,
    min_off_seconds: float,
    now: datetime,
    off_offset: float | None = None,
) -> tuple[bool, datetime | None, datetime | None]:
    """Pure compressor on/off deadband + dwell-timer decision.

    ``deadband`` is the START distance from the target; ``off_offset`` (Issue #1234) is the STOP
    point as a signed distance past the target in the direction of travel (heat stops at
    target + off_offset, cool at target - off_offset). ``None`` = symmetric (stop at +/-deadband).

    Extracted from _async_tick() so it can be hand-verified in test_compressor_state.py
    without importing this module (which requires the `homeassistant` package — see
    test_sim_math.py's module docstring for why that's not available in this repo/venv).

    Returns (new_compressor_on, new_last_on_ts, new_last_off_ts). Callers are
    responsible for resetting compressor_on/last_on_ts/last_off_ts to
    (False, None, None) whenever hvac_mode actually changes (see
    SimulatedThermostat._reset_compressor_state()) — this function only evaluates
    one mode's thresholds and has no way to detect a mode change on its own.
    """
    if mode is None:
        return False, last_on_ts, last_off_ts
    if target_temp is None or deadband is None:
        return compressor_on, last_on_ts, last_off_ts

    if not compressor_on:
        wants_on = (mode == "heat" and current_temp <= target_temp - deadband) or (
            mode == "cool" and current_temp >= target_temp + deadband
        )
        can_turn_on = last_off_ts is None or (now - last_off_ts).total_seconds() >= min_off_seconds
        if wants_on and can_turn_on:
            return True, now, last_off_ts
        return compressor_on, last_on_ts, last_off_ts

    stop = deadband if off_offset is None else off_offset
    wants_off = (mode == "heat" and current_temp >= target_temp + stop) or (
        mode == "cool" and current_temp <= target_temp - stop
    )
    can_turn_off = last_on_ts is None or (now - last_on_ts).total_seconds() >= min_run_seconds
    if wants_off and can_turn_off:
        return False, last_on_ts, now
    return compressor_on, last_on_ts, last_off_ts


try:
    # The real Climate Advisor ODE step — imported, not reimplemented, so this
    # simulator tracks production physics automatically. See module docstring.
    from custom_components.climate_advisor.coordinator import _simulate_indoor_physics
except ImportError as err:  # pragma: no cover - exercised only without climate_advisor installed
    _simulate_indoor_physics = None
    _IMPORT_ERROR = err
else:
    _IMPORT_ERROR = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the simulated thermostat entity from a config entry."""
    if _simulate_indoor_physics is None:
        _LOGGER.error(
            "CA Dev Thermostat Sim requires the climate_advisor integration to be "
            "installed alongside it (imports _simulate_indoor_physics from its "
            "coordinator.py). Import failed: %s",
            _IMPORT_ERROR,
        )
        raise ConfigEntryNotReady(
            "climate_advisor is not installed — CA Dev Thermostat Sim reuses its "
            "ODE physics function and cannot run standalone"
        )

    async_add_entities([SimulatedThermostat(entry)])


class SimulatedThermostat(RestoreEntity, ClimateEntity):
    """A synthetic thermostat whose indoor temperature evolves via the real CA ODE."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    # HEAT_COOL deliberately excluded: this sim has one setpoint
    # (target_temperature) and one active-mode direction per tick, matching
    # how Climate Advisor's own automation drives a thermostat (single
    # setpoint, explicit heat/cool/off mode switches) — not the dual
    # target_temp_low/target_temp_high range HEAT_COOL implies. Advertising
    # it without implementing it left the mode silently inert (Issue #809
    # verification finding).
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    # FAN_MODE: real production call sites (automation.py's _set_hvac_mode(),
    # _activate_fan(), _deactivate_fan()) call climate.set_fan_mode with "auto"/"on"
    # whenever a zone's fan_mode config is hvac_fan/both — CA's "fan runs independent
    # of heat/cool" feature. Without this feature flag, HA rejects that service call
    # with a ServiceValidationError, and two of those three call sites don't catch it.
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # Matches the exact string values automation.py sends — HA's own FAN_AUTO/FAN_ON
    # constants, not custom labels, so a real integration's fan_modes list is what
    # this fixture is validated against.
    _attr_fan_modes = [FAN_AUTO, FAN_ON]

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the simulated thermostat from its config entry data."""
        self._entry = entry
        data = entry.data
        self._attr_unique_id = f"{entry.entry_id}_sim_thermostat"
        self._attr_name = data.get("name")

        self._k_passive: float = data[CONF_K_PASSIVE]
        # HVAC profile (owner request 2026-11-12, hvac_profile.py). Unset preset = custom = the entry's own
        # k_active_heat / k_active_cool, so entries saved before presets existed simulate exactly as before.
        self._heat_rate_preset: str = data.get(CONF_HEAT_RATE_PRESET) or DEFAULT_RATE_PRESET
        self._cool_rate_preset: str = data.get(CONF_COOL_RATE_PRESET) or DEFAULT_RATE_PRESET
        self._k_active_heat: float = resolve_rate(self._heat_rate_preset, data[CONF_K_ACTIVE_HEAT], cooling=False)
        self._k_active_cool: float = resolve_rate(self._cool_rate_preset, data[CONF_K_ACTIVE_COOL], cooling=True)
        # Reported precision: "default" leaves Home Assistant's own (whole degrees on a Fahrenheit system).
        self._precision_option: str = data.get(CONF_PRECISION) or DEFAULT_PRECISION
        if (precision := precision_value(self._precision_option)) is not None:
            self._attr_precision = precision
        # Companion sensor: a tenths temperature sensor on this thermostat's DEVICE (sensor.py), the shape Climate
        # Advisor discovers (indoor_temp.discover_companion_sensors: same device, device_class temperature). With a
        # device the climate entity takes the device's name (entity name None), so its entity id does not change.
        self._companion_enabled: bool = bool(data.get(CONF_COMPANION_SENSOR, False))
        if self._companion_enabled:
            self._attr_name = None
            self._attr_device_info = sim_device_info(entry)
        # Sensor lag: the reported reading(s) follow the simulated air through a first-order lag; 0 = no lag.
        self._sensor_lag_min: float = float(data.get(CONF_SENSOR_LAG_MIN, DEFAULT_SENSOR_LAG_MIN) or 0.0)
        # Edge report delay: hvac_action trails the equipment by this many seconds (a cloud-polled thermostat); 0 =
        # on time (the default: a directly connected thermostat reports its edge as it happens).
        self._edge_report_delay_s: float = min(
            EDGE_REPORT_DELAY_MAX_S,
            max(0.0, float(data.get(CONF_EDGE_REPORT_DELAY_S, DEFAULT_EDGE_REPORT_DELAY_S) or 0.0)),
        )
        self._action_history: list[tuple[datetime, str]] = []
        # Idle gap before the blower overrun (the owner's Ecobee: heating -> idle, then fan 1 s later); 0 = none.
        self._idle_before_overrun_s: float = min(
            IDLE_BEFORE_OVERRUN_MAX_S,
            max(0.0, float(data.get(CONF_IDLE_BEFORE_OVERRUN_S, DEFAULT_IDLE_BEFORE_OVERRUN_S) or 0.0)),
        )
        self._overrun_idle_until: datetime | None = None
        # A thermostat that does not report hvac_action at all (the attribute is absent); default: reported.
        self._report_hvac_action: bool = bool(data.get(CONF_REPORT_HVAC_ACTION, True))
        # Blower lead-in / overrun and after-stop coast, per direction. All zero (the default) = no blower phase.
        self._blower: dict[str, BlowerConfig] = {
            "heat": BlowerConfig(
                lead_in_min=float(data.get(CONF_BLOWER_LEAD_IN_HEAT_MIN, 0.0) or 0.0),
                overrun_min=float(data.get(CONF_BLOWER_OVERRUN_HEAT_MIN, 0.0) or 0.0),
                coast_rate_f_per_h=float(data.get(CONF_COAST_HEAT_RATE, 0.0) or 0.0),
                coast_tau_min=float(data.get(CONF_COAST_HEAT_TAU_MIN, 0.0) or 0.0),
            ),
            "cool": BlowerConfig(
                lead_in_min=float(data.get(CONF_BLOWER_LEAD_IN_COOL_MIN, 0.0) or 0.0),
                overrun_min=float(data.get(CONF_BLOWER_OVERRUN_COOL_MIN, 0.0) or 0.0),
                coast_rate_f_per_h=float(data.get(CONF_COAST_COOL_RATE, 0.0) or 0.0),
                coast_tau_min=float(data.get(CONF_COAST_COOL_TAU_MIN, 0.0) or 0.0),
            ),
        }
        self._equipment = EquipmentState()
        self._last_coast_f_per_h: float = 0.0
        self._comfort_heat: float = data[CONF_COMFORT_HEAT]
        self._comfort_cool: float = data[CONF_COMFORT_COOL]
        self._outdoor_source: str = data[CONF_OUTDOOR_SOURCE]
        self._tick_seconds: int = int(data[CONF_TICK_SECONDS])
        # New keys (Issue: HVAC hysteresis + short-cycle protection) — read via .get()
        # with DEFAULT_* fallback, not [...] , so existing entries created before these
        # keys existed (including the live "Simulated"/"Simulated 2" zones) keep working
        # unmodified until reconfigured.
        self._deadband_heat_f: float = float(data.get(CONF_DEADBAND_HEAT_F, DEFAULT_DEADBAND_HEAT_F))
        self._deadband_cool_f: float = float(data.get(CONF_DEADBAND_COOL_F, DEFAULT_DEADBAND_COOL_F))
        # Unset falls back to the measured real-thermostat stop point (Issue #1387), not the symmetric far edge.
        self._off_offset_heat_f: float = float(data.get(CONF_OFF_OFFSET_HEAT_F, DEFAULT_OFF_OFFSET_HEAT_F))
        self._off_offset_cool_f: float = float(data.get(CONF_OFF_OFFSET_COOL_F, DEFAULT_OFF_OFFSET_COOL_F))
        self._min_run_seconds: float = float(data.get(CONF_MIN_RUN_SECONDS, DEFAULT_MIN_RUN_SECONDS))
        self._min_off_seconds: float = float(data.get(CONF_MIN_OFF_SECONDS, DEFAULT_MIN_OFF_SECONDS))

        self._current_temp: float = float(data.get(CONF_INITIAL_TEMP_F, 70.0))
        # The thermostat's own sensor reading. Equals _current_temp (the simulated air) unless sensor lag is set;
        # see reported_temperature.
        self._reading: float | None = self._current_temp
        self._target_temp: float | None = self._comfort_heat
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._fan_mode: str = FAN_AUTO
        # Whether the last tick actually applied heating/cooling capacity (t_start was
        # on the correct side of target_temperature) vs. having already reached
        # setpoint — drives the hvac_action property below. Real thermostats report
        # "idle" once they reach setpoint even while still in heat/cool mode; a sim
        # that only ever reports the commanded mode (or nothing, pre-fix) can't
        # distinguish "actively driving" from "holding," which several production
        # decision points (thermal-observation gating, restart-cause classification)
        # read via hvac_action.
        self._actively_driving: bool = False
        self._last_update_ts: datetime = dt_util.utcnow()

        # Compressor on/off state machine (deadband + min-run/min-off dwell). See
        # _async_tick() for the state transitions. Timestamps are reset (None) on
        # restart — see async_added_to_hass() — since equipment-protection dwell timers
        # tracking real wall-clock gaps across an HA restart isn't meaningful for a dev
        # fixture.
        self._compressor_on: bool = False
        self._last_on_ts: datetime | None = None
        self._last_off_ts: datetime | None = None

        # Cloud-thermostat echo delay (Issue #1232): seconds between a ``set_temperature`` call and the thermostat
        # reporting it (real cloud thermostats answer 30-120 s late, with no Context of the caller's). 0 = the
        # historical instant, Context-carrying behaviour; only the test house turns it on (``set_echo_delay``).
        self._echo_delay_s: float = 0.0
        self._cancel_echo: list[Any] = []
        # Setpoint drift (Issue #1397): a HomeKit thermostat reports the setpoint it was told, then ``delay`` seconds
        # later the same setpoint ``offset`` off (18.0 C, then 17.9 C). None = off; only the test house turns it on.
        self._setpoint_drift: tuple[float, float] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore prior simulated state (if any) and start the tick timer."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored_temp = last_state.attributes.get("current_temperature")
            if restored_temp is not None:
                try:
                    self._current_temp = float(restored_temp)
                except TypeError, ValueError:
                    _LOGGER.warning(
                        "Could not restore current_temperature=%r for %s, keeping initial_temp_f",
                        restored_temp,
                        self.entity_id,
                    )

            restored_target = last_state.attributes.get("temperature")
            if restored_target is not None:
                with contextlib.suppress(TypeError, ValueError):
                    self._target_temp = float(restored_target)

            if last_state.state in (m.value for m in self._attr_hvac_modes):
                self._hvac_mode = HVACMode(last_state.state)

            restored_fan_mode = last_state.attributes.get("fan_mode")
            if restored_fan_mode in self._attr_fan_modes:
                self._fan_mode = restored_fan_mode

            restored_hvac_action = last_state.attributes.get("hvac_action")
            self._compressor_on = restored_hvac_action in ("heating", "cooling")
            if self._compressor_on:
                self._equipment.phase = PHASE_RUNNING
                self._equipment.direction = "heat" if restored_hvac_action == "heating" else "cool"
            self._reading = self._current_temp  # the restored value is the last reported reading
        else:
            _LOGGER.debug(
                "No prior state for %s — starting from configured initial_temp_f=%.1f",
                self.entity_id,
                self._current_temp,
            )

        # Real elapsed wall time drives the ODE step, not tick_seconds — this
        # keeps the simulation correct across restarts and missed/delayed ticks.
        self._last_update_ts = dt_util.utcnow()
        # Edge report delay: the (restored) action is the starting point, so the first change after a start or a
        # restart is reported late too (it used to show at once: the history started empty).
        self._actively_driving = self._equipment.phase == PHASE_RUNNING
        self._action_history.clear()
        self._note_action(self._last_update_ts)

        # Register this entity so number.py's manual temperature override (Issue #898)
        # can reach it without a direct cross-platform object reference — same pattern
        # occupancy_schedule.py's ZoneOccupancyState uses for the switch entities.
        self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry.entry_id, {})["thermostat_entity"] = self

        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=self._tick_seconds))
        )
        self.async_on_remove(self._cancel_pending_echoes)
        self._notify_siblings()  # the rate/preset/precision controls and the companion read their value from here

    # ------------------------------------------------------------------ HVAC profile (runtime controls)
    def _zone_data(self) -> dict[str, Any]:
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}) if self.hass else {}

    def _notify_siblings(self, *, controls: bool = True) -> None:
        """Re-write the companion sensor (and, unless told not to, the profile controls) from this entity's values."""
        zone = self._zone_data()
        siblings = list(zone.get("profile_controls", ())) if controls else []
        if (companion := zone.get("companion_entity")) is not None:
            siblings.append(companion)
        for entity in siblings:
            if entity.hass is not None and entity.entity_id:
                entity.async_write_ha_state()

    def _persist(self, **changes: Any) -> None:
        """Store a runtime change in the entry's data (no reload), so it survives a restart and shows in reconfigure."""
        self.hass.config_entries.async_update_entry(self._entry, data={**self._entry.data, **changes})

    def rate(self, direction: str) -> float:
        """Equipment capacity in F/h (a magnitude; cooling is applied as negative)."""
        return abs(self._k_active_heat if direction == "heat" else self._k_active_cool)

    def rate_preset(self, direction: str) -> str:
        return self._heat_rate_preset if direction == "heat" else self._cool_rate_preset

    def set_rate(self, direction: str, rate_f_per_h: float) -> None:
        """Set heating or cooling capacity (F/h, magnitude). The preset follows: a preset's value selects it."""
        rate = abs(float(rate_f_per_h))
        preset = preset_for_rate(rate)
        if direction == "heat":
            self._k_active_heat, self._heat_rate_preset = rate, preset
            self._persist(**{CONF_K_ACTIVE_HEAT: rate, CONF_HEAT_RATE_PRESET: preset})
        else:
            self._k_active_cool, self._cool_rate_preset = -rate, preset
            self._persist(**{CONF_K_ACTIVE_COOL: -rate, CONF_COOL_RATE_PRESET: preset})
        _LOGGER.info("CA Dev Thermostat Sim %s: %s rate %.1f F/h (%s)", self.entity_id, direction, rate, preset)
        self.async_write_ha_state()
        self._notify_siblings()

    def set_rate_preset(self, direction: str, preset: str) -> None:
        """Choose a rate preset; ``custom`` keeps the current rate (then adjust it with the rate number)."""
        if preset in RATE_PRESETS:
            self.set_rate(direction, RATE_PRESETS[preset])
            return
        if direction == "heat":
            self._heat_rate_preset = RATE_PRESET_CUSTOM
            self._persist(**{CONF_HEAT_RATE_PRESET: RATE_PRESET_CUSTOM})
        else:
            self._cool_rate_preset = RATE_PRESET_CUSTOM
            self._persist(**{CONF_COOL_RATE_PRESET: RATE_PRESET_CUSTOM})
        self.async_write_ha_state()
        self._notify_siblings()

    @property
    def precision_option(self) -> str:
        return self._precision_option

    def set_precision(self, option: str) -> None:
        """Set the reported precision (whole / half / tenth, or Home Assistant's default)."""
        value = precision_value(option)
        if value is None:  # Home Assistant's own default for this system's unit
            option = PRECISION_DEFAULT
            value = 0.1 if self.hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS else 1.0
        self._precision_option = option
        self._attr_precision = value
        self._persist(**{CONF_PRECISION: option})
        self.async_write_ha_state()
        self._notify_siblings()

    @property
    def sensor_lag_min(self) -> float:
        return self._sensor_lag_min

    def set_sensor_lag(self, minutes: float) -> None:
        """Set the first-order lag (minutes) between the simulated air and the reported reading(s); 0 = none."""
        if self._sensor_lag_min <= 0:
            self._reading = self._current_temp  # start the lag from where the reading is now
        self._sensor_lag_min = max(0.0, float(minutes))
        self._persist(**{CONF_SENSOR_LAG_MIN: self._sensor_lag_min})
        self.async_write_ha_state()
        self._notify_siblings()

    def blower_overrun_min(self, direction: str) -> float:
        return self._blower[direction].overrun_min

    def set_blower_overrun(self, direction: str, minutes: float) -> None:
        """Set how long the blower keeps running (hvac_action ``fan``) after a heat or cool run stops."""
        minutes = max(0.0, float(minutes))
        old = self._blower[direction]
        self._blower[direction] = BlowerConfig(
            lead_in_min=old.lead_in_min,
            overrun_min=minutes,
            coast_rate_f_per_h=old.coast_rate_f_per_h,
            coast_tau_min=old.coast_tau_min,
        )
        key = CONF_BLOWER_OVERRUN_HEAT_MIN if direction == "heat" else CONF_BLOWER_OVERRUN_COOL_MIN
        self._persist(**{key: minutes})
        self.async_write_ha_state()
        self._notify_siblings()

    def _blower_for(self, direction: str | None) -> BlowerConfig:
        return self._blower.get(direction or "", BlowerConfig())

    @property
    def reported_temperature(self) -> float | None:
        """The thermostat's sensor reading: the simulated air, through the sensor lag when one is set.

        With no lag this is ``_current_temp`` itself (tests and the real-HA harness write that directly).
        """
        return self._current_temp if self._sensor_lag_min <= 0 else self._reading

    @property
    def companion_enabled(self) -> bool:
        return self._companion_enabled

    def apply_manual_temperature_override(self, value: float) -> None:
        """Jump the simulated indoor temperature directly (Issue #898) — called by
        number.py's CurrentTemperatureOverride entity. A one-shot override: physics
        resumes from this new value on the next tick, same as if the real world had
        instantaneously changed temperature."""
        _LOGGER.info(
            "CA Dev Thermostat Sim %s: manual override %.1f°F -> %.1f°F",
            self.entity_id,
            self._current_temp,
            value,
        )
        self._current_temp = value
        self._reading = value  # a one-shot jump of the world: the sensor reads it at once too
        self.async_write_ha_state()
        self._notify_siblings(controls=False)

    @property
    def current_temperature(self) -> float | None:
        """Return the thermostat's reading of the simulated indoor temperature (through the sensor lag, if set)."""
        return self.reported_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the current target temperature."""
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return what the thermostat is actually doing right now.

        Read by 12 separate production sites in coordinator.py/automation.py
        (thermal-observation gating, restart-cause classification, fan-expectation
        checks, etc.) — the base ClimateEntity default (None/absent) silently defeated
        all of them for this fixture. HEATING/COOLING only while actively driving
        toward setpoint (see _actively_driving); once setpoint is reached the mode
        stays HEAT/COOL but the real appliance goes idle, matching a real thermostat.

        HVAC profile: during a configured blower lead-in (before the burner or compressor starts) or overrun (after
        it stops) the blower alone runs, reported as FAN in any mode, as an Ecobee reports its fan dissipation time.
        """
        if not self._report_hvac_action:
            return None  # the attribute is absent: this thermostat never says whether it heats or cools
        current = self._true_action()
        if self._edge_report_delay_s <= 0:
            return current
        return HVACAction(reported_action(self._action_history, dt_util.utcnow(), self._edge_report_delay_s, current))

    def _true_action(self) -> HVACAction:
        """What the equipment is doing now (``hvac_action`` shows it ``edge_report_delay_s`` late when that is set)."""
        idle_gap = (
            self._equipment.phase == PHASE_OVERRUN
            and self._overrun_idle_until is not None
            and dt_util.utcnow() < self._overrun_idle_until
        )
        if self._equipment.phase in (PHASE_LEAD_IN, PHASE_OVERRUN) and not idle_gap:
            return HVACAction.FAN
        if self._hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING if self._actively_driving else HVACAction.IDLE
        if self._hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING if self._actively_driving else HVACAction.IDLE
        if self._fan_mode == FAN_ON:
            return HVACAction.FAN
        return HVACAction.OFF

    def _note_action(self, now: datetime) -> None:
        """Remember each change of the true action (for a delayed report); keeps only what the delay can reach."""
        if self._edge_report_delay_s <= 0:
            return
        action = str(self._true_action())
        if not self._action_history or self._action_history[-1][1] != action:
            self._action_history.append((now, action))
        horizon = now - timedelta(seconds=self._edge_report_delay_s + 3600)
        while len(self._action_history) > 2 and self._action_history[1][0] < horizon:
            self._action_history.pop(0)

    @property
    def fan_mode(self) -> str:
        """Return the current fan mode."""
        return self._fan_mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the deadband/dwell state machine's internals for debugging.

        Once dwell timers can hold the compressor in an unexpected on/off state, that
        state is no longer inferable from hvac_action alone — this surfaces it directly.
        """
        return {
            "compressor_on": self._compressor_on,
            "deadband_heat_f": self._deadband_heat_f,
            "deadband_cool_f": self._deadband_cool_f,
            "off_offset_heat_f": self._off_offset_heat_f,
            "off_offset_cool_f": self._off_offset_cool_f,
            "min_run_seconds": self._min_run_seconds,
            "min_off_seconds": self._min_off_seconds,
            # HVAC profile (hvac_profile.py)
            "heat_rate_f_per_h": self.rate("heat"),
            "cool_rate_f_per_h": self.rate("cool"),
            "heat_rate_preset": self._heat_rate_preset,
            "cool_rate_preset": self._cool_rate_preset,
            "precision_option": self._precision_option,
            "sensor_lag_min": self._sensor_lag_min,
            "edge_report_delay_s": self._edge_report_delay_s,
            "idle_before_overrun_s": self._idle_before_overrun_s,
            "report_hvac_action": self._report_hvac_action,
            "air_temperature": None if self._current_temp is None else round(self._current_temp, 3),
            "blower_phase": self._equipment.phase,
            "blower_lead_in_heat_min": self._blower["heat"].lead_in_min,
            "blower_lead_in_cool_min": self._blower["cool"].lead_in_min,
            "blower_overrun_heat_min": self._blower["heat"].overrun_min,
            "blower_overrun_cool_min": self._blower["cool"].overrun_min,
            "coast_f_per_h": round(self._last_coast_f_per_h, 3),
        }

    def _reset_compressor_state(self) -> None:
        """Clear compressor on/off + dwell-timer state (Issue #970-adjacent fix).

        Called whenever hvac_mode actually changes. Heat and cool have independent
        deadbands and dwell timers; without this reset, a direct heat<->cool switch
        (CA's automation never routes through OFF) carries the old mode's
        compressor-on flag and _last_on_ts/_last_off_ts into the new mode's state
        machine in _async_tick(), letting the new mode skip its own turn-on deadband
        check entirely and compute min_run/min_off against the wrong mode's clock.
        """
        self._compressor_on = False
        self._last_on_ts = None
        self._last_off_ts = None
        # The equipment stops with the call: a running run goes to its blower overrun (and coast) if configured,
        # else straight off, as before.
        stop_equipment(self._equipment, self._blower_for(self._equipment.direction), dt_util.utcnow())

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a new HVAC mode (reported ``set_echo_delay()`` seconds later when a cloud echo delay is set, #1246)."""
        if self._echo_delay_s > 0:

            @callback
            def _answer_mode(_now: datetime) -> None:  # @callback: runs in the event loop (it writes entity state)
                self._apply_hvac_mode(hvac_mode)

            self._cancel_echo.append(async_call_later(self.hass, self._echo_delay_s, _answer_mode))
            return
        self._apply_hvac_mode(hvac_mode)

    def _apply_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode != self._hvac_mode:
            self._reset_compressor_state()
        self._hvac_mode = hvac_mode
        # A mode change is the thermostat's own setting, shown at once even with an edge report delay: never
        # "heating" in Off mode (verification 2026-11-13). The delayed report restarts from the new action.
        self._actively_driving = self._equipment.phase == PHASE_RUNNING
        self._action_history.clear()
        self._note_action(dt_util.utcnow())
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set a new fan mode.

        No thermal effect modeled — fan-only mode circulates air without meaningfully
        heating/cooling in the real world either, so this only needs to accept the
        command and report it back correctly (hvac_action reflects it when the
        thermostat is otherwise idle/off).
        """
        self._fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature, and hvac_mode when bundled in the same call.

        Climate Advisor's own ``_set_temperature()`` (automation.py) deliberately sends
        a single combined ``climate.set_temperature`` call with both ``temperature`` and
        ``hvac_mode`` — it never calls ``climate.set_hvac_mode`` separately. HA's core
        service dispatch does not split that back out on this entity's behalf; each
        ClimateEntity is responsible for reading ``hvac_mode`` out of its own
        ``async_set_temperature`` kwargs if it wants to honor a combined call. This
        entity previously read only ``temperature`` and silently dropped ``hvac_mode``
        entirely — the setpoint attribute updated (so CA's setpoint-confirm check, which
        only compares ``temperature``, saw a false success) while ``self._hvac_mode``
        stayed wherever it last was (e.g. "off"), so ``_async_tick()`` always simulated
        passive-only decay no matter what mode CA believed it had commanded. This is what
        Issue #830 traced to: the sim entity, not CA's decision logic, silently discarded
        every heat/cool command sent through the (correct, by-design) combined call path.
        """
        if self._echo_delay_s > 0:
            pending = dict(kwargs)

            @callback
            def _answer(_now: datetime) -> None:  # @callback: runs in the event loop (it writes entity state)
                self._apply_set_temperature(pending)

            self._cancel_echo.append(async_call_later(self.hass, self._echo_delay_s, _answer))
            return
        self._apply_set_temperature(kwargs)

    def _cancel_pending_echoes(self) -> None:
        for cancel in self._cancel_echo:
            cancel()
        self._cancel_echo.clear()

    def set_setpoint_drift(self, offset: float | None, delay_s: float = 30.0) -> None:
        """After reporting each new setpoint, report it again ``offset`` off ``delay_s`` later (None = off, #1397)."""
        self._setpoint_drift = None if offset is None else (float(offset), float(delay_s))

    def set_echo_delay(self, seconds: float) -> None:
        """Make the thermostat report a set_temperature or set_hvac_mode ``seconds`` after the call (0 = instant)."""
        self._echo_delay_s = float(seconds)

    def _apply_set_temperature(self, kwargs: dict[str, Any]) -> None:
        """Apply (and report) one ``set_temperature`` call -- immediately, or when its cloud echo is due."""
        hvac_mode = kwargs.get("hvac_mode")
        if hvac_mode is not None:
            new_mode = HVACMode(hvac_mode) if not isinstance(hvac_mode, HVACMode) else hvac_mode
            if new_mode != self._hvac_mode:
                _LOGGER.info(
                    "CA Dev Thermostat Sim %s: hvac_mode %s -> %s (bundled with set_temperature)",
                    self.entity_id,
                    self._hvac_mode.value,
                    new_mode.value,
                )
                self._reset_compressor_state()
            self._hvac_mode = new_mode

        temperature = kwargs.get("temperature")
        if temperature is None:
            self.async_write_ha_state()
            return
        self._target_temp = float(temperature)
        self.async_write_ha_state()
        if self._setpoint_drift is not None:
            offset, delay_s = self._setpoint_drift
            drifted = round(float(temperature) + offset, 2)

            @callback
            def _drift(_now: datetime) -> None:  # @callback: runs in the event loop (it writes entity state)
                if self._target_temp == float(temperature):  # nothing newer was set meanwhile
                    self._target_temp = drifted
                    self.async_write_ha_state()

            self._cancel_echo.append(async_call_later(self.hass, delay_s, _drift))

    def _read_outdoor_temp(self) -> float | None:
        """Read outdoor temp from the configured source, trying weather then sensor shapes."""
        state = self.hass.states.get(self._outdoor_source)
        if state is None:
            return None

        # Weather entities expose temperature as an attribute; plain sensors as .state.
        attr_temp = state.attributes.get("temperature")
        if attr_temp is not None:
            try:
                return float(attr_temp)
            except TypeError, ValueError:
                pass

        try:
            return float(state.state)
        except TypeError, ValueError:
            return None

    async def _async_tick(self, now: datetime) -> None:
        """Advance the simulated indoor temperature by real elapsed wall time.

        Also evaluates this zone's occupancy schedule (Issue #898 decision 1: reuse
        this same tick rather than a second timer) — a no-op when no occupancy
        schedules are configured for this zone.
        """
        zone_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if zone_data is not None:
            occupancy_state = zone_data.get("occupancy_state")
            if occupancy_state is not None:
                occupancy_state.evaluate(dt_util.as_local(now))

        dt_hours = (now - self._last_update_ts).total_seconds() / 3600.0
        if dt_hours <= 0:
            return

        outdoor_temp = self._read_outdoor_temp()
        if outdoor_temp is None:
            _LOGGER.warning(
                "CA Dev Thermostat Sim %s: outdoor_source %s has no usable temperature — skipping tick",
                self.entity_id,
                self._outdoor_source,
            )
            self._last_update_ts = now
            return

        if self._hvac_mode == HVACMode.HEAT:
            mode, deadband, off_offset = "heat", self._deadband_heat_f, self._off_offset_heat_f
        elif self._hvac_mode == HVACMode.COOL:
            mode, deadband, off_offset = "cool", self._deadband_cool_f, self._off_offset_cool_f
        else:
            mode, deadband, off_offset = None, None, None

        # 1. The elapsed interval [last tick, now] runs with the equipment as it WAS (Issue #1512 reopened, test
        #    realism): the decision below is made at ``now`` and reported at ``now``, before any of its heat. The
        #    tick used to decide first and then apply the new state to the interval that had already elapsed, so
        #    every on/off edge was reported one tick (30 s) after the room had started (stopped) changing; a directly
        #    connected thermostat reports its edge as it happens. ``edge_report_delay_s`` models a late (cloud-polled)
        #    one instead.
        run_dir = self._equipment.direction or mode
        running = self._equipment.phase == PHASE_RUNNING
        coast = coast_average_f_per_h(self._equipment, self._last_update_ts, dt_hours)
        self._last_coast_f_per_h = coast

        if coast == 0.0:
            physics_mode = run_dir if running else mode
            k_active = (self._k_active_heat if run_dir == "heat" else self._k_active_cool) if running else None
            # Issue #1387: a running compressor delivers full capacity until the state machine below stops it.
            # The ODE step only applies capacity while the room is short of the setpoint it is given, and clamps
            # there; the target used to be passed, so with a stop point past the target (the old symmetric
            # default, target+1.5) the room pinned at the target, reporting "heating" with no heat, for hours. An
            # unbounded drive target removes both limits; the room passes the stop point by at most one tick
            # before the compressor stops.
            drive_to = (math.inf if run_dir == "heat" else -math.inf) if running else self._target_temp
        else:
            # After-stop coast (furnace heat exchanger / cold coil) adds to whatever is running: one signed q.
            q = coast
            if running:
                q += abs(self._k_active_heat) if run_dir == "heat" else -abs(self._k_active_cool)
            physics_mode = "heat" if q > 0 else "cool"
            k_active = abs(q)
            drive_to = math.inf if q > 0 else -math.inf

        self._current_temp = _simulate_indoor_physics(
            self._current_temp,
            outdoor_temp,
            self._k_passive,
            k_active,
            dt_hours,
            drive_to,
            comfort_heat=self._comfort_heat,
            comfort_cool=self._comfort_cool,
            hvac_mode=physics_mode,
        )
        self._reading = lag_step(self._reading, self._current_temp, dt_hours, self._sensor_lag_min)

        # 2. Compressor on/off state machine: deadband thresholds decide when the
        # compressor *wants* to change state; min_run/min_off dwell timers can delay
        # that change (equipment short-cycle protection), same as real HVAC hardware.
        # Mode-change resets (heat<->cool losing stale compressor/dwell state) are
        # handled at the call sites that change self._hvac_mode — see
        # _reset_compressor_state(). This function only evaluates one mode's own
        # thresholds against whatever state it's handed.
        # The thermostat decides on its own sensor's reading (the lagged one when sensor lag is set), as a real one.
        was_on = self._compressor_on
        self._compressor_on, self._last_on_ts, self._last_off_ts = _decide_compressor_state(
            mode=mode,
            current_temp=self.reported_temperature,
            target_temp=self._target_temp,
            deadband=deadband,
            compressor_on=self._compressor_on,
            last_on_ts=self._last_on_ts,
            last_off_ts=self._last_off_ts,
            min_run_seconds=self._min_run_seconds,
            min_off_seconds=self._min_off_seconds,
            now=now,
            off_offset=off_offset,
        )
        if self._compressor_on and not was_on:
            _LOGGER.info(
                "CA Dev Thermostat Sim %s: compressor ON (%s, indoor=%.1f target=%.1f)",
                self.entity_id,
                mode,
                self._current_temp,
                self._target_temp,
            )
        elif was_on and not self._compressor_on:
            _LOGGER.info(
                "CA Dev Thermostat Sim %s: compressor OFF (%s, indoor=%.1f target=%.1f)",
                self.entity_id,
                mode,
                self._current_temp,
                self._target_temp,
            )

        # HVAC profile: the call drives the blower phases (lead-in -> running -> overrun). Capacity flows only while
        # RUNNING (from the next interval on); with no lead-in configured (the default) running is exactly the call.
        phase_before = self._equipment.phase
        advance_equipment(
            self._equipment,
            call_on=self._compressor_on,
            direction=mode,
            now=now,
            cfg=self._blower_for(mode or self._equipment.direction),
            cfg_last=self._blower_for(self._equipment.direction),
        )
        self._actively_driving = self._equipment.phase == PHASE_RUNNING
        if phase_before == PHASE_RUNNING and self._equipment.phase == PHASE_OVERRUN and self._idle_before_overrun_s > 0:
            # report "idle" for the gap, then "fan" (one extra state write when the gap ends)
            self._overrun_idle_until = now + timedelta(seconds=self._idle_before_overrun_s)

            @callback
            def _end_idle_gap(_now: datetime) -> None:
                self._note_action(dt_util.utcnow())
                self.async_write_ha_state()

            self._cancel_echo.append(async_call_later(self.hass, self._idle_before_overrun_s, _end_idle_gap))
        self._note_action(now)
        self._last_update_ts = now
        self.async_write_ha_state()
        self._notify_siblings(controls=False)
