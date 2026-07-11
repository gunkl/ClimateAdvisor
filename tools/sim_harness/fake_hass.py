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
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class FakeState:
    """Minimal state object returned by FakeHass.states.get()."""

    state: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeEvent:
    """Minimal stand-in for homeassistant.core.Event.

    Coordinator listener callbacks (e.g. ``_async_thermostat_changed``) read
    ``event.data.get("entity_id"/"old_state"/"new_state")`` — this is the only
    shape that matters for dispatch fidelity.

    ``context`` (Issue #482) mirrors real HA's ``Event.context`` — every real
    state-changed event carries one. When a scenario dispatches a state change
    via ``_FakeServices.async_call(..., context=...)`` (i.e. production issued
    the service call and passed its own ``Context``), that same context is
    threaded through to the resulting ``FakeEvent`` so
    ``_async_fan_entity_changed()``'s context-based provenance check can be
    exercised faithfully. Externally-injected state changes (a scenario calling
    ``states.async_set()`` directly to model a real user/manual action) have no
    such context, so ``context`` defaults to ``None`` — correctly modeling "no
    CA attribution available" for a genuine external actor.
    """

    event_type: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    context: Any = None


class _FakeServices:
    """Intercept point for all hass.services.async_call() invocations.

    Besides recording every call in ``action_log``, this applies a
    **state-feedback loop**: climate/fan commands update the target entity's
    ``FakeState`` so that production code which reads the thermostat back via
    ``hass.states.get(...)`` (e.g. ``handle_occupancy_away`` checks the actual
    HVAC mode before choosing setback_heat vs setback_cool) sees the commanded
    state — exactly as real HA would, and as the legacy ``SimState`` mirrors by
    mutating its own state after each decision. Without this, read-backs return
    the stale initial state and production diverges for the wrong reason.
    """

    def __init__(self, action_log: list[dict], clock_fn: Any, states: _FakeStates) -> None:
        self._action_log = action_log
        self._clock_fn = clock_fn  # callable → current sim datetime
        self._states = states

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        blocking: bool = False,
        context: Any = None,
        **kw: Any,
    ) -> None:
        """Record the service call, then apply the state-feedback loop.

        ``context`` (Issue #482): when production passes its own ``Context``
        (see ``automation.py``'s ``_call_fan_service_with_context``), it is
        threaded through to the resulting state's dispatched ``FakeEvent`` so
        the coordinator's context-based provenance check has real data to
        compare against, matching real HA's behavior of carrying the
        originating service call's context onto the entity's state write.
        """
        data = dict(data or {})
        ts: datetime | None = None
        with contextlib.suppress(Exception):
            ts = self._clock_fn()
        self._action_log.append(
            {
                "domain": domain,
                "service": service,
                "data": data,
                "ts": ts,
                "context": context,
            }
        )
        self._apply_state_feedback(domain, service, data, context=context)

    def _apply_state_feedback(self, domain: str, service: str, data: dict, context: Any = None) -> None:
        """Reflect a command into the target entity's FakeState (real-HA behaviour).

        Issue #474: dispatches via ``states.async_set()`` rather than mutating
        the ``FakeState`` in place. In real HA, a CA-issued service call causes
        the real thermostat integration to update its own state and fire a
        real ``state_changed`` event — which DOES reach a coordinator's
        ``_async_thermostat_changed`` listener. That round-trip is exactly
        what the expected-confirmation guard (``_is_expected_confirmation`` /
        ``_last_commanded_hvac_mode``) exists to filter out as "not a user
        override." Silently mutating state in place (the old behavior) would
        make that guard untestable — no listener would ever see CA's own
        commands. For engine-only scenarios (no coordinator constructed, no
        listeners registered), this dispatch is a no-op — same behavior as
        before.
        """
        entity_id = data.get("entity_id")
        if isinstance(entity_id, list):
            entity_id = entity_id[0] if entity_id else None
        if not entity_id:
            return

        existing = self._states.get(entity_id)
        state_str = existing.state if existing is not None else "off"
        attrs = dict(existing.attributes) if existing is not None else {}

        if domain == "climate":
            if service == "set_hvac_mode" and "hvac_mode" in data:
                state_str = data["hvac_mode"]
            elif service == "set_temperature":
                if "hvac_mode" in data:
                    state_str = data["hvac_mode"]
                if "temperature" in data:
                    attrs["temperature"] = data["temperature"]
                if "target_temp_low" in data:
                    attrs["target_temp_low"] = data["target_temp_low"]
                if "target_temp_high" in data:
                    attrs["target_temp_high"] = data["target_temp_high"]
            elif service == "set_fan_mode" and "fan_mode" in data:
                attrs["fan_mode"] = data["fan_mode"]
        elif domain in ("fan", "switch"):
            if service == "turn_on":
                state_str = "on"
            elif service == "turn_off":
                state_str = "off"

        self._states.async_set(entity_id, state_str, attrs, context=context)


class _FakeStates:
    """Minimal state registry backed by an injected dict.

    ``set()``/``set_simple()`` mutate silently (engine-only fidelity — no
    listener is invoked). ``async_set()`` additionally dispatches to any
    listeners registered via ``FakeHass.add_state_listener()`` for this
    entity_id, matching what real HA's ``async_track_state_change_event``
    delivers. Scenario-seed code should use ``set()``; scenario *event*
    injection that must reach a real coordinator listener should use
    ``async_set()``.
    """

    def __init__(self, dispatch_fn: Any | None = None) -> None:
        self._states: dict[str, FakeState] = {}
        self._dispatch_fn = dispatch_fn

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    def set(self, entity_id: str, state: FakeState) -> None:
        self._states[entity_id] = state

    def set_simple(self, entity_id: str, state_str: str, attributes: dict | None = None) -> None:
        self._states[entity_id] = FakeState(state=state_str, attributes=attributes or {})

    def async_set(self, entity_id: str, state_str: str, attributes: dict | None = None, context: Any = None) -> None:
        """Set a new state and dispatch a state-changed event to real listeners.

        ``context`` (Issue #482): forwarded to the dispatched event so
        listeners can exercise context-based provenance. Callers that model an
        external/manual state change (not a CA-issued service call) should
        omit it — ``None`` correctly represents "no CA attribution available."
        """
        old_state = self._states.get(entity_id)
        new_state = FakeState(state=state_str, attributes=attributes or {})
        self._states[entity_id] = new_state
        if self._dispatch_fn is not None:
            self._dispatch_fn(entity_id, old_state, new_state, context=context)


class _FakeBus:
    """Minimal event bus: ``async_listen`` / ``async_listen_once`` / ``async_fire``.

    Covers the coordinator's two ``hass.bus.async_listen(...)`` registrations
    (``EVENT_CALL_SERVICE``, ``EVENT_HOMEASSISTANT_STOP``) so
    ``async_setup()`` doesn't raise ``AttributeError`` on construction, and
    supports scenarios that want to fire those events directly.
    """

    def __init__(self, task_runner: Any | None = None) -> None:
        self._listeners: dict[str, list[Any]] = {}
        self._task_runner = task_runner  # optional FakeHass.async_create_task, for async listeners

    def async_listen(self, event_type: str, callback: Any) -> Any:
        self._listeners.setdefault(event_type, []).append(callback)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners[event_type].remove(callback)

        return _remove

    def async_listen_once(self, event_type: str, callback: Any) -> Any:
        remove_holder: list[Any] = []

        def _wrapped(event: Any) -> None:
            if remove_holder:
                remove_holder[0]()
            callback(event)

        remove_holder.append(self.async_listen(event_type, _wrapped))
        return remove_holder[0]

    def async_fire(self, event_type: str, event_data: dict | None = None) -> None:
        """Fire event_type; async listeners are scheduled via task_runner (see _dispatch_state_change)."""
        event = FakeEvent(event_type=event_type, data=event_data or {})
        for cb in list(self._listeners.get(event_type, [])):
            result = cb(event)
            if inspect.iscoroutine(result) and self._task_runner is not None:
                self._task_runner(result)


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
        self._state_listeners: dict[str, list[Any]] = {}
        self.states = _FakeStates(dispatch_fn=self._dispatch_state_change)
        self.services = _FakeServices(self.action_log, self._clock_fn, self.states)
        self.bus = _FakeBus(task_runner=self.async_create_task)
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
    # State-change listener registry (real dispatch, coordinator-fidelity)
    # ------------------------------------------------------------------

    def add_state_listener(self, entity_ids: str | list[str], callback: Any) -> Any:
        """Register a state-change listener; mirrors async_track_state_change_event.

        Returns a zero-arg cancel function, matching the real HA contract.
        """
        ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids)
        for eid in ids:
            self._state_listeners.setdefault(eid, []).append(callback)

        def _remove() -> None:
            for eid in ids:
                with contextlib.suppress(ValueError):
                    self._state_listeners[eid].remove(callback)

        return _remove

    def _dispatch_state_change(self, entity_id: str, old_state: Any, new_state: Any, context: Any = None) -> None:
        """Invoke every listener registered for entity_id with a synthesized Event.

        Coordinator listeners (e.g. ``_async_thermostat_changed``) are ``async
        def`` — real HA's ``async_track_state_change_event`` dispatch does not
        await an async listener inline; it schedules it via
        ``hass.async_create_task()`` (HA's ``async_run_hass_job`` for a
        coroutine-function job). Calling ``cb(event)`` on an async listener
        only constructs the coroutine — it must be handed to
        ``async_create_task()`` or it never runs (and Python warns about an
        unawaited coroutine). Sync ``@callback``-decorated listeners run
        immediately, matching real HA.
        """
        event = FakeEvent(
            event_type="state_changed",
            data={"entity_id": entity_id, "old_state": old_state, "new_state": new_state},
            context=context,
        )
        for cb in list(self._state_listeners.get(entity_id, [])):
            result = cb(event)
            if inspect.iscoroutine(result):
                self.async_create_task(result)

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
