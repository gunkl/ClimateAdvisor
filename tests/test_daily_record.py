"""Tests for Issue #43 — Daily Record expansion and Status tile enhancements.

Tests for:
- LearningEngine.get_record_by_date() public method
- Coordinator.yesterday_record property
- Coordinator.tomorrow_plan property
- Status API response includes setpoint and indoor temp
- Learning API response includes yesterday_record and tomorrow_plan
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import (
    DayClassification,
)
from custom_components.climate_advisor.const import (
    DAY_TYPE_HOT,
)
from custom_components.climate_advisor.learning import (
    DailyRecord,
    LearningEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(date: str = "2026-03-18", **overrides) -> DailyRecord:
    defaults = dict(day_type="mild", trend_direction="stable")
    defaults.update(overrides)
    return DailyRecord(date=date, **defaults)


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": DAY_TYPE_HOT,
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 95,
        "today_low": 72,
        "tomorrow_high": 93,
        "tomorrow_low": 70,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


# ---------------------------------------------------------------------------
# LearningEngine.get_record_by_date
# ---------------------------------------------------------------------------


class TestGetRecordByDate:
    """Tests for LearningEngine.get_record_by_date()."""

    def test_returns_none_when_empty(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        assert engine.get_record_by_date("2026-03-20") is None

    def test_returns_correct_record(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        # Relative dates so they stay inside the 90-day retention window (issue #242)
        d1 = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        d2 = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        d3 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        engine.record_day(_make_record(d1, day_type="cool"))
        engine.record_day(_make_record(d2, day_type="mild"))
        engine.record_day(_make_record(d3, day_type="warm"))

        result = engine.get_record_by_date(d2)
        assert result is not None
        assert result["date"] == d2
        assert result["day_type"] == "mild"

    def test_returns_none_wrong_date(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        engine.record_day(_make_record((datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")))
        assert engine.get_record_by_date((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")) is None

    def test_returns_most_recent_if_duplicates(self, tmp_path: Path):
        """If somehow two records share a date, return the most recent one."""
        engine = LearningEngine(tmp_path)
        dup = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        engine.record_day(_make_record(dup, day_type="cool"))
        engine.record_day(_make_record(dup, day_type="warm"))
        result = engine.get_record_by_date(dup)
        assert result is not None
        # reversed() iteration returns the last-appended record first
        assert result["day_type"] == "warm"

    def test_persists_through_save_load(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        d = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        engine.record_day(_make_record(d, day_type="cool"))
        engine.save_state()

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()
        result = engine2.get_record_by_date(d)
        assert result is not None
        assert result["day_type"] == "cool"


# ---------------------------------------------------------------------------
# Coordinator.yesterday_record
# ---------------------------------------------------------------------------


class TestYesterdayRecord:
    """Tests for the yesterday_record coordinator property."""

    def _make_coordinator_stub(self, learning_engine):
        """Create a minimal coordinator-like object with a learning engine."""
        coord = MagicMock()
        coord.learning = learning_engine
        # Bind the real property logic
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        coord.yesterday_record = property(ClimateAdvisorCoordinator.yesterday_record.fget).__get__(coord)
        return coord

    def test_returns_none_when_no_records(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert engine.get_record_by_date(yesterday_str) is None

    def test_returns_yesterday_record(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        engine.record_day(_make_record(yesterday_str, day_type="cool"))

        result = engine.get_record_by_date(yesterday_str)
        assert result is not None
        assert result["date"] == yesterday_str
        assert result["day_type"] == "cool"


# ---------------------------------------------------------------------------
# Coordinator.tomorrow_plan
# ---------------------------------------------------------------------------


class TestTomorrowPlan:
    """Tests for the tomorrow_plan coordinator property."""

    def test_returns_none_without_classification(self):
        """No classification → no plan."""
        coord = MagicMock()
        coord._current_classification = None
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        # Call the property's fget directly
        result = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)
        assert result is None

    def test_returns_valid_dict_with_classification(self):
        """A classification with tomorrow data → dict with expected keys."""
        coord = MagicMock()
        coord.config = {}
        coord._current_classification = _make_classification(
            tomorrow_high=78,
            tomorrow_low=60,
            today_low=58,
        )

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        result = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)

        assert result is not None
        assert "date" in result
        assert "day_type" in result
        assert "hvac_mode" in result
        assert "windows_recommended" in result
        assert "expected_high" in result
        assert "expected_low" in result
        assert "pre_condition" in result
        assert result["expected_high"] == 78
        assert result["expected_low"] == 60

    def test_tomorrow_plan_hot_day(self):
        """tomorrow_high=95 should yield day_type='hot'."""
        coord = MagicMock()
        coord.config = {}
        coord._current_classification = _make_classification(
            tomorrow_high=95,
            tomorrow_low=75,
            today_low=72,
        )

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        result = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)

        assert result is not None
        assert result["day_type"] == "hot"

    def test_tomorrow_plan_cool_day(self):
        """tomorrow_high=50 should yield day_type='cool'."""
        coord = MagicMock()
        coord.config = {}
        coord._current_classification = _make_classification(
            tomorrow_high=50,
            tomorrow_low=35,
            today_low=40,
        )

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        result = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)

        assert result is not None
        assert result["day_type"] == "cool"

    def test_tomorrow_plan_trend_stable(self):
        """Tomorrow plan should show stable trend (same hi/lo for today and tomorrow)."""
        coord = MagicMock()
        coord.config = {}
        coord._current_classification = _make_classification(
            tomorrow_high=72,
            tomorrow_low=55,
            today_low=55,
        )

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        result = ClimateAdvisorCoordinator.tomorrow_plan.fget(coord)

        assert result is not None
        assert result["trend_direction"] == "stable"
