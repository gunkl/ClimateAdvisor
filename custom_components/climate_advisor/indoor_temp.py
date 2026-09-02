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
"""

from __future__ import annotations

import logging
from typing import Any

from .const import TEMP_SOURCE_INPUT_NUMBER, TEMP_SOURCE_SENSOR
from .temperature import to_fahrenheit

_LOGGER = logging.getLogger(__name__)

# Plausible indoor temperature range in Fahrenheit. Values outside this band indicate
# a sensor glitch (e.g. a thermostat echoing its new setpoint into current_temperature
# during a setpoint-only transition) and are treated as unavailable rather than
# propagated into automation decisions or the chart log.
MIN_PLAUSIBLE_INDOOR_F: float = 40.0
MAX_PLAUSIBLE_INDOOR_F: float = 110.0


def resolve_indoor_temp_f(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
) -> float | None:
    """Read the current indoor temperature in Fahrenheit, or None if unavailable.

    ``source`` is the configured ``indoor_temp_source`` value
    (``TEMP_SOURCE_SENSOR``/``TEMP_SOURCE_INPUT_NUMBER`` or
    ``TEMP_SOURCE_CLIMATE_FALLBACK`` — anything else falls through to the
    climate_fallback path, matching both callers' prior behavior). ``unit`` is the
    configured display unit ("fahrenheit"/"celsius") used to convert the raw
    reading. A reading outside [MIN_PLAUSIBLE_INDOOR_F, MAX_PLAUSIBLE_INDOOR_F] or
    a non-numeric raw value is logged as a WARNING and treated as unavailable
    (returns None) on every path — callers must never receive an unvalidated
    glitch value.
    """
    if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
        if not indoor_temp_entity:
            return None
        state = hass.states.get(indoor_temp_entity)
        if state is None:
            return None
        try:
            val_f = to_fahrenheit(float(state.state), unit)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Indoor temp entity %s has non-numeric state %r; treating as unavailable",
                indoor_temp_entity,
                state.state,
            )
            return None
        return _check_plausible(val_f, indoor_temp_entity)

    # climate_fallback source (also the default for any unrecognized source value)
    climate_state = hass.states.get(climate_entity)
    if climate_state is None:
        return None
    temp = climate_state.attributes.get("current_temperature")
    if temp is None:
        return None
    try:
        val_f = to_fahrenheit(float(temp), unit)
    except (ValueError, TypeError):
        _LOGGER.warning(
            "Indoor temp from climate entity %s has non-numeric current_temperature %r; treating as unavailable",
            climate_entity,
            temp,
        )
        return None
    return _check_plausible(val_f, climate_entity)


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
