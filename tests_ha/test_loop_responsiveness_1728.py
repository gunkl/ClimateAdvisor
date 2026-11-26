"""Loop responsiveness on real Home Assistant (Issue #1728): a heavy job must not starve the event loop.

Occupant view: while Climate Advisor refits its models (the v4 fit, the night replay, the cycle refit), Home Assistant
must keep answering: lights, automations and the dashboard stay snappy. #1722 was a v4 fit that ran "off the loop" in an
executor THREAD: it still held the GIL, so the loop stalled for seconds on a Raspberry Pi, and only a user noticed.
Production now runs these jobs in a worker process (``process_offload``).

How it measures: a real-time ticker. A helper thread sleeps 5 ms of REAL time and wakes the loop with
``call_soon_threadsafe``; a task on the Home Assistant loop records how late each wake-up ran. (The tier's loop clock is
the frozen simulated one, so ``asyncio.sleep`` cannot be used as the ticker; this probe needs no loop timer.) An idle
baseline is measured first, in the same test, then the same probe runs while the REAL coordinator path runs the job:
``_v4_run_fit`` / ``_v4_run_night_replay`` / ``_cycle_refit``. Thresholds are relative to that baseline so a loaded
CI runner does not flake. The simulated clock is held still (no ``house.advance()`` while a worker runs; see
``test_process_offload_realha_1722.py``).

Each job has a process test (today's code: must stay within budget) and a control in thread mode (the #1722 mechanism:
must exceed it), so a green test proves the probe can see the bug. The tier normally runs executor jobs synchronously
on the loop (``track_executor`` in ``house/settle.py``), which only blocks the loop for one tick; that would not be
#1722. The controls therefore use the ``real_executor`` fixture, which restores asyncio's real ``run_in_executor`` for
the test, so the job runs in a pool thread competing with the loop for the GIL, exactly as in production.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor import process_offload

pytestmark = pytest.mark.house

# ---- budgets (Issue #1728 plan: two-way constants) ------------------------------------------------------------------
_TICK_S = 0.005  # the probe's period
# Why 15 / 25 ms: 3 ms flaked (4.20 ms measured on 2 busy cores). Across 36 process-mode runs (inside a full 4-worker
# HA tier, and pinned to 2 cores with and without CPU hogs) the worst p99 was 10.15 ms (1.48x headroom) and the worst
# tick loss 3%. The real-GIL thread controls (`real_executor` fixture) lost 49-94% of their ticks, far above
# _MAX_TICK_LOSS, which is the primary discriminator: under a loaded tier some controls' p99 sat at or under the limit
# and only tick loss caught them. With about 160 ticks in a short window p99 is roughly the second-worst tick. Do not
# raise the p99 floors above 20 ms: the night-replay control's own p99 is about 16 ms and must stay detectable.
_P99_LAG_BUDGET_MS = 15.0  # p99 wake-up lateness while a heavy job runs, at most ...
_P99_IDLE_FACTOR = 4.0  # ... or this many times the idle p99, whichever is larger (a noisy runner)
# On ONE usable core a worker process shares the loop's core, so the loop waits for scheduler time slices. That is the
# OS, not the GIL; the budget is for a runner with a spare core.
_ONE_CORE_P99_MS = 25.0
_MAX_TICK_LOSS = 0.15  # fraction of ticks lost against the idle baseline
_IDLE_TICKS = 300  # about 1.5 s of baseline
_MIN_JOB_S = 0.4  # a job shorter than this proves nothing: the fixture history is too small
_REAL = time.clock_gettime  # freezegun patches time.monotonic / perf_counter, not clock_gettime


def _real_now() -> float:
    return _REAL(time.CLOCK_MONOTONIC)


def _p99(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.99) - 1)] if ordered else 0.0


# ---- the probe -------------------------------------------------------------------------------------------------------
@dataclass
class Lag:
    ticks: int
    wall_s: float
    p99_ms: float
    max_ms: float
    period_ms: float  # mean time per tick (idle: about 5.1 ms)

    def __str__(self) -> str:
        return (
            f"ticks={self.ticks} wall={self.wall_s:.2f}s period={self.period_ms:.2f}ms "
            f"p99={self.p99_ms:.2f}ms max={self.max_ms:.1f}ms"
        )


class _Ticker:
    """The thread half of the probe: sleeps ``_TICK_S`` of real time per request, then wakes the loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._req = threading.Event()
        self._stop = False
        self._fut: asyncio.Future | None = None
        self._thread = threading.Thread(target=self._run, name="ca-lag-ticker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            self._req.wait()
            self._req.clear()
            if self._stop:
                return
            time.sleep(_TICK_S)
            fut = self._fut
            self._loop.call_soon_threadsafe(lambda f=fut: f.done() or f.set_result(None))

    async def tick(self) -> float:
        """One tick: the lateness in seconds (how much longer than ``_TICK_S`` the wake-up took)."""
        self._fut = self._loop.create_future()
        t0 = _real_now()
        self._req.set()
        await self._fut
        return _real_now() - t0 - _TICK_S

    def close(self) -> None:
        self._stop = True
        self._req.set()
        self._thread.join(5)


async def _sample(ticker: _Ticker, *, ticks: int | None = None, until: asyncio.Future | None = None) -> Lag:
    lags: list[float] = []
    t0 = _real_now()
    while (len(lags) < ticks) if ticks is not None else not until.done():
        lags.append(await ticker.tick())
    wall = _real_now() - t0
    return Lag(
        ticks=len(lags),
        wall_s=wall,
        p99_ms=_p99(lags) * 1000,
        max_ms=max(lags) * 1000,
        period_ms=wall / max(1, len(lags)) * 1000,
    )


async def _measure(job) -> tuple[Lag, Lag]:
    """(idle baseline, lag while ``job()`` runs). ``job`` is a coroutine function: the real coordinator path."""
    ticker = _Ticker(asyncio.get_running_loop())
    try:
        await _sample(ticker, ticks=40)  # warm up
        idle = await _sample(ticker, ticks=_IDLE_TICKS)
        task = asyncio.ensure_future(job())
        busy = await _sample(ticker, until=task)
        await task
    finally:
        ticker.close()
    return idle, busy


def _tick_loss(idle: Lag, busy: Lag) -> float:
    """Fraction of the ticks an idle loop would have completed in the same wall time that did not happen."""
    expected = busy.wall_s / (idle.period_ms / 1000)
    return max(0.0, 1.0 - busy.ticks / expected)


def _allowed_p99_ms(idle: Lag) -> float:
    floor = _P99_LAG_BUDGET_MS if len(os.sched_getaffinity(0)) > 1 else _ONE_CORE_P99_MS
    return max(floor, idle.p99_ms * _P99_IDLE_FACTOR)


def _report(label: str, idle: Lag, busy: Lag) -> None:
    print(  # noqa: T201  (shown with -s: the measured margins)
        f"[loop-lag] {label}: idle[{idle}] busy[{busy}] "
        f"loss={_tick_loss(idle, busy):.0%} allowed_p99={_allowed_p99_ms(idle):.2f}ms"
    )


def _assert_responsive(label: str, idle: Lag, busy: Lag) -> None:
    _report(label, idle, busy)
    assert busy.wall_s >= _MIN_JOB_S, f"{label}: the job took {busy.wall_s:.2f}s; the history is too small to prove it"
    assert busy.p99_ms <= _allowed_p99_ms(idle), f"{label}: the loop was late: {busy} vs idle {idle}"
    assert _tick_loss(idle, busy) <= _MAX_TICK_LOSS, f"{label}: the loop lost ticks: {busy} vs idle {idle}"


def _assert_starved(label: str, idle: Lag, busy: Lag) -> None:
    """The control: the same job in a real pool thread must starve the loop (else the probe could not see #1722).

    "Starves" means EITHER p99 lateness over the allowed value OR tick loss over the limit; both are printed.
    """
    _report(label, idle, busy)
    assert busy.wall_s >= _MIN_JOB_S, f"{label}: the job took {busy.wall_s:.2f}s; the history is too small"
    p99_over = busy.p99_ms > _allowed_p99_ms(idle)
    loss_over = _tick_loss(idle, busy) > _MAX_TICK_LOSS
    print(f"[loop-lag] {label}: p99_over={p99_over} loss_over={loss_over}")  # noqa: T201
    assert p99_over or loss_over, (
        f"{label}: a real-executor thread job did not starve the loop ({busy} vs idle {idle}); "
        "the probe cannot see #1722 (GIL contention)"
    )


# ---- the histories ---------------------------------------------------------------------------------------------------
_SENSORS: tuple[str, ...] = ()  # extra indoor sensors besides the thermostat (each one multiplies the fit cost)
_HISTORY_DAYS = 8
_HISTORY_STEP_MIN = 30  # about 1.4 s of CPU for the fit and 0.8 s for the replay on x86 (not linear in the history)


def _chart_rows(now, primary: str, days: float = _HISTORY_DAYS, step_min: int = _HISTORY_STEP_MIN) -> list[dict]:
    """A heating-season chart log: indoor sensors, a thermostat that cycles, a daily outdoor swing, a noisy forecast."""
    rnd = random.Random(1728)
    n = int(days * 24 * 60 / step_min)
    start = now - timedelta(minutes=step_min * n)
    ind, heating, out = 68.0, False, []
    for i in range(n):
        ts = start + timedelta(minutes=step_min * i)
        hr = ts.hour + ts.minute / 60
        outd = 35 + 10 * math.sin((hr - 9) / 24 * 2 * math.pi) + 6 * math.sin(ts.timetuple().tm_yday / 5.0)
        outd += rnd.gauss(0, 0.4)
        sp = 66.0 if (hr >= 22.5 or hr < 6.5) else 70.0
        if ind < sp - 1.0:
            heating = True
        elif ind > sp + 0.5:
            heating = False
        ind += step_min / 60 * (0.06 * (outd - ind) + (3.2 if heating else 0.0)) + rnd.gauss(0, 0.05)
        sensors = {s: round(ind - 1.2 * k + rnd.gauss(0, 0.05), 2) for k, s in enumerate((primary, *_SENSORS))}
        out.append(
            {
                "ts": ts.isoformat(),
                "hvac": "heating" if heating else "idle",
                "fan": False,
                "indoor": round(ind, 2),
                "outdoor": round(outd, 1),
                "windows_open": False,
                "windows_recommended": False,
                "pred_outdoor": round(outd + rnd.gauss(0, 1.5), 1),
                "pred_indoor": None,
                "setpoint": sp,
                "indoor_sensors": sensors,
            }
        )
    return out


async def _v4_house(make_house, monkeypatch):
    """A v4-primary house whose chart log holds eight days of history (seeded straight into the log)."""
    from tests_ha.test_cycle_wiring_realha_1512 import _HOUSE

    house = await make_house(**{**_HOUSE, "ca_config": {"primary_engine": "v4"}})
    coord = house.coordinator
    primary = coord.config["climate_entity"]
    coord._chart_log._entries.extend(_chart_rows(dt_util.now(), primary))
    monkeypatch.setattr(
        coord, "_read_chart_indoor_sensors", lambda: dict.fromkeys((primary, *_SENSORS), 68.0)
    )  # the node set the fit models (the sim house has one sensor)
    return house


@pytest.fixture
def real_executor(monkeypatch):
    """Returns ``enable()``: call it AFTER ``make_house`` to run executor jobs on asyncio's real pool for this test.

    The tier's ``track_executor`` shadows ``loop.run_in_executor`` with a synchronous wrapper (it blocks the loop for
    the whole job, which hides GIL contention) and is installed while the house is built, so the wrapper must be
    removed afterwards. Deleting the instance attribute exposes the class method; ``monkeypatch`` puts the wrapper
    back at teardown, so other tests are unaffected. Safe because the simulated clock is held still during a job.
    """

    def enable() -> None:
        loop = asyncio.get_running_loop()
        assert "run_in_executor" in vars(loop), "the tier no longer wraps run_in_executor; revisit this fixture"
        monkeypatch.delattr(loop, "run_in_executor")
        assert "run_in_executor" not in vars(loop)

    return enable


def _process_mode(monkeypatch) -> None:
    monkeypatch.setattr(process_offload, "default_mode", "process")


def _thread_mode(monkeypatch) -> None:
    monkeypatch.setattr(process_offload, "default_mode", "thread")


async def _v4_fit_only(coord) -> None:
    """The v4 fit through the real ``_v4_start_fit`` / ``_v4_run_fit`` path, without the replay that follows it."""
    assert coord._v4_start_fit("nightly") is True
    await coord._v4_fit_task


_CYCLES = 40  # about 0.8 s of CPU for the pooled heat refit on x86


async def _noop_replay(reason: str) -> None:
    return None


async def _fitted_house(make_house, monkeypatch):
    """A v4 house with a stored model: fitted in a thread, unmeasured (the replay needs a model)."""
    house = await _v4_house(make_house, monkeypatch)
    coord = house.coordinator
    with monkeypatch.context() as local:  # the replay below is the real one; the caller picks its mode
        local.setattr(coord, "_v4_run_night_replay", _noop_replay)
        local.setattr(process_offload, "default_mode", "thread")
        await _v4_fit_only(coord)
    assert coord._v4_store.v4_model.get("confidence", "none") != "none", "the setup fit gave no usable model"
    return house


async def _cycle_house(make_house):
    """A v3 house whose cycle store holds ``_CYCLES`` recorded heating cycles (synthetic, seeded into the store)."""
    from tests.test_cycle_model_1512 import synth
    from tests_ha.test_cycle_wiring_realha_1512 import _HOUSE

    house = await make_house(**_HOUSE)
    cycles = synth("heat", n=_CYCLES, seed=3)
    for cycle in cycles:
        assert house.coordinator._cycle_store.add(cycle)
    return house, max(c["t_off"] for c in cycles)


# ---- tests -----------------------------------------------------------------------------------------------------------
async def test_v4_fit_keeps_the_loop_responsive_in_a_worker(make_house, monkeypatch) -> None:
    house = await _v4_house(make_house, monkeypatch)
    coord = house.coordinator
    monkeypatch.setattr(coord, "_v4_run_night_replay", _noop_replay)
    _process_mode(monkeypatch)
    idle, busy = await _measure(lambda: _v4_fit_only(coord))
    assert coord._v4_store.v4_model, "the fit stored no model"
    _assert_responsive("v4 fit / process", idle, busy)


async def test_control_v4_fit_in_a_thread_starves_the_loop(make_house, monkeypatch, real_executor) -> None:
    house = await _v4_house(make_house, monkeypatch)
    coord = house.coordinator
    monkeypatch.setattr(coord, "_v4_run_night_replay", _noop_replay)
    _thread_mode(monkeypatch)
    real_executor()
    idle, busy = await _measure(lambda: _v4_fit_only(coord))
    _assert_starved("v4 fit / thread (control)", idle, busy)


async def test_night_replay_keeps_the_loop_responsive_in_a_worker(make_house, monkeypatch) -> None:
    house = await _fitted_house(make_house, monkeypatch)
    coord = house.coordinator
    _process_mode(monkeypatch)
    idle, busy = await _measure(lambda: coord._v4_run_night_replay("test"))
    assert coord._v4_store.night_validation, "the replay stored no record"
    _assert_responsive("night replay / process", idle, busy)


async def test_control_night_replay_in_a_thread_starves_the_loop(make_house, monkeypatch, real_executor) -> None:
    house = await _fitted_house(make_house, monkeypatch)
    coord = house.coordinator
    _thread_mode(monkeypatch)
    real_executor()
    idle, busy = await _measure(lambda: coord._v4_run_night_replay("test"))
    _assert_starved("night replay / thread (control)", idle, busy)


async def test_cycle_refit_keeps_the_loop_responsive_in_a_worker(make_house, monkeypatch) -> None:
    house, now_ts = await _cycle_house(make_house)
    coord = house.coordinator
    _process_mode(monkeypatch)
    idle, busy = await _measure(lambda: coord._cycle_refit("heat", now_ts))
    assert "cycle_fit_heat" in coord.learning._state.thermal_model_cache, "the refit stored no fit"
    _assert_responsive("cycle refit / process", idle, busy)


async def test_control_cycle_refit_in_a_thread_starves_the_loop(make_house, monkeypatch, real_executor) -> None:
    house, now_ts = await _cycle_house(make_house)
    coord = house.coordinator
    _thread_mode(monkeypatch)
    real_executor()
    idle, busy = await _measure(lambda: coord._cycle_refit("heat", now_ts))
    _assert_starved("cycle refit / thread (control)", idle, busy)
