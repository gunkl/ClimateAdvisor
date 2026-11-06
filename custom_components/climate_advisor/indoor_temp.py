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

Issue #1033 made failures *observable*: the resolver now also reports **why** a reading is
unavailable (``IndoorTempReading.reason`` / ``sleep_reason``: ``not_configured``,
``entity_missing``, ``unavailable``, ``no_reading``, ``non_numeric``, ``non_finite``,
``implausible``, ``stale``). It stays a pure read: it holds no state and never announces —
the once-per-episode log line and Activity Log events are owned by exactly one event-loop
caller (``coordinator._refresh_indoor_sensor_health``). Announcing from here would be unsafe:
``get_chart_data`` reaches this module from an executor thread, and ``coordinator._emit_event``
itself calls back into the indoor read. Per-call diagnostics are therefore DEBUG only; the one INFO line (sleep
sensor in use / back on the primary source) fires only when a zone's source changes, through a log-only memory
(``_logged_indoor_source``, Issue #1194 a) that never affects a reading.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any, NamedTuple

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    COMPANION_CONSISTENCY_F,
    INDOOR_SENSOR_STALE_HOURS,
    REMOTE_CONTEXT_SLEEP,
    REMOTE_OFFSET_FRESHNESS_MINUTES,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    is_unavailable_state,
)
from .temperature import is_sensor_stale, read_attribute_temp_f, read_sensor_state_f

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
    # Issue #1033: why the primary source produced no reading (None = healthy), and - only while
    # the sleep window is active with a sleep sensor configured - why the sleep sensor was not
    # used. Trailing + defaulted so existing 3-argument constructions keep working.
    reason: str | None = None
    sleep_reason: str | None = None
    # Issue #1194: the REMOTE context whose sensor supplied ``value`` (``REMOTE_CONTEXT_SLEEP`` today), or None
    # when ``value`` is the primary reading. This is the provenance the remote-sensor outer loop keys on
    # (remote value vs ``primary_value``), so a future remote-sensor context only has to set it here.
    remote_context: str | None = None
    # Issue #1230 R1b: the thermostat's finer companion sensor that supplied ``primary_value`` (None = the climate
    # attribute did), and - only when a candidate exists but was not used - why: ``not_proven`` / ``unavailable`` /
    # ``stale`` / ``implausible`` / ``inconsistent`` / ``entity_missing`` / ``non_numeric`` / ``non_finite`` /
    # ``ambiguous``. The sleep swap never changes these: they describe the primary reading.
    companion_entity: str | None = None
    companion_reason: str | None = None


class CompanionSensors:
    """The thermostat-companion candidates for one zone plus the per-lifetime 'finer' latch (Issue #1230 R1b).

    One instance per coordinator, shared with its AutomationEngine, so both resolve the same value. ``entities`` is
    refreshed by the coordinator (event loop, registry reads); the resolver only reads it. ``proven`` holds the
    entities for which a fractional reading has been seen while the thermostat's own attribute was whole-degree - a
    one-way latch for the coordinator's lifetime, so the source never flaps between 69 and 68.54 on a whole reading.
    Headless runs (no registry) simply never create one, so resolution is byte-identical to before.
    """

    __slots__ = ("entities", "proven")

    def __init__(self, entities: Iterable[str] = ()) -> None:
        self.entities: tuple[str, ...] = tuple(entities)
        self.proven: set[str] = set()


def _is_whole(raw: Any) -> bool | None:
    """True when ``raw`` is a whole number, False when it has a fraction, None when it is not a finite number."""
    try:
        value = float(raw)
    except TypeError, ValueError:
        return None
    if not math.isfinite(value):
        return None
    return abs(value - round(value)) < 0.005


def _resolve_companion_f(
    *,
    hass: Any,
    unit: str,
    climate_state: Any,
    climate_val_f: float,
    companion: CompanionSensors,
    now: datetime,
) -> tuple[float | None, str | None, str | None]:
    """The thermostat's companion sensor reading when provably more accurate: ``(value_f, entity, reason)``.

    Per reading: (a) consistent - within ``COMPANION_CONSISTENCY_F`` of the thermostat's own reading; (b) finer -
    latched in ``companion.proven`` the first time a consistent companion shows a fraction while the thermostat's
    attribute is whole-degree (never latched when the attribute itself has fractions, and not used before it is
    latched, so there is no flapping). It must also pass the sensor rules (parse, 40-110 F, 12 h stale). With several
    candidates it is used only when exactly one passes everything, else ``reason='ambiguous'``. Failure is a fallback,
    never an error: ``(None, None, reason)`` and the caller keeps the thermostat's reading.
    """
    climate_whole = _is_whole(getattr(climate_state, "attributes", {}).get("current_temperature"))
    accepted: list[tuple[float, str]] = []
    reasons: list[str] = []
    for entity_id in companion.entities:
        state = hass.states.get(entity_id)
        val_f, reason = read_sensor_state_f(state, unit)
        if reason is not None:
            reasons.append(reason)
            continue
        checked = _check_plausible(val_f, entity_id)
        if checked is None:
            reasons.append("implausible")
            continue
        if is_sensor_stale(state, INDOOR_SENSOR_STALE_HOURS, now):
            reasons.append("stale")
            continue
        if abs(checked - climate_val_f) > COMPANION_CONSISTENCY_F:
            reasons.append("inconsistent")
            continue
        if entity_id not in companion.proven and climate_whole is True and _is_whole(state.state) is False:
            companion.proven.add(entity_id)
        if entity_id not in companion.proven:
            reasons.append("not_proven")
            continue
        accepted.append((checked, entity_id))
    if len(accepted) == 1:
        return accepted[0][0], accepted[0][1], None
    if len(accepted) > 1:
        return None, None, "ambiguous"
    return None, None, reasons[0] if reasons else None


def _resolve_primary_indoor_temp_f(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
    now: datetime,
) -> tuple[float | None, str | None, str | None]:
    """Resolve the primary-source indoor temperature (sensor/input_number/climate_fallback).

    Factored out of ``resolve_indoor_temp_with_provenance()`` so it's computed exactly
    once and reused for both the comfort-facing result and the always-primary
    ``primary_value`` field, rather than being resolved twice. Returns
    ``(value_f, source_entity, reason)``; ``reason`` is None exactly when ``value_f`` is set.
    """
    if source in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER):
        if not indoor_temp_entity:
            return None, None, "not_configured"
        state = hass.states.get(indoor_temp_entity)
        val_f, reason = read_sensor_state_f(state, unit)
        if reason is not None:
            _LOGGER.debug("Indoor temp entity %s unusable: reason=%s", indoor_temp_entity, reason)
            return None, None, reason
        checked = _check_plausible(val_f, indoor_temp_entity)
        if checked is None:
            return None, None, "implausible"
        # input_number helpers legitimately stay unchanged for days - stale applies to `sensor` only.
        if source == TEMP_SOURCE_SENSOR and is_sensor_stale(state, INDOOR_SENSOR_STALE_HOURS, now):
            return None, None, "stale"
        return checked, indoor_temp_entity, None

    # climate_fallback source (also the default for any unrecognized source value)
    climate_state = hass.states.get(climate_entity)
    val_f, reason = read_attribute_temp_f(climate_state, "current_temperature", unit)
    if reason is not None:
        # Only a FAILED attribute read is relabelled: a thermostat can report state "unknown"
        # (no HVAC mode) yet still publish a valid current_temperature, which must keep working.
        if reason == "no_reading" and is_unavailable_state(getattr(climate_state, "state", None)):
            reason = "unavailable"
        _LOGGER.debug("Indoor temp from climate entity %s unusable: reason=%s", climate_entity, reason)
        return None, None, reason
    checked = _check_plausible(val_f, climate_entity)
    if checked is None:
        return None, None, "implausible"
    return checked, climate_entity, None


def _resolve_sleep_indoor_temp_f(
    *, hass: Any, unit: str, sleep_indoor_temp_entity: str, now: datetime
) -> tuple[float | None, str | None]:
    """Resolve the sleep-window bedroom sensor: ``(value_f, None)`` or ``(None, reason)``."""
    state = hass.states.get(sleep_indoor_temp_entity)
    val_f, reason = read_sensor_state_f(state, unit)
    if reason is not None:
        _LOGGER.debug("Sleep indoor sensor %s unusable: reason=%s", sleep_indoor_temp_entity, reason)
        return None, reason
    checked = _check_plausible(val_f, sleep_indoor_temp_entity)
    if checked is None:
        return None, "implausible"
    if not sleep_indoor_temp_entity.startswith("input_number.") and is_sensor_stale(
        state, INDOOR_SENSOR_STALE_HOURS, now
    ):
        return None, "stale"
    return checked, None


def resolve_indoor_temp_with_provenance(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
    in_sleep_window: bool = False,
    sleep_indoor_temp_entity: str | None = None,
    now: datetime | None = None,
    companion: CompanionSensors | None = None,
) -> IndoorTempReading:
    """Resolve indoor temperature: comfort-facing value + provenance + always-primary value.

    ``source``/``unit``/``indoor_temp_entity``/``climate_entity`` behave exactly as before
    (see module docstring history). When ``in_sleep_window`` and ``sleep_indoor_temp_entity``
    are both set, the sleep sensor is tried first for the comfort-facing ``value`` only; on
    any failure (missing state, non-numeric, implausible, stale) it falls through to the primary
    resolution rather than returning an unavailable reading - automation must never stall
    overnight because a bedroom sensor's battery died. ``primary_value`` is always the
    unswapped primary-source reading, regardless of the sleep-sensor outcome. ``now`` (UTC) is
    only used for the staleness check and defaults to the current time.

    ``companion`` (Issue #1230 R1b, "Bug D"; default None = exactly the behavior above): on the
    ``climate_fallback`` source only, a finer temperature sensor on the thermostat's own device replaces the
    thermostat's reading as the PRIMARY value when ``_resolve_companion_f`` proves it is the same reading at finer
    resolution. ``sensor``/``input_number`` sources are explicit user choices and are never overridden. The sleep
    sensor still takes priority over both for the comfort-facing value.
    """
    now = now or dt_util.utcnow()
    primary_val, primary_entity, primary_reason = _resolve_primary_indoor_temp_f(
        hass=hass,
        source=source,
        unit=unit,
        indoor_temp_entity=indoor_temp_entity,
        climate_entity=climate_entity,
        now=now,
    )

    companion_entity: str | None = None
    companion_reason: str | None = None
    if (
        companion is not None
        and companion.entities
        and source not in (TEMP_SOURCE_SENSOR, TEMP_SOURCE_INPUT_NUMBER)
        and primary_val is not None
    ):
        comp_val, companion_entity, companion_reason = _resolve_companion_f(
            hass=hass,
            unit=unit,
            climate_state=hass.states.get(climate_entity),
            climate_val_f=primary_val,
            companion=companion,
            now=now,
        )
        if comp_val is not None:
            primary_val, primary_entity = comp_val, companion_entity

    sleep_reason: str | None = None
    if in_sleep_window and sleep_indoor_temp_entity:
        val_f, sleep_reason = _resolve_sleep_indoor_temp_f(
            hass=hass, unit=unit, sleep_indoor_temp_entity=sleep_indoor_temp_entity, now=now
        )
        if val_f is not None:
            _log_indoor_source(climate_entity, sleep_indoor_temp_entity, val_f, remote=True)
            return IndoorTempReading(
                val_f,
                sleep_indoor_temp_entity,
                primary_val,
                primary_reason,
                None,
                REMOTE_CONTEXT_SLEEP,
                companion_entity,
                companion_reason,
            )

    _log_indoor_source(climate_entity, primary_entity, primary_val, remote=False)
    return IndoorTempReading(
        primary_val, primary_entity, primary_val, primary_reason, sleep_reason, None, companion_entity, companion_reason
    )


def read_indoor_sensors_f(
    *,
    hass: Any,
    unit: str,
    climate_entity: str | None,
    indoor_temp_entity: str | None = None,
    sleep_indoor_temp_entity: str | None = None,
    companion_entities: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, float | None]:
    """Read EVERY configured indoor temperature source: ``{entity_id: temp_f_or_None}`` (chart_log ``indoor_sensors``).

    Occupant-first: the v4 thermal model fits one line per indoor sensor, so each chart-log row must carry what every
    configured sensor read, not only the comfort-facing value (which swaps to the bedroom sensor in the sleep window).
    Sources, in order and de-duplicated: the climate entity's ``current_temperature``, ``indoor_temp_entity`` (sensor or
    input_number) and ``sleep_indoor_temp_entity``, then ``companion_entities`` (Issue #1230 R1: the temperature sensors
    on the climate entity's own device, e.g. a thermostat whose climate attribute is whole degrees but whose device also
    exposes a 0.18 F-step sensor; found by ``discover_companion_temp_sensors``, passed in so this stays a pure read).
    Companions are read exactly like any other sensor (same unit conversion, plausibility band and 12 h stale rule).

    Each source is read under the SAME rules as the resolver (``read_sensor_state_f`` / ``read_attribute_temp_f``, the
    ``_check_plausible`` band, and the ``INDOOR_SENSOR_STALE_HOURS`` stale rule, exempting ``input_number.*`` helpers);
    a failed read is ``None``, never a placeholder. Values are internal degrees F rounded to 2 decimals (a 0.18 F-step
    sensor keeps its resolution). Independent of the sleep window and of which source is in use.

    Pure read: no events, nothing above DEBUG (``_refresh_indoor_sensor_health`` already announces failures), safe on an
    executor thread, never raises (a failing source reads ``None``).
    """
    now = now or dt_util.utcnow()
    sources: list[tuple[str, bool]] = []  # (entity_id, is_climate)
    if climate_entity:
        sources.append((climate_entity, True))
    for extra in (indoor_temp_entity, sleep_indoor_temp_entity, *companion_entities):
        if extra:
            sources.append((extra, False))

    readings: dict[str, float | None] = {}
    for entity_id, is_climate in sources:
        if entity_id in readings:
            continue
        value: float | None = None
        try:
            state = hass.states.get(entity_id)
            if is_climate:
                val_f, reason = read_attribute_temp_f(state, "current_temperature", unit)
            else:
                val_f, reason = read_sensor_state_f(state, unit)
            if reason is None and val_f is not None:
                checked = _check_plausible(val_f, entity_id)
                if checked is not None and (
                    is_climate
                    or entity_id.startswith("input_number.")
                    or not is_sensor_stale(state, INDOOR_SENSOR_STALE_HOURS, now)
                ):
                    value = round(checked, 2)
        except Exception:  # noqa: BLE001 - a chart row must never fail on a sensor read
            _LOGGER.debug("Indoor sensor snapshot read failed for %s", entity_id, exc_info=True)
        readings[entity_id] = value
    return readings


# Issue #1230 R1b: a thermostat device can also carry temperature sensors that do NOT read the room air - e.g. a
# Honeywell TCC thermostat's "Outdoor temperature", or a floor-heating thermostat's "Floor temperature". The 1 F
# consistency rule alone would let such a sensor become CA's indoor reading whenever it happens to sit within 1 F of the
# room. The words are matched on the entity's OWN name (``original_name`` / ``translation_key``, i.e. without the
# device name), so a thermostat named "First Floor" keeps its room sensor. A false exclusion only means the
# pre-0.9.2 behavior (the thermostat's own reading).
_NON_ROOM_SENSOR_WORDS = ("outdoor", "outside", "exterior", "floor")


def _names_non_room_sensor(entry: Any) -> bool:
    """True when the registry entry's own name says it measures something other than the room air."""
    for raw in (getattr(entry, "original_name", None), getattr(entry, "translation_key", None)):
        if isinstance(raw, str):
            words = raw.lower().replace("_", " ").split()
            if any(word in words for word in _NON_ROOM_SENSOR_WORDS):
                return True
    return False


def discover_companion_temp_sensors(hass: Any, climate_entity: str | None) -> tuple[str, ...]:
    """``sensor.*`` temperature entities on the SAME device as ``climate_entity`` (Issue #1230 R1), sorted.

    Occupant-first: many thermostats report ``current_temperature`` in whole degrees while the same device also exposes
    a finer ``device_class: temperature`` sensor; logging that sensor lets the thermal model see the real heating curve.
    Reads only the in-memory entity registry (no I/O) and so must run on the event loop. Disabled entities are skipped
    (``include_disabled_entities=False``); ``device_class`` is the user override else the integration's original class.
    A sensor whose own name says outdoor / outside / exterior / floor is skipped (``_names_non_room_sensor``).
    No climate entity, an unregistered one, or one with no device -> ``()``. Never raises. Consumers: chart_log
    ``indoor_sensors`` and (Issue #1230 R1b) the ``CompanionSensors`` candidates the resolver may use for control.
    """
    if not climate_entity:
        return ()
    try:
        ent_reg = er.async_get(hass)
        entry = ent_reg.async_get(climate_entity)
        if entry is None or entry.device_id is None:
            return ()
        found = [
            sibling.entity_id
            for sibling in er.async_entries_for_device(ent_reg, entry.device_id, include_disabled_entities=False)
            if sibling.domain == "sensor"
            and (sibling.device_class or sibling.original_device_class) == "temperature"
            and sibling.entity_id != climate_entity
            and not _names_non_room_sensor(sibling)
        ]
        return tuple(sorted(found))
    except Exception:  # noqa: BLE001 - discovery is best-effort; a chart row must never fail on it
        _LOGGER.debug("Companion temperature sensor discovery failed for %s", climate_entity, exc_info=True)
        return ()


# Issue #1194 (a): LOG-ONLY memory of the comfort-facing source last logged per zone (keyed by climate entity), so the
# "using the sleep sensor" line is INFO once per source change and DEBUG on every other call. The remote-sensor outer
# loop resolves every minute, which made the old per-call INFO ~600 lines a night. Resolution itself stays a pure read:
# nothing here feeds any decision or the returned reading. Callers on an executor thread (``get_chart_data``) can at
# worst race one duplicate or missing INFO line (single-key dict get/set), never a wrong reading.
_logged_indoor_source: dict[str, str | None] = {}


def _log_indoor_source(zone: str, source: str | None, value_f: float | None, *, remote: bool) -> None:
    previous = _logged_indoor_source.get(zone, "<unset>")
    _logged_indoor_source[zone] = source
    changed = previous != source and (previous != "<unset>" or remote)
    if remote:
        (_LOGGER.info if changed else _LOGGER.debug)(
            "Using sleep indoor sensor entity=%s value=%.1f°F", source, value_f if value_f is not None else float("nan")
        )
    elif changed:
        _LOGGER.info("Indoor reading source changed to the primary source entity=%s (was %s)", source, previous)


def remote_reading_fresh(state: Any, entity_id: str | None, now: datetime) -> bool:
    """True when a remote sensor's reading is recent enough for the remote-sensor outer loop (Issue #1194).

    Age is measured on ``last_reported`` (``is_sensor_stale`` falls back to ``last_updated``): these sensors
    report on change in coarse steps and can sit idle for 100+ minutes, so ``last_changed`` would mark a
    healthy sensor stale. Older than ``REMOTE_OFFSET_FRESHNESS_MINUTES`` -> not fresh -> the caller uses the
    plain setpoint. ``input_number`` helpers legitimately stay unchanged for days, so they are always fresh
    (same exemption as the resolver). Fails open when timestamps are unavailable, like ``is_sensor_stale``.
    """
    if state is None:
        return False
    if entity_id and entity_id.startswith("input_number."):
        return True
    return not is_sensor_stale(state, REMOTE_OFFSET_FRESHNESS_MINUTES / 60.0, now)


def resolve_indoor_temp_f(
    *,
    hass: Any,
    source: str,
    unit: str,
    indoor_temp_entity: str | None,
    climate_entity: str,
    in_sleep_window: bool = False,
    sleep_indoor_temp_entity: str | None = None,
    companion: CompanionSensors | None = None,
) -> float | None:
    """Read the current comfort-facing indoor temperature in Fahrenheit, or None.

    Back-compat thin wrapper over ``resolve_indoor_temp_with_provenance()`` - unchanged
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
        companion=companion,
    ).value


def _check_plausible(val_f: float, source_entity: str) -> float | None:
    """Return val_f if within the plausible indoor range, else log (DEBUG) and return None."""
    if MIN_PLAUSIBLE_INDOOR_F <= val_f <= MAX_PLAUSIBLE_INDOOR_F:
        return val_f
    _LOGGER.debug(
        "Indoor temp %.1f°F from %s is outside plausible range [%.0f, %.0f]°F; treating as unavailable",
        val_f,
        source_entity,
        MIN_PLAUSIBLE_INDOOR_F,
        MAX_PLAUSIBLE_INDOOR_F,
    )
    return None
