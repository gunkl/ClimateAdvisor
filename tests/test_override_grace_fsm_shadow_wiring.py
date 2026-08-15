"""Tests for Issue #639's wiring increment: running the unified override/grace
joint-lifecycle FSM live against production's real readings, tracked as a third
comparison point alongside the existing production/shadow mirror comparison.

v1 scope, deliberately narrow (see ``coordinator._evaluate_override_grace_fsm()``'s
own docstring and ``_OVERRIDE_GRACE_FSM_EVENT_KINDS``): only triggered from the 2
mirrored methods with an unambiguous override/grace FSM event-kind correspondence
(``handle_manual_override_during_pause``, ``resume_from_pause``) — of the 7
``OverrideGraceFsmEventKind`` members, the other 5 have no ``_mirror_to_shadow(...)``
call site today (see ``_sync_shadow_inputs()``'s own docstring and
``tests/test_shadow_engine_coverage.py``'s registry). Mirrors
``tests/test_door_window_fsm_shadow_wiring.py``'s structure.
"""

from __future__ import annotations

import logging

from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD
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
        called: list[str] = []
        coordinator._evaluate_override_grace_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "apply_classification")
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert called == []

    def test_not_triggered_by_door_window_only_mirrored_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_override_grace_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert called == []

    def test_triggered_by_handle_manual_override_during_pause(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_override_grace_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_manual_override_during_pause")
        _run(coordinator._mirror_to_shadow("handle_manual_override_during_pause"))
        assert called == ["handle_manual_override_during_pause"]

    def test_triggered_by_resume_from_pause(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_override_grace_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "resume_from_pause")
        _run(coordinator._mirror_to_shadow("resume_from_pause"))
        assert called == ["resume_from_pause"]


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
