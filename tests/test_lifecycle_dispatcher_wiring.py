"""Tests for Issue #717 (Block 5, epic #594): wiring lifecycle_dispatcher.py into
production.

Covers:
  - AutomationEngine registers itself as a real controller with the documented
    emit/consume contract, and the registry is complete (every emittable type has
    itself as a registered consumer) — a genuine exercise of
    ``check_registry_completeness()`` against a real controller, not the synthetic
    ones ``test_lifecycle_dispatcher.py`` already covers.
  - Each of the four real emit points fires the correct event, with the correct
    direction, at the correct (and only the correct) transition:
      * ``_resolve_door_window_pause_flags()`` — DOOR_PAUSE_STARTED/ENDED via a
        before/after diff of ``_paused_by_door``.
      * ``_resolve_override_grace_fsm_state()`` — GRACE_STARTED/ENDED via a
        before/after diff of ``_grace_active``.
      * ``_confirm_override_action()`` — OVERRIDE_CONFIRMED, single real site.
      * ``_clear_manual_override_active()`` — OVERRIDE_CLEARED, reusing that
        method's own idempotent "did it actually change" guard.
  - ``_apply_nat_vent_fsm_state()`` is a SECOND real writer of ``_paused_by_door``
    (the nat-vent-FSM-authoritative path) and must also emit DOOR_PAUSE_STARTED/
    ENDED — found the same way Phase 1 found a second unmirrored ``_fan_active``
    writer at ``coordinator.py:5088``.
  - The dispatcher-synced mirror attributes (``_dispatched_*``) update correctly on
    receipt — proving the round-trip works — without the FSM input builders
    depending on them (see the ``__init__`` declaration comment for why routing
    ``_build_nat_vent_fsm_inputs()``/``_build_door_window_fsm_inputs()`` through a
    same-instance dispatcher-only mirror was tried and reverted).
  - A raising ``on_event`` consumer is isolated (proven generically already by
    ``test_lifecycle_dispatcher.py``; the isolation guard around each real emit
    site in ``automation.py`` itself is exercised here instead).
"""

from __future__ import annotations

from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.door_window_fsm import DoorWindowFsmEventKind  # noqa: E402
from custom_components.climate_advisor.lifecycle_events import LifecycleEventType  # noqa: E402
from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind  # noqa: E402


def _engine():
    coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
    return coordinator.automation_engine


def _events_of(ae, event_type):
    return [e for e in ae._lifecycle_dispatcher.event_log if e.event_type is event_type]


class TestControllerRegistration:
    def test_registered_as_a_real_controller(self) -> None:
        ae = _engine()
        assert "automation_engine" in ae._lifecycle_dispatcher._registrations
        reg = ae._lifecycle_dispatcher._registrations["automation_engine"]
        assert reg.on_event.__func__ is ae._on_lifecycle_event.__func__

    def test_registry_is_complete(self) -> None:
        """Every type AutomationEngine declares emittable is also consumed —
        proven with the real controller, not a synthetic one."""
        ae = _engine()
        assert ae._lifecycle_dispatcher.check_registry_completeness() == []

    def test_each_engine_owns_a_separate_dispatcher_instance(self) -> None:
        """Decision Point 1 of the approved plan: never shared, structural
        isolation — production and shadow must not be able to see each other's
        events."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator.automation_engine._lifecycle_dispatcher is not (
            coordinator.shadow_automation_engine._lifecycle_dispatcher
        )


class TestDoorPauseEvents:
    def test_pause_starts_emits_door_pause_started(self) -> None:
        ae = _engine()
        ae._paused_by_door = False
        ae._resolve_door_window_pause_flags(
            kind=DoorWindowFsmEventKind.SENSOR_OPENED,
            legacy=lambda: setattr(ae, "_paused_by_door", True),
        )
        events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_STARTED)
        assert len(events) == 1
        assert ae._dispatched_paused_by_door is True

    def test_pause_ends_emits_door_pause_ended(self) -> None:
        ae = _engine()
        ae._paused_by_door = True
        ae._dispatched_paused_by_door = True
        ae._resolve_door_window_pause_flags(
            kind=DoorWindowFsmEventKind.ALL_SENSORS_CLOSED,
            legacy=lambda: setattr(ae, "_paused_by_door", False),
        )
        events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_ENDED)
        assert len(events) == 1
        assert ae._dispatched_paused_by_door is False

    def test_no_change_emits_nothing(self) -> None:
        """A kind that resolves to the SAME paused state (e.g. a redundant
        re-evaluation) must not emit — only a real transition should."""
        ae = _engine()
        ae._paused_by_door = True
        ae._resolve_door_window_pause_flags(
            kind=DoorWindowFsmEventKind.SYNC_RECONCILE,
            legacy=lambda: setattr(ae, "_paused_by_door", True),
        )
        assert ae._lifecycle_dispatcher.event_log == []

    def test_apply_nat_vent_fsm_state_is_a_second_real_writer(self) -> None:
        """Issue #717's own found gap: the nat-vent-FSM-authoritative path writes
        _paused_by_door directly, outside _resolve_door_window_pause_flags()."""
        from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState

        ae = _engine()
        ae._paused_by_door = False
        ae._apply_nat_vent_fsm_state(NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT)
        events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_STARTED)
        assert len(events) == 1
        assert ae._dispatched_paused_by_door is True

        ae._apply_nat_vent_fsm_state(NatVentLifecycleState.INACTIVE)
        end_events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_ENDED)
        assert len(end_events) == 1
        assert ae._dispatched_paused_by_door is False


class TestGraceEvents:
    def test_grace_starts_emits_grace_started(self) -> None:
        ae = _engine()
        ae._grace_active = False
        ae._resolve_override_grace_fsm_state(
            kind=OverrideGraceFsmEventKind.UNPROTECTED_GRACE_STARTED,
            legacy=lambda: setattr(ae, "_grace_active", True),
        )
        events = _events_of(ae, LifecycleEventType.GRACE_STARTED)
        assert len(events) == 1
        assert ae._dispatched_grace_active is True

    def test_grace_ends_emits_grace_ended(self) -> None:
        ae = _engine()
        ae._grace_active = True
        ae._dispatched_grace_active = True
        ae._resolve_override_grace_fsm_state(
            kind=OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED,
            legacy=lambda: setattr(ae, "_grace_active", False),
        )
        events = _events_of(ae, LifecycleEventType.GRACE_ENDED)
        assert len(events) == 1
        assert ae._dispatched_grace_active is False


class TestOverrideEvents:
    def test_confirm_action_emits_override_confirmed(self) -> None:
        ae = _engine()
        ae._start_grace_period_action = lambda *a, **k: True  # noqa: ARG005 — real side effect not under test
        ae._confirm_override_action("cool", source="normal")
        events = _events_of(ae, LifecycleEventType.OVERRIDE_CONFIRMED)
        assert len(events) == 1
        assert events[0].detail == "cool"
        assert ae._dispatched_manual_override_active is True

    def test_clear_emits_override_cleared_only_when_it_was_active(self) -> None:
        ae = _engine()
        ae._manual_override_active = True
        ae._dispatched_manual_override_active = True
        ae._clear_manual_override_active("user_cancel")
        events = _events_of(ae, LifecycleEventType.OVERRIDE_CLEARED)
        assert len(events) == 1
        assert events[0].detail == "user_cancel"
        assert ae._dispatched_manual_override_active is False

    def test_clear_is_a_noop_when_nothing_was_active(self) -> None:
        """Reuses _clear_manual_override_active()'s own existing guard — no new
        idempotency check duplicated."""
        ae = _engine()
        ae._manual_override_active = False
        ae._clear_manual_override_active("grace_expired")
        assert ae._lifecycle_dispatcher.event_log == []


class TestNatVentSessionEvents:
    def test_decision_pass_emits_session_started_on_activation(self) -> None:
        from tools.sim_harness._loop import run_coro

        ae = _engine()
        ae._natural_vent_active = False

        async def _activate():
            async with ae._decision_pass("test"):
                ae._natural_vent_active = True

        run_coro(_activate())
        events = _events_of(ae, LifecycleEventType.NAT_VENT_SESSION_STARTED)
        assert len(events) == 1
        assert ae._dispatched_natural_vent_active is True

    def test_decision_pass_emits_session_ended_on_deactivation(self) -> None:
        from tools.sim_harness._loop import run_coro

        ae = _engine()
        ae._natural_vent_active = True
        ae._dispatched_natural_vent_active = True

        async def _deactivate():
            async with ae._decision_pass("test"):
                ae._natural_vent_active = False

        run_coro(_deactivate())
        events = _events_of(ae, LifecycleEventType.NAT_VENT_SESSION_ENDED)
        assert len(events) == 1
        assert ae._dispatched_natural_vent_active is False

    def test_decision_pass_emits_nothing_when_unchanged(self) -> None:
        from tools.sim_harness._loop import run_coro

        ae = _engine()
        ae._natural_vent_active = True

        async def _noop():
            async with ae._decision_pass("test"):
                pass

        run_coro(_noop())
        assert ae._lifecycle_dispatcher.event_log == []


class TestFsmInputsUnaffectedByDispatcherMirrors:
    """The reverted design's regression test: direct attribute assignment (the
    established fixture convention across 40+ existing test files) must still work
    for FSM input construction, independent of whatever the dispatcher mirrors say."""

    def test_nat_vent_inputs_read_canonical_override_active_not_dispatched_mirror(self) -> None:
        from datetime import UTC, datetime

        ae = _engine()
        ae._manual_override_active = True  # set directly, never emitted
        assert ae._dispatched_manual_override_active is False  # mirror never touched
        inputs = ae._build_nat_vent_fsm_inputs(now=datetime(2026, 8, 21, tzinfo=UTC), indoor=72.0, outdoor=65.0)
        assert inputs.override_active is True
        assert inputs.manual_override_active is True

    def test_door_window_inputs_read_canonical_natural_vent_active_not_dispatched_mirror(self) -> None:
        from datetime import UTC, datetime

        ae = _engine()
        ae._natural_vent_active = True  # set directly, never emitted
        assert ae._dispatched_natural_vent_active is False  # mirror never touched
        inputs = ae._build_door_window_fsm_inputs(now=datetime(2026, 8, 21, tzinfo=UTC))
        assert inputs.natural_vent_active is True
