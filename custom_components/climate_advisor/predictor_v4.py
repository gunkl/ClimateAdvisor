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
import dataclasses
import math
import operator
import statistics
from collections.abc import Callable, Mapping, Sequence
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
_OPENNESS_DAYS = 28  # expected-openness prior: look-back of the hour-of-day window / whole-house-fan share
_OPENNESS_MIN_DAYS = 7.0  # fewer days of samples than this -> no prior (all zeros)
THERMOSTAT_DEFAULT_ON_F = 0.0  # calibrated defaults: "hold at the plan setpoint" (experiments, #1230 R1b)
THERMOSTAT_DEFAULT_OFF_F = 0.0
GATE_MIN_ACCEPTED_STARTS = 5  # accepted heating (cooling) starts needed to trust the learned thermostat of a mode
_MIN_RUN_CAP_MIN = 30.0  # cap of the learned minimum-run length
_MIN_RUN_DEFAULT_MIN = 15.0  # minimum run when the model does not carry one (v1 models)
# closed-loop calibration grid (degrees F relative to the setpoint the predictor receives)
_CAL_ON_GRID = (-0.5, 0.0, 0.5, 1.0, 1.5, 2.0)
_CAL_OFF_GRID = (-0.5, 0.0, 0.5, 1.0, 1.5, 2.0)
_CAL_MSE_TOLERANCE = 0.04  # squared-error slack within which calibration prefers the least biased candidate
_CAL_MIN_POINTS = 50  # a mode needs this many scored (anchor, lead) points to be calibrated
_CAL_MAX_ANCHORS = 100

CA_NODE = "_ca"  # the reading Climate Advisor itself used (``indoor``): the fallback primary node

HOUSE_KEYS = ("a_o", "a_w", "a_f", "c_hm", "c_mh", "s", "phase", "q_h", "q_c")
NODE_KEYS = ("b_h", "b_o", "b_w", "s_b", "q_b")

#: (low, high) per parameter - physical bounds. ``a_f`` is capped at 2 so explicit Euler at 15 min
#: stays stable (leak + coupling rates times dt stay well below 2).
HOUSE_BOUNDS: dict[str, tuple[float, float]] = {
    "a_o": (0.001, 0.5),
    "a_w": (0.0, 1.0),
    "a_f": (0.0, 2.0),
    "c_hm": (0.0, 3.0),
    "c_mh": (0.02, 1.0),  # R1b: below ~0.02 the hidden mass is a constant "ambient level", not a mass (trace cause 3)
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

# Display-only sensor nodes (Issue #1422). A sensor whose per-sensor history began recently (``indoor_sensors``) is
# judged over the span since ITS first row, not the whole training window, so it draws its own line within days. It
# is fitted AFTER the control fit with the house parameters held, and kept in ``V4Model.display_nodes`` - never in
# ``sensors`` / ``nodes`` - so nothing that decides (control rates, control path, ``_ca``, night strategy) reads it.
NODE_MIN_HISTORY_DAYS = 3.0  # a display node needs this much of its own history (its first row to the window's end)
NODE_MIN_COVERAGE = 0.5  # ... and a reading on this share of the rows since its first row
DISPLAY_MAX_ITERATIONS = 30  # node-only Levenberg-Marquardt: 5 parameters, the house trajectory rolled once

# Which model the decisions read (Issue #1432). A = one model of the stitched Climate Advisor reading (``_ca``); B = a
# node per room (the thermostat entity as the primary, the sleep sensor as a node) composed into ``_ca`` by
# :func:`ca_line`. B is a candidate once the thermostat AND the sleep sensor each have ``NODE_MIN_HISTORY_DAYS`` of
# their own readings (``NODE_MIN_COVERAGE``), and it is used only when its ``_ca`` replay on the training rows is no
# worse than A's at every ``CA_GATE_LEADS`` lead (``CA_GATE_TOLERANCE_F`` slack). Sleep hours are never forecast from
# the thermostat's model when a separate sleep sensor exists.
MODEL_STITCHED = "stitched"  # A
MODEL_PER_ROOM = "per_room"  # B
CA_GATE_LEAD = "4h"  # the deciding lead (the confidence gate's skill lead); 1 h and 12 h are checked too
CA_GATE_LEADS = ("1h", "4h", "12h")
CA_GATE_TOLERANCE_F = 0.05  # B may be at most this much worse than A (MAE, degrees F) at each lead: noise, not skill
CA_GATE_MAX_ANCHORS = 480  # shared replay anchors scored (an even stride), ~5 days of 15-min starts
_GATE_LEAD_STEPS = {"1h": 4, "4h": 16, "12h": 48}

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
    #: extension: the whole-house fan itself (``fan_running`` and the nat-vent flag), not the furnace blower.
    whf_running: bool = False


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
    #: R1b: the emulated thermostat is hysteretic. Heating starts at ``sp - on_below_f`` and runs until the
    #: reading reaches ``sp + off_above_f``; cooling starts at ``sp + on_above_f`` and runs until ``sp - off_below_f``.
    off_above_f: float = 0.0
    off_below_f: float = 0.0
    #: minimum run per mode in minutes; ``None`` -> ``min(burst, 15)`` (v1 models carry neither field).
    min_run_heat_min: float | None = None
    min_run_cool_min: float | None = None
    #: True when on/off were chosen by closed-loop calibration (False: learned medians or the defaults).
    calibrated: bool = False


@dataclass(frozen=True)
class V4Openness:
    """Expected-openness prior: share of each local hour with windows open / the whole-house fan running."""

    window_by_hour: tuple[float, ...]
    fan_by_hour: tuple[float, ...]
    days: float = 0.0


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
    openness: V4Openness | None = None
    #: Issue #1422: display-only sensor nodes ``{entity_id: node params}`` (ordered). Drawn on the chart, never a
    #: control node: not in ``sensors`` / ``nodes``, so the control rates, the control path, the ``_ca`` composite and
    #: the night strategy cannot see them.
    display_nodes: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


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
    init_run: tuple[int, float] | None = None,
) -> V4Roll:
    """Integrate the primary node, the hidden mass and every extra node over ``len(out)`` 15-min steps.

    ``out`` / ``win`` / ``fan`` / ``hours`` hold the inputs of the step *ending* at each point (``hours`` is
    the local clock hour used by the sun term). With ``thermostat=None`` the recorded ``heat_u`` /
    ``cool_u`` duties are replayed (fitting); otherwise the thermostat is emulated from ``heat_sp`` /
    ``cool_sp`` (predicting) and ``init_run`` = (+1 heating / -1 cooling, minutes already run) carries a
    run that is in progress at the anchor. Cooling subtracts ``q_c``.
    """
    hs, sun, uh, uc, mass_end = _roll_house(
        tuple(house), h0, m0, out, win, fan, hours, heat_u, cool_u, thermostat, heat_sp, cool_sp, init_run
    )
    q_h, q_c = house[7], house[8]
    ratio = q_c / q_h if q_h > 1e-9 else 1.0
    node_traj = [_roll_node(tuple(nd), h0, b0[j], out, win, hs, sun, uh, uc, ratio) for j, nd in enumerate(nodes)]
    return V4Roll(primary=hs, nodes=node_traj, heat_duty=uh, cool_duty=uc, sun=sun, mass_end=mass_end)


def free_step(
    house: Sequence[float], h: float, m: float, *, out: float, win: float, fan: float, hour: float
) -> tuple[float, float]:
    """One 15-min step of the primary node and hidden mass with the equipment off: ``(h_next, m_next)``.

    Issue #1505: the operation timeline steps v4's own model when it tries window plans on a v4 zone. Equipment is
    linear in this Euler step (``h_next = free + dt * (q_h * u_h - q_c * u_c)``) and the mass update does not see it,
    so the caller adds exactly the duty it needs to hold a band edge. ``hour`` is the local clock hour (fractional) at
    the end of the step, as ``rollout``'s ``hours``.
    """
    roll = rollout(house, [], h0=h, m0=m, b0=[], out=[out], win=[win], fan=[fan], hours=[hour])
    return roll.primary[0], roll.mass_end


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
    init_run: tuple[int, float] | None = None,
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
        off_above = therm.off_above_f
        off_below = therm.off_below_f
        min_h = _min_run(therm, heat=True)
        min_c = _min_run(therm, heat=False)
        rstep = therm.reading_step_f if therm.reading_step_f >= 0.05 else 0.0
        uh_l = [0.0] * n
        uc_l = [0.0] * n
        mode = 0
        elapsed = 0.0
        if init_run is not None and init_run[0] in (1, -1):
            mode, elapsed = init_run[0], max(0.0, float(init_run[1]))
        step_min = float(V4_STEP_MINUTES)
        floor = math.floor
    else:
        uh_l = list(heat_u) if heat_u is not None else [0.0] * n
        uc_l = list(cool_u) if cool_u is not None else [0.0] * n
    for k in range(n):
        e = hours[k] - ph
        sol = sin((e - 8.0) * 0.3141592653589793) if 8.0 <= e < 18.0 else 0.0
        sun[k] = sol
        leak = a_o + a_w * win[k] + a_f * fan[k]
        base = leak * (out[k] - h) + c_hm * (m - h) + s_h * sol
        u_h = uh_l[k]
        u_c = uc_l[k]
        if emulate:
            sp_h = hsp[k] if hsp is not None else None
            sp_c = csp[k] if csp is not None else None
            rd = h if rstep <= 0.0 else floor(h / rstep + 0.5) * rstep
            # a run ends when the reading reaches its stop point (after the minimum run) or its setpoint is gone
            if mode == 1:
                stop = sp_h is None
                if not stop and elapsed >= min_h:
                    stop = rd >= sp_h + off_above - 1e-6 or h >= sp_h + off_above - 1e-3
                if stop:
                    mode = 0
            elif mode == -1:
                stop = sp_c is None
                if not stop and elapsed >= min_c:
                    stop = rd <= sp_c - off_below + 1e-6 or h <= sp_c - off_below + 1e-3
                if stop:
                    mode = 0
            if mode == 0:
                elapsed = 0.0
                if sp_h is not None and rd <= sp_h - on_below + 1e-6:
                    mode = 1
                elif sp_c is not None and rd >= sp_c + on_above - 1e-6:
                    mode = -1
            if mode == 1:
                assert sp_h is not None
                u = 1.0
                if elapsed >= min_h:  # past the minimum run: the last step is partial, landing on the stop point
                    need = (sp_h + off_above - (h + dt * base)) / (q_h * dt)
                    if need < 1.0:
                        u = need if need > 0.0 else 0.0
                        mode = 0
                elapsed += step_min * u
                u_h = u
                uh_l[k] = u
            elif mode == -1:
                assert sp_c is not None
                u = 1.0
                if elapsed >= min_c:
                    need = ((h + dt * base) - (sp_c - off_below)) / (q_c * dt)
                    if need < 1.0:
                        u = need if need > 0.0 else 0.0
                        mode = 0
                elapsed += step_min * u
                u_c = u
                uc_l[k] = u
            else:
                u_h = u_c = 0.0
        dh = base + q_h * u_h - q_c * u_c
        dm = c_mh * (h - m)
        h += dh * dt
        m += dm * dt
        hs[k] = h
    return hs, sun, uh_l, uc_l, m


def _min_run(th: V4Thermostat, *, heat: bool) -> float:
    """Minimum run (minutes) of the emulated thermostat for one mode."""
    own = th.min_run_heat_min if heat else th.min_run_cool_min
    if own is not None:
        return float(own)
    burst = th.burst_minutes if (heat or th.burst_cool_minutes is None) else th.burst_cool_minutes
    return min(float(burst), _MIN_RUN_DEFAULT_MIN)


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


@dataclass(frozen=True)
class _ThermostatStats:
    """Start bookkeeping of :func:`learn_thermostat_detail` (accepted = usable for the start offset)."""

    heat_starts: int
    heat_accepted: int
    cool_starts: int
    cool_accepted: int


def learn_thermostat(samples: Sequence[V4Sample], *, primary: str) -> V4Thermostat:
    """Learn the thermostat's start / stop offsets, run lengths and reading quantisation from history.

    See :func:`learn_thermostat_detail`; this is the public wrapper that returns the thermostat only.
    """
    return learn_thermostat_detail(samples, primary=primary)[0]


def learn_thermostat_detail(samples: Sequence[V4Sample], *, primary: str) -> tuple[V4Thermostat, _ThermostatStats]:
    """Learn the thermostat from history and count the starts it could use.

    A *start* is a sample where heating (cooling) is active and the previous sample's was not. The
    start offset is ``setpoint - reading`` (``reading - setpoint``) on the lower (higher) of the two
    readings around the start, skipping starts within ~10 minutes of a setpoint change (the setpoint
    in force differs between the previous two samples or the start sample). A run's *stop* offset is the
    higher (lower) reading around the run's end minus the setpoint. Run length is the total active minutes of
    a run: the median is ``burst_minutes`` (reported), a low quantile (capped at 30 min) is the minimum run
    the emulator enforces. These are the uncalibrated estimates; ``fit`` refines on/off by closed loop.
    With fewer than 5 accepted starts / run ends the offsets fall back to the defaults (0 F, 0 F, 10 min) -
    the counts are returned so the caller can cap confidence.
    """
    reads = [s.readings.get(primary) for s in samples]
    step = _reading_step([r for r in reads if r is not None])
    h_starts: list[float] = []
    c_starts: list[float] = []
    h_stops: list[float] = []
    c_stops: list[float] = []
    h_runs: list[float] = []
    c_runs: list[float] = []
    n_h_starts = n_c_starts = 0
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
        r_now = reads[k]
        r_prev = reads[k - 1] if k > 0 else None
        if act_h:
            run_h += _heat_u(s) * V4_STEP_MINUTES
        elif prev_h and run_h > 0:
            h_runs.append(run_h)
            run_h = 0.0
            p = samples[k - 1]
            if r_now is not None and r_prev is not None and p.heat_sp_f is not None and s.heat_sp_f == p.heat_sp_f:
                h_stops.append(max(r_now, r_prev) - p.heat_sp_f)
        if act_c:
            run_c += _cool_u(s) * V4_STEP_MINUTES
        elif prev_c and run_c > 0:
            c_runs.append(run_c)
            run_c = 0.0
            p = samples[k - 1]
            if r_now is not None and r_prev is not None and p.cool_sp_f is not None and s.cool_sp_f == p.cool_sp_f:
                c_stops.append(p.cool_sp_f - min(r_now, r_prev))
        if cont and (act_h or act_c) and r_now is not None and r_prev is not None:
            p = samples[k - 1]
            sp2_h = samples[k - 2].heat_sp_f if k >= 2 else None
            sp2_c = samples[k - 2].cool_sp_f if k >= 2 else None
            if act_h and not prev_h:
                n_h_starts += 1
                if s.heat_sp_f is not None and s.heat_sp_f == p.heat_sp_f and (sp2_h is None or sp2_h == p.heat_sp_f):
                    h_starts.append(s.heat_sp_f - min(r_now, r_prev))
            if act_c and not prev_c:
                n_c_starts += 1
                if s.cool_sp_f is not None and s.cool_sp_f == p.cool_sp_f and (sp2_c is None or sp2_c == p.cool_sp_f):
                    c_starts.append(max(r_now, r_prev) - s.cool_sp_f)
        prev_h, prev_c = act_h, act_c

    def _med(vals: list[float], default: float, lo: float, hi: float) -> float:
        return min(hi, max(lo, statistics.median(vals))) if len(vals) >= GATE_MIN_ACCEPTED_STARTS else default

    def _low_run(runs: list[float]) -> float | None:
        if len(runs) < GATE_MIN_ACCEPTED_STARTS:
            return None
        srt = sorted(runs)
        return round(min(_MIN_RUN_CAP_MIN, max(5.0, srt[int(0.2 * len(srt))])), 1)

    on_below = _med(h_starts, THERMOSTAT_DEFAULT_ON_F, -2.0, 4.0)
    on_above = _med(c_starts, THERMOSTAT_DEFAULT_ON_F, -2.0, 4.0)
    off_above = _med(h_stops, THERMOSTAT_DEFAULT_OFF_F, -2.0, 4.0)
    off_below = _med(c_stops, THERMOSTAT_DEFAULT_OFF_F, -2.0, 4.0)
    burst_h = _med(h_runs, 10.0, 5.0, 90.0)
    burst_c = _med(c_runs, burst_h, 5.0, 120.0) if len(c_runs) >= GATE_MIN_ACCEPTED_STARTS else None
    if len(h_runs) < GATE_MIN_ACCEPTED_STARTS and burst_c is not None:
        burst_h = burst_c
    th = V4Thermostat(
        on_below_f=round(on_below, 2),
        on_above_f=round(on_above, 2),
        burst_minutes=round(burst_h, 1),
        reading_step_f=step,
        burst_cool_minutes=None if burst_c is None else round(burst_c, 1),
        off_above_f=round(off_above, 2),
        off_below_f=round(off_below, 2),
        min_run_heat_min=_low_run(h_runs),
        min_run_cool_min=_low_run(c_runs),
    )
    return th, _ThermostatStats(n_h_starts, len(h_starts), n_c_starts, len(c_starts))


def run_state_at(samples: Sequence[V4Sample], i: int) -> tuple[int, float] | None:
    """(+1 / -1, minutes already run) when heating / cooling is active at sample ``i``, else ``None``."""
    s = samples[i]
    if _heat_u(s) > 0.0:
        u_of, sign = _heat_u, 1
    elif _cool_u(s) > 0.0:
        u_of, sign = _cool_u, -1
    else:
        return None
    minutes = 0.0
    j = i
    while j >= 0 and u_of(samples[j]) > 0.0 and minutes < 240.0:
        minutes += u_of(samples[j]) * V4_STEP_MINUTES
        if j == 0 or not _contiguous(samples[j - 1].ts, samples[j].ts):
            break
        j -= 1
    return sign, minutes


def _calibrate_thermostat(
    prob: _Problem,
    samples: Sequence[V4Sample],
    house: Sequence[float],
    th: V4Thermostat,
    *,
    heat: bool,
    cool: bool,
) -> tuple[V4Thermostat, dict[str, Any]]:
    """Choose each requested mode's (on, off) offsets by closed-loop rollout error on the training anchors.

    The emulator (not the replay) is rolled from every anchor with the setpoint the sample carries - the same
    quantity ``predict`` receives - and the weighted squared error against the recorded primary readings is
    minimised over a small grid. Candidates are visited nearest-to-default first and a challenger must beat
    the incumbent by a hair, so a mode the anchors cannot tell apart keeps the defaults. A mode needs
    ``_CAL_MIN_POINTS`` scored points, else it is left as it is.
    """
    mass = prob.mass_series(house[4])
    anchors = prob.anchors
    if len(anchors) > _CAL_MAX_ANCHORS:
        stride = len(anchors) / _CAL_MAX_ANCHORS
        anchors = [anchors[int(j * stride)] for j in range(_CAL_MAX_ANCHORS)]
    prepared = []
    for a in anchors:
        sl = slice(a.i + 1, a.i + 1 + a.length)
        ss = samples[sl]
        prepared.append(
            (a, [x.heat_sp_f for x in ss], [x.cool_sp_f for x in ss], mass[a.i], run_state_at(samples, a.i))
        )
    info: dict[str, Any] = {}
    cur = th
    for is_heat, wanted in ((True, heat), (False, cool)):
        if not wanted:
            continue
        key = "heat" if is_heat else "cool"
        # only windows whose setpoint is known at EVERY step: a step with no setpoint (unknown, or the system off)
        # would be emulated as idle while the recording heated, and would bias the choice toward "heat harder".
        rel = [p for p in prepared if all((v is not None) for v in (p[1] if is_heat else p[2]))]
        if sum(len(p[0].targets[0]) for p in rel) < _CAL_MIN_POINTS:
            info[key] = {"calibrated": False, "reason": "too few scored points"}
            continue

        def score(on: float, off: float, rel=rel, is_heat=is_heat, cur=cur) -> tuple[float, float]:
            """(weighted mean squared error, weighted mean signed error) of the emulated rollouts."""
            if is_heat:
                t = dataclasses.replace(cur, on_below_f=on, off_above_f=off)
            else:
                t = dataclasses.replace(cur, on_above_f=on, off_below_f=off)
            sse = 0.0
            sbias = 0.0
            wsum = 0.0
            for a, hsp, csp, m0, run in rel:
                init = run if (run is not None and run[0] == (1 if is_heat else -1)) else None
                hs, _, _, _, _ = _roll_house(
                    tuple(house),
                    a.h0,
                    m0,
                    a.out,
                    a.win,
                    a.fan,
                    a.hours,
                    None,
                    None,
                    t,
                    hsp if is_heat else None,
                    None if is_heat else csp,
                    init,
                )
                for k, v, w in a.targets[0]:
                    d = hs[k] - v
                    sse += w * d * d
                    sbias += w * d
                    wsum += w
            if wsum <= 0:
                return float("inf"), 0.0
            return sse / wsum, sbias / wsum

        d_on = THERMOSTAT_DEFAULT_ON_F
        d_off = THERMOSTAT_DEFAULT_OFF_F
        grid = sorted(
            ((on, off) for on in _CAL_ON_GRID for off in _CAL_OFF_GRID),
            key=lambda c: (abs(c[0] - d_on) + abs(c[1] - d_off), c),
        )
        scored = [(*score(on, off), on, off) for on, off in grid]
        base_score = scored[0][0]
        # Among the candidates within a hair of the lowest squared error (the grid cannot tell them apart), take the
        # one whose emulated room is least biased: the emulator must hold the level the replayed fit assumed.
        floor_mse = min(c[0] for c in scored)
        near = [c for c in scored if c[0] <= floor_mse * (1.0 + _CAL_MSE_TOLERANCE) + 1e-9]
        best = min(near, key=lambda c: abs(c[1]))
        best = (best[0], best[2], best[3])
        sc, on, off = best
        if is_heat:
            cur = dataclasses.replace(cur, on_below_f=on, off_above_f=off, calibrated=True)
        else:
            cur = dataclasses.replace(cur, on_above_f=on, off_below_f=off, calibrated=True)
        info[key] = {
            "calibrated": True,
            "on_f": on,
            "off_f": off,
            "rmse_f": round(math.sqrt(sc), 3) if math.isfinite(sc) else None,
            "rmse_default_f": None
            if base_score is None or not math.isfinite(base_score)
            else round(math.sqrt(base_score), 3),
            "anchors": len(rel),
        }
    return cur, info


# --------------------------------------------------------------------- expected-openness prior
def learn_openness(samples: Sequence[V4Sample], *, tz: tzinfo) -> V4Openness:
    """Hour-of-day share of ``windows_open`` and of the whole-house fan over the last 28 days of samples.

    The whole house fan is ``V4Sample.whf_running`` (``fan_running`` with the nat-vent flag) - the furnace
    blower also sets ``fan_running`` and must not count. Fewer than 7 days of samples -> all zeros.
    """
    if not samples:
        return V4Openness((0.0,) * 24, (0.0,) * 24, 0.0)
    end = samples[-1].ts.timestamp()
    lo = end - _OPENNESS_DAYS * 86400
    sw = [0] * 24
    sf = [0] * 24
    cnt = [0] * 24
    used = 0
    for s in reversed(samples):
        if s.ts.timestamp() < lo:
            break
        hr = int((_local_hour(s.ts, tz) - 0.125) % 24.0)
        cnt[hr] += 1
        used += 1
        if s.windows_open:
            sw[hr] += 1
        if s.whf_running:
            sf[hr] += 1
    days = used * _DT_H / 24.0
    if days < _OPENNESS_MIN_DAYS:
        return V4Openness((0.0,) * 24, (0.0,) * 24, round(days, 2))
    win = tuple(round(sw[h] / cnt[h], 3) if cnt[h] else 0.0 for h in range(24))
    fan = tuple(round(sf[h] / cnt[h], 3) if cnt[h] else 0.0 for h in range(24))
    return V4Openness(win, fan, round(days, 2))


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

    def mode_errors(self, hp: Sequence[float], step: int) -> dict[str, tuple[list[float], list[float]]]:
        """Primary ``(model, persistence)`` absolute errors at lead ``step``, per mode (Issue #1469).

        An anchor counts for a mode when that mode had any duty in its first ``step`` steps: the hours whose
        forecast depends on that mode's learned gain. An anchor with both counts for both."""
        out: dict[str, tuple[list[float], list[float]]] = {"heat": ([], []), "cool": ([], [])}
        for a, (hs, _sun) in zip(self.anchors, self.house_pass(hp), strict=True):
            uses = {"heat": any(u > 0 for u in a.uh[:step]), "cool": any(u > 0 for u in a.uc[:step])}
            if not (uses["heat"] or uses["cool"]):
                continue
            for k, v, _ in a.targets[0]:
                if k + 1 != step:
                    continue
                for mode, used in uses.items():
                    if used:
                        out[mode][0].append(abs(hs[k] - v))
                        out[mode][1].append(abs(a.h0 - v))
        return out

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


def _mode_grades(
    prob: _Problem,
    hp: Sequence[float],
    *,
    confidence: str,
    sample_days: float,
    seen: Mapping[str, bool],
    accepted: Mapping[str, int],
    step: int,
) -> dict[str, dict[str, Any]]:
    """Heating and cooling graded apart (Issue #1469): ``{mode: {grade, reason, n, mae, flat}}``.

    Occupant view: a winter of good heating forecasts is not held at "low" because the AC ran twice. Each mode is
    ``ok`` once the shared gates pass (``confidence`` above ``none``, ``GATE_MIN_DAYS_OK`` days) AND the mode has
    ``GATE_MIN_ACCEPTED_STARTS`` usable thermostat starts AND, on the ``GATE_MIN_ANCHORS``+ anchors where that mode
    ran, the model beats "no change" at the skill lead by ``GATE_MIN_SKILL``. A mode the home did not use in the window
    is ``unseen``. Display only: ``confidence`` (the whole-model grade every decision reads) is unchanged by this."""
    if confidence == "none":
        return {m: {"grade": "none", "reason": "no usable fit"} for m in ("heat", "cool")}
    errs = prob.mode_errors(hp, step)
    out: dict[str, dict[str, Any]] = {}
    for mode in ("heat", "cool"):
        model_e, flat_e = errs[mode]
        m, f = _mae(model_e), _mae(flat_e)
        rec: dict[str, Any] = {"n": len(model_e), "mae": round(m, 3) if m is not None else None}
        rec["flat"] = round(f, 3) if f is not None else None
        word = "heating" if mode == "heat" else "cooling"
        if not seen.get(mode):
            rec.update(grade="unseen", reason=f"{word} not seen")
        elif sample_days < GATE_MIN_DAYS_OK:
            rec.update(grade="low", reason="under 10 days of samples")
        elif accepted.get(mode, 0) < GATE_MIN_ACCEPTED_STARTS:
            rec.update(grade="low", reason="too few accepted thermostat starts")
        elif len(model_e) < GATE_MIN_ANCHORS or m is None or not f:
            rec.update(grade="low", reason=f"too few anchors with {word}")
        elif m > (1.0 - GATE_MIN_SKILL) * f:
            rec.update(grade="low", reason=f"does not beat no-change by 10 % while {word}")
        else:
            rec.update(grade="ok", reason="")
        out[mode] = rec
    return out


def _mae(vals: Sequence[float] | None) -> float | None:
    return None if not vals else sum(vals) / len(vals)


def _default_model(
    primary: str,
    sensors: Sequence[str],
    fitted_at: str,
    thermostat: V4Thermostat,
    forecast: V4ForecastCorrection,
    fit_info: Mapping[str, Any],
    openness: V4Openness | None = None,
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
        openness=openness,
    )


def fit(
    samples: Sequence[V4Sample],
    *,
    primary: str,
    sensors: Sequence[str],
    tz: tzinfo,
    now: datetime,
    horizon_h: int = 12,
    max_iterations: int = 100,
    max_anchors: int = 160,
    warm_start: V4Model | None = None,
    display_sensors: Sequence[str] = (),
) -> V4Model:
    """Fit the model by bounded Levenberg-Marquardt on multi-step rollout residuals.

    ``display_sensors`` (Issue #1422, from :func:`resolve_display_nodes`) are fitted AFTER the model, house held, by
    :func:`fit_display_nodes` into ``display_nodes``; the rest of the model is exactly what it is without them.

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
    thermostat, tstats = learn_thermostat_detail(samples, primary=primary)
    forecast = learn_forecast_correction(samples, tz=tz)
    openness = learn_openness(samples, tz=tz)
    n_samples = len(samples)
    span_days = (samples[-1].ts.timestamp() - samples[0].ts.timestamp()) / 86400.0 if n_samples >= 2 else 0.0
    # Confidence gates count samples that actually carry a primary reading (R1b, M2): grid points whose primary node
    # is empty (a log older than the entity primary) say nothing about how long the model has been learning.
    n_primary = sum(1 for sm in samples if _finite(sm.readings.get(primary)))
    sample_days = n_primary * _DT_H / 24.0
    info: dict[str, Any] = {
        "n_anchors": 0,
        "span_days": round(span_days, 2),
        "sample_days": round(sample_days, 2),
        "primary_samples": n_primary,
        "mae_by_lead": {},
        "flat_mae_by_lead": {},
        "per_sensor": {},
        "iterations": 0,
        "horizon_h": horizon_h,
        "heat_seen": False,
        "cool_seen": False,
        "frozen": [],
        "reason": "",
        "starts_accepted": {"heat": tstats.heat_accepted, "cool": tstats.cool_accepted},
        "starts_total": {"heat": tstats.heat_starts, "cool": tstats.cool_starts},
        "thermostat_calibration": {},
        "openness_days": openness.days,
    }
    base = _default_model(primary, sensor_list, fitted_at, thermostat, forecast, info, openness)
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
        model = _fit_problem(
            prob,
            base,
            sensor_list,
            thermostat,
            forecast,
            info,
            sample_days,
            max_iterations,
            warm_start,
            samples=samples,
            tstats=tstats,
            openness=openness,
        )
    except (ArithmeticError, ValueError, TypeError, IndexError, OverflowError) as exc:  # pragma: no cover
        base.fit["reason"] = f"fit failed: {type(exc).__name__}"
        return base
    if display_sensors and model.confidence != "none":
        model = with_display_nodes(
            model,
            samples,
            display_sensors=display_sensors,
            tz=tz,
            warm=warm_start.display_nodes if warm_start is not None else None,
            max_anchors=max_anchors,
        )
    return model


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
    *,
    samples: Sequence[V4Sample],
    tstats: _ThermostatStats,
    openness: V4Openness,
) -> V4Model:
    extra = sensor_list[1:]
    heat_seen = prob.heat_steps >= GATE_MIN_MODE_STEPS
    cool_seen = prob.cool_steps >= GATE_MIN_MODE_STEPS
    info["n_anchors"] = len(prob.anchors)
    info["heat_seen"] = heat_seen
    info["cool_seen"] = cool_seen

    hp0 = dict(HOUSE_START)
    nd0 = [dict(NODE_START) for _ in extra]
    warm_applied = False
    if (
        warm_start is not None
        and warm_start.schema_version == V4_SCHEMA_VERSION
        and warm_start.primary == sensor_list[0]
    ):
        warm_applied = True
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

    def run_lm(max_it: int):
        z = to_z()
        hp, nodes = apply_z(z)
        r, passes = prob.all_residuals(hp, nodes)
        cost = 0.5 * sum(x * x for x in r)
        if not math.isfinite(cost):
            return None
        lam = 1e-2
        n_free = len(free)
        iterations = 0
        delta_z = 1e-3
        stale = 0
        converged = False
        for iterations in range(1, max_it + 1):  # noqa: B007
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
                converged = True
                break
        return z, hp, nodes, r, passes, cost, iterations, converged

    def set_start(house: Mapping[str, float], node_params: Sequence[Mapping[str, float]]) -> None:
        hp0.update(house)
        for d, src in zip(nd0, node_params, strict=True):
            d.update(src)
        if not cool_seen:
            hp0["q_c"] = min(HOUSE_BOUNDS["q_c"][1], hp0["q_h"] * _COOL_HEAT_RATIO)
        clamp_all()

    warm_started = warm_applied
    start_a = (dict(hp0), [dict(d) for d in nd0])
    best = run_lm(max_iterations)
    if best is None:
        info["reason"] = "non-finite initial cost"
        return base
    restarts = 0
    # A fit that ran out of iterations, or one that started from the previous night's model, gets a second chance from
    # a different start; the lower-cost optimum is kept (R1b: 7 of 10 backtest fits hit the 60-iteration cap and two
    # landed in poor minima). The extra start is the cold defaults (warm-started) or a "light house" variant.
    if not best[7] or warm_started:
        alt_house = dict(HOUSE_START)
        if not warm_started:
            alt_house.update(a_o=0.08, c_hm=0.25, c_mh=0.1, q_h=12.0, q_c=12.0 * _COOL_HEAT_RATIO)
        set_start(alt_house, [dict(NODE_START) for _ in extra])
        cand = run_lm(max_iterations)
        restarts = 1
        if cand is not None and cand[5] < best[5]:
            best = cand
            info["restart_won"] = True
    set_start(*start_a)  # leave the start dictionaries as the caller passed them
    z, hp, nodes, r, passes, cost, iterations, converged = best
    info["iterations"] = iterations
    info["converged"] = converged
    info["restarts"] = restarts

    # Closed-loop thermostat calibration (R1b): choose the start / stop offsets of each mode that the training data
    # shows (enough accepted starts) by rolling the EMULATOR, not the replay, from the training anchors.
    heat_ok = heat_seen and tstats.heat_accepted >= GATE_MIN_ACCEPTED_STARTS
    cool_ok = cool_seen and tstats.cool_accepted >= GATE_MIN_ACCEPTED_STARTS
    if not heat_ok:
        thermostat = dataclasses.replace(
            thermostat, on_below_f=THERMOSTAT_DEFAULT_ON_F, off_above_f=THERMOSTAT_DEFAULT_OFF_F
        )
    if not cool_ok:
        thermostat = dataclasses.replace(
            thermostat, on_above_f=THERMOSTAT_DEFAULT_ON_F, off_below_f=THERMOSTAT_DEFAULT_OFF_F
        )
    thermostat, cal_info = _calibrate_thermostat(prob, samples, hp, thermostat, heat=heat_ok, cool=cool_ok)
    info["thermostat_calibration"] = cal_info

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
    info["thermostat_defaults_used"] = (heat_seen and not heat_ok) or (cool_seen and not cool_ok)
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
    elif not (heat_ok and cool_ok):
        confidence, info["reason"] = "low", "too few accepted thermostat starts in a used mode"
    else:
        confidence, info["reason"] = "ok", ""
    info["modes"] = _mode_grades(
        prob,
        hp,
        confidence=confidence,
        sample_days=sample_days,
        seen={"heat": heat_seen, "cool": cool_seen},
        accepted={"heat": tstats.heat_accepted, "cool": tstats.cool_accepted},
        step=leads.get(ref, 16) if ref is not None else 16,
    )
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
        openness=openness,
    )


# ------------------------------------------------------------------- display-only nodes (Issue #1422)
def _lm_node(
    prob: _Problem,
    passes: list[tuple[list[float], list[float]]],
    ratio: float,
    start: Mapping[str, float],
    frozen: set[str],
    max_iterations: int,
) -> dict[str, float]:
    """Bounded Levenberg-Marquardt over ONE node's parameters (node 0 of ``prob``) on a fixed house trajectory."""
    free = [k for k in NODE_KEYS if k not in frozen]
    bnds = [NODE_BOUNDS[k] for k in free]

    def params(z: Sequence[float]) -> list[float]:
        d = dict(start)
        for k, (lo, hi), zi in zip(free, bnds, z, strict=True):
            d[k] = lo + min(1.0, max(0.0, zi)) * (hi - lo)
        return [d[k] for k in NODE_KEYS]

    z = [(start[k] - lo) / (hi - lo) for k, (lo, hi) in zip(free, bnds, strict=True)]
    r = prob.node_residuals(0, params(z), passes, ratio)
    cost = 0.5 * sum(x * x for x in r)
    if not free or not r or not math.isfinite(cost):
        return dict(zip(NODE_KEYS, params(z), strict=True))
    lam = 1e-2
    n = len(free)
    for _ in range(max_iterations):
        cols: list[list[float]] = []
        for i in range(n):
            step = 1e-3 if z[i] + 1e-3 <= 1.0 else -1e-3
            zz = list(z)
            zz[i] += step
            rp = prob.node_residuals(0, params(zz), passes, ratio)
            cols.append([(a - b) / step for a, b in zip(rp, r, strict=True)])
        jtj = [[sum(map(operator.mul, cols[a], cols[b], strict=True)) for b in range(n)] for a in range(n)]
        jtr = [sum(map(operator.mul, cols[a], r, strict=True)) for a in range(n)]
        accepted = False
        rel = moved = 0.0
        for _ in range(8):
            mat = [row[:] for row in jtj]
            for d in range(n):
                mat[d][d] += lam * (jtj[d][d] + 1e-9) + 1e-12
            step_z = _solve(mat, [-g for g in jtr])
            if step_z is None:
                lam *= 10.0
                continue
            zn = [min(1.0, max(0.0, zi + si)) for zi, si in zip(z, step_z, strict=True)]
            rn = prob.node_residuals(0, params(zn), passes, ratio)
            cn = 0.5 * sum(x * x for x in rn)
            if math.isfinite(cn) and cn < cost:
                rel = (cost - cn) / max(cost, 1e-12)
                moved = max(abs(a - b) for a, b in zip(zn, z, strict=True))
                z, r, cost = zn, rn, cn
                lam = max(lam / 3.0, 1e-7)
                accepted = True
                break
            lam *= 4.0
        if not accepted or rel < 1e-5 or moved < 1e-5:
            break
    return dict(zip(NODE_KEYS, params(z), strict=True))


def fit_display_nodes(
    model: V4Model,
    samples: Sequence[V4Sample],
    *,
    display_sensors: Sequence[str],
    tz: tzinfo,
    warm: Mapping[str, Mapping[str, float]] | None = None,
    max_anchors: int = 160,
    max_iterations: int = DISPLAY_MAX_ITERATIONS,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Fit display-only nodes on top of a fitted model (Issue #1422): ``(display_nodes, info)``.

    Occupant view: a thermostat or bedroom sensor whose per-sensor logging began a few days ago gets its own v4 line
    on the Status chart instead of "v4 is still learning" for two weeks. Nothing that decides reads these nodes.

    The house parameters (and so the primary trajectory) are HELD at the model's values: each node is fitted alone,
    by a node-only LM on the samples from that sensor's first reading on, so the control model is untouched by
    construction (a node never feeds back into ``H``). A node is kept only when it has ``GATE_MIN_ANCHORS`` anchors
    and beats "its reading won't change" at 4 h by ``GATE_MIN_SKILL`` - the same skill bar the model itself passes;
    otherwise ``info["rejected"][sid]`` says why. Never raises.
    """
    out: dict[str, dict[str, float]] = {}
    info: dict[str, Any] = {"tried": [], "kept": [], "per_sensor": {}, "rejected": {}}
    if model.confidence == "none" or not samples:
        return out, info
    hp = [float(model.house[k]) for k in HOUSE_KEYS]
    ratio = hp[8] / hp[7] if hp[7] > 1e-9 else 1.0
    fit_info = model.fit if isinstance(model.fit, Mapping) else {}
    try:
        horizon_h = int(fit_info.get("horizon_h", 12) or 12)
    except TypeError, ValueError:
        horizon_h = 12
    horizon_steps = max(4, int(horizon_h * 60 / V4_STEP_MINUTES))
    taken = {model.primary, CA_NODE, *model.sensors}
    for sid in dict.fromkeys(display_sensors):
        if not isinstance(sid, str) or sid in taken:
            continue
        info["tried"].append(sid)
        try:
            k0 = next((k for k, s in enumerate(samples) if _finite(s.readings.get(sid))), None)
            if k0 is None:
                info["rejected"][sid] = "no readings"
                continue
            prob = _Problem(
                samples[k0:],
                primary=model.primary,
                extra=[sid],
                tz=tz,
                horizon_steps=horizon_steps,
                max_anchors=max_anchors,
            )
            n_node = sum(1 for a in prob.anchors if a.b0[0] is not None and a.targets[1])
            if n_node < GATE_MIN_ANCHORS:
                info["rejected"][sid] = "too few anchors"
                continue
            passes = prob.house_pass(hp)
            frozen: set[str] = set()
            if prob.win_steps < GATE_MIN_MODE_STEPS:
                frozen.add("b_w")
            if prob.heat_steps < GATE_MIN_MODE_STEPS and prob.cool_steps < GATE_MIN_MODE_STEPS:
                frozen.add("q_b")
            start = dict(NODE_START)
            prev = (warm or {}).get(sid)
            if isinstance(prev, Mapping):
                start.update({k: float(v) for k, v in prev.items() if k in NODE_BOUNDS and _finite(v)})
            for k, (lo, hi) in NODE_BOUNDS.items():
                start[k] = min(hi, max(lo, start[k]))
            nd = _lm_node(prob, passes, ratio, start, frozen, max_iterations)
            if not all(math.isfinite(v) for v in nd.values()):
                info["rejected"][sid] = "fit diverged"
                continue
            params = [nd[k] for k in NODE_KEYS]
            model_err: dict[int, list[float]] = {}
            flat_err: dict[int, list[float]] = {}
            for a, (hs, sun) in zip(prob.anchors, passes, strict=True):
                b0 = a.b0[0]
                if b0 is None:
                    continue
                traj = _roll_node(params, a.h0, b0, a.out, a.win, hs, sun, a.uh, a.uc, ratio)
                for k, v, _ in a.targets[1]:
                    model_err.setdefault(k + 1, []).append(abs(traj[k] - v))
                    flat_err.setdefault(k + 1, []).append(abs(b0 - v))
            ref = 16 if 16 in model_err else 4
            m, f = _mae(model_err.get(ref)), _mae(flat_err.get(ref))
            if m is None or f is None or f <= 0 or m > (1.0 - GATE_MIN_SKILL) * f:
                info["rejected"][sid] = f"does not beat no-change at {ref // 4} h by 10 %"
                continue
            out[sid] = nd
            info["kept"].append(sid)
            info["per_sensor"][sid] = {f"{ref // 4}h": round(m, 3), "flat": round(f, 3), "anchors": n_node}
        except (ArithmeticError, ValueError, TypeError, IndexError, OverflowError) as exc:  # pragma: no cover
            info["rejected"][sid] = f"fit failed: {type(exc).__name__}"
    return out, info


def with_display_nodes(
    model: V4Model,
    samples: Sequence[V4Sample],
    *,
    display_sensors: Sequence[str],
    tz: tzinfo,
    warm: Mapping[str, Mapping[str, float]] | None = None,
    max_anchors: int = 160,
) -> V4Model:
    """``model`` with its ``display_nodes`` refitted (:func:`fit_display_nodes`); ``fit["display"]`` records why.
    Every other field is the input model's own object: the control model is unchanged."""
    nodes, info = fit_display_nodes(
        model, samples, display_sensors=display_sensors, tz=tz, warm=warm, max_anchors=max_anchors
    )
    fit_info = dict(model.fit) if isinstance(model.fit, Mapping) else {}
    fit_info["display"] = info
    return dataclasses.replace(model, display_nodes=nodes, fit=fit_info)


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
    hvac_action_now: str | None = None,
    run_elapsed_min: float | None = None,
    openness_prior: bool = True,
    ca_sleep_node: str | None = None,
    ca_sleep_time: str | None = None,
    ca_wake_time: str | None = None,
    display_nodes: bool = False,
) -> dict[str, list[tuple[datetime, float]]] | None:
    """Predict every model sensor at each ``inputs[k].ts`` (consecutive 15-minute grid points after ``now``).

    ``display_nodes=True`` (Issue #1422, the chart's lines only) also rolls the model's display-only nodes; every
    other line (primary, control nodes, ``_ca``) is the same with or without it, because a node never feeds back into
    the house and ``_ca`` only ever takes a CONTROL sleep node.

    ``None`` when confidence is "none", the primary reading is missing, or there are no inputs. Sensors
    whose own reading is missing are left out of the result. ``outdoor_now_f`` / ``forecast_now_f``
    (extension to the contract, optional) are today's observed outdoor temperature and what the raw
    forecast says for now; with both the nowcast term (current miss decaying over ``nowcast_tau_h``)
    is applied, otherwise only the hourly bias. ``hvac_action_now`` ("heating" / "cooling") and
    ``run_elapsed_min`` (optional) carry a run that is already in progress: it is not restarted and its minimum
    run is counted from ``run_elapsed_min`` (unknown -> 0). ``openness_prior`` applies the model's expected-openness
    prior (fractional windows / whole-house fan by hour of day) wherever the plan says closed / off.
    With ``ca_sleep_node`` (a model sensor) the result also carries ``"_ca"`` = :func:`ca_line` (the sleep node
    inside the sleep window, else the primary); without it ``_ca`` is the primary and callers derive it.
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
    if display_nodes:
        for sid, params in model.display_nodes.items():
            r = readings.get(sid)
            if sid in model.nodes or sid == model.primary or r is None or not math.isfinite(r):
                continue
            node_ids.append(sid)
            node_params.append([params[k] for k in NODE_KEYS])
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
    op = model.openness if openness_prior else None
    win = [1.0 if i.windows_open else 0.0 for i in inputs]
    fan = [1.0 if i.fan_running else 0.0 for i in inputs]
    if op is not None:
        for k in range(len(inputs)):
            hr = int((hours[k] - 0.125) % 24.0)
            if win[k] < 1.0:
                win[k] = op.window_by_hour[hr]
            if fan[k] < 1.0:
                fan[k] = op.fan_by_hour[hr]
    init_run: tuple[int, float] | None = None
    act = (hvac_action_now or "").lower()
    if act in (_ACTIVE_HEAT, _ACTIVE_COOL):
        init_run = (1 if act == _ACTIVE_HEAT else -1, float(run_elapsed_min) if run_elapsed_min is not None else 0.0)
    roll = rollout(
        house,
        node_params,
        h0=float(h0),
        m0=float(m0),
        b0=b0,
        out=out,
        win=win,
        fan=fan,
        hours=hours,
        thermostat=model.thermostat,
        heat_sp=[i.heat_sp_f for i in inputs],
        cool_sp=[i.cool_sp_f for i in inputs],
        init_run=init_run,
    )
    res: dict[str, list[tuple[datetime, float]]] = {
        model.primary: [(inp.ts, roll.primary[k]) for k, inp in enumerate(inputs)]
    }
    for j, sid in enumerate(node_ids):
        res[sid] = [(inp.ts, roll.nodes[j][k]) for k, inp in enumerate(inputs)]
    # ``_ca`` composes from a CONTROL sleep node only: a display-only bedroom node never becomes the CA-read line.
    # Issue #1432: with a room node as the primary (the per-room model) and a separate sleep sensor, ``_ca`` is ALWAYS
    # composed, so a sleep hour the sleep node cannot answer (not modelled, reading missing this cycle) is left out of
    # ``_ca`` instead of being filled from the thermostat's room by a caller that copies the primary line.
    control_sleep = bool(ca_sleep_node) and ca_sleep_node in res and ca_sleep_node in model.nodes
    if (
        ca_sleep_node
        and ca_sleep_node != model.primary
        and (control_sleep or (model.primary != CA_NODE and ca_sleep_time and ca_wake_time))
    ):
        src = res if control_sleep else {k: v for k, v in res.items() if k != ca_sleep_node}
        res[CA_NODE] = ca_line(
            src,
            primary=model.primary,
            sleep_node=ca_sleep_node,
            sleep_time=ca_sleep_time,
            wake_time=ca_wake_time,
            tz=tz,
        )
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
            "off_above_f": th.off_above_f,
            "off_below_f": th.off_below_f,
            "min_run_heat_min": th.min_run_heat_min,
            "min_run_cool_min": th.min_run_cool_min,
            "calibrated": th.calibrated,
        },
        "openness": (
            None
            if model.openness is None
            else {
                "window_by_hour": [float(v) for v in model.openness.window_by_hour],
                "fan_by_hour": [float(v) for v in model.openness.fan_by_hour],
                "days": float(model.openness.days),
            }
        ),
        "forecast": {
            "hourly_bias_f": [float(v) for v in model.forecast.hourly_bias_f],
            "nowcast_tau_h": float(model.forecast.nowcast_tau_h),
        },
        "fit": _jsonable(model.fit),
        "confidence": model.confidence,
        # Issue #1422: display-only nodes (chart lines, never control). Optional on read: older files have none.
        "display_nodes": {sid: {k: float(p[k]) for k in NODE_KEYS} for sid, p in model.display_nodes.items()},
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
        # a model fitted before R1b may carry c_mh below the new lower bound: clamp it (everything else is checked)
        house["c_mh"] = max(HOUSE_BOUNDS["c_mh"][0], house["c_mh"])
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
            off_above_f=float(th.get("off_above_f", 0.0)),
            off_below_f=float(th.get("off_below_f", 0.0)),
            min_run_heat_min=None if th.get("min_run_heat_min") is None else float(th["min_run_heat_min"]),
            min_run_cool_min=None if th.get("min_run_cool_min") is None else float(th["min_run_cool_min"]),
            calibrated=bool(th.get("calibrated", False)),
        )
        if not all(
            _finite(v)
            for v in (
                thermostat.on_below_f,
                thermostat.on_above_f,
                thermostat.burst_minutes,
                thermostat.off_above_f,
                thermostat.off_below_f,
            )
        ) or not _finite(thermostat.reading_step_f):
            return None
        if any(
            v is not None and (not _finite(v) or v <= 0)
            for v in (thermostat.min_run_heat_min, thermostat.min_run_cool_min)
        ):
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
        openness: V4Openness | None = None
        op_raw = d.get("openness")
        if op_raw is not None:  # absent in models written before R1b: no prior
            wbh = tuple(float(v) for v in op_raw["window_by_hour"])
            fbh = tuple(float(v) for v in op_raw["fan_by_hour"])
            if len(wbh) != 24 or len(fbh) != 24 or not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in (*wbh, *fbh)):
                return None
            openness = V4Openness(wbh, fbh, float(op_raw.get("days", 0.0)))
        display = _display_nodes_from(d.get("display_nodes"), sensors)
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
            openness=openness,
            display_nodes=display,
        )
    except KeyError, TypeError, ValueError, AttributeError, IndexError:
        return None


def _display_nodes_from(raw: Any, sensors: Sequence[str]) -> dict[str, dict[str, float]]:
    """The display-only nodes of a stored model (Issue #1422). Absent (older files) or malformed -> none; a bad node
    is dropped on its own. Never rejects the model: these lines are display only, so they cannot cost the control
    model a refit."""
    out: dict[str, dict[str, float]] = {}
    if not isinstance(raw, Mapping):
        return out
    for sid, p in raw.items():
        if not isinstance(sid, str) or sid in sensors or sid == CA_NODE or not isinstance(p, Mapping):
            continue
        try:
            nd = {k: float(p[k]) for k in NODE_KEYS}
        except KeyError, TypeError, ValueError:
            continue
        if all(_finite(nd[k]) and lo - 1e-9 <= nd[k] <= hi + 1e-9 for k, (lo, hi) in NODE_BOUNDS.items()):
            out[sid] = nd
    return out


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


# --------------------------------------------------------------------------- the "_ca" line
def in_sleep_window(local: datetime, sleep_time: str | None, wake_time: str | None) -> bool:
    """True when ``local`` is inside the ``sleep_time`` -> ``wake_time`` ("HH:MM") window.

    Same semantics as ``automation._in_sleep_window`` (this module may not import it: it is hass-free): the window
    wraps midnight when bedtime is after wake time, does not wrap when bedtime is after midnight, equal or
    malformed times are an empty window. A test pins the two together.
    """
    if not sleep_time or not wake_time:
        return False
    try:
        sh, sm = (int(x) for x in str(sleep_time).split(":")[:2])
        wh, wm = (int(x) for x in str(wake_time).split(":")[:2])
        now_m = local.hour * 60 + local.minute
    except ValueError, AttributeError:
        return False
    s_m, w_m = sh * 60 + sm, wh * 60 + wm
    if s_m == w_m:
        return False
    if s_m < w_m:
        return s_m <= now_m < w_m
    return now_m >= s_m or now_m < w_m


def ca_line(
    preds: Mapping[str, Sequence[tuple[datetime, float]]],
    *,
    primary: str,
    sleep_node: str | None,
    sleep_time: str | None,
    wake_time: str | None,
    tz: tzinfo,
) -> list[tuple[datetime, float]]:
    """The ``_ca`` line: the reading Climate Advisor itself uses (R1b, M3).

    During the sleep window ``indoor`` is the sleep sensor's reading, so a step inside the window takes the sleep
    node's prediction when that node was predicted; every other step takes the primary node's.

    Never the wrong room (Issue #1432): when the primary is a room node (not the stitched ``_ca`` reading) and a
    separate ``sleep_node`` exists, a step inside the sleep window that the sleep node does not answer is LEFT OUT of
    the line, never filled from the primary (the thermostat's room). With the stitched ``_ca`` as the primary, its own
    value is the CA reading at night too, so it stands in as before.
    """
    base = preds.get(primary) or []
    sleep = preds.get(sleep_node) if sleep_node else None
    guard = bool(sleep_node) and sleep_node != primary and primary != CA_NODE
    if not sleep and not guard:
        return list(base)
    by_ts = {t.timestamp(): v for t, v in (sleep or [])}
    out = []
    for t, v in base:
        if not in_sleep_window(t.astimezone(tz), sleep_time, wake_time):
            out.append((t, v))
        elif t.timestamp() in by_ts:
            out.append((t, by_ts[t.timestamp()]))
        elif not guard:
            out.append((t, v))
    return out


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
    primary: str | None = None,
    primary_aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
) -> list[V4Sample]:
    """Resample raw chart_log rows to the 15-minute grid (aligned to the hour, any timezone).

    Each field is carried forward from the latest non-null row up to 2 hours (older -> ``None``); a
    grid point with no row in the previous 2 hours is dropped, so a gap breaks continuity (consumers
    check consecutive timestamps). ``readings`` come from each row's ``indoor_sensors`` map, and
    ``"_ca"`` from ``indoor``. ``hvac_action`` is the row's ``hvac`` string; the heat / cool duty of
    each step is integrated from the row timeline (action-change rows make it exact). The setpoint the
    predictor will receive - the plan's target band - is assigned to the mode: only ACTIVE rows set the
    mode (heating -> heat, cooling -> cool); an idle row keeps the last active mode within 12 h (else a guess
    from the outdoor temperature) and takes that mode's own band edge (``lower`` for heat, ``upper`` for
    cool), never the opposite edge. Rows without a band fall back to the row ``setpoint`` on active rows and
    to the last active row's setpoint of that mode on idle rows. ``primary`` (an entity id) names the node
    the live fit uses: rows lacking it in ``indoor_sensors`` (older logs, or the climate-attribute era before
    the finer sensor) fill it from ``primary_aliases`` readings, else ``indoor_primary``, else ``indoor`` unless
    the row is inside the sleep window (``in_sleep``, when a sleep sensor is configured: ``indoor`` was then
    the bedroom). Forecast: with ``archive_snapshots`` (ForecastArchive read shape) the value the
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
    last_active_sp: dict[str, tuple[float, float]] = {}
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
                    sp_row = _num(e.get("setpoint"))
                    if sp_row is not None:
                        last_active_sp["heat" if hv.lower() == _ACTIVE_HEAT else "cool"] = (ep, sp_row)
            for key in ("indoor", "outdoor", "pred_outdoor", "setpoint", "lower", "upper"):
                v = _num(e.get(key))
                if v is not None:
                    state[key] = (ep, v)
            for key in ("windows_open",):
                if isinstance(e.get(key), bool):
                    state[key] = (ep, e[key])
            fr = e.get("fan_running", e.get("fan"))
            if isinstance(fr, bool):
                state["fan"] = (ep, fr)
                state["whf"] = (ep, bool(fr) and e.get("nat_vent_active") is True)
            sens = e.get("indoor_sensors")
            if isinstance(sens, Mapping):
                for sid, val in sens.items():
                    v = _num(val)
                    if v is not None:
                        sensor_state[str(sid)] = (ep, v)
            if primary:
                pv = _row_primary_value(e, primary, primary_aliases, in_sleep, tz)
                if pv is not None:
                    sensor_state[primary] = (ep, pv)
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
        heat_sp = cool_sp = None
        if hv_now not in (None, "off", ""):
            mode = _mode_for(hv_now, last_active, g, outdoor)
            sp = fresh("lower" if mode == "heat" else "upper")
            if sp is None:
                if hv_now in (_ACTIVE_HEAT, _ACTIVE_COOL):
                    sp = fresh("setpoint")
                else:
                    kept = last_active_sp.get(mode)
                    sp = kept[1] if kept is not None and g - kept[0] <= 12 * 3600 else None
            if sp is not None:
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
                whf_running=bool(fresh("whf")),
            )
        )
        g += _STEP_S
    return out


def _row_primary_value(
    row: Mapping[str, Any],
    primary: str,
    aliases: Sequence[str],
    in_sleep: Callable[[datetime], bool] | None,
    tz: tzinfo,
) -> float | None:
    """The primary node's reading on one chart_log row, or ``None`` (see :func:`samples_from_chart_log`)."""
    sens = row.get("indoor_sensors")
    if isinstance(sens, Mapping):
        if primary in sens:
            return _num(sens[primary])
        for alias in aliases:
            if alias in sens:
                return _num(sens[alias])
    v = _num(row.get("indoor_primary"))
    if v is not None:
        return v
    if in_sleep is not None:
        ts = _parse_ts(row.get("ts"), tz)
        if ts is None or in_sleep(ts.astimezone(tz)):
            return None
    return _num(row.get("indoor"))


def resolve_nodes(
    entries: Sequence[Mapping[str, Any]],
    *,
    primary: str,
    sensors: Sequence[str],
    aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    tz: tzinfo,
    forced: bool = False,
    min_primary_cov: float = 0.8,
    min_extra_cov: float = 0.5,
) -> tuple[str, list[str]]:
    """Primary node + extra nodes for a fit on ``entries``: the ONE rule the live shadow and the backtest share.

    The primary entity is used when it reads (directly, through ``aliases``, ``indoor_primary`` or an ``indoor``
    row outside the sleep window) on at least ``min_primary_cov`` of the rows, else the model is the ``_ca``
    reading alone; ``forced`` skips that check. Extra entities in ``sensors`` need ``min_extra_cov`` coverage in
    ``indoor_sensors``. Judged on the rows given, i.e. the training rows only.
    """
    n = len(entries) or 1
    pcov = sum(1 for r in entries if _row_primary_value(r, primary, aliases, in_sleep, tz) is not None) / n
    prim = primary if (forced or pcov >= min_primary_cov) else CA_NODE
    extras: list[str] = []
    for sid in sensors:
        if sid in (prim, primary, CA_NODE) or sid in extras:
            continue
        cov = (
            sum(
                1
                for r in entries
                if isinstance(r.get("indoor_sensors"), Mapping) and _num(r["indoor_sensors"].get(sid)) is not None
            )
            / n
        )
        if cov >= min_extra_cov:
            extras.append(sid)
    return prim, [prim, *extras]


def resolve_display_nodes(
    entries: Sequence[Mapping[str, Any]],
    *,
    nodes: Sequence[str],
    primary: str,
    sensors: Sequence[str],
    tz: tzinfo,
    min_history_days: float = NODE_MIN_HISTORY_DAYS,
    min_cov: float = NODE_MIN_COVERAGE,
) -> list[str]:
    """Display-only extra nodes for a fit on ``entries`` (Issue #1422): the sensors :func:`resolve_nodes` left out
    that qualify on THEIR OWN history.

    Occupant view: per-sensor logging (``indoor_sensors``) began on a known day, so judging a sensor over the whole
    30-day window kept the thermostat and bedroom lines at "v4 is still learning" for about two weeks. Here each
    candidate (``sensors``, plus the entity ``primary`` when the model fell back to ``_ca``) is judged over the span
    from its first ``indoor_sensors`` row to the last row given: at least ``min_history_days`` of it, and a reading
    on at least ``min_cov`` of those rows. ``nodes`` (the control nodes ``resolve_nodes`` chose) are never repeated, so
    a sensor that passes the control rule stays a control node exactly as before.
    """
    taken = {CA_NODE, *nodes}
    cands = [s for s in dict.fromkeys([primary, *sensors]) if isinstance(s, str) and s and s not in taken]
    if not cands:
        return []
    rows: list[tuple[float, Mapping[str, Any] | None]] = []
    for r in entries:
        if not isinstance(r, Mapping):
            continue
        ts = _parse_ts(r.get("ts"), tz)
        if ts is None:
            continue
        sens = r.get("indoor_sensors")
        rows.append((ts.timestamp(), sens if isinstance(sens, Mapping) else None))
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    last = rows[-1][0]
    out: list[str] = []
    for sid in cands:
        first = next((i for i, (_, s) in enumerate(rows) if s is not None and sid in s), None)
        if first is None or last - rows[first][0] < min_history_days * 86400.0:
            continue
        since = rows[first:]
        good = sum(1 for _, s in since if s is not None and _num(s.get(sid)) is not None)
        if good / len(since) >= min_cov:
            out.append(sid)
    return out


# ------------------------------------------------------------------- the control model choice (Issue #1432)
def sensor_history(
    entries: Sequence[Mapping[str, Any]],
    sid: str,
    *,
    tz: tzinfo,
    primary_aliases: Sequence[str] | None = None,
    in_sleep: Callable[[datetime], bool] | None = None,
) -> dict[str, float] | None:
    """``{"days", "coverage"}`` of ``sid``'s OWN history in ``entries``, or ``None`` when it never appears.

    The span runs from the sensor's first ``indoor_sensors`` row to the last row given (the #1422 display rule);
    ``coverage`` is the share of the rows since then with a reading. With ``primary_aliases`` (not ``None``) ``sid`` is
    the primary entity: a row also starts and counts its history through an alias or ``indoor_primary``, and a reading
    is whatever :func:`_row_primary_value` maps onto the primary (the same mapping the fit's samples use)."""
    rows: list[tuple[float, Mapping[str, Any]]] = []
    for r in entries:
        if not isinstance(r, Mapping):
            continue
        ts = _parse_ts(r.get("ts"), tz)
        if ts is not None:
            rows.append((ts.timestamp(), r))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    is_primary = primary_aliases is not None
    keys = (sid, *(primary_aliases or ()))

    def starts(r: Mapping[str, Any]) -> bool:
        sens = r.get("indoor_sensors")
        if isinstance(sens, Mapping) and any(k in sens for k in keys):
            return True
        return is_primary and _num(r.get("indoor_primary")) is not None

    def reads(r: Mapping[str, Any]) -> bool:
        if is_primary:
            return _row_primary_value(r, sid, tuple(primary_aliases or ()), in_sleep, tz) is not None
        sens = r.get("indoor_sensors")
        return isinstance(sens, Mapping) and _num(sens.get(sid)) is not None

    first = next((i for i, (_, r) in enumerate(rows) if starts(r)), None)
    if first is None:
        return None
    since = rows[first:]
    days = (rows[-1][0] - rows[first][0]) / 86400.0
    return {"days": round(days, 2), "coverage": round(sum(1 for _, r in since if reads(r)) / len(since), 3)}


@dataclass(frozen=True)
class ControlNodePlan:
    """The two candidate node sets for a fit (Issue #1432): A ``stitched`` (always) and B ``per_room`` (or None)."""

    stitched: tuple[str, list[str]]
    per_room: tuple[str, list[str]] | None
    sleep_node: str | None  # the separate sleep sensor (None: none configured, or it is the primary)
    record: dict[str, Any]  # what qualified and why: logged, stored as ``fit["node_choice"]``


def resolve_control_nodes(
    entries: Sequence[Mapping[str, Any]],
    *,
    primary: str,
    sensors: Sequence[str],
    sleep_sensor: str | None = None,
    aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    tz: tzinfo,
    min_history_days: float = NODE_MIN_HISTORY_DAYS,
    min_cov: float = NODE_MIN_COVERAGE,
) -> ControlNodePlan:
    """The candidate control models for a fit on ``entries`` (the training rows): the ONE rule the live fit and the
    backtest share (Issue #1432).

    * A (``stitched``): ``_ca`` alone plus the extra entities :func:`resolve_nodes` keeps (50 % of the window).
    * B (``per_room``): the primary entity plus the sleep sensor as a node (plus the same extras). It is a candidate
      when the primary qualifies - 80 % of the window (:func:`resolve_nodes`, legacy rows through ``aliases`` /
      ``indoor_primary`` / ``indoor`` outside the sleep window count) or ``min_history_days`` of its own history at
      ``min_cov`` (:func:`sensor_history`, the #1422 rule) - AND, when a separate ``sleep_sensor`` is configured, the
      sleep sensor qualifies the same way (50 % of the window, or its own history). The primary qualifying alone is
      NOT enough while a sleep sensor exists: sleep hours would then be forecast from the thermostat's room.

    Whether B is used is decided by :func:`choose_control_model` (the backtest gate)."""
    legacy_prim, legacy_nodes = resolve_nodes(
        entries, primary=primary, sensors=sensors, aliases=aliases, in_sleep=in_sleep, tz=tz
    )
    sleep = sleep_sensor if sleep_sensor and sleep_sensor not in (primary, CA_NODE) else None
    extras = [s for s in legacy_nodes[1:] if s not in (primary, CA_NODE)]
    stitched = (CA_NODE, [CA_NODE, *extras])
    rec: dict[str, Any] = {
        "primary": primary if primary and primary != CA_NODE else None,
        "sleep": sleep,
        "min_days": min_history_days,
        "min_coverage": min_cov,
    }
    if not primary or primary == CA_NODE:
        rec["reason"] = "no_primary_entity"
        return ControlNodePlan(stitched, None, sleep, rec)
    ph = sensor_history(entries, primary, tz=tz, primary_aliases=tuple(aliases), in_sleep=in_sleep) or {}
    p_ok = legacy_prim == primary or (ph.get("days", 0.0) >= min_history_days and ph.get("coverage", 0.0) >= min_cov)
    rec.update(primary_days=ph.get("days"), primary_coverage=ph.get("coverage"), primary_ok=p_ok)
    s_ok = True
    if sleep:
        sh = sensor_history(entries, sleep, tz=tz) or {}
        s_ok = sleep in extras or (sh.get("days", 0.0) >= min_history_days and sh.get("coverage", 0.0) >= min_cov)
        rec.update(sleep_days=sh.get("days"), sleep_coverage=sh.get("coverage"), sleep_ok=s_ok)
    if not p_ok:
        rec["reason"] = "primary_not_ready"
        return ControlNodePlan(stitched, None, sleep, rec)
    if not s_ok:
        rec["reason"] = "sleep_not_ready"
        return ControlNodePlan(stitched, None, sleep, rec)
    per_room = (primary, [primary, *([sleep] if sleep else []), *(s for s in extras if s != sleep)])
    return ControlNodePlan(stitched, per_room, sleep, rec)


def _ca_composes(model: V4Model, sleep_node: str | None) -> bool:
    """True when ``model``'s ``_ca`` takes ``sleep_node``'s line inside the sleep window (``predict``'s rule)."""
    return bool(sleep_node) and sleep_node != model.primary and sleep_node in model.nodes


def score_ca_lines(
    models: Mapping[str, V4Model],
    samples: Sequence[V4Sample],
    *,
    tz: tzinfo,
    sleep_node: str | None = None,
    in_sleep: Callable[[datetime], bool] | None = None,
    max_anchors: int = CA_GATE_MAX_ANCHORS,
) -> dict[str, Any]:
    """Replay MAE of each model's ``_ca`` line against the recorded CA reading, on SHARED anchors (Issue #1432).

    The same replay as ``fit["mae_by_lead"]`` (recorded outdoor, windows, fan and heating/cooling duty from each
    anchor's reading and hidden-mass state), but scored on the CA reading (``readings["_ca"]``, the comfort-facing
    ``indoor``) at the ``CA_GATE_LEADS`` leads, with each model's ``_ca`` composed the way :func:`predict` composes it:
    the sleep node's value inside the sleep window when that node is modelled, else the primary's. Only anchors every
    model can start from are scored (each needs its primary reading, a composing model also its sleep reading), up to
    ``max_anchors`` by an even stride, so the numbers compare like with like.
    ``{"n_anchors": n, "mae": {label: {lead: mae | None}}, "n": {label: {lead: count}}}``. Never raises."""
    out: dict[str, Any] = {"n_anchors": 0, "mae": {k: {} for k in models}, "n": {k: {} for k in models}}
    if not models or not samples:
        return out
    try:
        horizon = max(_GATE_LEAD_STEPS.values())
        probs: dict[str, _Problem] = {}
        for label, m in models.items():
            extra = [s for s in m.sensors if s != m.primary]
            probs[label] = _Problem(
                samples, primary=m.primary, extra=extra, tz=tz, horizon_steps=horizon, max_anchors=10**9
            )
        usable: set[int] | None = None
        for label, m in models.items():
            pr = probs[label]
            j = pr.extra.index(sleep_node) if _ca_composes(m, sleep_node) and sleep_node in pr.extra else None
            ok = {a.i for a in pr.anchors if j is None or a.b0[j] is not None}
            usable = ok if usable is None else usable & ok
        common = sorted(usable or ())
        if max_anchors and len(common) > max_anchors:
            stride = math.ceil(len(common) / max_anchors)
            common = common[::stride]
        keep = set(common)
        out["n_anchors"] = len(common)
        if not common:
            return out
        for label, m in models.items():
            pr = probs[label]
            pr.anchors = [a for a in pr.anchors if a.i in keep]
            hp = [float(m.house[k]) for k in HOUSE_KEYS]
            ratio = hp[8] / hp[7] if hp[7] > 1e-9 else 1.0
            passes = pr.house_pass(hp)
            compose = _ca_composes(m, sleep_node) and sleep_node in pr.extra
            j = pr.extra.index(sleep_node) if compose else None
            nd = [float(m.nodes[sleep_node][k]) for k in NODE_KEYS] if compose and sleep_node else None
            errs: dict[str, list[float]] = {lead: [] for lead in CA_GATE_LEADS}
            for a, (hs, sun) in zip(pr.anchors, passes, strict=True):
                traj = None
                if j is not None and nd is not None and a.b0[j] is not None:
                    traj = _roll_node(nd, a.h0, a.b0[j], a.out, a.win, hs, sun, a.uh, a.uc, ratio)
                for lead in CA_GATE_LEADS:
                    s = _GATE_LEAD_STEPS[lead]
                    if s > a.length:
                        continue
                    smp = samples[a.i + s]
                    actual = smp.readings.get(CA_NODE)
                    if not _finite(actual):
                        continue
                    asleep = in_sleep is not None and in_sleep(smp.ts.astimezone(tz))
                    pred = traj[s - 1] if (traj is not None and asleep) else hs[s - 1]
                    errs[lead].append(abs(pred - float(actual)))  # type: ignore[arg-type]
            out["mae"][label] = {lead: _round3(_mae(e)) for lead, e in errs.items()}
            out["n"][label] = {lead: len(e) for lead, e in errs.items()}
    except (ArithmeticError, ValueError, TypeError, IndexError, OverflowError) as exc:  # pragma: no cover
        out["error"] = type(exc).__name__
    return out


def _round3(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def ca_gate(scores: Mapping[str, Any], *, tolerance_f: float = CA_GATE_TOLERANCE_F) -> tuple[bool, str]:
    """``(per_room passes, reason)`` from :func:`score_ca_lines` output for ``stitched`` and ``per_room`` (Issue #1432).

    Passes when at least ``GATE_MIN_ANCHORS`` shared anchors were scored, both models have a ``CA_GATE_LEAD`` score,
    and at every ``CA_GATE_LEADS`` lead both scored the per-room MAE is at most the stitched MAE + ``tolerance_f``."""
    mae = scores.get("mae") or {}
    a, b = mae.get(MODEL_STITCHED) or {}, mae.get(MODEL_PER_ROOM) or {}
    n = int(scores.get("n_anchors") or 0)
    if n < GATE_MIN_ANCHORS or a.get(CA_GATE_LEAD) is None or b.get(CA_GATE_LEAD) is None:
        return False, "too_few_shared_anchors"
    for lead in CA_GATE_LEADS:
        if a.get(lead) is not None and b.get(lead) is not None and b[lead] > a[lead] + tolerance_f:
            return False, "per_room_scored_worse"
    return True, "per_room_no_worse"


@dataclass
class ControlFit:
    """The model the decisions read, the samples it was fitted on and why it was chosen (Issue #1432)."""

    model: V4Model
    samples: list[V4Sample]
    primary: str
    nodes: list[str]
    choice: dict[str, Any]


def choose_control_model(
    entries: Sequence[Mapping[str, Any]],
    *,
    tz: tzinfo,
    now: datetime,
    primary: str,
    sensors: Sequence[str],
    sleep_sensor: str | None = None,
    aliases: Sequence[str] = (),
    in_sleep: Callable[[datetime], bool] | None = None,
    archive_snapshots: Sequence[dict] | None = None,
    warm_start: V4Model | None = None,
    horizon_h: int = 12,
    sample_filter: Callable[[list[V4Sample]], list[V4Sample]] | None = None,
) -> ControlFit:
    """Fit the model the decisions will read: A (stitched ``_ca``) or B (per room), Issue #1432.

    Occupant view: pre-heat, pre-cool, the ceiling guard and the setback depth read v4's forecast of the reading CA
    uses (the thermostat by day, the bedroom in the sleep window). B forecasts each room with its own node, so it is
    used as soon as both rooms have ``NODE_MIN_HISTORY_DAYS`` of their own readings (:func:`resolve_control_nodes`) -
    but only when, on this home's own training rows, B's ``_ca`` replay is no worse than A's (:func:`ca_gate`). Until
    then, and whenever B is worse, A stands. Both models are fitted when B is a candidate (about twice a fit's cost).

    ``sample_filter`` (the backtest's no-leak cut) is applied to each sample list before fitting. Display-only nodes
    are NOT fitted here (callers add them with :func:`with_display_nodes`). The model's ``fit["node_choice"]`` records
    the choice; ``choice["model"]`` is ``MODEL_STITCHED`` or ``MODEL_PER_ROOM``. Never raises on thin data."""
    plan = resolve_control_nodes(
        entries, primary=primary, sensors=sensors, sleep_sensor=sleep_sensor, aliases=aliases, in_sleep=in_sleep, tz=tz
    )
    choice: dict[str, Any] = dict(plan.record)
    choice.update(tolerance_f=CA_GATE_TOLERANCE_F, lead=CA_GATE_LEAD)
    keep = sample_filter or (lambda s: s)

    def samples_for(prim: str) -> list[V4Sample]:
        return keep(
            samples_from_chart_log(
                entries,
                tz=tz,
                archive_snapshots=archive_snapshots,
                primary=None if prim == CA_NODE else prim,
                primary_aliases=tuple(aliases),
                in_sleep=in_sleep,
            )
        )

    def fit_for(prim: str, nodes: list[str], smp: list[V4Sample]) -> V4Model:
        return fit(smp, primary=prim, sensors=nodes, tz=tz, now=now, horizon_h=horizon_h, warm_start=warm_start)

    a_prim, a_nodes = plan.stitched
    a_samples = samples_for(a_prim)
    model_a = fit_for(a_prim, a_nodes, a_samples)
    chosen = ControlFit(model_a, a_samples, a_prim, list(a_nodes), choice)
    if plan.per_room is not None:
        b_prim, b_nodes = plan.per_room
        b_samples = samples_for(b_prim)
        model_b = fit_for(b_prim, b_nodes, b_samples)
        scores = score_ca_lines(
            {MODEL_STITCHED: model_a, MODEL_PER_ROOM: model_b}, b_samples, tz=tz, sleep_node=plan.sleep_node,
            in_sleep=in_sleep,
        )  # fmt: skip
        choice["scores"] = scores
        if model_b.confidence == "none":
            choice["reason"] = "per_room_not_usable"
            choice["per_room_fit_reason"] = model_b.fit.get("reason", "")
        elif model_a.confidence == "none":
            choice["reason"] = "stitched_not_usable"
            chosen = ControlFit(model_b, b_samples, b_prim, list(b_nodes), choice)
        else:
            ok, why = ca_gate(scores)
            choice["reason"] = why
            if ok:
                chosen = ControlFit(model_b, b_samples, b_prim, list(b_nodes), choice)
    choice["model"] = MODEL_PER_ROOM if chosen.primary != CA_NODE else MODEL_STITCHED
    fit_info = dict(chosen.model.fit) if isinstance(chosen.model.fit, Mapping) else {}
    fit_info["node_choice"] = _jsonable_choice(choice)
    chosen.model = dataclasses.replace(chosen.model, fit=fit_info)
    return chosen


def _jsonable_choice(choice: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy (no NaN) of a node-choice record."""

    def clean(v: Any) -> Any:
        if isinstance(v, Mapping):
            return {str(k): clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v

    return clean(choice)


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
    return V4Thermostat(
        on_below_f=THERMOSTAT_DEFAULT_ON_F,
        on_above_f=THERMOSTAT_DEFAULT_ON_F,
        burst_minutes=10.0,
        reading_step_f=0.0,
        off_above_f=THERMOSTAT_DEFAULT_OFF_F,
        off_below_f=THERMOSTAT_DEFAULT_OFF_F,
    )


def plan_setpoints(
    lo: float | None,
    hi: float | None,
    last: tuple[float | None, float | None] = (None, None),
) -> tuple[float | None, float | None]:
    """The plan's setpoints for one future step: BOTH band edges, whatever the day's mode.

    ``lo`` / ``hi`` are the target band's floor / ceiling at that step; an edge the band does not give is carried
    over from the previous step (``last``). The one rule the live shadow (``v4_shadow.build_inputs``) and the backtest
    (``predict_backtest.build_inputs``) share, so the gate scores the inputs that run live (Issue #1230 R1b).
    """
    return (lo if lo is not None else last[0], hi if hi is not None else last[1])


def grid_steps(start: datetime, count: int) -> list[datetime]:
    """``count`` consecutive 15-minute grid timestamps after ``start``, stepped in absolute (UTC) time so a
    DST transition neither repeats nor skips a grid point (wall-clock ``+ timedelta`` would)."""
    base = start.astimezone(UTC)
    return [(base + timedelta(minutes=V4_STEP_MINUTES * (k + 1))).astimezone(start.tzinfo) for k in range(count)]


# --------------------------------------------------------------------------- decision rates (Issue #1230 R6b)
CONTROL_RATE_STEPS = 4  # the rate decisions size a one-hour run (pre-heat lead, TOU bank, setback recovery)
CONTROL_CONFIDENCE = {"low": "low", "ok": "medium"}  # v4's grades in the grades the decisions' thermal-model reads use


def control_rates(model: V4Model) -> dict[str, Any]:
    """The thermal-model scalars the rate decisions read, derived from a fitted v4 model.

    Occupant view: pre-heat, the time-of-use bank, the setback depth and the ceiling guard's lead time ask "how many
    degrees per hour does my heating (cooling) deliver?" and "how fast does the house drift to the outdoor
    temperature?". When v4 is the primary engine those answers come from v4's own fit, in the same keys the v3 thermal
    model uses, so no decision code names an engine.

    * ``k_active_heat`` / ``heating_rate_f_per_hour``: the net gain of ONE HOUR of continuous heating, measured by
      rolling this model's own integrator for ``CONTROL_RATE_STEPS`` steps from a settled room with no leak and no sun
      (so it is the gross heater gain the way v3's ``k_active`` is, but it includes the hidden-mass lag: the first hour
      of heat does not raise the air by the full ``q_h``). Cooling is the mirror: ``k_active_cool`` is negative,
      ``cooling_rate_f_per_hour`` its magnitude.
    * ``k_passive``: ``-a_o``, the envelope leak toward the outdoor temperature (v3's ``k_passive`` is negative by the
      same convention; ``a_o`` is bounded to ``[0.001, 0.5]``, v3's own bounds).
    * ``confidence`` / ``confidence_k_passive``: v4 ``low`` -> ``low``, ``ok`` -> ``medium`` ("none" never gets here).
    """
    if model.confidence == "none":
        return {}
    house = [float(model.house[k]) for k in HOUSE_KEYS]
    leakless = list(house)
    leakless[0] = 0.0  # a_o: the gross heater gain, not net of the leak back to a (here: equal) outdoor
    n = CONTROL_RATE_STEPS
    h0 = 68.0
    zeros = [0.0] * n
    common: dict[str, Any] = {
        "h0": h0,
        "m0": h0,
        "b0": [],
        "out": [h0] * n,
        "win": zeros,
        "fan": zeros,
        "hours": zeros,  # local midnight: no sun term
    }
    heat = rollout(leakless, [], heat_u=[1.0] * n, cool_u=zeros, **common)
    cool = rollout(leakless, [], heat_u=zeros, cool_u=[1.0] * n, **common)
    hours = n * _DT_H
    k_heat = max(0.0, (heat.primary[-1] - h0) / hours)
    k_cool = min(0.0, (cool.primary[-1] - h0) / hours)
    grade = CONTROL_CONFIDENCE.get(model.confidence, "low")
    a_o = min(max(float(model.house["a_o"]), 0.001), 0.5)
    out: dict[str, Any] = {
        "k_active_heat": k_heat,
        "k_active_cool": k_cool,
        "heating_rate_f_per_hour": round(k_heat, 2),
        "cooling_rate_f_per_hour": round(abs(k_cool), 2),
        "k_passive": -a_o,
        "k_passive_via_bridge": False,
        "confidence": grade,
        "confidence_k_passive": grade,
        "k_vent_window": None,
        "k_vent_fan": None,
        "confidence_k_vent": "none",
    }
    # Issue #1505: the window and fan leak v4 fitted together with the envelope, mass, sun and equipment, in v3's
    # keys and sign (the total rate toward outdoor with windows open, without / with the fan running). A term with no
    # data behind it (``fit["frozen"]``) is not exported: its start value is not a measurement.
    frozen = set((model.fit or {}).get("frozen") or ())
    if "a_w" not in frozen:
        out["k_vent_window"] = -(a_o + max(float(model.house["a_w"]), 0.0))
        out["confidence_k_vent"] = grade
    if "a_f" not in frozen:
        out["k_vent_fan"] = -(a_o + max(float(model.house["a_f"]), 0.0))
        out["confidence_k_vent"] = grade
    return out
