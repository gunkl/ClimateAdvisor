"""Tests for the restart-cause summary (Issue #563 Phase 2).

`coordinator.py`'s restart-cause classification (Issue #403/#413) already
distinguishes benign restarts (user_restart, version_changed) from crash-like ones
(unknown), but nothing previously surfaced that breakdown to the AI Investigator —
only a raw `system_restarted` count was visible, which is how a day of routine
deploys could get narrated as an alarming number of restarts. These tests prove
the summary correctly separates benign from crash-like causes.
"""

from __future__ import annotations

from custom_components.climate_advisor.ai_skills_context import _build_restart_summary


class TestBuildRestartSummary:
    def test_no_restarts_in_window(self):
        ctx = _build_restart_summary([])
        assert "No restarts in this window." in ctx

    def test_all_benign_restarts_shown_with_breakdown(self):
        events = [
            {"type": "system_restarted", "cause": "user_restart", "time": "10:00"},
            {"type": "system_restarted", "cause": "version_changed", "time": "11:00"},
        ]
        ctx = _build_restart_summary(events)
        assert "user_restart=1" in ctx
        assert "version_changed=1" in ctx

    def test_unknown_cause_timestamps_are_listed(self):
        events = [
            {"type": "system_restarted", "cause": "unknown", "time": "03:15"},
        ]
        ctx = _build_restart_summary(events)
        assert "cause=unknown restart timestamps: 03:15" in ctx

    def test_missing_cause_field_defaults_to_unknown(self):
        events = [{"type": "system_restarted", "time": "03:15"}]
        ctx = _build_restart_summary(events)
        assert "unknown=1" in ctx

    def test_instructs_model_not_to_narrate_benign_restarts_as_problems(self):
        events = [{"type": "system_restarted", "cause": "version_changed", "time": "10:00"}]
        ctx = _build_restart_summary(events)
        assert "do not narrate them as problems" in ctx

    def test_non_restart_events_are_ignored(self):
        events = [
            {"type": "fan_activated", "time": "09:00"},
            {"type": "system_restarted", "cause": "user_restart", "time": "10:00"},
        ]
        ctx = _build_restart_summary(events)
        assert "1 restart(s) in this window" in ctx

    def test_malformed_entries_are_skipped_without_crashing(self):
        events = ["not a dict", None, {"type": "system_restarted", "cause": "unknown", "time": "x"}]
        ctx = _build_restart_summary(events)
        assert "unknown=1" in ctx
