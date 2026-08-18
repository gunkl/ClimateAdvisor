"""Tests for Issue #639's wiring increment: running the unified override/grace
joint-lifecycle FSM live against production's real readings, tracked as a third
comparison point alongside the existing production/shadow mirror comparison.

Issue #647: ``_evaluate_override_grace_fsm()`` now takes an explicit
``OverrideGraceFsmEventKind`` rather than deriving one from a mirrored method name —
see its own docstring and ``_OVERRIDE_GRACE_FSM_EVENT_KINDS``/
``_OVERRIDE_GRACE_FSM_EVENT_TYPE_MAP``. The 3 originally-mirrored entry points
(``handle_manual_override_during_pause``, ``resume_from_pause``,
``handle_fan_manual_override``) still resolve their event kind via the mirror-name
dict at the ``_mirror_to_shadow()`` call site; every override/grace *exit* path
(confirm/self-resolve/cancel/grace-expiry) is now also reachable — see
``tests/test_shadow_engine_coverage.py``'s registry. Mirrors
``tests/test_door_window_fsm_shadow_wiring.py``'s structure.
"""

from __future__ import annotations

import logging

from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD
from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind
from custom_components.climate_advisor.override_grace_lifecycle import GraceState, OverrideConfirmState
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator


def _run(coro):
    return run_coro(coro)


def _noop_shadow_methods(coordinator, *names: str) -> None:
    async def _noop(*args, **kwargs):
        return None

    for name in names:
        setattr(coordinator.shadow_automation_engine, name, _noop)


class TestFsmEvaluationScoping:
    def test_not_triggered_by_unrelated_mirrored_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[OverrideGraceFsmEventKind] = []
        coordinator._evaluate_override_grace_fsm = lambda event_kind: called.append(event_kind)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "apply_classification")
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert called == []

    def test_not_triggered_by_door_window_only_mirrored_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[OverrideGraceFsmEventKind] = []
        coordinator._evaluate_override_grace_fsm = lambda event_kind: called.append(event_kind)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert called == []

    def test_triggered_by_handle_manual_override_during_pause(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[OverrideGraceFsmEventKind] = []
        coordinator._evaluate_override_grace_fsm = lambda event_kind: called.append(event_kind)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_manual_override_during_pause")
        _run(coordinator._mirror_to_shadow("handle_manual_override_during_pause"))
        assert called == [OverrideGraceFsmEventKind.MANUAL_OVERRIDE_DURING_PAUSE]

    def test_triggered_by_resume_from_pause(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[OverrideGraceFsmEventKind] = []
        coordinator._evaluate_override_grace_fsm = lambda event_kind: called.append(event_kind)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))
        assert called == [OverrideGraceFsmEventKind.DASHBOARD_RESUME]

    def test_triggered_by_handle_fan_manual_override(self) -> None:
        """Issue #661: handle_fan_manual_override() must resolve to the dedicated
        FAN_OVERRIDE_DETECTED kind, not the thermostat path's OVERRIDE_DETECTED."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[OverrideGraceFsmEventKind] = []
        coordinator._evaluate_override_grace_fsm = lambda event_kind: called.append(event_kind)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_fan_manual_override")
        _run(coordinator._mirror_to_shadow("handle_fan_manual_override", fan_before="auto", fan_after="on"))
        assert called == [OverrideGraceFsmEventKind.FAN_OVERRIDE_DETECTED]


class TestFsmStateTracking:
    def test_starts_idle_none(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

    def test_resume_from_pause_lands_unprotected_grace(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_by_door = True

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)

    def test_manual_override_during_pause_confirm_enabled_lands_pending(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_by_door = True
        coordinator.config[CONF_OVERRIDE_CONFIRM_PERIOD] = 600

        _noop_shadow_methods(coordinator, "handle_manual_override_during_pause")
        _run(coordinator._mirror_to_shadow("handle_manual_override_during_pause"))

        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.PENDING, GraceState.NONE)

    def test_fan_manual_override_lands_protecting_regardless_of_confirm_period(self) -> None:
        """Issue #661: this is the exact live disagreement — a nonzero confirm
        period previously landed the FSM on (PENDING, NONE) for a fan override,
        when handle_fan_manual_override() never uses confirm-delay at all."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.config[CONF_OVERRIDE_CONFIRM_PERIOD] = 600

        _noop_shadow_methods(coordinator, "handle_fan_manual_override")
        _run(coordinator._mirror_to_shadow("handle_fan_manual_override", fan_before="auto", fan_after="on"))

        assert coordinator._override_grace_fsm_state == (
            OverrideConfirmState.IDLE,
            GraceState.ACTIVE_PROTECTING_OVERRIDE,
        )

    def test_manual_override_during_pause_confirm_disabled_lands_protecting(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_by_door = True
        coordinator.config[CONF_OVERRIDE_CONFIRM_PERIOD] = 0

        _noop_shadow_methods(coordinator, "handle_manual_override_during_pause")
        _run(coordinator._mirror_to_shadow("handle_manual_override_during_pause"))

        assert coordinator._override_grace_fsm_state == (
            OverrideConfirmState.IDLE,
            GraceState.ACTIVE_PROTECTING_OVERRIDE,
        )


class TestUnprotectedGraceStartedEventWiring:
    """Issue #672: closes the exact live disagreement observed 2026-08-17
    (production=idle/active_unprotected, fsm=idle/none) for fan-off/window-close/
    nat-vent-exit/drift-correction grace starts — none of these had ANY feed to the
    coordinator's diagnostic FSM tracker at all before this fix. Runs the real
    production on_fan_turned_off() call (not direct engine construction) so
    _emit_event_callback genuinely fires through coordinator._emit_event() ->
    _feed_lifecycle_fsms_from_event(), the same path the live bug went through.
    """

    def test_on_fan_turned_off_lands_unprotected_grace_via_event(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = build_headless_coordinator()
        # Pre-fix baseline: a fresh coordinator's tracked state starts (IDLE, NONE) and
        # nothing would ever move it for this trigger — this assertion is itself the
        # positive control (the "before" state fails it by construction).
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.NONE)

        _noop_shadow_methods(coordinator, "on_fan_turned_off")
        with scheduler.installed():
            coordinator.automation_engine.on_fan_turned_off(fan_before="on", fan_after="off")
            # Drains the real hass.async_create_task(start_min_fan_runtime_cycles())
            # fire-and-forget task on_fan_turned_off() schedules — same pattern
            # build_headless_coordinator()'s own startup sequence uses (Issue #476).
            scheduler.advance_to(scheduler.now())

        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)

    def test_on_fan_turned_off_no_longer_disagrees(self) -> None:
        coordinator, _fake_hass, scheduler, _event_log = build_headless_coordinator()

        # Matches coordinator.py's real call sites (production call immediately followed
        # by the mirror) — the diagnostic is only computed inside _mirror_to_shadow()'s
        # finally block, same pattern test_fan_manual_override_no_longer_disagrees uses.
        _noop_shadow_methods(coordinator, "on_fan_turned_off")
        with scheduler.installed():
            coordinator.automation_engine.on_fan_turned_off(fan_before="on", fan_after="off")
            _run(coordinator._mirror_to_shadow("on_fan_turned_off", fan_before="on", fan_after="off"))
            scheduler.advance_to(scheduler.now())

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["override_grace_production_state"] == "idle/active_unprotected"
        assert diag["override_grace_fsm_state"] == "idle/active_unprotected"
        assert diag["override_grace_fsm_agrees"] is True

    def test_shadow_engine_isolated_does_not_leak_into_coordinator_state(self) -> None:
        """The shadow engine also runs on_fan_turned_off() (it's a mirrored method), but
        its own emit_event callback (_on_shadow_emit_event) is isolated from
        coordinator._emit_event() — verify that isolation holds for this new event
        rather than assuming it, per _build_shadow_automation_callbacks()'s own design."""
        coordinator, _fake_hass, scheduler, _event_log = build_headless_coordinator()

        with scheduler.installed():
            coordinator.automation_engine.on_fan_turned_off(fan_before="on", fan_after="off")
            _run(coordinator._mirror_to_shadow("on_fan_turned_off", fan_before="on", fan_after="off"))
            scheduler.advance_to(scheduler.now())

        # Exactly one real transition happened (production's own call above) — the
        # shadow replay must not have fed a second, duplicate event into the tracker.
        assert coordinator._override_grace_fsm_state == (OverrideConfirmState.IDLE, GraceState.ACTIVE_UNPROTECTED)


class TestDiagnosticIntegration:
    def test_diagnostic_includes_override_grace_fsm_state(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert "override_grace_fsm_state" in diag
        assert "override_grace_production_state" in diag
        assert "override_grace_shadow_state" in diag
        assert "override_grace_mirror_agrees" in diag
        assert "override_grace_fsm_agrees" in diag

    def test_positive_control_fsm_disagreement_is_detected(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        # Force production directly into ACTIVE_PROTECTING_OVERRIDE (simulating a
        # real override's grace already running) BEFORE the mirrored call — the
        # FSM, evaluated purely from a DASHBOARD_RESUME event against its own
        # (IDLE, NONE) starting state, always lands on ACTIVE_UNPROTECTED
        # regardless of what production's own live flags say (resume_from_pause's
        # trigger="dashboard_resume" is never in GRACE_TRIGGERS_PROTECTING_OVERRIDE),
        # so the two are guaranteed to disagree here.
        coordinator.automation_engine._grace_active = True
        coordinator.automation_engine._grace_protects_override = True

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["agrees"] is False
        assert diag["override_grace_fsm_agrees"] is False
        assert diag["override_grace_fsm_state"] == "idle/active_unprotected"
        assert diag["override_grace_production_state"] == "idle/active_protecting_override"

    def test_positive_control_mirror_disagreement_is_detected(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._grace_active = True
        coordinator.automation_engine._grace_protects_override = False

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

        # Shadow engine never actually ran resume_from_pause() (noop'd above), so
        # its own flags stay whatever _sync_shadow_inputs() raw-copied from
        # production just before the noop ran (i.e. now in sync). Force a genuine
        # shadow-mirror disagreement by directly diverging the shadow's flag AFTER
        # that sync already happened, then recompute the diagnostic.
        coordinator.shadow_automation_engine._grace_active = False
        coordinator._update_shadow_engine_diagnostic()

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["override_grace_mirror_agrees"] is False
        assert diag["agrees"] is False

    def test_fan_manual_override_no_longer_disagrees(self) -> None:
        """Issue #661: closes the exact live disagreement observed 2026-08-16
        (production=idle/active_protecting_override, fsm=pending/none) triggered
        by a QuietCool RF remote timer press. Runs the real production
        handle_fan_manual_override() call (as coordinator.py's real
        _async_fan_entity_changed/_flush_fan_remote_burst call sites do,
        production call immediately followed by the mirror), then asserts the
        FSM's own tracked state now matches production and the diagnostic
        reports agreement."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.config[CONF_OVERRIDE_CONFIRM_PERIOD] = 600

        coordinator.automation_engine.handle_fan_manual_override(fan_before="auto", fan_after="on")
        _noop_shadow_methods(coordinator, "handle_fan_manual_override")
        _run(coordinator._mirror_to_shadow("handle_fan_manual_override", fan_before="auto", fan_after="on"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["override_grace_production_state"] == "idle/active_protecting_override"
        assert diag["override_grace_fsm_state"] == "idle/active_protecting_override"
        assert diag["override_grace_fsm_agrees"] is True

    def test_disagreement_logs_distinct_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._grace_active = True
        coordinator.automation_engine._grace_protects_override = True

        _noop_shadow_methods(coordinator, "resume_from_pause")
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            coordinator._update_shadow_engine_diagnostic()
        assert any("Override/grace FSM disagreement" in r.message for r in caplog.records)


class TestIsolation:
    def test_fsm_exception_does_not_propagate(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom(method_name: str) -> None:
            raise RuntimeError("override/grace fsm blew up")

        coordinator._evaluate_override_grace_fsm = _boom  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

    def test_fsm_exception_logs_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom(method_name: str) -> None:
            raise RuntimeError("override/grace fsm blew up")

        coordinator._evaluate_override_grace_fsm = _boom  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "resume_from_pause")
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            _run(coordinator._mirror_to_shadow("resume_from_pause"))
        assert any("override/grace fsm blew up" in r.message for r in caplog.records)

    def test_fsm_state_never_written_onto_production_engine(self) -> None:
        """The FSM's own tracked state is a third, independent computation —
        never read by or written onto AutomationEngine itself."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_by_door = True

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))

        assert not hasattr(coordinator.automation_engine, "_override_grace_fsm_state")
        assert not hasattr(coordinator.shadow_automation_engine, "_override_grace_fsm_state")
