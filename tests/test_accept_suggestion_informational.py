"""Tests for accept_suggestion() informational-key handling (Issue #874, Fix 6).

`thermal_model_ready` and `forecast_bias_significant` are informational-only
suggestions per docs/05-LEARNING-ENGINE-DESIGN.md's suggestion table — accepting
either applies no config change, but they ARE recognized keys and must not fall
through to the "not recognized" WARNING that's reserved for genuine typos/unknown
keys (regression guard: `accept_suggestion("not_a_real_key")` must still warn).
"""

from __future__ import annotations

import logging

import pytest

from custom_components.climate_advisor.learning import LearningEngine

LEARNING_LOGGER = "custom_components.climate_advisor.learning"


class TestAcceptSuggestionInformationalKeys:
    def test_thermal_model_ready_returns_empty_no_warning(self, tmp_path, caplog):
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion("thermal_model_ready")
        assert result == {}
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_forecast_bias_significant_returns_empty_no_warning(self, tmp_path, caplog):
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion("forecast_bias_significant")
        assert result == {}
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_high_runtime_mild_days_returns_empty_with_info_no_warning(self, tmp_path, caplog):
        """`high_runtime_mild_days` has no single deterministic config change (it asks
        the user to either add sensors or adjust setpoints) — treated like the other
        informational-only keys, but must still log an INFO acceptance line (Issue #874
        Observability Requirements: "INFO at each decision outcome")."""
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion("high_runtime_mild_days")
        assert result == {}
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
        assert any(r.levelno == logging.INFO and "high_runtime_mild_days" in r.message for r in caplog.records)

    def test_unrecognized_key_still_warns(self, tmp_path, caplog):
        """Regression guard: genuine typo/unknown keys must still be flagged."""
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion("not_a_real_key")
        assert result == {}
        assert any(r.levelno == logging.WARNING and "not recognized" in r.message for r in caplog.records)

    @pytest.mark.parametrize("key", ["frequent_door_pauses", "comfort_violations"])
    def test_removed_keys_are_unknown(self, tmp_path, caplog, key):
        """Issue #1652 (owner decision): these two suggestions were removed; accepting one records nothing."""
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion(key)
        assert result == {}
        assert engine._state.settings_history == []
        assert any(r.levelno == logging.WARNING and "not recognized" in r.message for r in caplog.records)

    def test_frequent_overrides_is_advice_only(self, tmp_path, caplog):
        """Issue #1652: it used to return the fake key request_setpoint_analysis, which nothing read."""
        engine = LearningEngine(tmp_path)
        with caplog.at_level(logging.INFO, logger=LEARNING_LOGGER):
            result = engine.accept_suggestion("frequent_overrides")
        assert result == {}
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
