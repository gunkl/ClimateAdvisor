"""Tests for LearningEngine I/O decoupling (Issue #20).

Verifies that LearningEngine no longer performs file I/O implicitly —
callers must explicitly invoke load_state() / save_state().
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.climate_advisor.const import LEARNING_DB_FILE
from custom_components.climate_advisor.learning import (
    DailyRecord,
    LearningEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(date: str = "2026-03-18", **overrides) -> DailyRecord:
    defaults = dict(day_type="mild", trend_direction="stable")
    defaults.update(overrides)
    return DailyRecord(date=date, **defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitNoIO:
    """Constructor must NOT touch the filesystem."""

    def test_starts_with_empty_state(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        assert engine.generate_suggestions() == []
        assert engine.get_last_suggestion_keys() == []

    def test_ignores_existing_file_until_load(self, tmp_path: Path):
        """A pre-existing DB file should not be read until load_state()."""
        db_path = tmp_path / LEARNING_DB_FILE
        db_path.write_text(
            json.dumps(
                {
                    "records": [{"date": "2026-03-01", "day_type": "hot", "trend_direction": "warming"}],
                    "active_suggestions": [],
                    "dismissed_suggestions": [],
                    "settings_history": [],
                }
            )
        )

        engine = LearningEngine(tmp_path)
        # State is empty — file not read yet
        assert engine.get_compliance_summary()["days_recorded"] == 0

        engine.load_state()
        assert engine.get_compliance_summary()["days_recorded"] == 1


class TestLoadState:
    """load_state() reads from disk into memory."""

    def test_loads_records(self, tmp_path: Path):
        db_path = tmp_path / LEARNING_DB_FILE
        db_path.write_text(
            json.dumps(
                {
                    "records": [
                        {"date": "2026-03-01", "day_type": "hot", "trend_direction": "warming"},
                        {"date": "2026-03-02", "day_type": "mild", "trend_direction": "stable"},
                    ],
                    "active_suggestions": [],
                    "dismissed_suggestions": [],
                    "settings_history": [],
                }
            )
        )

        engine = LearningEngine(tmp_path)
        engine.load_state()
        assert engine.get_compliance_summary()["days_recorded"] == 2

    def test_handles_missing_file(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        engine.load_state()  # Should not raise
        assert engine.get_compliance_summary()["days_recorded"] == 0

    def test_handles_corrupt_file(self, tmp_path: Path):
        db_path = tmp_path / LEARNING_DB_FILE
        db_path.write_text("not valid json {{{")

        engine = LearningEngine(tmp_path)
        engine.load_state()  # Should not raise, falls back to empty
        assert engine.get_compliance_summary()["days_recorded"] == 0


class TestSaveState:
    """save_state() writes memory to disk."""

    def test_writes_file(self, tmp_path: Path):
        db_path = tmp_path / LEARNING_DB_FILE
        engine = LearningEngine(tmp_path)
        engine.record_day(_make_record())
        assert not db_path.exists()  # record_day must NOT write

        engine.save_state()
        assert db_path.exists()

        data = json.loads(db_path.read_text())
        assert len(data["records"]) == 1
        assert data["records"][0]["date"] == "2026-03-18"

    def test_record_day_does_not_write(self, tmp_path: Path):
        """record_day only mutates in-memory state — no disk I/O."""
        db_path = tmp_path / LEARNING_DB_FILE
        engine = LearningEngine(tmp_path)
        engine.record_day(_make_record())
        assert not db_path.exists()

    def test_dismiss_does_not_write(self, tmp_path: Path):
        db_path = tmp_path / LEARNING_DB_FILE
        engine = LearningEngine(tmp_path)
        engine.dismiss_suggestion("some_key")
        assert not db_path.exists()

    def test_accept_does_not_write(self, tmp_path: Path):
        db_path = tmp_path / LEARNING_DB_FILE
        engine = LearningEngine(tmp_path)
        engine.accept_suggestion("frequent_overrides")
        assert not db_path.exists()


class TestRoundTrip:
    """Data survives save → new engine → load."""

    def test_record_round_trip(self, tmp_path: Path):
        engine1 = LearningEngine(tmp_path)
        engine1.record_day(_make_record("2026-03-10", day_type="hot"))
        engine1.record_day(_make_record("2026-03-11", day_type="cold"))
        engine1.save_state()

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()
        summary = engine2.get_compliance_summary()
        assert summary["days_recorded"] == 2

    def test_dismissed_suggestions_round_trip(self, tmp_path: Path):
        engine1 = LearningEngine(tmp_path)
        engine1.dismiss_suggestion("low_window_compliance")
        engine1.save_state()

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()
        # The dismissed key should survive the round-trip
        # (internal state check via generate_suggestions behavior)
        assert "low_window_compliance" not in engine2.generate_suggestions()


class TestComfortScoreCalculation:
    """Tests for get_compliance_summary() comfort_score calculation."""

    def test_perfect_score_when_no_violations(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        # Add 3 records with 0 violations
        for i in range(3):
            engine._state.records.append(
                {
                    "date": f"2026-03-{i + 1:02d}",
                    "day_type": "mild",
                    "comfort_violations_minutes": 0.0,
                }
            )
        summary = engine.get_compliance_summary()
        assert summary["comfort_score"] == 1.0

    def test_zero_score_when_fully_violated(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        for i in range(3):
            engine._state.records.append(
                {
                    "date": f"2026-03-{i + 1:02d}",
                    "day_type": "mild",
                    "comfort_violations_minutes": 1440.0,
                }
            )
        summary = engine.get_compliance_summary()
        assert summary["comfort_score"] == pytest.approx(0.0)

    def test_partial_violations_single_day(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        engine._state.records.append(
            {
                "date": "2026-03-01",
                "day_type": "mild",
                "comfort_violations_minutes": 720.0,
            }
        )
        summary = engine.get_compliance_summary()
        assert summary["comfort_score"] == pytest.approx(0.5)

    def test_score_across_multiple_days(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        # Day 1: 0 violations, Day 2: 720 violations, Day 3: 1440 violations
        # total = 2160 / (3 * 1440) = 0.5 violated → score = 0.5
        for violations in [0.0, 720.0, 1440.0]:
            engine._state.records.append(
                {
                    "date": "2026-03-01",
                    "day_type": "mild",
                    "comfort_violations_minutes": violations,
                }
            )
        summary = engine.get_compliance_summary()
        assert summary["comfort_score"] == pytest.approx(0.5)

    def test_uses_only_last_14_days_when_more_exist(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        # 20 records: first 6 have violations (should be excluded), last 14 have 0
        for i in range(6):
            engine._state.records.append(
                {
                    "date": f"2026-02-{i + 1:02d}",
                    "day_type": "mild",
                    "comfort_violations_minutes": 1440.0,
                }
            )
        for i in range(14):
            engine._state.records.append(
                {
                    "date": f"2026-03-{i + 1:02d}",
                    "day_type": "mild",
                    "comfort_violations_minutes": 0.0,
                }
            )
        summary = engine.get_compliance_summary()
        assert summary["comfort_score"] == pytest.approx(1.0)

    def test_comfort_score_key_present_in_summary(self, tmp_path):
        engine = LearningEngine(tmp_path)
        engine.load_state()
        engine._state.records.append(
            {
                "date": "2026-03-01",
                "day_type": "mild",
                "comfort_violations_minutes": 30.0,
            }
        )
        summary = engine.get_compliance_summary()
        assert "comfort_score" in summary
        assert 0.0 <= summary["comfort_score"] <= 1.0
