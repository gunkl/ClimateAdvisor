"""Duty-balance fit of the home's heating, cooling and leak rates (Issues #1512, #1516).

Pure module: no Home Assistant imports. It reads chart_log rows and fits, over closed-house settled intervals::

    dT = (k_passive * mean(T_in - T_out)) * dt + k_active_heat * heat_h + k_active_cool * cool_h

(``dT`` = indoor change over the interval, ``heat_h``/``cool_h`` = hours of heating/cooling in it). Because it
regresses the temperature *change* on hours, it is the duty-weighted least squares of the per-hour rate
``(T1 - T0) / dt`` (weight ``dt**2``, which is also the right weight for sensor quantization noise: the rate error of
a step is ``resolution / dt``). Hours of data average the quantization out; 1.0 F sensors need more hours than 0.1 F.

Residual bias: heat that keeps arriving after a furnace stops (duct/plenum coast) lands in the next idle interval,
which is excluded (``settling_hvac``) when the caller's row states mark it, so the bias is the part of the coast that
falls inside the interval whose start row is still active. The estimand is therefore close to the steady rate.

Intervals are excluded when: windows open, fan running/paused, any settling flag, a gap > ``max_gap_min``, a solar
hour at either end, a missing reading, or ``|dT| > 3 F`` in one step (sensor swap/jump, as in Issue #1505).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .const import (
    THERMAL_K_ACTIVE_COOL_MAX,
    THERMAL_K_ACTIVE_COOL_MIN,
    THERMAL_K_ACTIVE_HEAT_MAX,
    THERMAL_K_ACTIVE_HEAT_MIN,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
)

_MAX_STEP_DELTA_F = 3.0
_HEAT_ACTIONS = ("heating", "heat")
_COOL_ACTIONS = ("cooling", "cool")


def _parse(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts))
    except ValueError, TypeError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a small square system; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-9:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def _intervals(
    rows: Sequence[Mapping],
    states: Sequence[Mapping[str, bool]],
    is_solar_hour: Callable[[str], bool],
    max_gap_min: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(min(len(rows), len(states)) - 1):
        r0, r1, s0, s1 = rows[i], rows[i + 1], states[i], states[i + 1]
        if r0.get("windows_open") or r1.get("windows_open"):
            continue
        if any(s.get(k) for s in (s0, s1) for k in ("running", "paused", "settling_fan", "settling_close")):
            continue
        t0, t1 = _parse(r0.get("ts")), _parse(r1.get("ts"))
        if t0 is None or t1 is None:
            continue
        dt_h = (t1 - t0).total_seconds() / 3600.0
        if dt_h <= 0 or dt_h * 60.0 > max_gap_min:
            continue
        if is_solar_hour(str(r0.get("ts"))) or is_solar_hour(str(r1.get("ts"))):
            continue
        try:
            a0, a1 = float(r0["indoor"]), float(r1["indoor"])
            o0, o1 = float(r0["outdoor"]), float(r1["outdoor"])
        except KeyError, TypeError, ValueError:
            continue
        if abs(a1 - a0) > _MAX_STEP_DELTA_F:
            continue
        action = str(r0.get("hvac") or "").lower()
        heat = action in _HEAT_ACTIONS
        cool = action in _COOL_ACTIONS
        # The interval right after a run is the furnace/duct coast: not a closed-house leak measurement.
        if not (heat or cool) and s0.get("settling_hvac"):
            continue
        out.append(
            {
                "dt_h": dt_h,
                "d_t": a1 - a0,
                "delta": ((a0 - o0) + (a1 - o1)) / 2.0,
                "heat": 1.0 if heat else 0.0,
                "cool": 1.0 if cool else 0.0,
                "t0": str(r0.get("ts")),
                "t1": str(r1.get("ts")),
            }
        )
    return out


def fit_duty_balance(
    rows: Sequence[Mapping],
    states: Sequence[Mapping[str, bool]],
    *,
    is_solar_hour: Callable[[str], bool],
    max_gap_min: float = 35.0,
    min_intervals: int = 24,
    min_duty_h: float = 1.0,
) -> dict | None:
    """Fit k_passive, k_active_heat and k_active_cool from chart_log rows; None when not identifiable or invalid.

    ``r_squared`` is *centered* on the mean temperature change (1 - SSres/SStot over ``dT``): with no intercept the
    uncentered form is inflated by the mean and would look good for a model that explains nothing.
    """
    ivs = _intervals(rows, states, is_solar_hour, max_gap_min)
    if len(ivs) < min_intervals:
        return None

    heat_h = sum(v["heat"] * v["dt_h"] for v in ivs)
    cool_h = sum(v["cool"] * v["dt_h"] for v in ivs)
    terms = ["p"]
    if heat_h >= min_duty_h:
        terms.append("h")
    if cool_h >= min_duty_h:
        terms.append("c")

    def feat(v: dict[str, Any], t: str) -> float:
        if t == "p":
            return v["delta"] * v["dt_h"]
        return v["heat"] * v["dt_h"] if t == "h" else v["cool"] * v["dt_h"]

    ata = [[sum(feat(v, a) * feat(v, b) for v in ivs) for b in terms] for a in terms]
    atb = [sum(feat(v, a) * v["d_t"] for v in ivs) for a in terms]
    coef = _solve(ata, atb)
    if coef is None:
        return None
    k = dict(zip(terms, coef, strict=True))

    ss_res = sum((v["d_t"] - sum(k[t] * feat(v, t) for t in terms)) ** 2 for v in ivs)
    mean_d = sum(v["d_t"] for v in ivs) / len(ivs)
    ss_tot = sum((v["d_t"] - mean_d) ** 2 for v in ivs)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

    kp, kh, kc = k.get("p"), k.get("h"), k.get("c")
    if kp is None or not (THERMAL_K_PASSIVE_MIN <= kp <= THERMAL_K_PASSIVE_MAX):
        return None
    if kh is not None and not (THERMAL_K_ACTIVE_HEAT_MIN <= kh <= THERMAL_K_ACTIVE_HEAT_MAX):
        return None
    if kc is not None and not (THERMAL_K_ACTIVE_COOL_MIN <= kc <= THERMAL_K_ACTIVE_COOL_MAX):
        return None
    return {
        "k_passive": round(kp, 5),
        "k_active_heat": None if kh is None else round(kh, 4),
        "k_active_cool": None if kc is None else round(kc, 4),
        "n_intervals": len(ivs),
        "r_squared": round(r2, 4),
        "heat_hours": round(heat_h, 3),
        "cool_hours": round(cool_h, 3),
        "window_start": ivs[0]["t0"],
        "window_end": ivs[-1]["t1"],
    }
