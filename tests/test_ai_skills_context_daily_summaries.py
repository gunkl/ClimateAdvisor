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

    def test_single_day_window_returns_empty(self):
        """hours=24 does NOT trigger the historical summaries section — the event
        log / activity timeline already covers a same-day window."""
        coord = _make_coordinator()
        ctx = asyncio.run(build_daily_summaries_context(None, coord, hours=24))
        assert ctx == ""

    def test_registered_in_provider_registry(self):
        registry = get_provider_registry()
        names = [p.name for p in registry.select()]
        assert "daily_summaries" in names
