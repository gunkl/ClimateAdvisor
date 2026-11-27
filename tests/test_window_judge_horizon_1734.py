"""Issue #1734: a window period is judged over a fixed horizon, not to the end of the forecast.

Occupant view: "open the windows 2-5 pm" used to depend on how the house would be doing six days later, because every
period was scored by simulating the plan with and without it to the forecast's end (up to a week), and the far days
are daily highs and lows nobody scores, run through a model trained on rollouts of 12 h or less. A period is now
judged to ``max(now + 72 h, its close + 24 h)``; the plan itself still runs to the forecast's end.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.operation_timeline import (
    WINDOW_JUDGE_AFTER_CLOSE_HOURS,
    WINDOW_JUDGE_MIN_HOURS,
    build_timeline,
    timeline_end,
    window_judge_end,
)
from custom_components.climate_advisor.prediction_engines import HouseStepper
from tests.test_operation_timeline import ZONE1_HOUSE
from tests.test_timeline_outlook_1483 import NOW, _later_day, _week_hourly, _week_snap
from tests.timeline_fixtures import DAY_TYPES

# Zone 1's v4 fit with a cooling gain in line with its heating gain (7.1 * 6.13 / 5.55 = 7.84 F/h). ZONE1_HOUSE itself
# has q_c at its 0.5 lower bound (the pre-#1525 failure mode), so a house that cools hardly at all: other tests pin it.
REALISTIC_HOUSE = ZONE1_HOUSE[:8] + (ZONE1_HOUSE[7] * 6.13 / 5.55,)
HOUSES = {"v3": None, "v4_realistic": REALISTIC_HOUSE, "v4_zone1": ZONE1_HOUSE}


def _stepper(house) -> HouseStepper:
    return HouseStepper(
        step=lambda t, m, o, w, f, hr: p4.free_step(house, t, m, out=o, win=w, fan=f, hour=hr),
        heat_gain=house[7],
        cool_gain=house[8],
    )


def _snap(day_type: str, days: int, house_id: str, shift_h: int = 0):
    later = tuple(_later_day(k, windows=True) for k in range(2, days))
    snap = _week_snap(days=days, later=later)
    snap.hourly = _week_hourly(days, day_type)
    if shift_h:
        snap = replace(snap, now=NOW + timedelta(hours=shift_h))
    house = HOUSES[house_id]
    return snap if house is None else replace(snap, house_stepper=_stepper(house))


def _perturbed(snap, after):
    """The forecast after ``after`` turned upside down (a hot week becomes a cold one); nothing before it changes."""
    return replace(snap, hourly=[(t, 130.0 - v if t > after else v) for t, v in snap.hourly])


def _decisions(tl, before=None):
    return {
        (c.kind, c.start): (c.end, c.accepted, c.reason, c.hvac_saved_min, c.discomfort_saved_fh, c.end_reason)
        for c in tl.candidates
        if before is None or c.start < before
    }


@pytest.fixture
def legacy_judging(monkeypatch):
    """The pre-#1734 rule: every period judged to the plan's end (reference for the contrast tests)."""
    monkeypatch.setattr(ot, "window_judge_end", lambda _snap, _close, end: end)


def test_the_judged_end_is_the_later_of_72_h_and_a_day_after_the_close_never_past_the_plan_end():
    snap = _snap("hot", 8, "v4_realistic")
    end = timeline_end(snap)
    h = timedelta(hours=1)
    assert (WINDOW_JUDGE_MIN_HOURS, WINDOW_JUDGE_AFTER_CLOSE_HOURS) == (72, 24)
    assert window_judge_end(snap, snap.now + 10 * h, end) == snap.now + 72 * h
    assert window_judge_end(snap, snap.now + 48 * h, end) == snap.now + 72 * h  # 48 + 24 = 72
    assert window_judge_end(snap, snap.now + 70 * h, end) == snap.now + 94 * h
    assert window_judge_end(snap, snap.now + 160 * h, end) == end
    short = snap.now + 60 * h
    assert window_judge_end(snap, snap.now + 10 * h, short) == short


@pytest.mark.parametrize("house_id", ["v3", "v4_realistic", "v4_zone1"])
@pytest.mark.parametrize("day_type", ["hot", "warm", "mild"])
def test_a_periods_decision_ignores_the_forecast_after_its_judged_end(house_id, day_type):
    """Turn days 4-7 upside down: every period closing within 48 h (judged to 72 h) is decided exactly as before."""
    snap = _snap(day_type, 8, house_id)
    early = snap.now + timedelta(hours=48)
    far = snap.now + timedelta(hours=96)
    base = _decisions(build_timeline(snap), early)
    assert base, "the fixture has no early window period to judge"
    assert _decisions(build_timeline(_perturbed(snap, far)), early) == base


def test_the_far_forecast_does_move_the_decision_under_the_old_whole_horizon_rule(legacy_judging):
    """Contrast (revert proof of the test above): judged to the plan's end, the same far days change the figures."""
    snap = _snap("hot", 8, "v4_realistic")
    early = snap.now + timedelta(hours=48)
    base = _decisions(build_timeline(snap), early)
    moved = _decisions(build_timeline(_perturbed(snap, snap.now + timedelta(hours=96))), early)
    assert moved != base


def _spy_runs(monkeypatch):
    calls: list[tuple[list, object]] = []
    real = ot._Simulator.run

    def spy(self, spans, until=None):
        calls.append((list(spans), until))
        return real(self, spans, until)

    monkeypatch.setattr(ot._Simulator, "run", spy)
    return calls


def test_a_period_closing_late_is_judged_to_a_day_after_its_close_not_to_72_h(monkeypatch):
    snap = _snap("warm", 8, "v4_realistic", shift_h=13)  # now 21:00: day 3's period closes 66.8 h on
    calls = _spy_runs(monkeypatch)
    tl = build_timeline(snap)
    late = [c for c in tl.candidates if c.hvac_saved_min is not None and c.end - snap.now > timedelta(hours=60)]
    late = [c for c in late if c.end - snap.now < timedelta(hours=72)]
    assert late, [(c.kind, c.start, c.end) for c in tl.candidates]
    for c in late:
        trials = [u for spans, u in calls if spans and spans[-1] == (c.start, c.end)]
        assert trials, c
        # the period closes before now + 72 h, so a day after its close is now + 91 h or so: past the 72 h floor
        assert c.end + timedelta(hours=WINDOW_JUDGE_AFTER_CLOSE_HOURS) in trials, (c, trials)
        assert snap.now + timedelta(hours=WINDOW_JUDGE_MIN_HOURS) not in trials, (c, trials)
        assert c.end + timedelta(hours=24) > snap.now + timedelta(hours=72)


def test_an_early_period_is_judged_to_72_h(monkeypatch):
    snap = _snap("hot", 8, "v4_realistic")
    calls = _spy_runs(monkeypatch)
    tl = build_timeline(snap)
    early = [c for c in tl.candidates if c.hvac_saved_min is not None and c.end <= snap.now + timedelta(hours=48)]
    assert early
    for c in early:
        trials = {u for spans, u in calls if spans and spans[-1] == (c.start, c.end)}
        assert snap.now + timedelta(hours=72) in trials, (c, trials)


def test_a_late_periods_decision_ignores_the_forecast_after_a_day_past_its_close():
    snap = _snap("warm", 8, "v4_realistic", shift_h=13)
    tl = build_timeline(snap)
    late = [c for c in tl.candidates if timedelta(hours=60) < c.end - snap.now < timedelta(hours=72)]
    assert late
    after = max(c.end for c in late) + timedelta(hours=26)
    cut = max(c.start for c in late) + timedelta(minutes=1)
    assert _decisions(build_timeline(_perturbed(snap, after)), cut) == _decisions(tl, cut)


@pytest.mark.parametrize("house_id", ["v3", "v4_realistic"])
def test_the_plan_still_runs_to_the_forecasts_end(house_id):
    snap = _snap("hot", 8, house_id)
    tl = build_timeline(snap)
    end = timeline_end(snap)
    assert end - snap.now > timedelta(hours=WINDOW_JUDGE_MIN_HOURS + 48)
    assert tl.horizon_end == end
    assert any(s.ts > snap.now + timedelta(hours=150) for s in tl.steps)
    assert any(p.ts > snap.now + timedelta(hours=150) for p in tl.path)
    assert any(ts > snap.now + timedelta(hours=150) for ts, _m in tl.hvac_hourly)


def test_a_build_with_no_window_period_is_unchanged_by_the_judged_horizon(monkeypatch):
    """No candidate, no judging: the plan equals the whole-horizon one in every field."""
    snap = replace(_week_snap(days=8), windows_recommended=False, window_open_time=None, window_close_time=None)
    snap.hourly = _week_hourly(8, "cold")
    new = build_timeline(snap)
    assert not [c for c in new.candidates if c.hvac_saved_min is not None]
    monkeypatch.setattr(ot, "window_judge_end", lambda _snap, _close, end: end)
    assert repr(build_timeline(snap)) == repr(new)


@pytest.mark.parametrize("day_type", list(DAY_TYPES))
@pytest.mark.parametrize("days", [4, 7, 8])
@pytest.mark.parametrize("shift_h", [0, 5, 13])
def test_a_v3_build_decides_every_period_as_the_whole_horizon_rule_did(monkeypatch, day_type, days, shift_h):
    snap = _snap(day_type, days, "v3", shift_h)

    def decided(tl):
        return {k: (v[0], v[1], v[2], v[5]) for k, v in _decisions(tl).items()}

    new = decided(build_timeline(snap))
    monkeypatch.setattr(ot, "window_judge_end", lambda _snap, _close, end: end)
    assert decided(build_timeline(snap)) == new


def _built_sim(snap):
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
    return holder[0]


@pytest.mark.parametrize("house_id", ["v3", "v4_realistic"])
def test_a_run_stopped_at_until_is_the_prefix_of_the_whole_run(house_id):
    """The base-prefix reuse stays exact when a run stops early: before, inside and after the first span, and with no
    span at all, the steps and running totals equal the whole run's up to ``until``."""
    snap = _snap("hot", 8, house_id)
    sim = _built_sim(snap)
    h = timedelta(hours=1)
    until = snap.now + 30 * h
    span_sets = [
        [],
        [(snap.now + 2 * h, snap.now + 5 * h)],
        [(snap.now + 28 * h, snap.now + 33 * h)],  # straddles until
        [(snap.now + 40 * h, snap.now + 44 * h)],  # first span after until: the run is the base's prefix
        [(snap.now + 3 * h, snap.now + 6 * h), (snap.now + 50 * h, snap.now + 52 * h)],
    ]
    for spans in span_sets:
        whole = sim.run(spans)
        cut = sim.run(spans, until=until)
        n = len(cut.steps)
        assert 0 < n < len(whole.steps)
        assert cut.steps[-1].ts <= until < whole.steps[n].ts
        assert cut.steps == whole.steps[:n], spans
        assert cut.hvac_minutes == pytest.approx(sum(s.hvac_min for s in cut.steps))
        assert cut.discomfort_fh == pytest.approx(sum(s.discomfort_fh for s in cut.steps))


def test_a_stopped_run_never_becomes_the_base_run():
    snap = _snap("hot", 7, "v4_realistic")
    sim = ot._Simulator(
        snap,
        ot.model_rates(snap.thermal_model),
        ot._SegmentIndex(ot.build_segments(snap, timeline_end(snap))),
        timeline_end(snap),
    )
    short = sim.run([], until=snap.now + timedelta(hours=10))
    whole = sim.run([])
    assert len(short.steps) == 40 < len(whole.steps)
    assert sim.run([], until=snap.now + timedelta(hours=10)).steps == short.steps  # reuse of the whole base
    assert sim.run([]).steps == whole.steps
