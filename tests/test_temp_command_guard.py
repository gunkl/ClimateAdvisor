"""Tests for _temp_command_time race guard (Issue #221).

Verifies that a thermostat setpoint change fired by the integration's own
_set_temperature() call does NOT get recorded as a manual override, even after
_temp_command_pending has been cleared (the finally block runs before
_async_thermostat_changed fires).

Issue #301: _set_temperature_dual() removed; all writes use single-setpoint.

Test coverage:
1. Setpoint change within 30 s of _set_temperature() → no manual override
2. Setpoint change after 30 s of _set_temperature() → manual override recorded
3. _temp_command_time is set by _set_temperature()
4. _is_recent_temp_command() returns False when _temp_command_time is None
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 7, 12, 0, 0)


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _get_automation_class():
    mod = importlib.import_module("custom_components.climate_advisor.automation")
    return mod.AutomationEngine


def _consume_coroutine(coro):
    coro.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(temp: float, hvac_action: str = "idle") -> MagicMock:
    s = MagicMock()
    s.state = "heat"
    s.attributes = {
        "hvac_action": hvac_action,
        "temperature": temp,
        "fan_mode": "auto",
    }
    return s


def _make_thermostat_event(old_state: MagicMock, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


def _make_coord(*, temp_command_time: datetime | None = None):
    """Coordinator stub with real _async_thermostat_changed and _is_recent_temp_command bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    coord.hass = hass
    coord.config = {
        "climate_entity": "climate.test",
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
    ae._temp_command_pending = False  # cleared (as in production post-finally)
    ae._temp_command_time = temp_command_time
    ae._hvac_command_time = None
    ae._fan_command_time = None
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    # Must be explicitly None (not truthy MagicMock) so Bug C/D _ca_active_mode
    # falls back to classification.hvac_mode correctly.
    ae._last_commanded_hvac_mode = None
    ae._last_commanded_hvac_time = None
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
            "hvac_mode": "heat",
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

    coord._today_record = DailyRecord(date="2026-06-07", day_type="mild", trend_direction="stable")
    coord._async_save_state = AsyncMock()
    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = MagicMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=55.0)
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()

    # Bind real methods under test
    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    coord._is_recent_temp_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_temp_command, coord)

    return coord


# ---------------------------------------------------------------------------
# Tests: _is_recent_temp_command
# ---------------------------------------------------------------------------


class TestIsRecentTempCommand:
    def test_returns_false_when_time_is_none(self):
        """_is_recent_temp_command() returns False when _temp_command_time is None."""
        coord = _make_coord(temp_command_time=None)
        now = datetime(2026, 6, 7, 12, 0, 0)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            result = coord._is_recent_temp_command(threshold_seconds=30.0)
        assert result is False

    def test_returns_true_within_threshold(self):
        """_is_recent_temp_command() returns True when command was < 30 s ago."""
        now = datetime(2026, 6, 7, 12, 0, 0)
        cmd_time = now - timedelta(seconds=10)
        coord = _make_coord(temp_command_time=cmd_time)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            result = coord._is_recent_temp_command(threshold_seconds=30.0)
        assert result is True

    def test_returns_false_after_threshold(self):
        """_is_recent_temp_command() returns False when command was > 30 s ago."""
        now = datetime(2026, 6, 7, 12, 0, 0)
        cmd_time = now - timedelta(seconds=31)
        coord = _make_coord(temp_command_time=cmd_time)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            result = coord._is_recent_temp_command(threshold_seconds=30.0)
        assert result is False

    def test_returns_true_at_boundary(self):
        """_is_recent_temp_command() returns True at exactly 29 s (< threshold)."""
        now = datetime(2026, 6, 7, 12, 0, 0)
        cmd_time = now - timedelta(seconds=29)
        coord = _make_coord(temp_command_time=cmd_time)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            result = coord._is_recent_temp_command(threshold_seconds=30.0)
        assert result is True


# ---------------------------------------------------------------------------
# Tests: setpoint change in _async_thermostat_changed with guard active
# ---------------------------------------------------------------------------


class TestSetpointChangeGuard:
    def test_no_override_within_30s_of_set_temperature(self):
        """Setpoint change within 30 s of _set_temperature() does NOT trigger manual override.

        This is the core regression: away setback fires _set_temperature() (72→79°F),
        _temp_command_pending is cleared in the finally block, then
        _async_thermostat_changed fires with the new setpoint. Without the time guard
        this was wrongly recorded as a user manual override.
        """
        now = datetime(2026, 6, 7, 12, 0, 0)
        cmd_time = now - timedelta(seconds=5)  # command was 5 s ago — within guard
        coord = _make_coord(temp_command_time=cmd_time)

        old = _make_state(72.0)
        new = _make_state(79.0)  # setback applied by automation

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        # handle_manual_override must NOT have been called
        coord.automation_engine.handle_manual_override.assert_not_called()
        assert coord._today_record.manual_overrides == 0

    def test_override_recorded_after_30s(self):
        """Setpoint change after 30 s of _set_temperature() DOES trigger manual override.

        After the race window closes, a genuine user change should still be detected.
        """
        now = datetime(2026, 6, 7, 12, 0, 0)
        cmd_time = now - timedelta(seconds=60)  # command was 60 s ago — window expired
        coord = _make_coord(temp_command_time=cmd_time)

        old = _make_state(79.0)
        new = _make_state(74.0)  # user override: lowered setpoint manually

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        # handle_manual_override MUST have been called — this is a real user action
        coord.automation_engine.handle_manual_override.assert_called_once()
        assert coord._today_record.manual_overrides == 1

    def test_no_override_when_temp_unchanged(self):
        """No override when temperature attribute does not change."""
        coord = _make_coord(temp_command_time=None)
        old = _make_state(72.0)
        new = _make_state(72.0)  # same temperature — no override

        now = datetime(2026, 6, 7, 12, 0, 0)
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        coord.automation_engine.handle_manual_override.assert_not_called()
        assert coord._today_record.manual_overrides == 0


# ---------------------------------------------------------------------------
# Tests: _temp_command_time set by automation methods
# ---------------------------------------------------------------------------


class TestTempCommandTimeIsSet:
    def test_set_temperature_sets_command_time(self):
        """_set_temperature() sets _temp_command_time before calling the service."""
        AutomationEngine = _get_automation_class()
        ae = object.__new__(AutomationEngine)

        hass = MagicMock()
        hass.services.async_call = AsyncMock()

        ae.hass = hass
        ae.climate_entity = "climate.test"
        ae.dry_run = False
        ae.config = {"temp_unit": "fahrenheit"}
        ae._temp_command_pending = False
        ae._temp_command_time = None
        ae._write_seq = 0
        ae._pending_setpoint_single = None
        ae._record_action = MagicMock()

        # Bind the real method
        ae._set_temperature = types.MethodType(AutomationEngine._set_temperature, ae)

        cmd_now = datetime(2026, 6, 7, 12, 0, 0)
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = cmd_now
            asyncio.run(ae._set_temperature(72.0, reason="away setback"))

        assert ae._temp_command_time == cmd_now


# ---------------------------------------------------------------------------
# Tests: fan command pending / recent fan command suppresses setpoint override
# (Bug 1A — Issue #313)
# ---------------------------------------------------------------------------


def _make_state_cool(temp: float) -> MagicMock:
    """Like _make_state() but hvac_mode='cool' for setpoint override detection."""
    s = MagicMock()
    s.state = "cool"
    s.attributes = {
        "hvac_action": "cooling",
        "temperature": temp,
        "fan_mode": "auto",
    }
    return s


class TestFanCommandSetpointGuard:
    """Fan command guards must suppress setpoint override detection.

    Before Bug 1A fix: _fan_command_pending and _is_recent_fan_command were
    NOT checked in the setpoint-override block, so a CA-issued fan command
    could race with the thermostat event and be recorded as a manual override.
    """

    def _make_coord_fan(
        self,
        *,
        fan_command_pending: bool,
        fan_command_time,
    ):
        """Coordinator stub configured for fan-guard setpoint tests."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = _make_coord(temp_command_time=None)

        ae = coord.automation_engine
        ae._fan_command_pending = fan_command_pending
        ae._fan_command_time = fan_command_time
        # Ensure HVAC command guards are inactive so only fan guards matter
        ae._hvac_command_pending = False
        ae._hvac_command_time = None
        ae._temp_command_pending = False
        ae._temp_command_time = None
        # Set last commanded hvac to cool >2 min ago so _is_expected_confirmation
        # does not suppress the override path
        ae._last_commanded_hvac_mode = "cool"
        ae._last_commanded_hvac_time = datetime(2026, 6, 7, 11, 55, 0)

        # Bind real _is_recent_fan_command so it reads ae._fan_command_time
        coord._is_recent_fan_command = types.MethodType(ClimateAdvisorCoordinator._is_recent_fan_command, coord)
        # _is_recent_hvac_command is already mocked to return False in _make_coord()

        # Update classification to hvac_mode='cool' so mode-match logic fires
        coord._current_classification.hvac_mode = "cool"

        return coord

    def test_fan_command_pending_suppresses_override(self):
        """_fan_command_pending=True must suppress setpoint override detection.

        FAILS before Bug 1A fix: the pending flag is not checked, so the
        override is recorded even while the fan command is still in flight.
        """
        now = datetime(2026, 6, 7, 12, 0, 0)
        coord = self._make_coord_fan(
            fan_command_pending=True,
            fan_command_time=now,
        )

        old = _make_state_cool(74.0)
        new = _make_state_cool(77.0)

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        coord.automation_engine.handle_manual_override.assert_not_called()
        assert coord._today_record.manual_overrides == 0, (
            "_fan_command_pending should suppress setpoint override but manual_overrides > 0"
        )

    def test_recent_fan_command_suppresses_override(self):
        """_is_recent_fan_command(30s) must suppress setpoint override detection.

        FAILS before Bug 1A fix: the recency check is not in the setpoint block,
        so a setpoint change arriving 15 s after a CA fan command is wrongly
        recorded as a user manual override.
        """
        now = datetime(2026, 6, 7, 12, 0, 0)
        fan_cmd_time = now - timedelta(seconds=15)  # 15 s ago — within 30 s window
        coord = self._make_coord_fan(
            fan_command_pending=False,
            fan_command_time=fan_cmd_time,
        )

        old = _make_state_cool(74.0)
        new = _make_state_cool(77.0)

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        coord.automation_engine.handle_manual_override.assert_not_called()
        assert coord._today_record.manual_overrides == 0, (
            "_is_recent_fan_command(30s) should suppress override but manual_overrides > 0"
        )

    def test_expired_fan_command_allows_genuine_override(self):
        """Fan command older than 30 s must NOT suppress a genuine user override.

        This is a regression guard: after the race window closes, real user
        setpoint changes must still be detected.  Must PASS before AND after fix.
        """
        now = datetime(2026, 6, 7, 12, 0, 0)
        fan_cmd_time = now - timedelta(seconds=60)  # 60 s ago — window expired
        coord = self._make_coord_fan(
            fan_command_pending=False,
            fan_command_time=fan_cmd_time,
        )

        old = _make_state_cool(74.0)
        new = _make_state_cool(77.0)

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        coord.automation_engine.handle_manual_override.assert_called_once()
        assert coord._today_record.manual_overrides == 1, "Expired fan command should NOT suppress a genuine override"
