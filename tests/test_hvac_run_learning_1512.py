"""Issue #1512 / #1516: ageing of the stored heat/cool/passive rates, and the removed gate bridge.

The per-run run-edge estimator and the duty-balance fit are retired (the pooled cycle model replaced them, see
tests/test_cycle_wiring_1512.py); their STORED observations still feed the weighted-median ageing below so old
values fade instead of jumping at upgrade. Also covers the removal of the k_vent_window -> k_passive gate bridge.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.const import (
    THERMAL_LEGACY_OBS_WEIGHT,
    THERMAL_RATE_HALF_LIFE_DAYS,
)
from custom_components.climate_advisor.learning import (
    LearningEngine,
    weighted_median,
)
from custom_components.climate_advisor.operation_timeline import model_rates
from custom_components.climate_advisor.thermal_confidence import explain_physics_eligibility, physics_eligibility

K_P = -0.05


def _date(days_ago: int) -> str:
    return (dt_util.now().date() - timedelta(days=days_ago)).isoformat()


def _stored_run_edge(e: LearningEngine, k: float = 6.0) -> dict:
    """A run-edge observation as an older version stored it (the writer is retired; the stored ones age out)."""
    obs = _obs("heat", k, method="run_edge")
    obs["k_passive"] = None
    e.record_thermal_observation(obs)
    return obs


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
            _stored_run_edge(e)
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
        # old expectation had no "n": the prior must carry the evidence count behind the value (1 when none known)
        assert e._state.thermal_model_cache["rate_prior"]["k_active_heat"] == {"value": 4.2, "ts": stamp, "n": 1}

    def test_prior_ages_out_behind_new_observations(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {"k_active_heat": 9.0, "k_passive": K_P}
        e._ensure_rate_prior()
        _stored_run_edge(e)
        _stored_run_edge(e)
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
        e._state.thermal_model_cache = {"k_active_heat": 2.0, "observation_count_heat": 20}
        e.save_state()
        again = LearningEngine(tmp_path)
        again.load_state()
        c = again._state.thermal_model_cache
        assert c["rate_prior"]["k_active_heat"]["value"] == 2.0
        assert c["rate_prior"]["k_active_heat"]["n"] == 20
        # Old expectation (5.0) jumped to the single retained observation on first load because the prior weighed
        # 0.25 however many observations stood behind the cached value; 20 observations (weight 5.0) outweigh it (0.6).
        assert c["k_active_heat"] == 2.0

    def test_prior_carries_the_evidence_count_per_field(self, tmp_path):
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {
            "k_active_heat": 5.0,
            "k_active_cool": -6.0,
            "k_passive": -0.05,
            "observation_count_heat": 65,
            "observation_count_cool": 286,
            "observation_count_passive": 35,
        }
        e._ensure_rate_prior()
        prior = e._state.thermal_model_cache["rate_prior"]
        assert prior["k_active_heat"]["n"] == 65
        assert prior["k_active_cool"]["n"] == 286
        assert prior["k_passive"]["n"] == 35 + 65 + 286  # passive, heat and cool all fed the old k_passive EWMA

    def test_upgrade_does_not_jump_and_then_fades(self, tmp_path):
        """A few new fits cannot yank a value backed by hundreds of observations; the prior fades with the half-life."""
        e = _engine(tmp_path)
        e._state.thermal_model_cache = {
            "k_passive": -0.0482,
            "observation_count_passive": 35,
            "observation_count_heat": 65,
            "observation_count_cool": 286,
        }
        e._state.thermal_observations = [_obs("passive", -0.0264, grade="high") for _ in range(10)]
        e._ensure_rate_prior()
        e._recompute_aged_rates()
        assert e._state.thermal_model_cache["k_passive"] == pytest.approx(-0.0482)  # old behaviour: -0.0264
        # a long time later with no fresh evidence the prior (96.5 -> ~0.4) has faded below the observations
        from custom_components.climate_advisor.learning import aged_rates

        prior = e._state.thermal_model_cache["rate_prior"]
        late = dt_util.now().date() + timedelta(days=int(THERMAL_RATE_HALF_LIFE_DAYS * 8))
        obs = [_obs("passive", -0.0264, grade="high")]
        obs[0]["date"] = late.isoformat()
        assert aged_rates(obs, late, prior)["k_passive"] == pytest.approx(-0.0264)

    def test_k_passive_confidence_gives_way_only_to_new_passive_observations(self, tmp_path):
        """Owner decision 2026-11-11 ("fade only on new evidence"). Occupant: a closed-house leak rate learned before
        the upgrade keeps the bedtime-setback and forecast physics on through a summer with no passive window; the old
        evidence yields only as new passive observations replace it."""
        from datetime import UTC, datetime

        from custom_components.climate_advisor.const import THERMAL_EVIDENCE_HALF_N
        from custom_components.climate_advisor.thermal_confidence import (
            grade_passive_confidence,
            passive_evidence_count,
        )

        now = datetime.now(UTC)
        stamp = now.isoformat()
        base = {"observation_count_passive": 3, "observation_count_heat": 100, "observation_count_cool": 100}
        # pre-upgrade shape (no prior yet): heat/cool runs fed k_passive, so they still count
        assert grade_passive_confidence(dict(base), now) == "high"
        # upgraded: the prior's evidence (n=203) counts at full weight on day 0 ...
        up = {**base, "rate_prior": {"k_passive": {"value": -0.05, "ts": stamp, "n": 203}}}
        assert grade_passive_confidence(up, now) == "high"
        # ... and time alone changes nothing (was: "none" after 8 half-lives with no new observation)
        later = now + timedelta(days=THERMAL_RATE_HALF_LIFE_DAYS * 8)
        assert grade_passive_confidence(up, later) == "high"
        assert passive_evidence_count(up, later) == pytest.approx(3 + 203)
        # new passive observations displace it: keep halves every THERMAL_EVIDENCE_HALF_N of them
        e = LearningEngine(tmp_path)
        e.load_state()
        e._state.thermal_model_cache = {
            "k_passive": -0.05,
            **up,
            "rate_prior": {"k_passive": dict(up["rate_prior"]["k_passive"])},
        }
        for i in range(int(THERMAL_EVIDENCE_HALF_N)):
            e._update_thermal_model_cache(
                {"hvac_mode": "passive", "k_passive": -0.05, "date": f"2026-11-{1 + i % 9:02d}"}
            )
        c = e._state.thermal_model_cache
        assert c["rate_prior"]["k_passive"]["keep"] == pytest.approx(0.5, abs=1e-4)
        assert passive_evidence_count(c, later) == pytest.approx(3 + THERMAL_EVIDENCE_HALF_N + 203 * 0.5, abs=0.1)
        # heat/cool observations never displace the leak rate's evidence
        e._update_thermal_model_cache({"hvac_mode": "heat", "k_active": 6.0, "date": "2026-11-10"})
        assert c["rate_prior"]["k_passive"]["keep"] == pytest.approx(0.5, abs=1e-4)


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
