"""Tests validating window field population per day type.

The dashboard consolidates window-related rows based on day_type.
These tests verify that the classifier populates the correct window
fields for each day type, matching the frontend's conditional branches.

Issue: #44 — Consolidate window schedule display in Classification Details
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
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_forecast(
    today_high: float,
    today_low: float = 50.0,
    tomorrow_high: float | None = None,
    tomorrow_low: float | None = None,
    current_outdoor_temp: float = 60.0,
) -> ForecastSnapshot:
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
# HOT day — opportunity fields populated, open/close times are None
# ---------------------------------------------------------------------------


class TestHotDayWindowFields:
    """On hot days the dashboard shows morning/evening opportunity rows."""

    def test_hot_day_has_no_open_close_times(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_HOT + 5, today_low=65))
        assert c.day_type == DAY_TYPE_HOT
        assert c.window_open_time is None
        assert c.window_close_time is None

    def test_hot_day_morning_opportunity_when_low_moderate(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_HOT + 5, today_low=75))
        assert c.window_opportunity_morning is True
        assert isinstance(c.window_opportunity_morning_start, time)
        assert isinstance(c.window_opportunity_morning_end, time)

    def test_hot_day_evening_opportunity_when_tomorrow_low_moderate(self):
        c = classify_day(
            _make_forecast(
                today_high=THRESHOLD_HOT + 5,
                today_low=75,
                tomorrow_high=THRESHOLD_HOT + 5,
                tomorrow_low=75,
            )
        )
        assert c.window_opportunity_evening is True
        assert isinstance(c.window_opportunity_evening_start, time)

    def test_hot_day_no_opportunity_when_lows_too_high(self):
        c = classify_day(
            _make_forecast(
                today_high=THRESHOLD_HOT + 5,
                today_low=85,
                tomorrow_high=THRESHOLD_HOT + 5,
                tomorrow_low=85,
            )
        )
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False


# ---------------------------------------------------------------------------
# WARM day — open/close times populated, opportunity fields are False
# ---------------------------------------------------------------------------


class TestWarmDayWindowFields:
    """On warm days the dashboard shows open/close time rows."""

    def test_warm_day_has_open_close_times(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_WARM + 2, today_low=55))
        assert c.day_type == DAY_TYPE_WARM
        assert isinstance(c.window_open_time, time)
        assert isinstance(c.window_close_time, time)

    def test_warm_day_no_opportunity_fields(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_WARM + 2, today_low=55))
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False
        assert c.window_opportunity_morning_start is None
        assert c.window_opportunity_evening_start is None


# ---------------------------------------------------------------------------
# MILD day — open/close times populated, opportunity fields are False
# ---------------------------------------------------------------------------


class TestMildDayWindowFields:
    """On mild days the dashboard shows open/close time rows."""

    def test_mild_day_has_open_close_times(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_MILD + 2, today_low=45))
        assert c.day_type == DAY_TYPE_MILD
        assert isinstance(c.window_open_time, time)
        assert isinstance(c.window_close_time, time)

    def test_mild_day_windows_recommended(self):
        # Issue #1211: recommended only when the high reaches the comfort floor (68 F default)
        c = classify_day(_make_forecast(today_high=70, today_low=45))
        assert c.windows_recommended is True

    def test_mild_day_no_opportunity_fields(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_MILD + 2, today_low=45))
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False


# ---------------------------------------------------------------------------
# COOL day — all window fields at defaults
# ---------------------------------------------------------------------------


class TestCoolDayWindowFields:
    """On cool days the dashboard shows 'No window ventilation today'."""

    def test_cool_day_no_window_fields(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_COOL + 2, today_low=30))
        assert c.day_type == DAY_TYPE_COOL
        assert c.windows_recommended is False
        assert c.window_open_time is None
        assert c.window_close_time is None
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False


# ---------------------------------------------------------------------------
# COLD day — all window fields at defaults
# ---------------------------------------------------------------------------


class TestColdDayWindowFields:
    """On cold days the dashboard shows 'No window ventilation today'."""

    def test_cold_day_no_window_fields(self):
        c = classify_day(_make_forecast(today_high=THRESHOLD_COOL - 5, today_low=10))
        assert c.day_type == DAY_TYPE_COLD
        assert c.windows_recommended is False
        assert c.window_open_time is None
        assert c.window_close_time is None
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False


# ---------------------------------------------------------------------------
# Mutual exclusivity — open/close and opportunity never both populated
# ---------------------------------------------------------------------------


class TestWindowFieldMutualExclusivity:
    """Ensure the two window display modes never overlap."""

    @pytest.mark.parametrize(
        "high,expected_type",
        [
            (THRESHOLD_HOT + 5, DAY_TYPE_HOT),
            (THRESHOLD_WARM + 2, DAY_TYPE_WARM),
            (THRESHOLD_MILD + 2, DAY_TYPE_MILD),
            (THRESHOLD_COOL + 2, DAY_TYPE_COOL),
            (THRESHOLD_COOL - 5, DAY_TYPE_COLD),
        ],
    )
    def test_no_overlap(self, high, expected_type):
        c = classify_day(_make_forecast(today_high=high, today_low=50))
        assert c.day_type == expected_type

        has_schedule = c.window_open_time is not None or c.window_close_time is not None
        has_opportunity = c.window_opportunity_morning or c.window_opportunity_evening

        assert not (has_schedule and has_opportunity), (
            f"Day type {c.day_type} has both schedule and opportunity fields populated"
        )
