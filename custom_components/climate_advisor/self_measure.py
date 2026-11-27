"""Self-measurement: what Climate Advisor costs Home Assistant, and a warning when it is too much (Issue #1730).

Hass-free (stdlib only), so the tools can share :func:`callable_name`. Measures and warns; it changes no behaviour.

* **Per-job CPU.** ``coordinator._executor_job`` wraps every callable with :meth:`SelfMeasure.timed`: thread CPU
  time (``time.thread_time``) measured INSIDE the worker thread, keyed by :func:`callable_name`. Per job: last, max,
  count and an EWMA. At most :data:`MAX_JOB_NAMES` names are kept (the rest share ``<other>``), so memory is bounded.
* **Loop-thread costs.** The timeline build (it runs on the loop) reports through
  :meth:`SelfMeasure.record_loop_cost`.
* **Loop-lag probe.** One self-rescheduling ``loop.call_at`` callback per Home Assistant instance (zones share it by
  a reference count), every :data:`LAG_INTERVAL_S`. Lateness is ``loop.time() - scheduled`` (the monotonic clock);
  the last :data:`LAG_SLOTS` samples (about ten minutes) live in a fixed ``array('d')`` ring, so nothing
  grows. A stall shows as ONE large sample (missed ticks are skipped, not replayed).
* **Overloaded state.** Once a minute the probe's tick asks :meth:`SelfMeasure.evaluate`. A minute counts as "over
  budget" when at least :data:`LAG_LATE_TICKS` of that minute's own ticks were late by more than
  :data:`LOOP_BUDGET_MS`, or a loop-thread cost recorded in that minute exceeded :data:`LOOP_BUDGET_MS`, or an
  executor job's thread CPU recorded in that minute exceeded :data:`EXECUTOR_BUDGET_MS`. The lag verdict reads only
  the minute's own ticks: the ten-minute p99 on the Debug card would carry one busy minute (Home Assistant starting,
  a backup) into the next ten verdicts, which alone is the ten over-budget minutes that enter the state. Enter when
  :data:`ENTER_OVER_MINUTES` of the last :data:`ENTER_WINDOW_MINUTES` minutes were over budget; leave after
  :data:`CALM_S` without one. One WARNING on entering (the worst offender with its numbers and the machine's figures),
  one INFO on leaving, one more WARNING if it lasts :data:`PERSIST_S`. Nothing else changes.

The clock is injected (``evaluate(now)``), so the state machine is tested with a fake one.
"""

from __future__ import annotations

import array
import functools
import logging
import math
import os
import platform
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

# Budgets (milliseconds). Loop: the plan's "no loop callback over about 50 ms on a Pi 4". Executor: the registry's
# short_cpu budget (``tests/test_executor_job_registry_1728.py``), x86 thread CPU.
LOOP_BUDGET_MS: Final = 50.0
EXECUTOR_BUDGET_MS: Final = 500.0

LAG_INTERVAL_S: Final = 1.0
LAG_WINDOW_S: Final = 600.0
LAG_SLOTS: Final = int(LAG_WINDOW_S / LAG_INTERVAL_S)
# A minute's loop lag is over budget when at least this many of its (about 60) ticks were later than LOOP_BUDGET_MS:
# sustained lag, not one slow callback (one stall is one sample).
LAG_LATE_TICKS: Final = 3

EVAL_INTERVAL_S: Final = 60.0
ENTER_WINDOW_MINUTES: Final = 60
ENTER_OVER_MINUTES: Final = 10
CALM_S: Final = 3600.0
PERSIST_S: Final = 86400.0

EWMA_ALPHA: Final = 0.2
MAX_JOB_NAMES: Final = 64
OTHER_NAME: Final = "<other>"
TOP_JOBS: Final = 8  # rows in the debug payload

HASS_DATA_KEY: Final = "climate_advisor_self_measure"
_LAG_SOURCE: Final = f"loop lag ({LAG_LATE_TICKS} late ticks in a minute)"


def callable_name(fn: Any) -> str:
    """``module.qualname`` of a callable, looking through ``functools.partial`` wrappers and ``__wrapped__`` ones."""
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


class CostStat:
    """Last / max / count / EWMA of a cost in milliseconds, plus the max since the last evaluation."""

    __slots__ = ("count", "ewma_ms", "interval_max_ms", "last_ms", "max_ms")

    def __init__(self) -> None:
        self.count = 0
        self.last_ms = 0.0
        self.max_ms = 0.0
        self.ewma_ms = 0.0
        self.interval_max_ms = 0.0

    def add(self, ms: float) -> None:
        self.count += 1
        self.last_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms
        if ms > self.interval_max_ms:
            self.interval_max_ms = ms
        self.ewma_ms = ms if self.count == 1 else self.ewma_ms + EWMA_ALPHA * (ms - self.ewma_ms)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "last_ms": round(self.last_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "ewma_ms": round(self.ewma_ms, 2),
        }


class LagWindow:
    """Fixed ring of the last ``slots`` lateness samples (seconds). ``add`` is allocation-free."""

    __slots__ = ("_buf", "_i", "_n")

    def __init__(self, slots: int = LAG_SLOTS) -> None:
        self._buf = array.array("d", bytes(8 * slots))
        self._i = 0
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def add(self, seconds: float) -> None:
        self._buf[self._i] = seconds
        self._i = (self._i + 1) % len(self._buf)
        if self._n < len(self._buf):
            self._n += 1

    def max(self) -> float:
        return max(self._buf[: self._n], default=0.0)

    def percentile(self, pct: float) -> float:
        """Nearest-rank percentile of the samples held (0.0 when empty)."""
        if not self._n:
            return 0.0
        vals = sorted(self._buf[: self._n] if self._n < len(self._buf) else self._buf)
        return vals[max(0, min(len(vals) - 1, math.ceil(pct / 100.0 * len(vals)) - 1))]


class SelfMeasure:
    """One per Home Assistant instance (``for_hass``): job costs, the loop-lag probe, the overloaded state."""

    def __init__(self, *, logger: logging.Logger | None = None, cpu_count: int | None = None) -> None:
        self._log = logger or _LOGGER
        self._cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
        self._lock = threading.Lock()  # executor threads record jobs; the loop reads and resets
        self.jobs: dict[str, CostStat] = {}
        self.loop_costs: dict[str, CostStat] = {}
        self.lag = LagWindow()
        self._minute_top = [0.0] * LAG_LATE_TICKS  # this minute's LAG_LATE_TICKS largest lateness samples, descending
        # probe
        self._refs = 0
        self._loop: Any = None
        self._handle: Any = None
        self._due = 0.0
        # overloaded state
        self._last_eval: float | None = None
        self._flags: deque[tuple[bool, tuple[str, float, float] | None]] = deque(maxlen=ENTER_WINDOW_MINUTES)
        self.overloaded = False
        self.overloaded_since: float | None = None
        self._last_over_at: float | None = None
        self._persist_logged = False
        self.episodes = 0

    # -- recording ---------------------------------------------------------------------------------------------------
    def _stat(self, table: dict[str, CostStat], name: str) -> CostStat:
        st = table.get(name)
        if st is None:
            if len(table) >= MAX_JOB_NAMES:
                name = OTHER_NAME
                st = table.get(name)
            if st is None:
                st = table[name] = CostStat()
        return st

    def record_job(self, name: str, cpu_s: float) -> None:
        """One executor job's thread CPU (called from the worker thread)."""
        with self._lock:
            self._stat(self.jobs, name).add(cpu_s * 1000.0)

    def record_loop_cost(self, name: str, cpu_s: float) -> None:
        """One loop-thread cost (the timeline build)."""
        with self._lock:
            self._stat(self.loop_costs, name).add(cpu_s * 1000.0)

    def timed(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """``fn`` wrapped so its thread CPU is recorded under its stable name. Measured where it runs (the worker)."""
        name = callable_name(fn)
        record = self.record_job
        thread_time = time.thread_time

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            t0 = thread_time()
            try:
                return fn(*args, **kwargs)
            finally:
                record(name, thread_time() - t0)

        return run

    # -- the probe ---------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._handle is not None

    def acquire(self, loop: Any) -> bool:
        """A zone takes a reference; the first one starts the probe. False when ``loop`` cannot run it (tests)."""
        self._refs += 1
        if self._handle is not None:
            return True
        try:
            now = loop.time()
            if not isinstance(now, float):
                raise TypeError("loop.time() is not a float")
            self._loop = loop
            self._last_eval = now
            self._due = now + LAG_INTERVAL_S
            self._handle = loop.call_at(self._due, self._tick)
        except Exception:  # noqa: BLE001 - measuring must never stop a zone from setting up
            self._loop = self._handle = None
            self._log.debug("loop-lag probe not started", exc_info=True)
            return False
        return True

    def release(self) -> None:
        """A zone drops its reference; the last one cancels the probe."""
        self._refs = max(0, self._refs - 1)
        if self._refs == 0:
            self.stop()

    def stop(self) -> None:
        handle, self._handle, self._loop = self._handle, None, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # noqa: BLE001
                self._log.debug("loop-lag probe cancel failed", exc_info=True)

    def _tick(self) -> None:
        loop = self._loop
        if loop is None:
            return
        now = loop.time()
        try:
            self.add_lag(max(0.0, now - self._due))
            if self._last_eval is None or now - self._last_eval >= EVAL_INTERVAL_S:
                self.evaluate(now)
        except Exception:  # noqa: BLE001 - never raise into the loop; keep ticking
            self._log.debug("loop-lag probe tick failed", exc_info=True)
        nxt = self._due + LAG_INTERVAL_S
        if nxt <= now:  # a stall is one big sample, not a burst of catch-up ticks
            nxt = now + LAG_INTERVAL_S
        self._due = nxt
        if self._loop is not None:
            self._handle = loop.call_at(nxt, self._tick)

    def add_lag(self, seconds: float) -> None:
        """One probe sample: into the ten-minute ring (the card) and this minute's verdict. Loop thread only."""
        self.lag.add(seconds)
        top = self._minute_top
        if seconds > top[-1]:
            top[-1] = seconds
            top.sort(reverse=True)

    # -- the overloaded state ----------------------------------------------------------------------------------------
    def evaluate(self, now: float) -> None:
        """One minute's verdict at monotonic time ``now``; moves the overloaded state (logs outside the lock)."""
        # the minute's LAG_LATE_TICKS-th largest lateness: over budget means that many ticks were later than it
        minute_lag_ms = self._minute_top[-1] * 1000.0
        self._minute_top = [0.0] * LAG_LATE_TICKS
        sources: list[tuple[str, float, float]] = [(_LAG_SOURCE, minute_lag_ms, LOOP_BUDGET_MS)]
        with self._lock:
            for table, label, budget in (
                (self.loop_costs, "", LOOP_BUDGET_MS),
                (self.jobs, "executor job ", EXECUTOR_BUDGET_MS),
            ):
                for name, st in table.items():
                    sources.append((label + name, st.interval_max_ms, budget))
                    st.interval_max_ms = 0.0
        over = [s for s in sources if s[1] > s[2]]
        worst = max(over, key=lambda s: s[1] / s[2]) if over else None
        self._last_eval = now
        self._flags.append((worst is not None, worst))
        if worst is not None:
            self._last_over_at = now
        over_minutes = sum(1 for flag, _w in self._flags if flag)
        if not self.overloaded:
            if over_minutes >= ENTER_OVER_MINUTES:
                self.overloaded = True
                self.overloaded_since = now
                self._persist_logged = False
                self.episodes += 1
                self._log.warning(
                    "Climate Advisor is over its time budget: %s; machine: %s",
                    self._offender_text(self._worst_recent(), over_minutes),
                    self._machine_text(),
                )
            return
        since = self.overloaded_since if self.overloaded_since is not None else now
        calm_since = self._last_over_at if self._last_over_at is not None else since
        if now - calm_since >= CALM_S:
            self.overloaded = False
            self._log.info(
                "Climate Advisor is back within its time budget: calm for %d min after %d min over budget",
                int((now - calm_since) // 60),
                int((calm_since - since) // 60),
            )
            self.overloaded_since = None
            self._flags.clear()
        elif now - since >= PERSIST_S and not self._persist_logged:
            self._persist_logged = True
            self._log.warning(
                "Climate Advisor is still over its time budget after %d h: %s; machine: %s",
                int((now - since) // 3600),
                self._offender_text(self._worst_recent(), over_minutes),
                self._machine_text(),
            )

    def _worst_recent(self) -> tuple[str, float, float] | None:
        worsts = [w for _flag, w in self._flags if w is not None]
        return max(worsts, key=lambda s: s[1] / s[2]) if worsts else None

    def _offender_text(self, worst: tuple[str, float, float] | None, over_minutes: int) -> str:
        if worst is None:
            return "no single source stands out"
        name, value, budget = worst
        return (
            f"worst={name} {value:.0f} ms against {budget:.0f} ms; over budget in {over_minutes} of the last "
            f"{min(len(self._flags), ENTER_WINDOW_MINUTES)} minutes; "
            f"loop lag p99={self.lag.percentile(99) * 1000:.0f} ms max={self.lag.max() * 1000:.0f} ms. "
            "Climate Advisor only measures and reports this; nothing was changed"
        )

    def _machine_text(self) -> str:
        return f"cpus={self._cpus} arch={platform.machine() or '?'} python={sys.version_info[0]}.{sys.version_info[1]}"

    # -- the debug payload -------------------------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Install-wide figures for the Debug card (JSON-able; never raises into the caller)."""
        with self._lock:
            jobs = sorted(self.jobs.items(), key=lambda kv: kv[1].max_ms, reverse=True)[:TOP_JOBS]
            top = [{"name": n, **st.as_dict()} for n, st in jobs]
            loop_costs = {n: st.as_dict() for n, st in self.loop_costs.items()}
        return {
            "executor_jobs": top,
            "loop_costs": loop_costs,
            "loop_lag": {
                "running": self.running,
                "samples": len(self.lag),
                "window_s": LAG_WINDOW_S,
                "max_ms": round(self.lag.max() * 1000.0, 2),
                "p99_ms": round(self.lag.percentile(99.0) * 1000.0, 2),
            },
            "overloaded": self.overloaded,
            "over_minutes": sum(1 for flag, _w in self._flags if flag),
            "episodes": self.episodes,
            "budgets_ms": {"loop": LOOP_BUDGET_MS, "executor_job": EXECUTOR_BUDGET_MS},
        }


def for_hass(hass: Any) -> SelfMeasure:
    """The instance-wide :class:`SelfMeasure` (one per Home Assistant, kept in ``hass.data``)."""
    try:
        data = hass.data
        existing = data.get(HASS_DATA_KEY)
        if isinstance(existing, SelfMeasure):
            return existing
        created = SelfMeasure()
        data[HASS_DATA_KEY] = created
        return created
    except Exception:  # noqa: BLE001 - a hass without a usable data dict still gets a working (unshared) instance
        return SelfMeasure()
