"""Tests for temperature unit conversion utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor.temperature import (
    CELSIUS,
    FAHRENHEIT,
    UNIT_SYMBOL,
    find_temperature_crossing,
    format_temp,
    format_temp_delta,
    free_cooling_direction_ok,
    from_fahrenheit,
    to_fahrenheit,
)


def _curve(temps: list[float], start_hour: int = 8) -> list[dict]:
    base = datetime(2026, 7, 27, start_hour, 0, 0, tzinfo=UTC)
    return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]


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


class TestFreeCoolingDirectionOk:
    """Tests for free_cooling_direction_ok() — the shared free-cooling direction gate (Issue #428)."""

    def test_outdoor_cooler_than_indoor_is_ok(self):
        assert free_cooling_direction_ok(outdoor_temp=70.0, indoor_temp=78.0) is True

    def test_outdoor_hotter_than_indoor_is_not_ok(self):
        """The exact reported scenario: indoor 75, outdoor 80 — free cooling doesn't help."""
        assert free_cooling_direction_ok(outdoor_temp=80.0, indoor_temp=75.0) is False

    def test_outdoor_equal_to_indoor_is_not_ok(self):
        assert free_cooling_direction_ok(outdoor_temp=75.0, indoor_temp=75.0) is False

    def test_outdoor_none_defaults_to_ok(self):
        """Unknown outdoor reading doesn't itself block — the caller decides how to act on unknown."""
        assert free_cooling_direction_ok(outdoor_temp=None, indoor_temp=78.0) is True

    def test_indoor_none_defaults_to_ok(self):
        assert free_cooling_direction_ok(outdoor_temp=70.0, indoor_temp=None) is True


class TestFindTemperatureCrossing:
    """Tests for find_temperature_crossing() (Issue #528)."""

    def test_finds_first_crossing_on_aligned_curves(self):
        indoor = _curve([72.0, 73.0, 74.0, 75.0], start_hour=8)
        outdoor = _curve([65.0, 68.0, 73.0, 76.0], start_hour=8)
        result = find_temperature_crossing(indoor, outdoor, lambda _ts, o, i: o >= i - 1.0)
        assert result is not None
        assert result.hour == 10

    def test_returns_none_when_no_crossing(self):
        indoor = _curve([72.0, 73.0, 74.0], start_hour=8)
        outdoor = _curve([50.0, 51.0, 52.0], start_hour=8)
        assert find_temperature_crossing(indoor, outdoor, lambda _ts, o, i: o >= i) is None

    def test_returns_none_for_empty_or_none_curves(self):
        curve = _curve([72.0], start_hour=8)
        assert find_temperature_crossing(None, curve, lambda _ts, o, i: True) is None
        assert find_temperature_crossing(curve, None, lambda _ts, o, i: True) is None
        assert find_temperature_crossing([], [], lambda _ts, o, i: True) is None

    def test_misaligned_curves_align_by_timestamp_not_index(self):
        """The core Issue #528 regression: curves starting at different hours must be
        paired by matching ISO timestamp, never by list position."""
        indoor = _curve([72.0, 73.0, 74.0, 75.0], start_hour=8)
        outdoor = _curve([73.0, 76.0, 78.0, 80.0], start_hour=10)  # only hours 10-11 overlap
        result = find_temperature_crossing(indoor, outdoor, lambda _ts, o, i: o >= i - 1.0)
        assert result is not None
        assert result.hour == 10  # not 8 — hour 8 has no matching outdoor entry at all

    def test_hour_present_in_only_one_curve_is_skipped(self):
        indoor = _curve([72.0, 999.0], start_hour=8)  # hour 9 would trivially "cross" if matched wrongly
        outdoor = _curve([65.0], start_hour=8)  # only hour 8 present
        result = find_temperature_crossing(indoor, outdoor, lambda _ts, o, i: o >= i - 1.0)
        assert result is None  # hour 8: 65 >= 72-1=71 is False; hour 9 has no outdoor match to test against

    def test_after_parameter_restricts_scan_to_later_timestamps(self):
        indoor = _curve([72.0, 73.0, 74.0, 75.0], start_hour=8)
        outdoor = _curve([73.0, 76.0, 78.0, 80.0], start_hour=8)  # crosses at hour 8 already
        cutoff = datetime(2026, 7, 27, 8, 0, 0, tzinfo=UTC)
        result = find_temperature_crossing(indoor, outdoor, lambda _ts, o, i: o >= i - 1.0, after=cutoff)
        assert result is not None
        assert result.hour == 9  # hour 8 excluded by `after`, even though it also crosses

    def test_comparator_receives_timestamp(self):
        """Comparator must see `ts`, not just the two temperatures, for time-of-day-aware gates."""
        indoor = _curve([72.0, 72.0], start_hour=8)
        outdoor = _curve([72.0, 72.0], start_hour=8)
        result = find_temperature_crossing(indoor, outdoor, lambda ts, _o, _i: ts.hour == 9)
        assert result is not None
        assert result.hour == 9
