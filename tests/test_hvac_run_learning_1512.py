"""Issue #1512 / #1516: HVAC rates learned from the run's own edges, aged instead of EWMA-forever.

Covers ``compute_hvac_run_rate``, ``LearningEngine.record_hvac_run``, ``record_duty_balance_fit``, the
weighted-median ageing of k_active_heat / k_active_cool / k_passive, and the removal of the
k_vent_window -> k_passive gate bridge.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.const import (
    REJECT_HVAC_RUN_TOO_SHORT,
    REJECT_HVAC_SIGNAL_BELOW_RESOLUTION,
    REJECT_NO_K_PASSIVE,
    REJECT_OLS_BOUNDS,
    THERMAL_LEGACY_OBS_WEIGHT,
    THERMAL_RATE_HALF_LIFE_DAYS,
)
from custom_components.climate_advisor.learning import (
    LearningEngine,
    compute_hvac_run_rate,
    weighted_median,
)
from custom_components.climate_advisor.operation_timeline import model_rates
from custom_components.climate_advisor.thermal_confidence import explain_physics_eligibility, physics_eligibility

K_P = -0.05


def _rate(**kw):
    base = {
        "mode": "heat",
        "start_f": 63.4,
        "off_f": 66.8,
        "minutes": 26.0,
        "avg_delta_t": 20.0,
        "k_passive": K_P,
        "resolution_f": None,
    }
    base.update(kw)
    return compute_hvac_run_rate(**base)


def _date(days_ago: int) -> str:
    return (dt_util.now().date() - timedelta(days=days_ago)).isoformat()


def _run(mode="heat", **kw):
    run = {
        "obs_id": "run-1",
        "mode": mode,
        "start_ts": dt_util.now().isoformat(),
        "off_ts": dt_util.now().isoformat(),
        "start_f": 63.4 if mode == "heat" else 76.0,
        "off_f": 66.8 if mode == "heat" else 72.6,
        "peak_f": 66.8 if mode == "heat" else 72.6,
        "minutes": 26.0,
        "avg_delta_t": 20.0 if mode == "heat" else -15.0,
        "avg_outdoor_f": 45.0,
        "k_passive": K_P,
        "k_passive_source": "v3",
        "resolution_f": None,
        "n_active": 3,
    }
    run.update(kw)
    return run


def _obs(mode, k, *, days_ago=0, grade="medium", method=None):
    o = {
        "hvac_mode": mode,
        "k_active": k if mode in ("heat", "cool") else None,
        "k_passive": k if mode == "passive" else None,
        "date": _date(days_ago),
        "confidence_grade": grade,
    }
    if method:
        o["method"] = method
    return o


def _engine(tmp_path: Path) -> LearningEngine:
    e = LearningEngine(tmp_path)
    e.load_state()
    return e


class TestRunEdgeRate:
    def test_heat_coast_case_uses_only_the_edge_readings(self):
        """63.4 -> 66.8 over 26 min: the rate comes from these two readings and the run's own delta-T."""
        k, code = _rate()
        assert code is None
        expected = (66.8 - 63.4) / (26.0 / 60.0) - K_P * 20.0
        assert k == pytest.approx(expected)

    def test_result_does_not_depend_on_anything_after_the_off_edge(self):
        """The estimator has no post-run input at all: a furnace coast (68.9 later) cannot change it."""
        before, _ = _rate()
        after, _ = _rate(off_f=66.8)
        assert before == after

    def test_cool_run_unaffected_by_the_room_warming_after_off(self):
        """#1516: an AC run followed by the room warming back up is judged on its off-edge reading only."""
        k, code = _rate(mode="cool", start_f=76.0, off_f=72.6, avg_delta_t=-15.0)
        assert code is None
        expected = (72.6 - 76.0) / (26.0 / 60.0) - K_P * -15.0
        assert k == pytest.approx(expected)
        assert k < 0

    def test_heat_cool_mirror_symmetry(self):
        kh, _ = _rate(mode="heat", start_f=60.0, off_f=63.0, minutes=30.0, avg_delta_t=0.0)
        kc, _ = _rate(mode="cool", start_f=63.0, off_f=60.0, minutes=30.0, avg_delta_t=0.0)
        assert kh == pytest.approx(-kc)

    def test_mirror_codes(self):
        assert _rate(mode="heat", off_f=63.5)[1] == REJECT_HVAC_SIGNAL_BELOW_RESOLUTION
        assert _rate(mode="cool", start_f=76.0, off_f=75.7, avg_delta_t=-15.0)[1] == REJECT_HVAC_SIGNAL_BELOW_RESOLUTION

    def test_wrong_direction_is_below_signal(self):
        assert _rate(mode="heat", start_f=66.0, off_f=64.0)[1] == REJECT_HVAC_SIGNAL_BELOW_RESOLUTION
        assert _rate(mode="cool", start_f=70.0, off_f=72.0)[1] == REJECT_HVAC_SIGNAL_BELOW_RESOLUTION

    def test_coarse_sensor_needs_two_degrees(self):
        """A 1.0 F sensor needs >= 2 F net change; 0.1 F sensors keep the 0.5 F floor."""
        assert _rate(start_f=65.0, off_f=66.0, resolution_f=1.0)[1] == REJECT_HVAC_SIGNAL_BELOW_RESOLUTION
        k, code = _rate(start_f=65.0, off_f=67.0, resolution_f=1.0)
        assert code is None and k is not None
        k, code = _rate(start_f=65.0, off_f=65.6, minutes=10.0, resolution_f=0.1, avg_delta_t=0.0)
        assert code is None

    def test_too_short(self):
        assert _rate(minutes=4.0) == (None, REJECT_HVAC_RUN_TOO_SHORT)
        assert _rate(minutes=5.0, off_f=64.4)[1] is None

    @pytest.mark.parametrize("k_p", [None, 0.0, 0.02])
    def test_no_closed_house_rate_rejects(self, k_p):
        assert _rate(k_passive=k_p) == (None, REJECT_NO_K_PASSIVE)

    def test_bounds(self):
        # 10 F in 5 minutes = 120 F/hr: not a physical heating rate
        assert _rate(minutes=5.0, start_f=60.0, off_f=70.0)[1] == REJECT_OLS_BOUNDS
        # 0.6 F over 5 h with no conduction: 0.12 F/hr is under the 0.5 F/hr minimum
        assert _rate(start_f=60.0, off_f=60.6, minutes=300.0, avg_delta_t=0.0)[1] == REJECT_OLS_BOUNDS


class TestRecordHvacRun:
    def test_commit_shape(self, tmp_path):
        e = _engine(tmp_path)
        obs, code = e.record_hvac_run(_run())
        assert code is None and obs is not None
        assert obs["method"] == "run_edge"
        assert obs["k_passive"] is None
        assert obs["k_passive_used"] == K_P and obs["k_passive_source"] == "v3"
        assert obs["end_indoor_f"] == 66.8 and obs["start_indoor_f"] == 63.4
        assert obs["confidence_grade"] == "medium"
        assert obs["signal_f"] == pytest.approx(3.4)
        assert e._state.thermal_observations[-1] is obs
        assert e._state.thermal_model_cache["observation_count_heat"] == 1
        assert e._state.thermal_model_cache["k_active_heat"] == pytest.approx(obs["k_active"])

    def test_short_run_grade_low(self, tmp_path):
        e = _engine(tmp_path)
        obs, _ = e.record_hvac_run(_run(minutes=10.0, off_f=64.6))
        assert obs is not None and obs["confidence_grade"] == "low"

    def test_cool_commit(self, tmp_path):
        e = _engine(tmp_path)
        obs, code = e.record_hvac_run(_run("cool"))
        assert code is None and obs["hvac_mode"] == "cool" and obs["k_active"] < 0
        assert e._state.thermal_model_cache["k_active_cool"] == pytest.approx(obs["k_active"])

    def test_reject_returns_code_and_records_nothing(self, tmp_path):
        e = _engine(tmp_path)
        assert e.record_hvac_run(_run(k_passive=None)) == (None, REJECT_NO_K_PASSIVE)
        assert e.record_hvac_run(_run(minutes=2.0)) == (None, REJECT_HVAC_RUN_TOO_SHORT)
        assert e._state.thermal_observations == []

    def test_hvac_runs_never_change_k_passive(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_passive": -0.031, "observation_count_passive": 9}
        for _ in range(6):
            e.record_hvac_run(_run())
            e.record_hvac_run(_run("cool"))
        assert e._state.thermal_model_cache["k_passive"] == pytest.approx(-0.031)
        assert all(o["k_passive"] is None for o in e._state.thermal_observations)

    def test_hvac_runs_do_not_create_k_passive(self, tmp_path):
        e = _engine(tmp_path)
        e.record_hvac_run(_run())
        assert e._state.thermal_model_cache.get("k_passive") is None


class TestAgeing:
    def test_weighted_median_basics(self):
        assert weighted_median([]) is None
        assert weighted_median([(3.0, 1.0)]) == 3.0
        assert weighted_median([(1.0, 1.0), (5.0, 1.0)]) == 3.0
        assert weighted_median([(1.0, 1.0), (2.0, 1.0), (9.0, 5.0)]) == 9.0

    def test_legacy_observations_are_down_weighted(self, tmp_path):
        """At equal grade and age, a new-method observation outweighs a legacy one."""
        e = _engine(tmp_path)
        e._state.thermal_observations = [
            _obs("heat", 2.0),  # legacy (no method)
            _obs("heat", 6.0, method="run_edge"),
        ]
        e._state.thermal_model_cache = {}
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_active_heat"] == 6.0
        assert THERMAL_LEGACY_OBS_WEIGHT < 1.0

    def test_old_observations_decay(self, tmp_path):
        e = _engine(tmp_path)
        age = int(THERMAL_RATE_HALF_LIFE_DAYS) * 3
        e._state.thermal_observations = [
            _obs("heat", 2.0, days_ago=0, method="run_edge"),
            *[_obs("heat", 6.0, days_ago=age, method="run_edge") for _ in range(3)],
        ]
        e._state.thermal_model_cache = {}
        e._recompute_aged_rates()
        # three 3-half-life-old obs weigh 3/8 of one fresh one: the fresh value wins
        assert e._state.thermal_model_cache["k_active_heat"] == 2.0

    def test_new_run_edge_dominates_within_a_few_commits(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_observations = [_obs("heat", 2.0, days_ago=d) for d in (3, 5, 7, 9)]
        e._state.thermal_model_cache = {"k_active_heat": 2.0, "k_passive": K_P}
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_active_heat"] == 2.0
        for _ in range(3):
            e.record_hvac_run(_run())
        new_k = e._state.thermal_observations[-1]["k_active"]
        assert e._state.thermal_model_cache["k_active_heat"] == pytest.approx(new_k)

    def test_prior_keeps_continuity_at_upgrade(self, tmp_path):
        """No retained observations but a cached value: the prior is stored and the value is kept."""
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_active_heat": 4.2, "k_active_cool": -3.1, "k_passive": -0.04}
        e._ensure_rate_prior()
        e._recompute_aged_rates()
        c = e._state.thermal_model_cache
        assert (c["k_active_heat"], c["k_active_cool"], c["k_passive"]) == (4.2, -3.1, -0.04)
        assert set(c["rate_prior"]) == {"k_active_heat", "k_active_cool", "k_passive"}

    def test_prior_is_taken_once(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_active_heat": 4.2}
        e._ensure_rate_prior()
        stamp = e._state.thermal_model_cache["rate_prior"]["k_active_heat"]["ts"]
        e._state.thermal_model_cache["k_active_heat"] = 9.9
        e._ensure_rate_prior()
        assert e._state.thermal_model_cache["rate_prior"]["k_active_heat"] == {"value": 4.2, "ts": stamp}

    def test_prior_ages_out_behind_new_observations(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_active_heat": 9.0, "k_passive": K_P}
        e._ensure_rate_prior()
        e.record_hvac_run(_run())
        e.record_hvac_run(_run())
        new_k = e._state.thermal_observations[-1]["k_active"]
        assert e._state.thermal_model_cache["k_active_heat"] == pytest.approx(new_k)

    def test_no_observations_keeps_value(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_active_heat": 4.2, "rate_prior": {}}
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_active_heat"] == 4.2

    def test_ageing_never_falls_to_none(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_passive": -0.04}
        e._ensure_rate_prior()
        e._state.thermal_observations = [_obs("heat", 5.0, days_ago=80)]
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_passive"] == pytest.approx(-0.04)

    def test_legacy_heat_k_passive_feeds_k_passive_at_legacy_weight(self, tmp_path):
        e = _engine(tmp_path)
        legacy = {
            "hvac_mode": "heat",
            "k_active": 3.0,
            "k_passive": -0.08,
            "date": _date(0),
            "confidence_grade": "high",
        }
        e._state.thermal_observations = [legacy, _obs("passive", -0.02, grade="low")]
        e._state.thermal_model_cache = {}
        e._recompute_aged_rates()
        # legacy high: 1.0 * 0.25 = 0.25 ; passive low: 0.3 -> the passive fit wins
        assert e._state.thermal_model_cache["k_passive"] == -0.02

    def test_vent_observations_never_feed_k_passive(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_observations = [
            _obs("passive", -0.02),
            {"hvac_mode": "vent_window", "k_passive": -0.3, "date": _date(0), "confidence_grade": "high"},
            {"hvac_mode": "vent_fan", "k_passive": -0.4, "date": _date(0), "confidence_grade": "high"},
        ]
        e._state.thermal_model_cache = {}
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_passive"] == -0.02

    def test_load_recomputes_and_keeps_prior(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_observations = [_obs("heat", 5.0, method="run_edge")]
        e._state.thermal_model_cache = {"k_active_heat": 2.0, "observation_count_heat": 1}
        e.save_state()
        again = LearningEngine(tmp_path)
        again.load_state()
        c = again._state.thermal_model_cache
        assert c["rate_prior"]["k_active_heat"]["value"] == 2.0
        assert c["k_active_heat"] == 5.0  # 0.6 new vs 0.25 prior


class TestDutyBalanceFit:
    FIT = {
        "k_passive": -0.04,
        "k_active_heat": 4.5,
        "k_active_cool": -5.5,
        "n_intervals": 60,
        "r_squared": 0.8,
        "heat_hours": 6.0,
        "cool_hours": 3.0,
        "window_start": "2026-10-01 00:00",
        "window_end": "2026-10-30 23:30",
    }

    def test_records_three_observations(self, tmp_path):
        e = _engine(tmp_path)
        assert e.record_duty_balance_fit(dict(self.FIT)) == 3
        by_mode = {o["hvac_mode"]: o for o in e._state.thermal_observations}
        assert set(by_mode) == {"heat", "cool", "passive"}
        assert all(o["method"] == "duty_balance" and o["source"] == "duty_balance" for o in by_mode.values())
        assert by_mode["heat"]["k_active"] == 4.5 and by_mode["heat"]["k_passive"] is None
        assert by_mode["passive"]["k_passive"] == -0.04
        c = e._state.thermal_model_cache
        assert (c["k_active_heat"], c["k_active_cool"], c["k_passive"]) == (4.5, -5.5, -0.04)

    def test_same_window_is_deduped(self, tmp_path):
        e = _engine(tmp_path)
        e.record_duty_balance_fit(dict(self.FIT))
        assert e.record_duty_balance_fit(dict(self.FIT)) == 0
        assert len(e._state.thermal_observations) == 3

    def test_unidentified_terms_are_skipped(self, tmp_path):
        e = _engine(tmp_path)
        assert e.record_duty_balance_fit(dict(self.FIT, k_active_cool=None)) == 2

    def test_newer_window_replaces_older_fit(self, tmp_path):
        e = _engine(tmp_path)
        e.record_duty_balance_fit(dict(self.FIT))
        assert e.record_duty_balance_fit(dict(self.FIT, window_end="2026-11-02 23:30", k_active_heat=4.8)) == 3
        assert len(e._state.thermal_observations) == 3
        assert e._state.thermal_model_cache["observation_count_heat"] == 1
        assert e._state.thermal_model_cache["k_active_heat"] == 4.8

    def test_duty_balance_is_a_chart_log_fit_source(self):
        from custom_components.climate_advisor.learning import _CHART_LOG_FIT_SOURCES

        assert "duty_balance" in _CHART_LOG_FIT_SOURCES


class TestGateBridgeRemoved:
    def test_physics_eligibility_never_promotes_k_vent_window(self):
        assert physics_eligibility("none", "none", None, -0.2) == (False, None)
        eligible, _k = physics_eligibility("high", "none", None, -0.2)
        assert eligible is False and _k is None

    def test_real_k_passive_still_eligible(self):
        assert physics_eligibility("low", "none", -0.05, -0.2) == (True, -0.05)

    def test_reason_text_names_the_missing_closed_house_rate(self):
        ok, reason = explain_physics_eligibility("none", "none", None, -0.2)
        assert ok is False and "k_vent_window" in reason

    def test_model_rates_has_no_k_vent_promotion(self):
        assert model_rates({"k_passive": None, "k_vent_window": -0.2}) is None
        assert model_rates({"k_passive": 0.01, "k_vent_window": -0.2}) is None
        rates = model_rates({"k_passive": -0.05, "k_vent_window": -0.2})
        assert rates is not None and rates.k_passive == -0.05 and rates.k_vent_window == -0.2

    def test_get_thermal_model_keys_stable(self, tmp_path):
        model = _engine(tmp_path).get_thermal_model()
        for key in ("k_active_heat", "k_active_cool", "k_passive", "k_vent_window", "observation_count_heat"):
            assert key in model
