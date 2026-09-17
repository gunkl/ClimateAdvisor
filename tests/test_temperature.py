"""Tests for temperature.py's conversion boundary (Issue #903).

Covers:
1. to_fahrenheit()/from_fahrenheit()/convert_delta() raise a clear ValueError on
   None instead of a cryptic operator TypeError — the defense-in-depth guard added
   after a None reaching to_fahrenheit() in celsius mode crashed the entire
   coordinator update cycle with "unsupported operand type(s) for *".
2. read_state_temp_f() — the new single sanctioned way to read a temperature
   attribute off an HA state object and convert it to internal Fahrenheit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.temperature import (
    CELSIUS,
    FAHRENHEIT,
    convert_delta,
    from_fahrenheit,
    read_state_temp_f,
    to_fahrenheit,
)


class TestNoneGuards:
    """None must never reach the arithmetic — it must raise a clear ValueError instead."""

    def test_to_fahrenheit_none_celsius_raises_value_error(self):
        with pytest.raises(ValueError, match="to_fahrenheit"):
            to_fahrenheit(None, CELSIUS)

    def test_to_fahrenheit_none_fahrenheit_raises_value_error(self):
        with pytest.raises(ValueError, match="to_fahrenheit"):
            to_fahrenheit(None, FAHRENHEIT)

    def test_from_fahrenheit_none_celsius_raises_value_error(self):
        with pytest.raises(ValueError, match="from_fahrenheit"):
            from_fahrenheit(None, CELSIUS)

    def test_from_fahrenheit_none_fahrenheit_raises_value_error(self):
        with pytest.raises(ValueError, match="from_fahrenheit"):
            from_fahrenheit(None, FAHRENHEIT)

    def test_convert_delta_none_celsius_raises_value_error(self):
        with pytest.raises(ValueError, match="convert_delta"):
            convert_delta(None, CELSIUS)

    def test_convert_delta_none_fahrenheit_raises_value_error(self):
        with pytest.raises(ValueError, match="convert_delta"):
            convert_delta(None, FAHRENHEIT)


class TestConversionStillCorrect:
    """The None guard must not change behavior for real values."""

    def test_to_fahrenheit_celsius(self):
        assert to_fahrenheit(0.0, CELSIUS) == pytest.approx(32.0)

    def test_to_fahrenheit_fahrenheit_passthrough(self):
        assert to_fahrenheit(72.0, FAHRENHEIT) == pytest.approx(72.0)

    def test_from_fahrenheit_celsius(self):
        assert from_fahrenheit(32.0, CELSIUS) == pytest.approx(0.0)

    def test_convert_delta_celsius(self):
        assert convert_delta(9.0, CELSIUS) == pytest.approx(5.0)


class TestReadStateTempF:
    """read_state_temp_f() — the single sanctioned HA-state temperature read."""

    def test_none_state_returns_none(self):
        assert read_state_temp_f(None, "temperature", FAHRENHEIT) is None

    def test_missing_attribute_returns_none(self):
        state = SimpleNamespace(attributes={})
        assert read_state_temp_f(state, "temperature", FAHRENHEIT) is None

    def test_present_but_null_attribute_returns_none(self):
        state = SimpleNamespace(attributes={"temperature": None})
        assert read_state_temp_f(state, "temperature", CELSIUS) is None

    def test_non_numeric_attribute_returns_none(self):
        state = SimpleNamespace(attributes={"temperature": "unavailable"})
        assert read_state_temp_f(state, "temperature", FAHRENHEIT) is None

    def test_valid_fahrenheit_value_passthrough(self):
        state = SimpleNamespace(attributes={"temperature": 72.0})
        assert read_state_temp_f(state, "temperature", FAHRENHEIT) == pytest.approx(72.0)

    def test_valid_celsius_value_converted_to_internal_fahrenheit(self):
        state = SimpleNamespace(attributes={"temperature": 23.0})
        assert read_state_temp_f(state, "temperature", CELSIUS) == pytest.approx(73.4)

    def test_reads_the_requested_attribute_name(self):
        state = SimpleNamespace(attributes={"current_temperature": 20.0, "temperature": 999.0})
        assert read_state_temp_f(state, "current_temperature", CELSIUS) == pytest.approx(68.0)
