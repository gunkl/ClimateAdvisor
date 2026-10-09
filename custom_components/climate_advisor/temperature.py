"""Temperature unit utilities for Climate Advisor.

All internal temperatures are stored and calculated in Fahrenheit.
This module provides the only conversion boundary used throughout the integration.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NamedTuple

FAHRENHEIT = "fahrenheit"
CELSIUS = "celsius"

UNIT_SYMBOL: dict[str, str] = {
    FAHRENHEIT: "°F",
    CELSIUS: "°C",
}


# Derived from UNIT_SYMBOL: each unit's symbol ("°F"), bare letter ("F") and key name
# ("FAHRENHEIT"), upper-cased for case-insensitive lookup.
_UNIT_ATTR_ALIASES: dict[str, str] = {
    alias.upper(): key for key, symbol in UNIT_SYMBOL.items() for alias in (symbol, symbol.lstrip("°"), key)
}


def unit_key_from_attr(attr: object) -> str | None:
    """Map an entity's ``temperature_unit`` attribute to FAHRENHEIT/CELSIUS.

    Accepts the symbol ("°F"/"°C"), the bare letter ("F"/"C") and the word
    ("fahrenheit"/"celsius"), case-insensitive and whitespace-tolerant. Returns None for
    None or anything unrecognised — callers decide what an unrecognised value means.
    """
    if attr is None:
        return None
    return _UNIT_ATTR_ALIASES.get(str(attr).strip().upper())


def unit_mismatch(attr: object, configured: str) -> str | None:
    """Return the reported unit key when it is recognised and differs from ``configured``.

    ``attr`` is a raw unit attribute (``unit_of_measurement``/``temperature_unit``). Returns
    FAHRENHEIT/CELSIUS for a recognised unit that differs from ``configured``; None when
    ``attr`` is None, unrecognised (cannot know) or equal to ``configured``.
    """
    reported = unit_key_from_attr(attr)
    if reported is None or reported == configured:
        return None
    return reported


def ha_system_unit(hass: object) -> str | None:
    """Return Home Assistant's system temperature unit as FAHRENHEIT/CELSIUS (Issue #1023).

    None when ``hass.config.units.temperature_unit`` is unreadable or unrecognised.
    """
    return unit_key_from_attr(getattr(getattr(getattr(hass, "config", None), "units", None), "temperature_unit", None))


def to_fahrenheit(value: float, unit: str) -> float:
    """Convert a temperature value to the internal Fahrenheit canonical unit.

    Passthrough for fahrenheit, C→F for celsius.
    Unknown units are treated as fahrenheit (passthrough).

    Raises ValueError if ``value`` is None — every legitimate caller must resolve a
    concrete fallback before calling (Issue #903: a caller that let None reach here
    hit an unguarded ``None * 9.0`` in celsius mode, crashing the entire coordinator
    update cycle with a cryptic ``unsupported operand type(s)`` error instead of a
    clear one).
    """
    if value is None:
        raise ValueError("to_fahrenheit() received None — caller must resolve a fallback before calling")
    if unit == CELSIUS:
        return value * 9.0 / 5.0 + 32.0
    return float(value)


def from_fahrenheit(value: float, unit: str) -> float:
    """Convert a temperature from internal Fahrenheit to the display unit.

    Passthrough for fahrenheit, F→C for celsius.
    Unknown units are treated as fahrenheit (passthrough).

    Raises ValueError if ``value`` is None — see ``to_fahrenheit()``'s docstring.
    """
    if value is None:
        raise ValueError("from_fahrenheit() received None — caller must resolve a fallback before calling")
    if unit == CELSIUS:
        return (value - 32.0) * 5.0 / 9.0
    return float(value)


def read_state_temp_f(state, attr: str, unit: str) -> float | None:
    """Read a numeric temperature attribute off an HA state object, converting it to
    internal Fahrenheit.

    This is the single sanctioned way to read a temperature attribute directly off an
    HA entity state (a climate entity's "temperature"/"current_temperature" attribute,
    etc.) — it pairs the read with the mandatory to_fahrenheit() conversion in one
    place so a future call site cannot silently skip it (Issue #903 found six
    independent call sites across the codebase that had done exactly that, each
    "accidentally correct" in fahrenheit-configured installs and silently or loudly
    wrong only in celsius-configured ones).

    Returns None if the state is missing, the attribute is absent/None, the value
    isn't numeric, or it isn't finite — callers decide the fallback. Thin delegate over
    ``read_attribute_temp_f`` (Issue #1033) so there is one implementation; use that
    function directly when the failure *reason* is needed.
    """
    return read_attribute_temp_f(state, attr, unit)[0]


def read_attribute_temp_f(state, attr: str, unit: str) -> tuple[float | None, str | None]:
    """Read a temperature attribute off an HA state as °F, with the failure reason (Issue #1033).

    Returns ``(value_f, None)`` on success, else ``(None, reason)`` with ``reason`` one of
    ``"entity_missing"`` (state is None), ``"no_reading"`` (attribute absent or None),
    ``"non_numeric"``, ``"non_finite"`` (``nan``/``inf``). Attribute-reading sibling of
    ``read_sensor_state_f``.
    """
    if state is None:
        return None, "entity_missing"
    raw = state.attributes.get(attr)
    if raw is None:
        return None, "no_reading"
    try:
        parsed = float(raw)
    except (ValueError, TypeError):
        return None, "non_numeric"
    if not math.isfinite(parsed):
        return None, "non_finite"
    value_f = to_fahrenheit(parsed, unit)
    if not math.isfinite(value_f):
        return None, "non_finite"
    return value_f, None


def is_sensor_stale(state, stale_hours: float, now: datetime) -> bool:
    """True when a sensor state has not been reported for more than ``stale_hours`` (Issues #1032/#1033).

    Age comes from ``last_reported`` (updates on every report even when the value is unchanged;
    absent on older HA) falling back to ``last_updated``. Fails open (returns False, i.e.
    treated as fresh) when neither is a real ``datetime`` or ``now`` is not comparable with it
    (stub environments), so a missing timestamp can never mark a healthy sensor failed.
    """
    last_seen = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    if not isinstance(last_seen, datetime):
        return False
    try:
        return now - last_seen > timedelta(hours=stale_hours)
    except TypeError:
        return False


class EpisodeStep(NamedTuple):
    """Result of ``next_fallback_episode``: what to announce and the state to store."""

    kind: str  # "none" | "fallback" | "changed" | "recovered"
    announced: str | None
    since: datetime | None
    minutes_down: int | None


def next_fallback_episode(
    reason: str | None, announced: str | None, since: datetime | None, now: datetime
) -> EpisodeStep:
    """Pure once-per-episode transition for a sensor failure/recovery announcement (Issue #1033).

    ``reason`` is the current failure reason (None = healthy), ``announced`` the reason last
    announced (None = nothing outstanding), ``since`` when the outstanding episode began.
    ``kind``: ``"none"`` (no change — nothing to announce), ``"fallback"`` (None→reason),
    ``"changed"`` (reason A→B), ``"recovered"`` (reason→None, ``minutes_down`` filled when
    ``since`` is known). The caller owns logging, event emission and storing the returned state.
    Shared by the outdoor (#1032) and indoor (#1033) sensor trackers.
    """
    if reason == announced:
        return EpisodeStep("none", announced, since, None)
    if reason is not None:
        if announced is None:
            return EpisodeStep("fallback", reason, now, None)
        return EpisodeStep("changed", reason, since, None)
    minutes = round((now - since).total_seconds() / 60) if since else None
    return EpisodeStep("recovered", None, None, minutes)


def read_sensor_state_f(state, unit: str) -> tuple[float | None, str | None]:
    """Read a temperature sensor/input_number entity's *state* as internal Fahrenheit (Issue #1032).

    Sibling of ``read_state_temp_f`` (which reads an *attribute*). Single place that parses a
    dedicated-sensor reading so ``_get_outdoor_temp`` and the live outdoor-sensor listener
    cannot drift apart again. Returns ``(value_f, None)`` on success, else ``(None, reason)``
    with ``reason`` one of ``"entity_missing"``, ``"unavailable"``, ``"non_numeric"``,
    ``"non_finite"`` — callers own the fallback and the logging.

    ``nan``/``inf`` parse as valid floats, so they are rejected explicitly: otherwise they flow
    into every comparison (all False for nan) and silently disable the windows gate.
    """
    if state is None:
        return None, "entity_missing"
    raw = getattr(state, "state", None)
    if raw in ("unavailable", "unknown"):
        return None, "unavailable"
    try:
        parsed = float(raw)
    except (ValueError, TypeError):
        return None, "non_numeric"
    if not math.isfinite(parsed):
        return None, "non_finite"
    value_f = to_fahrenheit(parsed, unit)
    if not math.isfinite(value_f):
        return None, "non_finite"
    return value_f, None


def format_temp(value_fahrenheit: float, unit: str, decimals: int = 0) -> str:
    """Format an internal Fahrenheit temperature for display in the user's unit.

    Examples:
        format_temp(72.0, FAHRENHEIT)     → "72°F"
        format_temp(72.0, CELSIUS)        → "22°C"
        format_temp(72.5, CELSIUS, 1)     → "22.5°C"
        format_temp(85.0, CELSIUS)        → "29°C"
    """
    display_value = from_fahrenheit(value_fahrenheit, unit)
    symbol = UNIT_SYMBOL.get(unit, "°F")
    return f"{display_value:.{decimals}f}{symbol}"


def format_temp_delta(delta_fahrenheit: float, unit: str, decimals: int = 0) -> str:
    """Format a temperature *difference* for display in the user's unit.

    Unlike format_temp, this applies scale conversion only (no +32/−32 offset),
    because deltas are scale-only transformations.

    Examples:
        format_temp_delta(10.0, FAHRENHEIT)   → "10°F"
        format_temp_delta(9.0, CELSIUS)       → "5°C"
        format_temp_delta(5.0, CELSIUS)       → "3°C"
        format_temp_delta(0.0, CELSIUS)       → "0°C"
    """
    delta = delta_fahrenheit * 5.0 / 9.0 if unit == CELSIUS else float(delta_fahrenheit)
    symbol = UNIT_SYMBOL.get(unit, "°F")
    return f"{delta:.{decimals}f}{symbol}"


def free_cooling_direction_ok(outdoor_temp: float | None, indoor_temp: float | None) -> bool:
    """True if outdoor air is actually cooler than indoor — the precondition for any
    window/fan cooling advice or economizer/nat-vent action. Mirrors the direction
    guard already enforced in automation.py's economizer and nat-vent gates (Issue #327).
    Unknown readings default to True (caller decides whether to act on an unknown).
    """
    return indoor_temp is None or outdoor_temp is None or outdoor_temp < indoor_temp


def find_temperature_crossing(
    indoor_curve: list[dict] | None,
    outdoor_curve: list[dict] | None,
    comparator: Callable[[datetime, float, float], bool],
    after: datetime | None = None,
) -> datetime | None:
    """Find the first timestamp where comparator(ts, outdoor_temp, indoor_temp) is True.

    Each curve is a list of {"ts": ISO-8601 string, "temp": float} entries (the shape
    both _build_predicted_indoor_future() and _build_future_forecast_outdoor() in
    coordinator.py already produce). Aligns the two curves by matching ISO timestamps
    — not list position — so an hour present in only one curve is skipped rather than
    silently mismatched against a neighboring hour from the other curve. Curves may
    differ in length, start time, or filtering boundary; alignment is by timestamp
    value only (Issue #528 — replaces the zip()-by-index pairing that produced
    implausible warm-day window-close/reopen times whenever the two curves drifted).

    `ts` is passed to the comparator, not just the two temperatures, because some
    crossing conditions depend on time-of-day (e.g. a sleep-window-aware gate) and
    not on temperature alone.

    `after`, if given, restricts the scan to timestamps strictly after it — lets a
    "first crossing following an earlier crossing" scan reuse this same function.

    Returns None if either curve is empty/None or no crossing is found.
    """
    if not indoor_curve or not outdoor_curve:
        return None

    outdoor_by_ts: dict[datetime, float] = {}
    for entry in outdoor_curve:
        ts_str = entry.get("ts")
        temp = entry.get("temp")
        if ts_str is None or temp is None:
            continue
        try:
            outdoor_by_ts[datetime.fromisoformat(ts_str)] = float(temp)
        except (ValueError, TypeError):
            continue

    for entry in indoor_curve:
        ts_str = entry.get("ts")
        indoor_temp = entry.get("temp")
        if ts_str is None or indoor_temp is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        if after is not None and ts <= after:
            continue
        outdoor_temp = outdoor_by_ts.get(ts)
        if outdoor_temp is None:
            continue
        if comparator(ts, outdoor_temp, float(indoor_temp)):
            return ts
    return None


def convert_delta(value_fahrenheit: float, unit: str) -> float:
    """Convert a temperature delta from °F to the display unit (scale only, no offset).

    Unlike convert_temp, this applies scale conversion only — appropriate for
    rates (°F/hr → °C/hr) and differences where the +32/-32 offset does not apply.

    Examples:
        convert_delta(9.0, FAHRENHEIT)  → 9.0
        convert_delta(9.0, CELSIUS)     → 5.0
        convert_delta(0.0, CELSIUS)     → 0.0

    Raises ValueError if ``value_fahrenheit`` is None — see ``to_fahrenheit()``'s
    docstring.
    """
    if value_fahrenheit is None:
        raise ValueError("convert_delta() received None — caller must resolve a fallback before calling")
    if unit == CELSIUS:
        return value_fahrenheit * 5.0 / 9.0
    return float(value_fahrenheit)
