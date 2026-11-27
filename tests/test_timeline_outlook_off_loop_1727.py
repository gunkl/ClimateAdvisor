"""Issue #1727 phase 3: the plan's outlook is built off the event loop.

Occupant view: nothing about the first 48 hours changes. The plan list, the chart strips and the briefing past about
two and a half days may lag a few cycles (the outlook is rebuilt in a thread, not on every cycle) and are never wrong
at the join: no "close the windows" step appears where the loop-side plan merely ran out, and the predicted line has no
step in it where the two builds meet.

Layout: the loop builds to now + 60 h (``TimelineSnapshot.end_cap``); the full plan is built in an executor thread and
laid over it from a quiet whole hour in [now + 54 h, now + 60 h] (``splice_outlook``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import prediction_engines as pe
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.operation_timeline import (
    TIMELINE_LOOP_CAP_HOURS,
    PlanSignature,
    StepKind,
    TimelineLatch,
    build_segments,
    build_timeline,
    find_seam,
    promise_end,
    splice_outlook,
    timeline_end,
)
from tests.test_operation_timeline import ZONE1_HOUSE
from tests.test_operation_timeline_wiring import _build, _hourly
from tests.test_timeline_outlook_1483 import NOW, _later_day, _week_hourly, _week_snap
from tests.test_timeline_speedup_1727 import _stepper
from tests.timeline_fixtures import DAY_TYPES

# Window periods of the later days: the default afternoon, a long one that crosses the loop plan's end whatever the
# clock, a morning-to-evening one and an evening one (planned periods are clipped at the loop plan's end).
WINDOWS = [(time(14), time(17)), (time(10), time(23)), (time(8), time(20)), (time(18), time(23, 30))]
SHIFTS = [0, 1, 2, 5, 13, 20]  # hours after the fixture's 08:00: where the loop plan's end falls in the day


def _snap(day_type: str, days: int, v4: bool, shift: int, window: tuple[time, time], windows: bool = True):
    later = tuple(_later_day(k, windows=windows, open_t=window[0], close_t=window[1]) for k in range(2, days))
    snap = _week_snap(days=days, later=later)
    snap.hourly = _week_hourly(days, day_type)
    if shift:
        snap = replace(snap, now=NOW + timedelta(hours=shift))
    return replace(snap, house_stepper=_stepper()) if v4 else snap


def _capped(snap):
    return replace(snap, end_cap=snap.now + timedelta(hours=TIMELINE_LOOP_CAP_HOURS))


def _no_figures(step):
    """The step with the window-benefit figure left out: ``hvac_saved_min`` is the equipment minutes a window period
    saves over the plan's horizon (60 h capped, the forecast's length uncapped, as it already varied with it). It is
    rendered nowhere (no sentence uses it), and the periods accepted do not depend on it in the fixtures."""
    facts = {k: v for k, v in step.meta.get("facts", {}).items() if k != "hvac_saved_min"}
    return replace(step, meta={**step.meta, "facts": facts})


def _steps(tl, until=None):
    return [repr(_no_figures(s)) for s in tl.steps if until is None or s.ts < until]


def _accepted(tl, until=None):
    return [
        (c.kind, c.start, c.end, c.end_reason)
        for c in tl.candidates
        if c.accepted and (until is None or c.start < until)
    ]


GRID = [
    (day_type, days, v4, shift, window)
    for day_type in DAY_TYPES
    for days in (6, 8)
    for v4 in (False, True)
    for shift in (0, 13)
    for window in WINDOWS[:2]
]
GRID_IDS = [f"{d}-{n}d-{'v4' if v4 else 'v3'}-+{s}h-{w[0].hour}to{w[1].hour}" for d, n, v4, s, w in GRID]


# --------------------------------------------------------------------------------------------------------------------
# 1. The cap changes nothing the occupant is promised
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("day_type", "days", "v4", "shift", "window"), GRID, ids=GRID_IDS)
def test_the_first_48_hours_are_the_same_with_and_without_the_cap(day_type, days, v4, shift, window):
    snap = _snap(day_type, days, v4, shift, window)
    promise = promise_end(snap.now)
    full, loop = build_timeline(snap), build_timeline(_capped(snap))
    assert loop.horizon_end == snap.now + timedelta(hours=TIMELINE_LOOP_CAP_HOURS) < full.horizon_end
    assert _steps(loop, promise) == _steps(full, promise)
    assert _accepted(loop, promise) == _accepted(full, promise)
    # the predicted line up to the seam region is the same walk
    edge = snap.now + timedelta(hours=54)
    assert [p for p in loop.path if p.ts <= edge] == [p for p in full.path if p.ts <= edge]
    # and the plan's signature (what decides a regeneration) is the same
    for s in (snap, _capped(snap)):
        assert PlanSignature.of(s, build_segments(s, timeline_end(s))) == PlanSignature.of(
            snap, build_segments(snap, timeline_end(snap))
        )


@pytest.mark.parametrize("day_type", list(DAY_TYPES))
@pytest.mark.parametrize("v4", [False, True], ids=["v3", "v4"])
def test_the_latched_promise_is_the_same_over_a_day_of_cycles(day_type, v4):
    """Latch both ways through 12 half-hour cycles, an occupancy force and a new forecast shape: the same
    regenerations, the same generation and the same latched steps before the promise's end."""
    base = _snap(day_type, 7, v4, 0, WINDOWS[1])
    capped_latch, full_latch = TimelineLatch(), TimelineLatch()
    for k in range(14):
        snap = replace(base, now=base.now + timedelta(minutes=30 * k))
        if k == 9:
            snap = replace(snap, indoor=snap.indoor + 3.0, indoor_primary=(snap.indoor_primary or snap.indoor) + 3.0)
        force = "occupancy" if k == 6 else None
        a = capped_latch.update(_capped(snap), force=force)
        b = full_latch.update(snap, force=force)
        assert (a.regenerated, a.reasons, a.timeline.generation) == (b.regenerated, b.reasons, b.timeline.generation), k
        cut = promise_end(a.timeline.generated_at)
        assert _steps(a.timeline, cut) == _steps(b.timeline, cut), k


def test_a_forecast_that_ends_inside_the_cap_is_planned_whole():
    snap = _week_snap(days=2)
    snap.hourly = [p for p in snap.hourly if p[0] <= NOW + timedelta(hours=50)]
    assert timeline_end(_capped(snap)) == timeline_end(snap)
    assert repr(build_timeline(_capped(snap))) == repr(build_timeline(snap))


def test_the_cap_never_cuts_into_the_48_hour_promise():
    snap = _week_snap(days=7)
    low = replace(snap, end_cap=snap.now + timedelta(hours=10))
    assert timeline_end(low) == promise_end(snap.now)


def test_the_capped_build_is_cheaper_to_simulate():
    """The point of the cap: the loop simulates fewer steps (the cost is about quadratic in the horizon)."""
    snap = _snap("hot", 8, True, 0, WINDOWS[0])
    counts = []
    for s in (snap, _capped(snap)):
        built = {}
        orig = ot._Simulator.__init__

        def init(self, *a, _orig=orig, _built=built, **kw):
            _orig(self, *a, **kw)
            _built["sim"] = self

        try:
            ot._Simulator.__init__ = init
            build_timeline(s)
        finally:
            ot._Simulator.__init__ = orig
        counts.append(built["sim"].steps_simulated)
    assert counts[1] * 2 < counts[0], counts


# --------------------------------------------------------------------------------------------------------------------
# 2. The seam
# --------------------------------------------------------------------------------------------------------------------


def _both(snap):
    full, loop = build_timeline(snap), build_timeline(_capped(snap))
    spliced, seam = splice_outlook(snap.now, loop, full)
    return full, loop, spliced, seam


@pytest.mark.parametrize(("day_type", "days", "v4", "shift", "window"), GRID, ids=GRID_IDS)
def test_a_fresh_outlook_spliced_in_is_the_uncapped_plan(day_type, days, v4, shift, window):
    """The surface equals the full uncapped build when the outlook is fresh. Where it legitimately differs: the
    ``hvac_saved_min`` / ``discomfort_saved_fh`` figures of window candidates before the seam (measured over 60 h, not
    the forecast's length; rendered nowhere) and the last digits of ``hvac_minutes``."""
    snap = _snap(day_type, days, v4, shift, window)
    full, _loop, spliced, seam = _both(snap)
    assert seam is not None
    assert _steps(spliced) == _steps(full)
    assert [(c.kind, c.start, c.end, c.accepted, c.end_reason) for c in spliced.candidates] == [
        (c.kind, c.start, c.end, c.accepted, c.end_reason) for c in full.candidates
    ]
    assert [r.to_dict() for r in spliced.runs] == [r.to_dict() for r in full.runs]
    assert spliced.band == full.band
    assert spliced.path == full.path
    assert spliced.summary == full.summary and spliced.summary_msgs == full.summary_msgs
    assert spliced.horizon_end == full.horizon_end
    assert spliced.days == full.days and spliced.grade == full.grade and spliced.path_source == full.path_source
    assert [h for h, _m in spliced.hvac_hourly] == [h for h, _m in full.hvac_hourly]
    assert (spliced.hvac_minutes or 0.0) == pytest.approx(full.hvac_minutes or 0.0, abs=1e-6)


def test_a_window_period_that_crosses_the_cap_has_one_close_and_it_is_the_real_one():
    """Evening period 18:00-23:30 on day 3 with the loop plan ending at 20:00 that day. The capped plan alone closes the
    windows at 20:00 (it ran out); the spliced plan closes them once, at 23:30, and no step is tagged horizon."""
    snap = _snap("mild", 7, False, 0, WINDOWS[3])
    full, loop, spliced, seam = _both(snap)
    cap = snap.now + timedelta(hours=TIMELINE_LOOP_CAP_HOURS)
    closes = lambda tl: [s for s in tl.steps if s.kind == StepKind.WINDOWS_CLOSE and s.ts.date() == cap.date()]  # noqa: E731
    assert [s.ts for s in closes(loop)] == [cap], "the fixture must make the loop plan clip a period at its end"
    assert [s.ts for s in closes(spliced)] == [s.ts for s in closes(full)] and closes(full)[0].ts != cap
    assert not [s for s in spliced.steps if s.reason == "windows_close.horizon"]
    assert not [s for s in spliced.steps if s.kind == StepKind.NAT_VENT_STOP and s.ts == cap]
    assert seam is not None and seam <= cap and seam.minute == 0
    assert _steps(spliced) == _steps(full)


@pytest.mark.parametrize("shift", SHIFTS)
@pytest.mark.parametrize("window", WINDOWS)
def test_the_seam_produces_no_extra_and_no_missing_steps(shift, window):
    snap = _snap("mild", 7, True, shift, window)
    full, _loop, spliced, seam = _both(snap)
    assert seam is not None
    assert _steps(spliced) == _steps(full)
    # every window step in the plan has its partner on the same side of the seam
    opens = [s for s in spliced.steps if s.kind == StepKind.WINDOWS_OPEN and s.reason != "windows_open.now"]
    closes = [s for s in spliced.steps if s.kind == StepKind.WINDOWS_CLOSE]
    assert len(opens) == len(closes)


def test_a_stale_outlook_that_disagrees_about_a_period_leaves_every_period_whole():
    """The loop plan has a window period on day 3 that spans the first candidate seam (54 h out, rounded up to the
    hour). The outlook was built before the plan had it. A seam inside the period would keep its open and lose its
    close; the quiet seam is outside it, and the period stays whole on the loop's side."""
    shift = 2  # the loop plan's end is 22:00 on day 3; its period there is 15:00-17:00
    snap = _snap("mild", 7, False, shift, WINDOWS[0])
    outlook = build_timeline(_snap("mild", 7, False, shift, WINDOWS[0], windows=False))
    loop = build_timeline(_capped(snap))
    first = snap.now + timedelta(hours=ot.TIMELINE_SEAM_FROM_HOURS)
    first = first.replace(minute=0, second=0, microsecond=0) + (timedelta(hours=1) if first.minute else timedelta())

    def window_kinds(steps):
        return [s.kind for s in steps if s.kind in (StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE)]

    assert [c for c in loop.candidates if c.accepted and c.start < first < c.end], "the fixture's period spans the hour"
    naive = [s for s in loop.steps if s.ts < first] + [s for s in outlook.steps if s.ts >= first]
    assert window_kinds(naive)[-1:] != [StepKind.WINDOWS_CLOSE] or window_kinds(naive).count(
        StepKind.WINDOWS_OPEN
    ) != window_kinds(naive).count(StepKind.WINDOWS_CLOSE), "a seam at that hour leaves the period broken"
    spliced, seam = splice_outlook(snap.now, loop, outlook)
    assert seam is not None and seam > first
    kinds = window_kinds(spliced.steps)
    assert kinds == [StepKind.WINDOWS_OPEN, StepKind.WINDOWS_CLOSE] * (len(kinds) // 2) and kinds
    for tl in (loop, outlook):
        assert not [c for c in tl.candidates if c.accepted and c.start <= seam <= c.end]


def test_the_seam_is_a_whole_hour_in_the_window_that_no_period_spans():
    snap = _snap("mild", 7, False, 0, WINDOWS[3])
    full, loop, _spliced, seam = _both(snap)
    assert seam is not None and seam.minute == seam.second == 0
    assert snap.now + timedelta(hours=ot.TIMELINE_SEAM_FROM_HOURS) <= seam <= snap.now + timedelta(hours=60)
    for tl in (loop, full):
        assert not [c for c in tl.candidates if c.accepted and c.start <= seam <= c.end]


def test_every_hour_spanned_falls_back_to_earlier_hours_then_to_no_splice():
    snap = _snap("mild", 7, False, 0, WINDOWS[0])
    full, loop = build_timeline(snap), build_timeline(_capped(snap))

    def span(a_h: float, b_h: float):
        return dataclasses.replace(
            full.candidates[0],
            start=snap.now + timedelta(hours=a_h),
            end=snap.now + timedelta(hours=b_h),
            accepted=True,
        )

    wide = replace(loop, candidates=(span(53.5, 61),))  # 54..60 all spanned: the hours before 54 are used
    seam = find_seam(snap.now, wide, full)
    assert seam is not None and seam == snap.now + timedelta(hours=53)
    none = replace(loop, candidates=(span(47, 61),))
    assert find_seam(snap.now, none, full) is None
    assert splice_outlook(snap.now, none, full) == (none, None)


def test_an_outlook_that_cannot_be_laid_over_leaves_the_loop_plan_as_it_is():
    snap = _snap("mild", 7, False, 0, WINDOWS[0])
    full, loop = build_timeline(snap), build_timeline(_capped(snap))
    now = snap.now
    assert splice_outlook(now, loop, None) == (loop, None)
    tomorrow = replace(full, day=full.day + timedelta(days=1))
    assert splice_outlook(now, loop, tomorrow)[1] is None  # built on another local day
    assert splice_outlook(now + timedelta(hours=7), replace(loop, day=loop.day), full)[1] is None  # too old
    assert splice_outlook(now - timedelta(minutes=1), loop, full)[1] is None  # built after "now": not a thing
    short = replace(full, horizon_end=now + timedelta(hours=48))
    assert splice_outlook(now, loop, short)[1] is None  # does not reach the seam


# --------------------------------------------------------------------------------------------------------------------
# 3. The predicted line has no step at the seam
# --------------------------------------------------------------------------------------------------------------------


def _max_jump(path, around: datetime, width: timedelta) -> float:
    pts = [p for p in path if p.sensor == "_ca" and abs(p.ts - around) <= width]
    return max((abs(b.temp_f - a.temp_f) for a, b in zip(pts, pts[1:], strict=False)), default=0.0)


@pytest.mark.parametrize("day_type", list(DAY_TYPES))
@pytest.mark.parametrize("v4", [False, True], ids=["v3", "v4"])
@pytest.mark.parametrize("age_h", [0, 1, 3], ids=["fresh", "1h-old", "3h-old"])
def test_the_predicted_line_has_no_step_at_the_seam(day_type, v4, age_h):
    """An outlook built hours ago from another room temperature meets the loop's line without a jump: the largest
    change between neighbouring points around the seam is no bigger than the largest anywhere in the full plan."""
    snap = _snap(day_type, 7, v4, 0, WINDOWS[0])
    loop = build_timeline(_capped(snap))
    old = replace(
        snap,
        now=snap.now - timedelta(hours=age_h),
        indoor=(snap.indoor or 70.0) + (4.0 if age_h else 0.0),
        indoor_primary=(snap.indoor_primary or snap.indoor or 70.0) + (4.0 if age_h else 0.0),
    )
    outlook = build_timeline(old)
    spliced, seam = splice_outlook(snap.now, loop, outlook)
    assert seam is not None
    natural = max(
        abs(b.temp_f - a.temp_f) for a, b in zip(outlook.path, outlook.path[1:], strict=False) if a.sensor == b.sensor
    )
    ours = [p for p in loop.path if p.sensor == "_ca"]
    theirs = [p for p in outlook.path if p.sensor == "_ca"]
    gap = abs(ot._interp_path(ours, seam) - ot._interp_path(theirs, seam))
    # the shift fades over OUTLOOK_PATH_BLEND_HOURS, so neighbouring points differ by at most one step's share of it
    allowed = natural + gap * 0.25 / ot.OUTLOOK_PATH_BLEND_HOURS + 1e-9
    assert _max_jump(spliced.path, seam, timedelta(hours=2)) <= allowed, (age_h, natural, gap)
    # the line is ordered and has each time once
    times = [p.ts for p in spliced.path if p.sensor == "_ca"]
    assert times == sorted(set(times))
    if age_h == 0:
        assert spliced.path == build_timeline(snap).path


def test_an_unshifted_stale_outlook_would_have_jumped():
    """Non-vacuity: the same stale outlook, laid over without the shift, steps by more than the line's own slope."""
    snap = _snap("hot", 7, True, 0, WINDOWS[0])  # a house with mass still remembers its room 54 h on
    loop = build_timeline(_capped(snap))
    old = replace(
        snap,
        now=snap.now - timedelta(hours=3),
        indoor=(snap.indoor or 70.0) + 6.0,
        indoor_primary=(snap.indoor_primary or snap.indoor or 70.0) + 6.0,
    )
    outlook = build_timeline(old)
    seam = find_seam(snap.now, loop, outlook)
    raw = [p for p in loop.path if p.ts < seam] + [p for p in outlook.path if p.ts >= seam]
    natural = max(abs(b.temp_f - a.temp_f) for a, b in zip(outlook.path, outlook.path[1:], strict=False))
    assert _max_jump(raw, seam, timedelta(hours=2)) > natural


# --------------------------------------------------------------------------------------------------------------------
# 4. Thread safety: what the outlook thread reads
# --------------------------------------------------------------------------------------------------------------------


def _house_stepper_from_engine(monkeypatch):
    """``prediction_engines.control_stepper`` on a stand-in v4 engine, the way the coordinator calls it."""
    house = dict.fromkeys(p4.HOUSE_KEYS, 0.0)
    for key, value in zip(p4.HOUSE_KEYS, ZONE1_HOUSE, strict=True):
        house[key] = value

    class _Engine:
        id = pe.ENGINE_V4

        @staticmethod
        def state_of(_source):
            return {}

    class _Model:
        pass

    model = _Model()
    model.house = house
    monkeypatch.setattr(pe, "_control_engine", lambda _config: _Engine)
    monkeypatch.setattr(pe, "control_ready", lambda _source, _config: True)
    monkeypatch.setattr(p4, "from_dict", lambda _state: model)
    stepper = pe.control_stepper(object(), {})
    assert stepper is not None
    return stepper


def test_the_house_stepper_is_stateless_so_a_thread_may_use_it(monkeypatch):
    """Evidence for letting the outlook thread share ``snap.house_stepper`` with the loop.

    1. It is a frozen dataclass; its ``step`` closes over one tuple of floats and nothing else.
    2. ``predictor_v4.free_step`` / ``rollout`` and the helpers they call touch no module-level mutable state.
    3. Eight threads stepping it interleaved with the loop thread give exactly the serial answers."""
    stepper = _house_stepper_from_engine(monkeypatch)
    assert dataclasses.is_dataclass(stepper) and stepper.__dataclass_params__.frozen
    cells = [c.cell_contents for c in (stepper.step.__closure__ or ())]
    assert [c for c in cells if c is not p4] and all(
        (isinstance(c, tuple) and all(isinstance(x, float) for x in c)) or c is p4 for c in cells
    ), cells

    seen: set[str] = set()
    todo = [p4.free_step]
    while todo:
        fn = todo.pop()
        if fn.__name__ in seen:
            continue
        seen.add(fn.__name__)
        for name in fn.__code__.co_names:
            obj = fn.__globals__.get(name)
            if obj is None or name.startswith("__"):
                continue
            if callable(obj) and getattr(obj, "__module__", None) == p4.__name__ and hasattr(obj, "__code__"):
                todo.append(obj)
            elif getattr(obj, "__module__", p4.__name__) == p4.__name__ or not callable(obj):
                assert not isinstance(obj, list | dict | set | bytearray), f"{fn.__name__} reads mutable global {name}"
    assert {"free_step", "rollout"} <= seen

    def walk(out0: float) -> list[tuple[float, float]]:
        t, m, trail = 70.0, 69.0, []
        for k in range(600):
            t, m = stepper.step(t, m, out0 + (k % 24) * 0.7, float(k % 3 == 0), float(k % 5 == 0), (k % 96) / 4.0)
            trail.append((t, m))
        return trail

    serial = [walk(30.0 + 5 * i) for i in range(8)]
    results: list[Any] = [None] * 8

    def run(i: int) -> None:
        results[i] = walk(30.0 + 5 * i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results == serial


def test_the_outlook_snapshot_reads_nothing_live(monkeypatch):
    """Everything the thread's build reads was captured when the snapshot was taken: changing the coordinator
    afterwards (forecast, the classification object, the config) does not change the plan the thread builds."""
    coord, _hass, scheduler = _build()
    coord._hourly_forecast_temps = _hourly(scheduler.now(), 150)
    with scheduler.installed():
        c = coord._current_classification
        snap = coord._timeline_snapshot(c)
        before = repr(build_timeline(snap))
        assert snap.path_fn is not None and snap.path_fn([]), (
            "the path walk must run for the comparison to mean anything"
        )
        # the loop moves on: a new forecast, the classification's window flags flipped in place, a new setpoint config
        coord._hourly_forecast_temps = []
        c.windows_recommended = not c.windows_recommended
        c.hvac_mode = "cool" if c.hvac_mode != "cool" else "heat"
        c.window_open_time, c.window_close_time = time(1, 0), time(2, 0)
        coord.config["comfort_cool"] = 60.0
        coord.config["comfort_heat"] = 50.0
        after = repr(build_timeline(snap))
    assert before == after


def test_no_closure_of_the_snapshot_holds_a_live_coordinator_object():
    """The two callables a thread runs (``path_fn``, ``band_at``) close over copies: not the coordinator, not the live
    classification, not the live config dict, not the live forecast list."""
    coord, _hass, scheduler = _build()
    coord._hourly_forecast_temps = _hourly(scheduler.now(), 150)
    with scheduler.installed():
        live = [coord, coord._current_classification, coord.config, coord._hourly_forecast_temps]
        snap = coord._timeline_snapshot(coord._current_classification)
    seen: list[Any] = []

    def cells(fn: Any) -> None:
        for cell in getattr(fn, "__closure__", None) or ():
            seen.append(cell.cell_contents)
            if callable(cell.cell_contents) and hasattr(cell.cell_contents, "__closure__"):
                cells(cell.cell_contents)

    cells(snap.path_fn)
    cells(snap.band_at)
    assert len(seen) > 4
    for obj in seen:
        assert not any(obj is lv for lv in live), f"a live object is reachable from the thread: {type(obj)}"


def test_a_thread_builds_the_same_plan_as_the_loop():
    snap = _snap("hot", 8, True, 0, WINDOWS[1])
    here = repr(build_timeline(snap))
    box: dict[str, str] = {}
    th = threading.Thread(target=lambda: box.update(plan=repr(build_timeline(snap))))
    th.start()
    th.join()
    assert box["plan"] == here


# --------------------------------------------------------------------------------------------------------------------
# 5. The coordinator: single flight, failure, cadence, what the surfaces read
# --------------------------------------------------------------------------------------------------------------------


class _Gate:
    """A stand-in for ``_executor_job`` that holds each job until the test releases it, and counts them."""

    def __init__(self) -> None:
        self.started = 0
        self.threads: list[int] = []
        self.release: asyncio.Event | None = None
        self.fail: BaseException | None = None

    def __call__(self, fn, *args):
        self.started += 1

        async def job():
            if self.release is not None:
                await self.release.wait()
            if self.fail is not None:
                raise self.fail
            self.threads.append(threading.get_ident())
            return fn(*args)

        return asyncio.ensure_future(job())


def _coordinator(forecast_hours: int = 150):
    coord, _hass, scheduler = _build()
    coord._hourly_forecast_temps = _hourly(scheduler.now(), forecast_hours)
    return coord, scheduler


def _drive(coord, scheduler, gate: _Gate, builds: int, *, hold: bool = False):
    """Run ``builds`` plan builds, letting the spawned outlook tasks run to completion between them (or not)."""

    async def main():
        gate.release = asyncio.Event() if hold else None
        tasks: list[Any] = []
        coord._spawn = lambda coro, background=False: tasks.append(asyncio.ensure_future(coro)) or tasks[-1]
        coord._executor_job = gate
        with scheduler.installed():
            for _ in range(builds):
                coord._timeline_shadow_run("cycle")
                await asyncio.sleep(0)
                if not hold:
                    await asyncio.gather(*tasks)
            if hold:
                gate.release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        return tasks

    return asyncio.run(main())


def test_one_outlook_build_at_a_time_per_zone():
    coord, scheduler = _coordinator()
    gate = _Gate()

    async def main():
        gate.release = asyncio.Event()
        spawned: list[Any] = []
        coord._spawn = lambda coro, background=False: spawned.append(asyncio.ensure_future(coro)) or spawned[-1]
        coord._executor_job = gate
        with scheduler.installed():
            coord._timeline_shadow_run("cycle")
            await asyncio.sleep(0)
            for _ in range(5):  # every one of these is "due" (the plan regenerates, the forecast changes) and skipped
                coord._timeline_outlook_age = 99
                coord._timeline_shadow_run("occupancy")
                await asyncio.sleep(0)
            running = (gate.started, len(spawned))
            gate.release.set()
            await asyncio.gather(*spawned)
        return running, len(spawned)

    running, total = asyncio.run(main())
    assert running == (1, 1) and total == 1
    assert coord._timeline_outlook_info["skipped"] == 5, "a due build that found one running is counted, not queued"
    assert coord._timeline_outlook is not None


def test_after_a_build_finishes_the_next_due_one_starts():
    coord, scheduler = _coordinator()
    gate = _Gate()
    _drive(coord, scheduler, gate, builds=1)
    assert gate.started == 1
    _drive(coord, scheduler, gate, builds=ot.TIMELINE_OUTLOOK_EVERY_N_BUILDS)
    assert gate.started == 2, "the second build starts only when N plan builds have passed"


def test_the_outlook_is_rebuilt_every_n_builds_not_every_build():
    coord, scheduler = _coordinator()
    gate = _Gate()
    n = ot.TIMELINE_OUTLOOK_EVERY_N_BUILDS
    _drive(coord, scheduler, gate, builds=1 + 2 * n)
    assert gate.started == 3


def test_a_changed_forecast_rebuilds_the_outlook_at_once():
    coord, scheduler = _coordinator()
    gate = _Gate()
    _drive(coord, scheduler, gate, builds=2)
    assert gate.started == 1
    coord._hourly_forecast_temps = [dict(e, temperature=e["temperature"] + 5.0) for e in coord._hourly_forecast_temps]
    _drive(coord, scheduler, gate, builds=1)
    assert gate.started == 2


def test_the_outlook_runs_in_a_thread_and_the_surfaces_see_it_spliced():
    coord, scheduler = _coordinator()

    async def main():
        loop = asyncio.get_running_loop()
        threads: list[int] = []

        def executor_job(fn, *args):
            def timed():
                threads.append(threading.get_ident())
                return fn(*args)

            return loop.run_in_executor(None, timed)

        coord._spawn = lambda coro, background=False: asyncio.ensure_future(coro)
        coord._executor_job = executor_job
        with scheduler.installed():
            coord._timeline_shadow_run("cycle")
            loop_plan = coord._timeline_latch.timeline
            assert coord._timeline_plan() is loop_plan, "before the first outlook the surfaces read the loop plan"
            await asyncio.wait_for(coord._timeline_outlook_task, 30)
            surface = coord._surface_timeline(live=False)
            debug = coord.timeline_debug()
        return threads, loop_plan, surface, debug

    threads, loop_plan, surface, debug = asyncio.run(main())
    assert threads and threads[0] != threading.get_ident(), "the full-horizon build ran off the loop thread"
    assert loop_plan.horizon_end == loop_plan.generated_at + timedelta(hours=TIMELINE_LOOP_CAP_HOURS)
    assert coord._timeline_last["capped"] is True
    assert surface is not loop_plan and surface.horizon_end > loop_plan.horizon_end
    seam = datetime.fromisoformat(debug["outlook"]["seam"])
    assert [s for s in surface.steps if s.ts < seam] == [s for s in loop_plan.steps if s.ts < seam]
    assert surface.generation == loop_plan.generation and surface.latched == loop_plan.latched
    assert debug["outlook"]["seam"] and debug["horizon_end"] == surface.horizon_end.isoformat()
    # nothing the loop holds was altered by the splice
    assert coord._timeline_latch.timeline is loop_plan


def test_a_failing_outlook_is_logged_and_leaves_the_loop_plan_alone(caplog):
    coord, scheduler = _coordinator()
    gate = _Gate()
    caplog.set_level(logging.DEBUG)
    _drive(coord, scheduler, gate, builds=1)  # one good outlook first: a later failure must not drop it
    good = coord._timeline_outlook
    assert good is not None
    loop_before = coord._timeline_latch.timeline
    gate.fail = RuntimeError("outlook boom")
    coord._timeline_outlook_age = 99
    _drive(coord, scheduler, gate, builds=1)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "outlook" in r.message]
    assert len(warnings) == 1 and warnings[0].exc_info is not None
    assert coord._timeline_outlook is good, "the previous outlook stands"
    assert coord._timeline_outlook_info["failed"] == 1
    assert coord._timeline_latch.timeline is not None and coord._timeline_last["capped"] is True
    assert coord._timeline_latch.timeline.generation == loop_before.generation
    # a second failure is DEBUG only
    caplog.clear()
    coord._timeline_outlook_age = 99
    _drive(coord, scheduler, gate, builds=1)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "outlook" in r.message]
    # and a good build clears the "already logged" state
    gate.fail = None
    coord._timeline_outlook_age = 99
    _drive(coord, scheduler, gate, builds=1)
    assert coord._timeline_outlook is not good and coord._timeline_outlook_failed_logged is False


def test_a_failure_on_the_first_build_leaves_the_loop_plan_the_only_plan():
    coord, scheduler = _coordinator()
    gate = _Gate()
    gate.fail = RuntimeError("boom")
    _drive(coord, scheduler, gate, builds=1)
    with scheduler.installed():
        plan = coord._surface_timeline(live=False)
    assert plan is coord._timeline_latch.timeline and coord._timeline_outlook is None
    assert plan.steps


def test_a_plan_that_reaches_the_forecasts_end_has_no_outlook_job():
    coord, scheduler = _coordinator(forecast_hours=40)
    gate = _Gate()
    _drive(coord, scheduler, gate, builds=3)
    assert gate.started == 0 and coord._timeline_last["capped"] is False


def test_shutdown_cancels_a_running_outlook_build():
    coord, scheduler = _coordinator()
    gate = _Gate()

    async def main():
        gate.release = asyncio.Event()
        coord._spawn = lambda coro, background=False: asyncio.ensure_future(coro)
        coord._executor_job = gate
        with scheduler.installed():
            coord._timeline_shadow_run("cycle")
            await asyncio.sleep(0)
            task = coord._timeline_outlook_task
            assert task is not None and not task.done()
            coord._timeline_outlook_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return coord._timeline_outlook

    assert asyncio.run(main()) is None
