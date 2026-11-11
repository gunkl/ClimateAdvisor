"""Tests for the pooled cycle model (Issues #1512 / #1516): ``custom_components/climate_advisor/cycle_model.py``.

A small seeded synthetic generator (threshold-free: scheduled runs on a lagged, noisy, quantized sensor) ports the
poc2 validation: fine-sensor convergence, whole-degree short cycles never worse than the prior, coast
positive / zero / negative, spikes and restart patterns, a wild cycle, existing-home prior (nothing jumps),
heat/cool mirror, grade calibration, resolution inference and the pinned-k rules.
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor import cycle_model as cm

T0 = 1_760_000_000.0  # epoch seconds of the synthetic timeline start


# --------------------------------------------------------------------------- synthetic generator
def quant(x: float, res: float, off: float) -> float:
    return round(round((x - off) / res) * res + off, 6)


def synth(
    mode: str = "heat",
    *,
    q: float = 7.0,
    k: float = -0.04,
    c: float = 0.0,
    tau: float = 5.0,
    res: float = 0.1,
    n: int = 25,
    seed: int = 1,
    run: tuple[float, float] = (10.0, 28.0),
    noise: float | None = None,
    entity: str = "sensor.room",
    lag: float = 2.0,
) -> list[dict]:
    """``n`` cycles of ``mode``: scheduled run (``run`` minutes), 45 min tail, next on 5-40 min later.

    Truth: dT/dt = k (T - To) + q u + c sum exp(-(t - t_off)/tau) (earlier tails carry over). The sensor reads a
    lagged air temperature + noise, quantized to ``res`` at a random phase; one sample a minute. q is the
    signed rate (cool < 0). Returns public cycle dicts.
    """
    rnd = random.Random(seed)
    sgn = 1.0 if mode == "heat" else -1.0
    noise = (0.04 if res < 0.5 else 0.06) if noise is None else noise
    sp = 68.0 if mode == "heat" else 76.0
    to_base = 40.0 if mode == "heat" else 96.0
    ph = rnd.uniform(0, 1440)
    phase = rnd.uniform(-0.5, 0.5) * res

    def tout(t: float) -> float:
        return to_base + 5.0 * math.sin(2 * math.pi * (t + ph) / 1440.0)

    cycles: list[dict] = []
    offs: list[float] = []
    t = 20.0
    h = 0.25
    for i in range(n):
        dur = rnd.uniform(*run)
        t_on = t + 3.0
        t_off = t_on + dur
        t_end = t_off + 45.0
        temp = sp + rnd.gauss(0, 0.3)
        sensed = temp
        tt = t
        samples = []
        out = []
        next_s = t
        while tt <= t_end + 1e-9:
            u = 1.0 if t_on <= tt < t_off else 0.0
            coast = sum(c * math.exp(-(tt - o) / tau) for o in (*offs, t_off) if tt >= o)
            rate = (k * (temp - tout(tt)) + q * u + coast) / 60.0
            temp += rate * h
            sensed += (temp - sensed) * min(1.0, h / lag) if lag > 0 else temp - sensed
            tt += h
            if tt >= next_s:
                v = quant(sensed + rnd.gauss(0, noise), res, phase)
                samples.append([T0 + tt * 60.0, v, 1 if u else 0])
                if int(round(tt)) % 5 == 0:
                    out.append([T0 + tt * 60.0, round(tout(tt), 2)])
                next_s += 1.0
        offs.append(t_off)
        cycles.append(
            {
                "id": f"c{i}",
                "mode": mode,
                "t_on": T0 + t_on * 60.0,
                "t_off": T0 + t_off * 60.0,
                "cut": None,
                "res": res,
                "samples": samples,
                "outdoor": out,
                "setpoint_f": sp,
                "entity": entity,
            }
        )
        t = t_end + rnd.uniform(5.0, 40.0) - 3.0
    del sgn
    return cycles


def fit(cycles: list[dict], mode: str = "heat", prior: dict | None = None) -> dict:
    now = cycles[-1]["t_off"] + 3600 if cycles else T0
    return cm.fit_cycles(cycles, mode, prior or cm.typical_prior(mode), now_ts=now)


def rel(a: float, b: float) -> float:
    return abs(a - b) / abs(b)


def add_spikes(cycles: list[dict], seed: int, prob: float = 0.02) -> None:
    """Isolated +-2..5 F single-reading spikes (a sensor glitch), in place."""
    rnd = random.Random(seed)
    for cyc in cycles:
        for s in cyc["samples"]:
            if rnd.random() < prob:
                s[1] = round(s[1] + rnd.choice([-1, 1]) * rnd.uniform(2.0, 5.0), 2)


def add_restarts(cycles: list[dict], seed: int, prob: float = 0.3) -> int:
    """Thermostat restart after the off edge: 2 min of no readings, then 5 min of readings biased +-1..3 F."""
    rnd = random.Random(seed)
    hit = 0
    for cyc in cycles:
        if rnd.random() >= prob:
            continue
        hit += 1
        g0 = cyc["t_off"] + 30.0
        g1 = g0 + 120.0
        bias = rnd.choice([-1, 1]) * rnd.uniform(1.0, 3.0)
        kept = []
        for s in cyc["samples"]:
            if g0 <= s[0] < g1:
                continue
            if g1 <= s[0] < g1 + 300.0:
                s = [s[0], round(s[1] + bias, 2), s[2]]
            kept.append(s)
        cyc["samples"] = kept
    return hit


# --------------------------------------------------------------------------- resolution
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([70, 70, 71, 71, 72, 71, 71, 70], 1.0),  # whole degree
        ([70.0, 70.5, 71.0, 71.0, 70.5, 70.0], 0.5),  # half degree
        ([70.0, 70.1, 70.2, 70.2, 70.3, 70.2, 70.1], 0.1),  # fine
        ([70.0, 70.1, 70.2, 70.3, 74.0, 70.3, 70.4, 70.5], 0.1),  # one spike (two big steps) cannot move the mode
        ([70, 70, 70], 1.0),  # no change at all
        ([], 1.0),
        ([70.0, 70.2, 70.0, 70.5, 70.0, 70.5], 0.5),  # 0.2 x2, 0.5 x3 -> mode is 0.5
        ([70.0, 70.1, 70.2, 70.4, 70.5], 0.1),  # tie 0.1 x3 vs 0.2 x1 -> 0.1; a 2-2 tie goes to the smaller step
        ([70.0, 70.5, 70.0, 71.0, 70.0, 71.0, 70.0], 1.0),  # 0.5 x2, 1.0 x4
    ],
)
def test_infer_resolution(values: list[float], expected: float) -> None:
    assert cm.infer_resolution(values) == pytest.approx(expected)


def test_infer_resolution_tie_prefers_smaller() -> None:
    assert cm.infer_resolution([70.0, 70.1, 70.0, 70.5, 70.0]) == pytest.approx(0.1)


# --------------------------------------------------------------------------- cleaning
def _flat(n: int = 40, level: float = 68.0, res: float = 0.1) -> list[list[float]]:
    return [[T0 + 60.0 * i, level + (0.1 if i % 3 == 0 else 0.0), 0] for i in range(n)]


def test_clean_series_removes_isolated_spikes_and_keeps_clean_data() -> None:
    rows = _flat()
    assert cm.clean_series(rows, 0.1) == rows  # clean data untouched, input not mutated
    rows[10][1] += 4.0
    rows[25][1] -= 3.0
    out = cm.clean_series(rows, 0.1)
    assert len(out) == len(rows) - 2
    assert all(abs(r[1] - 68.0) < 0.5 for r in out)


def test_clean_series_restart_rule_drops_biased_leading_readings() -> None:
    rows = [r for r in _flat(60) if not (20 <= (r[0] - T0) / 60.0 < 24)]  # 4 min hole
    for r in rows:
        if 24 <= (r[0] - T0) / 60.0 < 28:
            r[1] += 2.5  # restarted thermostat reports stale values first
    out = cm.clean_series(rows, 0.1)
    assert all(abs(r[1] - 68.0) < 0.5 for r in out)
    assert len(out) == len(rows) - 4


def test_clean_series_short_input_passthrough() -> None:
    rows = _flat(4)
    assert cm.clean_series(rows, 0.1) == rows


# --------------------------------------------------------------------------- convergence
def test_fine_sensor_converges_within_5pct_by_25_cycles() -> None:
    for seed in range(3):
        r = fit(synth(q=7.0, seed=seed, lag=1.5))
        assert r["ne"] == 25
        assert rel(r["q"], 7.0) < 0.05, (seed, r["q"])
        assert r["grade"] == "high"
        assert r["sd_q"] < 0.3


def test_fine_sensor_error_shrinks_with_more_cycles() -> None:
    cyc = synth(q=6.0, seed=4, lag=1.5)
    errs = {n: rel(fit(cyc[:n])["q"], 6.0) for n in (1, 3, 10, 25)}
    prior_err = rel(8.0, 6.0)
    assert errs[1] < prior_err
    assert errs[25] < 0.05
    assert errs[25] <= errs[3] + 0.02


def test_first_cycle_is_used_pulled_toward_prior() -> None:
    r = fit(synth(q=5.0, seed=2, n=1, lag=1.5))
    assert r["ne"] == 1
    assert r["grade"] in ("low", "medium")
    assert abs(r["q"] - 5.0) < abs(8.0 - 5.0)  # better than the typical-house default already
    assert r["k_pinned"] is True  # fine pool: k pinned until 3 eligible cycles


def test_cool_mode_negative_rate() -> None:
    r = fit(synth("cool", q=-8.0, seed=3, lag=1.5), "cool")
    assert r["q"] < 0
    assert rel(r["q"], -8.0) < 0.06
    assert r["grade"] == "high"


def test_heat_cool_exact_mirror() -> None:
    heat = synth("heat", q=6.5, seed=5, lag=1.5)
    cool = []
    for c in heat:
        d = dict(c)
        d["mode"] = "cool"
        d["samples"] = [[s[0], round(200.0 - s[1], 6), s[2]] for s in c["samples"]]
        d["outdoor"] = [[o[0], 200.0 - o[1]] for o in c["outdoor"]]
        cool.append(d)
    ph = {"q": (8.0, 5.0), "k": (-0.03, 0.06), "area": (0.0, 2.0)}
    pc = {"q": (-8.0, 5.0), "k": (-0.03, 0.06), "area": (0.0, 2.0)}
    rh = fit(heat, "heat", ph)
    rc = fit(cool, "cool", pc)
    assert rc["q"] == pytest.approx(-rh["q"], rel=1e-6)
    assert rc["coast_area_f"] == pytest.approx(-rh["coast_area_f"], rel=1e-6, abs=1e-9)
    assert rc["sd_q"] == pytest.approx(rh["sd_q"], rel=1e-6)
    assert rc["grade"] == rh["grade"]


# --------------------------------------------------------------------------- coast
@pytest.mark.parametrize(("c", "tau"), [(6.0, 5.0), (0.0, 5.0), (-6.0, 5.0)])
def test_coast_positive_zero_negative(c: float, tau: float) -> None:
    truth_area = c * tau / 60.0
    r = fit(synth(q=7.0, c=c, tau=tau, seed=6, lag=0.0, run=(8.0, 25.0)))
    assert rel(r["q"], 7.0) < 0.07, r["q"]
    assert abs(r["coast_area_f"] - truth_area) < 0.25, (r["coast_area_f"], truth_area)
    if c > 0:
        assert r["coast_area_f"] > 0.2
    elif c < 0:
        assert r["coast_area_f"] < -0.2
    else:
        assert abs(r["coast_area_f"]) < 0.25


# --------------------------------------------------------------------------- artifacts
def test_spikes_cleaned() -> None:
    cyc = synth(q=7.0, seed=7, lag=1.5)
    add_spikes(cyc, seed=70, prob=0.03)
    r = fit(cyc)
    assert rel(r["q"], 7.0) < 0.08, r["q"]


def test_restart_pattern_cleaned() -> None:
    cyc = synth(q=7.0, seed=8, lag=1.5)
    assert add_restarts(cyc, seed=80, prob=0.4) >= 4
    add_spikes(cyc, seed=81, prob=0.01)
    r = fit(cyc)
    assert rel(r["q"], 7.0) < 0.08, r["q"]


def test_wild_cycle_damped_from_cycle_three() -> None:
    good = synth(q=6.0, seed=9, n=6, lag=1.5)
    wild = [dict(c) for c in good]
    # cycle 2 (index 1): a drift pulls the reading 6 F the wrong way over the second half of the run
    cyc = wild[1]
    mid = (cyc["t_on"] + cyc["t_off"]) / 2
    cyc["samples"] = [
        [s[0], round(s[1] - 6.0 * min(1.0, max(0.0, (s[0] - mid) / 600.0)), 2), s[2]] for s in cyc["samples"]
    ]
    clean3 = fit(good[:3])
    wild3 = fit(wild[:3])
    assert abs(wild3["q"] - 6.0) <= max(0.3 * 6.0, abs(clean3["q"] - 6.0) + 0.2)
    assert wild3["grade"] in ("low", "medium")
    wild6 = fit(wild)
    assert rel(wild6["q"], 6.0) < 0.15


# --------------------------------------------------------------------------- whole-degree short cycles
@pytest.mark.parametrize("n", [1, 2, 4, 8, 20])
def test_whole_degree_short_cycles_never_worse_than_prior(n: int) -> None:
    prior = cm.typical_prior("heat")
    worse = 0
    for seed in range(6):
        qt = random.Random(seed).uniform(3.5, 11.0)
        cyc = synth(q=qt, res=1.0, n=n, seed=seed, run=(4.0, 8.0), lag=0.0)
        r = fit(cyc)
        assert r["grade"] in ("none", "low"), (n, seed, r["grade"])
        if abs(r["q"] - qt) > abs(prior["q"][0] - qt) + 0.5:
            worse += 1
    assert worse == 0


def test_no_eligible_cycle_returns_prior_unchanged() -> None:
    prior = cm.typical_prior("heat")
    r = fit(synth(q=2.0, res=1.0, n=5, seed=1, run=(3.0, 5.0), lag=0.0))
    assert r["ne"] == 0 and r["grade"] == "none"
    assert r["q"] == prior["q"][0] and r["sd_q"] == prior["q"][1]
    assert r["k_p_cycle"] == prior["k"][0]
    assert r["reasons"].get(cm.REASON_SIGNAL, 0) + r["reasons"].get(cm.REASON_TOO_SHORT, 0) == 5


def test_whole_degree_long_runs_learn_but_never_high() -> None:
    r = fit(synth(q=7.0, res=1.0, n=30, seed=2, run=(15.0, 35.0), lag=0.0))
    assert r["ne"] >= 10
    assert rel(r["q"], 7.0) < 0.2, r["q"]
    assert r["grade"] in ("low", "medium")
    assert r["resolution_f"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- k pinning
def test_k_pinned_on_coarse_pool() -> None:
    prior = cm.typical_prior("heat")
    r = fit(synth(q=7.0, k=-0.1, res=1.0, n=30, seed=3, run=(15.0, 35.0), lag=0.0))
    assert r["k_pinned"] is True
    assert r["k_p_cycle"] == prior["k"][0]
    assert r["sd_k"] == prior["k"][1]


def test_k_free_on_fine_pool_after_three_eligible_cycles() -> None:
    cyc = synth(q=7.0, k=-0.06, seed=4, lag=1.5)
    assert fit(cyc[:2])["k_pinned"] is True
    r = fit(cyc)
    assert r["k_pinned"] is False
    assert r["k_p_cycle"] < 0
    assert r["sd_k"] < 0.06


# --------------------------------------------------------------------------- gates / exclusions
def test_window_fan_natvent_flags_exclude_cycle() -> None:
    cyc = synth(q=7.0, seed=10, n=6, lag=1.5)
    for bit in (2, 4, 8):
        mod = [dict(c) for c in cyc]
        mod[2]["samples"] = [list(s) for s in cyc[2]["samples"]]
        mod[2]["samples"][20][2] |= bit
        r = fit(mod)
        assert r["reasons"].get(cm.REASON_WINDOW_FAN) == 1
        assert r["ne"] == 5


def test_setpoint_cut_and_cut_cycles() -> None:
    cyc = synth(q=7.0, seed=11, n=6, lag=1.5)
    # Issue #1512 finding 3: a setpoint cut keeps its run portion (it used to be excluded, losing the normal
    # commanded-stop cycle); with an off edge it is an ordinary cycle whose recording ended at the change
    mod = [dict(c) for c in cyc]
    mod[1]["cut"] = "setpoint_change"
    r = fit(mod)
    assert not r["reasons"] and r["ne"] == 6
    # the run went on past the change: the part up to the change (no off edge, no coast) still counts
    sp = dict(cyc[2])
    sp["cut"], sp["t_off"] = "setpoint_change", None
    sp["samples"] = [list(s) for s in cyc[2]["samples"] if s[0] <= cyc[2]["t_on"] + 8 * 60]
    mod[2] = sp
    r = fit(mod)
    assert r["ne"] == 6
    # too short a run portion is reported as the setpoint change that cut it
    sp2 = dict(sp)
    sp2["samples"] = [list(s) for s in cyc[2]["samples"] if s[0] <= cyc[2]["t_on"] + 60]
    mod[2] = sp2
    r = fit(mod)
    assert r["reasons"] == {cm.REASON_SETPOINT: 1} and r["ne"] == 5
    # a restart that cut the cycle mid-run with no off edge: only the clean pre-gap part counts
    mod = [dict(c) for c in cyc]
    cut_cycle = dict(cyc[3])
    cut_cycle["cut"] = "restart"
    cut_cycle["t_off"] = None
    cut_cycle["samples"] = [list(s) for s in cyc[3]["samples"] if s[0] <= cyc[3]["t_on"] + 8 * 60]
    mod[3] = cut_cycle
    r = fit(mod)
    assert r["ne"] >= 5
    # too short to qualify -> reported as the cut
    cut_cycle2 = dict(cut_cycle)
    cut_cycle2["samples"] = [list(s) for s in cyc[3]["samples"] if s[0] <= cyc[3]["t_on"] + 1 * 60]
    mod[3] = cut_cycle2
    r = fit(mod)
    assert r["reasons"].get(cm.REASON_GAP_RESTART, 0) + r["reasons"].get(cm.REASON_TOO_SHORT, 0) == 1


def test_sensor_swap_resets_pool_to_newest_sensor() -> None:
    old = synth(q=7.0, res=1.0, n=5, seed=12, run=(15.0, 30.0), entity="sensor.old", lag=0.0)
    new = synth(q=7.0, res=0.1, n=8, seed=13, entity="sensor.new", lag=1.5)
    shift = old[-1]["t_off"] + 4 * 3600 - new[0]["t_on"]
    for c in new:
        c["t_on"] += shift
        c["t_off"] += shift
        c["samples"] = [[s[0] + shift, s[1], s[2]] for s in c["samples"]]
        c["outdoor"] = [[o[0] + shift, o[1]] for o in c["outdoor"]]
    r = fit(old + new)
    assert r["reasons"].get(cm.REASON_SENSOR_SWAP) == 5
    assert r["resolution_f"] == pytest.approx(0.1)
    assert r["n_cycles"] == 8


def test_other_mode_cycles_ignored_and_empty_input() -> None:
    cool = synth("cool", q=-8.0, n=3, seed=14, lag=1.5)
    r = fit(cool, "heat")
    assert r["ne"] == 0 and r["grade"] == "none"
    assert fit([], "heat")["grade"] == "none"


def test_plateau_trim_ignores_idle_tail_of_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # reading reaches its final level 20 min before the equipment-off edge (modulating unit): the long idle tail must
    # not drag q down as if the unit had kept delivering for the full flagged time.
    cyc = synth(q=7.0, seed=15, n=12, run=(12.0, 14.0), lag=1.5)
    stretched = []
    for c in cyc:
        d = dict(c)
        d["t_off"] = c["t_off"] + 20 * 60.0
        # hold the reading flat for the extra 20 minutes (thermostat satisfied, flag lagging)
        end_level = [s for s in c["samples"] if s[0] <= c["t_off"]][-1][1]
        samples = []
        for s in c["samples"]:
            if s[0] > c["t_off"]:
                s = [s[0] + 20 * 60.0, s[1], s[2]]
            samples.append(s)
        hold = [[c["t_off"] + 60.0 * m, end_level, 1] for m in range(1, 21)]
        d["samples"] = sorted(samples + hold, key=lambda s: s[0])
        stretched.append(d)
    r = fit(stretched)
    assert rel(r["q"], 7.0) < 0.15, r["q"]
    monkeypatch.setattr(cm, "CYCLE_PLATEAU_MIN", 1e9)  # trim disabled: the idle tail biases q low
    untrimmed = fit(stretched)
    assert rel(r["q"], 7.0) < rel(untrimmed["q"], 7.0)


# --------------------------------------------------------------------------- priors
def test_typical_prior_values_and_validation() -> None:
    h, c = cm.typical_prior("heat"), cm.typical_prior("cool")
    assert h["q"] == (8.0, 5.0) and c["q"] == (-8.83, 5.5)
    assert h["k"] == (-0.03, 0.06) and h["area"] == (0.0, 2.0)
    with pytest.raises(ValueError, match="mode"):
        cm.typical_prior("fan")


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _cache(q: float = 7.11, n: int = 65, age_days: float = 0.0, field: str = "k_active_heat") -> dict:
    ts = (NOW - timedelta(days=age_days)).isoformat()
    return {"rate_prior": {field: {"value": q, "ts": ts, "n": n}, "k_passive": {"value": -0.052, "ts": ts, "n": 300}}}


def test_prior_from_cache_fallbacks() -> None:
    typ = cm.typical_prior("heat")
    for bad in (None, {}, {"rate_prior": None}, {"rate_prior": {}}, "x"):
        p = cm.prior_from_cache(bad, "heat", NOW)
        assert p["q"] == typ["q"] and p["source"] == "typical"
    # wrong sign / non-numeric fields fall back to typical
    assert cm.prior_from_cache(_cache(q=-7.0), "heat", NOW)["q"] == typ["q"]
    assert cm.prior_from_cache(_cache(q="x"), "heat", NOW)["q"] == typ["q"]  # type: ignore[arg-type]
    # cool reads k_active_cool and keeps its negative sign
    pc = cm.prior_from_cache(_cache(q=-8.0, field="k_active_cool"), "cool", NOW)
    assert pc["source"] == "cache" and pc["q"][0] == pytest.approx(-8.0, abs=0.3)


def test_prior_from_cache_evidence_and_ageing() -> None:
    p0 = cm.prior_from_cache(_cache(n=65), "heat", NOW)
    assert p0["q"][0] == pytest.approx(7.11, abs=0.05)
    assert p0["q"][1] < 1.0  # strong evidence -> tight
    few = cm.prior_from_cache(_cache(n=3), "heat", NOW)
    assert few["q"][1] > p0["q"][1]
    # owner decision 2026-11-11: time alone changes nothing (was: looser after 60 days, typical after 400)
    aged = cm.prior_from_cache(_cache(n=65, age_days=400), "heat", NOW)
    assert aged["q"] == p0["q"] and aged["n_eff"] == p0["n_eff"]
    # new evidence displaces it: the carried share ``keep`` scales the evidence
    half = _cache(n=65)
    half["rate_prior"]["k_active_heat"]["keep"] = 0.5
    ph = cm.prior_from_cache(half, "heat", NOW)
    assert ph["n_eff"] == pytest.approx(p0["n_eff"] * 0.5, rel=1e-6) and ph["q"][1] > p0["q"][1]
    gone = _cache(n=65)
    gone["rate_prior"]["k_active_heat"]["keep"] = 1e-6
    ancient = cm.prior_from_cache(gone, "heat", NOW)
    assert abs(ancient["q"][0] - 8.0) < 0.2  # displaced: back to the typical house
    assert ancient["q"][1] == pytest.approx(5.0, rel=0.05)
    # date / epoch accepted for ``now``
    assert cm.prior_from_cache(_cache(), "heat", NOW.date())["q"][1] > 0
    assert cm.prior_from_cache(_cache(), "heat", NOW.timestamp())["q"][1] > 0


@pytest.mark.parametrize("truth_shift", [0.5, 0.25, -0.25, -0.5])
def test_existing_home_nothing_jumps_at_cycle_one(truth_shift: float) -> None:
    """poc2 regime: short fine-sensor runs (3-6 min); the stored rate (65 observations) is the evidence to beat."""
    q_old = 7.11
    prior = cm.prior_from_cache(_cache(q=q_old, n=65), "heat", NOW)
    qt = q_old * (1 + truth_shift)
    cyc = synth(q=qt, seed=16, n=10, lag=1.5, run=(3.0, 6.0))
    now_ts = NOW.timestamp()
    for c in cyc:  # place the cycles at "now" so age weights are ~1
        sh = now_ts - c["t_off"] - 3600
        c["t_on"] += sh
        c["t_off"] += sh
        c["samples"] = [[s[0] + sh, s[1], s[2]] for s in c["samples"]]
        c["outdoor"] = [[o[0] + sh, o[1]] for o in c["outdoor"]]
    steps = []
    last = q_old
    qs = []
    for n in (1, 2, 3, 5, 10):
        r = cm.fit_cycles(cyc[:n], "heat", prior, now_ts=now_ts)
        qs.append(r["q"])
        steps.append(abs(r["q"] - last) / q_old)
        last = r["q"]
    assert steps[0] < 0.10, steps  # cycle 1 never jumps (stored rate carries its evidence)
    assert max(steps[1:]) < 0.10, steps  # no cliff afterwards either
    # and the evidence still moves it the right way
    assert (qs[-1] - q_old) * truth_shift >= -0.05  # short runs: at worst a hair of typical-house pull


@pytest.mark.parametrize("truth_shift", [0.5, -0.5])
def test_existing_home_informative_cycle_moves_toward_truth_without_overshoot(truth_shift: float) -> None:
    """A long, clean cycle is real evidence: the rate moves, in the right direction, never past the truth."""
    q_old = 7.11
    prior = cm.prior_from_cache(_cache(q=q_old, n=65), "heat", NOW)
    qt = q_old * (1 + truth_shift)
    cyc = synth(q=qt, seed=16, n=3, lag=1.5)
    now_ts = NOW.timestamp()
    for c in cyc:
        sh = now_ts - c["t_off"] - 3600
        c["t_on"] += sh
        c["t_off"] += sh
        c["samples"] = [[s[0] + sh, s[1], s[2]] for s in c["samples"]]
        c["outdoor"] = [[o[0] + sh, o[1]] for o in c["outdoor"]]
    q1 = cm.fit_cycles(cyc[:1], "heat", prior, now_ts=now_ts)["q"]
    assert (q1 - q_old) * truth_shift > 0
    assert abs(q1 - qt) < abs(q_old - qt)
    assert (q1 - qt) * truth_shift <= 0.05


# --------------------------------------------------------------------------- grade
@pytest.mark.parametrize(
    ("res", "ne", "er", "expected"),
    [
        (0.1, 0, 0.0, "none"),
        (1.0, 0, 0.99, "none"),
        (0.1, 1, 0.99, "low"),
        (0.1, 2, 0.6, "medium"),
        (0.1, 2, 0.59, "low"),
        (0.1, 3, 0.99, "medium"),
        (0.1, 4, 0.95, "high"),
        (0.1, 4, 0.94, "medium"),
        (1.0, 1, 0.99, "low"),
        (1.0, 2, 0.95, "medium"),
        (1.0, 2, 0.94, "low"),
        (1.0, 50, 1.0, "medium"),  # a whole-degree sensor is never high
        (0.5, 50, 1.0, "medium"),
    ],
)
def test_grade_table(res: float, ne: int, er: float, expected: str) -> None:
    assert cm.grade({"ne": ne, "er_q": er, "resolution_f": res}) == expected


def test_grade_calibration_sanity() -> None:
    """Higher grades are (on average) more accurate; coarse short-run data never claims high."""
    by_grade: dict[str, list[float]] = {}
    for seed in range(8):
        qt = random.Random(seed * 3 + 1).uniform(4.0, 11.0)
        for n in (1, 3, 8, 20):
            for res, run in ((0.1, (8.0, 25.0)), (1.0, (6.0, 14.0))):
                cyc = synth(q=qt, res=res, n=n, seed=seed, run=run, lag=0.0)
                r = fit(cyc)
                if res >= 0.5:
                    assert r["grade"] != "high"
                by_grade.setdefault(r["grade"], []).append(abs(r["q"] - qt) / qt)
    med = {g: sorted(v)[len(v) // 2] for g, v in by_grade.items()}
    assert "high" in med and "none" in med
    assert med["high"] < 0.08
    assert med["high"] < med["none"]
    if "low" in med:
        assert med["high"] <= med["low"]


# --------------------------------------------------------------------------- misc / runtime
def test_age_weighting_prefers_recent_cycles() -> None:
    old = synth(q=4.0, seed=17, n=12, lag=1.5)
    new = synth(q=8.0, seed=18, n=12, lag=1.5)
    t_old_end = old[-1]["t_off"]
    shift = (new[0]["t_on"] - t_old_end) * 0 + 120 * 86400.0  # new cycles 120 days later
    for c in new:
        c["t_on"] += shift
        c["t_off"] += shift
        c["samples"] = [[s[0] + shift, s[1], s[2]] for s in c["samples"]]
        c["outdoor"] = [[o[0] + shift, o[1]] for o in c["outdoor"]]
    r = fit(old + new)
    assert r["q"] > 6.5  # 120-day-old cycles carry ~2% of the weight


def test_cap_at_last_60_eligible_cycles() -> None:
    cyc = synth(q=7.0, seed=19, n=70, lag=1.5)
    r = fit(cyc)
    assert r["ne"] == cm.CYCLE_MAX_CYCLES
    assert r["n_cycles"] == 70


def test_deterministic_and_inputs_not_mutated() -> None:
    cyc = synth(q=7.0, seed=20, n=8, lag=1.5)
    snap = [[list(s) for s in c["samples"]] for c in cyc]
    a, b = fit(cyc), fit(cyc)
    assert a == b
    assert snap == [c["samples"] for c in cyc]


def test_solar_regressor_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cm.CYCLE_SOLAR_REGRESSOR is False
    cyc = synth(q=7.0, seed=21, n=8, lag=1.5)
    for c in cyc:
        c["solar"] = [[c["t_on"] - 600, 0.5], [c["t_off"] + 3000, 0.5]]
    assert "solar_coeff" not in fit(cyc)
    monkeypatch.setattr(cm, "CYCLE_SOLAR_REGRESSOR", True)
    r = fit(cyc)
    assert "solar_coeff" in r and rel(r["q"], 7.0) < 0.1


def test_runtime_60_cycles_bounded() -> None:
    cyc = synth(q=7.0, seed=22, n=60, run=(8.0, 15.0), lag=1.5)
    n_samples = sum(len(c["samples"]) for c in cyc) / len(cyc)
    t0 = time.perf_counter()
    r = fit(cyc)
    dt = time.perf_counter() - t0
    print(f"fit_cycles: 60 cycles x ~{n_samples:.0f} samples in {dt:.2f}s")  # noqa: T201
    assert r["ne"] == 60
    assert dt < 10.0


def test_importable_standalone_without_package() -> None:
    """tools load the module by file path (no HA, no package): the const fallback must work."""
    path = Path(cm.__file__)
    spec = importlib.util.spec_from_file_location("cycle_model_standalone", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cycle_model_standalone"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("cycle_model_standalone", None)
    assert mod.typical_prior("heat")["q"] == (8.0, 5.0)
    assert mod.EVIDENCE_HALF_N == 20 and mod.HELD_MIN_KEEP == 0.01


# ---------------------------------------------------------------------------------------------------------------
# Owner decision 2026-11-11 ("Fade only on new evidence"): no clock in the fit
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["heat", "cool"])
def test_a_refit_after_a_quiet_season_gives_the_same_answer(mode: str) -> None:
    """Occupant: a home that does not heat (cool) for months keeps its learned rate and its grade; a restart's
    refit of the same cycles 1, 30, 89 or 400 days later is identical (was: every cycle's weight halved per 21 days,
    so the prior took over and the grade fell with time alone)."""
    cyc = synth(mode, q=7.0 if mode == "heat" else -7.0, n=20)
    base_ts = cyc[-1]["t_off"] + 3600
    prior = cm.typical_prior(mode)
    first = cm.fit_cycles(cyc, mode, prior, now_ts=base_ts)
    for days in (1, 30, 89, 400):
        again = cm.fit_cycles(cyc, mode, prior, now_ts=base_ts + days * 86400)
        assert again == first
    # weights count newer eligible cycles: 0.5 ** (j / EVIDENCE_HALF_N), j = 0 for the newest
    ne = first["ne"]
    assert first["ne_w"] == pytest.approx(sum(0.5 ** (j / cm.EVIDENCE_HALF_N) for j in range(ne)))
    assert len(first["t_eligible"]) == ne and max(first["t_eligible"]) == first["t_last"]
