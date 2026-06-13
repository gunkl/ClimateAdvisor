"""Tests for Fix 1 (grace-expiry refresh callback) and Fix 4 (single service call in
_apply_comfort_band for dual-setpoint thermostats).

Fix 1 — Issue #290: When a grace period expires, the engine must call
_request_refresh_callback so the coordinator immediately pushes updated
sensor state to HA. Without this, the occupant sees stale sensor values
until the next 30-min poll.

Fix 4 — Issue #290: _apply_comfort_band for a dual-setpoint thermostat must
emit ONE climate.set_temperature call (with hvac_mode embedded in the
payload). The former code emitted a separate set_hvac_mode call first, which
created a short window where Ecobee could revert to its own schedule before
the setpoints arrived.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs — must run before any climate_advisor import ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
    ComfortBand,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
)

# ---------------------------------------------------------------------------
# Minimal engine factory — bypasses full HA wiring, sets only what each test
# needs.
# ---------------------------------------------------------------------------


def _minimal_engine() -> AutomationEngine:
    """Return an AutomationEngine with all HA interactions stubbed."""
    hass = MagicMock()
    hass.states.get.return_value = None
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

    engine = object.__new__(AutomationEngine)
    engine.__dict__.update(
        {
            "hass": hass,
            "climate_entity": "climate.thermostat",
            "weather_entity": "weather.home",
            "door_window_sensors": [],
            "notify_service": "notify.test",
            "config": {"temp_unit": "fahrenheit", "comfort_heat": 68, "comfort_cool": 76},
            "sensor_polarity_inverted": False,
            "dry_run": False,
            "_active_listeners": [],
            "_current_classification": None,
            "_paused_by_door": False,
            "_pre_pause_mode": None,
            "_manual_grace_cancel": None,
            "_automation_grace_cancel": None,
            "_grace_active": True,  # grace is active when expiry fires
            "_last_resume_source": "manual",
            "_grace_end_time": None,
            "_grace_duration_seconds": 0,
            "_manual_override_active": True,
            "_manual_override_mode": "cool",
            "_manual_override_time": None,
            "_last_override_detected_time": None,
            "_sensor_check_callback": None,
            "_emit_event_callback": None,
            "_request_refresh_callback": None,
            "_revisit_callback": None,
            "_revisit_cancel": None,
            "_fan_active": False,
            "_fan_override_active": False,
            "_fan_override_time": None,
            "_fan_command_pending": False,
            "_fan_on_since": None,
            "_pre_fan_hvac_mode": None,
            "_hvac_command_pending": False,
            "_temp_command_pending": False,
            "_temp_command_time": None,
            "_hvac_command_time": None,
            "_fan_command_time": None,
            "_last_commanded_hvac_mode": None,
            "_last_commanded_hvac_time": None,
            "_natural_vent_active": False,
            "_economizer_active": False,
            "_economizer_phase": "inactive",
            "_last_action_time": None,
            "_last_action_reason": None,
            "_nat_vent_outdoor_exit_time": None,
            "_override_confirm_pending": False,
            "_override_confirm_cancel": None,
            "_override_confirm_time": None,
            "_override_confirm_mode": None,
            "_override_confirm_source": None,
            "_fan_min_runtime_active": False,
            "_fan_min_cycle_cancel": None,
            "_today_record": None,
            "_last_classification_applied": None,
            "_resumed_from_pause": False,
            "_last_welcome_home_notified": None,
            "_thermal_model": {},
            "_hourly_forecast_temps": [],
            "_occupancy_mode": "home",
        }
    )
    return engine


# ---------------------------------------------------------------------------
# FIX 1: Grace-expiry refresh callback
# ---------------------------------------------------------------------------


class TestGraceExpiryTriggersRefreshCallback:
    """Fix 1 — _request_refresh_callback must be called after grace expiry
    clears the override, on all three paths of _on_grace_expired."""

    def test_normal_expiry_calls_refresh_callback(self):
        """Normal path (no open sensors, not in planned window): callback fires."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Confirm no planned-window override and no sensor open
        engine._sensor_check_callback = None  # no sensors
        # _is_within_planned_window_period must return False
        engine._current_classification = None  # no classification → not in window

        engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_planned_window_path_calls_refresh_callback(self):
        """Planned-window path: callback fires after clearing grace."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Make _is_within_planned_window_period return True by patching
        engine._current_classification = MagicMock()
        engine._current_classification.windows_recommended = True

        import unittest.mock as mock

        with mock.patch.object(
            type(engine),
            "_is_within_planned_window_period",
            return_value=True,
        ):
            engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_sensor_still_open_path_calls_refresh_callback(self):
        """Re-pause path (sensor still open): callback fires after clearing grace."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Sensor check returns True → re-pause path
        engine._sensor_check_callback = lambda: True

        # Stub _re_pause_for_open_sensor so hass.async_create_task gets a coroutine
        async def _fake_repause():
            pass

        engine._re_pause_for_open_sensor = _fake_repause  # type: ignore[method-assign]

        import unittest.mock as mock

        with mock.patch.object(
            type(engine),
            "_is_within_planned_window_period",
            return_value=False,
        ):
            engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_no_callback_registered_does_not_raise(self):
        """If callback is None (not wired by coordinator), expiry still completes."""
        engine = _minimal_engine()
        engine._request_refresh_callback = None
        engine._sensor_check_callback = None

        # Must not raise
        engine._on_grace_expired(source="manual", duration=1800, should_notify=False)
        assert engine._grace_active is False


# ---------------------------------------------------------------------------
# FIX 4: Single service call in _apply_comfort_band (dual-setpoint path)
# ---------------------------------------------------------------------------


class TestApplyComfortBandSingleServiceCall:
    """Fix 4 — For a dual-setpoint thermostat in 'cool' mode, _apply_comfort_band
    must issue exactly ONE climate.set_temperature call, NOT a separate
    set_hvac_mode call first.

    The hvac_mode must appear INSIDE the set_temperature payload so Ecobee
    receives mode + setpoints atomically.
    """

    def test_single_async_call_for_dual_setpoint_thermostat(self):
        """Exactly one async_call to climate domain; hvac_mode in the payload."""
        engine = _minimal_engine()

        # Thermostat is currently in 'cool' mode (would previously trigger a mode switch)
        state_mock = MagicMock()
        state_mock.state = "cool"
        state_mock.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
        }
        engine.hass.states.get.return_value = state_mock

        band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")

        asyncio.run(engine._apply_comfort_band(band, reason="test"))

        # Exactly one service call — no separate set_hvac_mode call
        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate"]
        assert len(climate_calls) == 1, f"Expected 1 climate service call, got {len(climate_calls)}: {climate_calls}"

        # The single call must be set_temperature (not set_hvac_mode)
        the_call = climate_calls[0]
        assert the_call.args[1] == "set_temperature", f"Expected set_temperature, got {the_call.args[1]}"

        # hvac_mode must be embedded in the payload
        payload = the_call.args[2]
        assert payload.get("hvac_mode") == "heat_cool", f"Expected hvac_mode='heat_cool' in payload, got {payload}"
        assert "target_temp_low" in payload
        assert "target_temp_high" in payload

    def test_already_in_heat_cool_mode_still_single_call(self):
        """Thermostat already in heat_cool: still just one set_temperature call."""
        engine = _minimal_engine()

        state_mock = MagicMock()
        state_mock.state = "heat_cool"
        state_mock.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
        }
        engine.hass.states.get.return_value = state_mock

        band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")

        asyncio.run(engine._apply_comfort_band(band, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate"]
        assert len(climate_calls) == 1
        assert climate_calls[0].args[1] == "set_temperature"
