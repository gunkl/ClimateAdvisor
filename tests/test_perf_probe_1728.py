"""Issue #1728: the report-only performance probe (tools/sim_harness/perf_probe.py).

Covers the recording hooks (executor job, loop callback, scheduler task, worker job), GC exclusion, the versioned JSON
schema, the budget flags, clean install/removal, and that ``simulate.py --perf-report`` leaves results untouched.
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import time
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

GOLDEN = REPO_ROOT / "tools" / "simulations" / "golden"


def busy(ms: float) -> None:
    """Burn ``ms`` of this thread's CPU."""
    end = time.thread_time() + ms / 1000
    while time.thread_time() < end:
        pass


def slow_executor_job() -> int:
    busy(30)
    return 7


async def slow_loop_step() -> None:
    busy(25)


def _hass() -> fh.FakeHass:
    return fh.FakeHass()


def _row(report: dict, kind: str, needle: str) -> dict:
    rows = [r for r in report["kinds"].get(kind, []) if needle in r["name"]]
    assert rows, f"no {kind} row containing {needle!r} in {[r['name'] for r in report['kinds'].get(kind, [])]}"
    return rows[0]


def test_records_slow_executor_job_with_module_qualified_name():
    hass = _hass()

    async def go():
        return await hass.async_add_executor_job(slow_executor_job)

    with PerfProbe() as probe:
        assert _loop.run_coro(go()) == 7  # the job's result passes through untouched
    row = _row(probe.report(), "executor", "slow_executor_job")
    assert row["name"] == "test_perf_probe_1728.slow_executor_job"
    assert row["count"] == 1
    assert 25 <= row["max_cpu_ms"] < 400


def test_records_slow_loop_callback_by_coroutine_name_and_flags_it():
    with PerfProbe() as probe:
        _loop.run_coro(slow_loop_step())
    row = _row(probe.report(), "loop", "slow_loop_step")
    assert row["max_cpu_ms"] >= 20
    assert row["flag"] == "HIGHLIGHT"


def test_scheduler_task_row_is_named_after_the_real_coroutine():
    sched = fs.FakeScheduler()
    with PerfProbe() as probe:
        sched.enqueue_task(slow_loop_step())
        sched._drain_tasks()
    report = probe.report()
    assert _row(report, "task", "slow_loop_step")["max_cpu_ms"] >= 20
    assert "timed" not in _row(report, "loop", "slow_loop_step")["name"]  # not the probe's wrapper


def test_worker_job_is_recorded_by_wall_time():
    from custom_components.climate_advisor import process_offload as po

    async def fake_job(job, payload, **kw):
        await asyncio.sleep(0.02)
        return {"ok": True}

    original = po.async_run_job
    po.async_run_job = fake_job
    try:
        with PerfProbe() as probe:
            result = _loop.run_coro(po.async_run_job("v4_fit", {}, executor_job=None, logger=None, timeout_s=1))
            assert result == {"ok": True}
        report = probe.report()
        row = _row(report, "worker", "v4_fit")
        assert row["max_cpu_ms"] >= 15 and report["metrics"]["worker"] == "wall"
    finally:
        po.async_run_job = original


def test_gc_time_is_not_attributed_to_a_job():
    hass = _hass()
    junk = []
    built = {}

    def job_with_collect() -> None:
        t0 = time.thread_time()
        for _ in range(60000):  # cyclic garbage, so the explicit collection below has real work
            a: list = []
            a.append(a)
            junk.append(a)
        junk.clear()
        built["ms"] = (time.thread_time() - t0) * 1000
        gc.collect()  # INSIDE the job

    async def go():
        return await hass.async_add_executor_job(job_with_collect)

    with PerfProbe(collect_interval_s=1e9) as probe:
        _loop.run_coro(go())
    report = probe.report()
    gc_ms = report["gc"]["total_ms"]
    job = _row(report, "executor", "job_with_collect")
    assert gc_ms > 5, "the test needs a measurable collection"
    # Charged CPU is the job's own work. Without the subtraction it would be about built + gc_ms.
    assert job["max_cpu_ms"] <= built["ms"] + 0.5 * gc_ms + 2


def test_gc_pause_inside_loop_callback_is_not_charged_to_it():
    built = {}

    async def step():
        t0 = time.thread_time()
        for _ in range(40000):
            a: list = []
            a.append(a)
        built["ms"] = (time.thread_time() - t0) * 1000
        gc.collect()

    with PerfProbe(collect_interval_s=1e9) as probe:
        _loop.run_coro(step())
    report = probe.report()
    gc_ms = report["gc"]["total_ms"]
    assert gc_ms > 3
    assert _row(report, "loop", "step")["max_cpu_ms"] <= built["ms"] + 0.5 * gc_ms + 2


def test_report_json_schema():
    hass = _hass()
    with PerfProbe() as probe:
        _loop.run_coro(_wrap_job(hass))
    report = json.loads(json.dumps(probe.report()))
    assert report["schema"] == "climate-advisor-perf-report"
    assert report["schema_version"] == 1
    assert set(report["budgets_ms"]) == {"executor_job", "loop_callback_flag", "loop_callback_highlight"}
    assert set(report["gc"]) == {"disabled_during_run", "pauses", "total_ms", "max_ms"}
    assert report["line_counts"] is None
    row = report["kinds"]["executor"][0]
    assert set(row) == {
        "name",
        "count",
        "total_cpu_ms",
        "p50_cpu_ms",
        "p99_cpu_ms",
        "max_cpu_ms",
        "max_wall_ms",
        "flag",
    }
    assert "Executor jobs" in perf_probe.format_table(report)


async def _wrap_job(hass):
    await hass.async_add_executor_job(busy, 1)


def test_percentiles():
    vals = [float(i) for i in range(1, 101)]
    assert perf_probe._percentile(vals, 50) == 50.0
    assert perf_probe._percentile(vals, 99) == 99.0
    assert perf_probe._percentile([], 99) == 0.0


@pytest.mark.parametrize(
    ("kind", "ms", "flag"),
    [
        ("executor", 499.0, ""),
        ("executor", 501.0, "OVER_BUDGET"),
        ("loop", 9.9, ""),
        ("loop", 10.1, "FLAG"),
        ("loop", 20.1, "HIGHLIGHT"),
        ("task", 900.0, ""),
        ("worker", 900.0, ""),
    ],
)
def test_budget_flags(kind, ms, flag):
    assert PerfProbe._flag(kind, ms) == flag


def test_install_and_remove_restores_everything():
    from custom_components.climate_advisor import process_offload as po

    before = (
        fh.FakeHass.__dict__["async_add_executor_job"],
        fs.FakeScheduler.__dict__["enqueue_task"],
        asyncio.events.Handle.__dict__["_run"],
        po.async_run_job,
        gc.isenabled(),
        list(gc.callbacks),
    )
    with PerfProbe() as probe:
        assert fh.FakeHass.__dict__["async_add_executor_job"] is not before[0]
        assert asyncio.events.Handle.__dict__["_run"] is not before[2]
        assert not gc.isenabled()
        assert probe._gc_callback in gc.callbacks
    after = (
        fh.FakeHass.__dict__["async_add_executor_job"],
        fs.FakeScheduler.__dict__["enqueue_task"],
        asyncio.events.Handle.__dict__["_run"],
        po.async_run_job,
        gc.isenabled(),
        list(gc.callbacks),
    )
    assert after == before


def test_restores_even_when_the_run_raises_and_keeps_gc_off_if_it_was_off():
    gc.disable()
    try:
        with pytest.raises(ZeroDivisionError), PerfProbe():
            1 / 0  # noqa: B018
        assert not gc.isenabled()  # it was disabled before: not force-enabled
    finally:
        gc.enable()
    assert asyncio.events.Handle.__dict__["_run"].__qualname__ == "Handle._run"


def test_line_counts_are_deterministic_for_a_named_hot_function(monkeypatch):
    def hot(n: int) -> int:
        total = 0
        for i in range(n):
            total += i
        return total

    monkeypatch.setattr(sys.modules[__name__], "hot_under_test", hot, raising=False)
    monkeypatch.setattr(perf_probe, "HOT_FUNCTIONS", ((__name__, None, "hot_under_test"),))
    counts = []
    for _ in range(2):
        with PerfProbe(lines=True) as probe:
            hot(50)
        counts.append(probe.report()["line_counts"])
    assert counts[0] == counts[1]
    assert counts[0][f"{__name__.rsplit('.', 1)[-1]}.hot_under_test"] >= 100  # ~2 lines per loop pass
    with PerfProbe() as probe:  # off by default
        hot(50)
    assert probe.report()["line_counts"] is None
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None  # the tool id was freed


def test_perf_report_leaves_simulation_results_identical(tmp_path, capsys):
    import simulate  # noqa: PLC0415

    names = sorted(p for p in GOLDEN.glob("*.json") if p.name != "MANIFEST.json")[:2]
    plain = [simulate.run_scenario_production(p, state="golden") for p in names]
    with PerfProbe() as probe:
        probed = [simulate.run_scenario_production(p, state="golden") for p in names]
    assert [r["passed"] for r in plain] == [r["passed"] for r in probed]
    assert [r.get("callback_errors") for r in plain] == [r.get("callback_errors") for r in probed]
    assert json.dumps(plain, default=str, sort_keys=True) == json.dumps(probed, default=str, sort_keys=True)
    assert probe.report()["kinds"]["executor"]  # and it did record


def test_cli_flag_writes_json_and_keeps_exit_status_and_output(tmp_path, monkeypatch, capsys):
    import simulate  # noqa: PLC0415

    name = next(p.stem for p in sorted(GOLDEN.glob("*.json")) if p.name != "MANIFEST.json")
    out = tmp_path / "perf.json"

    def run(*extra: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["simulate.py", "-s", name, *extra])
        rc = simulate.main()
        return rc, capsys.readouterr().out

    rc_plain, text_plain = run()
    rc_perf, text_perf = run("--perf-report", str(out))
    assert rc_perf == rc_plain
    assert text_perf.startswith(text_plain)  # the simulation's own output is unchanged; the table follows it
    assert "Performance report" in text_perf[len(text_plain) :]
    assert json.loads(out.read_text())["schema_version"] == 1

    rc_missing, _ = (lambda: (run("--perf-report", str(out))[0], None))()  # same flag, still fine
    assert rc_missing == rc_plain

    monkeypatch.setattr(sys, "argv", ["simulate.py", "-s", "no_such_scenario_1728", "--perf-report", str(out)])
    assert simulate.main() == 1  # a failing run keeps its exit status


def test_memory_is_bounded_but_count_total_and_max_stay_exact():
    stat = perf_probe._Stat()
    n = perf_probe.RESERVOIR * 3
    for i in range(n):
        stat.add(i / 1000, i / 1000)
    assert stat.count == n
    assert len(stat.cpu) == perf_probe.RESERVOIR
    assert stat.max_cpu == (n - 1) / 1000
    assert stat.total_cpu == pytest.approx(sum(range(n)) / 1000)
