"""Issue #1476: a chart_log fit of one window is recorded once, and repeats already stored are undone.

Occupant first: the Analysis tab's "Open windows move it toward outdoors N x faster" kept climbing
after restarts. The newest passive window was re-committed every time a passive stretch ended, so
one night sat in the observation list 20+ times and dragged the closed-house rate (the divisor) onto it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from custom_components.climate_advisor.const import LEARNING_DB_FILE
from custom_components.climate_advisor.learning import LearningEngine, _replay_envelope_k_passive
from tests.test_vent_chart_log_split import (
    _make_coord_with_chart_log,
    _make_dt_mock,
    _make_vent_chart_log_entries,
)

_TODAY = datetime.now(UTC).date().isoformat()


def _fit(k: float, r2: float, *, mode: str = "passive", source: str = "block_ols", grade: str = "medium", **kw) -> dict:
    return {
        "hvac_mode": mode,
        "k_passive": k,
        "r_squared": r2,
        "source": source,
        "confidence_grade": grade,
        "date": _TODAY,
        **kw,
    }


def _engine(tmp_path: Path, observations: list[dict], cache: dict | None) -> LearningEngine:
    (tmp_path / LEARNING_DB_FILE).write_text(
        json.dumps({"thermal_observations": observations, "thermal_model_cache": cache})
    )
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


class TestRecordRefusesARepeatedWindow:
    def test_the_same_window_is_recorded_once(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        win = {"window_start": "2026-11-07T01:00:00+00:00", "window_end": "2026-11-07T06:00:00+00:00"}
        assert engine.record_thermal_observation(_fit(-0.0144, 0.71, **win)) is True
        assert engine.record_thermal_observation(_fit(-0.0144, 0.71, **win)) is False
        assert len(engine._state.thermal_observations) == 1
        assert engine._state.thermal_model_cache["observation_count_passive"] == 1

    def test_a_legacy_row_without_window_stamps_matches_on_the_fit(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        engine.record_thermal_observation(_fit(-0.0144, 0.71))
        assert engine.record_thermal_observation(_fit(-0.0144, 0.71, window_start="a", window_end="b")) is False

    def test_a_different_window_is_recorded(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        engine.record_thermal_observation(_fit(-0.0144, 0.71, window_start="a", window_end="b"))
        assert engine.record_thermal_observation(_fit(-0.0144, 0.71, window_start="c", window_end="d")) is True

    def test_same_window_other_mode_is_recorded(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        engine.record_thermal_observation(_fit(-0.05, 0.6, window_start="a", window_end="b"))
        assert engine.record_thermal_observation(_fit(-0.05, 0.6, mode="vent_window", window_start="a", window_end="b"))

    def test_live_observations_are_never_deduplicated(self, tmp_path: Path):
        """Only chart_log fits (endpoint / block_ols) are matched; a live decay observation always records."""
        engine = LearningEngine(tmp_path)
        live = {"hvac_mode": "heat", "k_passive": None, "k_active": None, "date": _TODAY, "confidence_grade": "low"}
        assert engine.record_thermal_observation(dict(live)) is True
        assert engine.record_thermal_observation(dict(live)) is True


class TestRepeatsStoredBefore1476AreUndoneOnLoad:
    def _stored(self):
        """Zone 1 shape: a few distinct nights, the newest one repeated, cache = replay of the list."""
        heat = {"hvac_mode": "heat", "k_passive": -0.14, "confidence_grade": "low", "date": _TODAY}
        obs = [
            _fit(-0.0178, 0.6),
            heat,
            _fit(-0.034, 0.5, source="endpoint", grade="low"),
            *[_fit(-0.0144, 0.71) for _ in range(19)],
            *[_fit(-0.0235, 0.4, mode="vent_window", source="endpoint", grade="low") for _ in range(3)],
        ]
        cache = {
            "k_passive": _replay_envelope_k_passive(obs),
            "k_vent_window": -0.1375,
            "observation_count_passive": 126,
            "observation_count_vent_window": 94,
        }
        return obs, cache

    def test_repeats_are_removed_counts_corrected_and_k_passive_rebuilt(self, tmp_path: Path):
        obs, cache = self._stored()
        engine = _engine(tmp_path, obs, cache)
        kept = engine._state.thermal_observations
        assert len(kept) == len(obs) - 18 - 2
        c = engine._state.thermal_model_cache
        assert c["observation_count_passive"] == 126 - 18
        assert c["observation_count_vent_window"] == 94 - 2
        # Old expectation (EWMA replay of the kept list) was wrong once rates age (Issue #1512): k_passive is now
        # the weighted median of the kept observations, so it sits inside the range of the kept fits.
        assert -0.034 <= c["k_passive"] <= -0.0144
        assert c["k_vent_window"] == -0.1375, "vent rates keep their stored value"

    def test_cleanup_is_idempotent(self, tmp_path: Path):
        obs, cache = self._stored()
        engine = _engine(tmp_path, obs, cache)
        engine.save_state()
        before = (list(engine._state.thermal_observations), dict(engine._state.thermal_model_cache))
        again = LearningEngine(tmp_path)
        again.load_state()
        assert (again._state.thermal_observations, again._state.thermal_model_cache) == before

    def test_k_passive_is_kept_when_the_list_does_not_explain_it(self, tmp_path: Path):
        """Counts are corrected whether or not the list explains the stored k_passive."""
        obs, cache = self._stored()
        cache["k_passive"] = -0.08
        engine = _engine(tmp_path, obs, cache)
        # Issue #1512 (owner decision: nothing jumps, old values age out): the stored value becomes the ageing prior,
        # weighted by the evidence behind it (observation counts), so on load it is kept and newer fits pull it
        # gradually. The interim expectation of an immediate re-derivation was the jump the owner ruled out.
        assert abs(engine._state.thermal_model_cache["k_passive"] - (-0.08)) < 1e-9
        assert engine._state.thermal_model_cache["rate_prior"]["k_passive"]["value"] == -0.08
        assert engine._state.thermal_model_cache["observation_count_passive"] == 126 - 18

    def test_a_clean_list_is_untouched(self, tmp_path: Path):
        obs = [_fit(-0.0178, 0.6), _fit(-0.0144, 0.71)]
        cache = {"k_passive": -0.02, "observation_count_passive": 40}
        engine = _engine(tmp_path, obs, cache)
        assert engine._state.thermal_observations == obs
        # Old expectation (cache == stored cache) was wrong: load also stores the ageing prior and re-derives
        # k_passive from the observations (Issue #1512); counts stay untouched.
        assert engine._state.thermal_model_cache["observation_count_passive"] == 40
        assert engine._state.thermal_model_cache["rate_prior"]["k_passive"]["value"] == -0.02


class TestCoordinatorRefitOfTheSameWindow:
    def test_refitting_the_newest_window_commits_once(self, tmp_path: Path):
        """The live trigger re-runs the fit on the newest window each time; only the first lands."""
        t0 = datetime.combine(datetime.now(UTC).date() - timedelta(days=1), datetime.min.time(), UTC) + timedelta(
            hours=1
        )
        entries = _make_vent_chart_log_entries(
            n_hours=4, t_indoor_start=74.0, t_outdoor=60.0, k_passive=-0.05, fan=False, t0=t0
        )
        coord = _make_coord_with_chart_log(entries)
        coord.learning = LearningEngine(tmp_path)
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_mock(t0 + timedelta(hours=11))):
            for _ in range(3):
                coord._run_vent_window_chart_log_fit(backfill=False)
            coord._run_vent_window_chart_log_fit(backfill=True)  # the v1 + v2 startup backfills
            coord._run_vent_window_chart_log_fit(backfill=True)
        rows = [o for o in coord.learning._state.thermal_observations if o["hvac_mode"] == "vent_window"]
        assert len(rows) == 1
        assert rows[0]["window_start"] == entries[0]["ts"]
        assert coord.learning._state.thermal_model_cache["observation_count_vent_window"] == 1
