"""Tests for Issue #744: decision-equivalence for the occupancy dispatch FSM
(away/vacation/home)-authoritative full-authority swap.

Prior coverage (``test_occupancy_fsm.py``, ``test_occupancy_priority.py``) only
proved wiring/pure-function correctness in isolation. This file proves the stronger
claim full authority needs before any live switch is considered: flipping
``AutomationEngine._occupancy_fsm_authoritative`` from its default ``False`` to
``True`` (alone — the other five ``*_fsm_authoritative`` flags stay at their
scenario-default ``False``) produces a byte-for-byte identical ``event_log``/
``action_log`` across the full golden + pending scenario corpus.

Single-switch only, same isolation discipline as the other 5
``test_*_fsm_authoritative_compare.py`` files — the combined comparator
(``test_combined_fsm_authoritative_compare.py``) is where compound interactions
with the other FSMs are diagnosed; this file attributes any divergence found here
to the occupancy FSM's own dispatch/derivation alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.occupancy_fsm_authoritative_compare import fsm_authoritative_mutation

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
            f"{scenario_path.stem}: occupancy FSM-authoritative diverged from baseline — "
            f"{len(diff.event_divergences)} event, {len(diff.action_divergences)} action divergences — "
            f"full authority must be a behavioral no-op (Issue #744)"
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
            "setback_heat": 60.0,
            "setback_cool": 82.0,
            "manual_grace_seconds": 1800,
            "automation_grace_seconds": 300,
        },
    )
    engine._occupancy_fsm_authoritative = True
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


class TestOccupancyFsmPositiveControl:
    """Proves each handler's FSM branch is actually reached (not vacuously
    passing) when self._occupancy_fsm_authoritative=True."""

    def test_away_broken_dispatch_detected(self) -> None:
        import asyncio
        from unittest.mock import patch

        engine = _make_engine()
        with (
            patch(
                "custom_components.climate_advisor.automation.decide_away_vacation_dispatch",
                side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be reached")),
            ),
            pytest.raises(AssertionError),
        ):
            asyncio.run(engine.handle_occupancy_away())

    def test_vacation_broken_dispatch_detected(self) -> None:
        import asyncio
        from unittest.mock import patch

        engine = _make_engine()
        with (
            patch(
                "custom_components.climate_advisor.automation.decide_away_vacation_dispatch",
                side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be reached")),
            ),
            pytest.raises(AssertionError),
        ):
            asyncio.run(engine.handle_occupancy_vacation())

    def test_home_broken_dispatch_detected(self) -> None:
        import asyncio
        from unittest.mock import patch

        engine = _make_engine()
        with (
            patch(
                "custom_components.climate_advisor.automation.decide_home_dispatch",
                side_effect=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be reached")),
            ),
            pytest.raises(AssertionError),
        ):
            asyncio.run(engine.handle_occupancy_home())

    def test_unbroken_dispatch_evaluates_normally(self) -> None:
        import asyncio

        engine = _make_engine()
        # Should not raise — the FSM branch runs to completion for all 3 handlers.
        asyncio.run(engine.handle_occupancy_away())
        asyncio.run(engine.handle_occupancy_vacation())
        asyncio.run(engine.handle_occupancy_home())
