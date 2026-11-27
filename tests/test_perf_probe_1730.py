"""Issue #1730 phase 4: the nightly perf report separates a job from the loop step that ran it, times FakeScheduler
timer callbacks, and adds one heavy generated household. Report only: none of it may change a run or fail it."""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.sim_harness import _loop, perf_probe  # noqa: E402
from tools.sim_harness import fake_hass as fh  # noqa: E402
from tools.sim_harness import fake_scheduler as fs  # noqa: E402
from tools.sim_harness.perf_probe import PerfProbe  # noqa: E402


def busy(ms: float) -> None:
    end = time.thread_time() + ms / 1000
    while time.thread_time() < end:
        pass


def job_30() -> int:
    busy(30)
    return 1


def _row(report: dict, kind: str, needle: str) -> dict:
    rows = [r for r in report["kinds"].get(kind, []) if needle in r["name"]]
    assert rows, f"no {kind} row with {needle!r}: {[r['name'] for r in report['kinds'].get(kind, [])]}"
    return rows[0]


# --- nested executor spans are not charged to the loop row that contains them ---------------------------------------
async def step_with_nested_job() -> None:
    busy(20)  # the step's own work
    await fh.FakeHass().async_add_executor_job(job_30)  # the harness runs this inline, inside the step


def test_loop_row_excludes_the_executor_job_it_ran_inline() -> None:
    with PerfProbe() as probe:
        _loop.run_coro(step_with_nested_job())
    report = probe.report()
    loop_row = _row(report, "loop", "step_with_nested_job")
    assert 15 <= loop_row["max_cpu_ms"] < 45  # 20 own, not 20 + 30
    assert 25 <= _row(report, "executor", "job_30")["max_cpu_ms"] < 400  # the job is still booked in full, once


def test_task_row_excludes_the_nested_job_too() -> None:
    sched = fs.FakeScheduler()
    with PerfProbe() as probe:
        sched.enqueue_task(step_with_nested_job())
        sched._drain_tasks()
    assert _row(probe.report(), "task", "step_with_nested_job")["max_cpu_ms"] < 45


def test_a_nested_job_deeper_in_the_stack_is_subtracted_from_every_row_above_it() -> None:
    async def outer() -> None:
        await step_with_nested_job()

    sched = fs.FakeScheduler()
    with PerfProbe() as probe:
        sched.enqueue_task(outer())
        sched._drain_tasks()
    report = probe.report()
    assert _row(report, "task", "outer")["max_cpu_ms"] < 45
    assert _row(report, "loop", "outer")["max_cpu_ms"] < 45


def test_a_job_that_raises_still_balances_the_span_stack() -> None:
    hass = fh.FakeHass()

    def boom() -> None:
        busy(5)
        raise RuntimeError("x")

    async def go() -> None:
        with pytest.raises(RuntimeError):
            await hass.async_add_executor_job(boom)

    with PerfProbe() as probe:
        _loop.run_coro(go())
        assert probe._stack == [] and probe._depth == 0
    assert probe.probe_errors == 0


# --- FakeScheduler timer callbacks ---------------------------------------------------------------------------------
def slow_timer_callback(_now) -> str:
    busy(15)
    return "kept"


def test_scheduler_sync_callbacks_are_timed_and_named_after_the_registered_function() -> None:
    sched = fs.FakeScheduler()
    with PerfProbe() as probe:
        sched._schedule_interval(timedelta(minutes=5), slow_timer_callback)
        sched.advance_by(15 * 60)
    row = _row(probe.report(), "callback", "slow_timer_callback")
    assert row["name"] == "test_perf_probe_1730.slow_timer_callback"  # not the scheduler's re-arming closure
    assert row["count"] == 3 and row["max_cpu_ms"] >= 12


def test_timer_callback_results_and_the_armed_log_are_untouched() -> None:
    sched = fs.FakeScheduler()
    got = []

    def cb(now):
        got.append(now)
        return "r"

    with PerfProbe():
        sched._schedule(60, cb)
        assert sched.armed_log[-1][2] is cb  # the harness records what production registered, not the probe's wrapper
        sched.advance_by(61)
    assert len(got) == 1


def test_timer_callback_row_excludes_a_job_it_ran() -> None:
    sched = fs.FakeScheduler()
    hass = fh.FakeHass()

    def cb(_now) -> None:
        busy(10)
        hass.async_add_executor_job(job_30)

    with PerfProbe() as probe:
        sched._schedule(1, cb)
        sched.advance_by(2)
    assert _row(probe.report(), "callback", "cb")["max_cpu_ms"] < 28


def test_the_probe_restores_the_scheduler_entry_class() -> None:
    before = fs._ScheduledCallback.__init__
    with PerfProbe():
        assert fs._ScheduledCallback.__init__ is not before
    assert fs._ScheduledCallback.__init__ is before


# --- the heavy household -------------------------------------------------------------------------------------------
def test_household_extras_are_a_seven_day_hourly_forecast_and_a_v4_spec() -> None:
    from tools.sim_harness import generator

    plan = generator.build_plan(perf_probe.HOUSEHOLD_DAY_SEED, "coordinator")
    ex = perf_probe.household_extras(plan)
    hours = ex["hourly_forecast"]
    assert len(hours) == 7 * 24 and hours[0]["datetime"].endswith("+00:00")
    assert all(set(h) == {"datetime", "temperature"} for h in hours)
    assert ex["v4_model"]["confidence"] == "ok" and ex == perf_probe.household_extras(plan)  # deterministic


def test_run_heavy_household_books_household_rows_and_resets_the_phase() -> None:
    with PerfProbe() as probe:
        perf_probe.run_heavy_household(probe)
        assert probe.phase == ""
    report = probe.report()
    assert report["household"]["ran"] is True and report["household"]["forecast_days"] == 7
    kinds = set(report["kinds"])
    assert "household_executor" in kinds and "household_loop" in kinds
    assert "Household" in perf_probe.format_table(report)
    assert report["metrics"].get("household_worker", "wall") == "wall"


def test_a_failing_household_is_noted_never_raised(monkeypatch) -> None:
    from tools.sim_harness import generator

    def boom(*_a, **_k):
        raise RuntimeError("household exploded")

    monkeypatch.setattr(generator, "run_plan", boom)
    with PerfProbe() as probe:
        perf_probe.run_heavy_household(probe)  # must not raise
        assert probe.phase == ""
    hh = probe.report()["household"]
    assert hh["ran"] is False and "household exploded" in hh["error"]
    perf_probe.format_table(probe.report())  # and the table still renders


def test_run_plan_without_extras_is_unchanged() -> None:
    """The hook is opt-in: a normal generated day builds its session from exactly the skeleton it always did."""
    from tools.sim_harness import generator

    seen = {}
    real = generator.make_session

    def spy(scenario, **kw):
        seen.update(scenario)
        return real(scenario, **kw)

    plan = generator.build_plan(7, "coordinator")
    try:
        generator.make_session = spy
        generator.run_plan(plan)
    finally:
        generator.make_session = real
    assert "hourly_forecast" not in seen and "v4_model" not in seen
