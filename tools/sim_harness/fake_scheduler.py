"""fake_scheduler — virtual clock + deterministic timer driver.

FakeScheduler generalises the ad-hoc timer-capture pattern used in existing
tests (test_override_confirmation.py:96–116, test_door_window.py:689–697):
those tests manually patch ``async_call_later``, capture the callback, and
fire it by hand.  FakeScheduler turns that into a reusable priority queue
with ``advance_to(target_dt)`` that fires callbacks in chronological order,
drains ``async_create_task`` coroutines after each callback, and loops until
quiescent — so self-recursive fan cycles and grace→convergence chains
just work.

Patching contract (applied via the ``installed()`` context manager):
  - ``custom_components.climate_advisor.automation.async_call_later``
    → records ``(fire_at, callback, cancelled_flag_list)``; returns a cancel fn
  - ``custom_components.climate_advisor.automation.callback``
    → identity (so @callback-decorated inner functions remain real callables)
  - ``custom_components.climate_advisor.automation.dt_util.now``
    → returns current virtual clock value
  - ``custom_components.climate_advisor.automation.dt_util.utcnow``
    → returns current virtual clock value
  - ``custom_components.climate_advisor.automation.dt_util.as_local``
    → identity pass-through

The scheduler is NOT a context manager itself — install/uninstall is done via
``installed()`` which returns a context manager that patches the module and
unpatches on exit.
"""

from __future__ import annotations

import asyncio
import heapq
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch


class _ScheduledCallback:
    """A single pending timer entry in the priority queue."""

    __slots__ = ("fire_at", "callback", "_cancelled")

    def __init__(self, fire_at: datetime, callback: Any) -> None:
        self.fire_at = fire_at
        self.callback = callback
        self._cancelled = False

    # heapq compares entries by (fire_at, ...) — make fire_at the sort key
    def __lt__(self, other: _ScheduledCallback) -> bool:
        return self.fire_at < other.fire_at

    def __le__(self, other: _ScheduledCallback) -> bool:
        return self.fire_at <= other.fire_at


class FakeScheduler:
    """Virtual clock and deterministic timer driver.

    Usage::

        scheduler = FakeScheduler(start_dt)
        hass = FakeHass(clock_fn=scheduler.now)
        hass.set_scheduler(scheduler)

        with scheduler.installed():
            # engine calls that schedule timers land in the scheduler
            asyncio.run(engine.handle_all_doors_windows_closed())
            # advance time; all due callbacks fire, tasks drain
            scheduler.advance_by(300 + 1)
    """

    def __init__(self, start: datetime | None = None) -> None:
        """Create a FakeScheduler.

        Args:
            start: Initial virtual clock value.  Defaults to
                   2024-01-15 08:00:00 UTC (arbitrary but deterministic).
        """
        if start is None:
            start = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        self._clock: datetime = start
        self._heap: list[_ScheduledCallback] = []
        self._task_queue: list[Any] = []  # coroutines from async_create_task

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        """Return the current virtual clock time."""
        return self._clock

    # ------------------------------------------------------------------
    # Timer registration (wired in by the installed() patches)
    # ------------------------------------------------------------------

    def _schedule(self, delay_seconds: float, callback: Any) -> Any:
        """Register a callback to fire after ``delay_seconds`` virtual seconds."""
        entry = _ScheduledCallback(
            fire_at=self._clock + timedelta(seconds=delay_seconds),
            callback=callback,
        )
        heapq.heappush(self._heap, entry)

        def _cancel() -> None:
            entry._cancelled = True

        return _cancel

    # ------------------------------------------------------------------
    # Task queue (fed by FakeHass.async_create_task)
    # ------------------------------------------------------------------

    def enqueue_task(self, coro: Any) -> None:
        """Enqueue a coroutine to be drained after the next callback fires."""
        self._task_queue.append(coro)

    def _drain_tasks(self) -> None:
        """Run all enqueued coroutines synchronously."""
        while self._task_queue:
            coro = self._task_queue.pop(0)
            try:
                asyncio.run(coro)
            except RuntimeError:
                # Already inside an event loop — schedule on the running loop
                loop = asyncio.get_event_loop()
                loop.run_until_complete(coro)

    # ------------------------------------------------------------------
    # Advance API
    # ------------------------------------------------------------------

    def advance_to(self, target: datetime) -> None:
        """Advance the clock to ``target``, firing all due callbacks.

        After each callback fires, enqueued tasks are drained, then newly
        scheduled callbacks that are now due are also fired (handles
        self-recursive chains like fan cycles and grace→convergence).
        Loops until the queue has nothing due at or before ``target``.
        """
        if target < self._clock:
            raise ValueError(f"advance_to({target}) is in the past (clock={self._clock})")

        while self._heap and self._heap[0].fire_at <= target:
            entry = heapq.heappop(self._heap)
            if entry._cancelled:
                continue
            # Move clock to exactly when this callback fires
            self._clock = entry.fire_at
            # Fire it — callbacks take a single ``_now`` positional arg
            try:
                entry.callback(self._clock)
            except Exception:  # noqa: BLE001 — don't let one bad callback stop the clock
                import traceback

                traceback.print_exc()
            # Drain any async_create_task coroutines queued by this callback
            self._drain_tasks()
            # Loop: newly-scheduled callbacks may now be due

        # Finally move clock to the requested target
        self._clock = target
        # One final drain in case the last timer enqueued tasks
        self._drain_tasks()

    def advance_by(self, seconds: float) -> None:
        """Advance the clock by ``seconds`` from the current position."""
        self.advance_to(self._clock + timedelta(seconds=seconds))

    # ------------------------------------------------------------------
    # Context manager: patch automation module symbols
    # ------------------------------------------------------------------

    @contextmanager
    def installed(self):
        """Patch automation.py's timer/clock symbols to use this scheduler.

        Yields ``self`` for convenience.  All patches are removed on exit.

        Patches applied:
          - ``async_call_later``  → self._schedule
          - ``callback``          → identity
          - ``dt_util.now``       → self.now
          - ``dt_util.utcnow``    → self.now
          - ``dt_util.as_local``  → identity
        """
        # We need to patch the dt_util *object* that automation.py imported,
        # not the original module.  automation.py does:
        #   from homeassistant.util import dt as dt_util
        # so the name ``dt_util`` lives in automation's own namespace.
        # We patch individual attributes on that object via the automation module.

        def _fake_async_call_later(hass: Any, delay: float, cb: Any) -> Any:
            return self._schedule(delay, cb)

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=_fake_async_call_later,
            ),
            patch(
                "custom_components.climate_advisor.automation.callback",
                side_effect=lambda fn: fn,
            ),
            patch(
                "custom_components.climate_advisor.automation.dt_util.now",
                side_effect=lambda: self._clock,
            ),
            patch(
                "custom_components.climate_advisor.automation.dt_util.utcnow",
                side_effect=lambda: self._clock,
            ),
            patch(
                "custom_components.climate_advisor.automation.dt_util.as_local",
                side_effect=lambda x: x,
            ),
        ):
            yield self
