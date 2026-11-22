"""Dew-point facts for decisions (Issue #1690, plan #1507 H0): one reader, one glitch rule, one latch helper.

Occupant view: before Climate Advisor lets humidity change anything (a fan start on a muggy night, the open-window
advice, an overcool run), it has to know the house's moisture reliably. This module turns the humidity the Status
chart already shows into a dew point the decisions can trust: the humidity is paired with the temperature of the
SAME device (read exactly as the temperature readers read it, so one sensor never gives two temperatures), a
reconnect glitch or a stale or unit-less reading is not used, and a 15-minute median calms the thermostat's
whole-percent flips. Unknown always means today's behaviour: a decision
that finds no dew point acts exactly as it did before humidity existed. Nothing here reads feels-like.

Pieces (all pure; the coordinator owns the only state):

* ``paired_glitch`` - the one reconnect-glitch rule: exactly 0 % RH with exactly 32.0 F (a sensor's reconnect
  default, seen on the thermostat and the outdoor sensor), or a first reading of exactly 32.0 F after the sensor was
  unavailable / unknown. Used by this reader AND by the outdoor temperature path (#1689). The reader also treats a
  lone 0 % RH as a glitch (0 % never happens in a house or outdoors), so it only drops that one reading.
* ``ReconnectWatch`` - remembers, per entity, the state that arrived right after ``unavailable`` / ``unknown``
  (``const.is_availability_recovery``); "first reading after unavailable" holds for ``SENSOR_RECONNECT_SETTLE_S``
  after it arrived (the glitch lasted 2.8-4.6 s).
* ``read_dew_point`` - the side-effect-free reader for one role (indoor day / sleep, outdoor): the humidity source
  ``humidity_sources.resolve_humidity_paired`` picks (the chart's own pick), its paired temperature, and the checks.
* ``DewPointHistory`` and ``smoothed_dew_point`` - the 15-minute median (samples written by the coordinator's 5-minute
  tick, the reading at the decision added).
* ``latch_above`` - the one hysteresis helper for every later humid rule (on at ``on``, off at or below ``off``).

Reasons a dew point is unknown (``DewPoint.reason``). Seven from the indoor/outdoor sensor vocabulary:
``not_configured`` (no humidity source), ``entity_missing``, ``unavailable``, ``non_numeric``, ``non_finite``,
``implausible`` (temperature outside the plausible range, or no dew point for the pair) and ``stale``; plus four of
its own: ``wrong_unit`` (a humidity sensor whose unit is not %), ``no_temperature`` (no temperature on the
humidity's device), ``recalibrating`` (the paired temperature is the thermostat's while it reads falsely high after a
heating shutdown, Issue #1676) and ``glitch`` (``paired_glitch``, or 0 % RH).
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    MAX_PLAUSIBLE_OUTDOOR_F,
    MIN_PLAUSIBLE_OUTDOOR_F,
    SENSOR_RECONNECT_SETTLE_S,
    is_availability_recovery,
    is_unavailable_state,
)
from .feels_like import dew_point_f
from .humidity_sources import ROLE_OUTDOOR, Discover, resolve_humidity_paired
from .indoor_temp import MAX_PLAUSIBLE_INDOOR_F, MIN_PLAUSIBLE_INDOOR_F
from .temperature import (
    is_sensor_stale,
    read_attribute_temp_f,
    read_sensor_state_f,
    stale_check_applies,
    unit_key_from_attr,
)
from .time_utils import seconds_since

GLITCH_RH = 0.0
GLITCH_TEMP_F = 32.0

SIGNAL_INDOOR = "indoor"
SIGNAL_OUTDOOR = "outdoor"

REASON_GLITCH = "glitch"
REASON_STALE = "stale"

# Event types reserved for the later #1507 lanes (registered unmapped in tools/sim_harness/outcomes.py now, so the
# lanes cannot collide on names). Nothing emits them yet.
RESERVED_EVENT_TYPES: tuple[str, ...] = (
    "nat_vent_humid_skip",  # H1: a nat-vent / fan start refused on humid outdoor air (sibling of *_forecast_skip)
    "humid_windows_close_notice",  # H1: humid outside with the windows open: "close the windows, the AC takes over"
    "overcool_started",  # H3: humid overcool lowered the commanded cool setpoint
    "overcool_ended",  # H3: humid overcool released (dew point dropped, run ended, 60-minute cap, gate closed)
)


def paired_glitch(temp_f: float | None, rh: float | None, *, first_after_unavailable: bool = False) -> bool:
    """True for a reconnect glitch: exactly 0 % RH with exactly 32.0 F, or a first reading of exactly 32.0 F after
    the sensor was unavailable / unknown.

    ``rh`` is the humidity of the SAME device as ``temp_f`` (None when it has none). A Celsius sensor's 0.0 C converts
    to exactly 32.0 F, so one rule serves both units. The second rule catches the 32.0 F default when its humidity has
    not arrived yet (temperature back first, humidity still unavailable). Any other first reading after an outage
    (52.9 F, even while the humidity still reads 0 %) is a real temperature and is used at once.
    """
    if rh is not None and temp_f is not None and rh == GLITCH_RH and temp_f == GLITCH_TEMP_F:
        return True
    return first_after_unavailable and temp_f == GLITCH_TEMP_F


class ReconnectWatch:
    """Per-entity memory of the state that arrived right after ``unavailable`` (Issues #1689, #1690).

    ``note`` is fed by a state-change listener (exact); ``observe`` by a periodic read (the sampler tick) for entities
    without a listener, which only catches a reconnect that a tick happens to straddle. Both only remember;
    ``first_after_unavailable`` is a pure read. "Unavailable" is ``const.UNAVAILABLE_STATES`` (``unavailable`` or
    ``unknown``, so ``unavailable -> unknown -> 32.0`` is still a reconnect); ``None`` (no previous state, e.g. a
    scenario's first write) is not (``const.is_availability_recovery``).
    """

    __slots__ = ("_seen_unavailable", "_since")

    def __init__(self) -> None:
        self._since: dict[str, datetime] = {}
        self._seen_unavailable: set[str] = set()

    def note(self, entity_id: str, old_state: Any, new_state: Any) -> None:
        """A state change: remember ``new_state`` when ``old_state`` was unavailable, else forget the entity."""
        new = getattr(new_state, "state", None) if new_state is not None else None
        stamp = getattr(new_state, "last_changed", None)
        if is_unavailable_state(new):
            return  # still down (``unavailable -> unknown``): keep waiting for the first real reading
        if is_availability_recovery(old_state) and new is not None and isinstance(stamp, datetime):
            self._since[entity_id] = stamp
        else:
            self._since.pop(entity_id, None)

    def observe(self, entity_id: str, state: Any) -> None:
        """A periodic read: an entity read as unavailable and now available is remembered like ``note`` does."""
        value = getattr(state, "state", None) if state is not None else None
        if is_unavailable_state(value):
            self._seen_unavailable.add(entity_id)
            self._since.pop(entity_id, None)
            return
        if entity_id in self._seen_unavailable and value is not None:
            self._seen_unavailable.discard(entity_id)
            stamp = getattr(state, "last_changed", None)
            if isinstance(stamp, datetime) and entity_id not in self._since:
                self._since[entity_id] = stamp

    def first_after_unavailable(self, entity_id: str | None, state: Any, now: datetime) -> bool:
        """True while ``state`` is the reading that arrived after unavailable, for ``SENSOR_RECONNECT_SETTLE_S``."""
        if not entity_id or state is None:
            return False
        marked = self._since.get(entity_id)
        if marked is None or getattr(state, "last_changed", None) != marked:
            return False
        try:
            return seconds_since(marked, now=now) < SENSOR_RECONNECT_SETTLE_S
        except TypeError:  # naive vs aware: not the same clock
            return False


@dataclass(frozen=True)
class DewPoint:
    """One role's dew point at a moment, with where it came from (``reason`` None = usable)."""

    dp_f: float | None
    rh: float | None = None
    temp_f: float | None = None
    humidity_source: str | None = None
    temp_source: str | None = None
    reason: str | None = None

    @property
    def known(self) -> bool:
        return self.dp_f is not None and self.reason is None


def _unknown(reason: str, *, rh: float | None = None, source: str | None = None, temp: str | None = None) -> DewPoint:
    return DewPoint(None, rh=rh, humidity_source=source, temp_source=temp, reason=reason)


def _own_unit(state: Any, attr: str, configured: str) -> str:
    """The weather entity's own ``temperature_unit`` (as the forecast is read), else the configured unit."""
    attrs = getattr(state, "attributes", None) or {}
    return unit_key_from_attr(attrs.get(attr)) or configured


def _read_temp(hass: Any, entity_id: str, unit: str) -> tuple[float | None, str | None, Any]:
    """``(value_f, reason, state)`` of an entity's temperature, read exactly as the temperature readers read it
    (``indoor_temp`` and ``coordinator._read_outdoor_sensor``: the configured unit, the shared parsers), so a sensor
    never gives the dew point a different temperature from the one the decisions act on."""
    state = hass.states.get(entity_id)
    if entity_id.startswith("climate."):
        value, reason = read_attribute_temp_f(state, "current_temperature", unit)
        if reason == "no_reading" and is_unavailable_state(getattr(state, "state", None)):
            return None, "unavailable", state
        return value, reason, state
    value, reason = read_sensor_state_f(state, unit)
    return value, reason, state


def _stale(
    state: Any, stale_hours: float, now: datetime, entity_id: str, sources: Mapping[str, str] | None = None
) -> bool:
    """The shared stale rule (``temperature.stale_check_applies``: input_number helpers are exempt)."""
    if not stale_check_applies(entity_id, (sources or {}).get(entity_id)):
        return False
    return state is not None and is_sensor_stale(state, stale_hours, now)


def read_dew_point(
    hass: Any,
    role: str,
    *,
    chosen: str | None,
    source_entities: Iterable[str | None],
    fallback_entity: str | None,
    discover: Discover,
    discover_temp: Callable[[str], tuple[str, ...]],
    unit: str,
    stale_hours: float,
    now: datetime,
    false_entities: Iterable[str] = (),
    reconnect: ReconnectWatch | None = None,
    configured_sources: Mapping[str, str] | None = None,
) -> DewPoint:
    """The dew point for ``role`` from the humidity the chart reads and the same device's temperature. Pure; no raise.

    ``discover`` is the resolver's companion lookup (the coordinator passes its cache-only one); ``discover_temp(eid)``
    lists the temperature sensors on ``eid``'s device (used for the user's chosen humidity entity). ``false_entities``
    are the entities reading falsely while the thermostat recalibrates (its own and its device's): pairing with one
    is ``recalibrating``. Staleness uses ``temperature.is_sensor_stale`` on both the humidity and the temperature,
    with the shared exemption (``configured_sources``: entity -> the source the occupant configured for it).
    """
    try:
        rh, source, via = resolve_humidity_paired(
            hass,
            role,
            chosen=chosen,
            source_entities=source_entities,
            fallback_entity=fallback_entity,
            discover=discover,
        )
        if source is None or rh is None:
            return _unknown("not_configured")
        outdoor = role == ROLE_OUTDOOR
        lo, hi = (
            (MIN_PLAUSIBLE_OUTDOOR_F, MAX_PLAUSIBLE_OUTDOOR_F)
            if outdoor
            else (MIN_PLAUSIBLE_INDOOR_F, MAX_PLAUSIBLE_INDOOR_F)
        )
        if "#" in source:  # an attribute of the climate / weather entity: humidity and temperature on one state
            entity_id, attr = source.split("#", 1)
            state = hass.states.get(entity_id)
            if _stale(state, stale_hours, now, entity_id, configured_sources):
                return _unknown(REASON_STALE, rh=rh, source=source)
            if attr == "humidity":  # the weather entity: its own dew point first, else its own temperature
                w_unit = _own_unit(state, "temperature_unit", unit)
                dp, dp_reason = read_attribute_temp_f(state, "dew_point", w_unit)
                if dp_reason is None and dp is not None:
                    if not lo <= dp <= hi:
                        return _unknown("implausible", rh=rh, source=source, temp=f"{entity_id}#dew_point")
                    return DewPoint(dp, rh=rh, humidity_source=source, temp_source=f"{entity_id}#dew_point")
                temp_f, t_reason = read_attribute_temp_f(state, "temperature", w_unit)
                temp_source = f"{entity_id}#temperature"
            else:
                temp_f, t_reason = read_attribute_temp_f(state, "current_temperature", unit)
                temp_source = entity_id
            if t_reason is not None or temp_f is None:
                return _unknown(t_reason or "no_temperature", rh=rh, source=source, temp=temp_source)
            temp_entity = entity_id
            temp_state = state
            h_state = state
            h_entity = entity_id
        else:
            h_entity = source
            h_state = hass.states.get(source)
            unit_attr = (getattr(h_state, "attributes", None) or {}).get("unit_of_measurement")
            if unit_attr != "%":
                return _unknown("wrong_unit", rh=rh, source=source)
            candidates: tuple[str, ...] = (via,) if via else tuple(discover_temp(source))
            if not candidates:
                return _unknown("no_temperature", rh=rh, source=source)
            temp_entity = candidates[0]
            temp_f, t_reason, temp_state = _read_temp(hass, temp_entity, unit)
            temp_source = temp_entity
            if t_reason is not None or temp_f is None:
                return _unknown(t_reason or "no_temperature", rh=rh, source=source, temp=temp_source)
            if _stale(h_state, stale_hours, now, h_entity, configured_sources):
                return _unknown(REASON_STALE, rh=rh, source=source, temp=temp_source)
        if _stale(temp_state, stale_hours, now, temp_entity, configured_sources):
            return _unknown(REASON_STALE, rh=rh, source=source, temp=temp_source)
        if temp_entity in set(false_entities):
            return _unknown("recalibrating", rh=rh, source=source, temp=temp_source)
        first_after = reconnect is not None and (
            reconnect.first_after_unavailable(h_entity, h_state, now)
            or reconnect.first_after_unavailable(temp_entity, temp_state, now)
        )
        # 0 % RH alone is a glitch too (no real air is that dry): it drops only this reading, never the window.
        if rh == GLITCH_RH or paired_glitch(temp_f, rh, first_after_unavailable=first_after):
            return _unknown(REASON_GLITCH, rh=rh, source=source, temp=temp_source)
        if not lo <= temp_f <= hi:
            return _unknown("implausible", rh=rh, source=source, temp=temp_source)
        dp = dew_point_f(temp_f, rh)
        if dp is None:
            return _unknown("implausible", rh=rh, source=source, temp=temp_source)
        return DewPoint(dp, rh=rh, temp_f=temp_f, humidity_source=source, temp_source=temp_source)
    except Exception:  # noqa: BLE001 - a decision input must never raise; unknown = today's behaviour
        return _unknown("not_configured")


class DewPointHistory:
    """The recent dew-point samples per signal (indoor / outdoor) for the 15-minute median. Coordinator-owned.

    The samples belong to ONE humidity source: a sample from another source (the bedroom at the sleep-window edge, a
    sensor that died and fell back to the thermostat) restarts the window, so the median never mixes two rooms.
    """

    __slots__ = ("_samples", "_sources", "_window")

    def __init__(self, window: timedelta) -> None:
        self._window = window
        self._samples: dict[str, deque[tuple[datetime, float]]] = {}
        self._sources: dict[str, str | None] = {}

    def add(self, signal: str, at: datetime, dp_f: float, source: str | None = None) -> None:
        if signal in self._sources and self._sources[signal] != source:
            self._samples.pop(signal, None)
        self._sources[signal] = source
        buf = self._samples.setdefault(signal, deque())
        buf.append((at, dp_f))
        self._prune(buf, at)

    def clear(self, signal: str) -> None:
        self._samples.pop(signal, None)
        self._sources.pop(signal, None)

    def source(self, signal: str) -> str | None:
        """The humidity source the window's samples came from (None when empty)."""
        return self._sources.get(signal) if self._samples.get(signal) else None

    def recent(self, signal: str, now: datetime, source: str | None = None) -> list[float]:
        """The samples of ``signal`` inside the window ending at ``now`` (pure read); none when ``source`` is given
        and differs from the window's."""
        if source is not None and self._sources.get(signal) != source:
            return []
        out: list[float] = []
        for at, value in self._samples.get(signal, ()):
            try:
                if 0.0 <= seconds_since(at, now=now) <= self._window.total_seconds():
                    out.append(value)
            except TypeError:
                continue
        return out

    def _prune(self, buf: deque[tuple[datetime, float]], now: datetime) -> None:
        while buf:
            try:
                old = seconds_since(buf[0][0], now=now) > self._window.total_seconds()
            except TypeError:
                old = True
            if not old:
                break
            buf.popleft()


def decision_dew_point(current: DewPoint, history: DewPointHistory | None, signal: str, now: datetime) -> DewPoint:
    """The fact a decision gets: ``smoothed_dew_point`` over the window of the SAME source, with the source used.

    A known reading uses only samples from its own source; a glitch falls back to the window's samples (whose
    source the fact then names). ``DewPoint(None, reason=...)`` when unknown.
    """
    if current.known:
        recent = history.recent(signal, now, current.humidity_source) if history is not None else []
        return DewPoint(
            smoothed_dew_point(current, recent),
            rh=current.rh,
            temp_f=current.temp_f,
            humidity_source=current.humidity_source,
            temp_source=current.temp_source,
        )
    if current.reason == REASON_GLITCH and history is not None:
        value = smoothed_dew_point(current, history.recent(signal, now))
        if value is not None:
            return DewPoint(value, humidity_source=history.source(signal))
    return DewPoint(None, humidity_source=current.humidity_source, reason=current.reason)


def smoothed_dew_point(current: DewPoint, recent: Iterable[float]) -> float | None:
    """The decision dew point: the median of the window's samples plus ``current``; None when unknown.

    A reading that is unknown for a lasting reason (no source, stale, unavailable, wrong unit, recalibrating, ...)
    makes the fact unknown at once (today's behaviour). A momentary glitch only drops the current reading: the
    window's samples still answer, if there are any.
    """
    values = list(recent)
    if current.known and current.dp_f is not None:
        values.append(current.dp_f)
    elif current.reason != REASON_GLITCH:
        return None
    return statistics.median(values) if values else None


def latch_above(value: float | None, on: float, off: float, was_on: bool) -> bool:
    """Hysteresis: on once ``value >= on``, stays on while ``value > off`` (off at or below ``off``); unknown ``value``
    -> off (Issue #1690).

    The one helper for every humid rule (the H1 ceiling, the H2 restrict, the H3 trigger). ``off`` must not exceed
    ``on``.
    """
    if off > on:
        raise ValueError(f"latch_above: off ({off}) must not exceed on ({on})")
    if value is None:
        return False
    return value > off if was_on else value >= on


@dataclass(frozen=True)
class LatchSpec:
    """One humid latch the coordinator keeps on a dew-point signal (filled by the later #1507 lanes)."""

    name: str
    signal: str
    on: float
    off: float


# No humid rule exists yet (wave 1 only carries the facts); later lanes append their specs here.
LATCH_SPECS: tuple[LatchSpec, ...] = ()


def update_latches(
    specs: Iterable[LatchSpec], dew_points: Mapping[str, float | None], state: Mapping[str, bool]
) -> dict[str, bool]:
    """The next latch state for every spec from the signals' dew points (pure; unknown releases a latch)."""
    return {
        spec.name: latch_above(dew_points.get(spec.signal), spec.on, spec.off, bool(state.get(spec.name, False)))
        for spec in specs
    }
