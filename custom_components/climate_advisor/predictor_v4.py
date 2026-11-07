"""Indoor-temperature predictor v4 for Climate Advisor (Issue #1230 R1, lane A).

A pure, headless module (no ``hass`` import, no numpy/scipy): ``math``, lists and dataclasses only.
Nothing on a decision path calls it in R1 - it runs in shadow next to the v3 ODE so its accuracy can be
scored before it is allowed to drive anything. Spec: ``docs/predictor-v4-spec.md``.

What the occupant gets: a 1-24 h prediction of what *each room sensor* will read, accurate enough to
beat "the temperature won't change" at 4 h (the v3 line runs warm and does not).

Model (everything in internal degrees F and hours, integrated on a 15-minute step)::

    leak = a_o + a_w*windows + a_f*fan
    dH   = leak*(out - H) + c_hm*(M - H) + s*sun(hour - phase) + q_h*u_heat - q_c*u_cool
    dM   = c_mh*(H - M)                                       # hidden thermal-mass node
    dB_j = b_h*(H - B) + (b_o + b_w*windows)*(out - B) + s_b*sun + q_b*(u_heat - (q_c/q_h)*u_cool)

``H`` is the primary node (the thermostat's room reading), ``M`` the hidden mass, ``B_j`` one node per
extra sensor. Nodes are driven by ``H`` and never feed back, so the fit's Jacobian is cheap for them.
The thermostat is *emulated* when predicting (learned ``on_below_f`` / ``on_above_f`` / burst length /
reading quantisation) and *replayed* (the recorded heating / cooling duty) when fitting.
"""

from __future__ import annotations

import bisect
import math
import operator
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

V4_SCHEMA_VERSION = 1
V4_STEP_MINUTES = 15

_DT_H = V4_STEP_MINUTES / 60.0
_STEP_S = V4_STEP_MINUTES * 60
_GAP_BREAK_S = 2 * 3600  # chart_log gaps longer than this break rollout continuity
_FORECAST_BIAS_LEAD_H = 6.0  # archive lead used to learn the forecast hour-of-day bias
_COOL_HEAT_RATIO = 6.13 / 5.55  # production's learned cool/heat gain ratio; prior for the unseen mode
_COOL_SEASON_OUTDOOR_F = 68.0  # idle with no recent action and no hint: mode guess from outdoor temperature
_STAY_ON_MARGIN_F = 1.0  # thermostat stays on (no off gap) when this far beyond its start threshold

HOUSE_KEYS = ("a_o", "a_w", "a_f", "c_hm", "c_mh", "s", "phase", "q_h", "q_c")
NODE_KEYS = ("b_h", "b_o", "b_w", "s_b", "q_b")

#: (low, high) per parameter - physical bounds. ``a_f`` is capped at 2 so explicit Euler at 15 min
#: stays stable (leak + coupling rates times dt stay well below 2).
HOUSE_BOUNDS: dict[str, tuple[float, float]] = {
    "a_o": (0.001, 0.5),
    "a_w": (0.0, 1.0),
    "a_f": (0.0, 2.0),
    "c_hm": (0.0, 3.0),
    "c_mh": (0.0, 1.0),
    "s": (0.0, 5.0),
    "phase": (0.0, 5.0),
    "q_h": (0.5, 40.0),
    "q_c": (0.5, 40.0),
}
NODE_BOUNDS: dict[str, tuple[float, float]] = {
    "b_h": (0.0, 3.0),
    "b_o": (0.0, 0.5),
    "b_w": (0.0, 1.0),
    "s_b": (0.0, 3.0),
    "q_b": (-5.0, 40.0),
}
HOUSE_START: dict[str, float] = {
    "a_o": 0.03,
    "a_w": 0.10,
    "a_f": 0.30,
    "c_hm": 0.6,
    "c_mh": 0.2,
    "s": 0.7,
    "phase": 3.0,
    "q_h": 8.0,
    "q_c": 8.0 * _COOL_HEAT_RATIO,
}
NODE_START: dict[str, float] = {"b_h": 0.4, "b_o": 0.02, "b_w": 0.10, "s_b": 0.1, "q_b": 4.0}

# confidence gates (documented in docs/predictor-v4-spec.md)
GATE_MIN_DAYS_NONE = 3.0  # < 3 days of samples -> "none"
GATE_MIN_DAYS_OK = 10.0  # < 10 days of samples -> "low"
GATE_MIN_ANCHORS = 12
GATE_MIN_SKILL = 0.10  # must beat "no change" at 4 h on the training data by >= 10 %
GATE_MIN_MODE_STEPS = 8  # >= 2 h of heating (or cooling) to call the mode "seen"

CONFIDENCE_VALUES = ("none", "low", "ok")

_ACTIVE_HEAT = "heating"
_ACTIVE_COOL = "cooling"


# --------------------------------------------------------------------------------------- data types
@dataclass(frozen=True)
class V4Sample:
    """One 15-minute grid point of history.

    ``heat_duty`` / ``cool_duty`` (extension to the contract, default ``None``) are the fraction of
    the 15 minutes *ending at* ``ts`` the thermostat was heating / cooling. They let a 10-minute
    burst between two chart_log rows count as 0.67 of a step rather than 0 or 1. When ``None`` the
    instant ``hvac_action`` is used (1.0 for heating / cooling, else 0.0).
    """

    ts: datetime
    outdoor_f: float | None
    forecast_outdoor_f: float | None
    hvac_action: str | None
    fan_running: bool
    windows_open: bool
    heat_sp_f: float | None
    cool_sp_f: float | None
    readings: Mapping[str, float | None]
    heat_duty: float | None = None
    cool_duty: float | None = None


@dataclass(frozen=True)
class V4Input:
    """One future grid point for prediction (``outdoor_f`` is the RAW forecast)."""

    ts: datetime
    outdoor_f: float
    heat_sp_f: float | None
    cool_sp_f: float | None
    windows_open: bool = False
    fan_running: bool = False


@dataclass(frozen=True)
class V4Thermostat:
    on_below_f: float
    on_above_f: float
    burst_minutes: float
    reading_step_f: float
    #: extension: cooling runs are longer than heat bursts; ``None`` -> use ``burst_minutes``.
    burst_cool_minutes: float | None = None


@dataclass(frozen=True)
class V4ForecastCorrection:
    hourly_bias_f: tuple[float, ...]
    nowcast_tau_h: float


@dataclass(frozen=True)
class V4Model:
    schema_version: int
    fitted_at: str
    primary: str
    sensors: tuple[str, ...]
    house: Mapping[str, float]
    nodes: Mapping[str, Mapping[str, float]]
    thermostat: V4Thermostat
    forecast: V4ForecastCorrection
    fit: Mapping[str, Any]
    confidence: str


@dataclass
class V4Roll:
    """Result of :func:`rollout`: per-step trajectories plus the emulated/replayed duty."""

    primary: list[float]
    nodes: list[list[float]]
    heat_duty: list[float]
    cool_duty: list[float]
    sun: list[float]
    mass_end: float
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------------ small helpers
def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num(x: Any) -> float | None:
    return float(x) if _finite(x) else None


def _local_hour(ts: datetime, tz: tzinfo) -> float:
    lt = ts.astimezone(tz)
    return lt.hour + lt.minute / 60.0 + lt.second / 3600.0


def _sun(hour: float, phase: float) -> float:
    e = hour - phase
    if 8.0 <= e < 18.0:
        return math.sin((e - 8.0) * 0.3141592653589793)
    return 0.0


def _is_heating(s: V4Sample) -> bool:
    return (s.hvac_action or "").lower() == _ACTIVE_HEAT


def _is_cooling(s: V4Sample) -> bool:
    return (s.hvac_action or "").lower() == _ACTIVE_COOL


def _heat_u(s: V4Sample) -> float:
    if s.heat_duty is not None:
        return min(1.0, max(0.0, s.heat_duty))
    return 1.0 if _is_heating(s) else 0.0


def _cool_u(s: V4Sample) -> float:
    if s.cool_duty is not None:
        return min(1.0, max(0.0, s.cool_duty))
    return 1.0 if _is_cooling(s) else 0.0


def _contiguous(a: datetime, b: datetime) -> bool:
    # epoch difference: aware datetimes sharing one tzinfo subtract as wall clock, which breaks on DST days
    return abs(b.timestamp() - a.timestamp() - _STEP_S) < 1.0


def _bias_interp(bias: Sequence[float], hour: float) -> float:
    """Hour-of-day bias interpolated around the clock (value ``bias[h]`` is centred at ``h + 0.5``)."""
    x = (hour - 0.5) % 24.0
    i = int(x)
    f = x - i
    return bias[i % 24] * (1.0 - f) + bias[(i + 1) % 24] * f


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; ``None`` if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-14:
            return None
        m[c], m[p] = m[p], m[c]
        inv = 1.0 / m[c][c]
        for r in range(c + 1, n):
            f = m[r][c] * inv
            if f:
                rowr, rowc = m[r], m[c]
                for k in range(c, n + 1):
                    rowr[k] -= f * rowc[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


# --------------------------------------------------------------------------------------- the rollout
def rollout(
    house: Sequence[float],
    nodes: Sequence[Sequence[float]],
    *,
    h0: float,
    m0: float,
    b0: Sequence[float],
    out: Sequence[float],
    win: Sequence[float],
    fan: Sequence[float],
    hours: Sequence[float],
    heat_u: Sequence[float] | None = None,
    cool_u: Sequence[float] | None = None,
    thermostat: V4Thermostat | None = None,
    heat_sp: Sequence[float | None] | None = None,
    cool_sp: Sequence[float | None] | None = None,
) -> V4Roll:
    """Integrate the primary node, the hidden mass and every extra node over ``len(out)`` 15-min steps.

    ``out`` / ``win`` / ``fan`` / ``hours`` hold the inputs of the step *ending* at each point (``hours`` is
    the local clock hour used by the sun term). With ``thermostat=None`` the recorded ``heat_u`` /
    ``cool_u`` duties are replayed (fitting); otherwise the thermostat is emulated from ``heat_sp`` /
    ``cool_sp`` (predicting). Cooling subtracts ``q_c``.
    """
    hs, sun, uh, uc, mass_end = _roll_house(
        tuple(house), h0, m0, out, win, fan, hours, heat_u, cool_u, thermostat, heat_sp, cool_sp
    )
    q_h, q_c = house[7], house[8]
    ratio = q_c / q_h if q_h > 1e-9 else 1.0
    node_traj = [_roll_node(tuple(nd), h0, b0[j], out, win, hs, sun, uh, uc, ratio) for j, nd in enumerate(nodes)]
    return V4Roll(primary=hs, nodes=node_traj, heat_duty=uh, cool_duty=uc, sun=sun, mass_end=mass_end)


def _roll_house(
    hp: Sequence[float],
    h0: float,
    m0: float,
    out: Sequence[float],
    win: Sequence[float],
    fan: Sequence[float],
    hours: Sequence[float],
    heat_u: Sequence[float] | None,
    cool_u: Sequence[float] | None,
    therm: V4Thermostat | None,
    hsp: Sequence[float | None] | None,
    csp: Sequence[float | None] | None,
) -> tuple[list[float], list[float], list[float], list[float], float]:
    a_o, a_w, a_f, c_hm, c_mh, s_h, ph, q_h, q_c = hp
    n = len(out)
    h = h0
    m = m0
    hs = [0.0] * n
    sun = [0.0] * n
    sin = math.sin
    dt = _DT_H
    emulate = therm is not None
    if emulate:
        assert therm is not None  # for type checkers
        on_below = therm.on_below_f
        on_above = therm.on_above_f
        burst_h = therm.burst_minutes
        burst_c = therm.burst_cool_minutes if therm.burst_cool_minutes is not None else therm.burst_minutes
        rstep = therm.reading_step_f if therm.reading_step_f >= 0.05 else 0.0
        uh_l = [0.0] * n
        uc_l = [0.0] * n
        left = 0.0
        mode = 0
        step_min = float(V4_STEP_MINUTES)
        floor = math.floor
    else:
        uh_l = list(heat_u) if heat_u is not None else [0.0] * n
        uc_l = list(cool_u) if cool_u is not None else [0.0] * n
    for k in range(n):
        e = hours[k] - ph
        sol = sin((e - 8.0) * 0.3141592653589793) if 8.0 <= e < 18.0 else 0.0
        sun[k] = sol
        if emulate:
            sp_h = hsp[k] if hsp is not None else None
            sp_c = csp[k] if csp is not None else None
            if sp_h is None and sp_c is None:
                left = 0.0
                mode = 0
            elif left <= 0.0:
                rd = h if rstep <= 0.0 else floor(h / rstep + 0.5) * rstep
                if sp_h is not None and rd <= sp_h - on_below + 1e-6:
                    left = burst_h
                    mode = 1
                    if (sp_h - on_below) - rd >= _STAY_ON_MARGIN_F and left < step_min:
                        left = step_min
                elif sp_c is not None and rd >= sp_c + on_above - 1e-6:
                    left = burst_c
                    mode = -1
                    if rd - (sp_c + on_above) >= _STAY_ON_MARGIN_F and left < step_min:
                        left = step_min
                else:
                    mode = 0
            if left > 0.0:
                dur = left if left < step_min else step_min
                left -= dur
                u = dur / step_min
                if mode == 1:
                    uh_l[k] = u
                elif mode == -1:
                    uc_l[k] = u
            u_h = uh_l[k]
            u_c = uc_l[k]
        else:
            u_h = uh_l[k]
            u_c = uc_l[k]
        leak = a_o + a_w * win[k] + a_f * fan[k]
        dh = leak * (out[k] - h) + c_hm * (m - h) + s_h * sol + q_h * u_h - q_c * u_c
        dm = c_mh * (h - m)
        h += dh * dt
        m += dm * dt
        hs[k] = h
    return hs, sun, uh_l, uc_l, m


def _roll_node(
    nd: Sequence[float],
    h_prev0: float,
    b0: float,
    out: Sequence[float],
    win: Sequence[float],
    hs: Sequence[float],
    sun: Sequence[float],
    uh: Sequence[float],
    uc: Sequence[float],
    ratio: float,
) -> list[float]:
    b_h, b_o, b_w, s_b, q_b = nd
    n = len(out)
    res = [0.0] * n
    b = b0
    hp = h_prev0
    dt = _DT_H
    for k in range(n):
        db = b_h * (hp - b) + (b_o + b_w * win[k]) * (out[k] - b) + s_b * sun[k] + q_b * (uh[k] - ratio * uc[k])
        b += db * dt
        res[k] = b
        hp = hs[k]
    return res


# ------------------------------------------------------------------------- thermostat learning
def _reading_step(values: Sequence[float]) -> float:
    """1.0 for whole-degree readings, 0.5 for half-degree, 0.0 for fine (or too few) readings."""
    vals = [v for v in values if v is not None]
    if len(vals) < 20:
        return 0.0
    if sum(1 for v in vals if abs(v - round(v)) < 0.01) >= 0.98 * len(vals):
        return 1.0
    if sum(1 for v in vals if abs(v * 2 - round(v * 2)) < 0.02) >= 0.98 * len(vals):
        return 0.5
    return 0.0


def learn_thermostat(samples: Sequence[V4Sample], *, primary: str) -> V4Thermostat:
    """Learn the thermostat's start offsets, burst length and reading quantisation from history.

    A *start* is a sample where heating (cooling) is active and the previous sample's was not. The
    offset is ``setpoint - reading`` (``reading - setpoint``) taken on the lower (higher) of the two
    readings around the start, skipping starts within ~10 minutes of a setpoint change (the setpoint
    in force differs between the previous two samples or the start sample). Burst length is the median
    total active minutes of a run. Falls back to (1.0, 1.0, 10 min) with fewer than 5 starts.
    """
    reads = [s.readings.get(primary) for s in samples]
    step = _reading_step([r for r in reads if r is not None])
    h_starts: list[float] = []
    c_starts: list[float] = []
    h_runs: list[float] = []
    c_runs: list[float] = []
    run_h = 0.0
    run_c = 0.0
    prev_h = False
    prev_c = False
    for k, s in enumerate(samples):
        act_h = _heat_u(s) > 0.0
        act_c = _cool_u(s) > 0.0
        cont = k > 0 and _contiguous(samples[k - 1].ts, s.ts)
        if not cont:
            prev_h = prev_c = False
            run_h = run_c = 0.0
        if act_h:
            run_h += _heat_u(s) * V4_STEP_MINUTES
        elif prev_h and run_h > 0:
            h_runs.append(run_h)
            run_h = 0.0
        if act_c:
            run_c += _cool_u(s) * V4_STEP_MINUTES
        elif prev_c and run_c > 0:
            c_runs.append(run_c)
            run_c = 0.0
        if cont and (act_h or act_c):
            p = samples[k - 1]
            r_now = reads[k]
            r_prev = reads[k - 1]
            if r_now is not None and r_prev is not None:
                sp2_h = samples[k - 2].heat_sp_f if k >= 2 else None
                sp2_c = samples[k - 2].cool_sp_f if k >= 2 else None
                if (
                    act_h
                    and not prev_h
                    and s.heat_sp_f is not None
                    and s.heat_sp_f == p.heat_sp_f
                    and (sp2_h is None or sp2_h == p.heat_sp_f)
                ):
                    h_starts.append(s.heat_sp_f - min(r_now, r_prev))
                if (
                    act_c
                    and not prev_c
                    and s.cool_sp_f is not None
                    and s.cool_sp_f == p.cool_sp_f
                    and (sp2_c is None or sp2_c == p.cool_sp_f)
                ):
                    c_starts.append(max(r_now, r_prev) - s.cool_sp_f)
        prev_h, prev_c = act_h, act_c

    def _med(vals: list[float], default: float, lo: float, hi: float) -> float:
        return min(hi, max(lo, statistics.median(vals))) if len(vals) >= 5 else default

    on_below = _med(h_starts, 1.0, 0.0, 4.0)
    on_above = _med(c_starts, 1.0, 0.0, 4.0)
    burst_h = _med(h_runs, 10.0, 5.0, 90.0)
    burst_c = _med(c_runs, burst_h, 5.0, 120.0) if len(c_runs) >= 5 else None
    if len(h_runs) < 5 and burst_c is not None:
        burst_h = burst_c
    return V4Thermostat(
        on_below_f=round(on_below, 2),
        on_above_f=round(on_above, 2),
        burst_minutes=round(burst_h, 1),
        reading_step_f=step,
        burst_cool_minutes=None if burst_c is None else round(burst_c, 1),
    )


# ------------------------------------------------------------------------- forecast correction
def learn_forecast_correction(samples: Sequence[V4Sample], *, tz: tzinfo) -> V4ForecastCorrection:
    """24 hourly biases (actual - forecast by local hour) and the nowcast decay time.

    Hours with few pairs are shrunk toward 0 (``n / (n + 4)``). The decay time ``tau`` is the candidate
    (1-12 h) that best predicts the corrected miss's persistence at 1-12 h leads. Defaults: no bias, 3 h.
    """
    sums = [0.0] * 24
    cnt = [0] * 24
    pairs = 0
    for s in samples:
        if s.outdoor_f is None or s.forecast_outdoor_f is None:
            continue
        hr = s.ts.astimezone(tz).hour
        sums[hr] += s.outdoor_f - s.forecast_outdoor_f
        cnt[hr] += 1
        pairs += 1
    if pairs < 48:
        return V4ForecastCorrection(hourly_bias_f=(0.0,) * 24, nowcast_tau_h=3.0)
    bias = tuple(round(sums[h] / cnt[h] * cnt[h] / (cnt[h] + 4.0), 3) if cnt[h] else 0.0 for h in range(24))
    # nowcast tau: error series after the hourly bias
    err: list[float | None] = []
    for s in samples:
        if s.outdoor_f is None or s.forecast_outdoor_f is None:
            err.append(None)
        else:
            err.append(s.outdoor_f - s.forecast_outdoor_f - _bias_interp(bias, _local_hour(s.ts, tz)))
    cands = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
    sse = dict.fromkeys(cands, 0.0)
    used = 0
    n = len(samples)
    lead_steps = [(4 * h, float(h)) for h in range(1, 13)]
    for i in range(0, n - 48, 4):
        e0 = err[i]
        if e0 is None:
            continue
        for ls, lh in lead_steps:
            j = i + ls
            if (
                j >= n
                or err[j] is None
                or abs(samples[j].ts.timestamp() - samples[i].ts.timestamp() - ls * _STEP_S) > 1.0
            ):
                continue
            ej = err[j]
            assert ej is not None
            used += 1
            for t in cands:
                d = ej - e0 * math.exp(-lh / t)
                sse[t] += d * d
    tau = min(cands, key=lambda t: sse[t]) if used >= 100 else 3.0
    return V4ForecastCorrection(hourly_bias_f=bias, nowcast_tau_h=tau)


def _corrected_outdoor(
    corr: V4ForecastCorrection,
    *,
    raw: Sequence[float],
    hours: Sequence[float],
    lead_h: Sequence[float],
    now_hour: float,
    outdoor_now: float | None,
    forecast_now: float | None,
) -> list[float]:
    now_err = 0.0
    if outdoor_now is not None and forecast_now is not None:
        now_err = outdoor_now - (forecast_now + _bias_interp(corr.hourly_bias_f, now_hour))
    tau = corr.nowcast_tau_h if corr.nowcast_tau_h > 0.05 else 0.05
    return [
        raw[k] + _bias_interp(corr.hourly_bias_f, hours[k]) + now_err * math.exp(-lead_h[k] / tau)
        for k in range(len(raw))
    ]


def corrected_forecast(
    model: V4Model,
    *,
    times: Sequence[datetime],
    raw_f: Sequence[float],
    tz: tzinfo,
    now: datetime | None = None,
    outdoor_now_f: float | None = None,
    forecast_now_f: float | None = None,
) -> list[float]:
    """The weather service's hourly outdoor values after the model's learned correction (display, Issue #1230 R2).

    The same arithmetic ``predict()`` applies to its outdoor input: the learned hour-of-day bias plus, when ``now`` and
    both ``*_now_f`` values are given, the current miss decaying over the nowcast time. Without ``now`` (a past line,
    where today's miss no longer applies) only the hourly bias is added.
    """
    hours = [_local_hour(t, tz) for t in times]
    if now is None:
        return [float(r) + _bias_interp(model.forecast.hourly_bias_f, h) for r, h in zip(raw_f, hours, strict=True)]
    lead_h = [(t.timestamp() - now.timestamp()) / 3600.0 for t in times]
    return _corrected_outdoor(
        model.forecast,
        raw=list(raw_f),
        hours=hours,
        lead_h=lead_h,
        now_hour=_local_hour(now, tz),
        outdoor_now=outdoor_now_f,
        forecast_now=forecast_now_f,
    )


# ------------------------------------------------------------------------------------- the fit
@dataclass
class _Anchor:
    i: int
    length: int
    h0: float
    b0: list[float | None]
    out: list[float]
    win: list[float]
    fan: list[float]
    hours: list[float]
    uh: list[float]
    uc: list[float]
    targets: list[list[tuple[int, float, float]]]  # per node (primary first): (step index, value, weight)


class _Problem:
    """Multi-step rollout residuals of a sample series, with cached anchors."""

    def __init__(
        self,
        samples: Sequence[V4Sample],
        *,
        primary: str,
        extra: Sequence[str],
        tz: tzinfo,
        horizon_steps: int,
        max_anchors: int,
    ) -> None:
        self.primary = primary
        self.extra = list(extra)
        self.horizon = horizon_steps
        self.n = len(samples)
        self.ts = [s.ts for s in samples]
        self.cont = [k > 0 and _contiguous(samples[k - 1].ts, s.ts) for k, s in enumerate(samples)]
        self.hours = [_local_hour(s.ts, tz) for s in samples]
        out = [s.outdoor_f for s in samples]
        self.out = out
        self.win = [1.0 if s.windows_open else 0.0 for s in samples]
        self.fan = [1.0 if s.fan_running else 0.0 for s in samples]
        self.uh = [_heat_u(s) for s in samples]
        self.uc = [_cool_u(s) for s in samples]
        self.reads: list[list[float | None]] = [[s.readings.get(sid) for s in samples] for sid in [primary, *extra]]
        # mass filter input: primary reading carried forward within a contiguous run
        prim = self.reads[0]
        hfill: list[float | None] = []
        last: float | None = None
        for k in range(self.n):
            if not self.cont[k]:
                last = None
            if prim[k] is not None:
                last = prim[k]
            hfill.append(last)
        self.hfill = hfill
        self.steps_used = [s for s in (*range(1, 5), *range(6, horizon_steps + 1, 2))] + (
            [horizon_steps] if horizon_steps % 2 else []
        )
        self.steps_used = sorted({s for s in self.steps_used if 1 <= s <= horizon_steps})
        self.weights = {s: 1.0 - 0.5 * (s - 1) / max(1, horizon_steps - 1) for s in range(1, horizon_steps + 1)}
        cands: list[tuple[int, int]] = []
        for i in range(self.n - 1):
            if prim[i] is None:
                continue
            length = 0
            for j in range(1, horizon_steps + 1):
                if i + j >= self.n or not self.cont[i + j] or out[i + j] is None:
                    break
                length = j
            if length >= 4:
                cands.append((i, length))
        stride = max(1, math.ceil(len(cands) / max_anchors))
        self.anchors: list[_Anchor] = []
        for i, length in cands[::stride]:
            sl = slice(i + 1, i + 1 + length)
            targets: list[list[tuple[int, float, float]]] = []
            for series in self.reads:
                tl = []
                for s in self.steps_used:
                    if s > length:
                        break
                    v = series[i + s]
                    if v is not None:
                        tl.append((s - 1, v, self.weights[s]))
                targets.append(tl)
            self.anchors.append(
                _Anchor(
                    i=i,
                    length=length,
                    h0=prim[i],  # type: ignore[arg-type]
                    b0=[series[i] for series in self.reads[1:]],
                    out=[float(v) for v in out[sl]],  # type: ignore[arg-type]
                    win=self.win[sl],
                    fan=self.fan[sl],
                    hours=self.hours[sl],
                    uh=self.uh[sl],
                    uc=self.uc[sl],
                    targets=targets,
                )
            )
        self.heat_steps = sum(1 for a in self.anchors for v in a.uh if v > 0)
        self.cool_steps = sum(1 for a in self.anchors for v in a.uc if v > 0)
        self.win_steps = sum(1 for a in self.anchors for v in a.win if v > 0)
        self.fan_steps = sum(1 for a in self.anchors for v in a.fan if v > 0)

    # -- mass filter (matches ``step_mass``) ---------------------------------------------------
    def mass_series(self, c_mh: float) -> list[float]:
        m = 0.0
        res = [0.0] * self.n
        k_gain = c_mh * _DT_H
        for k in range(self.n):
            h = self.hfill[k]
            if h is None:
                res[k] = m
                continue
            if not self.cont[k] or k == 0 or self.hfill[k - 1] is None:
                m = h
            else:
                m += k_gain * (h - m)
            res[k] = m
        return res

    # -- passes --------------------------------------------------------------------------------
    def house_pass(self, hp: Sequence[float]) -> list[tuple[list[float], list[float]]]:
        mass = self.mass_series(hp[4])
        passes = []
        for a in self.anchors:
            hs, sun, _, _, _ = _roll_house(
                hp, a.h0, mass[a.i], a.out, a.win, a.fan, a.hours, a.uh, a.uc, None, None, None
            )
            passes.append((hs, sun))
        return passes

    def primary_residuals(self, passes: list[tuple[list[float], list[float]]]) -> list[float]:
        res: list[float] = []
        for a, (hs, _) in zip(self.anchors, passes, strict=True):
            for k, v, w in a.targets[0]:
                res.append((hs[k] - v) * w)
        return res

    def node_residuals(
        self, j: int, nd: Sequence[float], passes: list[tuple[list[float], list[float]]], ratio: float
    ) -> list[float]:
        res: list[float] = []
        for a, (hs, sun) in zip(self.anchors, passes, strict=True):
            tl = a.targets[j + 1]
            b0 = a.b0[j]
            if not tl or b0 is None:
                continue
            traj = _roll_node(nd, a.h0, b0, a.out, a.win, hs, sun, a.uh, a.uc, ratio)
            for k, v, w in tl:
                res.append((traj[k] - v) * w)
        return res

    def all_residuals(
        self, hp: Sequence[float], nodes: Sequence[Sequence[float]]
    ) -> tuple[list[float], list[tuple[list[float], list[float]]]]:
        passes = self.house_pass(hp)
        ratio = hp[8] / hp[7] if hp[7] > 1e-9 else 1.0
        r = self.primary_residuals(passes)
        for j, nd in enumerate(nodes):
            r.extend(self.node_residuals(j, nd, passes, ratio))
        return r, passes

    def lead_errors(
        self, hp: Sequence[float], nodes: Sequence[Sequence[float]]
    ) -> tuple[dict[int, list[float]], dict[int, list[float]], list[dict[int, list[float]]]]:
        """Absolute errors by lead step: (primary model, primary persistence, per-node model)."""
        passes = self.house_pass(hp)
        ratio = hp[8] / hp[7] if hp[7] > 1e-9 else 1.0
        prim: dict[int, list[float]] = {}
        flat: dict[int, list[float]] = {}
        per_node: list[dict[int, list[float]]] = [{} for _ in nodes]
        for a, (hs, sun) in zip(self.anchors, passes, strict=True):
            for k, v, _ in a.targets[0]:
                prim.setdefault(k + 1, []).append(abs(hs[k] - v))
                flat.setdefault(k + 1, []).append(abs(a.h0 - v))
            for j, nd in enumerate(nodes):
                b0 = a.b0[j]
                if b0 is None:
                    continue
                traj = _roll_node(nd, a.h0, b0, a.out, a.win, hs, sun, a.uh, a.uc, ratio)
                for k, v, _ in a.targets[j + 1]:
                    per_node[j].setdefault(k + 1, []).append(abs(traj[k] - v))
        return prim, flat, per_node


def _dot(a: tuple[int, list[float]], b: tuple[int, list[float]]) -> float:
    s0 = max(a[0], b[0])
    e0 = min(a[0] + len(a[1]), b[0] + len(b[1]))
    if e0 <= s0:
        return 0.0
    return sum(map(operator.mul, a[1][s0 - a[0] : e0 - a[0]], b[1][s0 - b[0] : e0 - b[0]], strict=True))


def _mae(vals: Sequence[float] | None) -> float | None:
    return None if not vals else sum(vals) / len(vals)


def _default_model(
    primary: str,
    sensors: Sequence[str],
    fitted_at: str,
    thermostat: V4Thermostat,
    forecast: V4ForecastCorrection,
    fit_info: Mapping[str, Any],
) -> V4Model:
    return V4Model(
        schema_version=V4_SCHEMA_VERSION,
        fitted_at=fitted_at,
        primary=primary,
        sensors=tuple(sensors),
        house=dict(HOUSE_START),
        nodes={sid: dict(NODE_START) for sid in sensors if sid != primary},
        thermostat=thermostat,
        forecast=forecast,
        fit=dict(fit_info),
        confidence="none",
    )


def fit(
    samples: Sequence[V4Sample],
    *,
    primary: str,
    sensors: Sequence[str],
    tz: tzinfo,
    now: datetime,
    horizon_h: int = 12,
    max_iterations: int = 60,
    max_anchors: int = 160,
    warm_start: V4Model | None = None,
) -> V4Model:
    """Fit the model by bounded Levenberg-Marquardt on multi-step rollout residuals.

    Deterministic (no randomness, no wall-clock). Residuals are the primary and every extra sensor's
    rollout error at 1-h and then 30-min lead steps up to ``horizon_h``, weighted from 1.0 (lead 1)
    to 0.5 (last lead). The Jacobian is numeric (forward difference in bound-scaled coordinates);
    extra-node parameters only re-roll their own node. ``warm_start`` (the previous night's model)
    seeds the parameters so a nightly refit converges in a handful of iterations. Never raises on
    thin or degenerate data: it returns a model with ``confidence == "none"``.
    """
    sensor_list = [primary, *[s for s in sensors if s != primary]]
    extra = sensor_list[1:]
    fitted_at = now.isoformat()
    thermostat = learn_thermostat(samples, primary=primary)
    forecast = learn_forecast_correction(samples, tz=tz)
    n_samples = len(samples)
    span_days = (samples[-1].ts.timestamp() - samples[0].ts.timestamp()) / 86400.0 if n_samples >= 2 else 0.0
    sample_days = n_samples * _DT_H / 24.0
    info: dict[str, Any] = {
        "n_anchors": 0,
        "span_days": round(span_days, 2),
        "sample_days": round(sample_days, 2),
        "mae_by_lead": {},
        "flat_mae_by_lead": {},
        "per_sensor": {},
        "iterations": 0,
        "horizon_h": horizon_h,
        "heat_seen": False,
        "cool_seen": False,
        "frozen": [],
        "reason": "",
    }
    base = _default_model(primary, sensor_list, fitted_at, thermostat, forecast, info)
    horizon_steps = max(4, int(horizon_h * 60 / V4_STEP_MINUTES))
    if n_samples < 16:
        base.fit["reason"] = "too few samples"  # ``base`` holds a copy of ``info``: record the reason on the model
        return base
    try:
        prob = _Problem(
            samples, primary=primary, extra=extra, tz=tz, horizon_steps=horizon_steps, max_anchors=max_anchors
        )
        if len(prob.anchors) < GATE_MIN_ANCHORS:
            base.fit["n_anchors"] = len(prob.anchors)
            base.fit["reason"] = "too few anchors"
            return base
        return _fit_problem(
            prob, base, sensor_list, thermostat, forecast, info, sample_days, max_iterations, warm_start
        )
    except (ArithmeticError, ValueError, TypeError, IndexError, OverflowError) as exc:  # pragma: no cover
        base.fit["reason"] = f"fit failed: {type(exc).__name__}"
        return base


def _fit_problem(
    prob: _Problem,
    base: V4Model,
    sensor_list: list[str],
    thermostat: V4Thermostat,
    forecast: V4ForecastCorrection,
    info: dict[str, Any],
    sample_days: float,
    max_iterations: int,
    warm_start: V4Model | None,
) -> V4Model:
    extra = sensor_list[1:]
    heat_seen = prob.heat_steps >= GATE_MIN_MODE_STEPS
    cool_seen = prob.cool_steps >= GATE_MIN_MODE_STEPS
    info["n_anchors"] = len(prob.anchors)
    info["heat_seen"] = heat_seen
    info["cool_seen"] = cool_seen

    hp0 = dict(HOUSE_START)
    nd0 = [dict(NODE_START) for _ in extra]
    if (
        warm_start is not None
        and warm_start.schema_version == V4_SCHEMA_VERSION
        and warm_start.primary == sensor_list[0]
    ):
        hp0.update({k: float(v) for k, v in warm_start.house.items() if k in HOUSE_BOUNDS})
        for j, sid in enumerate(extra):
            if sid in warm_start.nodes:
                nd0[j].update({k: float(v) for k, v in warm_start.nodes[sid].items() if k in NODE_BOUNDS})
    # parameters with no data behind them stay at their start value
    frozen: set[tuple[Any, ...]] = set()
    if prob.win_steps < GATE_MIN_MODE_STEPS:
        frozen.add(("h", "a_w"))
        for j in range(len(extra)):
            frozen.add(("n", j, "b_w"))
    if prob.fan_steps < GATE_MIN_MODE_STEPS:
        frozen.add(("h", "a_f"))
    # heat and cool gains are tied by the production ratio when only one mode is observed
    if not heat_seen:
        frozen.add(("h", "q_h"))
    if not cool_seen:
        frozen.add(("h", "q_c"))
    if not heat_seen and not cool_seen:
        for j in range(len(extra)):
            frozen.add(("n", j, "q_b"))
    if not cool_seen:
        hp0["q_c"] = min(HOUSE_BOUNDS["q_c"][1], hp0["q_h"] * _COOL_HEAT_RATIO)

    free: list[tuple[Any, ...]] = [("h", k) for k in HOUSE_KEYS if ("h", k) not in frozen]
    for j in range(len(extra)):
        free.extend(("n", j, k) for k in NODE_KEYS if ("n", j, k) not in frozen)
    info["frozen"] = sorted("/".join(str(p) for p in f) for f in frozen)

    def bounds(p: tuple[Any, ...]) -> tuple[float, float]:
        return HOUSE_BOUNDS[p[1]] if p[0] == "h" else NODE_BOUNDS[p[2]]

    def clamp_all() -> None:
        for k, (lo, hi) in HOUSE_BOUNDS.items():
            hp0[k] = min(hi, max(lo, hp0[k]))
        for nd in nd0:
            for k, (lo, hi) in NODE_BOUNDS.items():
                nd[k] = min(hi, max(lo, nd[k]))

    clamp_all()

    def to_z() -> list[float]:
        z = []
        for p in free:
            lo, hi = bounds(p)
            v = hp0[p[1]] if p[0] == "h" else nd0[p[1]][p[2]]
            z.append((v - lo) / (hi - lo))
        return z

    def apply_z(z: Sequence[float]) -> tuple[list[float], list[list[float]]]:
        hd = dict(hp0)
        ndd = [dict(d) for d in nd0]
        for p, zi in zip(free, z, strict=True):
            lo, hi = bounds(p)
            v = lo + min(1.0, max(0.0, zi)) * (hi - lo)
            if p[0] == "h":
                hd[p[1]] = v
            else:
                ndd[p[1]][p[2]] = v
        if ("h", "q_c") in frozen and not cool_seen:
            hd["q_c"] = min(HOUSE_BOUNDS["q_c"][1], hd["q_h"] * _COOL_HEAT_RATIO)
        elif ("h", "q_h") in frozen and not heat_seen:
            # mirror: only cooling observed -> heating gain follows the fitted cooling gain
            lo_h, hi_h = HOUSE_BOUNDS["q_h"]
            hd["q_h"] = min(hi_h, max(lo_h, hd["q_c"] / _COOL_HEAT_RATIO))
        return [hd[k] for k in HOUSE_KEYS], [[d[k] for k in NODE_KEYS] for d in ndd]

    z = to_z()
    hp, nodes = apply_z(z)
    r, passes = prob.all_residuals(hp, nodes)
    cost = 0.5 * sum(x * x for x in r)
    if not math.isfinite(cost):
        info["reason"] = "non-finite initial cost"
        return base
    lam = 1e-2
    n_free = len(free)
    iterations = 0
    delta_z = 1e-3
    stale = 0
    for iterations in range(1, max_iterations + 1):  # noqa: B007
        ratio = hp[8] / hp[7] if hp[7] > 1e-9 else 1.0
        n_prim = len(prob.primary_residuals(passes))
        slices: dict[int, tuple[int, int]] = {}
        off = n_prim
        for j in range(len(extra)):
            cnt = sum(len(a.targets[j + 1]) for a in prob.anchors if a.b0[j] is not None)
            slices[j] = (off, cnt)
            off += cnt
        cols: list[tuple[int, list[float]]] = []
        for idx, p in enumerate(free):
            step = delta_z if z[idx] + delta_z <= 1.0 else -delta_z
            zz = list(z)
            zz[idx] = z[idx] + step
            hp_p, nodes_p = apply_z(zz)
            if p[0] == "h":
                r_p, _ = prob.all_residuals(hp_p, nodes_p)
                cols.append((0, [(a - b) / step for a, b in zip(r_p, r, strict=True)]))
            else:
                j = p[1]
                o, cnt = slices[j]
                r_p_node = prob.node_residuals(j, nodes_p[j], passes, ratio)
                cols.append((o, [(a - b) / step for a, b in zip(r_p_node, r[o : o + cnt], strict=True)]))
        jtj = [[0.0] * n_free for _ in range(n_free)]
        for a in range(n_free):
            for b in range(a, n_free):
                jtj[a][b] = jtj[b][a] = _dot(cols[a], cols[b])
        jtr = [_dot(cols[a], (0, r)) for a in range(n_free)]
        accepted = False
        for _ in range(8):
            mat = [row[:] for row in jtj]
            for d in range(n_free):
                mat[d][d] += lam * (jtj[d][d] + 1e-9) + 1e-12
            step_z = _solve(mat, [-g for g in jtr])
            if step_z is None:
                lam *= 10.0
                continue
            zn = [min(1.0, max(0.0, zi + si)) for zi, si in zip(z, step_z, strict=True)]
            hp_n, nodes_n = apply_z(zn)
            r_n, passes_n = prob.all_residuals(hp_n, nodes_n)
            cost_n = 0.5 * sum(x * x for x in r_n)
            if math.isfinite(cost_n) and cost_n < cost:
                rel = (cost - cost_n) / max(cost, 1e-12)
                moved = max(abs(a - b) for a, b in zip(zn, z, strict=True))
                z, hp, nodes, r, passes, cost = zn, hp_n, nodes_n, r_n, passes_n, cost_n
                lam = max(lam / 3.0, 1e-7)
                accepted = True
                stale = stale + 1 if rel < 1e-4 else 0
                if moved < 1e-5:
                    stale = 2
                break
            lam *= 4.0
        if not accepted or stale >= 2:
            break
    info["iterations"] = iterations

    # commit the optimum into the dictionaries
    house = dict(zip(HOUSE_KEYS, hp, strict=True))
    node_params = {sid: dict(zip(NODE_KEYS, nodes[j], strict=True)) for j, sid in enumerate(extra)}

    prim, flat, per_node = prob.lead_errors(hp, nodes)
    horizon = prob.horizon
    leads = {"1h": 4, "4h": 16, "12h": 48}
    mae_by: dict[str, float] = {}
    flat_by: dict[str, float] = {}
    for name, step in leads.items():
        if step <= horizon:
            m, f = _mae(prim.get(step)), _mae(flat.get(step))
            if m is not None and f is not None:
                mae_by[name] = round(m, 3)
                flat_by[name] = round(f, 3)
    per_sensor: dict[str, dict[str, float]] = {}
    pm = _mae(prim.get(16))
    if pm is not None:
        per_sensor[sensor_list[0]] = {"4h": round(pm, 3)}
    for j, sid in enumerate(extra):
        m = _mae(per_node[j].get(16))
        if m is not None:
            per_sensor[sid] = {"4h": round(m, 3)}
    info["mae_by_lead"] = mae_by
    info["flat_mae_by_lead"] = flat_by
    info["per_sensor"] = per_sensor
    info["cost"] = round(cost, 4)

    # confidence gates
    values_ok = all(math.isfinite(v) for v in house.values()) and all(
        math.isfinite(v) for nd in node_params.values() for v in nd.values()
    )
    ref = "4h" if "4h" in mae_by else (max(mae_by, key=lambda k: leads[k]) if mae_by else None)
    skill_ok = ref is not None and flat_by[ref] > 0 and mae_by[ref] <= (1.0 - GATE_MIN_SKILL) * flat_by[ref]
    if ref is not None:
        info["skill_vs_flat"] = round(1.0 - mae_by[ref] / flat_by[ref], 3) if flat_by[ref] > 0 else 0.0
    if sample_days < GATE_MIN_DAYS_NONE:
        confidence, info["reason"] = "none", "under 3 days of samples"
    elif not values_ok or not math.isfinite(cost):
        confidence, info["reason"] = "none", "fit diverged"
    elif not skill_ok:
        confidence, info["reason"] = "none", "does not beat no-change at 4 h by 10 %"
    elif sample_days < GATE_MIN_DAYS_OK:
        confidence, info["reason"] = "low", "under 10 days of samples"
    elif not (heat_seen and cool_seen):
        confidence, info["reason"] = "low", "heating or cooling not yet observed"
    else:
        confidence, info["reason"] = "ok", ""
    return V4Model(
        schema_version=V4_SCHEMA_VERSION,
        fitted_at=base.fitted_at,
        primary=sensor_list[0],
        sensors=tuple(sensor_list),
        house={k: float(v) for k, v in house.items()},
        nodes=node_params,
        thermostat=thermostat,
        forecast=forecast,
        fit=info,
        confidence=confidence,
    )


# --------------------------------------------------------------------------------- live use
def step_mass(
    model: V4Model, *, mass_f: float | None, readings: Mapping[str, float | None], sample: V4Sample | None = None
) -> float:
    """Advance the hidden thermal-mass node one 15-minute step from the live primary reading.

    ``mass_f`` None -> start at the reading. Missing reading -> the mass is returned unchanged.
    Matches the filter used for the fit's anchor initial conditions.
    """
    h = readings.get(model.primary)
    if h is None and sample is not None:
        h = sample.readings.get(model.primary)
    if h is None:
        return float(mass_f) if mass_f is not None else float("nan")
    if mass_f is None or not math.isfinite(mass_f):
        return float(h)
    return float(mass_f + model.house["c_mh"] * _DT_H * (h - mass_f))


def predict(
    model: V4Model,
    *,
    now: datetime,
    readings: Mapping[str, float | None],
    mass_f: float | None,
    inputs: Sequence[V4Input],
    tz: tzinfo,
    outdoor_now_f: float | None = None,
    forecast_now_f: float | None = None,
) -> dict[str, list[tuple[datetime, float]]] | None:
    """Predict every model sensor at each ``inputs[k].ts`` (consecutive 15-minute grid points after ``now``).

    ``None`` when confidence is "none", the primary reading is missing, or there are no inputs. Sensors
    whose own reading is missing are left out of the result. ``outdoor_now_f`` / ``forecast_now_f``
    (extension to the contract, optional) are today's observed outdoor temperature and what the raw
    forecast says for now; with both the nowcast term (current miss decaying over ``nowcast_tau_h``)
    is applied, otherwise only the hourly bias.
    """
    if model.confidence == "none" or not inputs:
        return None
    h0 = readings.get(model.primary)
    if h0 is None or not math.isfinite(h0):
        return None
    m0 = mass_f if (mass_f is not None and math.isfinite(mass_f)) else h0
    house = [model.house[k] for k in HOUSE_KEYS]
    node_ids: list[str] = []
    node_params: list[list[float]] = []
    b0: list[float] = []
    for sid in model.sensors:
        if sid == model.primary:
            continue
        r = readings.get(sid)
        if r is None or not math.isfinite(r) or sid not in model.nodes:
            continue
        node_ids.append(sid)
        node_params.append([model.nodes[sid][k] for k in NODE_KEYS])
        b0.append(float(r))
    hours = [_local_hour(i.ts, tz) for i in inputs]
    lead_h = [(i.ts.timestamp() - now.timestamp()) / 3600.0 for i in inputs]
    out = _corrected_outdoor(
        model.forecast,
        raw=[i.outdoor_f for i in inputs],
        hours=hours,
        lead_h=lead_h,
        now_hour=_local_hour(now, tz),
        outdoor_now=outdoor_now_f,
        forecast_now=forecast_now_f,
    )
    roll = rollout(
        house,
        node_params,
        h0=float(h0),
        m0=float(m0),
        b0=b0,
        out=out,
        win=[1.0 if i.windows_open else 0.0 for i in inputs],
        fan=[1.0 if i.fan_running else 0.0 for i in inputs],
        hours=hours,
        thermostat=model.thermostat,
        heat_sp=[i.heat_sp_f for i in inputs],
        cool_sp=[i.cool_sp_f for i in inputs],
    )
    res: dict[str, list[tuple[datetime, float]]] = {
        model.primary: [(inp.ts, roll.primary[k]) for k, inp in enumerate(inputs)]
    }
    for j, sid in enumerate(node_ids):
        res[sid] = [(inp.ts, roll.nodes[j][k]) for k, inp in enumerate(inputs)]
    return res


# ------------------------------------------------------------------------------ serialisation
def to_dict(model: V4Model) -> dict[str, Any]:
    th = model.thermostat
    return {
        "schema_version": model.schema_version,
        "fitted_at": model.fitted_at,
        "primary": model.primary,
        "sensors": list(model.sensors),
        "house": {k: float(model.house[k]) for k in HOUSE_KEYS},
        "nodes": {sid: {k: float(p[k]) for k in NODE_KEYS} for sid, p in model.nodes.items()},
        "thermostat": {
            "on_below_f": th.on_below_f,
            "on_above_f": th.on_above_f,
            "burst_minutes": th.burst_minutes,
            "reading_step_f": th.reading_step_f,
            "burst_cool_minutes": th.burst_cool_minutes,
        },
        "forecast": {
            "hourly_bias_f": [float(v) for v in model.forecast.hourly_bias_f],
            "nowcast_tau_h": float(model.forecast.nowcast_tau_h),
        },
        "fit": _jsonable(model.fit),
        "confidence": model.confidence,
    }


def _jsonable(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    return str(v)


def from_dict(d: Any) -> V4Model | None:
    """Rebuild a model from :func:`to_dict` output; ``None`` on unknown schema or any malformed field."""
    try:
        if not isinstance(d, Mapping) or d.get("schema_version") != V4_SCHEMA_VERSION:
            return None
        primary = d["primary"]
        sensors = d["sensors"]
        if not isinstance(primary, str) or not isinstance(sensors, list) or not sensors:
            return None
        if not all(isinstance(s, str) for s in sensors) or sensors[0] != primary or len(set(sensors)) != len(sensors):
            return None
        house_raw = d["house"]
        house = {k: float(house_raw[k]) for k in HOUSE_KEYS}
        nodes_raw = d["nodes"]
        nodes = {sid: {k: float(nodes_raw[sid][k]) for k in NODE_KEYS} for sid in sensors[1:]}
        vals = [*house.values(), *(v for nd in nodes.values() for v in nd.values())]
        if not all(_finite(v) for v in vals):
            return None
        for k, (lo, hi) in HOUSE_BOUNDS.items():
            if not lo - 1e-9 <= house[k] <= hi + 1e-9:
                return None
        for nd in nodes.values():
            for k, (lo, hi) in NODE_BOUNDS.items():
                if not lo - 1e-9 <= nd[k] <= hi + 1e-9:
                    return None
        th = d["thermostat"]
        bc = th.get("burst_cool_minutes")
        thermostat = V4Thermostat(
            on_below_f=float(th["on_below_f"]),
            on_above_f=float(th["on_above_f"]),
            burst_minutes=float(th["burst_minutes"]),
            reading_step_f=float(th["reading_step_f"]),
            burst_cool_minutes=None if bc is None else float(bc),
        )
        if not all(
            _finite(v) for v in (thermostat.on_below_f, thermostat.on_above_f, thermostat.burst_minutes)
        ) or not _finite(thermostat.reading_step_f):
            return None
        if thermostat.burst_minutes <= 0 or (
            thermostat.burst_cool_minutes is not None and thermostat.burst_cool_minutes <= 0
        ):
            return None
        fc = d["forecast"]
        bias = tuple(float(v) for v in fc["hourly_bias_f"])
        tau = float(fc["nowcast_tau_h"])
        if len(bias) != 24 or not all(math.isfinite(v) for v in bias) or not math.isfinite(tau) or tau <= 0:
            return None
        fit_info = d.get("fit", {})
        if not isinstance(fit_info, Mapping):
            return None
        conf = d["confidence"]
        if conf not in CONFIDENCE_VALUES:
            return None
        fitted_at = d["fitted_at"]
        if not isinstance(fitted_at, str):
            return None
        return V4Model(
            schema_version=V4_SCHEMA_VERSION,
            fitted_at=fitted_at,
            primary=primary,
            sensors=tuple(sensors),
            house=house,
            nodes=nodes,
            thermostat=thermostat,
            forecast=V4ForecastCorrection(hourly_bias_f=bias, nowcast_tau_h=tau),
            fit=dict(fit_info),
            confidence=conf,
        )
    except KeyError, TypeError, ValueError, AttributeError, IndexError:
        return None


def summary_line(model: V4Model) -> str:
    """One INFO-log line for the nightly fit (lane C logs it)."""
    f = model.fit
    mae = f.get("mae_by_lead", {}) if isinstance(f, Mapping) else {}
    flat = f.get("flat_mae_by_lead", {}) if isinstance(f, Mapping) else {}
    return (
        f"v4 fit: confidence={model.confidence} reason={f.get('reason', '')!r} anchors={f.get('n_anchors')} "
        f"days={f.get('sample_days')} mae={mae} flat={flat} iterations={f.get('iterations')} "
        f"thermostat=({model.thermostat.on_below_f}/{model.thermostat.on_above_f} F, "
        f"{model.thermostat.burst_minutes} min, step {model.thermostat.reading_step_f})"
    )


# ------------------------------------------------------------------------- chart_log -> samples
def _parse_ts(raw: Any, tz: tzinfo) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def samples_from_chart_log(
    entries: Sequence[Mapping[str, Any]],
    *,
    tz: tzinfo,
    archive_snapshots: Sequence[Mapping[str, Any]] | None = None,
) -> list[V4Sample]:
    """Resample raw chart_log rows to the 15-minute grid (aligned to the hour, any timezone).

    Each field is carried forward from the latest non-null row up to 2 hours (older -> ``None``); a
    grid point with no row in the previous 2 hours is dropped, so a gap breaks continuity (consumers
    check consecutive timestamps). ``readings`` come from each row's ``indoor_sensors`` map, and
    ``"_ca"`` from ``indoor``. ``hvac_action`` is the row's ``hvac`` string; the heat / cool duty of
    each step is integrated from the row timeline (action-change rows make it exact). ``setpoint`` is
    assigned to heat or cool by the action (idle: the last active action within 12 h, else by outdoor
    temperature). Forecast: with ``archive_snapshots`` (ForecastArchive read shape) the value the
    snapshot issued ~6 h earlier gave for the hour, else the row's ``pred_outdoor``.
    """
    rows: list[tuple[float, Mapping[str, Any]]] = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        ts = _parse_ts(e.get("ts"), tz)
        if ts is None:
            continue
        rows.append((ts.timestamp(), e))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return []
    epochs = [r[0] for r in rows]
    arch_issued, arch_hours = _archive_index(archive_snapshots, tz)

    first = math.ceil(epochs[0] / _STEP_S) * _STEP_S
    last = math.floor(epochs[-1] / _STEP_S) * _STEP_S
    out: list[V4Sample] = []
    # per-field "last non-null value and when"
    state: dict[str, tuple[float, Any]] = {}
    sensor_state: dict[str, tuple[float, float]] = {}
    hvac_tl: list[tuple[float, str]] = []  # (epoch, hvac) of every row
    last_active: tuple[float, str] | None = None
    ri = 0
    g = first
    while g <= last:
        while ri < len(rows) and rows[ri][0] <= g:
            ep, e = rows[ri]
            hv = e.get("hvac")
            if isinstance(hv, str):
                hvac_tl.append((ep, hv.lower()))
                if hv.lower() in (_ACTIVE_HEAT, _ACTIVE_COOL):
                    last_active = (ep, hv.lower())
            for key in ("indoor", "outdoor", "pred_outdoor", "setpoint"):
                v = _num(e.get(key))
                if v is not None:
                    state[key] = (ep, v)
            for key in ("windows_open",):
                if isinstance(e.get(key), bool):
                    state[key] = (ep, e[key])
            fr = e.get("fan_running", e.get("fan"))
            if isinstance(fr, bool):
                state["fan"] = (ep, fr)
            sens = e.get("indoor_sensors")
            if isinstance(sens, Mapping):
                for sid, val in sens.items():
                    v = _num(val)
                    if v is not None:
                        sensor_state[str(sid)] = (ep, v)
            ri += 1
        row_ep = epochs[ri - 1] if ri else None
        if row_ep is None or g - row_ep > _GAP_BREAK_S:
            g += _STEP_S
            continue

        def fresh(key: str, g: float = g) -> Any:
            item = state.get(key)
            return item[1] if item is not None and g - item[0] <= _GAP_BREAK_S else None

        readings: dict[str, float | None] = {}
        for sid, (ep, v) in sensor_state.items():
            readings[sid] = v if g - ep <= _GAP_BREAK_S else None
        readings["_ca"] = fresh("indoor")
        outdoor = fresh("outdoor")
        hv_now = _hvac_at(hvac_tl, g)
        sp = fresh("setpoint")
        heat_sp = cool_sp = None
        if sp is not None and hv_now not in (None, "off", ""):
            mode = _mode_for(hv_now, last_active, g, outdoor)
            heat_sp, cool_sp = (sp, None) if mode == "heat" else (None, sp)
        heat_d, cool_d = _duty(hvac_tl, g)
        forecast = _archive_forecast(arch_issued, arch_hours, g, tz)
        if forecast is None:
            forecast = fresh("pred_outdoor")
        out.append(
            V4Sample(
                ts=datetime.fromtimestamp(g, tz),
                outdoor_f=outdoor,
                forecast_outdoor_f=forecast,
                hvac_action=hv_now or None,
                fan_running=bool(fresh("fan")),
                windows_open=bool(fresh("windows_open")),
                heat_sp_f=heat_sp,
                cool_sp_f=cool_sp,
                readings=readings,
                heat_duty=heat_d,
                cool_duty=cool_d,
            )
        )
        g += _STEP_S
    return out


def _hvac_at(tl: list[tuple[float, str]], g: float) -> str | None:
    """Action of the latest row at or before ``g`` (timeline is time-ordered)."""
    lo, hi = 0, len(tl)
    while lo < hi:
        mid = (lo + hi) // 2
        if tl[mid][0] <= g:
            lo = mid + 1
        else:
            hi = mid
    return tl[lo - 1][1] if lo else None


def _mode_for(hv_now: str, last_active: tuple[float, str] | None, g: float, outdoor: float | None) -> str:
    if hv_now == _ACTIVE_HEAT:
        return "heat"
    if hv_now == _ACTIVE_COOL:
        return "cool"
    if last_active is not None and g - last_active[0] <= 12 * 3600:
        return "heat" if last_active[1] == _ACTIVE_HEAT else "cool"
    return "cool" if (outdoor is not None and outdoor >= _COOL_SEASON_OUTDOOR_F) else "heat"


def _duty(tl: list[tuple[float, str]], g: float) -> tuple[float | None, float | None]:
    """Fraction of ``(g - 15 min, g]`` spent heating / cooling, from the row timeline."""
    t0 = g - _STEP_S
    if not tl:
        return None, None
    # state in force at t0 (None if the last row before t0 is older than the gap limit)
    lo, hi = 0, len(tl)
    while lo < hi:
        mid = (lo + hi) // 2
        if tl[mid][0] <= t0:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None, None
    cur_t, cur = t0, tl[lo - 1][1]
    if t0 - tl[lo - 1][0] > _GAP_BREAK_S:
        return None, None
    heat = cool = 0.0
    j = lo
    while True:
        nxt_t = tl[j][0] if j < len(tl) and tl[j][0] <= g else g
        seg = nxt_t - cur_t
        if cur == _ACTIVE_HEAT:
            heat += seg
        elif cur == _ACTIVE_COOL:
            cool += seg
        if nxt_t >= g:
            break
        cur_t, cur = nxt_t, tl[j][1]
        j += 1
    return heat / _STEP_S, cool / _STEP_S


def _archive_index(snaps: Sequence[Mapping[str, Any]] | None, tz: tzinfo) -> tuple[list[float], list[dict[int, float]]]:
    if not snaps:
        return [], []
    items: list[tuple[float, dict[int, float]]] = []
    for s in snaps:
        if not isinstance(s, Mapping):
            continue
        issued = _parse_ts(s.get("issued"), tz)
        hours = s.get("hours")
        if issued is None or not isinstance(hours, Sequence):
            continue
        table: dict[int, float] = {}
        for pair in hours:
            try:
                hts = _parse_ts(pair[0], tz)
                val = _num(pair[1])
            except TypeError, IndexError, KeyError:
                continue
            if hts is not None and val is not None:
                table[int(hts.timestamp() // 3600)] = val
        items.append((issued.timestamp(), table))
    items.sort(key=lambda t: t[0])
    return [i[0] for i in items], [i[1] for i in items]


def _archive_forecast(issued: list[float], tables: list[dict[int, float]], g: float, tz: tzinfo) -> float | None:
    if not issued:
        return None
    k = bisect.bisect_right(issued, g - _FORECAST_BIAS_LEAD_H * 3600) - 1
    hour_key = int(g // 3600)
    for idx in range(k, max(-1, k - 6), -1):
        v = tables[idx].get(hour_key)
        if v is not None:
            return v
    return None


# ------------------------------------------------------------------------------ misc exports
def thermostat_default() -> V4Thermostat:
    return V4Thermostat(on_below_f=1.0, on_above_f=1.0, burst_minutes=10.0, reading_step_f=0.0)


def grid_steps(start: datetime, count: int) -> list[datetime]:
    """``count`` consecutive 15-minute grid timestamps after ``start``, stepped in absolute (UTC) time so a
    DST transition neither repeats nor skips a grid point (wall-clock ``+ timedelta`` would)."""
    base = start.astimezone(UTC)
    return [(base + timedelta(minutes=V4_STEP_MINUTES * (k + 1))).astimezone(start.tzinfo) for k in range(count)]
