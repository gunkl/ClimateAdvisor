"""Issue #1512 final verification: a crash that loses the newest cycles never double-counts the survivors.

Occupant: the learning file is saved after every refit, the cycle file only periodically, so after an unclean restart
the stored fit may have seen a cycle or two the cycle file lost. The refit after the restart used to treat that fit
as "left the pool" (it looked for the fit's newest cycle only), hold it at full weight and use it as the prior of a
refit over the same surviving cycles: their evidence counted twice (40.75 instead of 22.0), and the heating or cooling
rate then answered a real change of the equipment about half as fast (6.30 instead of 4.87 F/h after 18 cycles).
Now the refit after a crash is a normal refit of the survivors, the same as a home that never saw the lost cycles.
Losing those cycles is fine (owner); the save timing is unchanged.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from custom_components.climate_advisor import cycle_model as cm
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.thermal_confidence import hvac_evidence_count
from tests.test_cycle_model_1512 import synth

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _engine(tmp_path) -> LearningEngine:
    e = LearningEngine(tmp_path)
    e.load_state()
    e._state.thermal_model_cache = {
        "k_passive": -0.048,
        "k_active_heat": 5.5,
        "k_active_cool": -6.1,
        "observation_count_heat": 10,
        "observation_count_cool": 10,
        "observation_count_passive": 50,
    }
    with patch("custom_components.climate_advisor.learning.dt_util") as dt:
        dt.now.return_value = NOW
        e._ensure_rate_prior()
    return e


def _refit(e: LearningEngine, pool: list[dict], mode: str, v3: bool) -> dict:
    with patch("custom_components.climate_advisor.learning.dt_util") as dt:
        dt.now.return_value = NOW
        prior = cm.prior_from_cache(copy.deepcopy(e._state.thermal_model_cache), mode, NOW, cycles=pool)
        r = cm.fit_cycles(pool, mode, prior, now_ts=NOW.timestamp())
        e.apply_cycle_fit(mode, r, v3)
    c = e._state.thermal_model_cache
    return {
        "q": r["q"],
        "prior": r.get("prior_source"),
        "held": bool(c[f"cycle_fit_{mode}"].get("held")),
        "ev": hvac_evidence_count(c, mode),
    }


def _shifted(cycles: list[dict], after: float) -> list[dict]:
    off = after + 3600 - cycles[0]["t_on"]
    out = []
    for m in cycles:
        m = copy.deepcopy(m)
        m["t_on"] += off
        m["t_off"] += off
        m["id"] = "m" + m["id"]
        m["samples"] = [[s[0] + off, s[1], s[2]] for s in m["samples"]]
        m["outdoor"] = [[o[0] + off, o[1]] for o in m["outdoor"]]
        out.append(m)
    return out


@pytest.mark.parametrize("v3", [True, False], ids=["v3", "v4"])
@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_refit_after_a_crash_equals_a_home_that_never_saw_the_lost_cycles(tmp_path, mode, v3):
    sign = 1.0 if mode == "heat" else -1.0
    cyc = synth(mode, q=7.0 * sign, n=30, seed=3)
    crashed, control = _engine(tmp_path / "a"), _engine(tmp_path / "b")
    for i in range(3, 31, 3):
        _refit(crashed, cyc[:i], mode, v3)  # the learning file saw all 30 cycles
    survivors = cyc[:28]  # the cycle file lost the newest two
    for i in [*range(3, 28, 3), 28]:
        b = _refit(control, cyc[:i], mode, v3)
    for _ in range(3):  # the refit after the restart, and the next ones over the same pool
        a = _refit(crashed, survivors, mode, v3)
        assert not a["held"] and a["prior"] == "cache"  # old behaviour: held, prior "held"
        assert a["q"] == pytest.approx(b["q"], abs=1e-3)
        # old behaviour: 40.75 against 22.02; the crashed home saw two more cycles before, so slightly less
        assert a["ev"] == pytest.approx(b["ev"], abs=0.5)
    # the equipment changes: both homes follow it at the same pace
    more = _shifted(synth(mode, q=5.0 * sign, n=18, seed=4), cyc[-1]["t_off"])
    for i in range(3, 19, 3):
        a = _refit(crashed, survivors + more[:i], mode, v3)
        b = _refit(control, survivors + more[:i], mode, v3)
        assert a["q"] == pytest.approx(b["q"], abs=1e-3)  # old behaviour after 18 heat cycles: 6.15 vs 5.28
        assert a["ev"] == pytest.approx(b["ev"], abs=0.5)


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_a_sensor_swap_still_holds_the_fit(tmp_path, mode):
    """The mirror must keep working: when the cycles the fit was fitted on are gone (a new sensor), it is held."""
    sign = 1.0 if mode == "heat" else -1.0
    e = _engine(tmp_path)
    old = synth(mode, q=7.0 * sign, n=12, seed=3)
    _refit(e, old, mode, True)
    new = _shifted(synth(mode, q=7.0 * sign, n=3, seed=5, entity="sensor.new"), old[-1]["t_off"])
    a = _refit(e, new, mode, True)
    assert a["held"] and a["prior"] == "held"
