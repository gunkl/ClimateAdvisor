"""Pooled cycle model for the heating / cooling rate (Issues #1512 / #1516). Pure Python, math only.

What the occupant gets: every home has a heating / cooling rate that is never worse than the typical-house
default, improves with every informative HVAC cycle, says honestly how sure it is (a grade), survives sensor
spikes, thermostat restarts, the post-off coast (or immediate drop) and both short (5-8 min) and long
(recovery) runs, and never jumps when an existing home upgrades (its old values age out as new cycles replace
them; never with the passage of time alone, owner decision 2026-11-11).

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
last 60 eligible cycles, weighted 0.5 ** (newer eligible cycles / 20) -> ridge fit (Huber IRLS, calibrated design
effect) -> bounds -> ``grade``. No eligible cycle returns the prior unchanged (grade ``none``).

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
# Evidence yields to newer evidence, never to the calendar (owner decision 2026-11-11): a cycle's weight halves for
# every EVIDENCE_HALF_N newer eligible cycles, and the evidence carried into a prior (``rate_prior`` n, a held fit)
# is scaled by its ``keep`` share, which only ``learning.apply_cycle_fit`` lowers as new cycles arrive.
EVIDENCE_HALF_N: float = _k("THERMAL_EVIDENCE_HALF_N", 20.0)
EVIDENCE_HALF_N_CONTRADICT: float = _k("THERMAL_EVIDENCE_HALF_N_CONTRADICT", 5.0)
HELD_MIN_KEEP: float = _k("THERMAL_EVIDENCE_MIN_KEEP", 0.01)  # a held fit whose share fell below this is dropped

# --------------------------------------------------------------------------- constants (const.py THERMAL_CYCLE_*)
CYCLE_MIN_RUN_MIN: float = _k("THERMAL_CYCLE_MIN_RUN_MIN", 3.0)
CYCLE_MOVE_STEPS: float = _k("THERMAL_CYCLE_MOVE_STEPS", 3.0)
CYCLE_MOVE_AFTER_OFF_MIN: float = _k("THERMAL_CYCLE_MOVE_WINDOW_AFTER_OFF_MIN", 3.0)
CYCLE_COARSE_RES_F: float = _k("THERMAL_CYCLE_COARSE_RES_F", 0.5)
CYCLE_K_FREE_AFTER: int = _k("THERMAL_CYCLE_K_FREE_AFTER", 3)
CYCLE_PLATEAU_MIN: float = _k("THERMAL_CYCLE_PLATEAU_MIN", 10.0)
CYCLE_MAX_CYCLES: int = _k("THERMAL_CYCLE_MAX_FIT_CYCLES", 60)
CYCLE_SUPERPOSE_MIN: float = _k("THERMAL_CYCLE_TAIL_BACK_MIN", 120.0)
CYCLE_COARSE_EVID_N: float = _k("THERMAL_CYCLE_COARSE_EVID_N", 3)
CYCLE_FINE_DEFF_MULT: float = _k("THERMAL_CYCLE_DEFF_FINE_MULT", 0.5)
CYCLE_SOLAR_REGRESSOR: bool = _k("THERMAL_CYCLE_SOLAR_REGRESSOR", False)  # OFF: no poc benefit worth the risk
CYCLE_TAUS: tuple[float, ...] = _k("THERMAL_CYCLE_TAUS", (1.5, 2.5, 4.0, 6.0, 8.0, 11.0, 15.0, 20.0))
CYCLE_IRLS_ITERS: int = _k("THERMAL_CYCLE_IRLS_ITERS", 5)
CYCLE_HUBER_K: float = _k("THERMAL_CYCLE_HUBER_K", 1.345)
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
    "heat": _k("THERMAL_CYCLE_Q_BOUNDS_HEAT", (0.5, 25.0)),
    "cool": _k("THERMAL_CYCLE_Q_BOUNDS_COOL", (-25.0, -0.5)),
}
_K_BOUNDS = _k("THERMAL_CYCLE_K_BOUNDS", (-0.5, -0.001))
# samples after the off edge that still belong to the cycle (minutes): the recorder's post-off window, one definition
CYCLE_TAIL_MIN: float = _k("CYCLE_POST_OFF_WINDOW_S", 45 * 60) / 60.0
CYCLE_SOLAR_PRIOR_SD = 3.0  # F/h per unit solar factor, only used when the regressor is on
# Issue #1512 reopened (owner 2026-11-12): sensor lag, information weighting, honest grades, sensor classes.
CYCLE_INFO_TAU: float = _k("THERMAL_CYCLE_INFO_TAU_MIN", 4.0)
CYCLE_LAGS: tuple[float, ...] = _k("THERMAL_CYCLE_LAGS", (0.0, 1.0, 2.0, 3.0, 4.5))
CYCLE_EDGE_DELAYS: tuple[float, ...] = _k("THERMAL_CYCLE_EDGE_DELAYS", (0.0, 0.5, 1.0))


def timing_grid() -> list[tuple[float, float]]:
    """The (edge delay, sensor lag) pairs (minutes) the fit profiles: every CYCLE_EDGE_DELAYS x CYCLE_LAGS."""
    return [(float(d), float(lg)) for d in (CYCLE_EDGE_DELAYS or (0.0,)) for lg in (CYCLE_LAGS or (0.0,))]


CYCLE_LAG_IDENT_SD: float = _k("THERMAL_CYCLE_LAG_IDENT_SD_MIN", 0.6)
CYCLE_LAG_PLAUSIBLE: float = _k("THERMAL_CYCLE_LAG_PLAUSIBLE_MIN", 3.0)
CYCLE_LAG_RUN_MULT: float = _k("THERMAL_CYCLE_LAG_RUN_MULT", 3.0)
CYCLE_K_DEPEND_FRAC: float = _k("THERMAL_CYCLE_K_DEPEND_FRAC", 0.10)
CYCLE_K_LEARNED_MIN_NEFF: float = _k("THERMAL_CYCLE_K_LEARNED_MIN_NEFF", 1.0)
CYCLE_RES_WHOLE: float = _k("THERMAL_CYCLE_RES_WHOLE_F", 0.9)
CYCLE_RES_HALF: float = _k("THERMAL_CYCLE_RES_HALF_F", 0.45)
MODEL_VERSION: int = _k("THERMAL_CYCLE_MODEL_VERSION", 2)
_LAG_PRUNE_MASS = 0.005  # a timing point below this share of the coarse-grid posterior is not refined (Pi CPU)
# Prior against a late-reported edge (-2 log prior odds): most thermostats report their edge as it happens, so a
# delay must be clearly in the data (a cloud-polled thermostat) before it is used; without it
# a short run's sensor lag and an edge delay look alike.
_DELAY_PRIOR_PEN = 4.0


def _delay_pen(point: tuple[float, float]) -> float:
    return _DELAY_PRIOR_PEN if point[0] > 1e-9 else 0.0


_CONTRADICT_SDS = 3.0  # per-cycle contradiction test (one test per cycle: 2 sd would flag ~5 % by chance)
_K_ALT_SSE = 9.0  # (sse_alt - sse_fit) / (sigma^2 deff) below this: the data cannot tell the two leak rates apart


def sensor_class(res: Any) -> str:
    """``"whole"`` (reports whole degrees), ``"half"`` (half degrees) or ``"fine"`` from a pool's resolution (F).

    Occupant meaning (Issue #1512 reopened, item 5): on a whole- or half-degree sensor a normal heating or cooling
    cycle moves the reading by the thermostat's swing, one or two steps, which cannot measure the rate; only longer
    recoveries (a setback) can. ``"unknown"`` when the resolution is not a positive number."""
    if not _num(res) or res <= 0:
        return "unknown"
    if res >= CYCLE_RES_WHOLE:
        return "whole"
    if res >= CYCLE_RES_HALF:
        return "half"
    return "fine"


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
_CUT_REASON = {
    "restart": REASON_GAP_RESTART,
    "unavailable": REASON_UNAVAILABLE,
    "setpoint_change": REASON_SETPOINT,
}


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
    return {"q": t["q"], "k": t["k"], "area": t["area"], "source": "typical", "n_eff": 0.0, "k_source": "typical"}


def _as_naive_dt(now: Any) -> datetime:
    if isinstance(now, datetime):
        return now.replace(tzinfo=None)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day)
    if _num(now):
        return datetime.fromtimestamp(float(now), UTC).replace(tzinfo=None)
    raise TypeError(f"now must be datetime/date/epoch seconds, got {type(now).__name__}")


def _combine(mean_a: float, sd_a: float, mean_b: float, sd_b: float) -> tuple[float, float]:
    """Precision-weighted product of two Gaussians (an aged-out legacy prior hands over to the typical house)."""
    pa, pb = 1.0 / (sd_a * sd_a), 1.0 / (sd_b * sd_b)
    return (mean_a * pa + mean_b * pb) / (pa + pb), math.sqrt(1.0 / (pa + pb))


def evidence_keep(rec: Any) -> float:
    """The share of its evidence a carried record still holds (``rec["keep"]``, 1.0 when absent): lowered only by
    newer evidence (``learning.apply_cycle_fit``), never by time (owner decision 2026-11-11)."""
    k = rec.get("keep") if isinstance(rec, dict) else None
    return min(1.0, float(k)) if _num(k) and k >= 0 else 1.0


def _n_eff(rec: dict[str, Any]) -> float:
    n = float(rec["n"]) if _num(rec.get("n")) and rec["n"] > 0 else 1.0
    return max(n * THERMAL_LEGACY_OBS_WEIGHT * evidence_keep(rec), 1e-9)


def held_candidate(fit: Any, cycles: Sequence[dict[str, Any]], mode: str) -> dict[str, Any] | None:
    """The fit a new sensor's pool should start from, or None (Issue #1512 verifier finding 4).

    The current fit itself when it has eligible cycles but they are not in ``cycles`` (the sensor was swapped and the
    new sensor's first cycle is being fitted), else the stored ``held`` fit when its cycles are not in ``cycles``.
    A candidate needs a right-signed ``q``, a positive ``sd_q`` and a ``keep`` share of at least HELD_MIN_KEEP."""
    if not isinstance(fit, dict):
        return None
    cands = []
    if _num(fit.get("ne")) and fit["ne"] > 0:
        cands.append(fit)
    if isinstance(fit.get("held"), dict):
        cands.append(fit["held"])
    for c in cands:
        if (
            _num(c.get("q"))
            and (c["q"] > 0) == (mode == "heat")
            and _num(c.get("sd_q"))
            and c["sd_q"] > 0
            and evidence_keep(c) >= HELD_MIN_KEEP
            and not _held_in_pool(c, cycles, mode)
        ):
            return c
    return None


def _held_in_pool(held: dict[str, Any], cycles: Sequence[dict[str, Any]], mode: str) -> bool:
    """True when cycles that produced ``held`` are still in the pool a fit would use (a sensor swapped back, or a
    crash that lost only the newest cycles).

    The pool is the newest sensor's cycles (``fit_cycles``). Any cycle of held's sensor between held's first and last
    eligible cycle (``t_first`` .. ``t_last``) means its evidence is already in the data, so it must not be counted a
    second time as the prior. Issue #1512 final verification: this used to look for held's NEWEST cycle only, so a
    crash that lost the newest cycle or two (the learning file is saved after every refit, the cycle file every
    ``CYCLE_STORE_SAVE_INTERVAL_S``) made the current fit the prior of a refit over the same surviving cycles, held at
    keep 1.0: their evidence counted twice (40.75 instead of 22.0) and the rate answered a real change about half as
    fast. A fit has left the pool only when the cycles it was fitted on are gone (sensor swap, retention). A record
    without ``t_first`` (stored before it existed) counts any older cycle of its sensor."""
    mine = [c for c in cycles if c.get("mode") == mode and _num(c.get("t_on"))]
    if not mine:
        return False
    ent = max(mine, key=lambda c: c["t_on"]).get("entity")
    if ent != held.get("entity") or not _num(held.get("t_last")):
        return False
    lo = float(held["t_first"]) if _num(held.get("t_first")) else -math.inf
    hi = float(held["t_last"])
    return any(c.get("entity") == ent and lo - 1.0 <= c["t_on"] <= hi + 1.0 for c in mine)


def prior_from_cache(
    cache: Any, mode: str, now: Any, *, cycles: Sequence[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Prior of an existing home: its stored rates weighted by the evidence still behind them.

    Reads ``cache["rate_prior"]`` (``{"k_active_heat"|"k_active_cool"|"k_passive": {"value", "ts", "n", "keep"}}``,
    written once at upgrade by ``learning._ensure_rate_prior``). The evidence is
    ``n_eff = n * THERMAL_LEGACY_OBS_WEIGHT * keep``, ``keep`` (1.0 at upgrade) the share newer evidence left it
    (owner decision 2026-11-11: never lowered by time alone, so a quiet season changes nothing); the stored value
    is a pseudo-measurement with sd ``sqrt((0.30*|q|)^2 / n_eff + (0.08*|q|)^2)`` (k: 0.40 / 0.08), the
    poc2 ``legacy_prior``. It is then combined with the typical-house prior by precision, so as ``n_eff`` -> 0 the
    prior hands over smoothly to the typical house as new cycles displace the old evidence (nothing jumps at
    upgrade). Missing / wrong-signed / non-numeric fields fall back to the typical value. ``now`` (datetime, date or
    epoch seconds) is validated and otherwise unused: the prior has no clock.

    Held fit (Issue #1512 verifier finding 4): when the cycles behind the last usable fit are not in the pool (a
    sensor swap: the pool is the newest sensor's cycles) that fit (``held_candidate``: the current
    ``cycle_fit_<mode>`` itself, or the ``held`` copy kept from it) is the q prior, its variance inflated by
    ``1 / keep``, combined with the typical house: the held posterior already carries the legacy evidence, so the
    legacy value is not counted twice. The rate the house is judged by therefore never snaps back to the old value
    when a sensor changes; it yields as the new sensor's cycles arrive. Without ``cycles`` (a tool that cannot say
    which pool the fit will see) held is not used.
    """
    pr = typical_prior(mode)
    rp = cache.get("rate_prior") if isinstance(cache, dict) else None
    if not isinstance(rp, dict):
        rp = {}
    _as_naive_dt(now)  # validates the argument (TypeError on an unusable value); the prior has no clock
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
        n_eff_q = _n_eff(rec)
        sd_old = math.sqrt((0.30 * abs(q_old)) ** 2 / n_eff_q + (0.08 * abs(q_old)) ** 2)
        out["q"] = _combine(q_old, sd_old, *pr["q"])
    # k: the home's closed-house leak rate. Issue #1512 reopened: the CURRENT learned k_passive (what the decisions
    # use; it keeps learning from passive windows) when there is one, with the evidence behind it (the larger of the
    # carried upgrade evidence and the passive observation counter, which already includes the pre-upgrade count:
    # never both), else the upgrade-time value. ``k_source`` says whether k is learned or the typical house: a pinned
    # k that is only the typical value cannot back a confident rate.
    rec = rp.get("k_passive")
    k_val = cache.get("k_passive") if isinstance(cache, dict) else None
    n_eff_k = _n_eff(rec) if isinstance(rec, dict) and _num(rec.get("value")) and rec["value"] < 0 else 0.0
    n_pass = cache.get("observation_count_passive") if isinstance(cache, dict) else None
    if _num(n_pass) and n_pass > 0:
        n_eff_k = max(n_eff_k, float(n_pass) * THERMAL_LEGACY_OBS_WEIGHT)
    if not (_num(k_val) and k_val < 0) and isinstance(rec, dict) and _num(rec.get("value")) and rec["value"] < 0:
        k_val = rec["value"]
    if _num(k_val) and k_val < 0 and n_eff_k > 0:
        k_old = float(k_val)
        sd_old = math.sqrt((0.40 * abs(k_old)) ** 2 / n_eff_k + (0.08 * abs(k_old)) ** 2)
        out["k"] = _combine(k_old, sd_old, *pr["k"])
        if n_eff_k >= CYCLE_K_LEARNED_MIN_NEFF:
            out["k_source"] = "learned"
    if n_eff_q:
        out["source"] = "cache"
        out["n_eff"] = n_eff_q
    fit = cache.get(f"cycle_fit_{mode}") if isinstance(cache, dict) else None
    held = held_candidate(fit, cycles, mode) if cycles is not None else None
    if held is not None:
        keep = evidence_keep(held)
        out["q"] = _combine(float(held["q"]), float(held["sd_q"]) / math.sqrt(keep), *pr["q"])
        out["source"] = "held"
        ne_w = held.get("ne_w")
        out["n_eff"] = (float(ne_w) if _num(ne_w) else 1.0) * keep
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
        # A setpoint cut (Issue #1512 finding 3) has no gap flag: the recorder stopped recording at the change, so the
        # run portion is kept as recorded - with its off edge when the equipment stopped within the window, else
        # (the run went on chasing the new setpoint) ending at its last sample with no off edge and no coast.
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
    k_cum = [0.0] * n
    for j in range(1, n):
        a, b = t[j - 1], t[j]
        k_cum[j] = k_cum[j - 1] + 0.5 * ((temps[j - 1] - to[j - 1]) + (temps[j] - to[j])) * (b - a) / 60.0
    c["K"] = k_cum
    c["Q"] = _run_hours(c, 0.0)
    c["offs_all"] = ([] if c["toff"] is None else [c["toff"]]) + list(c["priors"])


def _run_hours(c: dict[str, Any], lag: float | tuple[float, float]) -> list[float]:
    """Cumulative run-hours ``Q`` as the SENSOR sees them through a first-order lag of ``lag`` minutes.

    Exact for the run's indicator (on at ``ton``, off at ``rend``) passed through ``dS/dt = (u - S) / lag``:
    during the run ``s - lag (1 - e^(-s/lag))``, afterwards the remaining catch-up ``lag u0 e^(-(t - rend)/lag)``
    fades out (``u0 = 1 - e^(-D/lag)``, D the run length). ``lag`` 0 is the plain run-hours. Relative to the first
    row (the per-cycle level absorbs the constant). Issue #1512 reopened: on a lagging sensor the post-off catch-up
    is part of the RUN's rise, not a coast.

    ``lag`` may be a ``(delay, lag)`` pair: the thermostat reports its edges ``delay`` minutes AFTER the
    burner/compressor changed (a polled or cloud-reported thermostat), so the run really was
    ``[ton - delay, rend - delay]``; the sensor lag then applies to it."""
    t = c["t"]
    delay, lag = lag if isinstance(lag, tuple) else (0.0, lag)
    ton, rend = c["ton"] - delay, c["rend"] - delay
    dur = max(0.0, rend - ton)

    def g(x: float) -> float:
        s = x - ton
        if s <= 0:
            return 0.0
        if lag <= 1e-9:
            return min(s, dur)
        if s <= dur:
            return s - lag * (1.0 - math.exp(-s / lag))
        u0 = 1.0 - math.exp(-dur / lag)
        return dur - lag * u0 * math.exp(-(s - dur) / lag)

    g0 = g(t[0])
    return [(g(x) - g0) / 60.0 for x in t]


def _coast(c: dict[str, Any], tau: float, lag: float | tuple[float, float] = 0.0) -> list[float]:
    """The coast response (integral of ``e^(-(t - t_off)/tau)`` of every off edge, F per unit c) as the sensor sees
    it through the first-order ``lag`` (minutes; 0 none), relative to the first row."""
    t = c["t"]
    n = len(t)
    out = [0.0] * n
    t0 = t[0]
    delay, lag = lag if isinstance(lag, tuple) else (0.0, lag)
    shift = -delay  # an edge reported late: the off edges really were earlier
    if lag > 1e-9 and abs(tau - lag) < 1e-3:
        lag = tau - 1e-3  # the closed form below has a removable singularity at tau == lag

    def g(s: float) -> float:
        if s <= 0:
            return 0.0
        if lag <= 1e-9:
            return tau * (1.0 - math.exp(-s / tau))
        return tau / (tau - lag) * (tau * (1.0 - math.exp(-s / tau)) - lag * (1.0 - math.exp(-s / lag)))

    for o0 in c["offs_all"]:
        o = o0 + shift
        g0 = g(t0 - o)
        for j in range(1, n):
            if t[j] > o:
                out[j] += (g(t[j] - o) - g0) / 60.0
    return out


def _cycle_information(c: dict[str, Any]) -> float:
    """Fisher information the cycle alone carries about q, per unit noise variance (Issue #1512 reopened, item 4).

    The run-hours' spread within the cycle, less the part the coast response (at CYCLE_INFO_TAU) could also explain:
    ``Sqq - Sqc^2 / Scc`` over the cycle's own readings. A short pulse is nearly indistinguishable from a coast and
    carries little; a long run carries a lot. Equal cycles get equal information, so the weight rule then reduces
    to the old count rule. Needs ``_features`` first."""
    q = c["Q"]
    co = _coast(c, CYCLE_INFO_TAU)
    n = len(q)
    if n < 2:
        return 0.0
    qm = sum(q) / n
    cm_ = sum(co) / n
    sqq = sum((x - qm) ** 2 for x in q)
    scc = sum((x - cm_) ** 2 for x in co)
    sqc = sum((x - qm) * (y - cm_) for x, y in zip(q, co, strict=True))
    info = sqq - (sqc * sqc / scc if scc > 1e-15 else 0.0)
    return max(info, 0.0)


def information_weights(
    infos: Sequence[float], half_n: float = EVIDENCE_HALF_N, halves: Sequence[float] | None = None
) -> list[float]:
    """Weights of cycles ordered oldest -> newest: ``0.5 ** (information of the newer cycles / (half x own))``.

    Owner 2026-11-12 (Issue #1512 reopened): evidence yields to newer INFORMATION, never to the calendar or to a mere
    count. A long accurate run keeps its weight while short low-information runs arrive (each displaces it only by
    its own share), and a short run yields quickly to a long one. With equal information this is exactly the old
    ``0.5 ** (newer cycles / half_n)``. ``halves`` (per cycle) overrides ``half_n``: a cycle the newer ones
    contradict yields at EVIDENCE_HALF_N_CONTRADICT. Deterministic; a zero-information cycle is floored at a tiny
    share of the largest so it yields to anything."""
    vals = [max(float(x), 0.0) if _num(x) else 0.0 for x in infos]
    if not vals:
        return []
    floor = max(max(vals) * 1e-6, 1e-15)
    vals = [max(v, floor) for v in vals]
    out = [0.0] * len(vals)
    newer = 0.0
    for i in range(len(vals) - 1, -1, -1):
        h = halves[i] if halves is not None and i < len(halves) and _num(halves[i]) and halves[i] > 0 else half_n
        out[i] = 0.5 ** (newer / (h * vals[i]))
        newer += vals[i]
    return out


def contradicted_by_newer(cyc_q: Sequence[float], cyc_var: Sequence[float]) -> list[bool]:
    """Per cycle (oldest -> newest): do the newer cycles, pooled by precision, contradict its own q?

    True when ``|q_j - q_newer| > 3 * sqrt(var_j + var_newer)`` (three sds, not the carried evidence's two: one test
    per cycle over a pool of up to 60, so two sds would flag a few agreeing cycles by chance alone). A cycle with
    nothing newer, or too uncertain to say, is not contradicted. Contradiction speeds displacement (owner
    2026-11-12): such a cycle yields at EVIDENCE_HALF_N_CONTRADICT instead of EVIDENCE_HALF_N."""
    n = len(cyc_q)
    out = [False] * n
    prec = 0.0
    acc = 0.0
    for i in range(n - 1, -1, -1):
        v = cyc_var[i] if i < len(cyc_var) else math.inf
        if prec > 0 and _num(v) and v > 0 and math.isfinite(v):
            m = acc / prec
            out[i] = abs(cyc_q[i] - m) > _CONTRADICT_SDS * math.sqrt(v + 1.0 / prec)
        if _num(v) and v > 0 and math.isfinite(v):
            prec += 1.0 / v
            acc += cyc_q[i] / v
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
    lag_evidence: Sequence[float] | None = None,
    only: tuple[tuple[float, float], float] | None = None,
) -> dict[str, Any] | None:
    """Consistent ridge / MAP over (q, [k], c | tau, lag, [solar]) with Huber IRLS on a (lag, tau) grid (poc2
    est5.fit_core; the lag axis is Issue #1512 reopened).

    The sensor's first-order lag is profiled jointly with the coast tau and Bayesian-averaged, so q is fitted net of
    the lag and the spread between lags is part of sd_q (an unidentified lag makes a short run honestly uncertain).
    ``lag_evidence`` (relative -2 log-likelihood per ``CYCLE_LAGS`` entry, from the other mode's cycles on the same
    sensor) pools the sensor's lag across heating and cooling; the returned ``lag_profile`` is this pool's own data
    only, so the two modes never echo each other. With k pinned, the q uncertainty that k's prior sd causes
    (``q_sd_from_k``) is added to sd_q."""
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
    # ``only`` = ((delay, lag), tau): one point of the grid (the k identifiability check reuses the main fit's timing)
    lags = (only[0],) if only is not None else tuple(timing_grid())
    ext = list(lag_evidence) if lag_evidence is not None and len(lag_evidence) == len(lags) else None
    cands: list[dict[str, Any]] = []
    q_by_lag: dict[int, list[list[float]]] = {}

    def _run(li: int, tau: float) -> None:
        lag = lags[li]
        if li not in q_by_lag:
            q_by_lag[li] = [_run_hours(c, lag) for c in cycles]
        q_l = q_by_lag[li]
        rows_by = []
        for ci, c in enumerate(cycles):
            cols = [q_l[ci]]
            if free_k:
                cols.append(c["K"])
            cols.append(_coast(c, tau, lag))
            if use_solar:
                cols.append(c["X"])
            y0 = c["T"][0]  # per-cycle offset (the level is free): keeps the raw moment sums well conditioned
            if free_k:
                ys = [t_ - y0 for t_ in c["T"]]
            else:
                ys = [t_ - y0 - km * k_ for t_, k_ in zip(c["T"], c["K"], strict=True)]
            rows_by.append((ys, cols, c["K"]))
        cand = _irls(cycles, rows_by, n_par, sfloor, res, deff, free_k, use_solar, qm, km, am, qsd, ksd, asd, tau)
        if cand is not None:
            cand["lag"] = lag
            cand["li"] = li
            cands.append(cand)

    # Pi CPU: every lag on a coarse tau subset first, then the full tau grid only for the lags that still carry
    # posterior mass (>= CYCLE_LAG_PRUNE_MASS). A lag left out had a negligible weight in the average.
    taus = (only[1],) if only is not None else tuple(CYCLE_TAUS)
    coarse_taus = tuple(taus[i] for i in sorted({len(taus) // 4, (3 * len(taus)) // 4}))
    for li in range(len(lags)):
        for tau in coarse_taus:
            _run(li, tau)
    if cands and len(lags) > 1:
        sref1 = min(c_["sigma"] for c_ in cands)
        o1 = [
            c_["sse"] / (sref1 * sref1 * deff)
            + c_["pen"]
            + (ext[c_["li"]] if ext is not None and _num(ext[c_["li"]]) else 0.0)
            + _delay_pen(lags[c_["li"]])
            for c_ in cands
        ]
        omin = min(o1)
        mass = [0.0] * len(lags)
        for o, c_ in zip(o1, cands, strict=True):
            mass[c_["li"]] += math.exp(-0.5 * (o - omin))
        tot = sum(mass) or 1.0
        kept = [li for li in range(len(lags)) if mass[li] / tot >= _LAG_PRUNE_MASS]
    else:
        kept = list(range(len(lags)))
    for li in kept:
        for tau in taus:
            if tau not in coarse_taus:
                _run(li, tau)
    if not cands:
        return None
    sref = min(c_["sigma"] for c_ in cands)
    for c_ in cands:
        c_["own"] = c_["sse"] / (sref * sref * deff) + c_["pen"]
        c_["obj"] = (
            c_["own"] + (ext[c_["li"]] if ext is not None and _num(ext[c_["li"]]) else 0.0) + _delay_pen(lags[c_["li"]])
        )
    best = min(cands, key=lambda c_: c_["obj"])
    o0 = best["obj"]
    ws = [math.exp(-0.5 * (c_["obj"] - o0)) for c_ in cands]
    sw_ = sum(ws)
    ws = [w / sw_ for w in ws]
    mean = list(best["beta"])
    var = [0.0] * n_par
    var_qk = 0.0
    var_qk_free = 0.0
    for w, c_ in zip(ws, cands, strict=True):
        mi = _inv(c_["M"])
        for i in range(n_par):
            vi = mi[i][i] if mi else 0.0
            var[i] += w * (vi + (c_["beta"][i] - mean[i]) ** 2)
        if not free_k and mi is not None:
            # dq/dk of the pinned k: the data part of the normal equations against the K column
            sens = -sum(mi[0][j] * c_["bk"][j] for j in range(n_par))
            var_qk += w * (sens * ksd) ** 2
        elif free_k and mi is not None:
            # k is fitted, but how far would q move if k's PRIOR mean moved by one prior sd? (dbeta/dm_k = M^-1 P e_k)
            # Short cycles that close at the next start leave a short tail in which the leak and a slow coast look
            # alike; then k stays near its prior and q is really the net climb (Issue #1512 reopened, real-HA probe).
            var_qk_free += w * (mi[0][1] * c_["pp"][1] * ksd) ** 2
    if not free_k:
        var[0] += var_qk
    else:
        var_qk = var_qk_free  # reported (and judged) only: the joint posterior already carries k's uncertainty
    # the lag's posterior (marginal over tau), and this pool's own lag profile (relative -2 log-likelihood)
    lag_w = [0.0] * len(lags)
    for w, c_ in zip(ws, cands, strict=True):
        lag_w[c_["li"]] += w
    lag_mean = sum(lw * lg[1] for lw, lg in zip(lag_w, lags, strict=True))
    lag_sd = math.sqrt(max(sum(lw * (lg[1] - lag_mean) ** 2 for lw, lg in zip(lag_w, lags, strict=True)), 0.0))
    delay_mean = sum(lw * lg[0] for lw, lg in zip(lag_w, lags, strict=True))
    own0 = min(c_["own"] for c_ in cands)
    prof: list[float | None] = []
    for li in range(len(lags)):
        s = sum(math.exp(-0.5 * (c_["own"] - own0)) for c_ in cands if c_["li"] == li)
        prof.append(-2.0 * math.log(s) if s > 0 else None)
    pmin = min((p for p in prof if p is not None), default=0.0)
    lag_profile = [round(p - pmin, 4) if p is not None else 1e6 for p in prof]
    # each cycle's own q at the best (lag, tau), its coast held at the pooled value: the pooled q plus the
    # projection of its residuals on its run-hours (used to tell whether newer cycles contradict an older one)
    cyc_q: list[float] = []
    cyc_var: list[float] = []
    for ci, c in enumerate(cycles):
        ql = _run_hours(c, best["lag"])
        n = len(ql)
        qbar = sum(ql) / n if n else 0.0
        xs = [x - qbar for x in ql]
        sxx = sum(x * x for x in xs)
        rr = best["res_by"][ci] if ci < len(best["res_by"]) else []
        if sxx <= 1e-12 or len(rr) != n:
            cyc_q.append(best["beta"][0])
            cyc_var.append(math.inf)
            continue
        cyc_q.append(best["beta"][0] + sum(x * r for x, r in zip(xs, rr, strict=True)) / sxx)
        cyc_var.append(best["sigma"] ** 2 * deff / sxx)
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
        "timing": best["lag"],
        "coast_area_f": mean[ic] * tau_b / 60.0,
        "sigma": best["sigma"],
        "solar_coeff": mean[-1] if use_solar else None,
        "lag_min": round(lag_mean, 3),
        "lag_sd_min": round(lag_sd, 3),
        "lag_profile": lag_profile,
        "edge_delay_min": round(delay_mean, 3),
        "q_sd_from_k": math.sqrt(var_qk),
        "sse": best["sse"],
        "deff": deff,
        "cyc_q": cyc_q,
        "cyc_var": cyc_var,
    }


def _irls(
    cycles: list[dict[str, Any]],
    rows_by: list[tuple[list[float], list[list[float]], list[float]]],
    n_par: int,
    sfloor: float,
    res: float,
    deff: float,
    free_k: bool,
    use_solar: bool,
    qm: float,
    km: float,
    am: float,
    qsd: float,
    ksd: float,
    asd: float,
    tau: float,
) -> dict[str, Any] | None:
    """One (lag, tau) candidate: ridge / MAP with Huber IRLS. Per cycle ``(y, feature columns, K)``.

    Each cycle's level is free, so every moment is centred per cycle (computed from weighted raw sums in one pass).
    """
    nz = sum(len(r[0]) for r in rows_by)
    nc = len(cycles)
    csd = 60.0 * asd / tau
    cm = 60.0 * am / tau
    m = [qm] + ([km] if free_k else []) + [cm] + ([0.0] if use_solar else [])
    pp = [1.0 / qsd**2] + ([1.0 / ksd**2] if free_k else []) + [1.0 / csd**2]
    if use_solar:
        pp.append(1.0 / CYCLE_SOLAR_PRIOR_SD**2)
    rw = [[1.0] * len(r[0]) for r in rows_by]
    sigma = max(sfloor, _SIGMA_FLOOR_FRAC * res + _SIGMA_INIT_ADD)
    beta: list[float] | None = list(m)
    res_by: list[list[float]] = []
    mat: list[list[float]] = []
    bk = [0.0] * n_par
    rng = range(n_par)
    for _ in range(CYCLE_IRLS_ITERS):
        a_mat = [[0.0] * n_par for _ in rng]
        b_vec = [0.0] * n_par
        bk_vec = [0.0] * n_par
        means: list[tuple[float, list[float]] | None] = []
        for ci, (ys, cols, ks) in enumerate(rows_by):
            cw = cycles[ci]["w"]
            w = [cw * x for x in rw[ci]]
            sw = sum(w)
            if sw <= 0:
                means.append(None)
                continue
            wx = [[wi * xi for wi, xi in zip(w, col, strict=True)] for col in cols]
            sx = [sum(v) for v in wx]
            sy = sum(wi * yi for wi, yi in zip(w, ys, strict=True))
            sk = sum(wi * ki for wi, ki in zip(w, ks, strict=True))
            for i in rng:
                wxi = wx[i]
                b_vec[i] += sum(a_ * yi for a_, yi in zip(wxi, ys, strict=True)) - sx[i] * sy / sw
                bk_vec[i] += sum(a_ * ki for a_, ki in zip(wxi, ks, strict=True)) - sx[i] * sk / sw
                for j in range(i, n_par):
                    a_mat[i][j] += sum(a_ * xj for a_, xj in zip(wxi, cols[j], strict=True)) - sx[i] * sx[j] / sw
            means.append((sy / sw, [v / sw for v in sx]))
        for i in rng:
            for j in range(i):
                a_mat[i][j] = a_mat[j][i]
        s2 = sigma * sigma
        mat = [[a_mat[i][j] / (s2 * deff) + (pp[i] + 1e-9 if i == j else 0.0) for j in rng] for i in rng]
        rhs = [b_vec[i] / (s2 * deff) + pp[i] * m[i] for i in rng]
        bk = [bk_vec[i] / (s2 * deff) for i in rng]
        beta = _solve(mat, rhs)
        if beta is None:
            break
        res_by = []
        all_r: list[float] = []
        for ci, (ys, cols, _ks) in enumerate(rows_by):
            mm = means[ci]
            if mm is None:
                n = len(ys)
                ym = sum(ys) / n
                xm = [sum(col) / n for col in cols]
            else:
                ym, xm = mm
            off = ym - sum(beta[i] * xm[i] for i in rng)
            fit_ = [off] * len(ys)
            for i in rng:
                bi = beta[i]
                fit_ = [f_ + bi * xv for f_, xv in zip(fit_, cols[i], strict=True)]
            rr = [yv - f_ for yv, f_ in zip(ys, fit_, strict=True)]
            res_by.append(rr)
            all_r.extend(rr)
        dof = max(nz - nc - n_par, 1) / max(nz, 1)
        sigma = max(_MAD_SCALE * _median([abs(r) for r in all_r]) / math.sqrt(max(dof, 0.2)), sfloor)
        kk = CYCLE_HUBER_K * sigma
        rw = [[1.0 if abs(r) <= kk else kk / abs(r) for r in rr] for rr in res_by]
    if beta is None:
        return None
    kk = CYCLE_HUBER_K * sigma
    sse = 0.0
    for ci, rr in enumerate(res_by):
        cw = cycles[ci]["w"]
        for r in rr:
            a = abs(r)
            sse += cw * (r * r if a <= kk else 2 * kk * a - kk * kk)
    pen = sum(pp[i] * (beta[i] - m[i]) ** 2 for i in rng)
    return {
        "sse": sse,
        "pen": pen,
        "beta": beta,
        "tau": tau,
        "M": mat,
        "pp": pp,
        "sigma": sigma,
        "bk": bk,
        "res_by": res_by,
    }


# --------------------------------------------------------------------------- public fit + grade
def grade(result: dict[str, Any]) -> str:
    """Calibrated confidence grade of a fit result (poc2 calibration tables, #1512; honesty caps, #1512 reopened).

    ``none``: no eligible cycle. Coarse pool (resolution >= 0.5 F): ``medium`` when er_q >= 0.95 and ne >= 2,
    else ``low`` - a whole-degree sensor is never ``high``. Fine pool: ``high`` er_q >= 0.95 and ne >= 4;
    ``medium`` er_q >= 0.6 and ne >= 2; else ``low``. ``er_q = 1 - posterior variance / prior variance``.

    The grade reflects information, not only the posterior width (owner 2026-11-12):

    * k pinned at an unlearned (typical-house) prior and the result depends on it (``k_dependent``: the q
      uncertainty k's prior sd causes is more than CYCLE_K_DEPEND_FRAC of |q|) -> ``low`` at most;
    * the sensor lag is not identified (``lag_identified`` False) and the median eligible run is shorter than
      CYCLE_LAG_RUN_MULT x CYCLE_LAG_PLAUSIBLE minutes -> ``medium`` at most (the lag could hide a large share of a
      short run's rise).

    A result without these fields (stored by an older model) is graded as before."""
    ne = int(result.get("ne", 0) or 0)
    er = float(result.get("er_q", 0.0) or 0.0)
    if ne == 0:
        return "none"
    res = float(result.get("resolution_f", 1.0) or 1.0)
    if res >= CYCLE_COARSE_RES_F:
        g = "medium" if (er >= CYCLE_GRADE_HIGH_ER and ne >= CYCLE_GRADE_MED_NE) else "low"
    elif er >= CYCLE_GRADE_HIGH_ER and ne >= CYCLE_GRADE_HIGH_NE:
        g = "high"
    elif er >= CYCLE_GRADE_MED_ER and ne >= CYCLE_GRADE_MED_NE:
        g = "medium"
    else:
        g = "low"
    if result.get("k_dependent") is True:
        g = "low"
    short = result.get("run_median_min")
    if (
        g == "high"
        and result.get("lag_identified") is False
        and _num(short)
        and short < CYCLE_LAG_RUN_MULT * CYCLE_LAG_PLAUSIBLE
    ):
        g = "medium"
    return g


def grade_caps(result: dict[str, Any]) -> list[str]:
    """Which honesty caps hold the grade down (``"k_typical_prior"``, ``"lag_unidentified"``), for Debug / AI."""
    out = []
    if result.get("k_dependent") is True:
        out.append("k_typical_prior")
    short = result.get("run_median_min")
    if result.get("lag_identified") is False and _num(short) and short < CYCLE_LAG_RUN_MULT * CYCLE_LAG_PLAUSIBLE:
        out.append("lag_unidentified")
    return out


def _prior_result(
    prior: dict[str, Any], res: float, n_cycles: int, reasons: dict[str, int], entity: Any = None
) -> dict[str, Any]:
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
        "sensor_class": sensor_class(res) if n_cycles else "unknown",
        "k_pinned": True,
        "k_source": prior.get("k_source", "typical"),
        "reasons": dict(reasons),
        "prior_source": prior.get("source", "given"),
        "prior_q": prior["q"][0],
        "prior_sd_q": prior["q"][1],
        "ne_w": 0.0,
        "entity": entity,
        "t_first": None,
        "t_last": None,
        "t_eligible": [],
        "info_eligible": [],
        "info_ref": None,
        "lag_min": None,
        "lag_sd_min": None,
        "lag_identified": False,
        "lag_profile": None,
        "model_version": MODEL_VERSION,
    }
    out["grade"] = grade(out)
    out["grade_caps"] = []
    return out


def fit_cycles(
    cycles: list[dict[str, Any]],
    mode: str,
    prior: dict[str, Any],
    *,
    now_ts: float,
    lag_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit the heating or cooling rate of ``mode`` from the stored cycles (poc2 R2F; Issue #1512).

    ``cycles``: public cycle dicts (see module docs; other-mode cycles are ignored). ``prior``: ``typical_prior``
    or ``prior_from_cache``. ``now_ts`` (epoch s) is accepted for the callers and does not weigh anything: a cycle's
    weight is ``0.5 ** (information of the newer eligible cycles / (EVIDENCE_HALF_N x its own information))``
    (``information_weights``; owner decisions 2026-11-11 and 2026-11-12: evidence yields to newer evidence, weighed
    by what it tells, never to the calendar: a refit after a quiet season gives the same answer). At most
    CYCLE_MAX_CYCLES cycles are fitted: those with the largest weights (a long accurate run is not pushed out by a
    run of short ones). ``lag_evidence``: the other mode's fit on the same sensor (its ``entity`` and
    ``lag_profile``), so the sensor's lag is pooled over heating and cooling; ignored when the sensor differs.
    Returns ``q`` (signed F/h: heat > 0, cool < 0), ``sd_q``, ``k_p_cycle`` / ``sd_k`` (diagnostic leak rate; pinned at
    the prior when ``k_pinned``), ``coast_area_f`` (= c*tau/60, post-off drift in F; signed, 0 none), ``tau`` (min),
    ``ne`` (eligible cycles used), ``n_cycles`` (same-mode, same-sensor cycles seen), ``er_q``, ``grade``,
    ``resolution_f``, ``sensor_class`` and ``reasons`` (counts per rejection code of the cycles that did not qualify).
    The sensor lag: ``lag_min`` / ``lag_sd_min`` (posterior), ``lag_identified``, ``lag_profile`` (this pool's own
    relative -2 log-likelihood per CYCLE_LAGS entry). Honesty: ``k_source`` (``learned`` / ``typical``),
    ``q_sd_from_k`` (part of sd_q caused by a pinned k), ``k_dependent``, ``run_median_min``, ``grade_caps``. Also
    the evidence bookkeeping the learning engine needs (Issue #1512 verifier findings 2 and 4): ``prior_q`` /
    ``prior_sd_q`` (the prior the data was weighed against), ``ne_w`` (the eligible cycles' summed weights),
    ``entity`` (the pool's sensor), ``t_first`` / ``t_last`` (``t_on`` of the oldest / newest eligible cycle),
    ``t_eligible`` and ``info_eligible`` (the eligible cycles' ``t_on`` and information, so the caller can tell which
    are new and how much they tell; not stored), ``info_ref`` (the median information of an eligible cycle) and
    ``model_version``. No eligible cycle returns the prior unchanged (grade ``none``). Never worse than the prior by
    construction (ridge toward it). Pure and deterministic.
    """
    mine = sorted((c for c in cycles if c.get("mode") == mode and c.get("t_on") is not None), key=lambda c: c["t_on"])
    reasons: dict[str, int] = {}

    def _count(code: str) -> None:
        reasons[code] = reasons.get(code, 0) + 1

    # sensor swap: the pool is the newest sensor's cycles; resolution of an older sensor does not carry over
    pool = mine
    ent = None
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
        pc, why = _prepare(c, res, prior_offs)
        if pc is None:
            why = why or REASON_TOO_SHORT
            # a cut cycle whose kept part is too short is reported as the cut (as in the eligibility gate below)
            _count(_CUT_REASON.get(str(c.get("cut") or ""), why) if why == REASON_TOO_SHORT else why)
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
    # retention weights (Issue #1512 reopened): every pool cycle 0 unless it is eligible (its information weight)
    w_by_id: dict[str, float] = {str(c.get("id")): 0.0 for c in pool if c.get("id") is not None}
    if not eligible:
        return dict(_prior_result(prior, res, n_seen, reasons, ent), w_by_id=w_by_id)
    del now_ts  # no clock: weights count newer information
    eligible.sort(key=lambda p: p["ton"])
    for pc in eligible:
        _features(pc)
        pc["info"] = _cycle_information(pc)
    for pc, w in zip(eligible, information_weights([pc["info"] for pc in eligible]), strict=True):
        pc["w"] = w
    all_eligible = list(eligible)
    if len(eligible) > CYCLE_MAX_CYCLES:
        keep_ids = {id(p) for p in sorted(eligible, key=lambda p: (p["w"], p["ton"]), reverse=True)[:CYCLE_MAX_CYCLES]}
        eligible = [p for p in eligible if id(p) in keep_ids]
    ne = len(eligible)
    coarse = res >= CYCLE_COARSE_RES_F
    free_k = (not coarse) and ne >= CYCLE_K_FREE_AFTER
    deff_mult = max(1.0, (CYCLE_COARSE_EVID_N / ne) ** 2) if coarse else CYCLE_FINE_DEFF_MULT
    use_solar = CYCLE_SOLAR_REGRESSOR and all("X" in pc for pc in eligible)
    ext = None
    if isinstance(lag_evidence, dict) and lag_evidence.get("entity") == ent:
        prof = lag_evidence.get("lag_profile")
        if isinstance(prof, list | tuple) and len(prof) == len(timing_grid()) and all(_num(x) for x in prof):
            ext = [float(x) for x in prof]
    fit = _fit_core(eligible, prior, mode, res, free_k, deff_mult, use_solar, ext)
    if fit is None:
        return dict(_prior_result(prior, res, n_seen, reasons, ent), w_by_id=w_by_id)
    # Contradiction speeds displacement: older cycles the newer ones contradict yield at the faster rate. One pass
    # (the weights of all eligible cycles are recomputed and the pool refitted once), so the result is deterministic.
    flags = contradicted_by_newer(fit["cyc_q"], fit["cyc_var"])
    n_contra = sum(flags)
    if n_contra:
        by_id = {id(p): f for p, f in zip(eligible, flags, strict=True)}
        halves = [EVIDENCE_HALF_N_CONTRADICT if by_id.get(id(p)) else EVIDENCE_HALF_N for p in all_eligible]
        for pc, w in zip(
            all_eligible, information_weights([p["info"] for p in all_eligible], halves=halves), strict=True
        ):
            pc["w"] = w
        fit2 = _fit_core(eligible, prior, mode, res, free_k, deff_mult, use_solar, ext)
        if fit2 is not None:
            fit = fit2
    k_source = prior.get("k_source", "typical")
    runs = [p["rend"] - p["ton"] for p in eligible]
    q_abs = max(abs(fit["q"]), 1e-9)
    # Is k really identified by these cycles? (Issue #1512 reopened, real-HA probe.) A regular train of short
    # maintenance cycles under a steady outdoor temperature cannot tell the leak from a slow coast: the superposed
    # coast tails of earlier cycles act like a constant drift, so k (and with it q = climb - k x gap) is set by the
    # priors, while the posterior looks tight. Profile check: refit with k pinned at the fitted value and at a
    # plausible alternative (the typical prior two sds leakier, or a third of it); when the data fit the alternative
    # about as well (within 3 sd: _K_ALT_SSE) but its q differs by more than CYCLE_K_DEPEND_FRAC, q depends on an
    # unlearned k. With k pinned (a coarse pool) the same test runs around the pinned value.
    q_k_alt = None
    if k_source != "learned":
        km, ksd = prior["k"]
        pin_sd = max(ksd * 1e-3, 1e-6)
        one = (fit["timing"], fit["tau"])  # the main fit's (delay, lag) and tau: one cheap pinned fit each
        hat = _fit_core(eligible, dict(prior, k=(fit["k"], pin_sd)), mode, res, False, deff_mult, use_solar, None, one)
        if hat is not None:
            scale = max(hat["sigma"], 1e-9) ** 2 * hat["deff"]
            for k_alt in (min(km - 2.0 * ksd, _K_BOUNDS[1]), min(km / 3.0, _K_BOUNDS[1])):
                if abs(k_alt - fit["k"]) < 1e-6:
                    continue
                alt = _fit_core(
                    eligible, dict(prior, k=(k_alt, pin_sd)), mode, res, False, deff_mult, use_solar, None, one
                )
                if alt is None:
                    continue
                alike = (alt["sse"] - hat["sse"]) / scale < _K_ALT_SSE
                moved = abs(alt["q"] - hat["q"])
                if (
                    alike
                    and moved > CYCLE_K_DEPEND_FRAC * q_abs
                    and (q_k_alt is None or moved > abs(q_k_alt - hat["q"]))
                ):
                    q_k_alt = alt["q"]
    infos = sorted(p["info"] for p in eligible)
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
        "sensor_class": sensor_class(res),
        "k_pinned": not free_k,
        "k_source": k_source,
        "q_sd_from_k": round(fit["q_sd_from_k"], 4),
        "k_dependent": bool(
            k_source != "learned" and (fit["q_sd_from_k"] > CYCLE_K_DEPEND_FRAC * q_abs or q_k_alt is not None)
        ),
        "q_if_k_alt": None if q_k_alt is None else round(q_k_alt, 3),
        "lag_min": fit["lag_min"],
        "lag_sd_min": fit["lag_sd_min"],
        "lag_identified": bool(fit["lag_sd_min"] <= CYCLE_LAG_IDENT_SD),
        "lag_profile": fit["lag_profile"],
        "lag_pooled": ext is not None,
        "edge_delay_min": fit["edge_delay_min"],
        "n_contradicted": n_contra,
        "run_median_min": round(_median(runs), 2),
        "sigma": fit["sigma"],
        "reasons": reasons,
        "prior_source": prior.get("source", "given"),
        "prior_q": prior["q"][0],
        "prior_sd_q": prior["q"][1],
        "ne_w": sum(pc["w"] for pc in eligible),
        "entity": ent,
        "t_first": min(pc["ton"] for pc in eligible) * 60.0,
        "t_last": max(pc["ton"] for pc in eligible) * 60.0,
        "t_eligible": [pc["ton"] * 60.0 for pc in eligible],
        "info_eligible": [pc["info"] for pc in eligible],
        "info_ref": _median(infos),
        "model_version": MODEL_VERSION,
    }
    for pc in all_eligible:
        if pc.get("id") is not None:
            w_by_id[str(pc["id"])] = pc["w"]
    out["w_by_id"] = w_by_id  # for the store's retention (not stored in the learning cache)
    if use_solar:
        out["solar_coeff"] = fit["solar_coeff"]
    out["grade"] = grade(out)
    out["grade_caps"] = grade_caps(out)
    return out


def coarse_sensor_notice(fit: Any, mode: str) -> dict[str, Any] | None:
    """Occupant-facing notice for a whole- or half-degree sensor (Issue #1512 reopened, item 5), or None.

    ``{"sensor_class", "maintenance_cycles_learn": False, "rate_in_use", "text"}``. ``rate_in_use``: ``"measured"``
    (longer runs, e.g. setback recoveries, already taught it), ``"learned"`` (the rate learned before, carried), or
    ``"default"`` (the typical-house rate). There are no user-configured heating/cooling rates, so none is named.
    The same text is shown on Analysis (the house-model row), Debug and in the AI context."""
    if not isinstance(fit, dict) or mode not in ("heat", "cool"):
        return None
    cls = fit.get("sensor_class") or sensor_class(fit.get("resolution_f"))
    if cls not in ("whole", "half") or not fit.get("n_cycles"):
        return None
    word = "heating" if mode == "heat" else "cooling"
    verb = "heats" if mode == "heat" else "cools"
    steps = "whole degrees" if cls == "whole" else "half degrees"
    ne = fit.get("ne") if _num(fit.get("ne")) else 0
    src = fit.get("prior_source")
    if ne and ne > 0:
        in_use = "measured"
        use = f"so far it has learned from {int(ne)} longer run{'s' if ne != 1 else ''} instead"
    elif src in ("cache", "held"):
        in_use = "learned"
        use = f"it keeps using the {word} rate it had already learned"
    else:
        in_use = "default"
        use = f"it uses a typical home's {word} rate (a default) until it can measure yours"
    recovery = "warming back up after a setback" if mode == "heat" else "cooling back down after a setback"
    text = (
        f"Your indoor temperature sensor reports {steps}. A normal {word} cycle moves the reading by only one or "
        f"two steps, so it can't teach Climate Advisor how fast your home {verb}; {use}. Longer runs, such as "
        f"{recovery}, can still teach it, and a finer sensor (for example the thermostat's companion temperature "
        "sensor, if it has one) fixes this."
    )
    return {"sensor_class": cls, "maintenance_cycles_learn": False, "rate_in_use": in_use, "text": text}
