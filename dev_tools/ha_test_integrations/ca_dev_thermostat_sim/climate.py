"""Climate platform for CA Dev Thermostat Sim.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Reuses the real Climate Advisor ODE step function (_simulate_indoor_physics)
so this simulator can never drift from production thermal-model behavior
(DRY rule, CLAUDE.md). That function lives in
custom_components/climate_advisor/coordinator.py:9519-9569 as a pure,
module-level function with no instance-state dependency, so it can be
imported directly.

NOTE ON HA VERSION: ClimateEntity/RestoreEntity's API shape used here
(hvac_modes, target_temperature, current_temperature, async_get_last_state,
async_write_ha_state) has been stable since well before hacs.json's pinned
minimum homeassistant version (2024.6.0), but this was NOT verified against
a locally-installed `homeassistant` package — none exists in this repo/venv.
Test on a real Home Assistant instance before relying on it.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.components.climate.const import FAN_AUTO, FAN_ON
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COMFORT_COOL,
    CONF_COMFORT_HEAT,
    CONF_INITIAL_TEMP_F,
    CONF_K_ACTIVE_COOL,
    CONF_K_ACTIVE_HEAT,
    CONF_K_PASSIVE,
    CONF_OUTDOOR_SOURCE,
    CONF_TICK_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

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
        self._k_active_heat: float = data[CONF_K_ACTIVE_HEAT]
        self._k_active_cool: float = data[CONF_K_ACTIVE_COOL]
        self._comfort_heat: float = data[CONF_COMFORT_HEAT]
        self._comfort_cool: float = data[CONF_COMFORT_COOL]
        self._outdoor_source: str = data[CONF_OUTDOOR_SOURCE]
        self._tick_seconds: int = int(data[CONF_TICK_SECONDS])

        self._current_temp: float = float(data.get(CONF_INITIAL_TEMP_F, 70.0))
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

    async def async_added_to_hass(self) -> None:
        """Restore prior simulated state (if any) and start the tick timer."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored_temp = last_state.attributes.get("current_temperature")
            if restored_temp is not None:
                try:
                    self._current_temp = float(restored_temp)
                except (TypeError, ValueError):
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
        else:
            _LOGGER.debug(
                "No prior state for %s — starting from configured initial_temp_f=%.1f",
                self.entity_id,
                self._current_temp,
            )

        # Real elapsed wall time drives the ODE step, not tick_seconds — this
        # keeps the simulation correct across restarts and missed/delayed ticks.
        self._last_update_ts = dt_util.utcnow()

        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=self._tick_seconds))
        )

    @property
    def current_temperature(self) -> float | None:
        """Return the simulated indoor temperature."""
        return self._current_temp

    @property
    def target_temperature(self) -> float | None:
        """Return the current target temperature."""
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Return what the thermostat is actually doing right now.

        Read by 12 separate production sites in coordinator.py/automation.py
        (thermal-observation gating, restart-cause classification, fan-expectation
        checks, etc.) — the base ClimateEntity default (None/absent) silently defeated
        all of them for this fixture. HEATING/COOLING only while actively driving
        toward setpoint (see _actively_driving); once setpoint is reached the mode
        stays HEAT/COOL but the real appliance goes idle, matching a real thermostat.
        """
        if self._hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING if self._actively_driving else HVACAction.IDLE
        if self._hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING if self._actively_driving else HVACAction.IDLE
        if self._fan_mode == FAN_ON:
            return HVACAction.FAN
        return HVACAction.OFF

    @property
    def fan_mode(self) -> str:
        """Return the current fan mode."""
        return self._fan_mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a new HVAC mode."""
        self._hvac_mode = hvac_mode
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
            self._hvac_mode = new_mode

        temperature = kwargs.get("temperature")
        if temperature is None:
            self.async_write_ha_state()
            return
        self._target_temp = float(temperature)
        self.async_write_ha_state()

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
            except (TypeError, ValueError):
                pass

        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _async_tick(self, now: datetime) -> None:
        """Advance the simulated indoor temperature by real elapsed wall time."""
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
            k_active = self._k_active_heat
            mode = "heat"
        elif self._hvac_mode == HVACMode.COOL:
            k_active = self._k_active_cool
            mode = "cool"
        else:
            # OFF simulates as passive-only decay toward outdoor temp.
            k_active = None
            mode = None

        # Mirrors _simulate_indoor_physics's own q!=0 condition exactly (coordinator.py)
        # so hvac_action can't drift out of sync with what the ODE step actually did
        # this tick — computed from the temperature BEFORE this tick's step, same as
        # the ODE function reads t_start.
        self._actively_driving = self._target_temp is not None and (
            (mode == "heat" and self._current_temp < self._target_temp)
            or (mode == "cool" and self._current_temp > self._target_temp)
        )

        self._current_temp = _simulate_indoor_physics(
            self._current_temp,
            outdoor_temp,
            self._k_passive,
            k_active,
            dt_hours,
            self._target_temp,
            comfort_heat=self._comfort_heat,
            comfort_cool=self._comfort_cool,
            hvac_mode=mode,
        )
        self._last_update_ts = now
        self.async_write_ha_state()
