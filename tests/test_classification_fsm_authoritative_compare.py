"""Tests for Issue #742: decision-equivalence for the classification FSM
(ODE ceiling guard)-authoritative full-authority swap.

Prior coverage (``test_classification_fsm.py``, ``test_ode_ceiling_guard.py``)
only proved wiring/pure-function correctness in isolation. This file proves
the stronger claim full authority needs before any live switch is considered:
flipping ``AutomationEngine._classification_fsm_authoritative`` from its
default ``False`` to ``True`` (alone — the other four ``*_fsm_authoritative``
flags stay at their scenario-default ``False``) produces a byte-for-byte
identical ``event_log``/``action_log`` across the full golden + pending
scenario corpus.

Single-switch only, same isolation discipline as the other 4
``test_*_fsm_authoritative_compare.py`` files — the combined comparator
(``test_combined_fsm_authoritative_compare.py``) is where compound
interactions with the other FSMs are diagnosed; this file attributes any
divergence found here to the classification FSM's own dispatch/derivation
alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.classification_fsm_authoritative_compare import fsm_authoritative_mutation
from tools.sim_harness.differential import diff_runs

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
            f"{scenario_path.stem}: classification FSM-authoritative diverged from baseline — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
            f"full authority must be a behavioral no-op (Issue #742)"
        )


def _make_engine(**overrides):
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.climate_advisor.automation import AutomationEngine

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    climate_state = MagicMock()
    climate_state.state = "off"
    climate_state.attributes = {"temperature": 74.0}
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
            "manual_grace_seconds": 1800,
            "automation_grace_seconds": 300,
        },
    )
    engine._classification_fsm_authoritative = True
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


class TestOdeCeilingGuardPositiveControl:
    """Proves apply_classification()'s FSM branch is actually reached (not
    vacuously passing) when self._classification_fsm_authoritative=True and
    predicted_indoor + classification.hvac_mode=="off" are supplied."""

    def test_broken_transition_detected(self) -> None:
        import asyncio
        from unittest.mock import patch

        from custom_components.climate_advisor.classifier import DayClassification

        engine = _make_engine(_thermal_model={"k_passive": -0.5, "confidence_k_passive": "medium"})
        classification = DayClassification(
            day_type="hot",
            trend_direction="rising",
            trend_magnitude=2.0,
            today_high=95.0,
            today_low=75.0,
            tomorrow_high=96.0,
            tomorrow_low=76.0,
            hvac_mode="off",
            pre_condition=False,
            pre_condition_target=None,
        )
        predicted_indoor = [{"ts": "2026-07-15T18:00:00+00:00", "temp": 90.0}]

        # If the FSM branch is wired, classification_fsm.transition() must be
        # called — patching it to raise proves the call happens; a
        # silently-skipped dispatch would instead run the legacy inline block
        # and never hit this patch.
        with (
            patch(
                "custom_components.climate_advisor.automation._classification_fsm_transition",
                side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be reached")),
            ),
            pytest.raises(AssertionError),
        ):
            asyncio.run(engine.apply_classification(classification, predicted_indoor=predicted_indoor))

    def test_unbroken_transition_evaluates_normally(self) -> None:
        import asyncio

        from custom_components.climate_advisor.classifier import DayClassification

        engine = _make_engine(_thermal_model={"k_passive": -0.5, "confidence_k_passive": "medium"})
        classification = DayClassification(
            day_type="hot",
            trend_direction="rising",
            trend_magnitude=2.0,
            today_high=95.0,
            today_low=75.0,
            tomorrow_high=96.0,
            tomorrow_low=76.0,
            hvac_mode="off",
            pre_condition=False,
            pre_condition_target=None,
        )
        predicted_indoor = [{"ts": "2026-07-15T18:00:00+00:00", "temp": 90.0}]
        # Should not raise — the FSM branch runs to completion.
        asyncio.run(engine.apply_classification(classification, predicted_indoor=predicted_indoor))
