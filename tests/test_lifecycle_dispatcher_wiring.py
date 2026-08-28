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

from custom_components.climate_advisor.const import CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE  # noqa: E402
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


class TestDoorPauseEvents:
    def test_pause_starts_emits_door_pause_started(self) -> None:
        ae = _engine()
        ae._paused_by_door = False
        ae._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.SENSOR_OPENED)
        events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_STARTED)
        assert len(events) == 1
        assert ae._dispatched_paused_by_door is True

    def test_pause_ends_emits_door_pause_ended(self) -> None:
        ae = _engine()
        ae._paused_by_door = True
        ae._dispatched_paused_by_door = True
        ae._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.ALL_SENSORS_CLOSED)
        events = _events_of(ae, LifecycleEventType.DOOR_PAUSE_ENDED)
        assert len(events) == 1
        assert ae._dispatched_paused_by_door is False

    def test_no_change_emits_nothing(self) -> None:
        """A kind that resolves to the SAME paused state (e.g. a redundant
        re-evaluation) must not emit — only a real transition should."""
        ae = _engine()
        ae._paused_by_door = True
        ae._resolve_door_window_pause_flags(kind=DoorWindowFsmEventKind.SYNC_RECONCILE)
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
        # Issue #757 Phase 6 Step 3: _resolve_override_grace_fsm_state() is now
        # unconditionally FSM-authoritative (no more legacy= closure) — the real
        # transition() naturally lands UNPROTECTED_GRACE_STARTED on
        # ACTIVE_UNPROTECTED (grace_active=True) given this engine's default config
        # (manual/automation grace both enabled), which is exactly what this test
        # needs for the before/after diff it's proving.
        ae._resolve_override_grace_fsm_state(kind=OverrideGraceFsmEventKind.UNPROTECTED_GRACE_STARTED)
        events = _events_of(ae, LifecycleEventType.GRACE_STARTED)
        assert len(events) == 1
        assert ae._dispatched_grace_active is True

    def test_grace_ends_emits_grace_ended(self) -> None:
        ae = _engine()
        ae._grace_active = True
        ae._dispatched_grace_active = True
        # Issue #757 Phase 6 Step 3: no more legacy= closure — GRACE_TIMER_EXPIRED
        # always lands the real transition() on (IDLE, NONE) i.e. grace_active=False,
        # which is exactly what this test needs for the before/after diff it's proving.
        ae._resolve_override_grace_fsm_state(kind=OverrideGraceFsmEventKind.GRACE_TIMER_EXPIRED)
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

    def test_door_window_inputs_read_canonical_whf_owns_hvac_not_dispatched_mirror(self) -> None:
        """Issue #722: a mid-implementation correction to the original plan draft.

        The original draft routed this input through _dispatched_whf_owns_hvac.
        Reverted before shipping — test_fan_control.py/
        test_whole_house_fan_hvac_suppression.py set engine._pre_fan_hvac_mode
        directly, bypassing the dispatcher; routing this input through the mirror
        would reproduce the exact regression the FSM-builder reversion above (for
        natural_vent_active/manual_override_active) already fixed once.
        """
        from datetime import UTC, datetime

        ae = _engine()
        ae.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae._pre_fan_hvac_mode = "cool"  # set directly, never emitted
        assert ae._dispatched_whf_owns_hvac is False  # mirror never touched
        inputs = ae._build_door_window_fsm_inputs(now=datetime(2026, 8, 21, tzinfo=UTC))
        assert inputs.whf_owns_hvac is True


class TestPausedByDoorGuardStaysCanonical:
    """Issue #721: same mid-implementation correction as above, for the guard in
    handle_manual_override_during_pause()/resume_from_pause(). The original plan
    draft re-sourced both to _dispatched_paused_by_door; reverted before shipping
    because test_resume_from_pause.py/test_manual_override_respect.py/
    test_bedtime_override.py/test_override_dedup.py all set
    engine._paused_by_door = True directly then call these methods immediately —
    re-sourcing the guard would silently no-op every one of those tests' real
    scenario, exactly the regression Issue #717's own FSM-builder wiring hit once
    already for a different field.
    """

    def test_resume_from_pause_proceeds_on_canonical_flag_alone(self) -> None:
        from tools.sim_harness._loop import run_coro

        ae = _engine()
        ae._paused_by_door = True  # set directly, never emitted
        assert ae._dispatched_paused_by_door is False  # mirror never touched
        ae._current_classification = None
        result = run_coro(ae.resume_from_pause())
        assert ae._paused_by_door is False  # guard did not silently no-op
        assert result is None or isinstance(result, str)

    def test_handle_manual_override_during_pause_proceeds_on_canonical_flag_alone(self) -> None:
        from tools.sim_harness._loop import run_coro

        ae = _engine()
        ae._paused_by_door = True  # set directly, never emitted
        assert ae._dispatched_paused_by_door is False  # mirror never touched
        run_coro(ae.handle_manual_override_during_pause(old_mode="off", new_mode="cool"))
        assert ae._paused_by_door is False  # guard did not silently no-op


class TestEmitBooleanTransitionHelper:
    """Issue #721/#722 DRY finding: _emit_boolean_transition() is now a shared
    dependency of 4 call sites (door/window, override/grace, nat-vent's second
    _paused_by_door writer, and WHF-suppression) rather than logic hand-rolled
    once in each — covered directly here rather than only indirectly through
    each of those 4 callers."""

    def test_emits_started_on_false_to_true(self) -> None:
        ae = _engine()
        ae._emit_boolean_transition(
            before=False,
            after=True,
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail="unit-test",
            caller="test",
        )
        events = _events_of(ae, LifecycleEventType.WHF_HVAC_SUPPRESSED)
        assert len(events) == 1
        assert events[0].detail == "unit-test"

    def test_emits_ended_on_true_to_false(self) -> None:
        ae = _engine()
        ae._emit_boolean_transition(
            before=True,
            after=False,
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail="unit-test",
            caller="test",
        )
        events = _events_of(ae, LifecycleEventType.WHF_HVAC_RELEASED)
        assert len(events) == 1

    def test_emits_nothing_on_no_change(self) -> None:
        ae = _engine()
        for value in (True, False):
            ae._emit_boolean_transition(
                before=value,
                after=value,
                started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
                ended=LifecycleEventType.WHF_HVAC_RELEASED,
                detail=None,
                caller="test",
            )
        assert ae._lifecycle_dispatcher.event_log == []

    def test_dispatcher_exception_is_isolated(self) -> None:
        ae = _engine()

        def _raise(_event):
            raise RuntimeError("boom")

        ae._lifecycle_dispatcher.emit = _raise
        # Must not raise — isolated per the try/except contract.
        ae._emit_boolean_transition(
            before=False,
            after=True,
            started=LifecycleEventType.WHF_HVAC_SUPPRESSED,
            ended=LifecycleEventType.WHF_HVAC_RELEASED,
            detail=None,
            caller="test",
        )


class TestWhfHvacSuppressionEvents:
    """Issue #722: the 4 real writers of _pre_fan_hvac_mode, all routed through the
    new _resolve_whf_hvac_suppression() chokepoint. 2 of the 4
    (_deactivate_fan()'s two release branches) were not named in #722's original
    text — found during investigation (Finding 2 of the approved plan)."""

    def test_suppress_hvac_for_whf_emits_suppressed(self) -> None:
        from tools.sim_harness._loop import run_coro

        async def _noop_set_hvac_mode(*_a, **_k):
            return None

        ae = _engine()
        ae.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae._pre_fan_hvac_mode = None
        ae._set_hvac_mode = _noop_set_hvac_mode  # real HVAC write not under test

        run_coro(ae._suppress_hvac_for_whf(reason="test"))
        events = _events_of(ae, LifecycleEventType.WHF_HVAC_SUPPRESSED)
        assert len(events) == 1
        assert ae._dispatched_whf_owns_hvac is True

    def test_release_and_reclassify_emits_released(self) -> None:
        ae = _engine()
        ae.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae._pre_fan_hvac_mode = "cool"
        ae._dispatched_whf_owns_hvac = True
        ae._natural_vent_active = False
        ae._get_fan_physical_state_callback = None
        ae._reclassify_callback = None
        ae._release_whf_and_reclassify(reason="test")
        events = _events_of(ae, LifecycleEventType.WHF_HVAC_RELEASED)
        assert len(events) == 1
        assert ae._dispatched_whf_owns_hvac is False

    def test_deactivate_fan_already_inactive_branch_emits_released(self) -> None:
        """Not named in #722's original text — found during investigation."""
        from tools.sim_harness._loop import run_coro

        async def _noop_set_hvac_mode(*_a, **_k):
            return None

        ae = _engine()
        ae.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae._fan_active = False
        ae._pre_fan_hvac_mode = "cool"
        ae._dispatched_whf_owns_hvac = True
        ae._set_hvac_mode = _noop_set_hvac_mode

        run_coro(ae._deactivate_fan(reason="test", restore_hvac=True, release_suppression=True))
        events = _events_of(ae, LifecycleEventType.WHF_HVAC_RELEASED)
        assert len(events) == 1
        assert ae._dispatched_whf_owns_hvac is False

    def test_no_change_emits_nothing(self) -> None:
        ae = _engine()
        ae.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae._pre_fan_hvac_mode = None
        ae._natural_vent_active = False
        ae._get_fan_physical_state_callback = None
        ae._reclassify_callback = None
        ae._release_whf_and_reclassify(reason="test")  # already None — no-op
        assert ae._lifecycle_dispatcher.event_log == []
