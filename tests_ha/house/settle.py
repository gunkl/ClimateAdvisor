"""Deterministic settling for a frozen-clock Home Assistant loop (Issue #1125).

The question every step of the real-HA tier asks is "has everything that can run NOW finished?".  The old answers
were bounded by iteration counts (``for _ in range(50): await asyncio.sleep(0)``) or by Home Assistant's own
``async_block_till_done``.  Both are wall-clock sensitive:

* a task waiting on a thread-pool job (Climate Advisor saves its state, chart log and learning files through
  ``async_add_executor_job``) is not finished after 50 loop turns on a loaded machine, so the old drain concluded
  "a sleeper is left" and moved the simulated clock a second - an event order that exists only on slow runners;
* ``async_block_till_done`` waits for tracked tasks that are merely ``asyncio.sleep``-ing, which on a frozen clock
  is forever (the 30 s test timeout).

Here "settled" is a property of the loop, not of a count: no thread-pool job in flight, nothing in the ready
queue, no timer already due.  Work that waits for the clock (a sleeper, a timer in the future) is NOT "runnable
now" and is left for the next clock step, exactly as in production.  The result is a function of the simulated
timeline only, so the same commit gives the same run on any machine, worker count or test order.

If the loop never settles the settle FAILS (``SettleError``) with what was still pending; it never gives up
silently.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
from typing import Any

# One observation of "quiet" can be a gap between two links of a call_soon chain; a chain link added by a task
# that ran after us in the same loop turn is visible from the next turn, so two quiet turns in a row end it.
_QUIET_TURNS = 3
# Loop turns before giving up.  Pure-CPU chains are a few dozen turns; waiting for a thread-pool job costs one
# turn per ~0.2 ms of real time (see _IDLE_WAIT_S), so this is ~ a minute of real time at most.
_MAX_TURNS = 300_000
_IDLE_WAIT_S = 0.0002

_real_sleep = time.sleep  # freezegun never patches sleep; bind it here anyway so a test cannot make us spin

_STATE = "_ca_executor_inflight"


class SettleError(RuntimeError):
    """The loop did not become quiet; the message says what was still pending."""


def track_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Make thread-pool jobs deterministic on ``loop`` (idempotent; call before anything can start one).

    Default (``synchronous``): ``loop.run_in_executor`` still runs the job on a pool thread (Home Assistant's
    blocking-call detector forbids file I/O on the loop thread), but the loop waits for it and hands back an
    already-finished future.  A job therefore costs zero loop time, as it does on the fast machine the goldens
    were triaged on; a slower or busier machine can no longer let a competing coroutine run "while the disk
    is busy" (Issue #1125: the #1130 race outcome flipped with 20 ms of executor latency).

    ``CA_HA_THREADED_EXECUTOR=1`` keeps real asynchronous pool jobs, counted, so ``quiesce`` waits for them
    (combine with ``CA_HA_EXECUTOR_JITTER_MS`` to stress the settling logic itself).
    """
    if getattr(loop, _STATE, None) is not None:
        return
    counter = [0]
    original = loop.run_in_executor

    if os.environ.get("CA_HA_THREADED_EXECUTOR") == "1":

        def _done(_future: Any) -> None:
            counter[0] -= 1

        def run_in_executor(executor: Any, func: Any, *args: Any) -> Any:
            future = original(executor, func, *args)
            counter[0] += 1
            future.add_done_callback(_done)
            return future

    else:

        def run_in_executor(executor: Any, func: Any, *args: Any) -> Any:
            pool = executor or getattr(loop, "_default_executor", None)
            if pool is None:
                return original(executor, func, *args)
            done = loop.create_future()
            submitted = pool.submit(func, *args)
            concurrent.futures.wait([submitted])
            exc = submitted.exception()
            if exc is not None:
                done.set_exception(exc)
            else:
                done.set_result(submitted.result())
            return done

    loop.run_in_executor = run_in_executor  # type: ignore[method-assign]
    setattr(loop, _STATE, counter)


def executor_jobs_in_flight(loop: asyncio.AbstractEventLoop) -> int:
    counter = getattr(loop, _STATE, None)
    return counter[0] if counter else 0


def _timers_due_now(loop: asyncio.AbstractEventLoop) -> int:
    """Timers whose time has come on the (frozen) loop clock: the loop will run them on its next turn."""
    horizon = loop.time() + loop._clock_resolution  # noqa: SLF001  (the loop's own test, base_events._run_once)
    return sum(1 for h in list(loop._scheduled) if not h.cancelled() and h.when() < horizon)  # noqa: SLF001


def _describe(loop: asyncio.AbstractEventLoop) -> str:
    return (
        f"executor_jobs_in_flight={executor_jobs_in_flight(loop)} ready={len(loop._ready)} "  # noqa: SLF001
        f"timers_due_now={_timers_due_now(loop)}"
    )


async def quiesce(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Return once nothing can run without the clock moving (see the module docstring)."""
    loop = loop or asyncio.get_running_loop()
    quiet = 0
    for _ in range(_MAX_TURNS):
        await asyncio.sleep(0)
        in_flight = executor_jobs_in_flight(loop)
        if in_flight == 0 and not loop._ready and _timers_due_now(loop) == 0:  # noqa: SLF001
            quiet += 1
            if quiet >= _QUIET_TURNS:
                return
            continue
        quiet = 0
        if in_flight and not loop._ready:  # noqa: SLF001
            _real_sleep(_IDLE_WAIT_S)  # only a thread can make progress: give it the CPU instead of spinning
    raise SettleError(f"the loop never became quiet after {_MAX_TURNS} turns: {_describe(loop)}")
