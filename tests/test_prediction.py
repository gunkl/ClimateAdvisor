"""Tests for temperature prediction logic (chart data computation)."""

from __future__ import annotations

from datetime import UTC, date, time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.coordinator import (
    ClimateAdvisorCoordinator,
    _build_future_forecast_outdoor,
    _build_outdoor_curve,
    _compute_ramp_hours,
    _compute_thermal_factors,
    _cosine_outdoor_curve,
    compute_predicted_temps,
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
    "sleep_heat": 66,
    "sleep_cool": 78,
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
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hours 0-5 should be at setback_heat (60)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(60.0, abs=0.5), f"Hour {h}: expected ~60, got {indoor[h]['temp']}"

    def test_daytime_at_comfort(self):
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hours 8-21 should be at comfort_heat (70)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(70.0, abs=1.0), f"Hour {h}: expected ~70, got {indoor[h]['temp']}"

    def test_bedtime_setback_applied(self):
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Hour 23: bedtime setback = comfort_heat - 4 = 66
        assert indoor[23]["temp"] == pytest.approx(66.0, abs=1.0)


class TestIndoorPredictionCooling:
    """Test indoor prediction in cooling mode."""

    def test_overnight_at_cool_setback(self):
        c = _make_classification(
            day_type="hot", hvac_mode="cool", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight should be setback_cool (80)
        assert indoor[3]["temp"] == pytest.approx(80.0, abs=0.5)

    def test_daytime_at_comfort_cool(self):
        c = _make_classification(
            day_type="hot", hvac_mode="cool", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Daytime should be comfort_cool (75)
        assert indoor[12]["temp"] == pytest.approx(75.0, abs=1.0)

    def test_bedtime_setback_cool(self):
        c = _make_classification(
            day_type="hot", hvac_mode="cool", windows_recommended=False, window_open_time=None, window_close_time=None
        )
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
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=3.0,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight setback = 60 + 3 = 63
        assert indoor[3]["temp"] == pytest.approx(63.0, abs=0.5)

    def test_negative_modifier_lowers_setback(self):
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=-3.0,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Overnight setback = 60 - 3 = 57
        assert indoor[3]["temp"] == pytest.approx(57.0, abs=0.5)


class TestIndoorPredictionHvacOff:
    """Test indoor prediction drift logic when HVAC mode is off."""

    def test_drift_toward_outdoor_windows_open(self):
        """On a hot hvac_off day, midday indoor rises toward outdoor via equilibrium model.

        In the new equilibrium model, windows are not a factor for off-days (no drift rate).
        On a hot day (high=90, low=60), outdoor at h=12 ≈ 85°F, warm_diff≈0 → indoor ≈ 85°F > 75.
        """
        # Warm day: outdoor will be above comfort_cool (75) during midday
        c = _make_classification(
            day_type="warm",
            hvac_mode="off",
            today_high=90.0,
            today_low=60.0,
            windows_recommended=True,
            window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        outdoor, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # At h=12 (midday), outdoor is well above 75 → equilibrium pushes indoor above comfort
        assert indoor[12]["temp"] > 75.0, f"Indoor at h=12 should be above comfort, got {indoor[12]['temp']}"

    def test_drift_direction_when_outdoor_cooler(self):
        """When outdoor < comfort, indoor drifts below comfort."""
        c = _make_classification(
            day_type="warm",
            hvac_mode="off",
            today_high=65.0,
            today_low=50.0,
            windows_recommended=True,
            window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Outdoor at midday ~65, comfort_cool=75 → outdoor < comfort
        # Indoor should drift below comfort
        assert indoor[12]["temp"] < 75.0, (
            f"Indoor should drift below comfort when outdoor is cooler, got {indoor[12]['temp']}"
        )

    def test_overnight_at_or_above_setback_heat_when_hvac_off(self):
        """On hvac_off days, overnight prediction = max(setback_heat, equilibrium).

        With warm outdoor (high=90/low=60) and default differential (cold_diff=15),
        equilibrium overnight ≈ 60+15=75°F → well above setback_heat (60°F).
        """
        c = _make_classification(day_type="warm", hvac_mode="off", today_high=90.0, today_low=60.0)
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        setback_heat = DEFAULT_CONFIG["setback_heat"]  # 60
        for h in range(6):
            assert indoor[h]["temp"] >= setback_heat, (
                f"Hour {h}: {indoor[h]['temp']}°F should be >= setback_heat ({setback_heat}°F)"
            )

    def test_hot_day_hvac_off_indoor_rises_toward_outdoor(self):
        """On a hot day with hvac_off, midday indoor should be high (no AC ceiling)."""
        c = _make_classification(
            day_type="warm",
            hvac_mode="off",
            today_high=90.0,
            today_low=60.0,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Midday outdoor ~85°F, warm_diff≈0 → equilibrium ≈ 85°F → indoor should be high
        assert indoor[14]["temp"] > 75.0, (
            f"Hot day with no AC: indoor at h=14 should exceed 75°F, got {indoor[14]['temp']}"
        )
        # Should never fall below setback_heat (heater floor)
        for entry in indoor:
            assert entry["temp"] >= DEFAULT_CONFIG["setback_heat"]

    def test_cool_off_day_stays_near_heater_floor(self):
        """Cold outdoor with hvac_off: equilibrium is low, heater floor dominates."""
        c = _make_classification(
            day_type="warm",
            hvac_mode="off",
            today_high=55.0,
            today_low=40.0,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        setback_heat = DEFAULT_CONFIG["setback_heat"]  # 60
        for entry in indoor:
            assert entry["temp"] >= setback_heat, (
                f"Hour {entry['hour']}: {entry['temp']}°F fell below setback_heat ({setback_heat}°F)"
            )

    def test_hvac_off_no_setback_cool_in_prediction(self):
        """hvac_off prediction must never produce setback_cool (80°F) in any hour."""
        c = _make_classification(
            day_type="warm",
            hvac_mode="off",
            today_high=90.0,
            today_low=60.0,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        setback_cool = DEFAULT_CONFIG["setback_cool"]  # 80
        for entry in indoor:
            assert entry["temp"] != pytest.approx(setback_cool, abs=0.1), (
                f"Hour {entry['hour']}: should not hit setback_cool ({setback_cool}°F), got {entry['temp']}"
            )


class TestRampTransitions:
    """Test wake-up and bedtime ramp transitions.

    Since compute_predicted_temps produces hourly (integer h) data points,
    ramps are only visible when wake/sleep times fall at exact hours so
    an integer h lands within the 30-minute ramp window [time, time+0.5).
    """

    def test_wake_ramp_start(self):
        """At wake_time="07:00", h=7 is ramp start (frac=0 → setback)."""
        cfg = {**DEFAULT_CONFIG, "wake_time": "07:00"}
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
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
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        # h=22: frac = 0 → temp = comfort + 0*(bedtime - comfort) = comfort = 70
        assert indoor[22]["temp"] == pytest.approx(70.0, abs=0.5)
        # h=23: past ramp (22.5) → bedtime setback = 70 - 4 = 66
        assert indoor[23]["temp"] == pytest.approx(66.0, abs=1.0)

    def test_bedtime_setback_heat_formula(self):
        """Heat mode bedtime setback = comfort_heat - 4 + setback_modifier."""
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=2.0,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Bedtime setback = 70 - 4 + 2 = 68
        assert indoor[23]["temp"] == pytest.approx(68.0, abs=1.0)

    def test_bedtime_setback_cool_formula(self):
        """Cool mode bedtime setback = comfort_cool + 3 (no modifier)."""
        c = _make_classification(
            day_type="hot",
            hvac_mode="cool",
            setback_modifier=2.0,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # Bedtime setback = 75 + 3 = 78 (modifier not applied in cool mode)
        assert indoor[23]["temp"] == pytest.approx(78.0, abs=1.0)

    def test_default_wake_no_ramp_visible(self):
        """With default wake_time=06:30, no integer hour falls in ramp [6.5, 7.0)."""
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, DEFAULT_CONFIG)
        # h=6: before 6.5 → setback (60)
        assert indoor[6]["temp"] == pytest.approx(60.0, abs=0.5)
        # h=7: after 7.0 → comfort (70) — no ramp value visible
        assert indoor[7]["temp"] == pytest.approx(70.0, abs=0.5)


class TestCustomConfig:
    """Test that custom config values are correctly reflected in predictions."""

    def test_custom_comfort_heat(self):
        cfg = {**DEFAULT_CONFIG, "comfort_heat": 72}
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(72.0, abs=1.0), f"Hour {h}: expected ~72, got {indoor[h]['temp']}"

    def test_custom_comfort_cool(self):
        cfg = {**DEFAULT_CONFIG, "comfort_cool": 78}
        c = _make_classification(
            day_type="hot", hvac_mode="cool", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(8, 22):
            assert indoor[h]["temp"] == pytest.approx(78.0, abs=1.0), f"Hour {h}: expected ~78, got {indoor[h]['temp']}"

    def test_custom_setback_heat(self):
        cfg = {**DEFAULT_CONFIG, "setback_heat": 55}
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(55.0, abs=0.5), f"Hour {h}: expected ~55, got {indoor[h]['temp']}"

    def test_custom_setback_cool(self):
        cfg = {**DEFAULT_CONFIG, "setback_cool": 82}
        c = _make_classification(
            day_type="hot", hvac_mode="cool", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        for h in range(6):
            assert indoor[h]["temp"] == pytest.approx(82.0, abs=0.5), f"Hour {h}: expected ~82, got {indoor[h]['temp']}"

    def test_custom_wake_time(self):
        cfg = {**DEFAULT_CONFIG, "wake_time": "08:00"}
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        # Hour 7 is before wake at 8:00 — should be at setback (60)
        assert indoor[7]["temp"] == pytest.approx(60.0, abs=0.5)
        # Hour 9 is past wake ramp (8:00 + 0.5 = 8:30) — should be at comfort (70)
        assert indoor[9]["temp"] == pytest.approx(70.0, abs=1.0)

    def test_custom_sleep_time(self):
        cfg = {**DEFAULT_CONFIG, "sleep_time": "23:00"}
        c = _make_classification(
            day_type="cold", hvac_mode="heat", windows_recommended=False, window_open_time=None, window_close_time=None
        )
        _, indoor = compute_predicted_temps(c, cfg)
        # Hour 22 is before sleep at 23:00 — should still be at comfort (70)
        assert indoor[22]["temp"] == pytest.approx(70.0, abs=1.0)


# ---------------------------------------------------------------------------
# Phase 5G: _compute_ramp_hours and thermal model integration
# ---------------------------------------------------------------------------


class TestComputeRampHours:
    """Tests for _compute_ramp_hours()."""

    def test_ramp_duration_falls_back_to_30min_when_no_model(self):
        """thermal_model=None → ramp is 0.5 hrs (30 min)."""
        result = _compute_ramp_hours(10.0, "heat", None)
        assert result == pytest.approx(0.5)

    def test_ramp_duration_uses_thermal_model_heat(self):
        """heating_rate=2.0°F/hr, delta=10°F → ramp is 5.0 hrs."""
        model = {"confidence": "high", "heating_rate_f_per_hour": 2.0}
        result = _compute_ramp_hours(10.0, "heat", model)
        assert result == pytest.approx(5.0)

    def test_ramp_duration_clamped_to_min_15min(self):
        """Very fast house → ramp is at least 0.25 hrs (15 min)."""
        model = {"confidence": "high", "heating_rate_f_per_hour": 1000.0}
        result = _compute_ramp_hours(1.0, "heat", model)
        assert result == pytest.approx(0.25)

    def test_ramp_falls_back_to_30min_when_confidence_none(self):
        """confidence='none' → ramp is 0.5 hrs regardless of rate."""
        model = {"confidence": "none", "heating_rate_f_per_hour": 5.0}
        result = _compute_ramp_hours(10.0, "heat", model)
        assert result == pytest.approx(0.5)

    def test_ramp_falls_back_to_30min_when_rate_is_none(self):
        """Rate is None → ramp is 0.5 hrs."""
        model = {"confidence": "high", "heating_rate_f_per_hour": None}
        result = _compute_ramp_hours(10.0, "heat", model)
        assert result == pytest.approx(0.5)

    def test_ramp_uses_cooling_rate_for_cool_mode(self):
        """Cool mode uses cooling_rate_f_per_hour."""
        model = {"confidence": "high", "cooling_rate_f_per_hour": 5.0}
        result = _compute_ramp_hours(10.0, "cool", model)
        assert result == pytest.approx(2.0)


class TestEveningRampUsesThermalModel:
    """Test that thermal_model is passed to compute_predicted_temps and affects ramp."""

    def test_evening_ramp_uses_thermal_model(self):
        """Pass thermal_model → evening ramp differs from 0.5 default."""
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        )
        # Slow house: rate=0.5°F/hr → ramp for 10°F delta (70-60) = 20 hrs (capped in practice by hours)
        slow_model = {"confidence": "high", "heating_rate_f_per_hour": 0.5}
        _, indoor_with_model = compute_predicted_temps(c, DEFAULT_CONFIG, thermal_model=slow_model)
        _, indoor_no_model = compute_predicted_temps(c, DEFAULT_CONFIG, thermal_model=None)

        # With a slow model, the morning ramp should be longer (start lower before reaching comfort)
        # h=7 is within wake ramp (wake_h=6.5, ramp_no_model=0.5 → ramp ends at 7.0)
        # With no model (ramp=0.5): h=7 is past ramp → comfort (70)
        # With slow model (ramp=20): h=7 is still in ramp → below comfort
        assert indoor_no_model[7]["temp"] == pytest.approx(70.0, abs=0.5)
        assert indoor_with_model[7]["temp"] < 70.0, (
            f"Slow model should still be ramping at h=7, got {indoor_with_model[7]['temp']}"
        )

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


def _mock_dt_util(today: date = _TODAY):
    """Return a MagicMock that makes dt_util.now().date() return *today*."""
    mock = MagicMock()
    mock.now.return_value.date.return_value = today
    # as_local: pass through for naive datetimes used in tests
    mock.as_local = lambda dt: dt
    return mock


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
        """Full 24h hourly data → output preserves shape, normalised to high/low."""
        # Build a forecast with a recognisable linear ramp 0→46.
        forecast = [_hourly_entry(h, float(h * 2)) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # After normalisation the range must match high/low.
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)
        # Shape preserved: hour 0 should be the minimum, hour 23 the maximum.
        assert temps[0] == pytest.approx(_LOW, abs=0.15)
        assert temps[23] == pytest.approx(_HIGH, abs=0.15)

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
        """Data at hours 0, 6, 12, 18 only → intermediate hours interpolated linearly."""
        sparse = [
            _hourly_entry(0, 60.0),
            _hourly_entry(6, 66.0),
            _hourly_entry(12, 78.0),
            _hourly_entry(18, 72.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, sparse)

        by_hour = {p["hour"]: p["temp"] for p in result}
        assert len(result) == 24
        # After normalisation the range spans _HIGH/_LOW.
        temps = [p["temp"] for p in result]
        assert min(temps) == pytest.approx(_LOW, abs=0.5)
        assert max(temps) == pytest.approx(_HIGH, abs=0.5)
        # Shape check: hour 12 had the highest raw value, so it should
        # be at or near _HIGH.  Hour 0 had the lowest, near _LOW.
        assert by_hour[12] == pytest.approx(_HIGH, abs=0.5)
        assert by_hour[0] == pytest.approx(_LOW, abs=0.5)
        # Monotonic between 0 and 6 (raw values increase)
        assert by_hour[3] > by_hour[0]
        assert by_hour[3] < by_hour[6]

    # ------------------------------------------------------------------
    # Edge-hour cosine fill
    # ------------------------------------------------------------------

    def test_edge_hours_use_cosine_fill(self):
        """Data only for hours 6–18 → hours outside that range get cosine fill,
        then the whole curve is normalised to high/low."""
        # All mid-day entries at the same temp; edge hours come from cosine.
        mid_forecast = [_hourly_entry(h, 70.0) for h in range(6, 19)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mid_forecast)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # Normalised → range matches high/low.
        assert min(temps) == pytest.approx(_LOW, abs=0.5)
        assert max(temps) == pytest.approx(_HIGH, abs=0.5)

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def test_malformed_entries_skipped(self):
        """Entries with missing datetime or temperature are skipped without error."""
        forecast = [
            {"temperature": 70.0},  # missing datetime
            {"datetime": f"{_TODAY_STR}T10:00:00"},  # missing temperature
            {"datetime": None, "temperature": 72.0},  # None datetime
            {"datetime": f"{_TODAY_STR}T12:00:00", "temperature": 80.0},  # valid
            {"datetime": "not-a-date", "temperature": 65.0},  # bad format
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24

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
        today_entries = [_hourly_entry(h, float(60 + h)) for h in range(24)]
        tomorrow_entries = [_hourly_entry(h, float(200 + h), _TOMORROW_STR) for h in range(24)]
        mixed = today_entries + tomorrow_entries

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(_TODAY),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mixed)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # Today's raw range is 60..83 (24 entries). Normalised to _HIGH/_LOW.
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)
        # Shape: hour 0 had the lowest raw value → should be near _LOW
        assert temps[0] == pytest.approx(_LOW, abs=0.5)
        # hour 23 had the highest raw value → should be near _HIGH
        assert temps[23] == pytest.approx(_HIGH, abs=0.5)

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def test_normalisation_spans_daily_high_low(self):
        """Hourly data with a narrow range is scaled to match daily high/low."""
        # Hourly data only spans 68-72 but daily says 55-85.
        forecast = [_hourly_entry(h, 68.0 + (4.0 * h / 23.0)) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        temps = [p["temp"] for p in result]
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)

    def test_flat_hourly_data_falls_back_to_cosine(self):
        """If all hourly values are the same, fall back to cosine model."""
        forecast = [_hourly_entry(h, 70.0) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        assert result == expected


# ---------------------------------------------------------------------------
# Phase 3F3: get_chart_data() thermal_model inclusion
# ---------------------------------------------------------------------------


def _make_chart_coordinator(temp_unit: str = "fahrenheit", thermal_model_return: dict | None = None) -> object:
    """Create a minimal coordinator stub for testing get_chart_data()."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"temp_unit": temp_unit}
    coord._current_classification = None
    coord._hourly_forecast_temps = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []

    ae = MagicMock()
    ae._thermal_model = None
    coord.automation_engine = ae

    learning = MagicMock()
    learning.get_thermal_model.return_value = thermal_model_return if thermal_model_return is not None else {}
    coord.learning = learning

    chart_log = MagicMock()
    chart_log.get_entries.return_value = []
    coord._chart_log = chart_log

    coord._thermal_factors = None

    return coord


def _mock_dt_util_fixed(hour: int = 12, minute: int = 0):
    """Return a dt_util mock whose now() returns a fixed time."""
    mock = MagicMock()
    mock.now.return_value.hour = hour
    mock.now.return_value.minute = minute
    mock.as_local = lambda dt: dt
    return mock


class TestGetChartDataThermalModel:
    """Tests verifying that get_chart_data() includes a correct thermal_model dict."""

    def test_chart_data_includes_thermal_model_key(self):
        """When model has no rates, thermal_model key includes confidence='none' and None rates."""
        model_return = {
            "confidence": "none",
            "observation_count_heat": 0,
            "observation_count_cool": 0,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        assert "thermal_model" in chart
        tm = chart["thermal_model"]
        assert tm["confidence"] == "none"
        assert tm["heating_rate"] is None
        assert tm["cooling_rate"] is None
        assert tm["observation_count_heat"] == 0
        assert tm["observation_count_cool"] == 0

    def test_chart_data_thermal_model_rates_unit_converted(self):
        """In Celsius mode, heating_rate is converted via *5/9; missing cooling_rate is None."""
        model_return = {
            "confidence": "low",
            "observation_count_heat": 6,
            "observation_count_cool": 0,
            "heating_rate_f_per_hour": 2.0,
        }
        coord = _make_chart_coordinator(temp_unit="celsius", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        tm = chart["thermal_model"]
        assert tm["heating_rate"] == pytest.approx(2.0 * 5 / 9)
        assert tm["cooling_rate"] is None
        assert tm["unit"] == "celsius"

    def test_chart_data_thermal_model_fahrenheit_rates_unchanged(self):
        """In Fahrenheit mode, heating_rate and cooling_rate are returned as-is."""
        model_return = {
            "confidence": "high",
            "observation_count_heat": 20,
            "observation_count_cool": 15,
            "heating_rate_f_per_hour": 3.5,
            "cooling_rate_f_per_hour": 2.0,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        tm = chart["thermal_model"]
        assert tm["heating_rate"] == pytest.approx(3.5)
        assert tm["cooling_rate"] == pytest.approx(2.0)
        assert tm["unit"] == "fahrenheit"

    def test_chart_data_none_rates_do_not_raise(self):
        """Regression test for #64: when get_thermal_model() returns None rate values
        (keys present, values None), get_chart_data() must not raise TypeError."""
        model_return = {
            "confidence": "none",
            "observation_count_heat": 0,
            "observation_count_cool": 0,
            "heating_rate_f_per_hour": None,
            "cooling_rate_f_per_hour": None,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()  # must not raise

        tm = chart["thermal_model"]
        assert tm["heating_rate"] is None
        assert tm["cooling_rate"] is None


def _make_dt_util_mock(now_dt):
    """Return a dt_util mock using now_dt as the current time.

    dt_util.now() returns now_dt; dt_util.as_local() returns the dt unchanged
    (tests use UTC datetimes throughout so no conversion is needed).
    """
    mock = MagicMock()
    mock.now.return_value = now_dt
    mock.as_local = lambda dt: dt
    return mock


class TestBuildFutureForecastOutdoor:
    """Tests for _build_future_forecast_outdoor() — multi-day forecast extraction."""

    def _make_entry(self, dt_str: str, temp: float) -> dict:
        return {"datetime": dt_str, "temperature": temp}

    def test_empty_on_none(self):
        result = _build_future_forecast_outdoor(None)
        assert result == []

    def test_empty_on_empty_list(self):
        result = _build_future_forecast_outdoor([])
        assert result == []

    def test_filters_past_entries(self):
        """Only entries at or after now should be returned."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        entries = [
            # 6 past entries (1h apart going backward)
            self._make_entry((now_utc - timedelta(hours=i + 1)).isoformat(), 60.0 + i)
            for i in range(6)
        ] + [
            # 6 future entries (1h apart going forward)
            self._make_entry((now_utc + timedelta(hours=i + 1)).isoformat(), 70.0 + i)
            for i in range(6)
        ]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert len(result) == 6
        for item in result:
            assert "ts" in item
            assert "temp" in item
            assert isinstance(item["temp"], float)

    def test_multi_day_coverage(self):
        """All available forecast days should be returned, not just today."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        # 72 future entries = 3 days of hourly data
        entries = [self._make_entry((now_utc + timedelta(hours=i + 1)).isoformat(), 55.0 + (i % 20)) for i in range(72)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert len(result) == 72
        # All should have ISO ts strings and float temps
        for item in result:
            assert isinstance(item["ts"], str)
            assert isinstance(item["temp"], float)
        # Should be sorted by timestamp
        assert result == sorted(result, key=lambda x: x["ts"])

    def test_result_sorted_ascending(self):
        """Results must be sorted ascending by timestamp."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        # Insert in reverse order
        entries = [self._make_entry((now_utc + timedelta(hours=6 - i)).isoformat(), 65.0) for i in range(5)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert result == sorted(result, key=lambda x: x["ts"])


class TestComputeThermalFactors:
    """Tests for _compute_thermal_factors helper."""

    def test_insufficient_data_returns_defaults(self):
        factors = _compute_thermal_factors([])
        assert factors["time_lag_hours"] == pytest.approx(1.0)
        assert factors["cold_diff"] == pytest.approx(15.0)
        assert factors["mild_diff"] == pytest.approx(8.0)
        assert factors["warm_diff"] == pytest.approx(0.0)
        assert factors["has_data"] is False

    def test_differential_bucketing(self):
        """Each outdoor temp range produces a separate differential bucket."""
        entries = (
            [{"outdoor": 55.0, "indoor": 70.0, "hvac": "idle"}] * 10  # cold: diff=15
            + [{"outdoor": 65.0, "indoor": 73.0, "hvac": "idle"}] * 10  # mild: diff=8
            + [{"outdoor": 75.0, "indoor": 75.0, "hvac": "idle"}] * 5  # warm: diff=0
        )
        factors = _compute_thermal_factors(entries)
        assert factors["cold_diff"] == pytest.approx(15.0, abs=0.5)
        assert factors["mild_diff"] == pytest.approx(8.0, abs=0.5)
        assert factors["warm_diff"] == pytest.approx(0.0, abs=0.5)
        assert factors["has_data"] is True


class TestOutdoorConditionalDiff:
    """Tests for _outdoor_conditional_diff — smooth bucket transitions."""

    def test_cold_zone_returns_cold_diff(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        assert _outdoor_conditional_diff(50.0, tf) == pytest.approx(15.0)
        assert _outdoor_conditional_diff(58.0, tf) == pytest.approx(15.0)

    def test_warm_zone_returns_warm_diff(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        assert _outdoor_conditional_diff(72.0, tf) == pytest.approx(0.0)
        assert _outdoor_conditional_diff(80.0, tf) == pytest.approx(0.0)

    def test_cold_mild_midpoint_is_halfway(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 16.0, "mild_diff": 8.0, "warm_diff": 0.0}
        # Midpoint 60°F = halfway between 58 and 62
        mid = _outdoor_conditional_diff(60.0, tf)
        assert mid == pytest.approx(12.0, abs=0.1)

    def test_mild_warm_midpoint_is_halfway(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 2.0}
        # Midpoint 70°F = halfway between 68 and 72
        mid = _outdoor_conditional_diff(70.0, tf)
        assert mid == pytest.approx(5.0, abs=0.1)

    def test_no_jump_crossing_cold_mild_boundary(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        d59 = _outdoor_conditional_diff(59.0, tf)
        d61 = _outdoor_conditional_diff(61.0, tf)
        # 2°F outdoor change near boundary → < 4°F diff change (vs 7.6°F hard cutoff)
        assert abs(d61 - d59) < 4.0


class TestSleepTempInPrediction:
    """sleep_heat/sleep_cool appear in the 24-hour predicted curve."""

    def test_prediction_heat_uses_sleep_heat_at_bedtime(self):
        """After sleep_time, indoor prediction should settle near sleep_heat."""
        c = _make_classification(hvac_mode="heat", today_high=45.0, today_low=35.0)
        config = {**DEFAULT_CONFIG, "sleep_heat": 67.0}
        _, indoor = compute_predicted_temps(c, config)
        # sleep_time is 22:30 → hours 23 are fully in the bedtime setback period
        overnight = [e for e in indoor if e["hour"] >= 23]
        for entry in overnight:
            assert entry["temp"] == pytest.approx(67.0, abs=0.5), (
                f"h={entry['hour']}: expected 67°F, got {entry['temp']}"
            )

    def test_prediction_cool_uses_sleep_cool_at_bedtime(self):
        """After sleep_time, indoor prediction should settle near sleep_cool."""
        c = _make_classification(hvac_mode="cool", today_high=95.0, today_low=75.0)
        config = {**DEFAULT_CONFIG, "sleep_cool": 79.0}
        _, indoor = compute_predicted_temps(c, config)
        overnight = [e for e in indoor if e["hour"] >= 23]
        for entry in overnight:
            assert entry["temp"] == pytest.approx(79.0, abs=0.5), (
                f"h={entry['hour']}: expected 79°F, got {entry['temp']}"
            )

    def test_custom_sleep_heat_warmer_than_default_overnight(self):
        """Non-default sleep_heat=69 (warmer) gives higher overnight temp than sleep_heat=66."""
        c = _make_classification(hvac_mode="heat", today_high=45.0, today_low=35.0)
        _, indoor_66 = compute_predicted_temps(c, {**DEFAULT_CONFIG, "sleep_heat": 66.0})
        _, indoor_69 = compute_predicted_temps(c, {**DEFAULT_CONFIG, "sleep_heat": 69.0})
        # Hour 23 should be at full setback for both (ramp finishes quickly)
        assert indoor_69[-1]["temp"] > indoor_66[-1]["temp"], (
            f"sleep_heat=69 should give warmer h=23 than sleep_heat=66; "
            f"got {indoor_69[-1]['temp']} vs {indoor_66[-1]['temp']}"
        )
