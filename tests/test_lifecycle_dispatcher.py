"""Tests for Issue #633: the generic cross-lifecycle event dispatcher.

Proven with synthetic controllers/events, independent of any real lifecycle —
the dispatcher must be correct before any real FSM is built on top of it, the
same "generic mechanism proven first" posture as ``_mirror_to_shadow()``/
``_sync_shadow_inputs()`` (Issue #613/#615).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.lifecycle_dispatcher import LifecycleDispatcher
from custom_components.climate_advisor.lifecycle_events import LifecycleEvent, LifecycleEventType

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _event(event_type: LifecycleEventType, source: str = "test_source") -> LifecycleEvent:
    return LifecycleEvent(event_type=event_type, source=source, at=_NOW)


class TestRegistration:
    def test_consumes_without_on_event_raises(self) -> None:
        dispatcher = LifecycleDispatcher()
        with pytest.raises(ValueError, match="no on_event callback"):
            dispatcher.register("a", consumes=frozenset({LifecycleEventType.GRACE_STARTED}))

    def test_emits_only_does_not_require_on_event(self) -> None:
        dispatcher = LifecycleDispatcher()
        dispatcher.register("a", emits=frozenset({LifecycleEventType.GRACE_STARTED}))  # must not raise


class TestEmitRouting:
    def test_routes_to_registered_consumer(self) -> None:
        dispatcher = LifecycleDispatcher()
        received: list[LifecycleEvent] = []
        dispatcher.register(
            "consumer",
            consumes=frozenset({LifecycleEventType.GRACE_STARTED}),
            on_event=received.append,
        )
        event = _event(LifecycleEventType.GRACE_STARTED)
        dispatcher.emit(event)
        assert received == [event]

    def test_does_not_route_to_non_matching_consumer(self) -> None:
        dispatcher = LifecycleDispatcher()
        received: list[LifecycleEvent] = []
        dispatcher.register(
            "consumer",
            consumes=frozenset({LifecycleEventType.GRACE_ENDED}),
            on_event=received.append,
        )
        dispatcher.emit(_event(LifecycleEventType.GRACE_STARTED))
        assert received == []

    def test_routes_to_multiple_consumers_fan_out(self) -> None:
        dispatcher = LifecycleDispatcher()
        received_a: list[LifecycleEvent] = []
        received_b: list[LifecycleEvent] = []
        dispatcher.register(
            "a", consumes=frozenset({LifecycleEventType.DOOR_PAUSE_STARTED}), on_event=received_a.append
        )
        dispatcher.register(
            "b", consumes=frozenset({LifecycleEventType.DOOR_PAUSE_STARTED}), on_event=received_b.append
        )
        event = _event(LifecycleEventType.DOOR_PAUSE_STARTED)
        dispatcher.emit(event)
        assert received_a == [event]
        assert received_b == [event]

    def test_one_consumer_exception_does_not_block_others(self) -> None:
        dispatcher = LifecycleDispatcher()
        received: list[LifecycleEvent] = []

        def _boom(_event: LifecycleEvent) -> None:
            raise RuntimeError("consumer blew up")

        dispatcher.register("broken", consumes=frozenset({LifecycleEventType.OVERRIDE_CONFIRMED}), on_event=_boom)
        dispatcher.register(
            "healthy", consumes=frozenset({LifecycleEventType.OVERRIDE_CONFIRMED}), on_event=received.append
        )
        event = _event(LifecycleEventType.OVERRIDE_CONFIRMED)
        dispatcher.emit(event)  # must not raise
        assert received == [event]


class TestEventLog:
    def test_logs_every_emitted_event_in_order(self) -> None:
        dispatcher = LifecycleDispatcher()
        e1 = _event(LifecycleEventType.GRACE_STARTED)
        e2 = _event(LifecycleEventType.GRACE_ENDED)
        dispatcher.emit(e1)
        dispatcher.emit(e2)
        assert dispatcher.event_log == [e1, e2]

    def test_logs_even_with_zero_consumers(self) -> None:
        dispatcher = LifecycleDispatcher()
        event = _event(LifecycleEventType.OVERRIDE_CLEARED)
        dispatcher.emit(event)
        assert dispatcher.event_log == [event]


class TestRegistryCompleteness:
    def test_empty_registry_is_complete(self) -> None:
        dispatcher = LifecycleDispatcher()
        assert dispatcher.check_registry_completeness() == []

    def test_emitted_with_consumer_is_complete(self) -> None:
        dispatcher = LifecycleDispatcher()
        dispatcher.register("emitter", emits=frozenset({LifecycleEventType.GRACE_STARTED}))
        dispatcher.register("consumer", consumes=frozenset({LifecycleEventType.GRACE_STARTED}), on_event=lambda e: None)
        assert dispatcher.check_registry_completeness() == []

    def test_positive_control_unconsumed_emit_is_caught(self) -> None:
        """Proves the completeness check actually catches a real gap — not
        just passing vacuously. This is the exact bug shape #615/#631 hit
        (a real write with no corresponding read/mirror), reproduced
        deliberately here to prove the check would have caught it."""
        dispatcher = LifecycleDispatcher()
        dispatcher.register("emitter", emits=frozenset({LifecycleEventType.DOOR_PAUSE_ENDED}))
        problems = dispatcher.check_registry_completeness()
        assert len(problems) == 1
        assert "door_pause_ended" in problems[0]

    def test_multiple_unconsumed_emits_all_reported(self) -> None:
        dispatcher = LifecycleDispatcher()
        dispatcher.register(
            "emitter",
            emits=frozenset({LifecycleEventType.GRACE_STARTED, LifecycleEventType.OVERRIDE_CLEARED}),
        )
        problems = dispatcher.check_registry_completeness()
        assert len(problems) == 2
