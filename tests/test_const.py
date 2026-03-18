"""Tests for Climate Advisor constants — validate internal consistency."""
from __future__ import annotations

from custom_components.climate_advisor.const import (
    COMPLIANCE_THRESHOLD_HIGH,
    COMPLIANCE_THRESHOLD_LOW,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_SETBACK_COOL,
    DEFAULT_SETBACK_HEAT,
    DOOR_WINDOW_PAUSE_SECONDS,
    MAX_CONTINUOUS_RUNTIME_HOURS,
    MIN_DATA_POINTS_FOR_SUGGESTION,
    OCCUPANCY_SETBACK_MINUTES,
    SUGGESTION_COOLDOWN_DAYS,
    THRESHOLD_COOL,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    TREND_THRESHOLD_MODERATE,
    TREND_THRESHOLD_SIGNIFICANT,
)


# ---------------------------------------------------------------------------
# Day type thresholds — ascending order
# ---------------------------------------------------------------------------

class TestDayTypeThresholds:
    """Thresholds must form a strict ascending chain: COOL < MILD < WARM < HOT."""

    def test_cool_less_than_mild(self):
        assert THRESHOLD_COOL < THRESHOLD_MILD

    def test_mild_less_than_warm(self):
        assert THRESHOLD_MILD < THRESHOLD_WARM

    def test_warm_less_than_hot(self):
        assert THRESHOLD_WARM < THRESHOLD_HOT

    def test_full_ascending_chain(self):
        assert THRESHOLD_COOL < THRESHOLD_MILD < THRESHOLD_WARM < THRESHOLD_HOT

    def test_thresholds_are_positive(self):
        for threshold in (THRESHOLD_COOL, THRESHOLD_MILD, THRESHOLD_WARM, THRESHOLD_HOT):
            assert threshold > 0, f"Threshold {threshold} should be positive"


# ---------------------------------------------------------------------------
# Setpoint values — sensible comfort / setback relationships
# ---------------------------------------------------------------------------

class TestSetpointValues:
    """Comfort and setback setpoints must satisfy physical/comfort constraints."""

    def test_comfort_heat_less_than_comfort_cool(self):
        # You don't want to heat past the cooling setpoint.
        assert DEFAULT_COMFORT_HEAT < DEFAULT_COMFORT_COOL

    def test_setback_heat_less_than_comfort_heat(self):
        # Heating setback should be cooler than the comfort heat target.
        assert DEFAULT_SETBACK_HEAT < DEFAULT_COMFORT_HEAT

    def test_setback_cool_greater_than_comfort_cool(self):
        # Cooling setback should be warmer than the comfort cool target.
        assert DEFAULT_SETBACK_COOL > DEFAULT_COMFORT_COOL

    def test_setback_heat_is_positive(self):
        assert DEFAULT_SETBACK_HEAT > 0

    def test_comfort_cool_is_positive(self):
        assert DEFAULT_COMFORT_COOL > 0

    def test_all_setpoints_positive(self):
        for sp in (DEFAULT_COMFORT_HEAT, DEFAULT_COMFORT_COOL,
                   DEFAULT_SETBACK_HEAT, DEFAULT_SETBACK_COOL):
            assert sp > 0


# ---------------------------------------------------------------------------
# Trend thresholds — positive and ordered
# ---------------------------------------------------------------------------

class TestTrendThresholds:
    """Trend thresholds must be positive and moderate < significant."""

    def test_moderate_threshold_positive(self):
        assert TREND_THRESHOLD_MODERATE > 0

    def test_significant_threshold_positive(self):
        assert TREND_THRESHOLD_SIGNIFICANT > 0

    def test_moderate_less_than_significant(self):
        assert TREND_THRESHOLD_MODERATE < TREND_THRESHOLD_SIGNIFICANT


# ---------------------------------------------------------------------------
# Timing values — positive
# ---------------------------------------------------------------------------

class TestTimingValues:
    """All timing constants must be positive numbers."""

    def test_door_window_pause_seconds_positive(self):
        assert DOOR_WINDOW_PAUSE_SECONDS > 0

    def test_occupancy_setback_minutes_positive(self):
        assert OCCUPANCY_SETBACK_MINUTES > 0

    def test_max_continuous_runtime_hours_positive(self):
        assert MAX_CONTINUOUS_RUNTIME_HOURS > 0


# ---------------------------------------------------------------------------
# Learning system constants — sanity checks
# ---------------------------------------------------------------------------

class TestLearningConstants:
    """Learning system thresholds must be internally consistent."""

    def test_suggestion_cooldown_days_positive(self):
        assert SUGGESTION_COOLDOWN_DAYS > 0

    def test_min_data_points_positive(self):
        assert MIN_DATA_POINTS_FOR_SUGGESTION > 0

    def test_compliance_low_less_than_high(self):
        assert COMPLIANCE_THRESHOLD_LOW < COMPLIANCE_THRESHOLD_HIGH

    def test_compliance_thresholds_between_zero_and_one(self):
        assert 0.0 < COMPLIANCE_THRESHOLD_LOW < 1.0
        assert 0.0 < COMPLIANCE_THRESHOLD_HIGH < 1.0
