"""Tests for Issue #633's wiring increment: running the unified nat-vent FSM
live against production's real readings, tracked as a third comparison point
alongside the existing production/shadow mirror comparison (Issue #613).

v1 scope, deliberately narrow (see ``coordinator._evaluate_nat_vent_fsm()``'s
own docstring): only triggered from ``check_natural_vent_conditions``'s
mirror — the one mirrored method that unambiguously corresponds to nat-vent's
own gate/exit re-evaluation. These tests confirm that scoping is honored (no
FSM evaluation from unrelated mirrored calls), the FSM's own state tracks
correctly across calls, disagreement detection works, and isolation holds
(an FSM exception never reaches production's control flow).
"""

from __future__ import annotations

import logging

from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator


def _run(coro):
    return run_coro(coro)


class TestFsmEvaluationScoping:
    def test_not_triggered_by_unrelated_mirrored_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_nat_vent_fsm = lambda: called.append("called")  # type: ignore[method-assign]

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.apply_classification = _noop
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert called == []

    def test_triggered_by_check_natural_vent_conditions(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_nat_vent_fsm = lambda: called.append("called")  # type: ignore[method-assign]

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))
        assert called == ["called"]


class TestFsmStateTracking:
    def test_starts_inactive(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator._nat_vent_fsm_state == NatVentLifecycleState.INACTIVE

    def test_activates_when_conditions_favorable(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_attributes={"current_temperature": 75.0}
        )
        coordinator.automation_engine.update_outdoor_temp(65.0)
        coordinator.config["comfort_heat"] = 68.0
        coordinator.config["comfort_cool"] = 76.0

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))

        assert coordinator._nat_vent_fsm_state == NatVentLifecycleState.ACTIVE_FULL_GATE

    def test_stays_inactive_when_conditions_unfavorable(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_attributes={"current_temperature": 70.0}
        )
        coordinator.automation_engine.update_outdoor_temp(80.0)  # outdoor above indoor -> no gate

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))

        assert coordinator._nat_vent_fsm_state == NatVentLifecycleState.INACTIVE


class TestDiagnosticIntegration:
    def test_diagnostic_includes_fsm_state(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert "nat_vent_fsm_state" in diag
        assert diag["nat_vent_fsm_state"] == NatVentLifecycleState.INACTIVE.value

    def test_positive_control_fsm_disagreement_is_detected(self) -> None:
        """Forces a real divergence: production is (incorrectly, for this test)
        marked active while conditions clearly favor the FSM staying inactive —
        confirms the diagnostic's overall agrees flag reflects it and the
        disagreement is logged distinctly from the mirror-based one."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_attributes={"current_temperature": 70.0}
        )
        coordinator.automation_engine.update_outdoor_temp(80.0)  # FSM will stay INACTIVE
        coordinator.automation_engine._natural_vent_active = True  # production disagrees

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["agrees"] is False
        assert diag["nat_vent_fsm_state"] == NatVentLifecycleState.INACTIVE.value
        assert diag["production_state"] == NatVentLifecycleState.ACTIVE_FULL_GATE.value

    def test_disagreement_logs_distinct_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_attributes={"current_temperature": 70.0}
        )
        coordinator.automation_engine.update_outdoor_temp(80.0)
        coordinator.automation_engine._natural_vent_active = True

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))
        assert any("Nat-vent FSM disagreement" in r.message for r in caplog.records)


class TestIsolation:
    def test_fsm_exception_does_not_propagate(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom() -> None:
            raise RuntimeError("fsm blew up")

        coordinator._evaluate_nat_vent_fsm = _boom  # type: ignore[method-assign]

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        # Must not raise -- production's own control flow must never depend on
        # the FSM evaluation succeeding, same isolation posture as the
        # existing shadow-mirror exception handling (Issue #613).
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))

    def test_fsm_exception_logs_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom() -> None:
            raise RuntimeError("fsm blew up")

        coordinator._evaluate_nat_vent_fsm = _boom  # type: ignore[method-assign]

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.check_natural_vent_conditions = _noop
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))
        assert any("fsm blew up" in r.message for r in caplog.records)
