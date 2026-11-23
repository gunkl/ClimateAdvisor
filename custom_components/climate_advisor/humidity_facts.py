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
* ``humid_air_verdict`` / ``humid_air_blocks`` - the H1 humid-air guard (Issue #1691): the one predicate every fan
  start and every open-window advice calls, on the facts' dew points and latches (``step_humid_latches`` is the one
  latch step, shared with the coordinator's 5-minute tick). ``humid_air_ceiling_from_config`` reads the option.

Reasons a dew point is unknown (``DewPoint.reason``). Seven from the indoor/outdoor sensor vocabulary:
``not_configured`` (no humidity source), ``entity_missing``, ``unavailable``, ``non_numeric``, ``non_finite``,
``implausible`` (temperature outside the plausible range, or no dew point for the pair) and ``stale``; plus four of
its own: ``wrong_unit`` (a humidity sensor whose unit is not %), ``no_temperature`` (no temperature on the
humidity's device), ``recalibrating`` (the paired temperature is the thermostat's while it reads falsely high after a
heating shutdown, Issue #1676) and ``glitch`` (``paired_glitch``, or 0 % RH).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    CONF_HUMID_AIR_GUARD_CEILING_F,
    CONF_HUMID_AIR_GUARD_ENABLED,
    DEFAULT_HUMID_AIR_GUARD_CEILING_F,
    DEFAULT_HUMID_AIR_GUARD_ENABLED,
    MAX_HUMID_AIR_GUARD_CEILING_F,
    MAX_PLAUSIBLE_OUTDOOR_F,
    MIN_HUMID_AIR_GUARD_CEILING_F,
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
# lanes cannot collide on names). Nothing emits them yet. ``nat_vent_humid_skip`` left this list when Issue #1691
# started emitting it (mapped in outcomes.py); ``humid_windows_close_notice`` when Issue #1692 did (re-justified
# unmapped in outcomes.py).
RESERVED_EVENT_TYPES: tuple[str, ...] = (
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


# H1 humid-air guard (Issue #1691, plan #1507 H1): the relative clause "outdoor air wetter than the house" as one
# latch on a derived signal (see ``humid_air_signals``). The configurable ceiling latch is not a fixed spec: it is
# built from the option by ``humid_air_latch_specs``. H2 (Issue #1693) adds its fixed spec below; H3 appends its own.
HUMID_AIR_CEILING_LATCH = "humid_air_ceiling"
HUMID_AIR_WETTER_LATCH = "humid_air_wetter"
SIGNAL_OUTDOOR_WETTER_MARGIN = "outdoor_wetter_margin"
HUMID_AIR_HYSTERESIS_F = 1.0  # every H1 latch releases 1 °F below where it engaged
HUMID_AIR_RELATIVE_FLOOR_F = 55.0  # below this outdoor dew point, wetter-than-house air never blocks (it is dry air)
HUMID_AIR_RELATIVE_MARGIN_F = 2.0  # outdoor dew point at least this far above the house's = wetter than the house

# H2 humid-aware HVAC-fan restrict (Issue #1693, plan #1507 H2): the indoor dew point at or above
# HVAC_FAN_RESTRICT_DP_F (released at or below HVAC_FAN_RESTRICT_DP_RELEASE_F) keeps the "cooled within the last 2 h"
# clause of the 'heat' restriction; see ``hvac_fan_restrict_humid``. A dry house has little condensate to
# re-evaporate, so the blower may circulate; unknown = the 2 h rule as before. Internal constants, no option. The same
# 55 F as HUMID_AIR_RELATIVE_FLOOR_F with a different meaning: never reuse one for the other. The "currently in cool
# mode" clause and the 'cool' restriction ("heated within 2 h") ignore humidity.
HVAC_FAN_RESTRICT_DP_F = 55.0
HVAC_FAN_RESTRICT_DP_RELEASE_F = 54.0
HVAC_FAN_RESTRICT_HUMID_LATCH = "hvac_fan_restrict_humid"
HVAC_FAN_RESTRICT_LATCH_SPEC = LatchSpec(
    HVAC_FAN_RESTRICT_HUMID_LATCH, SIGNAL_INDOOR, HVAC_FAN_RESTRICT_DP_F, HVAC_FAN_RESTRICT_DP_RELEASE_F
)

LATCH_SPECS: tuple[LatchSpec, ...] = (
    LatchSpec(HUMID_AIR_WETTER_LATCH, SIGNAL_OUTDOOR_WETTER_MARGIN, 0.0, -HUMID_AIR_HYSTERESIS_F),
    HVAC_FAN_RESTRICT_LATCH_SPEC,
)


def update_latches(
    specs: Iterable[LatchSpec], dew_points: Mapping[str, float | None], state: Mapping[str, bool]
) -> dict[str, bool]:
    """The next latch state for every spec from the signals' dew points (pure; unknown releases a latch)."""
    return {
        spec.name: latch_above(dew_points.get(spec.signal), spec.on, spec.off, bool(state.get(spec.name, False)))
        for spec in specs
    }


# ---------------------------------------------------------------------------------------------------------------
# H1 humid-air guard (Issue #1691, plan #1507 H1): one pure predicate for every consumer (fan starts, window advice).
# ---------------------------------------------------------------------------------------------------------------


def fmt_dp_log(dp_f: float | None) -> str:
    """A dew point (°F) for a log line, at the resolution it was computed (2 dp), or ``unknown``. The one formatter
    for every humid-air log line (review B11)."""
    return f"{dp_f:.2f}" if dp_f is not None else "unknown"


def humid_air_signals(indoor_dp_f: float | None, outdoor_dp_f: float | None) -> dict[str, float | None]:
    """The derived latch signals of the H1 guard (pure).

    ``outdoor_wetter_margin`` = min(outdoor DP - 55 °F, outdoor DP - indoor DP - 2 °F): at or above 0 the outdoor air
    is humid (DP >= 55 °F) AND at least 2 °F wetter than the house; at or below -1 one of the two let go by 1 °F (the
    hysteresis of both conditions in one latch). Needs both dew points: None (= not blocking) when either is unknown.
    """
    if indoor_dp_f is None or outdoor_dp_f is None:
        margin = None
    else:
        margin = min(
            outdoor_dp_f - HUMID_AIR_RELATIVE_FLOOR_F,
            outdoor_dp_f - indoor_dp_f - HUMID_AIR_RELATIVE_MARGIN_F,
        )
    return {SIGNAL_OUTDOOR_WETTER_MARGIN: margin}


def humid_air_latch_specs(ceiling_f: float | None) -> tuple[LatchSpec, ...]:
    """The ceiling latch for the configured ceiling (on at ``ceiling_f``, off 1 °F below); none when the guard is
    off (``ceiling_f`` None)."""
    if ceiling_f is None:
        return ()
    return (LatchSpec(HUMID_AIR_CEILING_LATCH, SIGNAL_OUTDOOR, ceiling_f, ceiling_f - HUMID_AIR_HYSTERESIS_F),)


def step_humid_latches(
    dew_points: Mapping[str, float | None], state: Mapping[str, bool], *, ceiling_f: float | None
) -> dict[str, bool]:
    """The next state of every humid latch (``LATCH_SPECS`` plus the configured ceiling) from the decision dew points
    (``{SIGNAL_INDOOR: dp, SIGNAL_OUTDOOR: dp}``). Pure; the coordinator's 5-minute tick keeps the result, and
    ``humid_air_verdict`` takes the same step at each decision from it (so the veto itself is stateless)."""
    signals: dict[str, float | None] = dict(dew_points)
    signals.update(humid_air_signals(dew_points.get(SIGNAL_INDOOR), dew_points.get(SIGNAL_OUTDOOR)))
    return update_latches((*LATCH_SPECS, *humid_air_latch_specs(ceiling_f)), signals, state)


HUMID_AIR_REASON_CEILING = "ceiling"
HUMID_AIR_REASON_WETTER = "wetter_than_house"


@dataclass(frozen=True)
class HumidAirVerdict:
    """The H1 guard's answer for one decision: ``blocks`` and why, with the dew points it judged (for Activity text,
    the Status line and the log)."""

    blocks: bool
    reason: str | None  # HUMID_AIR_REASON_CEILING / HUMID_AIR_REASON_WETTER (ceiling wins when both), None = clear
    indoor_dp_f: float | None
    outdoor_dp_f: float | None
    ceiling_f: float | None  # None = the guard is off
    latches: frozenset[str] = frozenset()  # the H1 latch names on after this step


def humid_air_verdict(
    indoor_dp_f: float | None,
    outdoor_dp_f: float | None,
    *,
    ceiling_f: float | None,
    latched: Iterable[str] = (),
) -> HumidAirVerdict:
    """Is the outdoor air too humid to bring into the house (fan start, open-window advice)? Pure, never raises.

    Blocks when outdoor DP >= ``ceiling_f`` (released at or below ``ceiling_f`` - 1 °F), OR outdoor DP >= 55 °F AND
    outdoor DP >= indoor DP + 2 °F (released once outdoor DP <= 54 °F or the gap <= 1 °F). ``latched`` are the latch
    names on from the facts (``EngineFacts.humid_latches``), so a decision between two ticks keeps the hysteresis.
    Missing inputs (plan #1507 table): the ceiling needs only the outdoor dew point, the relative clause needs both;
    an unknown input never blocks (today's behaviour). ``ceiling_f`` None = the guard is off: never blocks.
    """
    if ceiling_f is None:
        return HumidAirVerdict(False, None, indoor_dp_f, outdoor_dp_f, None)
    was = frozenset(latched)
    state = {name: name in was for name in (HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH)}
    nxt = step_humid_latches({SIGNAL_INDOOR: indoor_dp_f, SIGNAL_OUTDOOR: outdoor_dp_f}, state, ceiling_f=ceiling_f)
    ceiling = nxt.get(HUMID_AIR_CEILING_LATCH, False)
    wetter = nxt.get(HUMID_AIR_WETTER_LATCH, False)
    reason = HUMID_AIR_REASON_CEILING if ceiling else HUMID_AIR_REASON_WETTER if wetter else None
    on = frozenset(name for name in (HUMID_AIR_CEILING_LATCH, HUMID_AIR_WETTER_LATCH) if nxt.get(name))
    return HumidAirVerdict(reason is not None, reason, indoor_dp_f, outdoor_dp_f, ceiling_f, on)


def humid_air_blocks(
    indoor_dp_f: float | None,
    outdoor_dp_f: float | None,
    *,
    ceiling_f: float | None,
    latched: Iterable[str] = (),
) -> bool:
    """``humid_air_verdict(...).blocks``: the one humid-air predicate every H1 consumer calls."""
    return humid_air_verdict(indoor_dp_f, outdoor_dp_f, ceiling_f=ceiling_f, latched=latched).blocks


def _finite_dp(value: Any) -> float | None:
    """A dew point read from facts: a finite number, else None (unknown). A stub's placeholder (MagicMock, NaN, a
    bool) never passes for a real reading."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _facts_dew_points(facts: Any) -> tuple[float | None, float | None, frozenset[str]]:
    """The one reader of an ``EngineFacts``' humidity fields (duck-typed) for every humid rule: indoor and outdoor
    dew point (finite numbers only, else unknown) and the names of the latches that are on."""
    latches = getattr(facts, "humid_latches", None)
    latched = frozenset(latches) if isinstance(latches, frozenset | set | tuple | list) else frozenset()
    return (
        _finite_dp(getattr(facts, "indoor_dew_point_f", None)),
        _finite_dp(getattr(facts, "outdoor_dew_point_f", None)),
        latched,
    )


def humid_air_verdict_from_facts(facts: Any, ceiling_f: float | None) -> HumidAirVerdict:
    """``humid_air_verdict`` on an ``EngineFacts`` (through ``_facts_dew_points``)."""
    indoor, outdoor, latched = _facts_dew_points(facts)
    return humid_air_verdict(indoor, outdoor, ceiling_f=ceiling_f, latched=latched)


def humid_air_ceiling_from_config(config: Mapping[str, Any] | None) -> float | None:
    """The H1 ceiling (°F) from the options, None when the guard is off. Defaults: On, 65 °F; a stored value outside
    55-70 °F (or not a number) is clamped / falls back to the default, so a bad entry never switches the guard off."""
    cfg = config or {}
    if not cfg.get(CONF_HUMID_AIR_GUARD_ENABLED, DEFAULT_HUMID_AIR_GUARD_ENABLED):
        return None
    raw = cfg.get(CONF_HUMID_AIR_GUARD_CEILING_F, DEFAULT_HUMID_AIR_GUARD_CEILING_F)
    try:
        value = float(raw)
    except TypeError, ValueError:
        return DEFAULT_HUMID_AIR_GUARD_CEILING_F
    if value != value:  # NaN
        return DEFAULT_HUMID_AIR_GUARD_CEILING_F
    return min(max(value, MIN_HUMID_AIR_GUARD_CEILING_F), MAX_HUMID_AIR_GUARD_CEILING_F)


# ---------------------------------------------------------------------------------------------------------------
# H2 humid-aware HVAC-fan restrict (Issue #1693, plan #1507 H2).
# ---------------------------------------------------------------------------------------------------------------


def hvac_fan_restrict_humid(indoor_dp_f: float | None, *, latched: Iterable[str] = ()) -> bool | None:
    """Is the house humid enough that the blower must wait out the 2 h after cooling (wet coil)? Pure, never raises.

    True at an indoor dew point >= ``HVAC_FAN_RESTRICT_DP_F`` (55 F), held while above
    ``HVAC_FAN_RESTRICT_DP_RELEASE_F`` (54 F) when the latch ``hvac_fan_restrict_humid`` is in ``latched`` (from
    ``EngineFacts.humid_latches``: the coordinator's 5-minute tick steps it with the other humid latches); False
    below. None when the indoor dew point is unknown: the caller applies the 2 h rule as before (plan #1507 table).
    """
    if indoor_dp_f is None:
        return None
    was = frozenset(latched)
    spec = HVAC_FAN_RESTRICT_LATCH_SPEC
    return update_latches((spec,), {spec.signal: indoor_dp_f}, {spec.name: spec.name in was})[spec.name]


def hvac_fan_restrict_humid_from_facts(facts: Any) -> tuple[bool | None, float | None]:
    """``hvac_fan_restrict_humid`` on an ``EngineFacts`` (through ``_facts_dew_points``), with the indoor dew point
    it judged."""
    indoor, _outdoor, latched = _facts_dew_points(facts)
    return hvac_fan_restrict_humid(indoor, latched=latched), indoor
