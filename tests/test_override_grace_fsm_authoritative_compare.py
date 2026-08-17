"""Tests for Issue #664: decision-equivalence for the override/grace FSM-authoritative
full-authority swap.

Prior coverage (``test_shadow_engine_coverage.py``, ``test_override_grace_fsm_shadow_
wiring.py``) only proved *state-label* agreement in shadow mode. This file proves the
stronger claim full authority needs before any live switch is considered: flipping
``AutomationEngine._override_grace_fsm_authoritative`` from its default ``False`` to
``True`` produces a byte-for-byte identical ``event_log``/``action_log`` across the
full golden + pending scenario corpus.

**Corpus coverage confirmed, not assumed**: FAN_OVERRIDE_DETECTED (RF-remote/manual
fan-on) and DASHBOARD_RESUME (dashboard "Resume HVAC" button) both have no scenario-
event mapping in ``tools/sim_harness/run_production.py`` — same "narrower than it
looks" finding door/window's and nat-vent's own comparators already flagged. Both get
direct-engine-construction positive controls below.

**The manual_grace_seconds=0 edge case (Issue #664's own finding)**: investigation for
this migration found the FSM's landing branches originally never checked whether manual
grace was disabled by config, which would have made an authoritative FSM claim
``_grace_active=True`` with no real timer behind it — a worse bug class than #661. Fixed
in ``override_grace_fsm.py`` before this switch was added; ``TestGraceDisabledPositive
Control`` below is the regression guard proving it stays fixed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.override_grace_fsm_authoritative_compare import fsm_authoritative_mutation

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
            f"full authority must be a behavioral no-op (Issue #664 H1)"
        )


def _make_engine(**overrides):
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.climate_advisor.automation import AutomationEngine

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    climate_state = MagicMock()
    climate_state.state = "cool"
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
    engine._override_grace_fsm_authoritative = True
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


class TestFanOverrideDetectedPositiveControl:
    """FAN_OVERRIDE_DETECTED has no scenario-event mapping in run_production.py
    (fan manual-override detection isn't simulated) — direct construction needed."""

    def test_broken_transition_detected(self) -> None:
        from unittest.mock import patch

        from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState

        engine = _make_engine()
        with patch(
            "custom_components.climate_advisor.override_grace_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T",
                (),
                {"to_state": (OverrideConfirmState.IDLE, GraceState.NONE), "outcome": "broken", "at": None},
            )(),
        ):
            engine.handle_fan_manual_override(fan_before="auto", fan_after="on")

        assert engine._grace_active is False, (
            "positive control FAILED: forcing override_grace_fsm.transition() to return grace=NONE "
            "was not reflected in engine._grace_active — the mock wasn't actually exercised (real "
            "outcome should be grace=ACTIVE_PROTECTING_OVERRIDE, so an undetected broken swap would "
            "incorrectly show _grace_active=True instead)"
        )

    def test_unbroken_transition_activates_grace_normally(self) -> None:
        engine = _make_engine()
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on")
        assert engine._grace_active is True
        assert engine._grace_protects_override is True


class TestDashboardResumePositiveControl:
    """DASHBOARD_RESUME has no scenario-event mapping — the sim harness doesn't
    simulate the dashboard 'Resume HVAC' endpoint. Direct construction needed,
    same finding door/window's own comparator already established for this method."""

    def test_unbroken_transition_starts_unprotected_grace(self) -> None:
        import asyncio

        engine = _make_engine(_paused_by_door=True, _current_classification=None)
        asyncio.run(engine.resume_from_pause())
        assert engine._grace_active is True
        assert engine._grace_protects_override is False


class TestGraceDisabledPositiveControl:
    """Issue #664's own core finding: manual_grace_seconds=0 must land on
    GraceState.NONE, not be forced to an active state regardless of config. This is
    a regression guard for the fix in override_grace_fsm.py's landing branches."""

    def test_fan_override_with_grace_disabled_does_not_claim_active(self) -> None:
        engine = _make_engine(config={"comfort_heat": 70.0, "comfort_cool": 76.0, "manual_grace_seconds": 0})
        engine.handle_fan_manual_override(fan_before="auto", fan_after="on")
        assert engine._grace_active is False, (
            "manual_grace_seconds=0 must never result in _grace_active=True — no real timer exists "
            "to ever clear it, which would be a stuck-forever phantom grace"
        )

    def test_dashboard_resume_with_grace_disabled_does_not_claim_active(self) -> None:
        import asyncio

        engine = _make_engine(
            config={"comfort_heat": 70.0, "comfort_cool": 76.0, "manual_grace_seconds": 0},
            _paused_by_door=True,
            _current_classification=None,
        )
        asyncio.run(engine.resume_from_pause())
        assert engine._grace_active is False
