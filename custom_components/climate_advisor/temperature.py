"""Temperature unit utilities for Climate Advisor.

All internal temperatures are stored and calculated in Fahrenheit.
This module provides the only conversion boundary used throughout the integration.
"""

from __future__ import annotations

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
