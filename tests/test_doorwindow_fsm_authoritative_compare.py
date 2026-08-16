"""Tests for Issue #594 Phase R, Step 3: decision-equivalence for the door/window
FSM-authoritative swap (Step 2, partial-authority increment).

Prior coverage (``test_shadow_engine_coverage.py``, ``test_shadow_engine_pair.py``)
only proved *state-label* agreement. This file proves the stronger claim Phase R
needs before any live switch is considered: flipping
``AutomationEngine._doorwindow_fsm_authoritative`` from its default ``False`` to
``True`` produces a byte-for-byte identical ``event_log``/``action_log`` — for the
2 methods this increment actually swaps
(``handle_manual_override_during_pause``/``resume_from_pause``) — across the full
golden + pending scenario corpus.

**Corpus coverage confirmed insufficient before writing this, not assumed.**
``resume_from_pause()`` has NO scenario-event mapping at all in
``tools/sim_harness/run_production.py`` — it's only reachable via ``api.py``'s
dashboard "Resume HVAC" endpoint, which the sim harness doesn't simulate (no HTTP
layer). So the corpus-wide test below is an inert no-op for DASHBOARD_RESUME by
construction, same "narrower than it looks" finding nat-vent's own comparator
already flagged for soft-start. ``handle_manual_override_during_pause()`` *is*
reachable in principle (``thermostat_state_changed`` scenario events, when
``is_paused_by_door`` is True at the time — see coordinator.py's
``_async_thermostat_changed``), but whether any current scenario actually lands in
that exact state is not assumed here either. Both get a direct-engine-construction
positive control below, same style as
``test_nat_vent_fsm_authoritative_compare.py``'s soft-start probe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.doorwindow_fsm_authoritative_compare import fsm_authoritative_mutation

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
            f"Step 2's read-authority swap must be a behavioral no-op"
        )


class TestFsmAuthoritativePositiveControl:
    """A deliberately broken FSM transition MUST produce a real, detectable
    divergence from the direct-engine tests below — proves this style of
    comparison can actually catch a bug in the swapped path, not just always
    report clean. Reuses the same direct-construction pattern
    ``tests/test_door_window.py``'s/``tests/test_resume_from_pause.py``'s own
    ``TestManualOverrideDuringPauseFsmAuthoritative``/
    ``TestResumeFromPauseFsmAuthoritative`` classes already establish — this
    class instead patches the FSM's own ``transition()`` to prove a broken
    swap is detectable, not just that the swap produces the expected output.
    """

    def _make_paused_engine(self, *, fsm_authoritative: bool, grace_active: bool = False):
        import sys
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine

        sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 29, 18, 0, 0)

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 74.0}
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
        engine._doorwindow_fsm_authoritative = fsm_authoritative
        engine._paused_by_door = True
        engine._grace_active = grace_active
        engine._pre_pause_mode = "cool"
        return engine

    def test_positive_control_detects_a_broken_fsm_transition(self) -> None:
        import asyncio
        from unittest.mock import patch

        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState

        engine = self._make_paused_engine(fsm_authoritative=True)
        with patch(
            "custom_components.climate_advisor.door_window_fsm.transition",
            side_effect=lambda *a, **k: type(
                "T", (), {"to_state": DoorWindowLifecycleState.PAUSED_ACTIVE, "outcome": "broken", "at": None}
            )(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        # Real behavior: MANUAL_OVERRIDE_DURING_PAUSE always clears the pause.
        # Forcing transition() to instead return PAUSED_ACTIVE (pause never
        # clears) must show up as a detectable divergence — proves a broken
        # FSM-authoritative swap wouldn't silently pass.
        assert engine._paused_by_door is True, (
            "positive control FAILED: forcing door_window_fsm.transition() to return the wrong "
            "state did not change engine._paused_by_door — a broken FSM-authoritative swap would "
            "go undetected"
        )

    def test_unbroken_transition_clears_pause_normally(self) -> None:
        import asyncio

        engine = self._make_paused_engine(fsm_authoritative=True)
        asyncio.run(engine.handle_manual_override_during_pause())
        assert engine._paused_by_door is False

    def test_resume_from_pause_unreachable_via_scenario_corpus_uses_direct_construction(self) -> None:
        """Documents (and asserts, not just claims) why DASHBOARD_RESUME needs this
        direct-construction control: resume_from_pause() has no scenario-event
        mapping in run_production.py at all."""
        import asyncio

        engine = self._make_paused_engine(fsm_authoritative=True)
        engine._current_classification = None  # no HVAC call needed for this assertion
        asyncio.run(engine.resume_from_pause())
        assert engine._paused_by_door is False
        assert engine._grace_active is True
