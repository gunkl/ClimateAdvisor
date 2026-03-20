"""Tests for temperature prediction logic (chart data computation)."""
from __future__ import annotations

import pytest
from datetime import date, time
from unittest.mock import patch, MagicMock

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.coordinator import (
    compute_predicted_temps,
    _build_outdoor_curve,
    _cosine_outdoor_curve,
)


def _make_classification(day_type="warm", hvac_mode="off", **kwargs):
    """Create a DayClassification for testing (bypass __post_init__)."""
    defaults = dict(
        day_type=day_type,
        trend_direction="stable",
        trend_magnitude=1.0,
        today_high=78.0,
        today_low=58.0,
        tomorrow_high=79.0,
        tomorrow_low=59.0,
        hvac_mode=hvac_mode,
        windows_recommended=(hvac_mode == "off"),
        window_open_time=time(8, 0) if hvac_mode == "off" else None,
        window_close_time=time(18, 0) if hvac_mode == "off" else None,
        setback_modifier=0.0,
        pre_condition=False,
        pre_condition_target=None,
    )
    defaults.update(kwargs)
    c = object.__new__(DayClassification)
    for k, v in defaults.items():
        object.__setattr__(c, k, v)
    return c


DEFAULT_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}


class TestSinusoidalOutdoorPrediction:
    """Test the sinusoidal outdoor temperature interpolation."""

    def test_peak_near_3pm(self):
        c = _make_classification(today_high=90.0, today_low=60.0)
        outdoor, _ = compute_predicted_temps(c, DEFAULT_CONFIG)
        assert len(outdoor) == 24
        peak = max(outdoor, key=lambda p: p["temp"])
        assert 13 <= peak["hour"] <= 17, f"Peak at hour {peak['hour']}"

    def test_trough_near_3am(self):
        """Trough is 12 hours opposite the peak (h=3, i.e. 3 AM)."""
        c = _make_classification(today_high=90.0, today_low=60.0)
        outdoor, _ = compute_predicted_temps(c, DEFAULT_CONFIG)
        trough = min(outdoor, key=lambda p: p["temp"])
        assert 1 <= trough["hour"] <= 5, f"Trough at hour {trough['hour']}"

    def test_temp_range_matches_forecast(self):
        c = _make_classification(today_high=85.0, today_low=55.0)
        outdoor, _ = compute_predicted_temps(c, DEFAULT_CONFIG)
        temps = [p["temp"] for p in outdoor]
        assert max(temps) == pytest.approx(85.0, abs=1.0)
        assert min(temps) == pytest.approx(55.0, abs=1.0)

    def test_24_data_points(self):
        c = _make_classification()
        outdoor, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        assert len(outdoor) == 24
        assert len(indoor) == 24


class TestIndoorPredictionHeating:
    """Test indoor prediction in heating mode."""

    def test_overnight_at_setback(self):
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hours 0-5 should be at setback_heat (60)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(60.0, abs=0.5), \
                f"Hour {h}: expected ~60, got {indoor[h]['temp']}"

    def test_daytime_at_comfort(self):
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hours 8-21 should be at comfort_heat (70)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(70.0, abs=1.0), \
                f"Hour {h}: expected ~70, got {indoor[h]['temp']}"

    def test_bedtime_setback_applied(self):
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hour 23: bedtime setback = comfort_heat - 4 = 66
        assert indoor[23]["temp"] == pytest.approx(66.0, abs=1.0)


class TestIndoorPredictionCooling:
    """Test indoor prediction in cooling mode."""

    def test_overnight_at_cool_setback(self):
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight should be setback_cool (80)
        assert indoor[3]["temp"] == pytest.approx(80.0, abs=0.5)

    def test_daytime_at_comfort_cool(self):
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Daytime should be comfort_cool (75)
        assert indoor[12]["temp"] == pytest.approx(75.0, abs=1.0)

    def test_bedtime_setback_cool(self):
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Bedtime setback for cool: comfort_cool + 3 = 78
        assert indoor[23]["temp"] == pytest.approx(78.0, abs=1.0)


class TestNoClassification:
    """Test behavior when there's no classification."""

    def test_empty_when_no_classification(self):
        outdoor, indoor = compute_predicted_temps(None, DEFAULT_CONFIG)
        assert outdoor == []
        assert indoor == []


class TestSetbackModifier:
    """Test that setback modifier is applied correctly."""

    def test_positive_modifier_raises_setback(self):
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 setback_modifier=3.0,
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight setback = 60 + 3 = 63
        assert indoor[3]["temp"] == pytest.approx(63.0, abs=0.5)

    def test_negative_modifier_lowers_setback(self):
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 setback_modifier=-3.0,
                                 windows_recommended=False,
                                 window_open_time=None,
                                 window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight setback = 60 - 3 = 57
        assert indoor[3]["temp"] == pytest.approx(57.0, abs=0.5)


class TestIndoorPredictionHvacOff:
    """Test indoor prediction drift logic when HVAC mode is off."""

    def test_drift_toward_outdoor_windows_open(self):
        """With windows open during window hours, indoor drifts toward outdoor at rate 3.0."""
        # Warm day: outdoor will be above comfort_cool (75) during midday
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=90.0, today_low=60.0,
            windows_recommended=True, window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        outdoor, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # At h=12 (midday, within window hours), outdoor is well above 75
        # Indoor should drift above comfort (75) toward outdoor
        assert indoor[12]["temp"] > 75.0, \
            f"Indoor at h=12 should drift above comfort, got {indoor[12]['temp']}"

    def test_drift_toward_outdoor_without_windows(self):
        """Without windows recommended, drift rate is 1.5 (slower)."""
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=90.0, today_low=60.0,
            windows_recommended=False, window_open_time=None,
            window_close_time=None,
        )
        _, indoor_no_win = compute_predicted_temps(c, DEFAULT_CONFIG)

        c_win = _make_classification(
            day_type="warm", hvac_mode="off", today_high=90.0, today_low=60.0,
            windows_recommended=True, window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        _, indoor_win = compute_predicted_temps(c_win, DEFAULT_CONFIG)

        # At h=12 (within window hours), windows-open drift (3.0) should produce
        # a larger deviation from comfort than no-windows drift (1.5)
        comfort = 75.0
        drift_win = abs(indoor_win[12]["temp"] - comfort)
        drift_no_win = abs(indoor_no_win[12]["temp"] - comfort)
        assert drift_no_win < drift_win, \
            f"No-windows drift ({drift_no_win}) should be less than windows drift ({drift_win})"

    def test_drift_limited_by_rate(self):
        """Drift per hour is capped at drift_rate, even with huge outdoor-indoor delta."""
        # Extreme outdoor: today_high=120, comfort_cool=75 → diff=45
        # drift_rate=3.0 (windows open), so drift should be exactly 3.0
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=120.0, today_low=100.0,
            windows_recommended=True, window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # At any window hour, indoor = comfort + min(abs(diff), 3.0) = 75 + 3 = 78
        comfort = 75.0
        for h in range(8, 18):
            assert indoor[h]["temp"] == pytest.approx(comfort + 3.0, abs=0.1), \
                f"Hour {h}: expected {comfort + 3.0}, got {indoor[h]['temp']}"

    def test_drift_direction_when_outdoor_cooler(self):
        """When outdoor < comfort, indoor drifts below comfort."""
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=65.0, today_low=50.0,
            windows_recommended=True, window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Outdoor at midday ~65, comfort_cool=75 → outdoor < comfort
        # Indoor should drift below comfort
        assert indoor[12]["temp"] < 75.0, \
            f"Indoor should drift below comfort when outdoor is cooler, got {indoor[12]['temp']}"

    def test_overnight_still_at_setback_hvac_off(self):
        """Even in HVAC-off mode, hours before wake should be at setback (not drifting)."""
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=90.0, today_low=60.0,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hours 0-5 are before wake_h (6.5), so setback applies (setback_cool=80)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(80.0, abs=0.5), \
                f"Hour {h}: overnight should be at setback (80), got {indoor[h]['temp']}"

    def test_no_fast_drift_outside_window_hours(self):
        """Before window_open_time, drift rate should be 1.5 (slow), not 3.0."""
        # Window opens at 10:00, so h=8 and h=9 are outside window hours
        c = _make_classification(
            day_type="warm", hvac_mode="off", today_high=120.0, today_low=100.0,
            windows_recommended=True, window_open_time=time(10, 0),
            window_close_time=time(18, 0),
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        comfort = 75.0
        # h=8: before window open → drift rate 1.5, so indoor = 75 + 1.5 = 76.5
        assert indoor[8]["temp"] == pytest.approx(comfort + 1.5, abs=0.1), \
            f"Hour 8 (before window): expected {comfort + 1.5}, got {indoor[8]['temp']}"
        # h=12: within window → drift rate 3.0, so indoor = 75 + 3.0 = 78.0
        assert indoor[12]["temp"] == pytest.approx(comfort + 3.0, abs=0.1), \
            f"Hour 12 (during window): expected {comfort + 3.0}, got {indoor[12]['temp']}"


class TestRampTransitions:
    """Test wake-up and bedtime ramp transitions.

    Since compute_predicted_temps produces hourly (integer h) data points,
    ramps are only visible when wake/sleep times fall at exact hours so
    an integer h lands within the 30-minute ramp window [time, time+0.5).
    """

    def test_wake_ramp_start(self):
        """At wake_time="07:00", h=7 is ramp start (frac=0 → setback)."""
        cfg = {**DEFAULT_CONFIG, "wake_time": "07:00"}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        # h=7: frac = (7 - 7.0) / 0.5 = 0 → temp = setback + 0 = 60
        assert indoor[7]["temp"] == pytest.approx(60.0, abs=0.5)
        # h=6: still before wake → setback
        assert indoor[6]["temp"] == pytest.approx(60.0, abs=0.5)
        # h=8: past ramp (7.5) → comfort
        assert indoor[8]["temp"] == pytest.approx(70.0, abs=0.5)

    def test_bedtime_ramp_start(self):
        """At sleep_time="22:00", h=22 is ramp start (frac=0 → comfort)."""
        cfg = {**DEFAULT_CONFIG, "sleep_time": "22:00"}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        # h=22: frac = 0 → temp = comfort + 0*(bedtime - comfort) = comfort = 70
        assert indoor[22]["temp"] == pytest.approx(70.0, abs=0.5)
        # h=23: past ramp (22.5) → bedtime setback = 70 - 4 = 66
        assert indoor[23]["temp"] == pytest.approx(66.0, abs=1.0)

    def test_bedtime_setback_heat_formula(self):
        """Heat mode bedtime setback = comfort_heat - 4 + setback_modifier."""
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 setback_modifier=2.0,
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Bedtime setback = 70 - 4 + 2 = 68
        assert indoor[23]["temp"] == pytest.approx(68.0, abs=1.0)

    def test_bedtime_setback_cool_formula(self):
        """Cool mode bedtime setback = comfort_cool + 3 (no modifier)."""
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 setback_modifier=2.0,
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Bedtime setback = 75 + 3 = 78 (modifier not applied in cool mode)
        assert indoor[23]["temp"] == pytest.approx(78.0, abs=1.0)

    def test_default_wake_no_ramp_visible(self):
        """With default wake_time=06:30, no integer hour falls in ramp [6.5, 7.0)."""
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # h=6: before 6.5 → setback (60)
        assert indoor[6]["temp"] == pytest.approx(60.0, abs=0.5)
        # h=7: after 7.0 → comfort (70) — no ramp value visible
        assert indoor[7]["temp"] == pytest.approx(70.0, abs=0.5)


class TestCustomConfig:
    """Test that custom config values are correctly reflected in predictions."""

    def test_custom_comfort_heat(self):
        cfg = {**DEFAULT_CONFIG, "comfort_heat": 72}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(72.0, abs=1.0), \
                f"Hour {h}: expected ~72, got {indoor[h]['temp']}"

    def test_custom_comfort_cool(self):
        cfg = {**DEFAULT_CONFIG, "comfort_cool": 78}
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(78.0, abs=1.0), \
                f"Hour {h}: expected ~78, got {indoor[h]['temp']}"

    def test_custom_setback_heat(self):
        cfg = {**DEFAULT_CONFIG, "setback_heat": 55}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(55.0, abs=0.5), \
                f"Hour {h}: expected ~55, got {indoor[h]['temp']}"

    def test_custom_setback_cool(self):
        cfg = {**DEFAULT_CONFIG, "setback_cool": 82}
        c = _make_classification(day_type="hot", hvac_mode="cool",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(82.0, abs=0.5), \
                f"Hour {h}: expected ~82, got {indoor[h]['temp']}"

    def test_custom_wake_time(self):
        cfg = {**DEFAULT_CONFIG, "wake_time": "08:00"}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        # Hour 7 is before wake at 8:00 — should be at setback (60)
        assert indoor[7]["temp"] == pytest.approx(60.0, abs=0.5)
        # Hour 9 is past wake ramp (8:00 + 0.5 = 8:30) — should be at comfort (70)
        assert indoor[9]["temp"] == pytest.approx(70.0, abs=1.0)

    def test_custom_sleep_time(self):
        cfg = {**DEFAULT_CONFIG, "sleep_time": "23:00"}
        c = _make_classification(day_type="cold", hvac_mode="heat",
                                 windows_recommended=False,
                                 window_open_time=None, window_close_time=None)
        _, indoor = compute_predicted_temps(c, cfg)
        # Hour 22 is before sleep at 23:00 — should still be at comfort (70)
        assert indoor[22]["temp"] == pytest.approx(70.0, abs=1.0)

    def test_extreme_temp_range(self):
        c = _make_classification(today_high=110.0, today_low=30.0)
        outdoor, _ = compute_predicted_temps(c, DEFAULT_CONFIG)
        temps = [p["temp"] for p in outdoor]
        assert max(temps) == pytest.approx(110.0, abs=1.0)
        assert min(temps) == pytest.approx(30.0, abs=1.0)

    def test_narrow_temp_range(self):
        c = _make_classification(today_high=70.0, today_low=68.0)
        outdoor, _ = compute_predicted_temps(c, DEFAULT_CONFIG)
        temps = [p["temp"] for p in outdoor]
        assert max(temps) - min(temps) == pytest.approx(2.0, abs=0.5)


# ---------------------------------------------------------------------------
# Helpers shared by TestHourlyForecastOutdoorPrediction
# ---------------------------------------------------------------------------

_TODAY = date(2026, 3, 19)
_TODAY_STR = "2026-03-19"
_TOMORROW_STR = "2026-03-20"

_HIGH = 85.0
_LOW = 55.0


def _mock_datetime(today: date = _TODAY):
    """Return a MagicMock that makes datetime.now().date() return *today*."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = today
    mock_dt.fromisoformat = __import__("datetime").datetime.fromisoformat
    return mock_dt


def _hourly_entry(hour: int, temp: float, day_str: str = _TODAY_STR) -> dict:
    """Build a single hourly forecast dict for *hour* on *day_str*."""
    return {"datetime": f"{day_str}T{hour:02d}:00:00", "temperature": temp}


def _full_24h_forecast(high: float = _HIGH, low: float = _LOW) -> list[dict]:
    """Return a complete 24-entry hourly forecast for today using the cosine curve."""
    cosine = _cosine_outdoor_curve(high, low)
    return [_hourly_entry(p["hour"], p["temp"]) for p in cosine]


class TestHourlyForecastOutdoorPrediction:
    """Tests for hourly-forecast-based outdoor temperature prediction."""

    # ------------------------------------------------------------------
    # Basic pass-through / fallback
    # ------------------------------------------------------------------

    def test_uses_hourly_temps_when_provided(self):
        """Full 24h hourly data → each hour matches the forecast exactly."""
        # Build a forecast where every hour has a distinct, recognisable value.
        forecast = [_hourly_entry(h, float(h * 2)) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.datetime",
            _mock_datetime(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24
        for entry in result:
            h = entry["hour"]
            assert entry["temp"] == pytest.approx(float(h * 2), abs=0.05), \
                f"Hour {h}: expected {h * 2}, got {entry['temp']}"

    def test_falls_back_to_cosine_when_none(self):
        """hourly_forecast=None → result is identical to _cosine_outdoor_curve."""
        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        result = _build_outdoor_curve(_HIGH, _LOW, None)
        assert result == expected

    def test_falls_back_to_cosine_on_empty_list(self):
        """hourly_forecast=[] → result is identical to _cosine_outdoor_curve."""
        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        result = _build_outdoor_curve(_HIGH, _LOW, [])
        assert result == expected

    # ------------------------------------------------------------------
    # Interpolation
    # ------------------------------------------------------------------

    def test_interpolates_missing_hours(self):
        """Data at hours 0, 6, 12, 18 only → intermediate hours are interpolated."""
        sparse = [
            _hourly_entry(0, 60.0),
            _hourly_entry(6, 66.0),
            _hourly_entry(12, 78.0),
            _hourly_entry(18, 72.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.datetime",
            _mock_datetime(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, sparse)

        by_hour = {p["hour"]: p["temp"] for p in result}
        assert len(result) == 24

        # Hour 3 is midpoint between h=0 (60) and h=6 (66) → 63.0
        assert by_hour[3] == pytest.approx(63.0, abs=0.15)
        # Hour 9 is midpoint between h=6 (66) and h=12 (78) → 72.0
        assert by_hour[9] == pytest.approx(72.0, abs=0.15)

    # ------------------------------------------------------------------
    # Edge-hour cosine fill
    # ------------------------------------------------------------------

    def test_edge_hours_use_cosine_fill(self):
        """Data only for hours 6–18 → hours 0–5 and 19–23 use the cosine model."""
        mid_forecast = [_hourly_entry(h, 70.0) for h in range(6, 19)]
        cosine = {p["hour"]: p["temp"] for p in _cosine_outdoor_curve(_HIGH, _LOW)}

        with patch(
            "custom_components.climate_advisor.coordinator.datetime",
            _mock_datetime(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mid_forecast)

        by_hour = {p["hour"]: p["temp"] for p in result}
        assert len(result) == 24

        for h in list(range(0, 6)) + list(range(19, 24)):
            assert by_hour[h] == pytest.approx(cosine[h], abs=0.05), \
                f"Edge hour {h}: expected cosine {cosine[h]}, got {by_hour[h]}"

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def test_malformed_entries_skipped(self):
        """Entries with missing datetime or temperature are skipped without error."""
        forecast = [
            {"temperature": 70.0},                        # missing datetime
            {"datetime": f"{_TODAY_STR}T10:00:00"},       # missing temperature
            {"datetime": None, "temperature": 72.0},      # None datetime
            {"datetime": f"{_TODAY_STR}T12:00:00", "temperature": 80.0},  # valid
            {"datetime": "not-a-date", "temperature": 65.0},              # bad format
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.datetime",
            _mock_datetime(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24
        # Hour 12 is the only valid known entry → must match exactly.
        by_hour = {p["hour"]: p["temp"] for p in result}
        assert by_hour[12] == pytest.approx(80.0, abs=0.05)

    # ------------------------------------------------------------------
    # Backward compatibility
    # ------------------------------------------------------------------

    def test_backward_compat_two_arg_call(self):
        """compute_predicted_temps(classification, config) still returns 24 points."""
        c = _make_classification(today_high=_HIGH, today_low=_LOW)
        outdoor, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        assert len(outdoor) == 24
        assert len(indoor) == 24

    # ------------------------------------------------------------------
    # Date filtering
    # ------------------------------------------------------------------

    def test_filters_to_today_only(self):
        """Tomorrow's hourly entries are ignored; only today's date is used."""
        today_entries = [_hourly_entry(h, float(100 + h)) for h in range(24)]
        tomorrow_entries = [_hourly_entry(h, float(200 + h), _TOMORROW_STR) for h in range(24)]
        mixed = today_entries + tomorrow_entries

        with patch(
            "custom_components.climate_advisor.coordinator.datetime",
            _mock_datetime(_TODAY),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mixed)

        by_hour = {p["hour"]: p["temp"] for p in result}
        assert len(result) == 24
        # All hours should reflect today's values (100 + h), not tomorrow's (200 + h).
        for h in range(24):
            assert by_hour[h] == pytest.approx(float(100 + h), abs=0.05), \
                f"Hour {h}: expected today's value {100 + h}, got {by_hour[h]}"
