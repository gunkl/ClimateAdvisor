"""Tests for Issue #604 (Block 5, subtask N2): callback-bundle isolation on AutomationEngine.

Covers:
  - Back-compat characterization: no ``callbacks`` arg → all 9 attributes stay None.
  - Bundle application: passing ``AutomationEngineCallbacks`` sets exactly those 9.
  - Coordinator regression: ``_build_production_automation_callbacks()`` is a
    behavior-preserving refactor of the old post-hoc assignment block.

Issue #757 Phase 6 Step 8: the shadow-engine-specific tests that used to live here
(bundle isolation on a live shadow engine, and the hazard-characterization test for
sharing production's callbacks with a second engine) were removed along with the
dual-engine shell itself — there is no second engine to isolate a bundle from
anymore.
"""

from __future__ import annotations

from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
    AutomationEngineCallbacks,
)

_CALLBACK_ATTRS = (
    "_revisit_callback",
    "_sensor_check_callback",
    "_sensor_debounce_pending_callback",
    "_emit_event_callback",
    "_request_refresh_callback",
    "_post_grace_fan_check_callback",
    "_get_fan_physical_state_callback",
    "_is_recent_fan_command_callback",
    "_reclassify_callback",
)


def _make_bare_engine() -> AutomationEngine:
    return AutomationEngine(
        hass=None,
        climate_entity="climate.test",
        weather_entity="weather.test",
        door_window_sensors=[],
        notify_service="notify.test",
        config={},
    )


class TestBackCompatDefault:
    def test_no_callbacks_arg_leaves_all_nine_none(self) -> None:
        engine = _make_bare_engine()
        for attr in _CALLBACK_ATTRS:
            assert getattr(engine, attr) is None, f"{attr} should default to None"
        assert engine.role == "production"


class TestCallbackBundleApplication:
    def test_bundle_sets_exactly_the_nine_attributes(self) -> None:
        def _revisit():
            return None

        def _sensor_check():
            return True

        def _sensor_debounce():
            return False

        def _emit(event_type, data):
            return None

        def _refresh():
            return None

        def _post_grace():
            return None

        def _fan_state():
            return "on"

        def _recent_fan():
            return False

        def _reclassify():
            return None

        bundle = AutomationEngineCallbacks(
            revisit=_revisit,
            sensor_check=_sensor_check,
            sensor_debounce_pending=_sensor_debounce,
            emit_event=_emit,
            request_refresh=_refresh,
            post_grace_fan_check=_post_grace,
            get_fan_physical_state=_fan_state,
            is_recent_fan_command=_recent_fan,
            reclassify=_reclassify,
        )
        engine = AutomationEngine(
            hass=None,
            climate_entity="climate.test",
            weather_entity="weather.test",
            door_window_sensors=[],
            notify_service="notify.test",
            config={},
            callbacks=bundle,
            role="shadow",
        )
        assert engine._revisit_callback is _revisit
        assert engine._sensor_check_callback is _sensor_check
        assert engine._sensor_debounce_pending_callback is _sensor_debounce
        assert engine._emit_event_callback is _emit
        assert engine._request_refresh_callback is _refresh
        assert engine._post_grace_fan_check_callback is _post_grace
        assert engine._get_fan_physical_state_callback is _fan_state
        assert engine._is_recent_fan_command_callback is _recent_fan
        assert engine._reclassify_callback is _reclassify
        assert engine.role == "shadow"

    def test_partial_bundle_leaves_unset_fields_none(self) -> None:
        def _emit(event_type, data):
            return None

        bundle = AutomationEngineCallbacks(emit_event=_emit)
        engine = AutomationEngine(
            hass=None,
            climate_entity="climate.test",
            weather_entity="weather.test",
            door_window_sensors=[],
            notify_service="notify.test",
            config={},
            callbacks=bundle,
        )
        assert engine._emit_event_callback is _emit
        for attr in _CALLBACK_ATTRS:
            if attr == "_emit_event_callback":
                continue
            assert getattr(engine, attr) is None


class TestCoordinatorRefactorIsBehaviorPreserving:
    def test_production_engine_callbacks_are_the_coordinators_own_bound_methods(self) -> None:
        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        ae = coordinator.automation_engine

        assert ae.role == "production"
        assert ae._revisit_callback == coordinator.async_request_refresh
        assert ae._sensor_check_callback == coordinator._any_sensor_open
        # build_headless_coordinator() intentionally overrides _emit_event_callback
        # post-construction (see its own docstring) to redirect into the flat
        # scenario event_log instead of coordinator._emit_event — so this one
        # attribute doesn't stay identical to coordinator._emit_event here. Every
        # other callback is untouched by that harness step and must still match.
        assert ae._post_grace_fan_check_callback == coordinator._on_post_grace_fan_check
        assert ae._get_fan_physical_state_callback == coordinator._get_fan_physical_state
        assert ae._is_recent_fan_command_callback == coordinator._is_recent_fan_command
        assert ae._reclassify_callback == coordinator._on_whf_release_reclassify
        assert ae._sensor_debounce_pending_callback() == bool(coordinator._door_open_timers)
