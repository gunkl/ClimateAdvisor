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

Issue #474 extended the same contract to ``custom_components.climate_advisor.
coordinator`` — ``async_call_later``, ``async_track_time_change``,
``async_track_time_interval``, ``async_track_point_in_time``,
``async_track_state_change_event``, ``callback``, and ``dt_util.*`` — so a
real ``ClimateAdvisorCoordinator`` can be driven headlessly with the same
virtual clock that drives the ``AutomationEngine``. See ``installed()``'s
docstring for the full patch list.

The scheduler is NOT a context manager itself — install/uninstall is done via
``installed()`` which returns a context manager that patches the module and
unpatches on exit.
"""

from __future__ import annotations

import heapq
import inspect
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from tools.sim_harness._loop import run_coro


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
            run_coro(engine.handle_all_doors_windows_closed())
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
        # Exceptions raised during callback/task execution.  Advance never
        # raises mid-timeline, but G3 assertions can inspect this list and
        # fail a scenario that had unexpected production errors.
        self.callback_errors: list[tuple[datetime, BaseException]] = []

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
        return self._schedule_at(self._clock + timedelta(seconds=delay_seconds), callback)

    def _schedule_at(self, fire_at: datetime, callback: Any) -> Any:
        """Register a callback to fire at an absolute virtual datetime."""
        entry = _ScheduledCallback(fire_at=fire_at, callback=callback)
        heapq.heappush(self._heap, entry)

        def _cancel() -> None:
            entry._cancelled = True

        return _cancel

    def _schedule_daily(self, hour: int | None, minute: int | None, second: int | None, callback: Any) -> Any:
        """Fire ``callback`` when local time matches hour/minute/second, then reschedule +1 day.

        Coordinator call sites always supply all three fields (briefing_time,
        wake_time, sleep_time, midnight-finalize) — wildcard (``None``) fields
        are not exercised by production and simply match the clock's current
        value for that field, matching real ``async_track_time_change``
        semantics closely enough for a fixed daily trigger.
        """
        cancelled = [False]

        def _next_fire_at(after: datetime) -> datetime:
            h = hour if hour is not None else after.hour
            m = minute if minute is not None else after.minute
            s = second if second is not None else after.second
            candidate = after.replace(hour=h, minute=m, second=s, microsecond=0)
            if candidate <= after:
                candidate += timedelta(days=1)
            return candidate

        def _fire_and_reschedule(now: datetime) -> Any:
            if cancelled[0]:
                return None
            # Issue #476: return callback(now)'s result (rather than discarding it) so
            # advance_to()'s coroutine check — which inspects THIS wrapper's return value,
            # not the inner callback's — can detect and enqueue an async callback target
            # (e.g. coordinator._async_send_briefing/_async_morning_wakeup, both async def).
            result = callback(now)
            if not cancelled[0]:
                self._schedule_at(_next_fire_at(now), _fire_and_reschedule)
            return result

        self._schedule_at(_next_fire_at(self._clock), _fire_and_reschedule)

        def _cancel() -> None:
            cancelled[0] = True

        return _cancel

    def _schedule_interval(self, interval: timedelta, callback: Any) -> Any:
        """Fire ``callback`` every ``interval``, starting one interval from now."""
        cancelled = [False]

        def _fire_and_reschedule(now: datetime) -> Any:
            if cancelled[0]:
                return None
            # Issue #476: see matching comment in _schedule_daily's _fire_and_reschedule.
            result = callback(now)
            if not cancelled[0]:
                self._schedule_at(now + interval, _fire_and_reschedule)
            return result

        self._schedule_at(self._clock + interval, _fire_and_reschedule)

        def _cancel() -> None:
            cancelled[0] = True

        return _cancel

    # ------------------------------------------------------------------
    # Task queue (fed by FakeHass.async_create_task)
    # ------------------------------------------------------------------

    def enqueue_task(self, coro: Any) -> None:
        """Enqueue a coroutine to be drained after the next callback fires."""
        self._task_queue.append(coro)

    def _drain_tasks(self) -> None:
        """Run all enqueued coroutines synchronously on the shared persistent loop.

        (Issue: architecture-reset Step 2 root-caused thousands of fresh
        asyncio.run() calls per process to a Windows ProactorEventLoop handle
        exhaustion that freezes the process at scale — see tools/sim_harness/_loop.py.
        The old RuntimeError/get_event_loop() fallback here existed for the
        "already inside a running loop" case that asyncio.run() can hit; running
        everything on one shared, not-currently-running persistent loop via
        run_coro() doesn't have that failure mode, so the fallback is removed.)
        """
        while self._task_queue:
            coro = self._task_queue.pop(0)
            try:
                run_coro(coro)
            except Exception as exc:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                self.callback_errors.append((self._clock, exc))

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
                result = entry.callback(self._clock)
                # Issue #476: some timer-registration targets (e.g. async_track_time_change
                # callbacks like coordinator._async_send_briefing/_async_morning_wakeup/
                # _async_bedtime) are ``async def`` methods, not @callback-decorated sync
                # functions. Real HA's async_run_hass_job schedules a coroutine-function job
                # via async_create_task rather than calling it inline — calling it directly
                # here only constructs the coroutine without running it, silently dropping
                # the callback's entire effect (confirmed via "coroutine ... was never
                # awaited" warnings on a scenario spanning past the 06:00 briefing time).
                if inspect.iscoroutine(result):
                    self.enqueue_task(result)
            except Exception as exc:  # noqa: BLE001 — don't let one bad callback stop the clock
                import traceback

                traceback.print_exc()
                self.callback_errors.append((self._clock, exc))
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
        """Patch automation.py's AND coordinator.py's timer/clock symbols.

        Yields ``self`` for convenience.  All patches are removed on exit.

        Patches applied to ``automation`` (engine-level, pre-existing):
          - ``async_call_later``  → self._schedule
          - ``callback``          → identity
          - ``dt_util.now``       → self.now
          - ``dt_util.utcnow``    → self.now
          - ``dt_util.as_local``  → identity

        Patches applied to ``coordinator`` (Issue #474 — coordinator-level
        Tier A coverage), same clock/callback shape plus the additional
        timer-registration helpers the coordinator uses that ``automation``
        does not:
          - ``async_call_later``            → self._schedule
          - ``async_track_time_change``     → self._schedule_daily
          - ``async_track_time_interval``   → self._schedule_interval
          - ``async_track_point_in_time``   → self._schedule_at
          - ``async_track_state_change_event`` → hass.add_state_listener
          - ``callback``                    → identity
          - ``dt_util.now``/``utcnow``/``as_local`` → same as automation
          - ``dt_util.parse_datetime``      → real ISO8601 parse (Issue #476)

        Issue #476: ``dt_util.parse_datetime`` was NOT previously patched —
        it comes from ``ha_stubs.py``'s mocked ``homeassistant.util.dt``
        module, so any call returned a ``MagicMock``. Subtracting that from a
        real ``dt_util.now()`` datetime silently produces another
        ``MagicMock`` (Python tries ``MagicMock.__rsub__``), which then
        crashes with ``TypeError: '>' not supported between instances of
        'MagicMock' and 'int'`` the moment code compares the "elapsed"
        result against a real number — found via
        ``_check_hvac_stabilization`` crashing during a coordinator-mode
        scenario. This function is called at 19+ sites in coordinator.py
        alone, including the stuck-grace detection logic
        (``coordinator.py:1668,6623-6624``) that grace-period scenarios
        depend on directly — unpatched, any such scenario risks silently
        wrong behavior or a swallowed exception (``FakeScheduler`` catches
        broadly and records to ``callback_errors``), not just this one crash.
        """
        # We need to patch the dt_util *object* that automation.py/coordinator.py
        # imported, not the original module.  Both modules do:
        #   from homeassistant.util import dt as dt_util
        # so the name ``dt_util`` lives in each module's own namespace.
        # We patch individual attributes on that object via the module path.

        def _fake_async_call_later(hass: Any, delay: float, cb: Any) -> Any:
            return self._schedule(delay, cb)

        def _fake_parse_datetime(dt_str: str | None) -> datetime | None:
            if not dt_str:
                return None
            try:
                return datetime.fromisoformat(dt_str)
            except (ValueError, TypeError):
                return None

        def _fake_async_track_time_change(
            hass: Any,
            action: Any,
            hour: int | None = None,
            minute: int | None = None,
            second: int | None = None,
        ) -> Any:
            return self._schedule_daily(hour, minute, second, action)

        def _fake_async_track_time_interval(hass: Any, action: Any, interval: timedelta) -> Any:
            return self._schedule_interval(interval, action)

        def _fake_async_track_point_in_time(hass: Any, action: Any, point_in_time: datetime) -> Any:
            return self._schedule_at(point_in_time, action)

        def _fake_async_track_state_change_event(hass: Any, entity_ids: Any, action: Any) -> Any:
            return hass.add_state_listener(entity_ids, action)

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
            patch(
                "custom_components.climate_advisor.automation.dt_util.parse_datetime",
                side_effect=_fake_parse_datetime,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.async_call_later",
                side_effect=_fake_async_call_later,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.async_track_time_change",
                side_effect=_fake_async_track_time_change,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.async_track_time_interval",
                side_effect=_fake_async_track_time_interval,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.async_track_point_in_time",
                side_effect=_fake_async_track_point_in_time,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.async_track_state_change_event",
                side_effect=_fake_async_track_state_change_event,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.callback",
                side_effect=lambda fn: fn,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                side_effect=lambda: self._clock,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.utcnow",
                side_effect=lambda: self._clock,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.parse_datetime",
                side_effect=_fake_parse_datetime,
            ),
        ):
            yield self
