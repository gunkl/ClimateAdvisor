"""Indoor temperature read/validate logic shared by AutomationEngine and coordinator.

``AutomationEngine._get_indoor_temp_f()`` (automation.py) and
``ClimateAdvisorCoordinator._get_indoor_temp()`` (coordinator.py) independently
re-implemented the same "resolve the configured indoor temperature source, convert
to Fahrenheit" logic (Issue #796, Step 10). They had drifted: the coordinator's
version rejected physically implausible readings (e.g. a thermostat momentarily
echoing its new setpoint into ``current_temperature`` during a setpoint-only
transition) via a plausible-range guard; the automation.py version had no such
guard on either of its two source paths, so all 13 of its call sites could act on
an unvalidated glitch value. The automation.py version's ``climate_fallback`` path
also had no exception handling around the ``float(temp)`` conversion, so a
non-numeric ``current_temperature`` attribute would raise uncaught instead of
being treated as unavailable like every other malformed-reading case in this
module already is.

This module is the single source of truth for that resolution, mirroring the
existing ``fan_status.py`` precedent: a tiny, dependency-free, stateless module
function that each caller (``AutomationEngine._get_indoor_temp_f()``,
``ClimateAdvisorCoordinator._get_indoor_temp()``) delegates to, each still doing
its own fresh ``hass``/config read at call time — there is no shared caching here,
since the two callers read on different cadences (automation.py's timers/listeners
read between coordinator cycles).

Issue #895 added an optional sleep-window sensor override: while the configured sleep
schedule is active, ``resolve_indoor_temp_with_provenance()`` swaps the *comfort-facing*
reading to a second, bedroom-area sensor (falling back to the primary sensor if that
sensor is unavailable). The result carries both the comfort-facing value and an
always-primary ``primary_value`` field, because one consumer — thermal-learning's
whole-house OLS parameter fitting (see coordinator.py's ``_get_current_sample()``,
``_start_hvac_observation()``, ``_sample_all_observations()``,
``_end_hvac_active_phase()``) — must never see the swapped value: the thermal model
characterizes the whole house's envelope, not one room's comfort, so it always reads
``primary_value`` regardless of sleep-window state. Both fields are computed by one
resolver call so a caller never has to re-derive which sensor is "the" primary sensor
on its own.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from .const import TEMP_SOURCE_INPUT_NUMBER, TEMP_SOURCE_SENSOR
from .temperature import to_fahrenheit

_LOGGER = logging.getLogger(__name__)

# Plausible indoor temperature range in Fahrenheit. Values outside this band indicate
# a sensor glitch (e.g. a thermostat echoing its new setpoint into current_temperature
# during a setpoint-only transition) and are treated as unavailable rather than
# propagated into automation decisions or the chart log.
MIN_PLAUSIBLE_INDOOR_F: float = 40.0
MAX_PLAUSIBLE_INDOOR_F: float = 110.0


class IndoorTempReading(NamedTuple):
    """Result of resolving indoor temperature, with provenance.

    ``value``/``source_entity`` are the comfort-facing reading — sleep-window swapped
    when applicable. ``primary_value`` is ALWAYS the primary-source resolution, never
    swapped; thermal-learning sampling must read this field, never ``value``.
    """

    value: float | None
    source_entity: str | None
    primary_value: float | None


def _resolve_primary_indoor_temp_f(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
) -> tuple[float | None, str | None]:
    """Resolve the primary-source indoor temperature (sensor/input_number/climate_fallback).

    Factored out of ``resolve_indoor_temp_with_provenance()`` so it's computed exactly
    once and reused for both the comfort-facing result and the always-primary
    ``primary_value`` field, rather than being resolved twice.
    """
    if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
        if not indoor_temp_entity:
            return None, None
        state = hass.states.get(indoor_temp_entity)
        if state is None:
            return None, None
        try:
            val_f = to_fahrenheit(float(state.state), unit)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Indoor temp entity %s has non-numeric state %r; treating as unavailable",
                indoor_temp_entity,
                state.state,
            )
            return None, None
        checked = _check_plausible(val_f, indoor_temp_entity)
        return checked, (indoor_temp_entity if checked is not None else None)

    # climate_fallback source (also the default for any unrecognized source value)
    climate_state = hass.states.get(climate_entity)
    if climate_state is None:
        return None, None
    temp = climate_state.attributes.get("current_temperature")
    if temp is None:
        return None, None
    try:
        val_f = to_fahrenheit(float(temp), unit)
    except (ValueError, TypeError):
        _LOGGER.warning(
            "Indoor temp from climate entity %s has non-numeric current_temperature %r; treating as unavailable",
            climate_entity,
            temp,
        )
        return None, None
    checked = _check_plausible(val_f, climate_entity)
    return checked, (climate_entity if checked is not None else None)


def resolve_indoor_temp_with_provenance(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
    in_sleep_window: bool = False,
    sleep_indoor_temp_entity: str | None = None,
) -> IndoorTempReading:
    """Resolve indoor temperature: comfort-facing value + provenance + always-primary value.

    ``source``/``unit``/``indoor_temp_entity``/``climate_entity`` behave exactly as before
    (see module docstring history). When ``in_sleep_window`` and ``sleep_indoor_temp_entity``
    are both set, the sleep sensor is tried first for the comfort-facing ``value`` only; on
    any failure (missing state, non-numeric, implausible) it falls through to the primary
    resolution rather than returning an unavailable reading — automation must never stall
    overnight because a bedroom sensor's battery died. ``primary_value`` is always the
    unswapped primary-source reading, regardless of the sleep-sensor outcome.
    """
    primary_val, primary_entity = _resolve_primary_indoor_temp_f(
        hass=hass,
        source=source,
        unit=unit,
        indoor_temp_entity=indoor_temp_entity,
        climate_entity=climate_entity,
    )

    if in_sleep_window and sleep_indoor_temp_entity:
        state = hass.states.get(sleep_indoor_temp_entity)
        val_f: float | None = None
        if state is not None:
            try:
                val_f = to_fahrenheit(float(state.state), unit)
            except (ValueError, TypeError):
                val_f = None
            if val_f is not None:
                val_f = _check_plausible(val_f, sleep_indoor_temp_entity)
        if val_f is not None:
            _LOGGER.info(
                "Using sleep indoor sensor entity=%s value=%.1f°F",
                sleep_indoor_temp_entity,
                val_f,
            )
            return IndoorTempReading(val_f, sleep_indoor_temp_entity, primary_val)
        _LOGGER.warning(
            "Sleep sensor unavailable, falling back to primary indoor sensor entity=%s",
            sleep_indoor_temp_entity,
        )

    return IndoorTempReading(primary_val, primary_entity, primary_val)


def resolve_indoor_temp_f(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
    in_sleep_window: bool = False,
    sleep_indoor_temp_entity: str | None = None,
) -> float | None:
    """Read the current comfort-facing indoor temperature in Fahrenheit, or None.

    Back-compat thin wrapper over ``resolve_indoor_temp_with_provenance()`` — unchanged
    behavior for the ~45 existing call sites that only need the value, not provenance.
    """
    return resolve_indoor_temp_with_provenance(
        hass=hass,
        source=source,
        unit=unit,
        indoor_temp_entity=indoor_temp_entity,
        climate_entity=climate_entity,
        in_sleep_window=in_sleep_window,
        sleep_indoor_temp_entity=sleep_indoor_temp_entity,
    ).value


def _check_plausible(val_f: float, source_entity: str) -> float | None:
    """Return val_f if within the plausible indoor range, else log and return None."""
    if MIN_PLAUSIBLE_INDOOR_F <= val_f <= MAX_PLAUSIBLE_INDOOR_F:
        return val_f
    _LOGGER.warning(
        "Indoor temp %.1f°F from %s is outside plausible range [%.0f, %.0f]°F; treating as unavailable",
        val_f,
        source_entity,
        MIN_PLAUSIBLE_INDOOR_F,
        MAX_PLAUSIBLE_INDOOR_F,
    )
    return None
