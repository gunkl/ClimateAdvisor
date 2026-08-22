"""Tests for Issue #746 (strangler-fig completion program, Phase 5): decision-
equivalence for the economizer FSM-authoritative swap.

Proves flipping ``AutomationEngine._economizer_fsm_authoritative`` from its
default ``False`` to ``True`` produces a byte-for-byte identical
``event_log``/``action_log`` — the real commanded fan/HVAC actions, not just a
derived label — across the full golden + pending scenario corpus. Unlike
nat-vent's Phase 2d fast-loop widening, this extraction is a pure 1:1
translation of the existing branch structure, so no allowlist of known
divergences is expected or accepted here — any divergence found by this test
is a real bug to fix, not a candidate for allowlisting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.economizer_fsm_authoritative_compare import fsm_authoritative_mutation

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"


def _load_scenario(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _all_golden_and_pending_scenarios() -> list[Path]:
    paths = [p for p in _GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    paths += [p for p in _PENDING_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    return sorted(paths)


class TestFsmAuthoritativeEquivalence:
    @pytest.mark.parametrize("scenario_path", _all_golden_and_pending_scenarios(), ids=lambda p: p.stem)
    def test_flag_on_produces_identical_outcome(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        diff = diff_runs(scenario, mutate_b=fsm_authoritative_mutation, scenario_name=scenario_path.stem)
        assert not diff.a_error and not diff.b_error, (
            f"{scenario_path.stem}: a_error={diff.a_error!r} b_error={diff.b_error!r}"
        )
        assert diff.is_clean, (
            f"{scenario_path.stem}: FSM-authoritative diverged from baseline — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
            f"this extraction is a pure 1:1 translation and must be a behavioral no-op; "
            f"events: {[(d.index, d.a, d.b) for d in diff.event_divergences]}, "
            f"actions: {[(d.index, d.a, d.b) for d in diff.action_divergences]}"
        )


class TestFsmAuthoritativePositiveControl:
    """Test the test: a deliberately broken FSM gate MUST produce a real,
    detectable divergence — proves this comparator can actually catch a bug in
    the swapped path, not just always report clean.
    """

    def _make_engine(self, *, fsm_authoritative: bool):
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine
        from custom_components.climate_advisor.classifier import DayClassification
        from custom_components.climate_advisor.const import DAY_TYPE_HOT

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 78.0}
        hass.states = MagicMock()
        hass.states.get = MagicMock(return_value=climate_state)

        engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config={
                "comfort_heat": 70.0,
                "comfort_cool": 76.0,
                "setback_heat": 60,
                "setback_cool": 80,
                "notify_service": "notify.notify",
            },
        )
        engine._economizer_fsm_authoritative = fsm_authoritative

        c = object.__new__(DayClassification)
        c.day_type = DAY_TYPE_HOT
        c.trend_direction = "stable"
        c.trend_magnitude = 0.0
        c.today_high = 90.0
        c.today_low = 70.0
        c.tomorrow_high = 90.0
        c.tomorrow_low = 70.0
        c.hvac_mode = "cool"
        c.pre_condition = True
        c.pre_condition_target = -2.0
        c.windows_recommended = False
        c.window_open_time = None
        c.window_close_time = None
        c.setback_modifier = 0.0
        c.window_opportunity_morning = False
        c.window_opportunity_evening = False
        engine._current_classification = c
        return engine

    def test_positive_control_detects_a_broken_fsm_gate(self) -> None:
        import asyncio
        from unittest.mock import patch

        # Real conditions: windows open, outdoor cool enough, indoor above comfort
        # -> production would enter cool-down. Force the FSM's gate to always report
        # ineligible and confirm the FSM-authoritative branch stands down instead.
        engine = self._make_engine(fsm_authoritative=True)
        with patch(
            "custom_components.climate_advisor.economizer_fsm.decide_economizer_transition",
            lambda inputs: __import__(
                "custom_components.climate_advisor.economizer_gate", fromlist=["EconomizerDecision"]
            ).EconomizerDecision(eligible=False, phase="inactive", direction_ok=True),
        ):
            result = asyncio.run(
                engine.check_window_cooling_opportunity(
                    outdoor_temp=70.0, indoor_temp=78.0, windows_physically_open=True, current_hour=18
                )
            )

        assert result is False, (
            "positive control FAILED: forcing economizer_fsm.decide_economizer_transition() to always "
            "report ineligible did not suppress economizer activation — a broken FSM gate in the "
            "FSM-authoritative path would go undetected"
        )
        assert engine._economizer_active is False

    def test_unbroken_fsm_gate_activates_normally(self) -> None:
        import asyncio

        engine = self._make_engine(fsm_authoritative=True)
        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0, indoor_temp=78.0, windows_physically_open=True, current_hour=18
            )
        )
        assert result is True
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"
