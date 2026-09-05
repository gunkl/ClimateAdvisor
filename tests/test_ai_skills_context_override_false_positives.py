"""Tests for the Issue #205 override false-positive detector (Issue #563 Phase 2).

Previously this pattern ("an override_detected event within 60 seconds of an
automation-initiated event is a known false positive, not a real override") was
encoded as ~15 lines of prompt text the model had to re-derive from raw event
timestamps every run. `_build_known_override_false_positives()` computes the exact
match deterministically instead, so the model cites a pre-verified fact rather than
re-doing 60-second-window arithmetic itself. Note: this is a distinct check from
`_build_timing_correlations()`, which matches against known ~30/90/5/10-minute
automation cycle periods (a different pattern) — the two are not interchangeable.

Issue #840: `_build_known_override_false_positives()` now takes `hours`/`now` and
filters via `filter_events_by_window()` before the sort+bisect scan, matching the
three sibling functions already fixed under Issue #432. Tests below pass a wide
`hours` window and a `now` at/after the latest fixture timestamp so all existing
fixtures remain in-window and assertions are unaffected by the windowing itself;
new tests specifically exercise the window boundary and the optimized-vs-naive
equivalence and volume behavior.
"""

from __future__ import annotations

import datetime
import random
import time

from custom_components.climate_advisor.ai_skills_context import (
    _OVERRIDE_FALSE_POSITIVE_WINDOW_S,
    _build_known_override_false_positives,
    _is_issue_205_automation_event,
)

_BASE = datetime.datetime(2026, 7, 10, 14, 0, 0, tzinfo=datetime.UTC)
_WIDE_HOURS = 168
_NOW = _BASE + datetime.timedelta(hours=1)


def _evt(event_type: str, offset_seconds: float, **extra) -> dict:
    return {"type": event_type, "time": _BASE + datetime.timedelta(seconds=offset_seconds), **extra}


def _build(events: list, hours: float = _WIDE_HOURS, now: datetime.datetime = _NOW) -> str:
    return _build_known_override_false_positives(events, hours, now)


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
        ctx = _build(events)
        assert "known false override" in ctx
        assert "Issue #205" in ctx

    def test_override_immediately_before_automation_event_matches(self):
        events = [_evt("override_detected", 0), _evt("classification_applied", 45)]
        ctx = _build(events)
        assert "known false override" in ctx

    def test_exactly_at_60_second_boundary_matches(self):
        events = [_evt("ceiling_guard_fired", 0), _evt("override_detected", 60)]
        ctx = _build(events)
        assert "known false override" in ctx

    def test_just_past_60_second_boundary_does_not_match(self):
        events = [_evt("ceiling_guard_fired", 0), _evt("override_detected", 61)]
        ctx = _build(events)
        assert "None detected in this window." in ctx

    def test_override_far_from_any_automation_event_does_not_match(self):
        events = [_evt("nat_vent_started", 0), _evt("override_detected", 3600)]
        ctx = _build(events)
        assert "None detected in this window." in ctx

    def test_grace_started_manual_sourced_does_not_count_as_automation(self):
        events = [_evt("grace_started", 0, source="manual"), _evt("override_detected", 20)]
        ctx = _build(events)
        assert "None detected in this window." in ctx

    def test_grace_started_automation_sourced_matches(self):
        events = [_evt("grace_started", 0, source="automation"), _evt("override_detected", 20)]
        ctx = _build(events)
        assert "known false override" in ctx

    def test_empty_events_returns_none_detected(self):
        ctx = _build([])
        assert "None detected in this window." in ctx

    def test_malformed_entries_are_skipped_without_crashing(self):
        events = ["not a dict", None, 42, _evt("override_detected", 0)]
        ctx = _build(events)
        assert "None detected in this window." in ctx

    def test_unrelated_automation_event_does_not_match(self):
        events = [_evt("fan_activated", 0), _evt("override_detected", 5)]
        ctx = _build(events)
        assert "None detected in this window." in ctx


class TestWindowing:
    """Issue #840: events outside the `hours`/`now` window must be excluded."""

    def test_event_outside_window_is_excluded_from_matching(self):
        # Both events would match (30s apart) if considered, but they occurred
        # far in the past relative to `now` — outside a narrow 1-hour window.
        old_base = _NOW - datetime.timedelta(hours=10)
        events = [
            {"type": "nat_vent_started", "time": old_base},
            {"type": "override_detected", "time": old_base + datetime.timedelta(seconds=30)},
        ]
        ctx = _build_known_override_false_positives(events, hours=1, now=_NOW)
        assert "None detected in this window." in ctx

    def test_event_inside_window_still_matches(self):
        recent_base = _NOW - datetime.timedelta(minutes=30)
        events = [
            {"type": "nat_vent_started", "time": recent_base},
            {"type": "override_detected", "time": recent_base + datetime.timedelta(seconds=30)},
        ]
        ctx = _build_known_override_false_positives(events, hours=1, now=_NOW)
        assert "known false override" in ctx


def _brute_force_matches(events: list, hours: float, now: datetime.datetime) -> int:
    """Independent oracle: naive O(n*m) scan mirroring the pre-Issue-840 logic.

    Deliberately re-implemented here (not imported from production) since this is
    the test's own ground truth for the equivalence check below.
    """
    cutoff = now - datetime.timedelta(hours=hours)
    resolved = []
    for entry in events:
        if not isinstance(entry, dict):
            continue
        raw_time = entry.get("time")
        if isinstance(raw_time, datetime.datetime):
            dt = raw_time if raw_time.tzinfo else raw_time.replace(tzinfo=datetime.UTC)
        else:
            continue
        if dt >= cutoff:
            resolved.append((dt, entry))

    automation_events = [(dt, e) for dt, e in resolved if _is_issue_205_automation_event(e)]
    override_events = [(dt, e) for dt, e in resolved if str(e.get("type", "")) == "override_detected"]

    match_count = 0
    for evt_dt, _evt in override_events:
        for auto_dt, _auto_evt in automation_events:
            if abs((evt_dt - auto_dt).total_seconds()) <= _OVERRIDE_FALSE_POSITIVE_WINDOW_S:
                match_count += 1
                break
    return match_count


class TestBisectEquivalenceAndVolume:
    def test_randomized_matches_equal_brute_force_oracle(self):
        rng = random.Random(840)
        now = _BASE + datetime.timedelta(hours=48)
        events = []
        automation_types = ["nat_vent_started", "ceiling_guard_fired", "classification_applied"]
        for _ in range(150):
            offset = rng.uniform(0, 47 * 3600)
            etype = rng.choice(automation_types)
            events.append({"type": etype, "time": _BASE + datetime.timedelta(seconds=offset)})
        for _ in range(150):
            offset = rng.uniform(0, 47 * 3600)
            events.append({"type": "override_detected", "time": _BASE + datetime.timedelta(seconds=offset)})
        rng.shuffle(events)

        expected = _brute_force_matches(events, hours=168, now=now)
        ctx = _build_known_override_false_positives(events, hours=168, now=now)
        actual = ctx.count("known false override")
        assert actual == expected, f"expected {expected} matches, got {actual}\n{ctx}"

    def test_volume_completes_quickly(self):
        rng = random.Random(841)
        now = _BASE + datetime.timedelta(hours=48)
        events = []
        automation_types = ["nat_vent_started", "ceiling_guard_fired", "classification_applied"]
        for _ in range(1800):
            offset = rng.uniform(0, 47 * 3600)
            etype = rng.choice(automation_types)
            events.append({"type": etype, "time": _BASE + datetime.timedelta(seconds=offset)})
        for _ in range(1800):
            offset = rng.uniform(0, 47 * 3600)
            events.append({"type": "override_detected", "time": _BASE + datetime.timedelta(seconds=offset)})
        rng.shuffle(events)

        start = time.perf_counter()
        ctx = _build_known_override_false_positives(events, hours=168, now=now)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.3, f"scan took {elapsed:.3f}s — expected well under 0.3s (bisect-based, not O(n*m))"
        assert "KNOWN OVERRIDE FALSE POSITIVES" in ctx
