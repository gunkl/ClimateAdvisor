"""Tests for Fix B: _set_temperature_for_mode uses dual setpoint on capable thermostats.

When natural ventilation ends and the engine restores comfort, it calls
_set_temperature_for_mode(classification, reason=...).  For a dual-setpoint
thermostat (heat_cool in hvac_modes + TARGET_TEMP_RANGE feature) this must
emit a climate.set_temperature call with target_temp_low and target_temp_high
instead of a single temperature value.

Without this fix, the occupant on a dual-setpoint thermostat sees nat-vent
restore push only one setpoint edge — leaving the thermostat partially armed
and potentially drifting past the undefended edge (e.g. home cools below
comfort_heat while only the cooling ceiling was set).

See: GitHub Issue #293
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs — must run before any climate_advisor import ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
)

# ---------------------------------------------------------------------------
# Minimal engine factory — mirrors test_grace_refresh_and_band_call.py
# ---------------------------------------------------------------------------


def _minimal_engine(comfort_heat: float = 68.0, comfort_cool: float = 74.0) -> AutomationEngine:
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
            "config": {
                "temp_unit": "fahrenheit",
                "comfort_heat": comfort_heat,
                "comfort_cool": comfort_cool,
            },
            "sensor_polarity_inverted": False,
            "dry_run": False,
            "_active_listeners": [],
            "_current_classification": None,
            "_paused_by_door": False,
            "_pre_pause_mode": None,
            "_manual_grace_cancel": None,
            "_automation_grace_cancel": None,
            "_grace_active": False,
            "_last_resume_source": None,
            "_grace_end_time": None,
            "_grace_duration_seconds": 0,
            "_manual_override_active": False,
            "_manual_override_mode": None,
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
            "_write_seq": 0,
            "_pending_setpoint_low": None,
            "_pending_setpoint_high": None,
            "_pending_setpoint_single": None,
        }
    )
    return engine


def _make_classification(hvac_mode: str = "cool", pre_condition: bool = False, pre_condition_target=None):
    """Build a minimal classification stub with only the fields _set_temperature_for_mode reads."""
    c = MagicMock()
    c.hvac_mode = hvac_mode
    c.pre_condition = pre_condition
    c.pre_condition_target = pre_condition_target
    return c


def _dual_setpoint_state():
    """Return a mock HA state for a thermostat that supports heat_cool + TARGET_TEMP_RANGE."""
    state = MagicMock()
    state.state = "cool"
    state.attributes = {
        "hvac_modes": ["off", "heat", "cool", "heat_cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
    }
    return state


def _single_setpoint_state():
    """Return a mock HA state for a thermostat that supports only heat/cool (no heat_cool)."""
    state = MagicMock()
    state.state = "cool"
    state.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,  # no TARGET_TEMP_RANGE
    }
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNatVentRestoreDualSetpoint:
    """_set_temperature_for_mode must use dual setpoints on capable thermostats."""

    def test_dual_setpoint_thermostat_uses_dual_call(self):
        """Dual-setpoint thermostat on cool/pre-condition day → target_temp_low + target_temp_high.

        Occupant impact: without this fix, nat-vent restore only sets the cooling
        ceiling (target_temp_high) and ignores the heating floor — leaving the
        thermostat unbalanced.  The home can drift below comfort_heat overnight.
        """
        engine = _minimal_engine(comfort_heat=68.0, comfort_cool=74.0)
        engine.hass.states.get.return_value = _dual_setpoint_state()

        classification = _make_classification(
            hvac_mode="cool",
            pre_condition=True,
            pre_condition_target=-2.0,
        )

        with patch("custom_components.climate_advisor.automation.async_call_later"):
            asyncio.run(engine._set_temperature_for_mode(classification, reason="nat vent restore"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate" and c.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(climate_calls) == 2, f"Expected 2 set_temperature calls, got {len(climate_calls)}"

        payload = climate_calls[1].args[2]  # target write has the correct setpoints
        # Dual path: must carry both setpoints
        assert "target_temp_high" in payload, f"Expected target_temp_high in payload, got {payload}"
        assert "target_temp_low" in payload, f"Expected target_temp_low in payload, got {payload}"
        # Ceiling: comfort_cool (74) + pre_condition_target (-2) = 72°F
        assert abs(payload["target_temp_high"] - 72.0) < 0.1, (
            f"Expected target_temp_high=72.0, got {payload['target_temp_high']}"
        )
        # Floor: comfort_heat = 68°F
        assert abs(payload["target_temp_low"] - 68.0) < 0.1, (
            f"Expected target_temp_low=68.0, got {payload['target_temp_low']}"
        )
        # Single-setpoint key must NOT be present
        assert "temperature" not in payload, (
            f"Single-setpoint 'temperature' key must not appear in dual payload: {payload}"
        )

    def test_single_setpoint_thermostat_uses_single_call(self):
        """Single-setpoint thermostat on cool/pre-condition day → single temperature value.

        Occupant impact: a cool-only thermostat cannot accept target_temp_low/high;
        sending dual setpoints would be silently rejected, leaving no active setpoint.
        The function must fall back to the single temperature path for these devices.
        """
        engine = _minimal_engine(comfort_heat=68.0, comfort_cool=74.0)
        engine.hass.states.get.return_value = _single_setpoint_state()

        classification = _make_classification(
            hvac_mode="cool",
            pre_condition=True,
            pre_condition_target=-2.0,
        )

        with patch("custom_components.climate_advisor.automation.async_call_later"):
            asyncio.run(engine._set_temperature_for_mode(classification, reason="nat vent restore"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate" and c.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(climate_calls) == 2, f"Expected 2 set_temperature calls, got {len(climate_calls)}"

        payload = climate_calls[1].args[2]  # target write has the correct setpoint
        # Single path: must carry only temperature
        assert "temperature" in payload, f"Expected 'temperature' in single-setpoint payload, got {payload}"
        assert abs(payload["temperature"] - 72.0) < 0.1, (
            f"Expected temperature=72.0 (74 + -2), got {payload['temperature']}"
        )
        # Dual keys must NOT be present
        assert "target_temp_high" not in payload, (
            f"Dual 'target_temp_high' must not appear in single-setpoint payload: {payload}"
        )

    def test_heat_mode_dual_setpoint(self):
        """Heat classification on dual-setpoint thermostat → dual call with floor=comfort_heat.

        Occupant impact: on a cold day, nat-vent restore with a dual-setpoint thermostat
        must arm BOTH edges so the thermostat prevents over-cooling (floor) and over-heating
        (ceiling).  Sending only the floor leaves the ceiling undefended.
        """
        engine = _minimal_engine(comfort_heat=70.0, comfort_cool=76.0)

        # Thermostat currently in heat mode
        state = MagicMock()
        state.state = "heat"
        state.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
        }
        engine.hass.states.get.return_value = state

        classification = _make_classification(hvac_mode="heat")

        with patch("custom_components.climate_advisor.automation.async_call_later"):
            asyncio.run(engine._set_temperature_for_mode(classification, reason="nat vent restore heat"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate" and c.args[1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(climate_calls) == 2, f"Expected 2 set_temperature calls, got {len(climate_calls)}"

        payload = climate_calls[1].args[2]  # target write has the correct setpoints
        # Dual path: must carry both setpoints
        assert "target_temp_low" in payload, f"Expected target_temp_low in dual payload, got {payload}"
        assert "target_temp_high" in payload, f"Expected target_temp_high in dual payload, got {payload}"
        # Floor: comfort_heat = 70°F
        assert abs(payload["target_temp_low"] - 70.0) < 0.1, (
            f"Expected target_temp_low=70.0, got {payload['target_temp_low']}"
        )
        # Ceiling: comfort_cool = 76°F
        assert abs(payload["target_temp_high"] - 76.0) < 0.1, (
            f"Expected target_temp_high=76.0, got {payload['target_temp_high']}"
        )
        assert "temperature" not in payload, (
            f"Single-setpoint 'temperature' key must not appear in dual payload: {payload}"
        )
