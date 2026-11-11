"""Tests for learning_health being threaded through get_thermal_model() calls (Issue #468).

Three AI-context call sites (ai_skills_activity.py's swing acquisition,
ai_skills_context.py's build_learning_context() and
build_thermal_pipeline_context()) previously called get_thermal_model() with no
arguments, producing a structurally incomplete dict (learning_health always {})
that differed from the canonical shape used by coordinator.py and sensor.py's
ClimateAdvisorComplianceSensor for no principled reason. These tests prove the
fix: each site now passes learning_health matching coordinator._build_learning_health().
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills_context import (
    build_learning_context,
    build_thermal_pipeline_context,
)


def _make_coordinator(learning_health: dict) -> MagicMock:
    coord = MagicMock()
    coord._build_learning_health.return_value = learning_health
    coord.learning.get_compliance_summary.return_value = {}
    coord.learning.get_weather_bias.return_value = {}
    coord.learning.get_thermal_model.return_value = {
        "heating_rate_f_per_hour": 2.0,
        "cooling_rate_f_per_hour": 1.5,
        "confidence": "medium",
        "observation_count_heat": 5,
        "observation_count_cool": 3,
    }
    coord._build_thermal_pipeline_summary.return_value = {}
    coord.learning._state.thermal_model_cache = {}
    return coord


class TestBuildLearningContextThreadsLearningHealth:
    def test_get_thermal_model_called_with_learning_health(self):
        _health = {"hvac_heat": {"attempts": 3, "committed": 2}}
        coord = _make_coordinator(_health)
        hass = MagicMock()

        asyncio.run(build_learning_context(hass, coord))

        coord.learning.get_thermal_model.assert_called_once_with(learning_health=_health)

    def test_no_build_learning_health_attribute_falls_back_gracefully(self):
        """Coordinator without _build_learning_health (e.g. a bare stub) must not crash —
        falls back to an empty dict, matching the pre-#468 behavior for that edge case."""
        coord = _make_coordinator({})
        del coord._build_learning_health
        hass = MagicMock()

        ctx = asyncio.run(build_learning_context(hass, coord))

        assert "=== LEARNING — THERMAL MODEL ===" in ctx


class TestBuildThermalPipelineContextThreadsLearningHealth:
    def test_get_thermal_model_called_with_the_same_health_used_for_display(self):
        """Issue #468: this call site already computed `health` for its own per-type
        display — it must pass that SAME value into get_thermal_model(), not recompute
        or omit it."""
        _health = {"passive_decay": {"attempts": 10, "committed": 4}}
        coord = _make_coordinator(_health)
        hass = MagicMock()

        asyncio.run(build_thermal_pipeline_context(hass, coord))

        coord.learning.get_thermal_model.assert_called_once_with(learning_health=_health)


class TestRunRejectionCodesClassified:
    """Issues #1512/#1516: every code a heating/cooling run can be rejected with is labelled, not lumped in."""

    def test_quality_and_operational_codes_are_split(self):
        _health = {
            "hvac_heat": {
                "attempts": 6,
                "committed": 0,
                "rejections": {
                    "hvac_run_too_short": 2,
                    "hvac_signal_below_resolution": 1,
                    "no_k_passive": 1,
                    "new_session_started": 1,
                    "fan_activated": 1,
                },
            }
        }
        coord = _make_coordinator(_health)
        ctx = asyncio.run(build_thermal_pipeline_context(MagicMock(), coord))

        assert "quality failures: 4" in ctx
        assert "hvac_run_too_short x2" in ctx
        assert "hvac_signal_below_resolution x1" in ctx
        assert "no_k_passive x1" in ctx
        assert "operational interruptions: 2" in ctx
