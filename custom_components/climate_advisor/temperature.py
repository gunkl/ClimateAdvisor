"""Temperature unit utilities for Climate Advisor.

All internal temperatures are stored and calculated in Fahrenheit.
This module provides the only conversion boundary used throughout the integration.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

FAHRENHEIT = "fahrenheit"
CELSIUS = "celsius"

UNIT_SYMBOL: dict[str, str] = {
    FAHRENHEIT: "°F",
    CELSIUS: "°C",
}


def to_fahrenheit(value: float, unit: str) -> float:
    """Convert a temperature value to the internal Fahrenheit canonical unit.

    Passthrough for fahrenheit, C→F for celsius.
    Unknown units are treated as fahrenheit (passthrough).
    """
    if unit == CELSIUS:
        return value * 9.0 / 5.0 + 32.0
    return float(value)


def from_fahrenheit(value: float, unit: str) -> float:
    """Convert a temperature from internal Fahrenheit to the display unit.

    Passthrough for fahrenheit, F→C for celsius.
    Unknown units are treated as fahrenheit (passthrough).
    """
    if unit == CELSIUS:
        return (value - 32.0) * 5.0 / 9.0
    return float(value)


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
    """
    if unit == CELSIUS:
        return value_fahrenheit * 5.0 / 9.0
    return float(value_fahrenheit)
