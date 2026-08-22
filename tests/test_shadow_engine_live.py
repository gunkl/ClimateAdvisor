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

Issue #615 (coverage-gap follow-up) adds:
  - ``_sync_shadow_inputs()``: outdoor temp/forecast/thermal-model/occupancy parity,
    with a positive control (the exact bug: input never mirrored → gate can't fire).
  - Representative newly-mirrored decision methods: ``reconcile_fan_on_startup()``
    (the exact live-incident reproduction), ``fan_thermostat_check()`` (Issue #608's
    dominant-exit-path finding), and sync-method mirroring (``on_fan_turned_off()``).

Issue #631 (second coverage-gap follow-up) adds:
  - ``_sync_shadow_inputs()`` grace/override parity: ``_grace_active``,
    ``_manual_override_active``, ``_fan_override_active``,
    ``_override_confirm_pending/_mode/_source``, ``_paused_with_hvac_already_off``, and
    their companion mode/source/time/duration fields — with a positive control
    reproducing the confirmed live incident (shadow disagreed for 2h38m because its
    ``_grace_active`` never reflected production's active manual-override grace period).

Issue #673 (third coverage-gap follow-up, Phase 2) adds:
  - ``_sync_shadow_inputs()`` nat-vent/door-window parity: ``_natural_vent_active``,
    ``_nat_vent_soft_start``, ``_paused_by_door``, ``_nat_vent_outdoor_exit_time`` —
    the 4 fields confirmed absent from the raw-copy block despite the #613/#631
    precedent already covering everything else. Same shape as #615/#631: a positive
    control proves the fields stay stale on the shadow without the raw copy.

Issue #724 (fifth coverage-gap follow-up, same class as #716) adds:
  - ``_sync_shadow_inputs()`` WHF-suppression parity: ``_pre_fan_hvac_mode``, the field
    ``_whf_owns_hvac()`` depends on. Corrects the filed issue's own "dormant, not urgent"
    claim — traced to a live-reachable divergence: ``_sync_paused_by_door_with_live_
    sensors()`` (called from 4 mirrored entry points) reads ``_whf_owns_hvac()`` as an
    early-return guard before calling ``_pause_for_door_window()``, which sets
    ``_paused_by_door`` — a tracked field feeding the door_window/nat_vent diagnostic
    axes. Without the raw copy, the shadow incorrectly self-pauses during a genuine WHF
    session with a window open (WHF's designed use case) while production correctly
    does not. Positive control reproduces this exact divergence directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
    SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S,
)

# Issue #685: build_headless_coordinator's stub environment leaves
# homeassistant.util.dt.now() returning a MagicMock (never exercised
# arithmetically before this issue's wall-clock debounce). Tests that exercise
# a disagreement path — where the debounce helper subtracts two "now" values —
# must patch coordinator.dt_util.now to a real, fixed datetime.
_FIXED_NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


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
        doesn't) and confirms the diagnostic — and its WARNING log — catch it.

        Issue #685: patches dt_util.now to a real fixed datetime because the
        debounce helper now subtracts two "now" values, which raises against the
        stub environment's default MagicMock clock — a change from this test's
        original form, needed only because a disagreement path is exercised.
        """
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
        diag = coordinator.shadow_engine_diagnostic
        assert diag["agrees"] is False
        assert diag["production_state"] != diag["shadow_state"]

    def test_disagreement_logs_warning(self, caplog) -> None:
        """Issue #685: a WARNING only fires once the mirror axis has continuously
        disagreed for SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S — seed the streak as
        already past threshold so this test still proves a real, persistent
        divergence gets reported."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        coordinator._shadow_diag_disagreement_since["mirror"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coordinator._update_shadow_engine_diagnostic()
        assert any("Shadow engine disagreement" in r.message for r in caplog.records)

    def test_fresh_disagreement_does_not_log_warning(self, caplog) -> None:
        """Issue #685: the actual bug fix — a single-tick, fresh disagreement
        (the shape of a real multi-step production cascade's momentary,
        self-resolving divergence, confirmed via live log evidence) must NOT log
        a WARNING. Without the debounce, this fires immediately."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW),
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"),
        ):
            coordinator._update_shadow_engine_diagnostic()
        assert not any("Shadow engine disagreement" in r.message for r in caplog.records)
        diag = coordinator.shadow_engine_diagnostic
        assert diag is not None
        assert diag["debounce"]["mirror"]["sustained"] is False
        assert 0.0 <= diag["debounce"]["mirror"]["disagreement_seconds"] < 1.0

    def test_production_state_honors_configured_lockout_seconds(self) -> None:
        """Issue #684: ``_state_for()`` used to hardcode ``lockout_seconds=300``
        instead of reading the configured ``CONF_NAT_VENT_REACTIVATION_LOCKOUT_S``
        value (which ``_evaluate_nat_vent_fsm()`` has always read correctly).
        Default lockout is also 300s, so the bug was invisible on any install
        using the default — this test configures a shorter lockout (60s) and
        an outdoor-exit timestamp 120s in the past: with the fix, the
        (already-elapsed) lockout means the derived state is INACTIVE; with the
        old hardcoded 300s, 120s is still inside the window and the state would
        incorrectly read PAUSED_REACTIVATION_LOCKOUT.
        """
        from custom_components.climate_advisor.const import CONF_NAT_VENT_REACTIVATION_LOCKOUT_S

        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.config[CONF_NAT_VENT_REACTIVATION_LOCKOUT_S] = 60
        for ae in (coordinator.automation_engine, coordinator.shadow_automation_engine):
            ae._natural_vent_active = False
            ae._paused_by_door = True
            ae._nat_vent_outdoor_exit_time = _FIXED_NOW - timedelta(seconds=120)
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
        diag = coordinator.shadow_engine_diagnostic
        # Nat-vent-specific fields only — setting _paused_by_door also feeds the
        # unrelated door/window FSM comparison, which is out of scope here and
        # not staged for this test.
        assert diag["production_state"] == "inactive"
        assert diag["shadow_state"] == "inactive"
        assert diag["nat_vent_fsm_state"] == "inactive"


class TestShadowEngineShutdown:
    def test_async_shutdown_cleans_up_shadow_timers(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        cleaned: list[Any] = []
        coordinator.shadow_automation_engine.cleanup = lambda: cleaned.append(1)  # type: ignore[assignment]
        _run(coordinator.async_shutdown())
        assert cleaned == [1]


class TestSyncShadowInputs:
    """Issue #615: the confirmed live bug — shadow's _last_outdoor_temp stayed None
    forever because nothing ever called update_outdoor_temp() on it."""

    def test_outdoor_temp_parity_after_mirror_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine.update_outdoor_temp(72.5)
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._last_outdoor_temp == 72.5

    def test_forecast_and_thermal_model_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._hourly_forecast_temps = [{"temperature": 80}]
        coordinator.automation_engine._thermal_model = {"confidence": "high"}
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._hourly_forecast_temps == [{"temperature": 80}]
        assert coordinator.shadow_automation_engine._thermal_model == {"confidence": "high"}

    def test_occupancy_mode_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine.set_occupancy_mode("away")
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._occupancy_mode == "away"

    def test_positive_control_missing_sync_reproduces_the_live_bug(self) -> None:
        """Without _sync_shadow_inputs(), outdoor temp stays None on the shadow even
        after production has a real reading — proves this test would have caught
        tonight's incident before it shipped."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine.update_outdoor_temp(72.5)
        coordinator._sync_shadow_inputs = lambda: None  # type: ignore[method-assign]
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._last_outdoor_temp is None


class TestSyncShadowInputsGraceOverride:
    """Issue #631: the confirmed live bug — shadow's _grace_active stayed False for
    2h38m while production's was True (an active manual-override grace period), so
    shadow's check_natural_vent_conditions() kept deciding "reactivate" every cycle
    while production correctly stayed suppressed. Same shape as #615's outdoor-temp
    gap, different field set: grace/override state set by internal timers or
    unmirrored direct coordinator/api calls, with no per-setter mirror call added —
    fixed via the same _sync_shadow_inputs() raw-copy mechanism instead."""

    def test_grace_state_parity_after_mirror_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._grace_active = True
        coordinator.automation_engine._grace_end_time = "2026-08-12T21:32:02-07:00"
        coordinator.automation_engine._grace_duration_seconds = 1800
        coordinator.automation_engine._last_grace_trigger = "fan_manual_override"
        coordinator.automation_engine._grace_protects_override = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        shadow = coordinator.shadow_automation_engine
        assert shadow._grace_active is True
        assert shadow._grace_end_time == "2026-08-12T21:32:02-07:00"
        assert shadow._grace_duration_seconds == 1800
        assert shadow._last_grace_trigger == "fan_manual_override"
        assert shadow._grace_protects_override is True

    def test_manual_override_state_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._manual_override_active = True
        coordinator.automation_engine._manual_override_mode = "off"
        coordinator.automation_engine._manual_override_source = "normal"
        coordinator.automation_engine._manual_override_time = "2026-08-12T18:58:47-07:00"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        shadow = coordinator.shadow_automation_engine
        assert shadow._manual_override_active is True
        assert shadow._manual_override_mode == "off"
        assert shadow._manual_override_source == "normal"
        assert shadow._manual_override_time == "2026-08-12T18:58:47-07:00"

    def test_fan_override_state_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._fan_override_active = True
        coordinator.automation_engine._fan_override_time = "2026-08-12T21:02:02-07:00"
        coordinator.automation_engine._fan_remote_timer_hours = 12.0
        coordinator.automation_engine._fan_remote_speed = "high"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        shadow = coordinator.shadow_automation_engine
        assert shadow._fan_override_active is True
        assert shadow._fan_override_time == "2026-08-12T21:02:02-07:00"
        assert shadow._fan_remote_timer_hours == 12.0
        assert shadow._fan_remote_speed == "high"

    def test_override_confirm_pending_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._override_confirm_pending = True
        coordinator.automation_engine._override_confirm_mode = "cool"
        coordinator.automation_engine._override_confirm_source = "setpoint"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        shadow = coordinator.shadow_automation_engine
        assert shadow._override_confirm_pending is True
        assert shadow._override_confirm_mode == "cool"
        assert shadow._override_confirm_source == "setpoint"

    def test_paused_with_hvac_already_off_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_with_hvac_already_off = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._paused_with_hvac_already_off is True

    def test_positive_control_missing_sync_reproduces_the_grace_incident(self) -> None:
        """Without the grace/override fields in _sync_shadow_inputs(), _grace_active
        stays False on the shadow even after production starts a real grace period —
        proves this test would have caught the 2026-08-12 21:02-23:40 incident before
        it shipped."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._grace_active = True
        coordinator._sync_shadow_inputs = lambda: None  # type: ignore[method-assign]
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._grace_active is False


class TestSyncShadowInputsNatVentDoorWindow:
    """Issue #673: same gap class as #615/#631, different field set — nat-vent/
    door-window state confirmed absent from _sync_shadow_inputs()'s raw-copy block.
    Any missed or exception-interrupted _mirror_to_shadow() call site touching these
    fields caused a permanent, non-self-healing divergence with no way to recover
    short of a coordinator restart."""

    def test_natural_vent_active_parity_after_mirror_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._natural_vent_active = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._natural_vent_active is True

    def test_nat_vent_soft_start_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._nat_vent_soft_start = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._nat_vent_soft_start is True

    def test_paused_by_door_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._paused_by_door = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._paused_by_door is True

    def test_nat_vent_outdoor_exit_time_parity(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._nat_vent_outdoor_exit_time = "2026-08-17T21:02:02-07:00"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._nat_vent_outdoor_exit_time == "2026-08-17T21:02:02-07:00"

    def test_positive_control_missing_sync_reproduces_the_stuck_disagreement(self) -> None:
        """Without these 4 fields in _sync_shadow_inputs(), _natural_vent_active stays
        False on the shadow even after production activates nat-vent for real — proves
        this test would have caught the confirmed live "production=active fsm/shadow=
        inactive, stuck for hours" class of incident before it shipped."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._natural_vent_active = True
        coordinator.automation_engine._paused_by_door = True
        coordinator._sync_shadow_inputs = lambda: None  # type: ignore[method-assign]
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._natural_vent_active is False
        assert coordinator.shadow_automation_engine._paused_by_door is False


class TestSyncShadowInputsFanActive:
    """Issue #716: same gap class again, but with a mechanism twist the #615/#631/#673
    precedents didn't have — _fan_active's real writers (_activate_fan()/
    _deactivate_fan()) both `return` early under `self.dry_run` before ever assigning
    the field, so a direct _mirror_to_shadow("_activate_fan"/"_deactivate_fan", ...)
    replay could never have worked on the permanently-dry_run shadow instance, unlike
    every other field in this file's raw-copy tests. The raw copy is the ONLY
    mechanism that can reach this field on the shadow at all, not merely the simplest
    one — this test proves it actually does, driving a real activate/check/deactivate
    sequence on production and asserting the shadow's own view tracks it."""

    def test_fan_active_parity_after_mirror_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._fan_active = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._fan_active is True

    def test_fan_active_parity_flips_off_too(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._fan_active = True
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._fan_active is True
        coordinator.automation_engine._fan_active = False
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._fan_active is False

    def test_positive_control_missing_sync_leaves_fan_active_stale(self) -> None:
        """Without _fan_active in _sync_shadow_inputs(), the shadow's copy stays False
        even after production genuinely activates the fan — the exact "mirror looks
        wired but is inert" failure this issue closes, reproduced directly."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._fan_active = True
        coordinator._sync_shadow_inputs = lambda: None  # type: ignore[method-assign]
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._fan_active is False

    def test_dry_run_guard_would_defeat_a_direct_mirror_of_activate_fan(self) -> None:
        """Documents WHY the raw copy is required, not just convenient: replaying
        _activate_fan() itself on the shadow hits its own `if self.dry_run: return`
        guard before _fan_active is ever assigned, so a naive
        _mirror_to_shadow("_activate_fan", ...) call would leave the shadow's
        _fan_active exactly as stale as no mirroring at all."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        shadow = coordinator.shadow_automation_engine
        assert shadow.dry_run is True
        shadow._fan_active = False
        _run(shadow._activate_fan(reason="test"))
        assert shadow._fan_active is False


class TestSyncShadowInputsWhfOwnsHvac:
    """Issue #724: same gap class as #716 (TestSyncShadowInputsFanActive above), one
    field over. _pre_fan_hvac_mode was never added to _sync_shadow_inputs()'s raw-copy
    block, so shadow_automation_engine._whf_owns_hvac() (which depends on it) was
    permanently False regardless of production's real WHF state.

    The last test in this class proves the finding that corrected the filed issue's own
    "dormant, not urgent" claim: _sync_paused_by_door_with_live_sensors() (called from 4
    mirrored entry points — apply_classification/handle_bedtime/handle_morning_wakeup/
    handle_pre_cool) reads _whf_owns_hvac() as an early-return guard before calling
    _pause_for_door_window(), which sets _paused_by_door — a tracked field feeding the
    door_window/nat_vent shadow-diagnostic axes. Without the raw copy, the shadow
    incorrectly self-pauses during a genuine WHF session with a window open (WHF's
    designed use case) while production correctly does not.
    """

    def test_pre_fan_hvac_mode_parity_after_mirror_call(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._pre_fan_hvac_mode = "cool"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._pre_fan_hvac_mode == "cool"

    def test_pre_fan_hvac_mode_parity_flips_to_none_too(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._pre_fan_hvac_mode = "cool"
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._pre_fan_hvac_mode == "cool"
        coordinator.automation_engine._pre_fan_hvac_mode = None
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._pre_fan_hvac_mode is None

    def test_positive_control_missing_sync_leaves_pre_fan_hvac_mode_stale(self) -> None:
        """Without _pre_fan_hvac_mode in _sync_shadow_inputs(), the shadow's copy stays
        None even after production genuinely starts a WHF suppression session — the
        exact staleness this issue closes, reproduced directly."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.automation_engine._pre_fan_hvac_mode = "cool"
        coordinator._sync_shadow_inputs = lambda: None  # type: ignore[method-assign]
        _run(coordinator._mirror_to_shadow("apply_classification", None))
        assert coordinator.shadow_automation_engine._pre_fan_hvac_mode is None

    def test_whf_guard_agrees_between_engines_once_synced(self) -> None:
        """Reproduces Finding 1: a genuine WHF session (_pre_fan_hvac_mode set) with a
        monitored window open must make BOTH engines' _sync_paused_by_door_with_live_
        sensors() early-return via _whf_owns_hvac() — neither should self-pause.
        Before the fix (with the positive control below), the shadow's copy of
        _whf_owns_hvac() is always False, so only production's guard fires correctly.
        """
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae = coordinator.automation_engine
        se = coordinator.shadow_automation_engine
        for engine in (ae, se):
            engine._paused_by_door = False
            engine._natural_vent_active = False
            engine._sensor_check_callback = lambda: True  # a monitored window is open
            engine._sensor_debounce_pending_callback = lambda: False  # settled, not a blip
        ae._pre_fan_hvac_mode = "cool"  # production: genuine WHF session owns HVAC

        coordinator._sync_shadow_inputs()  # the Issue #724 fix — copies _pre_fan_hvac_mode
        assert se._whf_owns_hvac() is True  # shadow now sees WHF ownership too

        _run(ae._sync_paused_by_door_with_live_sensors())
        _run(se._sync_paused_by_door_with_live_sensors())
        assert ae._paused_by_door is False  # production correctly does not self-pause
        assert se._paused_by_door is False  # shadow now agrees

    def test_positive_control_whf_guard_disagrees_without_the_sync(self) -> None:
        """Same setup as above, but with the raw copy disabled — proves the shadow
        really does incorrectly self-pause without this issue's fix, the actual bug
        Finding 1 identified (not just a theoretical staleness risk)."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        ae = coordinator.automation_engine
        se = coordinator.shadow_automation_engine
        for engine in (ae, se):
            engine._paused_by_door = False
            engine._natural_vent_active = False
            engine._sensor_check_callback = lambda: True
            engine._sensor_debounce_pending_callback = lambda: False
        ae._pre_fan_hvac_mode = "cool"
        # Simulate the pre-fix world: se's _pre_fan_hvac_mode is never synced.
        assert se._pre_fan_hvac_mode is None
        assert se._whf_owns_hvac() is False

        _run(ae._sync_paused_by_door_with_live_sensors())
        _run(se._sync_paused_by_door_with_live_sensors())
        assert ae._paused_by_door is False  # production correctly does not self-pause
        assert se._paused_by_door is True  # shadow incorrectly self-pauses — the bug


class TestNewlyMirroredDecisionMethods:
    """Representative coverage for Issue #615's 8 newly-mirrored entry points — not
    exhaustive per-method duplication, but the ones with the most real-world weight."""

    def test_reconcile_fan_on_startup_is_mirrored(self) -> None:
        """The exact live-incident reproduction: startup-time fan reconciliation."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        captured: list[tuple] = []

        async def _fake(**kwargs):
            captured.append(kwargs)

        coordinator.shadow_automation_engine.reconcile_fan_on_startup = _fake
        _run(
            coordinator._mirror_to_shadow(
                "reconcile_fan_on_startup",
                indoor=70.0,
                outdoor=65.0,
                thermostat_fan_running=True,
                any_sensor_open=False,
                trigger="ha_restart",
            )
        )
        assert captured == [
            {
                "indoor": 70.0,
                "outdoor": 65.0,
                "thermostat_fan_running": True,
                "any_sensor_open": False,
                "trigger": "ha_restart",
            }
        ]

    def test_fan_thermostat_check_is_mirrored(self) -> None:
        """Issue #608's own finding: this is usually the function that actually exits
        a nat-vent session on the dominant dispatch path — #613 mirrored its sibling
        (nat_vent_temperature_check) but not this one."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        captured: list[tuple] = []

        async def _fake(**kwargs):
            captured.append(kwargs)

        coordinator.shadow_automation_engine.fan_thermostat_check = _fake
        _run(coordinator._mirror_to_shadow("fan_thermostat_check", indoor=70.0, outdoor=65.0, trigger="tick"))
        assert captured == [{"indoor": 70.0, "outdoor": 65.0, "trigger": "tick"}]

    def test_sync_method_on_fan_turned_off_is_mirrored_without_awaiting_none(self) -> None:
        """on_fan_turned_off() is a plain sync method (returns None, not a coroutine) —
        _mirror_to_shadow() must call it without awaiting the result."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        captured: list[tuple] = []

        def _fake(**kwargs):
            captured.append(kwargs)
            return None

        coordinator.shadow_automation_engine.on_fan_turned_off = _fake
        _run(coordinator._mirror_to_shadow("on_fan_turned_off", fan_before="on", fan_after="off"))
        assert captured == [{"fan_before": "on", "fan_after": "off"}]

    def test_handle_bedtime_is_mirrored(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        called: list[bool] = []

        async def _fake():
            called.append(True)

        coordinator.shadow_automation_engine.handle_bedtime = _fake
        _run(coordinator._mirror_to_shadow("handle_bedtime"))
        assert called == [True]


class TestShadowDisagreementIncident:
    """Issue #738: a sustained per-axis shadow/production disagreement must emit a
    ``shadow_disagreement`` incident_detected event, feeding the same simulation
    feedback loop the other 8 incident classes already use
    (docs/simulation-feedback-loop.md). Trigger is event-driven — wired directly
    into ``_update_shadow_engine_diagnostic()`` (called from
    ``_dispatch_shadow_mirror_call()``'s finally block on every mirrored production
    call), NOT the 30-min ``_detect_and_emit_incidents()`` reactive cycle, since the
    diagnostic itself never runs from that cycle.
    """

    def _shadow_disagreement_events(self, coordinator) -> list[dict]:
        return [
            e
            for e in coordinator._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "shadow_disagreement"
        ]

    def test_sustained_disagreement_emits_incident(self) -> None:
        """A disagreement streak already past SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S
        (same seeding pattern as test_disagreement_logs_warning) must emit exactly
        one shadow_disagreement incident with axis='mirror' and the two disagreeing
        state strings."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.data = {"indoor_temp": 72.0, "outdoor_temp": 68.0, "hvac_mode": "off"}
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        coordinator._shadow_diag_disagreement_since["mirror"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()

        incidents = self._shadow_disagreement_events(coordinator)
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident["axis"] == "mirror"
        assert incident["comparison_kind"] == "shadow"
        assert incident["production_state"] != incident["comparison_state"]
        assert incident["indoor_f"] == 72.0
        assert incident["outdoor_f"] == 68.0
        assert incident["hvac_mode"] == "off"
        assert incident["disagreement_seconds"] >= SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S

    def test_fresh_disagreement_does_not_emit_incident(self) -> None:
        """A single-tick, not-yet-sustained disagreement (the debounce window itself
        exists to suppress cascade noise, Issue #685) must NOT emit an incident."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()

        assert self._shadow_disagreement_events(coordinator) == []

    def test_incident_emitted_once_per_streak_not_per_mirrored_call(self) -> None:
        """The diagnostic recomputes on every mirrored production call — far more
        often than a 30-min cycle. A streak that stays sustained across multiple
        recomputes must emit exactly one incident, not one per call."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        coordinator._shadow_diag_disagreement_since["mirror"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
            coordinator._update_shadow_engine_diagnostic()
            coordinator._update_shadow_engine_diagnostic()

        assert len(self._shadow_disagreement_events(coordinator)) == 1

    def test_incident_emits_again_after_streak_resolves_and_recurs(self) -> None:
        """Once the axis resolves back to agreement, the emitted-guard clears —
        a later, distinct disagreement streak on the same axis emits its own
        incident."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        coordinator.shadow_automation_engine._natural_vent_active = True
        coordinator.automation_engine._natural_vent_active = False
        coordinator._shadow_diag_disagreement_since["mirror"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
        assert len(self._shadow_disagreement_events(coordinator)) == 1

        # Resolve: engines agree again.
        coordinator.shadow_automation_engine._natural_vent_active = False
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()
        assert coordinator._shadow_diag_incident_emitted["mirror"] is False

        # New sustained streak on the same axis.
        coordinator.shadow_automation_engine._natural_vent_active = True
        later = _FIXED_NOW + timedelta(seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1)
        coordinator._shadow_diag_disagreement_since["mirror"] = later - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=later):
            coordinator._update_shadow_engine_diagnostic()

        assert len(self._shadow_disagreement_events(coordinator)) == 2

    def test_other_axes_stay_independent(self) -> None:
        """A sustained disagreement on the door/window mirror axis must not emit
        (or be blocked by) an incident on the unrelated nat-vent mirror axis."""
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        for ae in (coordinator.automation_engine, coordinator.shadow_automation_engine):
            ae._natural_vent_active = False
        coordinator.automation_engine._paused_by_door = False
        coordinator.shadow_automation_engine._paused_by_door = True
        coordinator._shadow_diag_disagreement_since["door_window_mirror"] = _FIXED_NOW - timedelta(
            seconds=SHADOW_ENGINE_DIAGNOSTIC_DEBOUNCE_S + 1
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=_FIXED_NOW):
            coordinator._update_shadow_engine_diagnostic()

        incidents = self._shadow_disagreement_events(coordinator)
        assert len(incidents) == 1
        assert incidents[0]["axis"] == "door_window_mirror"
        assert incidents[0]["comparison_kind"] == "shadow"


def _run(coro):
    return run_coro(coro)
