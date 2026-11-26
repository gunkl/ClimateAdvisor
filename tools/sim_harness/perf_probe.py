"""Report-only performance probe for the simulation harness (Issue #1728).

While active it records, without touching production code:

* ``executor``   - thread CPU + wall per callable handed to ``FakeHass.async_add_executor_job`` (the harness runs
                   these inline, so thread CPU is exactly the job's own CPU), named ``module.qualname``.
* ``loop``       - thread CPU + wall per event-loop callback step. The hook is ``asyncio.events.Handle._run``
                   (what asyncio debug mode's ``slow_callback_duration`` measures). That is an asyncio internal;
                   it is used because the harness has no loop-level hook. It is restored on exit.
* ``task``       - the same per scheduler task: ``FakeScheduler.enqueue_task`` coroutines. ``FakeScheduler`` and
                   ``run_coro`` run a whole coroutine in one loop step, so this is the useful granularity.
* ``worker``     - wall per ``process_offload.async_run_job`` call (a child process in production; inline in sim).
* ``lines``      - optional (``lines=True``): deterministic Python line counts via ``sys.monitoring`` LINE events for
                   a small named set of hot functions (the #1331 technique, see tests/test_control_quality.py).
                   Off by default because monitoring roughly doubles CPU time.

GC: it is disabled for the run (restored on exit) and every pause is timed through ``gc.callbacks`` and subtracted
from any span it falls inside, so a collection is never attributed to a job. Because a disabled GC would grow
without bound over a long run, the probe collects explicitly between top-level spans: a young collection
(``gc.collect(1)``) at most once per ``collect_interval_s`` and a full one at most once per ``full_collect_interval_s``
(a forced full collection every second cost about 10% of a ``--generate 20`` run); that time is booked as GC too.

Memory is bounded: per name, count / total / max are exact and percentiles come from a 2048-sample reservoir.

Nothing here changes simulation results or exit status; budgets only flag rows in the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import gc
import json
import random
import sys
import time
from collections import defaultdict
from typing import Any

SCHEMA = "climate-advisor-perf-report"
SCHEMA_VERSION = 1

# Budgets, named once (milliseconds of thread CPU).
EXECUTOR_BUDGET_MS = 500.0  # a single executor job should never hold a worker thread this long
LOOP_FLAG_MS = 10.0  # a loop callback this long is flagged
LOOP_HIGHLIGHT_MS = 20.0  # ... and highlighted from here

# Hot functions whose line counts are recorded with lines=True: (module, owner attribute or None, function).
HOT_FUNCTIONS = (
    ("custom_components.climate_advisor.operation_timeline", None, "build_timeline"),
    ("custom_components.climate_advisor.night_strategy", None, "plan_night"),
    ("custom_components.climate_advisor.coordinator", "ClimateAdvisorCoordinator", "_v4_display_pending"),
    ("custom_components.climate_advisor.cycle_model", None, "fit_cycles"),
)


def callable_name(fn: Any) -> str:
    """``module.qualname`` of a callable, looking through ``functools.partial`` wrappers and bound-zone wrappers."""
    seen = 0
    while seen < 8:
        seen += 1
        if isinstance(fn, functools.partial):
            fn = fn.func
        elif hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        else:
            break
    qual = getattr(fn, "__qualname__", None) or type(fn).__name__
    mod = getattr(fn, "__module__", "") or ""
    return f"{mod.rsplit('.', 1)[-1]}.{qual}" if mod else qual


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(-(-pct / 100 * len(sorted_vals) // 1)) - 1))
    return sorted_vals[idx]


RESERVOIR = 2048  # samples kept per name for percentiles; count, total and max stay exact beyond it


class _Stat:
    """Exact count / total / max and a bounded reservoir of values, so a 3000-day nightly stays small in memory."""

    __slots__ = ("count", "total_cpu", "max_cpu", "max_wall", "cpu", "rng")

    def __init__(self) -> None:
        self.count = 0
        self.total_cpu = 0.0
        self.max_cpu = 0.0
        self.max_wall = 0.0
        self.cpu: list[float] = []
        self.rng = random.Random(1728)  # own instance: never touches the global random state the simulation uses

    def add(self, cpu: float, wall: float) -> None:
        self.count += 1
        self.total_cpu += cpu
        self.max_cpu = max(self.max_cpu, cpu)
        self.max_wall = max(self.max_wall, wall)
        if len(self.cpu) < RESERVOIR:
            self.cpu.append(cpu)
        else:
            j = self.rng.randrange(self.count)
            if j < RESERVOIR:
                self.cpu[j] = cpu


class PerfProbe:
    """Context manager; ``with PerfProbe() as probe: ...; probe.report()``."""

    def __init__(
        self, *, lines: bool = False, collect_interval_s: float = 1.0, full_collect_interval_s: float = 20.0
    ) -> None:
        self.lines = lines
        self.collect_interval_s = collect_interval_s
        self.full_collect_interval_s = full_collect_interval_s
        # kind -> name -> stats
        self.samples: dict[str, dict[str, _Stat]] = defaultdict(lambda: defaultdict(_Stat))
        self.line_counts: dict[str, int] = defaultdict(int)
        self.gc_pauses: list[float] = []
        self._gc_cum_wall = 0.0
        self._gc_cum_cpu = 0.0
        self._gc_start: tuple[float, float] | None = None
        self._depth = 0
        self._last_collect = 0.0
        self._last_full_collect = 0.0
        self._restore: list[Any] = []
        self._gc_was_enabled = True
        self._active = False
        self._tool: int | None = None
        self.started_wall = 0.0
        self.total_wall = 0.0

    # -- GC accounting -------------------------------------------------
    def _gc_callback(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._gc_start = (time.perf_counter(), time.thread_time())
        elif self._gc_start is not None:
            w = time.perf_counter() - self._gc_start[0]
            c = time.thread_time() - self._gc_start[1]
            self._gc_start = None
            self._gc_cum_wall += w
            self._gc_cum_cpu += c
            self.gc_pauses.append(w)

    # -- span bookkeeping ----------------------------------------------
    def _begin(self) -> tuple[float, float, float, float]:
        self._depth += 1
        return time.thread_time(), time.perf_counter(), self._gc_cum_cpu, self._gc_cum_wall

    def _end(self, kind: str, name: str, token: tuple[float, float, float, float]) -> None:
        c0, w0, gc_c0, gc_w0 = token
        cpu = time.thread_time() - c0 - (self._gc_cum_cpu - gc_c0)
        wall = time.perf_counter() - w0 - (self._gc_cum_wall - gc_w0)
        self.samples[kind][name].add(max(cpu, 0.0), max(wall, 0.0))
        self._depth -= 1
        if self._depth == 0 and self.collect_interval_s is not None:
            now = time.perf_counter()
            if now - self._last_collect >= self.collect_interval_s:
                # outside every span; booked as GC time by the callback. Young collections keep the garbage of the
                # frequent short-lived cycles bounded; the rare full one reclaims whatever got promoted.
                if now - self._last_full_collect >= self.full_collect_interval_s:
                    gc.collect()
                    self._last_full_collect = time.perf_counter()
                else:
                    gc.collect(1)
                self._last_collect = time.perf_counter()

    # -- install / uninstall -------------------------------------------
    def _patch(self, owner: Any, attr: str, new: Any) -> None:
        self._restore.append((owner, attr, owner.__dict__.get(attr, getattr(owner, attr))))
        setattr(owner, attr, new)

    def __enter__(self) -> PerfProbe:
        if self._active:
            raise RuntimeError("PerfProbe is already active")
        from tools.sim_harness import fake_hass as fh  # noqa: PLC0415
        from tools.sim_harness import fake_scheduler as fs  # noqa: PLC0415
        from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

        install_ha_stubs()
        from custom_components.climate_advisor import process_offload as po  # noqa: PLC0415

        self._active = True
        self._gc_was_enabled = gc.isenabled()
        try:
            self._install(fh, fs, po)
        except BaseException:
            self._rollback()  # a half-installed probe must leave nothing behind (GC back on, every patch undone)
            raise
        return self

    def _install(self, fh: Any, fs: Any, po: Any) -> None:
        gc.disable()
        gc.callbacks.append(self._gc_callback)
        self.started_wall = self._last_collect = self._last_full_collect = time.perf_counter()
        probe = self

        orig_exec = fh.FakeHass.async_add_executor_job

        def executor_job(hass_self: Any, fn: Any, *args: Any) -> Any:
            tok = probe._begin()
            try:
                return orig_exec(hass_self, fn, *args)
            finally:
                probe._end("executor", callable_name(fn), tok)

        self._patch(fh.FakeHass, "async_add_executor_job", executor_job)

        orig_enqueue = fs.FakeScheduler.enqueue_task

        def enqueue_task(sched_self: Any, coro: Any) -> Any:
            name = getattr(coro, "__qualname__", None) or type(coro).__name__

            async def timed() -> Any:
                tok = probe._begin()
                try:
                    return await coro
                finally:
                    probe._end("task", name, tok)

            timed.__qualname__ = name  # the loop-callback row is named after the real coroutine, not this wrapper
            return orig_enqueue(sched_self, timed())

        self._patch(fs.FakeScheduler, "enqueue_task", enqueue_task)

        orig_run = asyncio.events.Handle._run

        def handle_run(handle: Any) -> Any:
            tok = probe._begin()
            try:
                return orig_run(handle)
            finally:
                probe._end("loop", probe._handle_name(handle), tok)

        self._patch(asyncio.events.Handle, "_run", handle_run)

        orig_run_job = po.async_run_job

        async def run_job(job: str, payload: Any, **kw: Any) -> Any:
            w0 = time.perf_counter()
            try:
                return await orig_run_job(job, payload, **kw)
            finally:
                wall = time.perf_counter() - w0
                probe.samples["worker"][job].add(wall, wall)  # wall time stands in for CPU

        self._patch(po, "async_run_job", run_job)

        if self.lines:
            self._start_lines()

    def _rollback(self) -> None:
        """Undo whatever ``__enter__`` got through: patches, the monitoring tool, the GC callback, the GC itself."""
        with contextlib.suppress(Exception):
            self._stop_lines()
        for owner, attr, original in reversed(self._restore):
            with contextlib.suppress(Exception):
                setattr(owner, attr, original)
        self._restore.clear()
        with contextlib.suppress(ValueError):
            gc.callbacks.remove(self._gc_callback)
        if self._gc_was_enabled:
            gc.enable()
        self._active = False

    @staticmethod
    def _handle_name(handle: Any) -> str:
        cb = getattr(handle, "_callback", None)
        try:
            owner = getattr(cb, "__self__", None)
            coro = owner.get_coro() if hasattr(owner, "get_coro") else None
            name = getattr(coro, "__qualname__", None)
            if name:
                return name
            return callable_name(cb)
        except Exception:  # noqa: BLE001 - a name must never break the run
            return "<unknown callback>"

    def __exit__(self, *exc: Any) -> None:
        if not self._active:
            return
        self._rollback()
        self.total_wall = time.perf_counter() - self.started_wall

    # -- sys.monitoring line counts ------------------------------------
    def _start_lines(self) -> None:
        import importlib  # noqa: PLC0415

        code_names: dict[Any, str] = {}
        for mod_name, owner, func in HOT_FUNCTIONS:
            try:
                mod = importlib.import_module(mod_name)
                target = getattr(mod, owner) if owner else mod
                fn = getattr(target, func)
            except Exception:  # noqa: BLE001 - a missing hot function must not break the run
                continue
            code = getattr(fn, "__code__", None)
            if code is not None:
                code_names[code] = f"{mod_name.rsplit('.', 1)[-1]}.{func}"
        mon = sys.monitoring
        tool = next((t for t in (mon.PROFILER_ID, 3, 4) if mon.get_tool(t) is None), None)
        if tool is None:
            return
        mon.use_tool_id(tool, "ca-1728-perf-probe")
        counts = self.line_counts

        def on_line(code: Any, _line: int) -> Any:
            name = code_names.get(code)
            if name is None:
                return mon.DISABLE
            counts[name] += 1
            return None

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
        self._tool = tool

    def _stop_lines(self) -> None:
        if self._tool is None:
            return
        mon = sys.monitoring
        mon.set_events(self._tool, 0)
        mon.register_callback(self._tool, mon.events.LINE, None)
        mon.free_tool_id(self._tool)
        mon.restart_events()
        self._tool = None

    # -- reporting -----------------------------------------------------
    def report(self, top: int | None = None) -> dict:
        """The versioned JSON-able report (rows sorted by max CPU, then p99)."""
        kinds: dict[str, list[dict]] = {}
        for kind, names in self.samples.items():
            rows = []
            for name, st in names.items():
                cpu = sorted(v * 1000 for v in st.cpu)
                row = {
                    "name": name,
                    "count": st.count,
                    "total_cpu_ms": round(st.total_cpu * 1000, 3),
                    "p50_cpu_ms": round(_percentile(cpu, 50), 3),
                    "p99_cpu_ms": round(_percentile(cpu, 99), 3),
                    "max_cpu_ms": round(st.max_cpu * 1000, 3),
                    "max_wall_ms": round(st.max_wall * 1000, 3),
                    "flag": self._flag(kind, st.max_cpu * 1000),
                }
                rows.append(row)
            rows.sort(key=lambda r: (r["max_cpu_ms"], r["max_wall_ms"], r["p99_cpu_ms"]), reverse=True)
            kinds[kind] = rows[:top] if top else rows
        pauses = sorted(p * 1000 for p in self.gc_pauses)
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "budgets_ms": {
                "executor_job": EXECUTOR_BUDGET_MS,
                "loop_callback_flag": LOOP_FLAG_MS,
                "loop_callback_highlight": LOOP_HIGHLIGHT_MS,
            },
            "total_wall_s": round(self.total_wall, 3),
            "gc": {
                "disabled_during_run": True,
                "pauses": len(pauses),
                "total_ms": round(sum(pauses), 3),
                "max_ms": round(pauses[-1], 3) if pauses else 0.0,
            },
            "metrics": {k: ("wall" if k == "worker" else "thread_cpu") for k in kinds},
            "kinds": kinds,
            "line_counts": dict(sorted(self.line_counts.items())) if self.lines else None,
        }

    @staticmethod
    def _flag(kind: str, ms: float) -> str:
        if kind == "executor" and ms > EXECUTOR_BUDGET_MS:
            return "OVER_BUDGET"
        if kind == "loop":
            if ms > LOOP_HIGHLIGHT_MS:
                return "HIGHLIGHT"
            if ms > LOOP_FLAG_MS:
                return "FLAG"
        return ""


_TITLES = {
    "executor": f"Executor jobs (thread CPU; budget {EXECUTOR_BUDGET_MS:g} ms)",
    "loop": f"Loop callbacks (thread CPU; flag {LOOP_FLAG_MS:g} ms, highlight {LOOP_HIGHLIGHT_MS:g} ms)",
    "task": "Scheduler tasks (whole coroutine; report only)",
    "worker": "Worker jobs via process_offload.async_run_job (wall time)",
}


def format_table(report: dict, top: int = 15) -> str:
    """Plain-text table of the top ``top`` rows per kind."""
    out = [f"Performance report (report only; schema v{report['schema_version']}, run {report['total_wall_s']} s)"]
    g = report["gc"]
    out.append(
        f"GC disabled during run; {g['pauses']} pauses, {g['total_ms']} ms total, max {g['max_ms']} ms (excluded)"
    )
    for kind in ("executor", "loop", "task", "worker"):
        rows = report["kinds"].get(kind)
        if not rows:
            continue
        out.append("")
        out.append(f"-- {_TITLES[kind]}")
        out.append(f"{'count':>7} {'total_ms':>10} {'p50_ms':>8} {'p99_ms':>8} {'max_ms':>8}  flag        name")
        for r in rows[:top]:
            mx = r["max_cpu_ms"]
            out.append(
                f"{r['count']:>7} {r['total_cpu_ms']:>10.1f} {r['p50_cpu_ms']:>8.2f} {r['p99_cpu_ms']:>8.2f} "
                f"{mx:>8.2f}  {r['flag']:<10}  {r['name']}"
            )
    if report.get("line_counts"):
        out.append("")
        out.append("-- Line counts (sys.monitoring, deterministic)")
        for name, n in report["line_counts"].items():
            out.append(f"{n:>10}  {name}")
    return "\n".join(out)


def write_json(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
