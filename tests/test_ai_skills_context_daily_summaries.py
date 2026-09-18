"""Tests for build_daily_summaries_context() (moved from ai_skills_activity.py's
async_build_activity_context, Issue #563).

Ports the two genuinely-unique regression tests from the retired
TestAsyncBuildActivityContext (the rest of that class tested sections
redundant with the investigator's own existing context providers).
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills_context import (
    build_daily_summaries_context,
    get_provider_registry,
)


def _make_coordinator(records=None):
    coord = MagicMock()
    coord.learning._state.records = records or []
    return coord


class TestBuildDailySummariesContext:
    def test_multi_day_window_includes_historical_summaries(self):
        """hours=168 triggers HISTORICAL DAILY SUMMARIES with past records."""
        today = datetime.date.today()
        records = [
            {
                "date": (today - datetime.timedelta(days=i)).isoformat(),
                "day_type": "mild",
                "hvac_runtime_minutes": 30 * i,
                "manual_overrides": i,
                "comfort_violations_minutes": 0,
                "avg_indoor_temp": 71.0,
                "observed_high_f": 74.0,
                "observed_low_f": 62.0,
            }
            for i in range(1, 6)
        ]
        coord = _make_coordinator(records=records)
        ctx = asyncio.run(build_daily_summaries_context(None, coord, hours=168))
        assert "HISTORICAL DAILY SUMMARIES" in ctx
        for i in range(1, 6):
            assert (today - datetime.timedelta(days=i)).isoformat() in ctx

    def test_fan_only_runtime_column_present_with_correct_values(self):
        """Issue #912: the historical table must show fan-only runtime in its own
        column, separate from HVAC(min) — an occupant reading this table should
        never have to guess whether a day's runtime figure includes fan-only time.
        """
        today = datetime.date.today()
        records = [
            {
                "date": (today - datetime.timedelta(days=1)).isoformat(),
                "day_type": "mild",
                "hvac_runtime_minutes": 0,
                "thermostat_fan_only_runtime_minutes": 402,
                "manual_overrides": 0,
                "comfort_violations_minutes": 0,
                "avg_indoor_temp": 71.0,
                "observed_high_f": 74.0,
                "observed_low_f": 62.0,
            }
        ]
        coord = _make_coordinator(records=records)
        ctx = asyncio.run(build_daily_summaries_context(None, coord, hours=168))
        assert "FanOnly(min)" in ctx
        # HVAC(min)=0 and FanOnly(min)=402 must both appear on the same row, distinctly.
        row_lines = [ln for ln in ctx.splitlines() if (today - datetime.timedelta(days=1)).isoformat() in ln]
        assert len(row_lines) == 1
        assert "402" in row_lines[0]

    def test_single_day_window_returns_empty(self):
        """hours=24 does NOT trigger the historical summaries section — the event
        log / activity timeline already covers a same-day window."""
        coord = _make_coordinator()
        ctx = asyncio.run(build_daily_summaries_context(None, coord, hours=24))
        assert ctx == ""

    def test_registered_in_provider_registry(self):
        # Issue #920: daily_summaries is a priority-2 "deep" provider, excluded from
        # the default (non-deep) selection -- assert via deep=True.
        registry = get_provider_registry()
        names = [p.name for p in registry.select(deep=True)]
        assert "daily_summaries" in names
