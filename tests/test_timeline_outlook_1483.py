"""Issue #1483 part 2: the plan looks ahead as far as the forecast goes (at most a week); it promises 48 h.

Occupant view: the plan ("Next 24 hours", the chart's faded activity strips) used to end 48 h ahead whatever the
forecast said. It now runs on to the forecast's end as an outlook: each later day's own window period, the predicted
heat and AC runs, the wake and bedtime bands. The first 48 h are still the promise the latch keeps (steps as first
said, a close may only move earlier); the outlook is rebuilt from the newest forecast every cycle and never latched,
so a change to day 5's forecast never rewrites today's plan, and the later days are never stale.
"""

from __future__ import annotations

import dataclasses
import time as _time
from datetime import datetime, time, timedelta

from custom_components.climate_advisor.operation_timeline import (
    TIMELINE_HORIZON_HOURS,
    TIMELINE_OUTLOOK_MAX_HOURS,
    PlanSignature,
    StepKind,
    TimelineLatch,
    build_segments,
    build_timeline,
    material_changes,
    promise_end,
    timeline_end,
)
from tests.timeline_fixtures import DAY_TYPES, TZ, make_snapshot, sine_day

NOW = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)


def _week_hourly(days: int, day_type: str = "mild") -> list[tuple[datetime, float]]:
    hi, lo = DAY_TYPES[day_type]
    out: list[tuple[datetime, float]] = []
    for k in range(days):
        out += sine_day(NOW.date() + timedelta(days=k), hi, lo)[(1 if k else 0) :]
    return out


def _later_day(k: int, *, windows: bool, open_t=time(14, 0), close_t=time(17, 0), mode: str = "off") -> dict:
    """A later day's plan; the default 14:00-17:00 is when the fixture's mild afternoon passes the direction rule."""
    return {
        "date": NOW.date() + timedelta(days=k),
        "day_type": "mild",
        "hvac_mode": mode,
        "trend_direction": "stable",
        "windows_recommended": windows,
        "window_open_time": open_t if windows else None,
        "window_close_time": close_t if windows else None,
        "pre_condition": False,
        "expected_high": 70.0,
        "expected_low": 50.0,
    }


def _week_snap(days: int = 7, later: tuple = (), **kw):
    snap = make_snapshot("mild", now=NOW, **kw)
    snap.hourly = _week_hourly(days)
    snap.hourly_until = NOW + timedelta(hours=40)
    snap.plan_days = tuple(snap.plan_days) + tuple(later)
    snap.day_modes = {**snap.day_modes, **{d["date"]: d["hvac_mode"] for d in later}}
    return snap


class TestTimelineEnd:
    def test_a_short_forecast_keeps_the_48_h_plan(self):
        snap = make_snapshot("mild", now=NOW)
        snap.hourly = [p for p in snap.hourly if p[0] <= NOW + timedelta(hours=30)]
        assert timeline_end(snap) == NOW + timedelta(hours=TIMELINE_HORIZON_HOURS)

    def test_the_plan_runs_to_the_forecasts_end(self):
        snap = _week_snap(days=5)
        last = max(t for t, _v in snap.hourly)
        assert timeline_end(snap) == last > NOW + timedelta(hours=TIMELINE_HORIZON_HOURS)
        assert build_timeline(snap).horizon_end == last

    def test_never_past_a_week(self):
        snap = _week_snap(days=10)
        assert timeline_end(snap) == NOW + timedelta(hours=TIMELINE_OUTLOOK_MAX_HOURS)


class TestOutlookSteps:
    def test_a_later_days_window_period_is_in_the_plan(self):
        later = (_later_day(2, windows=False), _later_day(3, windows=False), _later_day(4, windows=True))
        tl = build_timeline(_week_snap(later=later))
        day4 = NOW.date() + timedelta(days=4)
        opens = [s for s in tl.steps if s.kind == StepKind.WINDOWS_OPEN and s.ts.date() == day4]
        closes = [s for s in tl.steps if s.kind == StepKind.WINDOWS_CLOSE and s.ts.date() == day4]
        assert opens and closes, [(s.kind, s.ts) for s in tl.steps if s.ts.date() == day4]
        assert opens[0].ts >= datetime.combine(day4, time(14, 0), tzinfo=TZ) and closes[-1].ts <= datetime.combine(
            day4, time(17, 0), tzinfo=TZ
        )

    def test_without_that_days_plan_there_is_no_window_period_planned_for_it(self):
        later = (_later_day(4, windows=False),)
        tl = build_timeline(_week_snap(later=later))
        day4 = NOW.date() + timedelta(days=4)
        assert not [c for c in tl.candidates if c.kind == "planned" and c.start.date() == day4]
        with_plan = build_timeline(_week_snap(later=(_later_day(4, windows=True),)))
        assert [c for c in with_plan.candidates if c.kind == "planned" and c.start.date() == day4]

    def test_every_outlook_day_has_its_wake_and_bedtime(self):
        tl = build_timeline(_week_snap(days=6))
        for k in range(2, 5):
            d = NOW.date() + timedelta(days=k)
            kinds = {s.kind for s in tl.steps if s.ts.date() == d}
            assert {StepKind.WAKE_BAND, StepKind.SLEEP_BAND} <= kinds, (d, kinds)

    def test_a_week_long_build_stays_cheap(self):
        snap = _week_snap(days=7)
        build_timeline(snap)  # warm
        t0 = _time.perf_counter()
        for _ in range(3):
            build_timeline(snap)
        ms = (_time.perf_counter() - t0) / 3 * 1000
        assert ms < 250, f"a week-long plan took {ms:.0f} ms"


class TestPromiseAndOutlook:
    def test_a_band_change_in_the_outlook_does_not_regenerate_the_promise(self):
        snap = _week_snap(later=(_later_day(5, windows=False, mode="off"),))
        old = PlanSignature.of(snap, build_segments(snap, timeline_end(snap)))
        snap2 = dataclasses.replace(snap)
        far = NOW.date() + timedelta(days=5)
        base = snap.band_at

        def shifted(ts, occ):
            b = base(ts, occ)
            return dataclasses.replace(b, floor=b.floor + 3, ceiling=b.ceiling + 3) if ts.date() == far and b else b

        snap2.band_at = shifted
        new = PlanSignature.of(snap2, build_segments(snap2, timeline_end(snap2)))
        assert material_changes(old, new, NOW) == []

    def test_a_band_change_inside_48_h_still_regenerates(self):
        snap = _week_snap()
        old = PlanSignature.of(snap, build_segments(snap, timeline_end(snap)))
        snap2 = dataclasses.replace(snap)
        near = NOW.date() + timedelta(days=1)
        base = snap.band_at

        def shifted(ts, occ):
            b = base(ts, occ)
            return dataclasses.replace(b, floor=b.floor + 3, ceiling=b.ceiling + 3) if ts.date() == near and b else b

        snap2.band_at = shifted
        new = PlanSignature.of(snap2, build_segments(snap2, timeline_end(snap2)))
        assert "band" in material_changes(old, new, NOW)

    def test_the_outlook_follows_the_newest_forecast_without_a_regeneration(self):
        """Day 4 gains a window period in a later cycle: the latch keeps its promise and shows day 4's new period."""
        latch = TimelineLatch()
        first = latch.update(_week_snap(later=(_later_day(4, windows=False),)))
        assert first.regenerated
        later_snap = _week_snap(later=(_later_day(4, windows=True),))
        later_snap.now = NOW + timedelta(minutes=30)
        out = latch.update(later_snap)
        assert not out.regenerated
        day4 = NOW.date() + timedelta(days=4)
        assert [s for s in out.timeline.steps if s.kind == StepKind.WINDOWS_OPEN and s.ts.date() == day4]
        # the promised part is the latched one, as first said
        cut = promise_end(first.timeline.generated_at)
        assert [s.id for s in first.timeline.steps if NOW + timedelta(minutes=30) <= s.ts < cut] == [
            s.id for s in out.timeline.steps if NOW + timedelta(minutes=30) <= s.ts < cut
        ]


def test_the_coordinator_hands_the_plan_every_later_day_and_the_outlook_forecast():
    from tests.test_debug_pane_words import _mild_day  # noqa: PLC0415
    from tests.test_v4_week_1483 import _setup  # noqa: PLC0415

    coord, scheduler, _model = _setup(days=7)
    now = scheduler.now()
    coord._current_classification = _mild_day()  # tomorrow's plan comes from the live classification
    with scheduler.installed():
        snap = coord._timeline_snapshot(_mild_day())
    dates = [d["date"] for d in snap.plan_days]
    assert dates == [now.date() + timedelta(days=k) for k in range(7)]  # today, tomorrow, then today + 2..6
    later = coord._later_day_plans(now)
    assert list(snap.plan_days[2:]) == later
    assert snap.hourly_until is not None and max(t for t, _v in snap.hourly) > snap.hourly_until + timedelta(hours=24)
    assert timeline_end(snap) > now + timedelta(hours=TIMELINE_HORIZON_HOURS)
