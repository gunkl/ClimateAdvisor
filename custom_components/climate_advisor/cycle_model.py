"""Pooled cycle model for the heating / cooling rate (Issues #1512 / #1516). Pure Python, math only.

What the occupant gets: every home has a heating / cooling rate that is never worse than the typical-house
default, improves with every informative HVAC cycle, says honestly how sure it is (a grade), survives sensor
spikes, thermostat restarts, the post-off coast (or immediate drop) and both short (5-8 min) and long
(recovery) runs, and never jumps when an existing home upgrades (its old values age out).

This module is a port of the validated proof of concept "R2F" (poc2 ``est4.fit_final`` / ``grade`` / ``infer_res``
on the ridge core ``est5.fit_core``, with the poc ``pooled.py`` level-mode machinery and ``shrink.py`` design-effect
calibration). No HA imports: it is importable standalone by ``tools/thermal_replay.py`` and
``tools/predict_backtest.py`` (DRY - the production function).

Model (minutes inside, rates in F/h), per mode and per cycle ``j``::

    dT/dt = k*(T - T_out) + q*u(t) + c * sum_o exp(-(t - t_off_o)/tau) * [t > t_off_o]
    level fit:  T_i = a_cycle + q*Q_i + k*K_i + c*C_i(tau)        (a_cycle free per cycle)

``Q`` is the cumulative run-hours, ``K`` the cumulative degree-hours against outdoor, ``C(tau)`` the coast
(post-off) response with earlier off-edge tails superposed (back 120 min). tau is profiled over a grid
(1.5-20 min) and Bayesian-averaged. All estimates are MAP / ridge against a prior (``typical_prior`` for a fresh
home, ``prior_from_cache`` for an existing one), so the first cycle already gives a usable, honest number.

Pipeline (``fit_cycles``): exclusions -> per-pool resolution (``infer_resolution``) -> ``clean_series`` (Hampel +
restart rule) -> plateau trim -> eligibility gate (run >= 3 min AND >= 3 resolution steps of robust movement) ->
last 60 eligible cycles, age-weighted 0.5 ** (age_d / 21) -> ridge fit (Huber IRLS, calibrated design effect) ->
bounds -> ``grade``. No eligible cycle returns the prior unchanged (grade ``none``).

Cycle dict: ``{"id", "mode", "t_on", "t_off"|None, "cut", "res", "samples": [[t_epoch_s, T_f, flags]],
"outdoor": [[t, T_out]], "setpoint_f", "entity"}``; flags bit0 running, bit1 windows, bit2 fan, bit3 nat_vent,
bit4 gap_before, bit5 unavailable. Optional ``"solar": [[t, factor]]`` is read only when ``CYCLE_SOLAR_REGRESSOR``.

Deterministic, no global state, no I/O.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

try:  # in the integration package; a standalone tool import (no package, no const.py) uses the same literals
    from . import const as _const
except ImportError:  # pragma: no cover - standalone import
    _const = None


def _k(name: str, default: Any) -> Any:
    """const.py value ``name`` (agent R's THERMAL_CYCLE_* block) or the identical literal when const is absent."""
    return getattr(_const, name, default)


THERMAL_LEGACY_OBS_WEIGHT: float = _k("THERMAL_LEGACY_OBS_WEIGHT", 0.25)
THERMAL_RATE_HALF_LIFE_DAYS: float = _k("THERMAL_RATE_HALF_LIFE_DAYS", 21.0)

# --------------------------------------------------------------------------- constants (const.py THERMAL_CYCLE_*)
CYCLE_MIN_RUN_MIN: float = _k("THERMAL_CYCLE_MIN_RUN_MIN", 3.0)
CYCLE_MOVE_STEPS: float = _k("THERMAL_CYCLE_MOVE_STEPS", 3.0)
CYCLE_MOVE_AFTER_OFF_MIN: float = _k("THERMAL_CYCLE_MOVE_WINDOW_AFTER_OFF_MIN", 3.0)
CYCLE_COARSE_RES_F: float = _k("THERMAL_CYCLE_COARSE_RES_F", 0.5)
CYCLE_K_FREE_AFTER: int = _k("THERMAL_CYCLE_K_FREE_AFTER", 3)
CYCLE_PLATEAU_MIN: float = _k("THERMAL_CYCLE_PLATEAU_MIN", 10.0)
CYCLE_MAX_CYCLES: int = _k("THERMAL_CYCLE_MAX_FIT_CYCLES", 60)
CYCLE_WEIGHT_HALF_LIFE_DAYS: float = _k("THERMAL_CYCLE_AGE_HALF_LIFE_DAYS", 21.0)
CYCLE_SUPERPOSE_MIN: float = _k("THERMAL_CYCLE_TAIL_BACK_MIN", 120.0)
CYCLE_COARSE_EVID_N: float = _k("THERMAL_CYCLE_COARSE_EVID_N", 3)
CYCLE_FINE_DEFF_MULT: float = _k("THERMAL_CYCLE_DEFF_FINE_MULT", 0.5)
CYCLE_SOLAR_REGRESSOR: bool = _k("THERMAL_CYCLE_SOLAR_REGRESSOR", False)  # OFF: no poc benefit worth the risk
CYCLE_TAUS: tuple[float, ...] = _k("THERMAL_CYCLE_TAUS", (1.5, 2.5, 4.0, 6.0, 8.0, 11.0, 15.0, 20.0))
CYCLE_IRLS_ITERS: int = _k("THERMAL_CYCLE_IRLS_ITERS", 5)
CYCLE_HUBER_K: float = _k("THERMAL_CYCLE_HUBER_K", 1.345)
CYCLE_K_SD_PINNED: float = _k("THERMAL_CYCLE_K_SD_PINNED", 0.004)
CYCLE_GRADE_MED_ER: float = _k("THERMAL_CYCLE_GRADE_MED_ER", 0.6)
CYCLE_GRADE_HIGH_ER: float = _k("THERMAL_CYCLE_GRADE_HIGH_ER", 0.95)
CYCLE_GRADE_MED_NE: int = _k("THERMAL_CYCLE_GRADE_MED_NE", 2)
CYCLE_GRADE_HIGH_NE: int = _k("THERMAL_CYCLE_GRADE_HIGH_NE", 4)
_DEFF_L_Q = _k("THERMAL_CYCLE_DEFF_L_Q", (2.0, 6.0, 0.35))
_DEFF_L_K = _k("THERMAL_CYCLE_DEFF_L_K", (3.0, 12.0, 1.5))
_DEFF_MAX_Q = _k("THERMAL_CYCLE_DEFF_MAX_Q", (2.0, 25.0, 0.2))
_DEFF_MAX_K = _k("THERMAL_CYCLE_DEFF_MAX_K", (3.5, 10.0, 0.2))
_HAMPEL_HALF = _k("THERMAL_CYCLE_HAMPEL_HALF_MIN", 5.0)
_HAMPEL_MIN_N = _k("THERMAL_CYCLE_HAMPEL_MIN_READINGS", 5)
_HAMPEL_SIGMAS = _k("THERMAL_CYCLE_HAMPEL_SIGMAS", 3.5)
_HAMPEL_RES_MULT = _k("THERMAL_CYCLE_HAMPEL_RES_MULT", 2.0)
_HAMPEL_FLOOR = _k("THERMAL_CYCLE_HAMPEL_FLOOR_F", 0.5)
_RESTART_GAP_MIN = _k("THERMAL_CYCLE_RESTART_GAP_MIN", 2.5)
_RESTART_GAP_DT = _k("THERMAL_CYCLE_RESTART_GAP_DT_MULT", 2.5)
_RESTART_EARLY = _k("THERMAL_CYCLE_RESTART_EARLY_MIN", 6.0)
_RESTART_LATE = _k("THERMAL_CYCLE_RESTART_LATE_MIN", 8.0)
_RESTART_RES_MULT = _k("THERMAL_CYCLE_RESTART_RES_MULT", 2.0)
_RESTART_FLOOR = _k("THERMAL_CYCLE_RESTART_FLOOR_F", 0.8)
_SIGMA_FLOOR_FRAC = _k("THERMAL_CYCLE_SIGMA_FLOOR_RES_FRAC", 0.3)
_SIGMA_FLOOR_MIN = _k("THERMAL_CYCLE_SIGMA_FLOOR_MIN_F", 0.05)
_SIGMA_INIT_ADD = _k("THERMAL_CYCLE_SIGMA_INIT_ADD_F", 0.1)
_MAD_SCALE = _k("THERMAL_CYCLE_MAD_SCALE", 1.4826)
_BOUNDS = {
    "heat": _k("THERMAL_CYCLE_Q_BOUNDS_HEAT", (0.5, 15.0)),
    "cool": _k("THERMAL_CYCLE_Q_BOUNDS_COOL", (-15.0, -0.5)),
}
_K_BOUNDS = _k("THERMAL_CYCLE_K_BOUNDS", (-0.5, -0.001))
CYCLE_TAIL_MIN = 45.0  # samples after the off edge that still belong to the cycle (minutes; recorder closes at off+45)
CYCLE_SOLAR_PRIOR_SD = 3.0  # F/h per unit solar factor, only used when the regressor is on

FLAG_RUNNING = _k("CYCLE_FLAG_RUNNING", 1)
FLAG_WINDOWS = _k("CYCLE_FLAG_WINDOWS", 2)
FLAG_FAN = _k("CYCLE_FLAG_FAN", 4)
FLAG_NAT_VENT = _k("CYCLE_FLAG_NAT_VENT", 8)
FLAG_GAP_BEFORE = _k("CYCLE_FLAG_GAP_BEFORE", 16)
FLAG_UNAVAILABLE = _k("CYCLE_FLAG_UNAVAILABLE", 32)
_EXCLUDE_FLAGS = FLAG_WINDOWS | FLAG_FAN | FLAG_NAT_VENT

_TYPICAL = {
    "heat": {
        "q": _k("THERMAL_CYCLE_PRIOR_Q_HEAT", (8.0, 5.0)),
        "k": _k("THERMAL_CYCLE_PRIOR_K", (-0.03, 0.06)),
        "area": _k("THERMAL_CYCLE_PRIOR_COAST_AREA_F", (0.0, 2.0)),
    },
    "cool": {
        "q": _k("THERMAL_CYCLE_PRIOR_Q_COOL", (-8.83, 5.5)),
        "k": _k("THERMAL_CYCLE_PRIOR_K", (-0.03, 0.06)),
        "area": _k("THERMAL_CYCLE_PRIOR_COAST_AREA_F", (0.0, 2.0)),
    },
}

# Rejection codes (the strings agent R defines in const.py; literal here so the module stays standalone).
REASON_TOO_SHORT = "cycle_too_short"
REASON_SIGNAL = "cycle_signal_below_resolution"
REASON_WINDOW_FAN = "cycle_excluded_window_fan"
REASON_SETPOINT = "cycle_setpoint_change"
REASON_GAP_RESTART = "cycle_gap_restart"
REASON_UNAVAILABLE = "cycle_thermostat_unavailable"
REASON_SENSOR_SWAP = "cycle_sensor_swap"
REASON_NO_OFF = "cycle_no_off_edge"
_CUT_REASON = {"restart": REASON_GAP_RESTART, "unavailable": REASON_UNAVAILABLE}


# --------------------------------------------------------------------------- small numeric helpers
def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return float("nan")
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mad(xs: Sequence[float]) -> float:
    m = _median(xs)
    return _median([abs(x - m) for x in xs])


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gauss-Jordan with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[p][i]) < 1e-14:
            return None
        m[i], m[p] = m[p], m[i]
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                if f:
                    for c in range(i, n + 1):
                        m[r][c] -= f * m[i][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inv(a: list[list[float]]) -> list[list[float]] | None:
    n = len(a)
    cols = []
    for j in range(n):
        s = _solve(a, [1.0 if i == j else 0.0 for i in range(n)])
        if s is None:
            return None
        cols.append(s)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _flags(s: Sequence[float]) -> int:
    return int(s[2]) if len(s) > 2 else 0


# --------------------------------------------------------------------------- resolution
def infer_resolution(values: Sequence[float]) -> float:
    """Per-pool reading resolution: the most common non-zero ``|step|`` between consecutive readings.

    Ties go to the smaller step. A few spikes or off-lattice values cannot move a mode (the round-1
    10th-percentile-of-gaps could be dragged by them; poc2 ``infer_res``). No change at all -> 1.0 F.
    """
    counts: dict[float, int] = {}
    prev: float | None = None
    for v in values:
        if prev is not None and abs(v - prev) > 1e-6:
            key = round(abs(v - prev), 2)
            counts[key] = counts.get(key, 0) + 1
        prev = v
    if not counts:
        return 1.0
    best = max(counts.values())
    return min(k for k, n in counts.items() if n == best)


# --------------------------------------------------------------------------- cleaning
def clean_series(samples: Sequence[Sequence[float]], res: float) -> list[list[float]]:
    """Hampel spike filter + thermostat-restart rule on ``[[t_epoch_s, T_f, flags], ...]``.

    Restart rule: after a gap longer than ``max(2.5 min, 2.5 x median dt)`` (or a sample flagged
    ``gap_before``), leading readings deviating more than ``max(2*res, 0.8 F)`` from the median of the
    following 8-min window (6-14 min after the gap) are dropped until one agrees (a restarted thermostat
    reports stale / default values first). Hampel: half-window 5 min, >= 5 readings, drop when
    ``|x - median| > max(3.5 * 1.4826 * MAD, 2*res, 0.5 F)``. Returns new lists; the input is untouched.
    """
    rows = [list(s) for s in samples]
    n = len(rows)
    if n < 5:
        return rows
    ts = [r[0] / 60.0 for r in rows]
    temps = [r[1] for r in rows]
    mdt = _median([b - a for a, b in zip(ts, ts[1:], strict=False)])
    thr_floor = max(_HAMPEL_RES_MULT * res, _HAMPEL_FLOOR)
    gap_thr = max(_RESTART_GAP_MIN, _RESTART_GAP_DT * mdt)
    drop = [False] * n
    for i in range(1, n):
        if ts[i] - ts[i - 1] > gap_thr or _flags(rows[i]) & FLAG_GAP_BEFORE:
            early = [j for j in range(i, n) if ts[j] - ts[i] <= _RESTART_EARLY]
            late = [j for j in range(i, n) if _RESTART_EARLY < ts[j] - ts[i] <= _RESTART_EARLY + _RESTART_LATE]
            if not early or len(late) < 2:
                continue
            ref = _median([temps[j] for j in late])
            for j in early:
                if abs(temps[j] - ref) > max(_RESTART_RES_MULT * res, _RESTART_FLOOR):
                    drop[j] = True
                else:
                    break
    keep = [i for i in range(n) if not drop[i]]
    kt = [ts[i] for i in keep]
    kv = [temps[i] for i in keep]
    m = len(keep)
    bad = [False] * m
    lo = hi = 0
    for i in range(m):
        while kt[lo] < kt[i] - _HAMPEL_HALF:
            lo += 1
        while hi < m and kt[hi] <= kt[i] + _HAMPEL_HALF:
            hi += 1
        w = kv[lo:hi]
        if len(w) < _HAMPEL_MIN_N:
            continue
        if abs(kv[i] - _median(w)) > max(_HAMPEL_SIGMAS * _MAD_SCALE * _mad(w), thr_floor):
            bad[i] = True
    return [rows[keep[i]] for i in range(m) if not bad[i]]


# --------------------------------------------------------------------------- priors
def typical_prior(mode: str) -> dict[str, Any]:
    """The typical-house prior of a fresh home (owner decision, #1512): heat q 8 +-5 F/h, cool q -8.83 +-5.5,
    k -0.03 +-0.06 /h, coast area 0 +-2 F. ``{"q": (mean, sd), "k": (mean, sd), "area": (mean, sd), ...}``."""
    if mode not in _TYPICAL:
        raise ValueError(f"mode must be 'heat' or 'cool', got {mode!r}")
    t = _TYPICAL[mode]
    return {"q": t["q"], "k": t["k"], "area": t["area"], "source": "typical", "n_eff": 0.0}


def _as_naive_dt(now: Any) -> datetime:
    if isinstance(now, datetime):
        return now.replace(tzinfo=None)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day)
    if _num(now):
        return datetime.fromtimestamp(float(now), UTC).replace(tzinfo=None)
    raise TypeError(f"now must be datetime/date/epoch seconds, got {type(now).__name__}")


def _age_days(ts: Any, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(str(ts)).replace(tzinfo=None)
    except ValueError, TypeError:
        return 0.0
    return max(0.0, (now - then).total_seconds() / 86400.0)


def _combine(mean_a: float, sd_a: float, mean_b: float, sd_b: float) -> tuple[float, float]:
    """Precision-weighted product of two Gaussians (an aged-out legacy prior hands over to the typical house)."""
    pa, pb = 1.0 / (sd_a * sd_a), 1.0 / (sd_b * sd_b)
    return (mean_a * pa + mean_b * pb) / (pa + pb), math.sqrt(1.0 / (pa + pb))


def _n_eff(rec: dict[str, Any], now: datetime) -> float:
    n = float(rec["n"]) if _num(rec.get("n")) and rec["n"] > 0 else 1.0
    age = _age_days(rec.get("ts"), now)
    return max(n * THERMAL_LEGACY_OBS_WEIGHT * 0.5 ** (age / THERMAL_RATE_HALF_LIFE_DAYS), 1e-9)


def prior_from_cache(cache: Any, mode: str, now: Any) -> dict[str, Any]:
    """Prior of an existing home: its stored rates weighted by their evidence, ageing with a 21-day half-life.

    Reads ``cache["rate_prior"]`` (``{"k_active_heat"|"k_active_cool"|"k_passive": {"value", "ts", "n"}}``, written
    once at upgrade by ``learning._ensure_rate_prior``). The evidence is
    ``n_eff = n * THERMAL_LEGACY_OBS_WEIGHT * 0.5 ** (age_d / THERMAL_RATE_HALF_LIFE_DAYS)``; the stored value
    is a pseudo-measurement with sd ``sqrt((0.30*|q|)^2 / n_eff + (0.08*|q|)^2)`` (k: 0.40 / 0.08), the
    poc2 ``legacy_prior``. It is then combined with the typical-house prior by precision, so as ``n_eff`` -> 0 the
    prior hands over smoothly to the typical house (old values age out, nothing jumps at upgrade). Missing /
    wrong-signed / non-numeric fields fall back to the typical value. ``now``: datetime, date or epoch seconds.
    """
    pr = typical_prior(mode)
    rp = cache.get("rate_prior") if isinstance(cache, dict) else None
    if not isinstance(rp, dict):
        return pr
    now_dt = _as_naive_dt(now)
    out = dict(pr)
    n_eff_q = 0.0
    rec = rp.get("k_active_heat" if mode == "heat" else "k_active_cool")
    if (
        isinstance(rec, dict)
        and _num(rec.get("value"))
        and (rec["value"] > 0) == (mode == "heat")
        and rec["value"] != 0
    ):
        q_old = float(rec["value"])
        n_eff_q = _n_eff(rec, now_dt)
        sd_old = math.sqrt((0.30 * abs(q_old)) ** 2 / n_eff_q + (0.08 * abs(q_old)) ** 2)
        out["q"] = _combine(q_old, sd_old, *pr["q"])
    rec = rp.get("k_passive")
    if isinstance(rec, dict) and _num(rec.get("value")) and rec["value"] < 0:
        k_old = float(rec["value"])
        sd_old = math.sqrt((0.40 * abs(k_old)) ** 2 / _n_eff(rec, now_dt) + (0.08 * abs(k_old)) ** 2)
        out["k"] = _combine(k_old, sd_old, *pr["k"])
    if n_eff_q:
        out["source"] = "cache"
        out["n_eff"] = n_eff_q
    return out


# --------------------------------------------------------------------------- cycle preparation
def _interp(xs: list[float], ys: list[float], x: float) -> float:
    i = bisect.bisect_right(xs, x)
    if i == 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    return ys[i - 1] if x1 <= x0 else ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def _prepare(cyc: dict[str, Any], res: float, prior_offs: list[float]) -> tuple[dict[str, Any] | None, str | None]:
    """Internal (minutes) cycle from a public cycle dict: window, clean, features source. Returns (cycle, reason)."""
    ton = cyc["t_on"] / 60.0
    toff_raw = cyc.get("t_off")
    toff = None if toff_raw is None else toff_raw / 60.0
    samples = sorted((list(s) for s in cyc.get("samples", [])), key=lambda s: s[0])
    run_end = toff
    cut = cyc.get("cut")
    if cut:
        # a restart / unavailable cut: only the clean pre-gap part counts, and there is no usable off edge after it.
        for s in samples:
            if s[0] / 60.0 > ton and _flags(s) & (FLAG_GAP_BEFORE | FLAG_UNAVAILABLE):
                cut_t = s[0] / 60.0
                samples = [x for x in samples if x[0] / 60.0 < cut_t]
                if toff is None or toff > cut_t:
                    toff, run_end = None, cut_t
                break
        if run_end is None and samples:
            run_end = samples[-1][0] / 60.0
    if run_end is None:
        return None, REASON_NO_OFF
    end = run_end + (CYCLE_TAIL_MIN if toff is not None else 3.0)
    samples = [s for s in samples if s[0] / 60.0 <= end + 1e-9]
    if any(_flags(s) & _EXCLUDE_FLAGS for s in samples):
        return None, REASON_WINDOW_FAN
    samples = clean_series(samples, res)
    # one pre-on reading (the level at the on edge) + everything after; at least 4 rows (poc build_cycles)
    pre = [s for s in samples if s[0] / 60.0 <= ton]
    rows = ([pre[-1]] if pre else []) + [s for s in samples if s[0] / 60.0 > ton]
    if len(rows) < 4 or (not pre and rows[0][0] / 60.0 - ton > 6.0):
        return None, REASON_TOO_SHORT
    t = [r[0] / 60.0 for r in rows]
    temps = [float(r[1]) for r in rows]
    outdoor = cyc.get("outdoor") or []
    if outdoor:
        ot = [o[0] / 60.0 for o in outdoor]
        ov = [float(o[1]) for o in outdoor]
        to = [_interp(ot, ov, x) for x in t]
    else:
        to = [sum(temps) / len(temps)] * len(t)  # no outdoor: K carries no signal (k is pinned then anyway)
    c: dict[str, Any] = {
        "id": cyc.get("id"),
        "ton": ton,
        "toff": toff,
        "rend": run_end,
        "t": t,
        "T": temps,
        "To": to,
        "w": 1.0,
        "cut": str(cut or ""),
        "priors": [o for o in prior_offs if t[0] - CYCLE_SUPERPOSE_MIN < o < t[0]],
    }
    solar = cyc.get("solar")
    if solar:
        stt = [x[0] / 60.0 for x in solar]
        sv = [float(x[1]) for x in solar]
        sr = [_interp(stt, sv, x) for x in t]
        xs = [0.0]
        for j in range(1, len(t)):
            xs.append(xs[-1] + 0.5 * (sr[j - 1] + sr[j]) * (t[j] - t[j - 1]) / 60.0)
        c["X"] = xs
    return c, None


def _moved_steps(c: dict[str, Any], res: float) -> float:
    """Robust reading movement (resolution steps) over [on, off + 3 min]; one extreme dropped each side."""
    lim = c["rend"] + CYCLE_MOVE_AFTER_OFF_MIN
    vals = [c["T"][j] for j in range(len(c["t"])) if c["t"][j] <= lim]
    if len(vals) < 2:
        return 0.0
    if len(vals) >= 6:
        vals.sort()
        return (vals[-2] - vals[1]) / res
    return (max(vals) - min(vals)) / res


def _trim_plateau(c: dict[str, Any], mode: str) -> dict[str, Any]:
    """Reading at its final level >= CYCLE_PLATEAU_MIN before the off edge: effective off = first reached.

    The unit was not delivering its full rate in that tail (thermostat modulation / flag lag). Needs a true
    off edge; returns a shallow copy with ``toff`` / ``rend`` moved, or the cycle itself when nothing to trim.
    """
    if c["toff"] is None:
        return c
    t, temps = c["t"], c["T"]
    idx = [j for j in range(len(t)) if t[j] <= c["toff"] + 1e-9]
    if len(idx) < 3:
        return c
    t_end = temps[idx[-1]]
    sgn = 1.0 if mode == "heat" else -1.0
    tp = None
    for j in idx:
        if t[j] > c["ton"] and (temps[j] - t_end) * sgn >= -1e-9:
            tp = t[j]
            break
    if tp is None or c["toff"] - tp < CYCLE_PLATEAU_MIN:
        return c
    d = dict(c)
    d["toff"] = tp
    d["rend"] = tp
    return d


def _ineligible_reason(c: dict[str, Any], res: float) -> str | None:
    if c["rend"] - c["ton"] < CYCLE_MIN_RUN_MIN:
        return REASON_TOO_SHORT
    if _moved_steps(c, res) < CYCLE_MOVE_STEPS:
        return REASON_SIGNAL
    return None


# --------------------------------------------------------------------------- features + ridge core
def _features(c: dict[str, Any]) -> None:
    t, temps, to = c["t"], c["T"], c["To"]
    n = len(t)
    q_cum = [0.0] * n
    k_cum = [0.0] * n
    for j in range(1, n):
        a, b = t[j - 1], t[j]
        q_cum[j] = q_cum[j - 1] + max(0.0, min(b, c["rend"]) - max(a, c["ton"])) / 60.0
        k_cum[j] = k_cum[j - 1] + 0.5 * ((temps[j - 1] - to[j - 1]) + (temps[j] - to[j])) * (b - a) / 60.0
    c["Q"], c["K"] = q_cum, k_cum
    c["offs_all"] = ([] if c["toff"] is None else [c["toff"]]) + list(c["priors"])


def _coast(c: dict[str, Any], tau: float) -> list[float]:
    t = c["t"]
    n = len(t)
    out = [0.0] * n
    t0 = t[0]
    for o in c["offs_all"]:
        ea = math.exp(-(max(t0, o) - o) / tau)
        for j in range(1, n):
            if t[j] > o:
                out[j] += tau / 60.0 * (ea - math.exp(-(t[j] - o) / tau))
    return out


def _deff_long(total_hours: float) -> tuple[float, float]:
    """Honest-variance inflation (q, k) vs total active-run hours (calibrated on synthetic, poc ``auto_deff_L``)."""
    q, k = _DEFF_L_Q, _DEFF_L_K
    return q[0] + q[1] / (1.0 + (total_hours / q[2]) ** 2), k[0] + k[1] / (1.0 + (total_hours / k[2]) ** 2)


def _deff_max(longest_hours: float) -> tuple[float, float]:
    """Inflation vs the LONGEST run: a pool of only short pulses cannot separate q from the coast area, one
    30+ min recovery can (poc ``auto_deff``)."""
    q, k = _DEFF_MAX_Q, _DEFF_MAX_K
    return (
        q[0] + q[1] / (1.0 + (longest_hours / q[2]) ** 3),
        k[0] + k[1] / (1.0 + (longest_hours / k[2]) ** 3),
    )


def _fit_core(
    cycles: list[dict[str, Any]],
    prior: dict[str, Any],
    mode: str,
    res: float,
    free_k: bool,
    deff_mult: float,
    use_solar: bool,
) -> dict[str, Any] | None:
    """Consistent ridge / MAP over (q, [k], c | tau, [solar]) with Huber IRLS and a tau grid (poc2 est5.fit_core)."""
    if not cycles:
        return None
    for c in cycles:
        _features(c)
    qm, qsd = prior["q"]
    km, ksd = prior["k"]
    am, asd = prior["area"]
    n_par = (3 if free_k else 2) + (1 if use_solar else 0)
    sfloor = max(_SIGMA_FLOOR_FRAC * res, _SIGMA_FLOOR_MIN)
    runs = [(c["rend"] - c["ton"]) / 60.0 for c in cycles]
    d_long = _deff_long(sum(runs))
    d_max = _deff_max(max(runs))
    w_ = 1.0 if len(cycles) < 3 else 0.0
    dq = max(d_long[0], w_ * d_max[0]) * deff_mult
    dk = max(d_long[1], w_ * d_max[1]) * deff_mult
    deff = 0.5 * (dq + dk) if free_k else dq
    cands: list[dict[str, Any]] = []
    for tau in CYCLE_TAUS:
        rows_by = []
        for c in cycles:
            coast = _coast(c, tau)
            feats = []
            for j in range(len(c["T"])):
                f = [c["Q"][j]]
                if free_k:
                    f.append(c["K"][j])
                f.append(coast[j])
                if use_solar:
                    f.append(c["X"][j])
                y = c["T"][j] if free_k else c["T"][j] - km * c["K"][j]
                feats.append((y, f))
            rows_by.append(feats)
        nz = sum(len(r) for r in rows_by)
        nc = len(cycles)
        csd = 60.0 * asd / tau
        cm = 60.0 * am / tau
        m = [qm] + ([km] if free_k else []) + [cm] + ([0.0] if use_solar else [])
        pp = [1.0 / qsd**2] + ([1.0 / ksd**2] if free_k else []) + [1.0 / csd**2]
        if use_solar:
            pp.append(1.0 / CYCLE_SOLAR_PRIOR_SD**2)
        rw = [[1.0] * len(r) for r in rows_by]
        sigma = max(sfloor, _SIGMA_FLOOR_FRAC * res + _SIGMA_INIT_ADD)
        beta: list[float] | None = list(m)
        res_by: list[list[float]] = []
        mat: list[list[float]] = []
        for _ in range(CYCLE_IRLS_ITERS):
            a_mat = [[0.0] * n_par for _ in range(n_par)]
            b_vec = [0.0] * n_par
            for ci, rows in enumerate(rows_by):
                w = [cycles[ci]["w"] * x for x in rw[ci]]
                sw = sum(w)
                if sw <= 0:
                    continue
                ym = sum(wi * r[0] for wi, r in zip(w, rows, strict=True)) / sw
                xm = [sum(wi * r[1][i] for wi, r in zip(w, rows, strict=True)) / sw for i in range(n_par)]
                for wi, (y, x) in zip(w, rows, strict=True):
                    yy = y - ym
                    xx = [x[i] - xm[i] for i in range(n_par)]
                    for i in range(n_par):
                        b_vec[i] += wi * xx[i] * yy
                        for j in range(i, n_par):
                            a_mat[i][j] += wi * xx[i] * xx[j]
            for i in range(n_par):
                for j in range(i):
                    a_mat[i][j] = a_mat[j][i]
            s2 = sigma * sigma
            mat = [
                [
                    a_mat[i][j] / (s2 * deff) + (pp[i] if i == j else 0.0) + (1e-9 if i == j else 0.0)
                    for j in range(n_par)
                ]
                for i in range(n_par)
            ]
            rhs = [b_vec[i] / (s2 * deff) + pp[i] * m[i] for i in range(n_par)]
            beta = _solve(mat, rhs)
            if beta is None:
                break
            res_by = []
            all_r: list[float] = []
            for ci, rows in enumerate(rows_by):
                w = [cycles[ci]["w"] * x for x in rw[ci]]
                sw = sum(w) or 1.0
                ym = sum(wi * r[0] for wi, r in zip(w, rows, strict=True)) / sw
                xm = [sum(wi * r[1][i] for wi, r in zip(w, rows, strict=True)) / sw for i in range(n_par)]
                rr = [(y - ym) - sum(beta[i] * (x[i] - xm[i]) for i in range(n_par)) for (y, x) in rows]
                res_by.append(rr)
                all_r.extend(rr)
            dof = max(nz - nc - n_par, 1) / max(nz, 1)
            sigma = max(_MAD_SCALE * _median([abs(r) for r in all_r]) / math.sqrt(max(dof, 0.2)), sfloor)
            kk = CYCLE_HUBER_K * sigma
            rw = [[1.0 if abs(r) <= kk else kk / abs(r) for r in rr] for rr in res_by]
        if beta is None:
            continue
        kk = CYCLE_HUBER_K * sigma
        sse = 0.0
        for ci, rr in enumerate(res_by):
            for r in rr:
                a = abs(r)
                sse += cycles[ci]["w"] * (r * r if a <= kk else 2 * kk * a - kk * kk)
        pen = sum(pp[i] * (beta[i] - m[i]) ** 2 for i in range(n_par))
        cands.append({"sse": sse, "pen": pen, "beta": beta, "tau": tau, "M": mat, "pp": pp, "sigma": sigma})
    if not cands:
        return None
    sref = min(c_["sigma"] for c_ in cands)
    for c_ in cands:
        c_["obj"] = c_["sse"] / (sref * sref * deff) + c_["pen"]
    best = min(cands, key=lambda c_: c_["obj"])
    o0 = best["obj"]
    ws = [math.exp(-0.5 * (c_["obj"] - o0)) for c_ in cands]
    sw_ = sum(ws)
    ws = [w / sw_ for w in ws]
    mean = list(best["beta"])
    var = [0.0] * n_par
    for w, c_ in zip(ws, cands, strict=True):
        mi = _inv(c_["M"])
        for i in range(n_par):
            vi = mi[i][i] if mi else 0.0
            var[i] += w * (vi + (c_["beta"][i] - mean[i]) ** 2)
    lo, hi = _BOUNDS[mode]
    q = min(max(mean[0], lo), hi)
    ic = 2 if free_k else 1  # index of the coast coefficient
    k = min(max(mean[1], _K_BOUNDS[0]), _K_BOUNDS[1]) if free_k else km
    er_q = max(0.0, min(1.0, 1.0 - var[0] * best["pp"][0]))
    er_k = max(0.0, min(1.0, 1.0 - var[1] * best["pp"][1])) if free_k else 0.0
    tau_b = best["tau"]
    return {
        "q": q,
        "k": k,
        "sd_q": math.sqrt(max(var[0], 0.0)),
        "sd_k": math.sqrt(max(var[1], 0.0)) if free_k else ksd,
        "er_q": er_q,
        "er_k": er_k,
        "tau": tau_b,
        "coast_area_f": mean[ic] * tau_b / 60.0,
        "sigma": best["sigma"],
        "solar_coeff": mean[-1] if use_solar else None,
    }


# --------------------------------------------------------------------------- public fit + grade
def grade(result: dict[str, Any]) -> str:
    """Calibrated confidence grade of a fit result (poc2 calibration tables, #1512).

    ``none``: no eligible cycle. Coarse pool (resolution >= 0.5 F): ``medium`` when er_q >= 0.95 and ne >= 2,
    else ``low`` - a whole-degree sensor is never ``high``. Fine pool: ``high`` er_q >= 0.95 and ne >= 4;
    ``medium`` er_q >= 0.6 and ne >= 2; else ``low``. ``er_q = 1 - posterior variance / prior variance``.
    """
    ne = int(result.get("ne", 0) or 0)
    er = float(result.get("er_q", 0.0) or 0.0)
    if ne == 0:
        return "none"
    res = float(result.get("resolution_f", 1.0) or 1.0)
    if res >= CYCLE_COARSE_RES_F:
        return "medium" if (er >= CYCLE_GRADE_HIGH_ER and ne >= CYCLE_GRADE_MED_NE) else "low"
    if er >= CYCLE_GRADE_HIGH_ER and ne >= CYCLE_GRADE_HIGH_NE:
        return "high"
    if er >= CYCLE_GRADE_MED_ER and ne >= CYCLE_GRADE_MED_NE:
        return "medium"
    return "low"


def _prior_result(prior: dict[str, Any], res: float, n_cycles: int, reasons: dict[str, int]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "q": prior["q"][0],
        "sd_q": prior["q"][1],
        "k_p_cycle": prior["k"][0],
        "sd_k": prior["k"][1],
        "coast_area_f": prior["area"][0],
        "tau": None,
        "ne": 0,
        "n_cycles": n_cycles,
        "er_q": 0.0,
        "resolution_f": res,
        "k_pinned": True,
        "reasons": dict(reasons),
        "prior_source": prior.get("source", "given"),
    }
    out["grade"] = grade(out)
    return out


def fit_cycles(cycles: list[dict[str, Any]], mode: str, prior: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    """Fit the heating or cooling rate of ``mode`` from the stored cycles (poc2 R2F; Issue #1512).

    ``cycles``: public cycle dicts (see module docs; other-mode cycles are ignored). ``prior``: ``typical_prior``
    or ``prior_from_cache``. ``now_ts`` (epoch s) sets the age weights. Returns ``q`` (signed F/h: heat > 0,
    cool < 0), ``sd_q``, ``k_p_cycle`` / ``sd_k`` (diagnostic leak rate; pinned at the prior when
    ``k_pinned``), ``coast_area_f`` (= c*tau/60, post-off drift in F; signed, 0 none), ``tau`` (min),
    ``ne`` (eligible cycles used), ``n_cycles`` (same-mode, same-sensor cycles seen), ``er_q``, ``grade``,
    ``resolution_f`` and ``reasons`` (counts per rejection code of the cycles that did not qualify).
    No eligible cycle returns the prior unchanged (grade ``none``). Never worse than the prior by construction
    (ridge toward it). Pure and deterministic.
    """
    mine = sorted((c for c in cycles if c.get("mode") == mode and c.get("t_on") is not None), key=lambda c: c["t_on"])
    reasons: dict[str, int] = {}

    def _count(code: str) -> None:
        reasons[code] = reasons.get(code, 0) + 1

    # sensor swap: the pool is the newest sensor's cycles; resolution of an older sensor does not carry over
    pool = mine
    if mine:
        ent = mine[-1].get("entity")
        pool = [c for c in mine if c.get("entity") == ent]
        for _ in range(len(mine) - len(pool)):
            _count(REASON_SENSOR_SWAP)
    values: list[float] = []
    for c in pool:
        values.extend(float(s[1]) for s in sorted(c.get("samples", []), key=lambda s: s[0]))
    if any(abs(a - b) > 1e-6 for a, b in zip(values, values[1:], strict=False)):
        res = infer_resolution(values)
    else:
        declared = [float(c["res"]) for c in pool if _num(c.get("res")) and c["res"] > 0]
        res = _median(declared) if declared else 1.0
    prior_offs = sorted(c["t_off"] / 60.0 for c in pool if c.get("t_off") is not None)

    prepared: list[dict[str, Any]] = []
    for c in pool:
        if str(c.get("cut") or "").startswith("setpoint"):
            _count(REASON_SETPOINT)
            continue
        pc, why = _prepare(c, res, prior_offs)
        if pc is None:
            _count(why or REASON_TOO_SHORT)
            continue
        prepared.append(pc)
    eligible: list[dict[str, Any]] = []
    for pc in prepared:
        if _ineligible_reason(pc, res) is None:
            pc = _trim_plateau(pc, mode)
        why = _ineligible_reason(pc, res)
        if why is not None:
            # a cycle cut by a restart / unavailable thermostat that did not leave a usable pre-gap part is
            # reported as the cut, not as a generic weak cycle (its pre-gap part is the only thing it could offer)
            _count(_CUT_REASON.get(pc["cut"], why))
            continue
        eligible.append(pc)
    n_seen = len(pool)
    if not eligible:
        return _prior_result(prior, res, n_seen, reasons)
    eligible = eligible[-CYCLE_MAX_CYCLES:]
    for pc in eligible:
        age_d = max(0.0, (now_ts - pc["ton"] * 60.0) / 86400.0)
        pc["w"] = 0.5 ** (age_d / CYCLE_WEIGHT_HALF_LIFE_DAYS)
    ne = len(eligible)
    coarse = res >= CYCLE_COARSE_RES_F
    free_k = (not coarse) and ne >= CYCLE_K_FREE_AFTER
    deff_mult = max(1.0, (CYCLE_COARSE_EVID_N / ne) ** 2) if coarse else CYCLE_FINE_DEFF_MULT
    use_solar = CYCLE_SOLAR_REGRESSOR and all("X" in pc for pc in eligible)
    fit = _fit_core(eligible, prior, mode, res, free_k, deff_mult, use_solar)
    if fit is None:
        return _prior_result(prior, res, n_seen, reasons)
    out: dict[str, Any] = {
        "q": fit["q"],
        "sd_q": fit["sd_q"],
        "k_p_cycle": fit["k"],
        "sd_k": fit["sd_k"],
        "coast_area_f": fit["coast_area_f"],
        "tau": fit["tau"],
        "ne": ne,
        "n_cycles": n_seen,
        "er_q": fit["er_q"],
        "resolution_f": res,
        "k_pinned": not free_k,
        "sigma": fit["sigma"],
        "reasons": reasons,
        "prior_source": prior.get("source", "given"),
    }
    if use_solar:
        out["solar_coeff"] = fit["solar_coeff"]
    out["grade"] = grade(out)
    return out
