"""Tests for thermal pipeline health section in AI investigator context (Issue #156)."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

# ── HA module stubs must be in place before importing climate_advisor modules ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

if "anthropic" not in sys.modules:
    _mock_anthropic = MagicMock()
    _mock_anthropic.__name__ = "anthropic"
    _mock_anthropic.__path__ = []
    _mock_anthropic.__file__ = None
    _mock_anthropic.__spec__ = None
    _mock_anthropic.__loader__ = None
    _mock_anthropic.__package__ = "anthropic"
    _mock_anthropic.APIError = type("APIError", (Exception,), {})
    _mock_anthropic.APITimeoutError = type("APITimeoutError", (Exception,), {})
    _mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
    _mock_anthropic.AsyncAnthropic = MagicMock()
    sys.modules["anthropic"] = _mock_anthropic

from custom_components.climate_advisor.ai_skills_investigator import (  # noqa: E402
    async_build_investigator_context,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_learning_health(
    hvac_heat_committed: int = 0,
    hvac_cool_committed: int = 0,
    hvac_heat_rejections: int = 0,
    hvac_cool_rejections: int = 0,
    hvac_heat_top_reason: str = "new_session_started",
    hvac_cool_top_reason: str = "new_session_started",
    passive_committed: int = 5,
) -> dict:
    """Build a minimal learning_health dict matching _build_learning_health() shape."""

    def _type_health(committed: int, top_reason: str, n_rejections: int) -> dict:
        rejection_counts = {
            "too_few_samples": 0,
            "too_few_blocks": 0,
            "small_delta": 0,
            "ols_bad_fit": 0,
            "ols_wrong_sign": 0,
            "ols_bounds": 0,
            "abandoned": 0,
            "window_too_short": 0,
            "no_interior_peak": 0,
        }
        if top_reason in rejection_counts and n_rejections > 0:
            rejection_counts[top_reason] = n_rejections
        last_rejection = {"reason_code": top_reason, "n": 0} if n_rejections > 0 else None
        return {
            "attempts": committed + n_rejections,
            "committed": committed,
            "rejections": rejection_counts,
            "last_rejection": last_rejection,
        }

    return {
        OBS_TYPE_HVAC_HEAT: _type_health(hvac_heat_committed, hvac_heat_top_reason, hvac_heat_rejections),
        OBS_TYPE_HVAC_COOL: _type_health(hvac_cool_committed, hvac_cool_top_reason, hvac_cool_rejections),
        OBS_TYPE_PASSIVE_DECAY: _type_health(passive_committed, "too_few_samples", 0),
        "fan_only_decay": _type_health(0, "too_few_samples", 0),
        "ventilated_decay": _type_health(0, "too_few_samples", 0),
        "solar_gain": _type_health(0, "too_few_samples", 0),
        "source_endpoint_count": 4,
        "source_block_ols_count": 1,
    }


def _make_engine_status(k_active_cool_learned: bool = True) -> dict:
    """Build a minimal engine_status dict matching get_engine_status() shape."""
    cool_val = {"heat": 3.5, "cool": 2.1 if k_active_cool_learned else None}
    return {
        "k_passive": {"active": True, "value": -0.15, "confidence": "medium", "obs_count": 5, "since": "2026-04-01"},
        "k_solar": {"active": False, "value": None, "confidence": "none", "obs_count": 0, "since": None},
        "solar_phase_offset_h": {"active": False, "value": None, "since": None},
        "k_vent_window": {"active": False, "value": None, "since": None},
        "k_active_hvac": {"active": k_active_cool_learned, "value": cool_val, "since": "2026-04-15"},
        "ode_version": "v3",
        "physics_eligible": True,
        "physics_eligible_reason": "k_passive+k_hvac",
    }


def _make_learning_mock(
    thermal_model_overrides: dict | None = None,
    engine_status_overrides: dict | None = None,
) -> MagicMock:
    """Build a minimal learning engine mock."""
    learning = MagicMock()
    learning.generate_suggestions.return_value = []
    thermal = {
        "heating_rate_f_per_hour": 2.5,
        "cooling_rate_f_per_hour": 1.8,
        "k_passive": -0.15,
        "k_active_heat": 3.5,
        "k_active_cool": None,
        "confidence": "medium",
        "observation_count_heat": 0,
        "observation_count_cool": 0,
    }
    if thermal_model_overrides:
        thermal.update(thermal_model_overrides)
    learning.get_thermal_model.return_value = thermal
    learning.get_compliance_summary.return_value = {
        "window_compliance": 1.0,
        "pending_suggestions": 0,
        "avg_daily_hvac_runtime_minutes": 60,
        "comfort_score": 0.9,
        "total_manual_overrides": 2,
    }
    learning.get_weather_bias.return_value = {
        "high_bias": 0.0,
        "low_bias": 0.0,
        "confidence": "low",
        "observation_count": 0,
    }
    engine_status = _make_engine_status(k_active_cool_learned=False)
    if engine_status_overrides:
        engine_status.update(engine_status_overrides)
    learning.get_engine_status.return_value = engine_status
    state_obj = MagicMock()
    state_obj.records = []
    learning._state = state_obj
    return learning


def _make_coordinator(learning=None) -> MagicMock:
    """Build a mock coordinator with thermal pipeline data."""
    coord = MagicMock()
    coord.data = {
        "day_type": "mild",
        "trend": "stable",
        "hvac_action": "idle",
        "hvac_runtime_today": 30,
        "automation_status": "active",
        "last_action_time": "2026-05-18T08:00:00",
        "last_action_reason": "wake-up comfort restore",
        "next_automation_action": "none",
        "next_automation_time": "unknown",
        "occupancy_mode": "home",
        "fan_status": "inactive",
        "contact_status": "all_closed",
    }
    coord.config = {
        "climate_entity": "climate.thermostat",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "briefing_time": "06:00",
        "ai_enabled": True,
        "ai_model": "claude-sonnet-4-6",
        "learning_enabled": True,
    }
    coord.learning = learning if learning is not None else _make_learning_mock()
    coord._event_log = []
    coord.get_ai_report_history.return_value = []
    coord._today_record = None
    coord._hvac_on_since = None
    coord.hass = MagicMock()
    coord._build_learning_health.return_value = _make_learning_health()
    coord._build_thermal_pipeline_summary.return_value = {
        "pending": [],
        "rejection_log_counts": {},
    }
    return coord


def _make_hass() -> MagicMock:
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.state = "heat"
    climate_state.attributes = {"current_temperature": 69}
    hass.states.get.return_value = climate_state
    return hass


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests — red phase targets
# ---------------------------------------------------------------------------


class TestThermalPipelineSection:
    """The investigator context must include a THERMAL OBSERVATION PIPELINE section."""

    def test_context_contains_thermal_pipeline_section(self):
        """Context must include the THERMAL OBSERVATION PIPELINE heading."""
        coord = _make_coordinator()
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        assert "THERMAL OBSERVATION PIPELINE" in ctx, (
            f"Expected 'THERMAL OBSERVATION PIPELINE' section in investigator context; context starts with: {ctx[:500]}"
        )

    def test_k_active_cool_none_shown_as_never_learned(self):
        """When k_active_cool is None, context must show NEVER LEARNED, not 'None'."""
        learning = _make_learning_mock(
            thermal_model_overrides={"k_active_cool": None, "observation_count_cool": 0},
        )
        coord = _make_coordinator(learning=learning)
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        excerpt = ctx[ctx.find("PIPELINE") : ctx.find("PIPELINE") + 600] if "PIPELINE" in ctx else ctx[:600]
        assert "NEVER LEARNED" in ctx, (
            f"Expected 'NEVER LEARNED' marker when k_active_cool=None; relevant excerpt: {excerpt}"
        )

    def test_per_type_rejection_counts_in_context(self):
        """Rejection counts from learning_health must appear in the context."""
        health = _make_learning_health(
            hvac_cool_committed=0,
            hvac_cool_rejections=5,
            hvac_cool_top_reason="new_session_started",
        )
        coord = _make_coordinator()
        coord._build_learning_health.return_value = health
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        # The context must surface rejection counts (exact value 5 must appear near hvac_cool)
        pipeline_start = ctx.find("THERMAL OBSERVATION PIPELINE")
        assert pipeline_start != -1, "THERMAL OBSERVATION PIPELINE section not found"
        pipeline_excerpt = ctx[pipeline_start : pipeline_start + 1200]
        assert "hvac_cool" in pipeline_excerpt, "hvac_cool not shown in pipeline section"
        assert "5" in pipeline_excerpt, "rejection count 5 not shown in pipeline section"

    def test_engine_status_in_context(self):
        """get_engine_status() results must appear in the investigator context."""
        learning = _make_learning_mock(
            engine_status_overrides={
                "k_passive": {
                    "active": True,
                    "value": -0.18,
                    "confidence": "high",
                    "obs_count": 12,
                    "since": "2026-04-01",
                }
            }
        )
        coord = _make_coordinator(learning=learning)
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        # Engine status section must appear (already exists in activity report; investigator needs it too)
        assert "k_passive" in ctx, "k_passive not found in investigator context"

    def test_system_prompt_has_thermal_health_rules(self):
        """_SYSTEM_PROMPT must document thermal pipeline diagnostic rules."""
        from custom_components.climate_advisor.ai_skills_investigator import _SYSTEM_PROMPT

        assert "THERMAL PIPELINE HEALTH" in _SYSTEM_PROMPT, (
            "_SYSTEM_PROMPT missing THERMAL PIPELINE HEALTH rules section"
        )
        assert "k_active_cool" in _SYSTEM_PROMPT or "NEVER LEARNED" in _SYSTEM_PROMPT, (
            "_SYSTEM_PROMPT must mention k_active_cool=None / NEVER LEARNED diagnostic rule"
        )
        assert "new_session_started" in _SYSTEM_PROMPT, (
            "_SYSTEM_PROMPT must mention new_session_started abandonment pattern"
        )

    def test_pending_observations_shown_in_context(self):
        """If pending observations exist, they must appear in the pipeline section."""
        coord = _make_coordinator()
        coord._build_thermal_pipeline_summary.return_value = {
            "pending": [
                {
                    "obs_type": "hvac_cool",
                    "status": "monitoring",
                    "elapsed_minutes": 18.5,
                    "sample_count": 6,
                    "last_sample_age_minutes": 2.1,
                    "indoor_range_f": [74.0, 75.0],
                    "indoor_delta_f": 1.0,
                    "outdoor_f": 85.0,
                }
            ],
            "rejection_log_counts": {"hvac_cool": 3},
        }
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        pipeline_start = ctx.find("THERMAL OBSERVATION PIPELINE")
        assert pipeline_start != -1, "THERMAL OBSERVATION PIPELINE section not found"
        pipeline_excerpt = ctx[pipeline_start : pipeline_start + 1200]
        assert "hvac_cool" in pipeline_excerpt, "pending hvac_cool obs not shown in pipeline section"

    def test_zero_hvac_committed_flagged_as_pipeline_failure(self):
        """0 committed HVAC observations with non-zero rejections must be flagged."""
        health = _make_learning_health(
            hvac_heat_committed=0,
            hvac_cool_committed=0,
            hvac_heat_rejections=8,
            hvac_cool_rejections=8,
            hvac_heat_top_reason="new_session_started",
            hvac_cool_top_reason="new_session_started",
        )
        coord = _make_coordinator()
        coord._build_learning_health.return_value = health
        hass = _make_hass()

        with patch(
            "custom_components.climate_advisor.ai_skills_investigator.async_build_github_context",
            return_value="",
        ):
            ctx = _run(async_build_investigator_context(hass, coord))

        pipeline_start = ctx.find("THERMAL OBSERVATION PIPELINE")
        assert pipeline_start != -1, "THERMAL OBSERVATION PIPELINE section not found"
        pipeline_excerpt = ctx[pipeline_start : pipeline_start + 1500]
        # Must contain either "PIPELINE FAILURE" or "0 committed" markers
        has_failure_marker = "PIPELINE FAILURE" in pipeline_excerpt or (
            "0 committed" in pipeline_excerpt and "hvac_heat" in pipeline_excerpt
        )
        assert has_failure_marker, (
            "Expected pipeline failure signal when hvac_heat/hvac_cool have 0 committed + rejections; "
            f"excerpt: {pipeline_excerpt[:600]}"
        )
