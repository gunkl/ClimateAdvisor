"""Issue #1727 L0: the build is cheaper but its output is byte-identical.

The reference below is the simulator as it was before the per-build grid and the base-prefix reuse: every step of
every run recomputed from scratch. ``build_timeline`` must give the same plan with the new ``_Simulator.run``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.operation_timeline import (
    SIM_STEP_MINUTES,
    THERMAL_SWING_DEFAULT_F,
    Sim,
    SimStep,
    _interp,
    _sleep_loop_edges,
    build_timeline,
    fan_would_run,
    ode_step,
    open_hour_rate,
    solar_factor,
)
from custom_components.climate_advisor.prediction_engines import HouseStepper
from tests.test_operation_timeline import ZONE1_HOUSE
from tests.test_timeline_outlook_1483 import NOW, _later_day, _week_hourly, _week_snap
from tests.timeline_fixtures import DAY_TYPES


def _reference_run(self, spans, until=None):
    """The pre-#1727 ``_Simulator.run``, verbatim: nothing precomputed, nothing reused (plus #1734's ``until``: stop
    at the last step that ends at or before it)."""
    if self.start_t is None or not self.pts or not self.index.segments:
        return None
    r, snap = self.rates, self.snap
    last_fc = self.pts[-1][0] + 1800
    dt_h = SIM_STEP_MINUTES / 60.0
    t = float(self.start_t)
    stepper = snap.house_stepper
    mass = t
    ts = snap.now
    steps = []
    hvac_total = 0.0
    disc_total = 0.0
    span_pts = [(a.timestamp(), b.timestamp()) for a, b in spans]
    while True:
        ts_next = ts + timedelta(minutes=SIM_STEP_MINUTES)
        if ts_next > self.end or ts_next.timestamp() > last_fc or (until is not None and ts_next > until):
            break
        out = _interp(self.pts, ts_next.timestamp())
        seg = self.index.at(ts)
        mid = ts.timestamp() + SIM_STEP_MINUTES * 30
        open_ = any(a <= mid < b for a, b in span_pts)
        if stepper is not None:
            fan_on = open_ and fan_would_run(snap, seg, out, t)
            local = ts_next.astimezone(snap.now.tzinfo) if snap.now.tzinfo is not None else ts_next
            hour = local.hour + local.minute / 60.0
            free, mass = stepper.step(t, mass, out, 1.0 if open_ else 0.0, 1.0 if fan_on else 0.0, hour)
            heat_rate = stepper.heat_gain or None
            cool_rate = stepper.cool_gain or None
        else:
            k = open_hour_rate(snap, r, seg, out, t) if open_ else r.k_passive
            q_sun = (r.k_solar or 0.0) * solar_factor(ts.hour, r.phase_h)
            free = ode_step(t, out, k, q_sun, dt_h)
            heat_rate = abs(r.k_active_heat) if r.k_active_heat else None
            cool_rate = abs(r.k_active_cool) if r.k_active_cool else None
        nxt = free
        hvac_min = 0.0
        hvac_mode = None
        hold_floor, hold_ceiling = _sleep_loop_edges(snap, seg)
        if not open_:
            current = ts < snap.now + timedelta(hours=1)
            can_heat = snap.can_heat or (current and "heat" in snap.released_now)
            can_cool = snap.can_cool or (current and "cool" in snap.released_now)
            if free < hold_floor and can_heat and heat_rate:
                gain = min(hold_floor - free, heat_rate * dt_h)
                nxt, hvac_min, hvac_mode = free + gain, gain / heat_rate * 60.0, "heat"
            elif free > hold_ceiling and can_cool and cool_rate:
                gain = min(free - hold_ceiling, cool_rate * dt_h)
                nxt, hvac_min, hvac_mode = free - gain, gain / cool_rate * 60.0, "cool"
        sw_h = r.swing_heat if r.swing_heat is not None else THERMAL_SWING_DEFAULT_F
        sw_c = r.swing_cool if r.swing_cool is not None else THERMAL_SWING_DEFAULT_F
        disc = max(0.0, hold_floor - sw_h - nxt) + max(0.0, nxt - hold_ceiling - sw_c)
        disc_fh = disc * dt_h
        steps.append(SimStep(ts_next, nxt, free, out, seg, open_, hvac_min, hvac_mode, disc_fh))
        hvac_total += hvac_min
        disc_total += disc_fh
        t, ts = nxt, ts_next
    return Sim(steps, hvac_total, disc_total)


def _stepper() -> HouseStepper:
    return HouseStepper(
        step=lambda t, m, o, w, f, hr: p4.free_step(ZONE1_HOUSE, t, m, out=o, win=w, fan=f, hour=hr),
        heat_gain=ZONE1_HOUSE[7],
        cool_gain=ZONE1_HOUSE[8],
    )


def _snap(day_type: str, days: int, v4: bool, now_shift_h: int = 0):
    later = tuple(_later_day(k, windows=True) for k in range(2, days))
    snap = _week_snap(days=days, later=later)
    snap.hourly = _week_hourly(days, day_type)
    if now_shift_h:
        shifted = NOW + timedelta(hours=now_shift_h)
        snap = replace(snap, now=shifted)
    return replace(snap, house_stepper=_stepper()) if v4 else snap


@pytest.mark.parametrize("day_type", list(DAY_TYPES))
@pytest.mark.parametrize("days", [2, 4, 7, 8])
@pytest.mark.parametrize("v4", [False, True], ids=["v3", "v4"])
def test_the_plan_is_identical_to_the_per_step_reference(monkeypatch, day_type, days, v4):
    for shift in (0, 5, 13):
        snap = _snap(day_type, days, v4, shift)
        new = repr(build_timeline(snap))
        monkeypatch.setattr(ot._Simulator, "run", _reference_run)
        ref = repr(build_timeline(snap))
        monkeypatch.undo()
        assert new == ref, (day_type, days, v4, shift)


def test_the_identity_test_catches_a_prefix_reused_one_step_too_far(monkeypatch):
    """Revert proof: resuming from the base run one step past the first span must change some plan."""
    orig = ot.bisect.bisect_left
    calls = {"n": 0}

    def too_far(a, x, *args, **kw):
        # only the prefix lookup searches a list of floats against a float with no extra args
        calls["n"] += 1
        return orig(a, x, *args, **kw) + 1

    differing = 0
    for day_type in DAY_TYPES:
        for v4 in (False, True):
            snap = _snap(day_type, 7, v4)
            good = repr(build_timeline(snap))
            monkeypatch.setattr(ot.bisect, "bisect_left", too_far)
            bad = repr(build_timeline(snap))
            monkeypatch.undo()
            differing += good != bad
    assert calls["n"] > 0
    assert differing > 0


def _steps_for(snap) -> int:
    holder = []
    real_init = ot._Simulator.__init__

    def spy(self, *a, **k):
        real_init(self, *a, **k)
        holder.append(self)

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(ot._Simulator, "__init__", spy)
        build_timeline(snap)
    finally:
        mp.undo()
    assert len(holder) == 1
    return holder[0].steps_simulated


def test_a_hot_v4_week_build_stays_within_its_step_budget():
    """Deterministic work guard (replaces a wall-time test): simulated steps per build, prefix reuse excluded.

    A 7-day hot v4 build with a window period every day measures 4,525 steps with the prefix reuse and the #1734
    judged horizon (every candidate stops at max(now + 72 h, close + 24 h)); 8,506 before #1734 (judged to the plan's
    end) and 12,800 re-simulating every candidate from the start. A regression to either (or a per-run grid rebuild)
    pushes this past the bound."""
    snap = _snap("hot", 7, True)
    n = _steps_for(snap)
    assert 0 < n <= STEP_BUDGET, n


STEP_BUDGET = 5000


def test_the_nearest_step_by_bisection_is_the_nearest_step_by_min():
    """``predicted_at`` (the band edge a segment arms) finds the nearest step by bisection; the pre-#1727 code took
    ``min`` over every step, which on a tie keeps the earlier one. Probes before, after, between, on and exactly
    halfway between steps."""
    steps = [
        SimStep(NOW + timedelta(minutes=SIM_STEP_MINUTES * (k + 1)), 60.0 + k, 60.0, 50.0, None, False, 0.0, None, 0.0)
        for k in range(12)
    ]
    times = [s.ts for s in steps]
    for minutes in range(-20, SIM_STEP_MINUTES * 14, 1):
        ts = NOW + timedelta(minutes=minutes, seconds=30 * (minutes % 2))
        ref = min(steps, key=lambda s: abs((s.ts - ts).total_seconds())).temp
        assert ot._nearest_step_temp(steps, times, ts) == ref, ts
    assert ot._nearest_step_temp([], [], NOW) is None


def _on_night(day_type: str, v4: bool, now: datetime):
    """The week snapshot moved, by calendar days, so that ``now`` falls on a DST night (wall-clock arithmetic)."""
    snap = _snap(day_type, 5, v4)
    days = timedelta(days=(now.date() - NOW.date()).days)
    plan_days = tuple({**d, "date": d["date"] + days} for d in snap.plan_days)
    return replace(
        snap,
        now=now,
        hourly=[(t + days, v) for t, v in snap.hourly],
        hourly_until=snap.hourly_until + days,
        plan_days=plan_days,
        day_modes={d["date"]: d["hvac_mode"] for d in plan_days},
    )


@pytest.mark.parametrize("v4", [False, True], ids=["v3", "v4"])
@pytest.mark.parametrize(
    "now",
    [datetime(2026, 3, 7, 21, 45, tzinfo=NOW.tzinfo), datetime(2026, 10, 31, 21, 45, tzinfo=NOW.tzinfo)],
    ids=["spring-forward", "fall-back"],
)
def test_prefix_reuse_is_exact_across_dst_nights(now, v4):
    """Issue #1734: the grid steps by wall-clock arithmetic, so on a spring-forward night its mid-step epochs are not
    sorted (02:00-02:45 repeats 03:00-03:45) and a bisect for the first span resumed at the wrong step."""
    snap = _on_night("hot", v4, now)
    end = ot.timeline_end(snap)
    sim = ot._Simulator(snap, ot.model_rates(snap.thermal_model), ot._SegmentIndex(ot.build_segments(snap, end)), end)
    assert sim.run([]) is not None
    night = now.replace(hour=0, minute=0) + timedelta(days=1)
    checked = 0
    for start_min in range(60, 241, 15):  # span starts 01:00-04:00 local
        a = night.replace(hour=0, minute=0) + timedelta(minutes=start_min)
        for hours in (1, 3):
            for until in (None, a + timedelta(hours=8)):
                spans = [(a, a + timedelta(hours=hours))]
                got, ref = sim.run(spans, until), _reference_run(sim, spans, until)
                assert got is not None and ref is not None
                assert got.steps == ref.steps, (a, hours, until)
                assert (got.hvac_minutes, got.discomfort_fh) == (ref.hvac_minutes, ref.discomfort_fh)
                checked += 1
    assert checked
