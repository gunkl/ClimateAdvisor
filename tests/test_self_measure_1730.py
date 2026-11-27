"""Issue #1730 phase 4: Climate Advisor measures what it costs Home Assistant, and warns once when it is too much.

Occupant view: nothing changes for the house. A developer (or the owner reading the Debug card) sees how long the
timeline build and each background job take, how late the event loop runs, and whether this machine is struggling. When
it is, one WARNING says who and by how much; one INFO says it is over; one more WARNING if it lasts a day.

Everything here is deterministic: the loop and the clock are fakes, the state machine takes ``now`` as an argument.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import self_measure as sm
from custom_components.climate_advisor.self_measure import LagWindow, SelfMeasure

LOGGER = "custom_components.climate_advisor.self_measure"


class FakeHandle:
    def __init__(self, when, cb):
        self.when, self.cb, self.cancelled = when, cb, False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """``time()`` and ``call_at`` of an asyncio loop, driven by hand."""

    def __init__(self, now: float = 1000.0):
        self.t = now
        self.handles: list[FakeHandle] = []

    def time(self) -> float:
        return self.t

    def call_at(self, when, cb):
        h = FakeHandle(when, cb)
        self.handles.append(h)
        return h

    @property
    def live(self) -> list[FakeHandle]:
        return [h for h in self.handles if not h.cancelled]

    def fire_next(self, at: float) -> None:
        """The next live callback runs at loop time ``at``."""
        h = self.live[-1]
        self.t = at
        h.cancelled = True  # a fired handle is spent
        h.cb()


def _records(caplog, level=None):
    return [r for r in caplog.records if r.name == LOGGER and (level is None or r.levelno == level)]


# --- the lag window ---------------------------------------------------------------------------------------------------
def test_lag_window_keeps_only_the_last_slots_and_never_grows() -> None:
    w = LagWindow(slots=600)
    buf_len = len(w._buf)
    w.add(5.0)  # an old stall
    for _ in range(600):
        w.add(0.001)
    assert len(w) == 600 and len(w._buf) == buf_len == 600
    assert w.max() == pytest.approx(0.001)  # the stall rolled out of the window
    for _ in range(5000):
        w.add(0.002)
    assert len(w._buf) == 600  # fixed storage: nothing accumulates


def test_lag_window_percentile_is_nearest_rank() -> None:
    w = LagWindow(slots=1000)
    assert w.percentile(99) == 0.0 and w.max() == 0.0  # empty
    for i in range(1, 101):
        w.add(i / 1000.0)
    assert w.percentile(99) == pytest.approx(0.099)
    assert w.percentile(50) == pytest.approx(0.050)
    assert w.max() == pytest.approx(0.100)


def test_one_hiccup_in_a_full_window_does_not_move_p99() -> None:
    w = LagWindow()
    for _ in range(sm.LAG_SLOTS - 1):
        w.add(0.001)
    w.add(0.250)
    assert w.max() == pytest.approx(0.250)
    assert w.percentile(99) == pytest.approx(0.001)  # 1 sample in 600 is the 99.8th percentile


# --- the probe --------------------------------------------------------------------------------------------------------
def test_probe_measures_actual_minus_scheduled_on_the_monotonic_grid() -> None:
    loop, m = FakeLoop(1000.0), SelfMeasure()
    assert m.acquire(loop) is True and m.running
    assert loop.live[-1].when == pytest.approx(1001.0)  # first tick one interval out
    loop.fire_next(1001.020)  # 20 ms late
    assert m.lag.max() == pytest.approx(0.020)
    assert loop.live[-1].when == pytest.approx(1002.0)  # the grid, not "late + interval": lateness does not accumulate
    loop.fire_next(1002.0)
    assert len(m.lag) == 2 and m.lag.percentile(50) == pytest.approx(0.0)


def test_a_stall_is_one_big_sample_and_missed_ticks_are_skipped() -> None:
    loop, m = FakeLoop(1000.0), SelfMeasure()
    m.acquire(loop)
    loop.fire_next(1001.0)
    loop.fire_next(1009.0)  # the loop was frozen 8 s: the 1002 tick fired at 1009
    assert m.lag.max() == pytest.approx(7.0)
    assert len(m.lag) == 2  # not seven catch-up samples
    assert loop.live[-1].when == pytest.approx(1010.0)  # rescheduled from now, never into the past


def test_an_early_fire_is_clamped_to_zero_lateness() -> None:
    loop, m = FakeLoop(1000.0), SelfMeasure()
    m.acquire(loop)
    loop.fire_next(1000.9995)  # a timer may fire within the clock resolution before its time
    assert m.lag.max() == 0.0


def test_the_first_zone_starts_the_probe_and_the_last_one_stops_it() -> None:
    loop, m = FakeLoop(), SelfMeasure()
    assert m.acquire(loop) and m.acquire(loop)  # two zones, ONE probe
    assert len(loop.live) == 1
    m.release()
    assert m.running and len(loop.live) == 1  # one zone left
    m.release()
    assert not m.running and loop.live == []  # cancelled
    m.release()  # an extra release (shutdown runs twice) is harmless
    assert not m.running
    assert m.acquire(loop) and len(loop.live) == 1  # a reload starts it again


def test_a_stopped_probe_does_not_reschedule_even_if_its_callback_still_fires() -> None:
    loop, m = FakeLoop(), SelfMeasure()
    m.acquire(loop)
    cb = loop.live[-1].cb
    m.release()
    before = len(loop.handles)
    cb()  # a callback already queued when stop() ran
    assert len(loop.handles) == before and len(m.lag) == 0


def test_a_loop_that_cannot_run_the_probe_never_breaks_setup() -> None:
    m = SelfMeasure()
    assert m.acquire(None) is False  # no loop (headless tools)
    bad = SimpleNamespace(time=lambda: 1.0, call_at=lambda *_a: (_ for _ in ()).throw(RuntimeError("closed")))
    assert m.acquire(bad) is False and not m.running
    m.release()
    m.release()


def test_a_failing_evaluation_never_stops_the_ticking(monkeypatch) -> None:
    loop, m = FakeLoop(), SelfMeasure()
    m.acquire(loop)

    def boom(_now):
        raise RuntimeError("x")

    monkeypatch.setattr(m, "evaluate", boom)
    loop.fire_next(1061.0)  # past the evaluation interval
    assert len(loop.live) == 1  # still scheduled


# --- the overloaded state ---------------------------------------------------------------------------------------------
T0 = 5000.0


def _minute(m: SelfMeasure, i: int, *, over: bool, name: str = "timeline build", cpu_s: float = 0.080) -> None:
    """One evaluation minute ``i``: 60 probe samples, 80 ms late (over the 50 ms budget) or 1 ms; a loop cost too."""
    m.record_loop_cost(name, cpu_s)
    for _ in range(60):
        m.add_lag(0.080 if over else 0.001)
    m.evaluate(T0 + 60.0 * i)


def test_overloaded_enters_after_ten_over_budget_minutes_with_exactly_one_warning(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure(cpu_count=4)
    for i in range(9):
        _minute(m, i, over=True)
    assert not m.overloaded and _records(caplog) == []
    _minute(m, 9, over=True)
    assert m.overloaded
    (rec,) = _records(caplog, logging.WARNING)
    text = rec.getMessage()
    assert "event loop has been running late" in text and "p99 80 ms, max 80 ms over the last 10 min" in text
    assert "for 10 of the last 10 minutes" in text
    assert "timeline build 80 ms CPU" in text  # our own 80 ms cost could explain the lag, so it is named
    assert "cpus=4" in text and "arch=" in text and "python=" in text  # the machine's figures
    assert "nothing was changed" in text
    for i in range(10, 60):
        _minute(m, i, over=True)
    assert len(_records(caplog, logging.WARNING)) == 1  # never repeats while it lasts (under 24 h)
    assert _records(caplog, logging.INFO) == []


def test_the_warning_names_the_biggest_own_cost_when_it_could_explain_the_lag(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        m.record_loop_cost("timeline build", 0.060)
        m.record_job("coordinator.Coordinator._cycle_judge._job", 1.5)  # the biggest, thread CPU 1500 ms
        for _ in range(60):
            m.add_lag(0.200)
        m.evaluate(T0 + 60.0 * i)
    (rec,) = _records(caplog, logging.WARNING)
    text = rec.getMessage()
    assert "biggest recent cost: executor job coordinator.Coordinator._cycle_judge._job 1500 ms CPU" in text
    assert "elsewhere" not in text


def test_the_warning_says_the_delay_is_probably_elsewhere_when_our_work_was_small(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        m.record_loop_cost("timeline build", 0.004)
        m.record_job("v4_archive.V4Archive.save", 0.020)  # 20 ms: too small to make the loop late
        for _ in range(60):
            m.add_lag(0.200)
        m.evaluate(T0 + 60.0 * i)
    (rec,) = _records(caplog, logging.WARNING)
    text = rec.getMessage()
    assert "own work was small (largest 20 ms)" in text and "probably elsewhere" in text
    assert "biggest recent cost" not in text and "v4_archive" not in text  # never blames Climate Advisor


def test_the_warning_with_no_recorded_work_at_all_says_elsewhere(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        _lag_minute(m, i, [0.200] * 60)
    (rec,) = _records(caplog, logging.WARNING)
    assert "largest 0 ms" in rec.getMessage() and "probably elsewhere" in rec.getMessage()


def test_a_big_own_cost_without_lag_never_makes_a_minute_over_budget(caplog) -> None:
    """A Pi's hourly archive save (seconds of thread CPU) and a 200 ms loop-thread build, on a loop that keeps up."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(3 * 60):
        m.record_job("v4_archive.V4Archive.save", 1.75)
        m.record_loop_cost("timeline build", 0.225)
        for _ in range(60):
            m.add_lag(0.002)
        m.evaluate(T0 + 60.0 * i)
    assert not m.overloaded and m.snapshot()["over_minutes"] == 0 and _records(caplog) == []
    assert m.jobs["v4_archive.V4Archive.save"].max_ms == pytest.approx(1750.0)  # still recorded for the card


def test_it_leaves_after_an_hour_without_an_over_budget_minute_with_one_info(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        _minute(m, i, over=True)
    last_over = T0 + 60.0 * 9
    # calm minutes: still overloaded until a full hour has passed since the last over-budget one
    for i in range(10, 69):
        _minute(m, i, over=False)
    assert m.overloaded and T0 + 60.0 * 68 - last_over < sm.CALM_S
    _minute(m, 69, over=False)  # exactly an hour after minute 9
    assert not m.overloaded
    (info,) = _records(caplog, logging.INFO)
    assert "back to normal" in info.getMessage()
    for i in range(70, 200):
        _minute(m, i, over=False)
    assert len(_records(caplog, logging.INFO)) == 1 and len(_records(caplog, logging.WARNING)) == 1


def test_an_over_budget_minute_while_overloaded_restarts_the_calm_hour() -> None:
    m = SelfMeasure()
    for i in range(10):
        _minute(m, i, over=True)
    for i in range(10, 50):
        _minute(m, i, over=False)
    _minute(m, 50, over=True)  # not calm after all
    for i in range(51, 110):
        _minute(m, i, over=False)
    assert m.overloaded  # 59 calm minutes since minute 50
    _minute(m, 110, over=False)
    assert not m.overloaded


def test_a_persisting_overload_warns_once_more_at_24_hours(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(24 * 60 + 120):
        _minute(m, i, over=True)
    warns = _records(caplog, logging.WARNING)
    assert len(warns) == 2  # entering + the 24 h reminder; nothing in between or after
    assert "still running late after 24 h" in warns[1].getMessage()
    assert _records(caplog, logging.INFO) == []


def test_it_can_enter_again_after_leaving_and_logs_that_episode_too(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        _minute(m, i, over=True)
    for i in range(10, 80):
        _minute(m, i, over=False)
    assert not m.overloaded
    for i in range(80, 90):
        _minute(m, i, over=True)
    assert m.overloaded and m.episodes == 2
    assert len(_records(caplog, logging.WARNING)) == 2 and len(_records(caplog, logging.INFO)) == 1


def test_sporadic_slow_minutes_are_not_an_overload(caplog) -> None:
    """One late minute every half hour is a busy minute, not sustained: 2 over-budget minutes an hour never reach 10."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(12 * 60):
        _minute(m, i, over=(i % 30 == 0))
    assert not m.overloaded and _records(caplog) == []


def _lag_minute(m: SelfMeasure, i: int, late_s: list[float]) -> None:
    """One evaluation minute ``i``: 60 probe samples, the given late ones first, the rest 1 ms."""
    for k in range(60):
        m.add_lag(late_s[k] if k < len(late_s) else 0.001)
    m.evaluate(T0 + 60.0 * i)


def test_a_lagging_loop_for_ten_minutes_enters(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(10):
        _lag_minute(m, i, [0.080] * 60)  # a loop that is 80 ms late all the time
    assert m.overloaded
    (rec,) = _records(caplog, logging.WARNING)
    assert "p99 80 ms, max 80 ms" in rec.getMessage()


def test_a_single_loop_hiccup_never_flags_a_minute() -> None:
    m = SelfMeasure()
    for i in range(30):
        _lag_minute(m, i, [0.400, 0.200])  # two slow callbacks every minute: under LAG_LATE_TICKS
    assert not m.overloaded and m.snapshot()["over_minutes"] == 0


def test_one_busy_minute_is_one_over_budget_minute_not_ten() -> None:
    """Home Assistant starting (or a backup) makes the loop late for a minute: that minute only, not the next ten
    verdicts too (the ten-minute p99 on the card still shows it)."""
    m = SelfMeasure()
    _lag_minute(m, 0, [0.080] * 30)
    for i in range(1, 5):
        _lag_minute(m, i, [])
    assert m.snapshot()["loop_lag"]["p99_ms"] == pytest.approx(80.0)  # the card's ten-minute view still shows it
    for i in range(5, 30):
        _lag_minute(m, i, [])
    assert m.snapshot()["over_minutes"] == 1 and not m.overloaded


def test_nothing_is_logged_below_budget(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    m = SelfMeasure()
    for i in range(300):
        _minute(m, i, over=False)
    assert _records(caplog) == []


# --- job costs --------------------------------------------------------------------------------------------------------
def test_cost_stat_keeps_last_max_count_and_an_ewma() -> None:
    s = sm.CostStat()
    for v in (10.0, 20.0, 5.0):
        s.add(v)
    assert (s.count, s.last_ms, s.max_ms) == (3, 5.0, 20.0)
    assert s.ewma_ms == pytest.approx(10.0 + 0.2 * 10.0 + 0.2 * (5.0 - 12.0))
    d = s.as_dict()
    assert set(d) == {"count", "last_ms", "max_ms", "ewma_ms"}


def test_timed_records_thread_cpu_inside_the_worker_thread_not_wall_time() -> None:
    m = SelfMeasure()
    seen = {}

    def burn(n_ms: float) -> int:
        seen["thread"] = threading.get_ident()
        end = time.thread_time() + n_ms / 1000.0
        while time.thread_time() < end:
            pass
        return 7

    def nap() -> None:
        time.sleep(0.05)  # 50 ms of wall time, ~0 CPU

    async def go():
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, m.timed(functools.partial(burn, 30.0)))
        await loop.run_in_executor(None, m.timed(nap))
        return out

    assert asyncio.run(go()) == 7  # the result passes through
    assert seen["thread"] != threading.get_ident()  # it really ran in a worker thread
    burn_stat = next(v for k, v in m.jobs.items() if k.endswith(".<locals>.burn"))  # named through the partial
    assert 25.0 <= burn_stat.max_ms < 400.0
    assert (
        next(v for k, v in m.jobs.items() if k.endswith(".<locals>.nap")).max_ms < 15.0
    )  # a sleeping job costs no CPU


def test_timed_records_a_job_that_raises_and_lets_the_error_through() -> None:
    m = SelfMeasure()

    def bad() -> None:
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        m.timed(bad)()
    ((name, stat),) = m.jobs.items()
    assert name.endswith("bad") and stat.count == 1


def test_job_names_are_bounded() -> None:
    m = SelfMeasure()
    for i in range(sm.MAX_JOB_NAMES + 50):
        m.record_job(f"job{i}", 0.001)
    assert len(m.jobs) == sm.MAX_JOB_NAMES + 1  # the cap plus the shared overflow bucket
    assert m.jobs[sm.OTHER_NAME].count == 50
    m.record_job("job0", 0.002)
    assert m.jobs["job0"].count == 2  # a known name keeps its own row


def test_callable_name_looks_through_partials_and_wrappers() -> None:
    def target() -> None: ...

    wrapped = functools.wraps(target)(lambda: None)
    assert sm.callable_name(functools.partial(functools.partial(target))) == sm.callable_name(target)
    assert sm.callable_name(wrapped) == sm.callable_name(target)
    assert sm.callable_name(target).startswith("test_self_measure_1730.")


def test_perf_probe_shares_the_production_name_helper() -> None:
    from tools.sim_harness import perf_probe

    def target() -> None: ...

    assert perf_probe.callable_name(functools.partial(target)) == sm.callable_name(target)


# --- the debug payload ------------------------------------------------------------------------------------------------
def test_snapshot_shape_and_top_jobs_are_ordered_by_max_cpu() -> None:
    m = SelfMeasure()
    for i in range(12):
        m.record_job(f"job{i}", 0.001 * (i + 1))
    m.record_loop_cost("timeline build", 0.004)
    snap = m.snapshot()
    assert set(snap) == {
        "executor_jobs",
        "loop_costs",
        "loop_lag",
        "overloaded",
        "over_minutes",
        "episodes",
        "budgets_ms",
    }
    assert [j["name"] for j in snap["executor_jobs"]] == [f"job{i}" for i in range(11, 3, -1)]  # top 8, biggest first
    assert set(snap["executor_jobs"][0]) == {"name", "count", "last_ms", "max_ms", "ewma_ms"}
    assert set(snap["loop_lag"]) == {"running", "samples", "window_s", "max_ms", "p99_ms"}
    assert snap["budgets_ms"] == {"loop_lag": 50.0}
    assert snap["overloaded"] is False and snap["loop_lag"]["running"] is False


def test_for_hass_is_one_per_hass_and_survives_a_hass_without_data() -> None:
    hass = SimpleNamespace(data={})
    assert sm.for_hass(hass) is sm.for_hass(hass)
    assert sm.for_hass(SimpleNamespace()) is not None  # no .data at all: an unshared instance, never an error
