"""Regression test for coordinator.py's ``_async_thermostat_changed()`` per-tick
reactive-check call site (coordinator.py's ``_async_run_temp_reactive_checks()``).

History: Issue #903 fixed a bug where the raw ``current_temperature`` attribute off
the thermostat state was passed straight into ``nat_vent_temperature_check()``
without unit conversion, via a dedicated ``read_state_temp_f()`` call inline in
``_async_thermostat_changed()``. Issue #964 replaced that per-call-site read (and two
sibling reads with the identical shape feeding ``fan_thermostat_check()`` and
``comfort_family_temperature_check()``) with a single choke point,
``_async_run_temp_reactive_checks()``, that resolves indoor temperature exactly once
via ``self._get_indoor_temp()`` — the sleep-aware resolver that already handles unit
conversion (covered directly by ``test_indoor_temp_helper.py``) and additionally
swaps to ``sleep_indoor_temp_entity`` during the sleep window, which the old
raw-attribute read never did.

This file now asserts the coupling that matters post-#964: whatever
``self._get_indoor_temp()`` resolves to is what reaches all three reactive checks,
independent of the raw ``current_temperature`` attribute's own value — the raw
attribute is used ONLY as the "did something change" dirty-check gate, never as the
decision value itself.
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


def _make_coord(*, temp_unit: str, resolved_indoor: float = 72.0) -> MagicMock:
    """Coordinator stub with the real _async_thermostat_changed +
    _async_run_temp_reactive_checks bound (Issue #964).

    Mirrors the established object.__new__() + types.MethodType partial-
    instantiation pattern from test_fan_command_guard.py's ``_make_coord``.
    old_state.state == new_state.state throughout this test module so every
    mode-change/override-detection branch in the (very long) production
    method is skipped — only the per-tick reactive-check block (which fires
    on any current_temperature tick regardless of mode change) is exercised.

    ``_get_indoor_temp`` is stubbed to return ``resolved_indoor`` — a fixed,
    caller-controlled value standing in for whatever the real sleep-aware
    resolver would produce, so these tests can assert the coupling
    (resolved value reaches the checks) without re-testing the resolver's
    own unit-conversion/sleep-sensor-swap logic, which test_indoor_temp_helper.py
    already covers directly.
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
    coord._get_indoor_temp = MagicMock(return_value=resolved_indoor)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._last_outdoor_temp = 65.0
    coord._last_predicted_indoor = None
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()
    coord._startup_coalesce_active = False

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    coord._async_run_temp_reactive_checks = types.MethodType(
        ClimateAdvisorCoordinator._async_run_temp_reactive_checks, coord
    )
    coord._is_recent_hvac_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_hvac_command, coord)
    coord._is_recent_temp_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_temp_command, coord)
    coord._is_recent_fan_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_fan_command, coord)
    return coord


class TestNatVentTemperatureCheckUsesResolvedIndoor:
    """coordinator.py's per-tick reactive checks must use self._get_indoor_temp()'s
    resolved value, never the raw current_temperature attribute directly."""

    def test_resolved_value_reaches_nat_vent_check_not_raw_attribute(self):
        """The value passed to nat_vent_temperature_check() must be whatever
        _get_indoor_temp() resolves to — even when that differs from the raw
        current_temperature attribute on the triggering state-change event
        (exactly what happens on a sleep-sensor-configured install, where the
        hallway thermostat's attribute triggers the tick but the resolver swaps
        to the bedroom sensor's value)."""
        coord = _make_coord(temp_unit="fahrenheit", resolved_indoor=66.0)

        # Raw attribute reads 69°F (e.g. the hallway thermostat) — deliberately
        # different from the resolved value (66°F, e.g. the bedroom sensor) to
        # prove the raw number never leaks into the decision.
        old_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=69.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_awaited_once()
        call_args = coord.automation_engine.nat_vent_temperature_check.call_args
        passed_temp = call_args[0][0]
        assert passed_temp == pytest.approx(66.0), (
            f"Expected the resolved indoor value (66.0) to reach nat_vent_temperature_check(), "
            f"got {passed_temp} (Issue #964: raw attribute must never be used as the decision value)"
        )
        assert call_args[1]["outdoor"] == pytest.approx(65.0)

    def test_resolved_value_reaches_fan_and_comfort_family_checks_too(self):
        """The same resolved value must reach fan_thermostat_check() and
        comfort_family_temperature_check() — the two sibling checks Issue #964
        consolidated into the same choke point as the nat-vent check."""
        coord = _make_coord(temp_unit="fahrenheit", resolved_indoor=66.0)
        coord.automation_engine._fan_active = True

        old_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=69.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.fan_thermostat_check.assert_awaited_once()
        assert coord.automation_engine.fan_thermostat_check.call_args[1]["indoor"] == pytest.approx(66.0)

        coord.automation_engine.comfort_family_temperature_check.assert_awaited_once()
        comfort_call_args = coord.automation_engine.comfort_family_temperature_check.call_args
        assert comfort_call_args[0][0] == pytest.approx(66.0)

    def test_no_crash_and_no_check_when_resolved_indoor_missing(self):
        """_get_indoor_temp() returning None (e.g. every source unavailable) must
        skip all three checks gracefully rather than crashing, consistent with
        this codebase's graceful-degradation pattern."""
        coord = _make_coord(temp_unit="fahrenheit", resolved_indoor=None)
        coord.automation_engine._fan_active = True

        old_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=69.0)
        event = _make_thermostat_event(old_state, new_state)

        # Must not raise.
        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_not_awaited()
        coord.automation_engine.fan_thermostat_check.assert_not_awaited()
        coord.automation_engine.comfort_family_temperature_check.assert_not_awaited()

    def test_no_check_when_current_temperature_attribute_missing(self):
        """current_temperature attribute absent on the triggering event (e.g.
        thermostat momentarily unavailable) → the dirty-check gate skips the
        whole reactive-check call, exactly as before Issue #964 — this is the
        one place the raw attribute still legitimately matters (as a trigger
        gate, not a decision value)."""
        coord = _make_coord(temp_unit="fahrenheit")

        old_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        del new_state.attributes["current_temperature"]
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_not_awaited()

    def test_not_called_when_nat_vent_inactive(self):
        """No nat-vent session active → nat-vent check is not invoked at all
        (unchanged behavior — this test just guards against a regression in the
        outer gate)."""
        coord = _make_coord(temp_unit="fahrenheit")
        coord.automation_engine._natural_vent_active = False

        old_state = _make_state(hvac_mode="cool", current_temperature=68.0)
        new_state = _make_state(hvac_mode="cool", current_temperature=69.0)
        event = _make_thermostat_event(old_state, new_state)

        asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.nat_vent_temperature_check.assert_not_awaited()
