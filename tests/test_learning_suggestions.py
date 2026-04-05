"""Tests for LearningEngine.record_feedback() and generate_suggestions() suppression (Issue #84).

Covers:
- record_feedback() stores a feedback entry in settings_history
- record_feedback() caps settings_history at 200 entries
- generate_suggestions() suppresses a suggestion after "incorrect" feedback within 30 days
- generate_suggestions() does NOT suppress after "correct" feedback
- Feedback older than 30 days does not block suggestion generation
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from custom_components.climate_advisor.learning import LearningEngine, LearningState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = "2026-04-05"


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine._state = LearningState()
    return engine


def _seed_window_compliance_records(engine: LearningEngine, count: int = 14) -> None:
    """Seed `count` DailyRecord-shaped dicts that trigger low_window_compliance.

    All records have windows_recommended=True and windows_opened=False (0% compliance),
    which exceeds the 7-day minimum and falls below the 30% threshold.
    """
    for i in range(count):
        engine._state.records.append(
            {
                "date": f"2026-03-{i + 1:02d}",
                "day_type": "warm",
                "trend_direction": "stable",
                "windows_recommended": True,
                "windows_opened": False,
                "occupancy_mode": "home",
                "hvac_runtime_minutes": 60.0,
                "manual_overrides": 0,
                "override_details": [],
                "door_window_pause_events": 0,
                "door_pause_by_sensor": {},
                "comfort_violations_minutes": 0.0,
                "occupancy_away_minutes": 0.0,
            }
        )


# ---------------------------------------------------------------------------
# TestRecordFeedback
# ---------------------------------------------------------------------------


class TestRecordFeedback:
    """Tests for LearningEngine.record_feedback()."""

    def test_record_feedback_correct(self, tmp_path: Path):
        """record_feedback('correct') appends a feedback entry to settings_history."""
        engine = _make_engine(tmp_path)

        engine.record_feedback("low_window_compliance", "correct")

        assert len(engine._state.settings_history) == 1
        entry = engine._state.settings_history[0]
        assert entry["type"] == "feedback"
        assert entry["suggestion"] == "low_window_compliance"
        assert entry["verdict"] == "correct"
        assert "timestamp" in entry

    def test_record_feedback_incorrect(self, tmp_path: Path):
        """record_feedback('incorrect') appends a feedback entry with verdict='incorrect'."""
        engine = _make_engine(tmp_path)

        engine.record_feedback("low_window_compliance", "incorrect")

        assert len(engine._state.settings_history) == 1
        entry = engine._state.settings_history[0]
        assert entry["type"] == "feedback"
        assert entry["suggestion"] == "low_window_compliance"
        assert entry["verdict"] == "incorrect"

    def test_record_feedback_caps_history(self, tmp_path: Path):
        """After 199 existing entries, adding one more stays at 200 (not 201)."""
        engine = _make_engine(tmp_path)

        # Seed 199 existing entries
        for _i in range(199):
            engine._state.settings_history.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "type": "feedback",
                    "suggestion": "some_key",
                    "verdict": "correct",
                }
            )

        engine.record_feedback("low_window_compliance", "incorrect")

        assert len(engine._state.settings_history) == 200

    def test_record_feedback_trims_when_over_cap(self, tmp_path: Path):
        """When settings_history exceeds 200, the oldest entries are dropped."""
        engine = _make_engine(tmp_path)

        # Seed 200 entries first
        for _i in range(200):
            engine._state.settings_history.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "type": "feedback",
                    "suggestion": "old_key",
                    "verdict": "correct",
                }
            )

        # Adding one more should trim to 200 — keeping the newest entries
        engine.record_feedback("new_key", "incorrect")

        assert len(engine._state.settings_history) == 200
        # The new entry should be the last one
        assert engine._state.settings_history[-1]["suggestion"] == "new_key"


# ---------------------------------------------------------------------------
# TestGenerateSuggestionsSuppression
# ---------------------------------------------------------------------------


class TestGenerateSuggestionsSuppression:
    """Tests for generate_suggestions() suppression via feedback history."""

    def test_suggestion_suppressed_after_incorrect_feedback(self, tmp_path: Path):
        """An 'incorrect' feedback within 30 days suppresses that suggestion."""
        engine = _make_engine(tmp_path)
        _seed_window_compliance_records(engine)

        # Record incorrect feedback with a recent timestamp
        engine._state.settings_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "type": "feedback",
                "suggestion": "low_window_compliance",
                "verdict": "incorrect",
            }
        )

        keys = engine.get_last_suggestion_keys()
        # generate_suggestions() updates _last_suggestion_keys
        engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()

        assert "low_window_compliance" not in keys

    def test_suggestion_not_suppressed_after_correct_feedback(self, tmp_path: Path):
        """A 'correct' feedback does NOT suppress the suggestion."""
        engine = _make_engine(tmp_path)
        _seed_window_compliance_records(engine)

        # Record correct feedback — this should NOT suppress
        engine._state.settings_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "type": "feedback",
                "suggestion": "low_window_compliance",
                "verdict": "correct",
            }
        )

        engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()

        assert "low_window_compliance" in keys

    def test_suppression_respects_30_day_cutoff(self, tmp_path: Path):
        """Incorrect feedback older than 30 days does NOT suppress the suggestion."""
        engine = _make_engine(tmp_path)
        _seed_window_compliance_records(engine)

        # Record incorrect feedback with a timestamp 31 days ago
        stale_ts = (datetime.now() - timedelta(days=31)).isoformat()
        engine._state.settings_history.append(
            {
                "timestamp": stale_ts,
                "type": "feedback",
                "suggestion": "low_window_compliance",
                "verdict": "incorrect",
            }
        )

        engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()

        assert "low_window_compliance" in keys

    def test_suppression_does_not_affect_other_suggestions(self, tmp_path: Path):
        """Incorrect feedback for one key does not suppress a different suggestion key."""
        engine = _make_engine(tmp_path)
        _seed_window_compliance_records(engine)

        # Mark a DIFFERENT key as incorrect
        engine._state.settings_history.append(
            {
                "timestamp": datetime.now().isoformat(),
                "type": "feedback",
                "suggestion": "frequent_overrides",
                "verdict": "incorrect",
            }
        )

        engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()

        # low_window_compliance is unaffected by feedback on a different key
        assert "low_window_compliance" in keys

    def test_recently_dismissed_still_suppresses_independently(self, tmp_path: Path):
        """A dismissed suggestion is also suppressed regardless of feedback."""
        engine = _make_engine(tmp_path)
        _seed_window_compliance_records(engine)

        engine.dismiss_suggestion("low_window_compliance")

        engine.generate_suggestions()
        keys = engine.get_last_suggestion_keys()

        assert "low_window_compliance" not in keys
