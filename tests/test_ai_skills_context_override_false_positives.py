"""Tests for the Issue #205 override false-positive detector (Issue #563 Phase 2).

Previously this pattern ("an override_detected event within 60 seconds of an
automation-initiated event is a known false positive, not a real override") was
encoded as ~15 lines of prompt text the model had to re-derive from raw event
timestamps every run. `_build_known_override_false_positives()` computes the exact
match deterministically instead, so the model cites a pre-verified fact rather than
re-doing 60-second-window arithmetic itself. Note: this is a distinct check from
`_build_timing_correlations()`, which matches against known ~30/90/5/10-minute
automation cycle periods (a different pattern) — the two are not interchangeable.
"""

from __future__ import annotations

import datetime

from custom_components.climate_advisor.ai_skills_context import (
    _build_known_override_false_positives,
    _is_issue_205_automation_event,
)

_BASE = datetime.datetime(2026, 7, 10, 14, 0, 0, tzinfo=datetime.UTC)


def _evt(event_type: str, offset_seconds: float, **extra) -> dict:
    return {"type": event_type, "time": _BASE + datetime.timedelta(seconds=offset_seconds), **extra}


class TestIsIssue205AutomationEvent:
    def test_nat_vent_prefixed_types_match(self):
        assert _is_issue_205_automation_event({"type": "nat_vent_started"}) is True
        assert _is_issue_205_automation_event({"type": "nat_vent_comfort_floor_exit"}) is True

    def test_ceiling_guard_and_classification_match(self):
        assert _is_issue_205_automation_event({"type": "ceiling_guard_fired"}) is True
        assert _is_issue_205_automation_event({"type": "classification_applied"}) is True

    def test_grace_started_only_matches_when_automation_sourced(self):
        assert _is_issue_205_automation_event({"type": "grace_started", "source": "automation"}) is True
        assert _is_issue_205_automation_event({"type": "grace_started", "source": "manual"}) is False
        assert _is_issue_205_automation_event({"type": "grace_started"}) is False

    def test_unrelated_type_does_not_match(self):
        assert _is_issue_205_automation_event({"type": "fan_activated"}) is False


class TestBuildKnownOverrideFalsePositives:
    def test_override_immediately_after_automation_event_matches(self):
        events = [_evt("nat_vent_started", 0), _evt("override_detected", 30)]
        ctx = _build_known_override_false_positives(events)
        assert "known false override" in ctx
        assert "Issue #205" in ctx

    def test_override_immediately_before_automation_event_matches(self):
        events = [_evt("override_detected", 0), _evt("classification_applied", 45)]
        ctx = _build_known_override_false_positives(events)
        assert "known false override" in ctx

    def test_exactly_at_60_second_boundary_matches(self):
        events = [_evt("ceiling_guard_fired", 0), _evt("override_detected", 60)]
        ctx = _build_known_override_false_positives(events)
        assert "known false override" in ctx

    def test_just_past_60_second_boundary_does_not_match(self):
        events = [_evt("ceiling_guard_fired", 0), _evt("override_detected", 61)]
        ctx = _build_known_override_false_positives(events)
        assert "None detected in this window." in ctx

    def test_override_far_from_any_automation_event_does_not_match(self):
        events = [_evt("nat_vent_started", 0), _evt("override_detected", 3600)]
        ctx = _build_known_override_false_positives(events)
        assert "None detected in this window." in ctx

    def test_grace_started_manual_sourced_does_not_count_as_automation(self):
        events = [_evt("grace_started", 0, source="manual"), _evt("override_detected", 20)]
        ctx = _build_known_override_false_positives(events)
        assert "None detected in this window." in ctx

    def test_grace_started_automation_sourced_matches(self):
        events = [_evt("grace_started", 0, source="automation"), _evt("override_detected", 20)]
        ctx = _build_known_override_false_positives(events)
        assert "known false override" in ctx

    def test_empty_events_returns_none_detected(self):
        ctx = _build_known_override_false_positives([])
        assert "None detected in this window." in ctx

    def test_malformed_entries_are_skipped_without_crashing(self):
        events = ["not a dict", None, 42, _evt("override_detected", 0)]
        ctx = _build_known_override_false_positives(events)
        assert "None detected in this window." in ctx

    def test_unrelated_automation_event_does_not_match(self):
        events = [_evt("fan_activated", 0), _evt("override_detected", 5)]
        ctx = _build_known_override_false_positives(events)
        assert "None detected in this window." in ctx
