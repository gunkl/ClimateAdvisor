"""Tests for confirmed_transition.py (Issue #821 Design §0) — the shared
sustain-confirmation primitive consumed by nat-vent's exit-reason chain and the
comfort-family switch lockout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.confirmed_transition import (
    is_confirmed,
    resolve_candidate_since,
)

_T0 = datetime(2026, 8, 31, 13, 0, 0, tzinfo=UTC)


class TestResolveCandidateSince:
    def test_no_candidate_returns_none(self):
        assert resolve_candidate_since(candidate=None, previous_candidate="x", previous_since=_T0, now=_T0) is None

    def test_fresh_candidate_resets_to_now(self):
        later = _T0 + timedelta(seconds=30)
        result = resolve_candidate_since(candidate="a", previous_candidate=None, previous_since=None, now=later)
        assert result == later

    def test_candidate_changed_resets_to_now(self):
        later = _T0 + timedelta(seconds=30)
        result = resolve_candidate_since(candidate="b", previous_candidate="a", previous_since=_T0, now=later)
        assert result == later

    def test_same_candidate_keeps_previous_since(self):
        later = _T0 + timedelta(seconds=30)
        result = resolve_candidate_since(candidate="a", previous_candidate="a", previous_since=_T0, now=later)
        assert result == _T0

    def test_cold_start_same_candidate_but_no_prior_since(self):
        """previous_since=None with a matching previous_candidate is a degenerate
        cold-start case — resolve_candidate_since keeps None (caller passed no
        timestamp yet); is_confirmed treats None as "just started"."""
        result = resolve_candidate_since(candidate="a", previous_candidate="a", previous_since=None, now=_T0)
        assert result is None


class TestIsConfirmed:
    def test_no_candidate_never_confirmed(self):
        assert is_confirmed(candidate=None, candidate_since=_T0, now=_T0, sustain_seconds=90) is False

    def test_no_timestamp_never_confirmed(self):
        assert is_confirmed(candidate="a", candidate_since=None, now=_T0, sustain_seconds=90) is False

    def test_not_yet_sustained(self):
        now = _T0 + timedelta(seconds=45)
        assert is_confirmed(candidate="a", candidate_since=_T0, now=now, sustain_seconds=90) is False

    def test_exactly_at_boundary_confirmed(self):
        now = _T0 + timedelta(seconds=90)
        assert is_confirmed(candidate="a", candidate_since=_T0, now=now, sustain_seconds=90) is True

    def test_past_boundary_confirmed(self):
        now = _T0 + timedelta(seconds=200)
        assert is_confirmed(candidate="a", candidate_since=_T0, now=now, sustain_seconds=90) is True

    def test_zero_sustain_confirms_immediately(self):
        assert is_confirmed(candidate="a", candidate_since=_T0, now=_T0, sustain_seconds=0) is True

    def test_negative_elapsed_not_confirmed(self):
        """Defensive: candidate_since somehow in the future (clock skew) — never
        confirms early."""
        now = _T0 - timedelta(seconds=10)
        assert is_confirmed(candidate="a", candidate_since=_T0, now=now, sustain_seconds=90) is False


class TestIntegratedSustainWorkflow:
    """Simulates a caller's tick-by-tick usage: reset -> candidate appears ->
    sustained -> confirmed; candidate flips before confirming -> clock restarts."""

    def test_sustained_candidate_confirms_after_window(self):
        since = None
        prev_candidate = None
        candidate = "reason_a"
        t = _T0
        since = resolve_candidate_since(
            candidate=candidate, previous_candidate=prev_candidate, previous_since=since, now=t
        )
        assert is_confirmed(candidate=candidate, candidate_since=since, now=t, sustain_seconds=90) is False

        prev_candidate = candidate
        t2 = t + timedelta(seconds=91)
        since = resolve_candidate_since(
            candidate=candidate, previous_candidate=prev_candidate, previous_since=since, now=t2
        )
        assert is_confirmed(candidate=candidate, candidate_since=since, now=t2, sustain_seconds=90) is True

    def test_candidate_flip_restarts_clock(self):
        since = _T0
        prev_candidate = "reason_a"
        t2 = _T0 + timedelta(seconds=80)  # not yet confirmed for reason_a
        # candidate flips to reason_b before reason_a confirms
        since2 = resolve_candidate_since(
            candidate="reason_b", previous_candidate=prev_candidate, previous_since=since, now=t2
        )
        assert since2 == t2  # clock restarted
        assert is_confirmed(candidate="reason_b", candidate_since=since2, now=t2, sustain_seconds=90) is False

    def test_condition_clears_then_reappears_is_timed_fresh(self):
        """Caller clears state to None when candidate reads NONE; a later
        reappearance of the same value must not resume the old timer."""
        since = _T0
        # condition clears
        since_after_clear = resolve_candidate_since(
            candidate=None, previous_candidate="reason_a", previous_since=since, now=_T0 + timedelta(seconds=10)
        )
        assert since_after_clear is None
        # reappears later
        t3 = _T0 + timedelta(seconds=200)
        since_fresh = resolve_candidate_since(
            candidate="reason_a", previous_candidate=None, previous_since=since_after_clear, now=t3
        )
        assert since_fresh == t3
        assert is_confirmed(candidate="reason_a", candidate_since=since_fresh, now=t3, sustain_seconds=90) is False
