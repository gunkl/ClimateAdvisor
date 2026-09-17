"""Regression test for Issue #903 Bug 2: coordinator.py's
``_async_thermostat_changed()`` nat-vent temperature-check call site
(coordinator.py ~4994-5003).

Prior to this fix, the raw ``current_temperature`` attribute off the thermostat
state was passed straight into ``nat_vent_temperature_check()`` without going
through the ``to_fahrenheit()`` conversion boundary:

    _new_temp_attr = new_state.attributes.get("current_temperature")
    ...
    await self.automation_engine.nat_vent_temperature_check(
        float(_new_temp_attr), outdoor=self._last_outdoor_temp
    )

``nat_vent_temperature_check()``'s contract (and its downstream comparisons
against ``comfort_heat``/``comfort_cool``/``nat_vent_target``) is internal
Fahrenheit — confirmed via the other two call sites of the same function
(automation.py), which both resolve indoor temp through ``_get_indoor_temp()``
first. On a Celsius-configured install, a raw ``20.0`` (68°F) reading would be
handed to the check as if it were already ``20.0°F`` — a 48°F error that could
leave nat-vent running well past the real comfort floor, or fail to cycle the
fan at the correct indoor temperature.

The fix routes the read through ``temperature.read_state_temp_f()`` (the new
Issue #903 shared conversion helper) before calling
``nat_vent_temperature_check()``. This test asserts the value the coordinator
actually passes downstream is the converted internal-Fahrenheit value, not the
raw Celsius number.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(*, hvac_mode: str, current_temperature: float, temperature: float = 70.0) -> MagicMock:
    s = MagicMock()
    s.state = hvac_mode
    s.attributes = {
        "hvac_action": "idle",
        "temperature": temperature,
        "fan_mode": "auto",
        "current_temperature": current_temperature,
    }
    return s


def _make_thermostat_event(old_state: MagicMock, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


def _make_coord(*, temp_unit: str) -> MagicMock:
    """Coordinator stub with the real _async_thermostat_changed bound (Issue #903).

    Mirrors the established object.__new__() + types.MethodType partial-
    instantiation pattern from test_fan_command_guard.py's ``_make_coord``.
    old_state.state == new_state.state throughout this test module so every
    mode-change/override-detection branch in the (very long) production
    method is skipped — only the nat-vent temperature-check block (which
    fires on any current_temperature tick regardless of mode change) is
    exercised.
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.test",
        "comfort_heat": 68,
        "comfort_cool": 75,
        "temp_unit": temp_unit,
    }

    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._fan_active = False
    ae._natural_vent_active = True  # required for the nat-vent check to fire
    ae._temp_command_pending = False
    ae._fan_command_time = None
    ae._hvac_command_time = None
    ae._temp_command_time = None
    ae._recent_fan_command_context_ids = []
    ae.fan_command_context_matches = MagicMock(return_value=False)
    ae.nat_vent_temperature_check = AsyncMock()
    ae.fan_thermostat_check = AsyncMock()
    ae.comfort_family_temperature_check = AsyncMock()
    coord.automation_engine = ae

    coord._current_classification = None
    coord._today_record = None
    coord._async_save_state = AsyncMock()
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = MagicMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._last_outdoor_temp = 65.0
    coord._last_predicted_indoor = None
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()
    coord._startup_coalesce_active = False

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    coord._is_recent_hvac_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_hvac_command, coord)
    coord._is_recent_temp_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_temp_command, coord)
    coord._is_recent_fan_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_fan_command, coord)
    return coord


class TestNatVentTemperatureCheckUnitConversion:
    """coordinator.py's nat-vent check call site must convert to internal °F."""

    def test_celsius_current_temperature_converted_before_nat_vent_check(self):
        """A raw 20.0°C current_temperature must reach nat_vent_temperature_check()
        as 68.0°F, not as a bare 20.0 misread as Fahrenheit."""
        coord = _make_coord(temp_unit="celsius")

        old_state = _make_state(hvac_mode="cool", current_temperature=19.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=20.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_awaited_once()
        call_args = coord.automation_engine.nat_vent_temperature_check.call_args
        passed_temp = call_args[0][0]
        assert passed_temp == pytest.approx(68.0), (
            f"Expected 20.0°C converted to 68.0°F, got {passed_temp} "
            "(Issue #903: raw Celsius attribute was passed through unconverted)"
        )
        assert call_args[1]["outdoor"] == pytest.approx(65.0)

    def test_fahrenheit_current_temperature_passed_through_unchanged(self):
        """Fahrenheit-configured installs are unaffected — a raw 68.0°F reading
        still reaches nat_vent_temperature_check() as 68.0°F (identity passthrough)."""
        coord = _make_coord(temp_unit="fahrenheit")

        old_state = _make_state(hvac_mode="cool", current_temperature=67.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_awaited_once()
        passed_temp = coord.automation_engine.nat_vent_temperature_check.call_args[0][0]
        assert passed_temp == pytest.approx(68.0)

    def test_no_crash_and_no_check_when_current_temperature_missing(self):
        """current_temperature attribute absent (e.g. thermostat momentarily
        unavailable) → read_state_temp_f() returns None → the check is skipped
        gracefully rather than crashing, consistent with this codebase's
        graceful-degradation pattern. The outer gate (`_new_temp_attr is not
        None`) already skips the sibling fan_thermostat_check/
        comfort_family_temperature_check calls in this same case, so nothing
        downstream sees a missing reading either."""
        coord = _make_coord(temp_unit="celsius")

        old_state = _make_state(hvac_mode="cool", current_temperature=19.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=19.0)
        del new_state.attributes["current_temperature"]
        event = _make_thermostat_event(old_state, new_state)

        # Must not raise.
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_not_awaited()

    def test_not_called_when_nat_vent_inactive(self):
        """No nat-vent session active → check is not invoked at all (unchanged
        behavior — this test just guards against a regression in the outer gate)."""
        coord = _make_coord(temp_unit="celsius")
        coord.automation_engine._natural_vent_active = False

        old_state = _make_state(hvac_mode="cool", current_temperature=19.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=20.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_not_awaited()
