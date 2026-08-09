"""Tests for Issue #613 (Block 5, subtask Q): live shadow AutomationEngine.

Covers:
  - Construction: the coordinator now builds a REAL, distinct, permanently
    dry_run=True shadow engine (superseding N2's ``None`` placeholder).
  - Callback isolation: the four callbacks N2 traced as capable of reaching
    production (revisit, request_refresh, post_grace_fan_check, reclassify)
    are structurally unable to on the shadow bundle — proven both directly
    and via a positive control showing the SAME test would catch the N2
    hazard (production's own bundle) if isolation broke.
  - ``_mirror_to_shadow``: replays a call on the shadow engine, never lets a
    shadow-side exception escape, and always recomputes the diagnostic.
  - ``_update_shadow_engine_diagnostic``: agreement/disagreement detection,
    including a positive control that forces disagreement.
  - Shutdown: shadow engine timers get cancelled alongside production's.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402


class TestShadowEngineConstruction:
    def test_shadow_engine_is_real_and_distinct(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert isinstance(coordinator.shadow_automation_engine, AutomationEngine)
        assert coordinator.shadow_automation_engine is not coordinator.automation_engine

    def test_shadow_engine_dry_run_always_true(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator.shadow_automation_engine.dry_run is True

    def test_shadow_engine_role_is_shadow(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator.shadow_automation_engine.role == "shadow"
        assert coordinator.automation_engine.role == "production"

    def test_shadow_bundle_hazardous_callbacks_differ_from_production(self) -> None:
        """The 4 callables N2 flagged as reachable-to-production must not be the
        coordinator's own production-bound methods on the shadow engine."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        shadow = coordinator.shadow_automation_engine
        prod = coordinator.automation_engine
        assert shadow._revisit_callback is None
        assert shadow._request_refresh_callback is not prod._request_refresh_callback
        assert shadow._post_grace_fan_check_callback is not prod._post_grace_fan_check_callback
        assert shadow._reclassify_callback is not prod._reclassify_callback

    def test_shadow_bundle_read_only_callbacks_are_shared(self) -> None:
        """Pure reads are safe (and correct) to share — they observe ground truth,
        never act. Structural proof they're the SAME bound methods, not reimplemented."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        shadow = coordinator.shadow_automation_engine
        assert shadow._sensor_check_callback == coordinator._any_sensor_open
        assert shadow._get_fan_physical_state_callback == coordinator._get_fan_physical_state
        assert shadow._is_recent_fan_command_callback == coordinator._is_recent_fan_command


class TestShadowCallbackIsolation:
    """Proves the 4 hazardous callbacks are true no-ops against the real coordinator,
    with a positive control showing this test suite would catch the N2 hazard."""

    def test_request_refresh_does_not_trigger_coordinator_refresh(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        calls: list[Any] = []
        coordinator.async_request_refresh = lambda: calls.append(1)  # type: ignore[assignment]
        coordinator.shadow_automation_engine._request_refresh_callback()
        assert calls == []

    def test_post_grace_fan_check_does_not_reach_production_reconcile(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        calls: list[Any] = []
        coordinator._on_post_grace_fan_check = lambda: calls.append(1)  # type: ignore[assignment]
        coordinator.shadow_automation_engine._post_grace_fan_check_callback()
        assert calls == []

    def test_reclassify_does_not_reassert_production_setpoint(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        calls: list[Any] = []
        coordinator._on_whf_release_reclassify = lambda: calls.append(1)  # type: ignore[assignment]
        coordinator.shadow_automation_engine._reclassify_callback()
        assert calls == []

    def test_positive_control_production_bundle_would_have_leaked(self) -> None:
        """If the shadow engine were (mis)built with the PRODUCTION callback bundle —
        exactly the N2 hazard — request_refresh WOULD reach the coordinator. Proves
        the isolation tests above are not vacuously passing."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        calls: list[Any] = []
        coordinator.async_request_refresh = lambda: calls.append(1)  # type: ignore[assignment]
        hazardous = AutomationEngine(
            hass=coordinator.hass,
            climate_entity="climate.test",
            weather_entity="weather.test",
            door_window_sensors=[],
            notify_service="notify.test",
            config={},
            callbacks=coordinator._build_production_automation_callbacks(),
            role="shadow",
        )
        hazardous._request_refresh_callback()
        assert calls == [1], "positive control failed to reproduce the N2 hazard"


class TestMirrorToShadow:
    def test_mirror_calls_shadow_method_with_same_args(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        captured: list[tuple] = []

        async def _fake_apply(classification, **kwargs):
            captured.append((classification, kwargs))

        coordinator.shadow_automation_engine.apply_classification = _fake_apply
        _run(coordinator._mirror_to_shadow("apply_classification", "day-marker", indoor_temp=70.0))
        assert captured == [("day-marker", {"indoor_temp": 70.0})]

    def test_mirror_swallows_shadow_exception_without_raising(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        async def _boom(*args, **kwargs):
            raise RuntimeError("shadow blew up")

        coordinator.shadow_automation_engine.apply_classification = _boom
        # Must not raise — production's own control flow must never depend on the
        # shadow engine succeeding.
        _run(coordinator._mirror_to_shadow("apply_classification", None))

    def test_mirror_logs_warning_on_shadow_exception(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        async def _boom(*args, **kwargs):
            raise RuntimeError("shadow blew up")

        coordinator.shadow_automation_engine.apply_classification = _boom
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert any("shadow blew up" in r.message for r in caplog.records)

    def test_mirror_swallows_diagnostic_update_exception_too(self) -> None:
        """Positive control: the diagnostic recompute itself is best-effort — a bug
        reading either engine's state (e.g. a partial-instantiation test double with
        MagicMock attributes, the exact shape several older coordinator tests use)
        must not propagate out of ``_mirror_to_shadow`` either."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()

        async def _noop(*args, **kwargs):
            return None

        coordinator.shadow_automation_engine.apply_classification = _noop

        def _boom():
            raise RuntimeError("diagnostic blew up")

        coordinator._update_shadow_engine_diagnostic = _boom
        # Must not raise.
        _run(coordinator._mirror_to_shadow("apply_classification", None))

    def test_mirror_recomputes_diagnostic_even_after_exception(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        assert coordinator.shadow_engine_diagnostic is None

        async def _boom(*args, **kwargs):
            raise RuntimeError("shadow blew up")

        coordinator.shadow_automation_engine.apply_classification = _boom
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_engine_diagnostic is not None


class TestShadowEngineDiagnostic:
    def test_diagnostic_agrees_by_default(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator._update_shadow_engine_diagnostic()
        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["agrees"] is True
        assert diag["production_state"] == diag["shadow_state"]

    def test_positive_control_disagreement_is_detected(self) -> None:
        """Forces a real divergence (shadow thinks nat-vent is active, production
        doesn't) and confirms the diagnostic — and its WARNING log — catch it."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        coordinator._update_shadow_engine_diagnostic()
        diag = coordinator.shadow_engine_diagnostic
        assert diag["agrees"] is False
        assert diag["production_state"] != diag["shadow_state"]

    def test_disagreement_logs_warning(self, caplog) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            coordinator._update_shadow_engine_diagnostic()
        assert any("Shadow engine disagreement" in r.message for r in caplog.records)


class TestShadowEngineShutdown:
    def test_async_shutdown_cleans_up_shadow_timers(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        cleaned: list[Any] = []
        coordinator.shadow_automation_engine.cleanup = lambda: cleaned.append(1)  # type: ignore[assignment]
        _run(coordinator.async_shutdown())
        assert cleaned == [1]


def _run(coro):
    return run_coro(coro)
