"""temperature.py: unit parsing, sensor reads, episode rules and the outside-air direction rule (Silver coverage).

Occupant view: a Celsius house sees the right numbers, a broken or stale sensor is reported once (not every cycle)
instead of silently steering the HVAC, and window advice never tells the person to open windows when outside air
would push the room the wrong way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.temperature import (
    CELSIUS,
    FAHRENHEIT,
    convert_delta,
    find_temperature_crossing,
    format_temp,
    format_temp_delta,
    free_cooling_direction_ok,
    from_fahrenheit,
    ha_system_unit,
    is_sensor_stale,
    next_fallback_episode,
    outdoor_air_helps,
    outdoor_direction_ok,
    outdoor_direction_ok_for_thermostat,
    read_attribute_temp_f,
    read_sensor_state_f,
    reminder_due,
    unit_key_from_attr,
    unit_mismatch,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class TestUnitParsing:
    @pytest.mark.parametrize(
        ("attr", "expected"),
        [("°F", FAHRENHEIT), (" c ", CELSIUS), ("Celsius", CELSIUS), ("kelvin", None), (None, None)],
    )
    def test_unit_key_from_attr(self, attr, expected):
        assert unit_key_from_attr(attr) == expected

    def test_unit_mismatch(self):
        assert unit_mismatch("°C", FAHRENHEIT) == CELSIUS
        assert unit_mismatch("°F", FAHRENHEIT) is None
        assert unit_mismatch("bogus", FAHRENHEIT) is None
        assert unit_mismatch(None, CELSIUS) is None

    def test_ha_system_unit(self):
        hass = SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")))
        assert ha_system_unit(hass) == CELSIUS
        assert ha_system_unit(object()) is None

    def test_none_conversions_raise(self):
        for fn in (from_fahrenheit, convert_delta):
            with pytest.raises(ValueError):
                fn(None, CELSIUS)


class TestReads:
    def test_attribute_reasons(self):
        st = SimpleNamespace(attributes={"t": "abc", "n": "nan", "big": 1e308, "ok": 20})
        assert read_attribute_temp_f(None, "t", FAHRENHEIT) == (None, "entity_missing")
        assert read_attribute_temp_f(st, "absent", FAHRENHEIT) == (None, "no_reading")
        assert read_attribute_temp_f(st, "t", FAHRENHEIT) == (None, "non_numeric")
        assert read_attribute_temp_f(st, "n", FAHRENHEIT) == (None, "non_finite")
        assert read_attribute_temp_f(st, "big", CELSIUS) == (None, "non_finite")  # overflows to inf on C->F
        assert read_attribute_temp_f(st, "ok", CELSIUS) == (68.0, None)

    @pytest.mark.parametrize(
        ("raw", "unit", "expected"),
        [
            ("unavailable", FAHRENHEIT, (None, "unavailable")),
            ("hot", FAHRENHEIT, (None, "non_numeric")),
            (None, FAHRENHEIT, (None, "non_numeric")),
            ("inf", FAHRENHEIT, (None, "non_finite")),
            ("1e308", CELSIUS, (None, "non_finite")),
            ("21.5", CELSIUS, (pytest.approx(70.7), None)),
        ],
    )
    def test_sensor_state(self, raw, unit, expected):
        assert read_sensor_state_f(SimpleNamespace(state=raw), unit) == expected

    def test_sensor_state_missing_entity(self):
        assert read_sensor_state_f(None, FAHRENHEIT) == (None, "entity_missing")


class TestStaleness:
    def test_last_reported_preferred_and_compared(self):
        st = SimpleNamespace(last_reported=NOW - timedelta(hours=3), last_updated=NOW)
        assert is_sensor_stale(st, 2, NOW) is True
        assert is_sensor_stale(st, 4, NOW) is False

    def test_falls_back_to_last_updated(self):
        st = SimpleNamespace(last_updated=NOW - timedelta(hours=5))
        assert is_sensor_stale(st, 2, NOW) is True

    def test_no_timestamp_fails_open(self):
        assert is_sensor_stale(SimpleNamespace(last_updated="yesterday"), 2, NOW) is False

    def test_naive_aware_mix_fails_open(self):
        st = SimpleNamespace(last_reported=datetime(2026, 1, 1))
        assert is_sensor_stale(st, 2, NOW) is False


class TestEpisodesAndReminders:
    def test_no_change(self):
        assert next_fallback_episode("x", "x", NOW, NOW).kind == "none"

    def test_fallback_starts_episode(self):
        step = next_fallback_episode("unavailable", None, None, NOW)
        assert (step.kind, step.announced, step.since) == ("fallback", "unavailable", NOW)

    def test_reason_changes_keeps_since(self):
        since = NOW - timedelta(minutes=5)
        step = next_fallback_episode("non_numeric", "unavailable", since, NOW)
        assert (step.kind, step.announced, step.since) == ("changed", "non_numeric", since)

    def test_recovered_reports_minutes_down(self):
        step = next_fallback_episode(None, "unavailable", NOW - timedelta(minutes=42), NOW)
        assert (step.kind, step.announced, step.since, step.minutes_down) == ("recovered", None, None, 42)

    def test_recovered_without_since_has_no_minutes(self):
        assert next_fallback_episode(None, "unavailable", None, NOW).minutes_down is None

    def test_reminder_due(self):
        assert reminder_due(None, NOW, 60) is True
        assert reminder_due(NOW - timedelta(seconds=30), NOW, 60) is False
        assert reminder_due(NOW - timedelta(seconds=60), NOW, 60) is True


class TestFormatting:
    def test_delta_localized_and_english(self):
        assert format_temp_delta(9.0, CELSIUS, 1) == "5.0°C"
        assert format_temp_delta(4.5, CELSIUS, 1, lang="fr") == "2,5°C"
        assert format_temp(72.5, CELSIUS, 1, lang="de") == "22,5°C"


class TestDirection:
    def test_free_cooling_direction(self):
        assert free_cooling_direction_ok(None, 75) is True
        assert free_cooling_direction_ok(70, 75) is True
        assert free_cooling_direction_ok(80, 75) is False

    def test_unknown_reading_does_not_veto(self):
        assert outdoor_direction_ok(None, 70, 68, 76) is True
        assert outdoor_direction_ok(60, None, 68, 76) is True

    def test_hot_house_needs_cooler_air(self):
        assert outdoor_direction_ok(70, 80, 68, 76) is True
        assert outdoor_direction_ok(85, 80, 68, 76) is False

    def test_cold_house_needs_warmer_air(self):
        assert outdoor_direction_ok(70, 65, 68, 76) is True
        assert outdoor_direction_ok(60, 65, 68, 76) is False

    def test_in_band_margins(self):
        assert outdoor_direction_ok(60, 69, 68, 76) is True  # no margin: band rule
        assert outdoor_direction_ok(60, 69, 68, 76, heat_margin=1.5) is False  # cooler air only starts the heat
        assert outdoor_direction_ok(80, 75, 68, 76, cool_margin=1.5) is False  # warmer air only starts the AC
        assert outdoor_direction_ok(80, 72, 68, 76, cool_margin=1.5) is True

    def test_thermostat_margin_only_on_held_side(self):
        kw = {"swing_heat_f": 1.5, "swing_cool_f": 1.5}
        assert outdoor_direction_ok_for_thermostat(60, 69, 68, 76, thermostat_mode="heat", **kw) is False
        assert outdoor_direction_ok_for_thermostat(60, 69, 68, 76, thermostat_mode="cool", **kw) is True
        assert outdoor_direction_ok_for_thermostat(80, 75, 68, 76, thermostat_mode="cool", **kw) is False
        assert outdoor_direction_ok_for_thermostat(80, 75, 68, 76, thermostat_mode="off", **kw) is True


class TestOutdoorAirHelps:
    def test_missing_or_estimated_says_nothing(self):
        assert outdoor_air_helps(None, 80, 68, 76) is None
        assert outdoor_air_helps(55, None, 68, 76) is None
        assert outdoor_air_helps(55, 80, 68, 76, low_is_estimated=True) is None

    def test_range_overlap(self):
        assert outdoor_air_helps(55, 80, 68, 76, delta=3) is True
        assert outdoor_air_helps(75, 80, 68, 76, delta=3) is False  # never cool enough
        assert outdoor_air_helps(55, 60, 68, 76, delta=3) is False  # never warm enough

    def test_hysteresis_keeps_recommended_day(self):
        assert outdoor_air_helps(55, 67, 68, 76, delta=3, hysteresis=2) is False
        assert outdoor_air_helps(55, 67, 68, 76, delta=3, hysteresis=2, was_helping=True) is True


def _pt(hour: int, temp) -> dict:
    return {"ts": datetime(2026, 7, 1, hour, tzinfo=UTC).isoformat(), "temp": temp}


class TestFindTemperatureCrossing:
    @staticmethod
    def _cooler(_ts, outdoor, indoor):
        return outdoor < indoor

    def test_empty_curves(self):
        assert find_temperature_crossing([], [_pt(1, 60)], self._cooler) is None
        assert find_temperature_crossing([_pt(1, 70)], None, self._cooler) is None

    def test_aligns_by_timestamp_and_skips_bad_entries(self):
        indoor = [
            {"ts": None, "temp": 70},
            {"ts": "garbage", "temp": 70},
            _pt(1, None),
            _pt(2, 70),  # no outdoor at 02:00
            _pt(3, 70),  # outdoor warmer
            _pt(4, 70),
        ]
        outdoor = [
            {"ts": None, "temp": 60},
            {"ts": "garbage", "temp": 60},
            _pt(1, None),
            _pt(3, 80),
            _pt(4, 65),
            _pt(5, 50),
        ]
        hit = find_temperature_crossing(indoor, outdoor, self._cooler)
        assert hit == datetime(2026, 7, 1, 4, tzinfo=UTC)

    def test_after_skips_earlier_hours(self):
        indoor = [_pt(1, 70), _pt(2, 70)]
        outdoor = [_pt(1, 60), _pt(2, 60)]
        after = datetime(2026, 7, 1, 1, tzinfo=UTC)
        assert find_temperature_crossing(indoor, outdoor, self._cooler, after=after) == datetime(
            2026, 7, 1, 2, tzinfo=UTC
        )

    def test_no_crossing(self):
        assert find_temperature_crossing([_pt(1, 70)], [_pt(1, 80)], self._cooler) is None
