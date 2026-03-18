"""Tests for the Climate Advisor classifier module.

The classifier is pure logic with no Home Assistant dependencies,
so no mocking is required.
"""
from __future__ import annotations

from datetime import time

import pytest

from custom_components.climate_advisor.classifier import (
    ForecastSnapshot,
    classify_day,
)
from custom_components.climate_advisor.const import (
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    THRESHOLD_COOL,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    TREND_THRESHOLD_MODERATE,
    TREND_THRESHOLD_SIGNIFICANT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_forecast(
    today_high: float,
    today_low: float = 50.0,
    tomorrow_high: float | None = None,
    tomorrow_low: float | None = None,
    current_outdoor_temp: float = 60.0,
) -> ForecastSnapshot:
    """Build a ForecastSnapshot with convenient defaults.

    When tomorrow_high / tomorrow_low are not supplied they default to the
    same as today, producing a stable trend with avg_delta == 0.
    """
    if tomorrow_high is None:
        tomorrow_high = today_high
    if tomorrow_low is None:
        tomorrow_low = today_low
    return ForecastSnapshot(
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=tomorrow_high,
        tomorrow_low=tomorrow_low,
        current_outdoor_temp=current_outdoor_temp,
    )


# ---------------------------------------------------------------------------
# Day type classification — basic values
# ---------------------------------------------------------------------------

class TestDayTypeClassification:
    """classify_day() returns the correct day_type for every temperature band."""

    # --- HOT (today_high >= 85) ---

    @pytest.mark.parametrize("high", [85, 90, 100])
    def test_hot_day(self, high):
        result = classify_day(make_forecast(today_high=high))
        assert result.day_type == DAY_TYPE_HOT, f"Expected hot for high={high}"

    # --- WARM (75 <= today_high < 85) ---

    @pytest.mark.parametrize("high", [75, 80, 84])
    def test_warm_day(self, high):
        result = classify_day(make_forecast(today_high=high))
        assert result.day_type == DAY_TYPE_WARM, f"Expected warm for high={high}"

    # --- MILD (60 <= today_high < 75) ---

    @pytest.mark.parametrize("high", [60, 67, 74])
    def test_mild_day(self, high):
        result = classify_day(make_forecast(today_high=high))
        assert result.day_type == DAY_TYPE_MILD, f"Expected mild for high={high}"

    # --- COOL (45 <= today_high < 60) ---

    @pytest.mark.parametrize("high", [45, 52, 59])
    def test_cool_day(self, high):
        result = classify_day(make_forecast(today_high=high))
        assert result.day_type == DAY_TYPE_COOL, f"Expected cool for high={high}"

    # --- COLD (today_high < 45) ---

    @pytest.mark.parametrize("high", [44, 30, 0])
    def test_cold_day(self, high):
        result = classify_day(make_forecast(today_high=high))
        assert result.day_type == DAY_TYPE_COLD, f"Expected cold for high={high}"


# ---------------------------------------------------------------------------
# Boundary tests — exact threshold values
# ---------------------------------------------------------------------------

class TestDayTypeBoundaries:
    """Exact threshold values must fall into the correct (upper) band."""

    def test_threshold_hot_is_hot(self):
        # 85 >= THRESHOLD_HOT → hot
        result = classify_day(make_forecast(today_high=THRESHOLD_HOT))
        assert result.day_type == DAY_TYPE_HOT

    def test_one_below_hot_is_warm(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_HOT - 1))
        assert result.day_type == DAY_TYPE_WARM

    def test_threshold_warm_is_warm(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_WARM))
        assert result.day_type == DAY_TYPE_WARM

    def test_one_below_warm_is_mild(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_WARM - 1))
        assert result.day_type == DAY_TYPE_MILD

    def test_threshold_mild_is_mild(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_MILD))
        assert result.day_type == DAY_TYPE_MILD

    def test_one_below_mild_is_cool(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_MILD - 1))
        assert result.day_type == DAY_TYPE_COOL

    def test_threshold_cool_is_cool(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_COOL))
        assert result.day_type == DAY_TYPE_COOL

    def test_one_below_cool_is_cold(self):
        result = classify_day(make_forecast(today_high=THRESHOLD_COOL - 1))
        assert result.day_type == DAY_TYPE_COLD


# ---------------------------------------------------------------------------
# Trend computation
#
# The classifier averages the high-delta and low-delta:
#   avg_delta = ((tomorrow_high - today_high) + (tomorrow_low - today_low)) / 2
# "warming"  when avg_delta >  2
# "cooling"  when avg_delta < -2
# "stable"   when -2 <= avg_delta <= 2
# trend_magnitude = abs(avg_delta)
# ---------------------------------------------------------------------------

class TestTrendComputation:
    """classify_day() computes trend_direction and trend_magnitude correctly."""

    # Use a fixed today (mild) so day_type doesn't interfere with reading results.
    TODAY_HIGH = 67.0
    TODAY_LOW = 50.0

    def _forecast(self, tomorrow_high, tomorrow_low):
        return ForecastSnapshot(
            today_high=self.TODAY_HIGH,
            today_low=self.TODAY_LOW,
            tomorrow_high=tomorrow_high,
            tomorrow_low=tomorrow_low,
            current_outdoor_temp=60.0,
        )

    # --- Stable ---

    def test_stable_when_equal(self):
        result = classify_day(self._forecast(self.TODAY_HIGH, self.TODAY_LOW))
        assert result.trend_direction == "stable"
        assert result.trend_magnitude == 0.0

    def test_stable_within_deadband(self):
        # avg_delta = (2 + 2) / 2 = 2 — boundary is exclusive (> 2), so stable
        result = classify_day(self._forecast(self.TODAY_HIGH + 2, self.TODAY_LOW + 2))
        assert result.trend_direction == "stable"

    def test_stable_negative_within_deadband(self):
        result = classify_day(self._forecast(self.TODAY_HIGH - 2, self.TODAY_LOW - 2))
        assert result.trend_direction == "stable"

    # --- Warming ---

    def test_warming_small(self):
        # avg_delta = (4 + 4) / 2 = 4  (> 2, < MODERATE=5)
        result = classify_day(self._forecast(self.TODAY_HIGH + 4, self.TODAY_LOW + 4))
        assert result.trend_direction == "warming"
        assert result.trend_magnitude == pytest.approx(4.0)

    def test_warming_moderate(self):
        # avg_delta = 5 (>= TREND_THRESHOLD_MODERATE)
        result = classify_day(self._forecast(self.TODAY_HIGH + 5, self.TODAY_LOW + 5))
        assert result.trend_direction == "warming"
        assert result.trend_magnitude == pytest.approx(float(TREND_THRESHOLD_MODERATE))

    def test_warming_significant(self):
        # avg_delta = 10 (>= TREND_THRESHOLD_SIGNIFICANT)
        result = classify_day(self._forecast(self.TODAY_HIGH + 10, self.TODAY_LOW + 10))
        assert result.trend_direction == "warming"
        assert result.trend_magnitude == pytest.approx(float(TREND_THRESHOLD_SIGNIFICANT))

    def test_warming_large(self):
        result = classify_day(self._forecast(self.TODAY_HIGH + 20, self.TODAY_LOW + 20))
        assert result.trend_direction == "warming"
        assert result.trend_magnitude == pytest.approx(20.0)

    # --- Cooling ---

    def test_cooling_small(self):
        # avg_delta = -4
        result = classify_day(self._forecast(self.TODAY_HIGH - 4, self.TODAY_LOW - 4))
        assert result.trend_direction == "cooling"
        assert result.trend_magnitude == pytest.approx(4.0)

    def test_cooling_moderate(self):
        result = classify_day(self._forecast(self.TODAY_HIGH - 5, self.TODAY_LOW - 5))
        assert result.trend_direction == "cooling"
        assert result.trend_magnitude == pytest.approx(float(TREND_THRESHOLD_MODERATE))

    def test_cooling_significant(self):
        result = classify_day(self._forecast(self.TODAY_HIGH - 10, self.TODAY_LOW - 10))
        assert result.trend_direction == "cooling"
        assert result.trend_magnitude == pytest.approx(float(TREND_THRESHOLD_SIGNIFICANT))

    # --- Average uses BOTH high and low deltas ---

    def test_trend_is_average_of_high_and_low_deltas(self):
        # high_delta = +10, low_delta = -6 → avg = 2.0 → stable (not > 2)
        result = classify_day(self._forecast(self.TODAY_HIGH + 10, self.TODAY_LOW - 6))
        assert result.trend_direction == "stable"
        assert result.trend_magnitude == pytest.approx(2.0)

    def test_trend_averaged_warming(self):
        # high_delta = +12, low_delta = +4 → avg = 8 → warming
        result = classify_day(self._forecast(self.TODAY_HIGH + 12, self.TODAY_LOW + 4))
        assert result.trend_direction == "warming"
        assert result.trend_magnitude == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Recommendations — DayClassification._compute_recommendations()
# ---------------------------------------------------------------------------

class TestRecommendations:
    """Verify that recommendations are derived correctly for each day type."""

    def _classify(self, today_high, tomorrow_high=None, today_low=50.0, tomorrow_low=None):
        """Convenience: produce a stable-trend DayClassification."""
        if tomorrow_high is None:
            tomorrow_high = today_high
        if tomorrow_low is None:
            tomorrow_low = today_low
        return classify_day(ForecastSnapshot(
            today_high=today_high,
            today_low=today_low,
            tomorrow_high=tomorrow_high,
            tomorrow_low=tomorrow_low,
            current_outdoor_temp=60.0,
        ))

    # --- HOT ---

    def test_hot_hvac_mode_is_cool(self):
        result = self._classify(today_high=90)
        assert result.hvac_mode == "cool"

    def test_hot_pre_condition_enabled(self):
        result = self._classify(today_high=90)
        assert result.pre_condition is True

    def test_hot_pre_condition_target_negative(self):
        # Should be 2°F *below* cooling setpoint
        result = self._classify(today_high=90)
        assert result.pre_condition_target == pytest.approx(-2.0)

    def test_hot_no_window_recommendation(self):
        result = self._classify(today_high=90)
        assert result.windows_recommended is False

    # --- WARM ---

    def test_warm_hvac_mode_is_off(self):
        result = self._classify(today_high=80)
        assert result.hvac_mode == "off"

    def test_warm_windows_recommended(self):
        result = self._classify(today_high=80)
        assert result.windows_recommended is True

    def test_warm_window_open_time(self):
        result = self._classify(today_high=80)
        assert result.window_open_time == time(8, 0)

    def test_warm_window_close_time(self):
        result = self._classify(today_high=80)
        assert result.window_close_time == time(18, 0)

    # --- MILD ---

    def test_mild_hvac_mode_is_off(self):
        result = self._classify(today_high=67)
        assert result.hvac_mode == "off"

    def test_mild_windows_recommended(self):
        result = self._classify(today_high=67)
        assert result.windows_recommended is True

    def test_mild_window_open_time(self):
        result = self._classify(today_high=67)
        assert result.window_open_time == time(10, 0)

    def test_mild_window_close_time(self):
        result = self._classify(today_high=67)
        assert result.window_close_time == time(17, 0)

    # --- COOL ---

    def test_cool_hvac_mode_is_heat(self):
        result = self._classify(today_high=52)
        assert result.hvac_mode == "heat"

    def test_cool_no_pre_condition_by_default(self):
        result = self._classify(today_high=52)
        # No trend modifier should trigger pre_condition on a plain stable cool day
        assert result.pre_condition is False

    def test_cool_no_window_recommendation(self):
        result = self._classify(today_high=52)
        assert result.windows_recommended is False

    # --- COLD ---

    def test_cold_hvac_mode_is_heat(self):
        result = self._classify(today_high=30)
        assert result.hvac_mode == "heat"

    def test_cold_no_window_recommendation(self):
        result = self._classify(today_high=30)
        assert result.windows_recommended is False

    # --- Pre-condition set by default for hot ---

    def test_cold_no_pre_condition_stable_trend(self):
        result = self._classify(today_high=30)
        assert result.pre_condition is False


# ---------------------------------------------------------------------------
# Trend modifiers applied on top of day-type recommendations
# ---------------------------------------------------------------------------

class TestTrendModifiers:
    """Setback modifiers and pre-condition overrides driven by trend magnitude."""

    def _forecast_with_deltas(self, today_high, high_delta, low_delta, today_low=50.0):
        return ForecastSnapshot(
            today_high=today_high,
            today_low=today_low,
            tomorrow_high=today_high + high_delta,
            tomorrow_low=today_low + low_delta,
            current_outdoor_temp=60.0,
        )

    # --- Significant cooling trend (avg_delta <= -10) ---

    def test_significant_cooling_sets_pre_condition(self):
        # avg_delta = (-10 + -10) / 2 = -10 → significant cooling
        result = classify_day(self._forecast_with_deltas(67, -10, -10))
        assert result.pre_condition is True

    def test_significant_cooling_pre_condition_target(self):
        result = classify_day(self._forecast_with_deltas(67, -10, -10))
        assert result.pre_condition_target == pytest.approx(3.0)

    def test_significant_cooling_setback_modifier(self):
        result = classify_day(self._forecast_with_deltas(67, -10, -10))
        assert result.setback_modifier == pytest.approx(3.0)

    # --- Significant warming trend (avg_delta >= 10) ---

    def test_significant_warming_setback_modifier_negative(self):
        result = classify_day(self._forecast_with_deltas(67, 10, 10))
        assert result.setback_modifier == pytest.approx(-3.0)

    def test_significant_warming_no_pre_condition(self):
        result = classify_day(self._forecast_with_deltas(67, 10, 10))
        assert result.pre_condition is False

    # --- Moderate cooling trend (5 <= avg_delta < 10) ---

    def test_moderate_cooling_sets_pre_condition(self):
        # avg_delta = -5 → moderate cooling
        result = classify_day(self._forecast_with_deltas(67, -5, -5))
        assert result.pre_condition is True

    def test_moderate_cooling_pre_condition_target(self):
        result = classify_day(self._forecast_with_deltas(67, -5, -5))
        assert result.pre_condition_target == pytest.approx(2.0)

    def test_moderate_cooling_setback_modifier(self):
        result = classify_day(self._forecast_with_deltas(67, -5, -5))
        assert result.setback_modifier == pytest.approx(2.0)

    # --- Moderate warming trend ---

    def test_moderate_warming_setback_modifier_negative(self):
        result = classify_day(self._forecast_with_deltas(67, 5, 5))
        assert result.setback_modifier == pytest.approx(-2.0)

    # --- Stable trend — no modifier ---

    def test_stable_no_setback_modifier(self):
        result = classify_day(self._forecast_with_deltas(67, 0, 0))
        assert result.setback_modifier == pytest.approx(0.0)

    # --- Trend modifier on hot day overrides pre_condition_target ---

    def test_hot_day_significant_cooling_overrides_pre_condition_target(self):
        # Hot day sets pre_condition_target = -2.0; significant cooling then sets 3.0.
        # The significant-cooling branch runs after the day-type branch and wins.
        result = classify_day(self._forecast_with_deltas(90, -10, -10))
        assert result.day_type == DAY_TYPE_HOT
        assert result.pre_condition_target == pytest.approx(3.0)
        assert result.setback_modifier == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# ForecastSnapshot — optional field defaults
# ---------------------------------------------------------------------------

class TestForecastSnapshotDefaults:
    """Optional fields should default to None."""

    def test_optional_fields_default_to_none(self):
        fs = ForecastSnapshot(
            today_high=70,
            today_low=55,
            tomorrow_high=72,
            tomorrow_low=57,
            current_outdoor_temp=65,
        )
        assert fs.current_indoor_temp is None
        assert fs.current_humidity is None
        assert fs.timestamp is None


# ---------------------------------------------------------------------------
# conftest fixture smoke-test
# ---------------------------------------------------------------------------

def test_basic_forecast_fixture(basic_forecast):
    """The shared fixture should produce a mild, stable-trend classification."""
    result = classify_day(basic_forecast)
    assert result.day_type == DAY_TYPE_MILD
    assert result.trend_direction == "stable"
