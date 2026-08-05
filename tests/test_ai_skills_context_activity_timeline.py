"""Tests for build_activity_timeline_context() (Issue #563 Phase 2/3 merge).

Wires the relocated build_event_timeline_table() into the investigator's
context-provider registry, so the merged skill's silent/scheduled narration mode
and on-demand investigation mode both ground their narrative in an actual
chronological record instead of re-deriving one from raw event-log counts.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.ai_skills_context import (
    build_activity_timeline_context,
    get_provider_registry,
)

_NOW = datetime.datetime(2026, 7, 10, 14, 0, 0, tzinfo=datetime.UTC)


def _make_coordinator(event_log=None, config=None):
    coord = MagicMock()
    coord._event_log = event_log if event_log is not None else []
    coord.config = config if config is not None else {"temp_unit": "fahrenheit"}
    return coord


class TestBuildActivityTimelineContext:
    def test_empty_event_log_produces_no_events_row(self):
        coord = _make_coordinator()
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_activity_timeline_context(None, coord))
        assert "=== ACTIVITY TIMELINE" in ctx
        assert "no events in window" in ctx

    def test_events_render_into_table(self):
        event_log = [{"type": "fan_activated", "time": _NOW, "reason": "natural ventilation"}]
        coord = _make_coordinator(event_log=event_log)
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_activity_timeline_context(None, coord, hours=24))
        assert "Fan" in ctx or "fan" in ctx

    def test_hours_kwarg_is_clamped(self):
        coord = _make_coordinator()
        # Should not raise even with an out-of-range value; clamped to [1, 720].
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_activity_timeline_context(None, coord, hours=99999))
        assert "=== ACTIVITY TIMELINE" in ctx

    def test_registered_in_provider_registry(self):
        registry = get_provider_registry()
        names = [p.name for p in registry.select()]
        assert "activity_timeline" in names
