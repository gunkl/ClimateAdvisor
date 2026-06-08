"""fake_hass — lightweight Home Assistant stand-in for headless engine testing.

FakeHass intercepts every HA service call the AutomationEngine makes and
appends a structured record to ``action_log``. It also provides the minimal
state-query API the engine uses via ``hass.states.get()``.

FakeScheduler (see fake_scheduler.py) is wired in after construction via
``fake_hass.set_scheduler(scheduler)`` so that ``async_create_task`` coroutines
are handed off to the virtual clock rather than dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FakeState:
    """Minimal state object returned by FakeHass.states.get()."""

    state: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)


class _FakeServices:
    """Intercept point for all hass.services.async_call() invocations."""

    def __init__(self, action_log: list[dict], clock_fn: Any) -> None:
        self._action_log = action_log
        self._clock_fn = clock_fn  # callable → current sim datetime

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        blocking: bool = False,
        **kw: Any,
    ) -> None:
        """Record the service call; do nothing else."""
        ts: datetime | None = None
        with contextlib.suppress(Exception):
            ts = self._clock_fn()
        self._action_log.append(
            {
                "domain": domain,
                "service": service,
                "data": dict(data or {}),
                "ts": ts,
            }
        )


class _FakeStates:
    """Minimal state registry backed by an injected dict."""

    def __init__(self) -> None:
        self._states: dict[str, FakeState] = {}

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    def set(self, entity_id: str, state: FakeState) -> None:
        self._states[entity_id] = state

    def set_simple(self, entity_id: str, state_str: str, attributes: dict | None = None) -> None:
        self._states[entity_id] = FakeState(state=state_str, attributes=attributes or {})


class FakeHass:
    """Minimal Home Assistant stand-in for headless AutomationEngine tests.

    Usage::

        scheduler = FakeScheduler(start_time)
        hass = FakeHass(clock_fn=scheduler.now)
        hass.set_scheduler(scheduler)
        # Inject entity states:
        hass.states.set_simple("climate.hallway", "cool", {"temperature": 76})
    """

    def __init__(self, clock_fn: Any | None = None) -> None:
        """Create a FakeHass.

        Args:
            clock_fn: Zero-argument callable that returns the current virtual
                      ``datetime``.  Defaults to ``datetime.now`` if not given
                      (real wall clock — only useful for non-time-sensitive tests).
        """
        self._clock_fn = clock_fn or datetime.now
        self.action_log: list[dict] = []
        self.states = _FakeStates()
        self.services = _FakeServices(self.action_log, self._clock_fn)
        self._scheduler: Any | None = None  # set via set_scheduler()

        # Minimal config stub the engine reads via hass.config.config_dir
        class _Config:
            config_dir = "/tmp/fake_ha_config"

        self.config = _Config()

    # ------------------------------------------------------------------
    # Scheduler linkage
    # ------------------------------------------------------------------

    def set_scheduler(self, scheduler: Any) -> None:
        """Wire in the FakeScheduler so async_create_task coroutines are driven."""
        self._scheduler = scheduler

    # ------------------------------------------------------------------
    # HA async helpers used by AutomationEngine
    # ------------------------------------------------------------------

    def async_create_task(self, coro: Any) -> None:
        """Hand a coroutine to the scheduler; never silently drop it."""
        if self._scheduler is not None:
            self._scheduler.enqueue_task(coro)
        else:
            # No scheduler attached — run immediately so nothing is silently lost.
            # This is the fallback for tests that don't need timer fidelity.
            try:
                asyncio.run(coro)
            except RuntimeError:
                # Already inside an event loop (e.g. pytest-asyncio) — schedule it
                loop = asyncio.get_event_loop()
                loop.create_task(coro)

    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        """Run a blocking function synchronously (no thread pool needed in tests)."""
        return fn(*args)
