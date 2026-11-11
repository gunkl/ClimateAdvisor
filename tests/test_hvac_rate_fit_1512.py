"""Duty-balance fit (Issues #1512/#1516): synthetic houses with known rates, chart_log-style sampling."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.hvac_rate_fit import fit_duty_balance

_T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
_QUIET = {"running": False, "paused": False, "settling_fan": False, "settling_close": False, "settling_hvac": False}


def _solar(ts: str) -> bool:
    return 8 <= datetime.fromisoformat(ts).hour <= 19


def _q(v: float, res: float) -> float:
    return round(round(v / res) * res, 3)


def simulate(
    *,
    kp=-0.1,
    qh=0.0,
    qc=0.0,
    days=6,
    on_min=10,
    off_min=20,
    res=0.1,
    coast_min=0,
    coast_frac=0.0,
    season="heat",
    seed=3,
):
    """Integrate dT/dt = kp (T - To) + qh*uh + qc*uc by 1-min steps; emit rows at every edge + 30-min idle rows."""
    rnd = random.Random(seed)
    t_in = 68.0 if season == "heat" else 76.0
    rows, ts, last_act, last_row = [], _T0, None, None
    cycle = on_min + off_min
    total = days * 24 * 60
    for m in range(total):
        ts = _T0 + timedelta(minutes=m)
        base = 30.0 if season == "heat" else 92.0
        t_out = base + 8 * math.sin(2 * math.pi * (m / 1440.0 - 0.3)) + 6 * math.sin(2 * math.pi * m / (1440.0 * 2.7))
        phase = m % cycle
        on = phase < on_min
        since_off = phase - on_min
        coasting = (not on) and 0 <= since_off < coast_min
        act = ""
        if on:
            act = "heating" if season in ("heat",) else "cooling"
        if season == "mixed":
            act = "heating" if (m // 1440) % 2 == 0 else "cooling"
            act = act if on else ""
        # emit row at the start of the minute with state for the coming minute
        edge = act != last_act
        if edge or last_row is None or (ts - last_row).total_seconds() >= 1800:
            rows.append(
                {
                    "ts": ts.isoformat(),
                    "indoor": _q(t_in, res),
                    "outdoor": _q(t_out + rnd.uniform(-0.01, 0.01), res),
                    "hvac": act or "idle",
                    "windows_open": False,
                    "fan": False,
                }
            )
            last_row = ts
        last_act = act
        rate = kp * (t_in - t_out)
        mode = "heating" if (act == "heating") else ("cooling" if act == "cooling" else "")
        if mode == "heating":
            rate += qh
        elif mode == "cooling":
            rate += qc
        if coasting and qh:
            rate += qh * coast_frac
        t_in += rate / 60.0
    return rows


def quiet_states(rows, settle_min=0):
    out, last = [], None
    for r in rows:
        t = datetime.fromisoformat(r["ts"])
        active = r["hvac"] in ("heating", "cooling")
        s = dict(_QUIET)
        s["settling_hvac"] = bool(settle_min and last is not None and (t - last).total_seconds() < settle_min * 60)
        if active:
            last = t
        out.append(s)
    return out


def fit(rows, states=None, **kw):
    return fit_duty_balance(rows, states or quiet_states(rows), is_solar_hour=_solar, **kw)


def _err(got, want):
    return abs(got - want) / abs(want)


def test_recovers_heat_house_01f():
    f = fit(simulate(qh=8.0, res=0.1, days=10))
    assert f is not None
    assert _err(f["k_passive"], -0.1) < 0.15 and _err(f["k_active_heat"], 8.0) < 0.15
    assert f["k_active_cool"] is None and f["cool_hours"] == 0.0
    assert f["r_squared"] > 0.5 and f["window_start"] < f["window_end"]


def test_recovers_cool_house_01f():
    f = fit(simulate(kp=-0.08, qc=-9.0, season="cool", days=10))
    assert f is not None and f["k_active_heat"] is None
    assert _err(f["k_passive"], -0.08) < 0.15 and _err(f["k_active_cool"], -9.0) < 0.15


def test_recovers_mixed_house():
    f = fit(simulate(kp=-0.1, qh=8.0, qc=-9.0, season="mixed", days=12))
    assert f is not None
    assert _err(f["k_passive"], -0.1) < 0.15
    assert _err(f["k_active_heat"], 8.0) < 0.15 and _err(f["k_active_cool"], -9.0) < 0.15


def test_accuracy_table_at_10f_resolution(capsys):
    """Reports (not tightly bounds) the 1.0 F error; 1.0 F still has to give a bounded, valid answer."""
    lines = []
    for res in (0.1, 1.0):
        for days in (7, 14, 30):
            f = fit(simulate(qh=8.0, res=res, days=days))
            if f is None:
                lines.append(f"res={res} days={days}: None")
                continue
            lines.append(
                f"res={res} days={days}: kp={f['k_passive']:.3f} err={_err(f['k_passive'], -0.1):.0%} "
                f"qh={f['k_active_heat']:.2f} err={_err(f['k_active_heat'], 8.0):.0%} r2={f['r_squared']}"
            )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    f = fit(simulate(qh=8.0, res=1.0, days=30))
    assert f is not None and _err(f["k_active_heat"], 8.0) < 0.35


def test_heat_only_leaves_cool_none_and_cool_only_leaves_heat_none():
    h = fit(simulate(qh=8.0, days=8))
    c = fit(simulate(qc=-9.0, season="cool", days=8))
    assert h["k_active_cool"] is None and c["k_active_heat"] is None


def test_excluded_rows_do_not_count():
    rows = simulate(qh=8.0, days=8)
    base = fit(rows)
    n0 = base["n_intervals"]
    for key, val in (("running", True), ("paused", True), ("settling_fan", True), ("settling_close", True)):
        st = quiet_states(rows)
        for s in st:
            s[key] = val
        assert fit(rows, st) is None, key
    wo = [dict(r, windows_open=True) for r in rows]
    assert fit(wo) is None
    # a missing settling_hvac key is treated as False
    st = [{k: v for k, v in s.items() if k != "settling_hvac"} for s in quiet_states(rows)]
    assert fit(rows, st)["n_intervals"] == n0
    # all-day solar exclusion
    assert fit_duty_balance(rows, quiet_states(rows), is_solar_hour=lambda ts: True) is None


def test_gap_and_jump_filters():
    rows = simulate(qh=8.0, days=8)
    n0 = fit(rows)["n_intervals"]
    g = fit(rows, max_gap_min=12)
    assert g is None or g["n_intervals"] < n0
    jumped = [dict(r) for r in rows]
    for i in range(10, len(jumped), 7):
        jumped[i]["indoor"] += 6.0  # sensor swap spike: both adjacent steps exceed 3 F and are dropped
    f = fit(jumped)
    assert f is None or f["n_intervals"] < n0


def test_too_few_intervals_none():
    rows = simulate(qh=8.0, days=1)[:20]
    assert fit(rows) is None
    assert fit(simulate(qh=8.0, days=8), min_intervals=10**6) is None


def test_out_of_bounds_none():
    # 40 F/h of heating is outside THERMAL_K_ACTIVE_HEAT_MAX
    rows = simulate(kp=-0.1, qh=40.0, days=6, on_min=3, off_min=27)
    assert fit(rows) is None


def test_term_below_min_duty_dropped():
    rows = simulate(qh=8.0, days=8)
    f = fit(rows, min_duty_h=10**6)
    assert f is None or (f["k_active_heat"] is None and f["k_active_cool"] is None)


def test_post_off_coast_bias(capsys):
    """Furnace adds 50% of its rate for 10 min after off (10 on / 20 off cycles). With the settling_hvac mask the coast
    interval is excluded; without the mask it biases the passive/active split. Measured and bounded."""
    clean = fit(simulate(qh=8.0, days=14, res=0.1))
    coast = simulate(qh=8.0, days=14, res=0.1, coast_min=10, coast_frac=0.5)
    unmasked = fit(coast)
    masked = fit(coast, quiet_states(coast, settle_min=30))
    assert unmasked and masked and clean
    b_un = (unmasked["k_active_heat"] - 8.0) / 8.0
    b_ma = (masked["k_active_heat"] - 8.0) / 8.0
    with capsys.disabled():
        print(
            f"\ncoast bias q_h: clean={(clean['k_active_heat'] - 8) / 8:+.1%} unmasked={b_un:+.1%} masked={b_ma:+.1%}"
            f" | k_passive err unmasked={_err(unmasked['k_passive'], -0.1):.0%}"
            f" masked={_err(masked['k_passive'], -0.1):.0%}"
        )
    # Measured: unmasked, coast heat lands in the idle interval and is read as a weaker leak, so q_h reads ~24% low;
    # with the settling_hvac mask the bias is ~3%.
    assert -0.35 < b_un < 0.0
    assert abs(b_ma) < 0.10
    assert math.isfinite(masked["k_passive"])


def _as_real_chart_log(rows):
    """Real chart_log shape: the row written at every hvac action change is an event row with ``outdoor: None``."""
    out, prev = [], None
    for r in rows:
        r = dict(r)
        if prev is not None and r["hvac"] != prev:
            r["outdoor"] = None
            r["event"] = "hvac_action_change"
        prev = r["hvac"]
        out.append(r)
    return out


def test_event_rows_without_outdoor_still_identify_duty():
    """Every run edge is an event row with outdoor None; dropping those intervals dropped all heating duty."""
    rows = _as_real_chart_log(simulate(qh=8.0, res=0.1, days=20, on_min=45, off_min=135))
    assert sum(r["outdoor"] is None for r in rows) > 50
    f = fit(rows)
    assert f is not None and f["heat_hours"] > 1.0
    assert _err(f["k_passive"], -0.1) < 0.2 and _err(f["k_active_heat"], 8.0) < 0.2


def test_event_rows_without_outdoor_cool_house():
    f = fit(_as_real_chart_log(simulate(kp=-0.08, qc=-9.0, season="cool", days=20, on_min=45, off_min=135)))
    assert f is not None and f["cool_hours"] > 1.0
    assert _err(f["k_active_cool"], -9.0) < 0.2


def test_outdoor_fill_does_not_cross_more_than_an_hour():
    """A row whose nearest outdoor reading is over 60 min away stays unfilled and its intervals are dropped."""
    from custom_components.climate_advisor.hvac_rate_fit import _filled

    t = lambda m: (_T0 + timedelta(minutes=m)).isoformat()  # noqa: E731
    rows = [{"ts": t(0), "outdoor": 40.0}, {"ts": t(30), "outdoor": None}, {"ts": t(61), "outdoor": None}]
    assert _filled(rows, "outdoor") == [40.0, 40.0, None]
    rows = [{"ts": t(0), "outdoor": None}, {"ts": t(20), "outdoor": 41.0}, {"ts": t(100), "outdoor": None}]
    assert _filled(rows, "outdoor") == [41.0, 41.0, None]  # back-filled from the next row; 80 min is too far forward


def test_failed_fit_explains_why():
    why: list[str] = []
    assert fit(simulate(qh=8.0, days=1)[:20], reason=why) is None
    assert why and "too few" in why[0]
