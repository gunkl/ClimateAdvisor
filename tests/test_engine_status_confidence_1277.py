"""Issue #1277: the diagnostic tools grade thermal confidence live, the way the engine does.

What the owner saw: ``tools/engine_status.py`` printed ``Physics ODE: eligible: NO (confidence too low
(none))`` for every zone, and ``tools/learning_db.py --model`` printed ``conf_passive: none``, while the
running engine used the physics model in 6 of 7 zones. Both tools read ``confidence_k_passive`` /
``confidence_k_hvac`` from ``climate_advisor_learning.json``: fields the engine seeded as ``"none"`` and
never refreshed (``get_thermal_model()`` grades from the observation counts instead).

These tests drive the real tool functions and compare them with the real ``LearningEngine`` and with
the rule ``_build_predicted_indoor_future`` applies (``thermal_confidence.physics_eligibility``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.thermal_confidence import physics_eligibility

_TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_tool(name: str):
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    spec = importlib.util.spec_from_file_location(f"_test_1277_{name}", _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_es = _load_tool("engine_status")
_ldb = _load_tool("learning_db")


def _stale_cache(passive: int, heat: int, cool: int, **extra) -> dict:
    """A production-shaped cache: counts say one thing, the never-refreshed stored grades say "none"."""
    cache = {
        "k_passive": -0.08,
        "k_active_heat": 3.1,
        "k_active_cool": -2.4,
        "observation_count_passive": passive,
        "observation_count_heat": heat,
        "observation_count_cool": cool,
        "confidence_k_passive": "none",
        "confidence_k_hvac": "none",
    }
    cache.update(extra)
    return cache


def _engine_from_cache(tmp_path: Path, cache: dict) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine._state.thermal_model_cache = dict(cache)
    return engine


class TestToolsIgnoreStaleStoredGrades:
    def test_engine_status_reports_physics_eligible_when_counts_are_high(self):
        """Zone 01M1G3... in the issue: 246/85/19 obs, stored grades "none", engine uses physics."""
        status = _es.build_engine_status(_stale_cache(246, 85, 19))
        assert status["physics_eligible"] is True, status["physics_eligible_reason"]
        assert status["k_passive"]["confidence"] == "high"
        assert status["k_active_hvac"]["confidence"] == "high"

    def test_engine_status_still_reports_fallback_for_a_genuinely_young_zone(self):
        """Zone 01M1YCV... in the issue: 4/0/0 obs -> the engine really uses the fallback ramp."""
        status = _es.build_engine_status(_stale_cache(4, 0, 0))
        assert status["physics_eligible"] is False
        assert status["physics_eligible_reason"] == "confidence insufficient (none)"
        assert status["k_passive"]["confidence"] == "none"

    def test_learning_db_model_summary_prints_live_grades(self, capsys):
        _ldb._print_model_summary({"thermal_model_cache": _stale_cache(102, 0, 8)})
        out = capsys.readouterr().out
        assert "conf_passive:  high" in out
        assert "conf_hvac:     low" in out

    def test_gate_bridge_zone_is_reported_not_eligible(self):
        """No k_passive but a learned k_vent_window: NOT eligible.

        The old expectation (eligible via a gate bridge) was wrong: k_vent_window includes ventilation, so it
        is not a closed-house rate and must never stand in for k_passive (Issue #1512, owner decision 2).
        """
        cache = _stale_cache(0, 0, 0, k_vent_window=-0.2)
        cache["k_passive"] = None
        status = _es.build_engine_status(cache)
        assert status["physics_eligible"] is False
        assert "no k_vent_window substitute" in status["physics_eligible_reason"]


_GRID = [
    (p, h, c, k, kv)
    for p, h, c in [(0, 0, 0), (4, 0, 0), (5, 0, 0), (0, 3, 2), (6, 105, 0), (14, 0, 0), (29, 1, 0), (73, 62, 286)]
    for k in (-0.08, 0.02, None)
    for kv in (None, -0.2)
]


class TestToolMatchesEngine:
    @pytest.mark.parametrize(("passive", "heat", "cool", "k_passive", "k_vent_window"), _GRID)
    def test_grades_and_eligibility_match_the_live_engine(
        self, tmp_path, passive, heat, cool, k_passive, k_vent_window
    ):
        cache = _stale_cache(passive, heat, cool, k_passive=k_passive, k_vent_window=k_vent_window)
        engine = _engine_from_cache(tmp_path, cache)
        model = engine.get_thermal_model()
        rest = engine.get_engine_status()
        tool = _es.build_engine_status(dict(cache))

        assert tool["k_passive"]["confidence"] == model["confidence_k_passive"] == rest["k_passive"]["confidence"]
        assert tool["k_active_hvac"]["confidence"] == model["confidence"] == rest["k_active_hvac"]["confidence"]
        assert tool["k_solar"]["confidence"] == model["confidence_k_solar"] == rest["k_solar"]["confidence"]
        # The predicted-indoor curve's own gate, fed from get_thermal_model() exactly as the coordinator does.
        ode_eligible, _ = physics_eligibility(
            model["confidence"], model["confidence_k_passive"], model["k_passive"], model["k_vent_window"]
        )
        assert tool["physics_eligible"] == rest["physics_eligible"] == ode_eligible
        assert tool["physics_eligible_reason"] == rest["physics_eligible_reason"]


class TestStoredGradesDropped:
    def test_load_drops_stale_stored_grades(self, tmp_path):
        """Other readers of the JSON must not be misled either: the stale copies go on the next load."""
        engine = LearningEngine(tmp_path)
        engine._state.thermal_model_cache = _stale_cache(73, 62, 286)
        engine.save_state()
        reloaded = LearningEngine(tmp_path)
        reloaded.load_state()
        cache = reloaded._state.thermal_model_cache
        assert "confidence_k_passive" not in cache
        assert "confidence_k_hvac" not in cache
        assert cache["observation_count_passive"] == 73  # nothing else touched
        assert reloaded.get_thermal_model()["confidence_k_passive"] == "high"
        reloaded.save_state()
        on_disk = json.loads(next(tmp_path.rglob("*learning*.json")).read_text())
        assert "confidence_k_passive" not in on_disk["thermal_model_cache"]

    def test_fresh_cache_does_not_seed_stored_grades(self):
        from custom_components.climate_advisor.learning import _new_thermal_cache

        assert not {"confidence_k_passive", "confidence_k_hvac"} & set(_new_thermal_cache())

    def test_thermal_replay_rebuild_does_not_write_stored_grades(self):
        """thermal_replay's rebuild wrote these with its own (divergent) thresholds and pushed them to HA."""
        replay = _load_tool("thermal_replay")
        cache = replay.rebuild_model_cache([], existing_cache=None)
        assert not {"confidence_k_passive", "confidence_k_hvac"} & set(cache)
