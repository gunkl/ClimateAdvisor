"""Tests for Issue #611 (Block 5 Phase 3, subtask O): offline whole-engine
shadow-pair comparator.

Layer 1: unit-level positive control — proves the comparator's own assertions
actually catch contamination/disagreement, not just that it reports "clean"
unconditionally (the project's "test the test" convention, already used by
``tools/sim_harness/differential.py``'s ``bump_setpoint_mutation``/
``extra_event_mutation``).

Layer 2: broad regression sweep across every golden + pending scenario that
does NOT use a real coordinator (``use_coordinator=False``) — this offline
harness's documented scope (see ``shadow_engine_pair.py``'s module docstring;
coordinator-level shadow wiring is Q's job, not O's).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.sim_harness.shadow_engine_pair import run_shadow_pair_scenario

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tools" / "simulations" / "golden"
_PENDING_DIR = _REPO_ROOT / "tools" / "simulations" / "pending"


def _load_scenario(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _offline_scenarios() -> list[Path]:
    """Golden + pending scenarios usable by this engine-only offline harness."""
    paths = [p for p in _GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    paths += [p for p in _PENDING_DIR.glob("*.json") if p.name != "MANIFEST.json"]
    out = []
    for p in sorted(paths):
        scenario = _load_scenario(p)
        if not scenario.get("use_coordinator", False):
            out.append(p)
    return out


_SIMPLE_NAT_VENT_SCENARIO = {
    "name": "unit-test-simple-nat-vent",
    "config": {},
    "events": [
        {"time": "2024-01-15T08:00:00+00:00", "type": "classification", "day_type": "mild", "hvac_mode": "off"},
        {"time": "2024-01-15T09:00:00+00:00", "type": "temp_update", "indoor_f": 72.0, "outdoor_f": 65.0},
        {"time": "2024-01-15T10:00:00+00:00", "type": "temp_update", "indoor_f": 71.0, "outdoor_f": 64.0},
    ],
}


class TestPositiveControlIsolationLeak:
    """Proves the shadow.action_log-empty assertion actually detects a leak."""

    def test_dry_run_shadow_produces_no_actions(self) -> None:
        """Baseline: an honest shadow (dry_run=True) never appends to action_log."""
        result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)
        assert result.shadow_action_log == []
        assert result.is_clean

    def test_broken_dry_run_is_caught(self) -> None:
        """Positive control: if a shadow engine were ever built with dry_run=False
        (the exact contamination class the epic flagged — a shadow issuing REAL
        service calls), the comparator's shadow_action_log check must catch it.

        Patches ``run_production_scenario`` to ignore the ``dry_run`` kwarg,
        simulating a future regression where a shadow builder forgets to force
        dry_run — proves detection works before any zero-divergence result from
        this module is trusted (the same "test the test" discipline
        ``differential.py``'s own positive controls use).
        """
        from tools.sim_harness import shadow_engine_pair as sep

        original = sep.run_production_scenario

        def _ignore_dry_run(scenario, **kwargs):
            kwargs["dry_run"] = False  # force off regardless of caller's intent
            return original(scenario, **kwargs)

        with patch.object(sep, "run_production_scenario", side_effect=_ignore_dry_run):
            result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)

        assert result.shadow_action_log != [], (
            "positive control failed: forcing dry_run=False on the shadow engine "
            "produced an empty action_log — the comparator cannot be trusted to "
            "catch a real isolation leak"
        )
        assert not result.is_clean


class TestPositiveControlLifecycleDisagreement:
    """Proves the lifecycle-state-agreement assertion actually detects disagreement."""

    def test_states_agree_by_default(self) -> None:
        result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)
        assert result.production_lifecycle_state == result.shadow_lifecycle_state

    def test_forced_disagreement_is_caught(self) -> None:
        """Positive control: corrupt the shadow's own final _natural_vent_active
        flag reading and confirm the comparator's lifecycle-agreement check fires."""
        from tools.sim_harness import shadow_engine_pair as sep

        original = sep._lifecycle_state_from_run

        def _corrupt_shadow(result, now):
            state = original(result, now)
            return "corrupted_state" if state is not None else state

        call_count = {"n": 0}

        def _corrupt_only_second_call(result, now):
            call_count["n"] += 1
            if call_count["n"] == 2:  # shadow is always the second call in run_shadow_pair_scenario
                return _corrupt_shadow(result, now)
            return original(result, now)

        with patch.object(sep, "_lifecycle_state_from_run", side_effect=_corrupt_only_second_call):
            result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)

        assert result.production_lifecycle_state != result.shadow_lifecycle_state
        assert not result.is_clean


class TestPositiveControlProductionContamination:
    """Proves the production-vs-baseline action-log check actually detects
    a shadow-caused change to production's own behavior."""

    def test_matches_baseline_by_default(self) -> None:
        result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)
        assert result.production_action_divergences == []

    def test_forced_contamination_is_caught(self) -> None:
        """Positive control: patch AutomationEngine._set_temperature (the same
        choke point differential.py's own bump_setpoint_mutation targets) so
        ONLY the paired production run's commanded setpoints shift — simulating
        a shadow engine that somehow altered what production commands. The
        comparator's production_action_divergences check must be non-empty."""
        from custom_components.climate_advisor.automation import AutomationEngine

        original = AutomationEngine._set_temperature
        call_state = {"n": 0}

        async def _wrapped(self, temperature, *args, **kwargs):
            call_state["n"] += 1
            # Only the SECOND run_production_scenario() call in
            # run_shadow_pair_scenario is "production" (paired) — shift its
            # setpoints so it diverges from the untouched baseline (1st call).
            if self.role == "production" and call_state["n"] > 1:
                temperature = float(temperature) + 5.0
            return await original(self, temperature, *args, **kwargs)

        with patch.object(AutomationEngine, "_set_temperature", _wrapped):
            result = run_shadow_pair_scenario(_SIMPLE_NAT_VENT_SCENARIO)

        assert result.production_action_divergences != [], (
            "positive control failed: a forced production-behavior change produced "
            "zero action divergences from baseline — the comparator cannot be "
            "trusted to catch real contamination"
        )
        assert not result.is_clean


class TestOfflineScenarioSweep:
    """Layer 2: broad regression sweep across every offline-eligible golden +
    pending scenario — the epic's O definition: 'run against every golden
    scenario in tools/simulate.py'."""

    @pytest.mark.parametrize("scenario_path", _offline_scenarios(), ids=lambda p: p.stem)
    def test_shadow_pair_is_clean(self, scenario_path: Path) -> None:
        scenario = _load_scenario(scenario_path)
        result = run_shadow_pair_scenario(scenario)
        assert result.is_clean, result.summary()
