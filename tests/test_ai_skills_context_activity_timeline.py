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
    build_event_log_context,
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


class TestBuildEventLogContext:
    """Tests for build_event_log_context() (Issue #432): the EVENT LOG section's
    window filtering must not silently drop older-but-still-in-window events when
    recent event volume exceeds the 200-entry display budget.

    build_event_log_context uses real datetime.datetime.now(datetime.UTC) directly
    (not dt_util.now), so fixtures use real-ish "now" timestamps rather than the
    fixed _NOW used by TestBuildActivityTimelineContext above.
    """

    def test_events_spread_across_window_all_counted(self):
        now = datetime.datetime.now(datetime.UTC)
        # Position 0: sentinel, 10h ago (inside a 24h window).
        # Positions 1-250: "noise" events, 30h ago (OUTSIDE the 24h window) — a
        #   large block that pushes the sentinel out of a raw last-200 slice.
        # Positions 251-300: "filler" events, 1h ago (inside the 24h window).
        # Total in-window count = 1 (sentinel) + 50 (filler) = 51, well under
        # the 200-entry display budget — the corrected filter-then-limit order
        # keeps all of them, including the sentinel; the old slice-then-filter
        # order dropped the sentinel purely due to its early raw array position.
        sentinel = {"type": "comfort_band_applied", "time": (now - datetime.timedelta(hours=10)).isoformat()}
        noise_events = [
            {"type": "fan_activated", "time": (now - datetime.timedelta(hours=30, minutes=i)).isoformat()}
            for i in range(250)
        ]
        filler_events = [
            {"type": "fan_deactivated", "time": (now - datetime.timedelta(hours=1, minutes=i)).isoformat()}
            for i in range(50)
        ]
        event_log = [sentinel] + noise_events + filler_events
        coord = _make_coordinator(event_log=event_log)
        ctx = asyncio.run(build_event_log_context(None, coord, hours=24))
        assert "'comfort_band_applied': 1" in ctx, f"sentinel event missing from type_counts:\n{ctx}"

    def test_limited_note_when_window_exceeds_200(self):
        now = datetime.datetime.now(datetime.UTC)
        event_log = [
            {"type": "fan_activated", "time": (now - datetime.timedelta(minutes=i)).isoformat()} for i in range(220)
        ]
        coord = _make_coordinator(event_log=event_log)
        ctx = asyncio.run(build_event_log_context(None, coord, hours=24))
        assert "showing the most recent 200" in ctx

    def test_no_limited_note_when_under_200(self):
        now = datetime.datetime.now(datetime.UTC)
        event_log = [
            {"type": "fan_activated", "time": (now - datetime.timedelta(minutes=i)).isoformat()} for i in range(10)
        ]
        coord = _make_coordinator(event_log=event_log)
        ctx = asyncio.run(build_event_log_context(None, coord, hours=24))
        assert "showing the most recent 200" not in ctx
