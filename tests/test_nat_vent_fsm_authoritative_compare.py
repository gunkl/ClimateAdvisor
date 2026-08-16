"""Tests for Issue #594 Phase R, Step 3: decision-equivalence for the nat-vent
FSM-authoritative swap (Step 2).

Prior coverage (``test_shadow_engine_pair.py``, ``test_nat_vent_fsm_shadow_wiring.py``)
only proved *state-label* agreement — would the FSM's independently-derived
state match production's flags. This file proves the stronger claim Phase R
actually needs before any live switch is considered: flipping
``AutomationEngine._natvent_fsm_authoritative`` from its default ``False`` to
``True`` produces a byte-for-byte identical ``event_log``/``action_log`` — the
real commanded fan/HVAC actions, not just a derived label — across the full
golden + pending scenario corpus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.sim_harness.differential import diff_runs
from tools.sim_harness.nat_vent_fsm_authoritative_compare import fsm_authoritative_mutation

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"

# Known to exercise soft-start entry + the upgrade-to-full-gate escalation
# (test_nat_vent_soft_start.py's TestSoftStartUpgrade covers the same math in
# isolation) — used as the positive-control probe below.
_PROBE_GOLDEN = "2026-03-28-overnight"


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
    """Test the test: a deliberately broken FSM gate MUST produce a real,
    detectable divergence — proves this comparator can actually catch a bug in
    the swapped path, not just always report clean.

    Note on scope: the corpus-wide test above is honest but narrower than it
    first looks. Step 2's swap only executes when ``_nat_vent_soft_start`` is
    True at the top of the active-session block — none of the 88 current
    golden/pending scenarios exercise soft-start (it's a newer, less common
    entry path; see Issue #540). For every scenario in the corpus test, the
    flag flip is therefore an inert no-op by construction (branch never
    taken), not evidence the FSM path itself computes correctly under load.
    This positive control exercises the actual swapped branch directly
    (bypassing the corpus, same direct-engine-construction style as
    ``test_nat_vent_soft_start.py``) so a broken FSM gate is provably
    detectable, and flags the corpus gap rather than silently relying on
    scenarios that never touch the new code."""

    def _make_soft_start_engine(self, *, fsm_authoritative: bool):
        import sys
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.climate_advisor.automation import AutomationEngine
        from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE

        sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 7, 29, 18, 0, 0)

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        climate_state = MagicMock()
        climate_state.state = "cool"
        climate_state.attributes = {"current_temperature": 76.0}
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
                "natural_vent_delta": 3.0,
                "notify_service": "notify.notify",
                CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            },
        )
        engine._natvent_fsm_authoritative = fsm_authoritative
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        # outdoor well below indoor - hysteresis -> full gate clears for real.
        engine._last_outdoor_temp = 70.0
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5
        return engine

    def test_positive_control_detects_a_broken_fsm_gate(self) -> None:
        import asyncio
        from unittest.mock import patch

        engine = self._make_soft_start_engine(fsm_authoritative=True)
        with patch("custom_components.climate_advisor.nat_vent_fsm.decide_nat_vent_gate", lambda inputs: False):
            asyncio.run(engine.check_natural_vent_conditions())

        # Real full-gate conditions hold (outdoor 70 vs indoor 76), so without
        # the injected break the session would upgrade (_nat_vent_soft_start ->
        # False, per TestSoftStartUpgradeFsmAuthoritative above). Forcing the
        # FSM's gate to always return False must suppress that upgrade —
        # proves this comparison style can catch a real regression in the
        # swapped path, not just report clean by construction.
        assert engine._nat_vent_soft_start is True, (
            "positive control FAILED: forcing nat_vent_fsm.decide_nat_vent_gate() to always "
            "return False did not suppress the soft-start upgrade — a broken FSM gate in the "
            "FSM-authoritative path would go undetected"
        )

    def test_unbroken_fsm_gate_upgrades_normally(self) -> None:
        import asyncio

        engine = self._make_soft_start_engine(fsm_authoritative=True)
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._nat_vent_soft_start is False
