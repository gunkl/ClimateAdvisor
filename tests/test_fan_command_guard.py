"""Regression tests for the _fan_command_time race guard (Issue #239).

The guard prevents CA's own fan activation/deactivation from being falsely
detected as a manual override when the cloud thermostat echo arrives after
_fan_command_pending has already cleared.

Three scenarios:
1. Recent fan command (within 30 s) → thermostat echo (fan_mode "auto" → "on")
   → handle_fan_manual_override NOT called (guard suppresses it).
2. Stale fan command (45 s ago) → fan_mode changes → override correctly detected.
3. Recent fan command (deactivation) → fan_mode changes "on" → "auto"
   → NOT detected as override.

Also tests the _async_fan_entity_changed path for belt-and-suspenders coverage.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

_NOW = datetime(2026, 6, 11, 14, 0, 0)


@contextmanager
def _fixed_now(when: datetime):
    """Temporarily pin coordinator.dt_util.now() to a fixed datetime.

    coordinator.py resolves dt_util as sys.modules["homeassistant.util"].dt
    (the attribute on the parent MagicMock), NOT sys.modules["homeassistant.util.dt"].
    We patch only the coordinator module's name so other modules (chart_log, etc.)
    that share the same MagicMock are unaffected.
    """
    coordinator_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    with patch.object(coordinator_mod.dt_util, "now", return_value=when):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(state_value: str, fan_mode: str = "auto") -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = {
        "hvac_action": "idle",
        "temperature": 70.0,
        "fan_mode": fan_mode,
    }
    return s


def _make_thermostat_event(old_state: MagicMock, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


def _make_fan_entity_event(old_state_str: str, new_state_str: str) -> MagicMock:
    old_s = MagicMock()
    old_s.state = old_state_str
    new_s = MagicMock()
    new_s.state = new_state_str
    event = MagicMock()
    event.data = {"old_state": old_s, "new_state": new_s}
    return event


def _make_coord(*, fan_command_time=None):
    """Coordinator stub with real _async_thermostat_changed / _async_fan_entity_changed bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.thermostat",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._fan_active = False
    ae._natural_vent_active = False
    ae._fan_override_active = False
    ae._temp_command_pending = False
    ae._fan_command_time = fan_command_time
    ae._hvac_command_time = None
    ae._temp_command_time = None
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "mild",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 72,
            "today_low": 55,
            "tomorrow_high": 73,
            "tomorrow_low": 56,
            "hvac_mode": "off",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    coord._current_classification = c

    from custom_components.climate_advisor.learning import DailyRecord

    coord._today_record = DailyRecord(date="2026-06-11", day_type="mild", trend_direction="stable")
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
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    coord._async_fan_entity_changed = types.MethodType(ClimateAdvisorCoordinator._async_fan_entity_changed, coord)
    coord._is_recent_hvac_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_hvac_command, coord)
    coord._is_recent_temp_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_temp_command, coord)
    coord._is_recent_fan_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_fan_command, coord)
    return coord


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFanCommandGuard:
    """_is_recent_fan_command suppresses false override on CA's own fan echo."""

    def test_recent_fan_command_suppresses_thermostat_echo(self):
        """fan_command_time=now → fan_mode echo auto→on → override NOT fired.

        Occupant effect: without this guard, CA's own fan activation would
        immediately cancel the fan (override detected → fan stopped), leaving
        the occupant with no ventilation even though CA just turned the fan on.
        """
        coord = _make_coord(fan_command_time=_NOW)

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_stale_fan_command_allows_override_detection(self):
        """fan_command_time=45 s ago → fan_mode changes → override correctly detected.

        Occupant effect: a user who manually adjusts the thermostat fan 45 seconds
        after CA last touched it should still have their preference respected.
        """
        stale_time = _NOW - timedelta(seconds=45)
        coord = _make_coord(fan_command_time=stale_time)
        coord.automation_engine._fan_override_active = False

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()

    def test_none_fan_command_time_allows_override_detection(self):
        """fan_command_time=None → override correctly detected (guard inactive at startup).

        Occupant effect: on a fresh start where CA has never run the fan, a manual
        fan change at the thermostat should always be respected.
        """
        coord = _make_coord(fan_command_time=None)

        old_state = _make_state("cool", fan_mode="auto")
        new_state = _make_state("cool", fan_mode="on")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()

    def test_recent_deactivation_echo_not_detected_as_override(self):
        """fan_command_time=now (deactivation) → fan_mode on→auto echo → NOT override.

        Occupant effect: when CA turns the fan off at bedtime, the thermostat echo
        (fan_mode "on" → "auto") should not re-trigger an override that would
        re-enable the fan and prevent the occupant from sleeping quietly.
        """
        coord = _make_coord(fan_command_time=_NOW)
        coord.automation_engine._fan_active = True  # CA had fan on

        old_state = _make_state("cool", fan_mode="on")
        new_state = _make_state("cool", fan_mode="auto")
        event = _make_thermostat_event(old_state, new_state)

        with _fixed_now(_NOW):
            asyncio.run(coord._async_thermostat_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()


class TestFanEntityChangedGuard:
    """_async_fan_entity_changed also respects _is_recent_fan_command (belt-and-suspenders)."""

    def test_recent_fan_command_suppresses_fan_entity_echo(self):
        """fan_command_time=now → fan entity state off→on → override NOT fired.

        Occupant effect: the whole-house fan entity echo after CA activates it
        must not be treated as a manual override.
        """
        coord = _make_coord(fan_command_time=_NOW)
        coord.automation_engine._fan_active = False

        event = _make_fan_entity_event("off", "on")
        with _fixed_now(_NOW):
            asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_not_called()

    def test_stale_fan_command_fan_entity_override_detected(self):
        """fan_command_time=45 s ago → fan entity on → override correctly detected.

        Occupant effect: manual use of a whole-house fan switch 45 seconds after
        CA last touched it should still be recorded as a user override.
        """
        stale_time = _NOW - timedelta(seconds=45)
        coord = _make_coord(fan_command_time=stale_time)
        coord.automation_engine._fan_active = False

        event = _make_fan_entity_event("off", "on")
        with _fixed_now(_NOW):
            asyncio.run(coord._async_fan_entity_changed(event))

        coord.automation_engine.handle_fan_manual_override.assert_called_once()
