"""Tests for temperature unit conversion utilities."""

from __future__ import annotations

import pytest

from custom_components.climate_advisor.temperature import (
    CELSIUS,
    FAHRENHEIT,
    UNIT_SYMBOL,
    format_temp,
    format_temp_delta,
    from_fahrenheit,
    to_fahrenheit,
)


class TestToFahrenheit:
    """Tests for to_fahrenheit()."""

    def test_freezing_point(self):
        assert to_fahrenheit(0.0, CELSIUS) == pytest.approx(32.0)

    def test_boiling_point(self):
        assert to_fahrenheit(100.0, CELSIUS) == pytest.approx(212.0)

    def test_body_temp(self):
        assert to_fahrenheit(37.0, CELSIUS) == pytest.approx(98.6, abs=0.1)

    def test_typical_comfort_cool(self):
        # 24°C → 75.2°F
        assert to_fahrenheit(24.0, CELSIUS) == pytest.approx(75.2, abs=0.01)

    def test_fahrenheit_passthrough_integer(self):
        assert to_fahrenheit(72.0, FAHRENHEIT) == 72.0

    def test_fahrenheit_passthrough_float(self):
        assert to_fahrenheit(72.5, FAHRENHEIT) == 72.5

    def test_unknown_unit_treated_as_fahrenheit(self):
        # Unknown units should passthrough (not raise)
        assert to_fahrenheit(72.0, "metric") == 72.0

    def test_negative_celsius(self):
        # -40°C == -40°F (the crossover point)
        assert to_fahrenheit(-40.0, CELSIUS) == pytest.approx(-40.0)


class TestFromFahrenheit:
    """Tests for from_fahrenheit()."""

    def test_freezing_point(self):
        assert from_fahrenheit(32.0, CELSIUS) == pytest.approx(0.0)

    def test_boiling_point(self):
        assert from_fahrenheit(212.0, CELSIUS) == pytest.approx(100.0)

    def test_body_temp(self):
        assert from_fahrenheit(98.6, CELSIUS) == pytest.approx(37.0, abs=0.1)

    def test_fahrenheit_passthrough(self):
        assert from_fahrenheit(72.0, FAHRENHEIT) == 72.0

    def test_unknown_unit_treated_as_fahrenheit(self):
        assert from_fahrenheit(72.0, "metric") == 72.0

    def test_roundtrip_celsius(self):
        """Converting to °F and back should yield the original value."""
        original = 22.0
        assert from_fahrenheit(to_fahrenheit(original, CELSIUS), CELSIUS) == pytest.approx(original)

    def test_crossover_point(self):
        # -40°F == -40°C
        assert from_fahrenheit(-40.0, CELSIUS) == pytest.approx(-40.0)

    def test_typical_hot_threshold(self):
        # 85°F → ~29.4°C
        assert from_fahrenheit(85.0, CELSIUS) == pytest.approx(29.4, abs=0.1)


class TestFormatTemp:
    """Tests for format_temp()."""

    def test_fahrenheit_integer(self):
        assert format_temp(72.0, FAHRENHEIT) == "72°F"

    def test_fahrenheit_rounds_to_integer(self):
        assert format_temp(72.6, FAHRENHEIT) == "73°F"

    def test_celsius_integer(self):
        # 72°F ≈ 22.2°C → rounds to 22°C
        assert format_temp(72.0, CELSIUS) == "22°C"

    def test_celsius_with_decimals(self):
        # 72.5°F ≈ 22.5°C
        assert format_temp(72.5, CELSIUS, 1) == "22.5°C"

    def test_hot_threshold(self):
        # 85°F ≈ 29.4°C → rounds to 29°C
        assert format_temp(85.0, CELSIUS) == "29°C"

    def test_comfort_heat_default(self):
        assert format_temp(70.0, FAHRENHEIT) == "70°F"

    def test_comfort_cool_default(self):
        # 75°F ≈ 23.9°C → rounds to 24°C
        assert format_temp(75.0, CELSIUS) == "24°C"

    def test_unknown_unit_defaults_to_fahrenheit_symbol(self):
        # Unknown unit: value passes through, symbol defaults to °F
        assert format_temp(72.0, "unknown") == "72°F"

    def test_zero_decimals_is_default(self):
        assert format_temp(70.0, FAHRENHEIT) == format_temp(70.0, FAHRENHEIT, 0)


class TestFormatTempDelta:
    """Tests for format_temp_delta() — scale-only conversion, no offset."""

    def test_fahrenheit_delta_passthrough(self):
        assert format_temp_delta(10.0, FAHRENHEIT) == "10°F"

    def test_celsius_delta_9f_equals_5c(self):
        assert format_temp_delta(9.0, CELSIUS) == "5°C"

    def test_celsius_delta_5f(self):
        # 5°F × 5/9 ≈ 2.8°C → rounds to 3°C
        assert format_temp_delta(5.0, CELSIUS) == "3°C"

    def test_zero_delta(self):
        assert format_temp_delta(0.0, CELSIUS) == "0°C"
        assert format_temp_delta(0.0, FAHRENHEIT) == "0°F"

    def test_significant_trend_10f(self):
        # 10°F delta → ~5.6°C → rounds to 6°C
        assert format_temp_delta(10.0, CELSIUS) == "6°C"

    def test_no_offset_applied(self):
        """Delta conversion must NOT add the +32/−32 offset that absolute temps use."""
        # If offset were wrongly applied: (9 - 32) * 5/9 = -12.8°C — wrong
        # Correct scale-only: 9 * 5/9 = 5°C
        assert format_temp_delta(9.0, CELSIUS) == "5°C"

    def test_unknown_unit_defaults_to_fahrenheit(self):
        assert format_temp_delta(10.0, "unknown") == "10°F"


class TestUnitSymbols:
    """Tests for UNIT_SYMBOL constants."""

    def test_fahrenheit_symbol(self):
        assert UNIT_SYMBOL[FAHRENHEIT] == "°F"

    def test_celsius_symbol(self):
        assert UNIT_SYMBOL[CELSIUS] == "°C"

    def test_constants_are_strings(self):
        assert isinstance(FAHRENHEIT, str)
        assert isinstance(CELSIUS, str)

    def test_fahrenheit_constant_value(self):
        assert FAHRENHEIT == "fahrenheit"

    def test_celsius_constant_value(self):
        assert CELSIUS == "celsius"
