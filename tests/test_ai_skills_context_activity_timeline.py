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

import custom_components.climate_advisor.ai_skills_context as _ctx_mod
from custom_components.climate_advisor.ai_skills_context import (
    _group_timeline_sessions,
    build_activity_sessions_context,
    build_activity_timeline_context,
    build_event_log_context,
    get_provider_registry,
)

_NOW = datetime.datetime(2026, 7, 10, 14, 0, 0, tzinfo=datetime.UTC)

# Patch dt_util.as_local to be identity so real-datetime arithmetic in
# _group_timeline_sessions() uses actual datetimes rather than MagicMock objects
# (same pattern as test_activity_renderers.py).
_as_local_identity = patch.object(_ctx_mod.dt_util, "as_local", side_effect=lambda x: x)


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
        # Should not raise even with an out-of-range value; clamped to [1, 168].
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_activity_timeline_context(None, coord, hours=99999))
        assert "=== ACTIVITY TIMELINE" in ctx

    def test_registered_in_provider_registry(self):
        registry = get_provider_registry()
        names = [p.name for p in registry.select()]
        assert "activity_timeline" in names


class TestGroupTimelineSessions:
    """Tests for _group_timeline_sessions() and build_activity_sessions_context()
    (Issue #925) — the deterministic input for the LLM-authored ACTIVITY SUMMARY."""

    def test_empty_event_log_returns_no_sessions(self):
        sessions, limited = _group_timeline_sessions([], {"temp_unit": "fahrenheit"}, 24, _NOW)
        assert sessions == []
        assert limited is False

    def test_burst_of_different_types_collapses_into_one_session(self):
        """A tight burst of different event types within a few minutes must produce
        ONE session, not one entry per event — the direct regression test for the
        "multiple lines per same minute" complaint."""
        base = _NOW
        event_log = [
            {
                "type": "comfort_band_applied",
                "time": base,
                "mode": "cool",
                "floor": 64,
                "ceiling": 72,
                "active": "ceiling",
            },
            {"type": "fan_activated", "time": base + datetime.timedelta(minutes=1), "reason": "natural ventilation"},
            {"type": "fan_deactivated", "time": base + datetime.timedelta(minutes=3)},
            {"type": "classification_applied", "time": base + datetime.timedelta(minutes=4), "day_type": "warm"},
        ]
        with _as_local_identity:
            sessions, _limited = _group_timeline_sessions(
                event_log, {"temp_unit": "fahrenheit"}, 24, base + datetime.timedelta(minutes=10)
            )
        assert len(sessions) == 1
        assert sessions[0].event_count == 4

    def test_events_separated_by_a_quiet_gap_stay_separate(self):
        base = _NOW
        event_log = [
            {"type": "fan_activated", "time": base},
            {"type": "fan_deactivated", "time": base + datetime.timedelta(minutes=2)},
            # 40 minutes of quiet -- exceeds _SESSION_GAP_MINUTES (25)
            {"type": "override_detected", "time": base + datetime.timedelta(minutes=42)},
        ]
        with _as_local_identity:
            sessions, _limited = _group_timeline_sessions(
                event_log, {"temp_unit": "fahrenheit"}, 24, base + datetime.timedelta(minutes=50)
            )
        assert len(sessions) == 2

    def test_session_event_lines_exclude_settings_text_jargon(self):
        """Session event_lines carry only ev_text (already fairly plain), never the
        jargon-dense settings_text (e.g. "setpoint: 72°F Cool (64°F Heat)")."""
        event_log = [
            {
                "type": "comfort_band_applied",
                "time": _NOW,
                "mode": "cool",
                "floor": 64,
                "ceiling": 72,
                "active": "ceiling",
            },
        ]
        sessions, _limited = _group_timeline_sessions(
            event_log, {"temp_unit": "fahrenheit"}, 24, _NOW + datetime.timedelta(minutes=1)
        )
        assert len(sessions) == 1
        combined = " ".join(sessions[0].event_lines).lower()
        assert "setpoint:" not in combined

    def test_registered_in_provider_registry(self):
        registry = get_provider_registry()
        names = [p.name for p in registry.select()]
        assert "activity_sessions" in names

    def test_remote_timer_duration_surfaces_in_plain_form(self):
        """Issue #925 follow-up: a remote-armed fan timer's duration is real,
        useful, non-jargon information -- it must survive into event_lines even
        though it originates in settings_text (excluded above for jargon), via
        the narrow plain-fact whitelist."""
        event_log = [
            {
                "type": "fan_manual_override",
                "time": _NOW,
                "fan_before": "off",
                "fan_after": "on",
                "remote_timer_hours": 4,
            },
        ]
        sessions, _limited = _group_timeline_sessions(
            event_log, {"temp_unit": "fahrenheit"}, 24, _NOW + datetime.timedelta(minutes=1)
        )
        assert len(sessions) == 1
        combined = " ".join(sessions[0].event_lines)
        assert "4-hour timer" in combined
        assert "remote timer:" not in combined.lower()


class TestBuildActivitySessionsContext:
    def test_empty_event_log_produces_no_sessions_note(self):
        coord = _make_coordinator()
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_activity_sessions_context(None, coord))
        assert "=== ACTIVITY SESSIONS" in ctx
        assert "No activity in this window" in ctx

    def test_sessions_render_with_time_range_and_events(self):
        event_log = [
            {"type": "fan_activated", "time": _NOW - datetime.timedelta(minutes=2), "reason": "natural ventilation"},
            {"type": "fan_deactivated", "time": _NOW},
        ]
        coord = _make_coordinator(event_log=event_log)
        with (
            patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW),
            _as_local_identity,
        ):
            ctx = asyncio.run(build_activity_sessions_context(None, coord, hours=24))
        assert "SESSION 1:" in ctx
        assert "setpoint:" not in ctx.lower()


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
