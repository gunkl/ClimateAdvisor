"""Tests for Issue #637's wiring increment: running the unified door/window
pause/grace FSM live against production's real readings, tracked as a third
comparison point alongside the existing production/shadow mirror comparison.

v1 scope, deliberately narrow (see ``coordinator._evaluate_door_window_fsm()``'s
own docstring and ``_DOOR_WINDOW_FSM_EVENT_KINDS``): only triggered from the 3
mirrored methods with an unambiguous door/window FSM event-kind correspondence.
Mirrors ``tests/test_nat_vent_fsm_shadow_wiring.py``'s structure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.const import SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S  # noqa: E402
from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState  # noqa: E402

# Issue #685: build_headless_coordinator's stub environment leaves
# homeassistant.util.dt.now() returning a MagicMock (never exercised
# arithmetically before this issue's wall-clock debounce). Tests that exercise
# a disagreement path — where the debounce helper subtracts two "now" values —
# must patch coordinator.dt_util.now to a real, fixed datetime.
_FIXED_NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


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
        coordinator._evaluate_door_window_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "apply_classification")
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert called == []

    def test_triggered_by_handle_door_window_open(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_door_window_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert called == ["handle_door_window_open"]

    def test_triggered_by_handle_all_doors_windows_closed(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_door_window_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_all_doors_windows_closed")
        _run(coordinator._mirror_to_shadow("handle_all_doors_windows_closed"))
        assert called == ["handle_all_doors_windows_closed"]

    def test_triggered_by_handle_manual_override_during_pause(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_door_window_fsm = lambda method_name: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_manual_override_during_pause")
        _run(coordinator._mirror_to_shadow("handle_manual_override_during_pause"))
        assert called == ["handle_manual_override_during_pause"]

    def test_not_triggered_by_check_natural_vent_conditions_mirror_alone(self) -> None:
        """Issue #668: check_natural_vent_conditions() was, until now, registered as a
        method-name-keyed, UNCONDITIONAL trigger for PAUSED_NAT_VENT_REACTIVATED (#660
        Step 3) -- merely mirroring this method (with no real reactivation-while-paused
        branch taken) wrongly fired the door/window FSM every single cycle, permanently
        resetting it to NORMAL moments after SYNC_RECONCILE correctly restored a paused
        state (confirmed live, Issue #668). The feed is now event-driven instead (see
        TestFsmEvaluationScoping's sibling test in test_shadow_fsm_harness_event_coverage.py
        for the positive case where the real branch DOES fire) -- mirroring this method
        alone, with no emitted "nat_vent_reactivated_while_paused" event, must not call
        _evaluate_door_window_fsm at all."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[str] = []
        coordinator._evaluate_door_window_fsm = lambda method_name, event_kind=None: called.append(method_name)  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "check_natural_vent_conditions")
        _run(coordinator._mirror_to_shadow("check_natural_vent_conditions"))
        assert called == []


class TestFsmStateTracking:
    def test_starts_normal(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL

    def test_pauses_when_sensor_opens_and_hvac_active(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_state="cool", climate_attributes={"current_temperature": 76.0}
        )
        coordinator.automation_engine.update_outdoor_temp(90.0)  # too warm for nat-vent
        coordinator.config["comfort_cool"] = 78.0

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))

        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.PAUSED_ACTIVE


class TestDiagnosticIntegration:
    def test_diagnostic_includes_door_window_fsm_state(self) -> None:
        """Issue #685: patches dt_util.now — the noop'd shadow handler leaves the
        shadow/production door-window state disagreeing, which now exercises the
        debounce helper's datetime arithmetic (raises against the stub
        environment's default MagicMock clock without a fixed patch)."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert "door_window_fsm_state" in diag
        assert "door_window_production_state" in diag
        assert "door_window_fsm_agrees" in diag

    def test_positive_control_fsm_disagreement_is_detected(self) -> None:
        """Issue #685: patches dt_util.now to a real fixed datetime because the
        debounce helper now subtracts two "now" values, which raises against the
        stub environment's default MagicMock clock when a disagreement occurs."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_state="cool", climate_attributes={"current_temperature": 76.0}
        )
        coordinator.automation_engine.update_outdoor_temp(90.0)
        coordinator.config["comfort_cool"] = 78.0
        # Force a disagreement: FSM will compute PAUSED_ACTIVE, production stays NORMAL.
        coordinator.automation_engine._paused_by_door = False

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))

        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["agrees"] is False
        assert diag["door_window_fsm_agrees"] is False
        assert diag["door_window_fsm_state"] == DoorWindowLifecycleState.PAUSED_ACTIVE.value
        assert diag["door_window_production_state"] == DoorWindowLifecycleState.NORMAL.value

    def test_disagreement_logs_distinct_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_state="cool", climate_attributes={"current_temperature": 76.0}
        )
        coordinator.automation_engine.update_outdoor_temp(90.0)
        coordinator.config["comfort_cool"] = 78.0
        coordinator.automation_engine._paused_by_door = False
        # Issue #685: WARNING now only fires once the axis has continuously
        # disagreed for SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S — seed the streak as
        # already past threshold so this test still exercises a real, persistent
        # divergence (its original intent) rather than a fresh, momentary one.
        coordinator._shadow_diag_disagreement_since["door_window_fsm"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert any("Door/window FSM disagreement" in r.message for r in caplog.records)

    def test_fresh_door_window_fsm_disagreement_does_not_log_warning(self, caplog) -> None:
        """Issue #685: the actual bug fix — a single-tick, fresh door/window FSM
        disagreement must NOT log a WARNING. Without the debounce, this fires
        immediately."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator(
            climate_state="cool", climate_attributes={"current_temperature": 76.0}
        )
        coordinator.automation_engine.update_outdoor_temp(90.0)
        coordinator.config["comfort_cool"] = 78.0
        coordinator.automation_engine._paused_by_door = False

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert not any("Door/window FSM disagreement" in r.message for r in caplog.records)
        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["debounce"]["door_window_fsm"]["sustained"] is False
        assert 0.0 <= diag["debounce"]["door_window_fsm"]["disagreement_seconds"] < 1.0


class TestIsolation:
    def test_fsm_exception_does_not_propagate(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom(method_name: str) -> None:
            raise RuntimeError("door/window fsm blew up")

        coordinator._evaluate_door_window_fsm = _boom  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))

    def test_fsm_exception_logs_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        def _boom(method_name: str) -> None:
            raise RuntimeError("door/window fsm blew up")

        coordinator._evaluate_door_window_fsm = _boom  # type: ignore[method-assign]

        _noop_shadow_methods(coordinator, "handle_door_window_open")
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            _run(coordinator._mirror_to_shadow("handle_door_window_open", "binary_sensor.test"))
        assert any("door/window fsm blew up" in r.message for r in caplog.records)
