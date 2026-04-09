"""Tests for HVAC session detection and state contradiction event fixes.

These tests call the REAL coordinator methods (bound via types.MethodType to a
stubbed coordinator instance) to verify the actual code paths changed, not a
replicated copy.

Fix 1a: Running detection falls back to hvac_mode when hvac_action is stuck at
"fan" on both old and new states.

Fix 1b: Session mode resolves from new_state.state when hvac_action is ambiguous.

Fix 2: _async_update_data emits a state_contradiction_warning event when
hvac_mode="off" but hvac_action is an active action ("fan"/"heating"/"cooling").
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 8, 21, 0, 0)


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(state_value: str, hvac_action: str = "") -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = {"hvac_action": hvac_action, "temperature": 70.0, "fan_mode": "auto"}
    return s


def _make_thermostat_event(old_state: MagicMock, new_state: MagicMock) -> MagicMock:
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}
    return event


def _make_thermostat_coord(*, hvac_session_mode=None, hvac_on_since=None):
    """Coordinator stub with real _async_thermostat_changed bound."""
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
    ae._temp_command_pending = False
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "warm",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 78,
            "today_low": 58,
            "tomorrow_high": 79,
            "tomorrow_low": 59,
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

    coord._today_record = DailyRecord(date="2026-04-08", day_type="warm", trend_direction="stable")
    coord._async_save_state = AsyncMock()
    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._hvac_on_since = hvac_on_since
    coord._hvac_session_start_indoor_temp = None
    coord._hvac_session_start_outdoor_temp = None
    coord._hvac_session_mode = hvac_session_mode
    coord._flush_hvac_runtime = MagicMock()
    coord._record_thermal_observation = MagicMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._cancel_all_debounce_timers = MagicMock()
    coord._chart_log = MagicMock()

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    return coord


def _make_update_data_coord(*, hvac_mode: str, hvac_action: str, ca_fan_active: bool = False):
    """Coordinator stub with real _async_update_data bound.

    Stubs everything except the state contradiction check and _emit_event.
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    climate_state = _make_state(hvac_mode, hvac_action)
    hass.states.get = MagicMock(return_value=climate_state)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "learning_enabled": True,
        "briefing_time": "07:00",
        "ai_enabled": False,
        "ai_model": "claude-sonnet-4-6",
    }

    from custom_components.climate_advisor.learning import DailyRecord

    coord._today_record = DailyRecord(date="2026-04-08", day_type="warm", trend_direction="stable")
    coord._hvac_on_since = None
    coord._last_state_contradiction_time = None

    ae = MagicMock()
    ae._fan_active = ca_fan_active
    ae._last_action_time = None
    ae._last_action_reason = ""
    ae._fan_override_time = None
    ae._get_fan_runtime_minutes = MagicMock(return_value=0.0)
    coord.automation_engine = ae

    coord._emit_event = MagicMock()
    coord._async_save_state = AsyncMock()
    coord.claude_client = None

    # Sensor resolution — needed at the very top of _async_update_data
    coord._resolved_sensors = []
    coord._resolve_monitored_sensors = MagicMock(return_value=[])
    coord._unsubscribe_door_window_listeners = MagicMock()
    coord._subscribe_door_window_listeners = MagicMock()

    # Forecast — return None so the `if forecast:` block is skipped entirely,
    # which lets the method fall straight through to the data dict section.
    coord._get_forecast = AsyncMock(return_value=None)
    coord._get_hourly_forecast_data = AsyncMock(return_value=MagicMock())
    coord._hourly_forecast_temps = MagicMock()
    coord._startup_retries_remaining = 0
    coord._startup_retry_delay = 30
    coord._briefing_sent_today = False
    coord._briefing_day_type = None
    coord._first_run = False
    coord._occupancy_mode = "home"
    coord._last_violation_check = None

    # Unused stubs kept for completeness (no-ops due to forecast=None path)
    coord._update_forecast = MagicMock()
    coord._current_forecast = None
    coord._classify_day = MagicMock()

    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "warm",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 78,
            "today_low": 58,
            "tomorrow_high": 79,
            "tomorrow_low": 59,
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

    learning = MagicMock()
    learning.generate_suggestions = MagicMock(return_value=[])
    learning.get_compliance_summary = MagicMock(
        return_value={
            "comfort_score": 1.0,
            "window_compliance": None,
            "avg_daily_hvac_runtime_minutes": 0,
            "total_manual_overrides": 0,
            "pending_suggestions": 0,
        }
    )
    coord.learning = learning

    coord._indoor_temp_history = []
    coord._outdoor_temp_history = []
    coord._compute_next_automation_action = MagicMock(return_value=("No action", ""))
    coord._compute_next_action = MagicMock(return_value="No action")
    coord._compute_automation_status = MagicMock(return_value="active")
    coord._compute_fan_status = MagicMock(return_value="off")
    coord._compute_contact_status = MagicMock(return_value="closed")
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._last_briefing = ""
    coord._last_briefing_short = ""
    coord._get_indoor_temp = MagicMock(return_value=72.0)

    coord._chart_log = MagicMock()

    # Suppress violation tracking helper
    coord._last_violation_check = None
    coord._check_comfort_violations = MagicMock()

    coord._startup_retries_remaining = 0

    coord._async_update_data = types.MethodType(ClimateAdvisorCoordinator._async_update_data, coord)
    return coord


# ---------------------------------------------------------------------------
# Fix 1a+b: real _async_thermostat_changed session detection
# ---------------------------------------------------------------------------


class TestThermalSessionDetectionReal:
    """Call the REAL _async_thermostat_changed and verify session state."""

    def test_turn_on_detected_when_hvac_action_stuck_at_fan(self):
        """old=off/fan, new=heat/fan: session starts (mode-based fallback fires)."""
        coord = _make_thermostat_coord()
        old = _make_state("off", hvac_action="fan")
        new = _make_state("heat", hvac_action="fan")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        assert coord._hvac_on_since is not None, "Session should have started"
        assert coord._hvac_session_mode == "heat", f"Expected session mode 'heat', got {coord._hvac_session_mode!r}"

    def test_session_mode_heat_set_from_state_not_hvac_action(self):
        """When hvac_action='fan', session mode is resolved from new_state.state='heat'."""
        coord = _make_thermostat_coord()
        old = _make_state("off", hvac_action="")
        new = _make_state("heat", hvac_action="fan")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        assert coord._hvac_session_mode == "heat"

    def test_session_mode_cool_set_from_state_when_hvac_action_is_fan(self):
        """hvac_action='fan' on a cool-mode turn-on → session mode = 'cool'."""
        coord = _make_thermostat_coord()
        old = _make_state("off", hvac_action="")
        new = _make_state("cool", hvac_action="fan")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        assert coord._hvac_session_mode == "cool"

    def test_turn_off_detected_when_hvac_action_stuck_at_fan(self):
        """old=heat/fan, new=off/fan: turn-off fires and _record_thermal_observation is called."""
        coord = _make_thermostat_coord(
            hvac_session_mode="heat",
            hvac_on_since=datetime(2026, 4, 8, 9, 0, 0),
        )
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)
        coord.learning = MagicMock()
        old = _make_state("heat", hvac_action="fan")
        new = _make_state("off", hvac_action="fan")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        coord._flush_hvac_runtime.assert_called_once()
        coord._record_thermal_observation.assert_called_once()

    def test_normal_heating_action_still_works(self):
        """Standard old=off/'' → new=heat/'heating' path still sets mode correctly."""
        coord = _make_thermostat_coord()
        old = _make_state("off", hvac_action="")
        new = _make_state("heat", hvac_action="heating")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        assert coord._hvac_on_since is not None
        assert coord._hvac_session_mode == "heat"

    def test_session_mode_none_when_state_is_unrecognised(self):
        """fan_only mode + fan action → session mode stays None."""
        coord = _make_thermostat_coord()
        old = _make_state("off", hvac_action="")
        new = _make_state("fan_only", hvac_action="fan")

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 8, 10, 0, 0)
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

        assert coord._hvac_session_mode is None


# ---------------------------------------------------------------------------
# Fix 2: real _async_update_data state contradiction event
# ---------------------------------------------------------------------------


class TestStateContradictionEventReal:
    """Call the REAL _async_update_data and verify _emit_event is called."""

    def test_event_emitted_when_mode_off_action_fan(self):
        coord = _make_update_data_coord(hvac_mode="off", hvac_action="fan")
        asyncio.run(coord._async_update_data())
        calls = [c[0][0] for c in coord._emit_event.call_args_list]
        assert "state_contradiction_warning" in calls, f"Expected state_contradiction_warning event; got: {calls}"

    def test_event_emitted_when_mode_off_action_heating(self):
        coord = _make_update_data_coord(hvac_mode="off", hvac_action="heating")
        asyncio.run(coord._async_update_data())
        calls = [c[0][0] for c in coord._emit_event.call_args_list]
        assert "state_contradiction_warning" in calls

    def test_no_event_when_mode_matches_action(self):
        coord = _make_update_data_coord(hvac_mode="heat", hvac_action="heating")
        asyncio.run(coord._async_update_data())
        calls = [c[0][0] for c in coord._emit_event.call_args_list]
        assert "state_contradiction_warning" not in calls

    def test_no_event_when_ca_fan_is_active(self):
        """hvac_mode=off + hvac_action=fan, but CA itself activated the fan — suppress."""
        coord = _make_update_data_coord(hvac_mode="off", hvac_action="fan", ca_fan_active=True)
        asyncio.run(coord._async_update_data())
        calls = [c[0][0] for c in coord._emit_event.call_args_list]
        assert "state_contradiction_warning" not in calls

    def test_dedup_suppresses_second_call_within_30_min(self):
        coord = _make_update_data_coord(hvac_mode="off", hvac_action="fan")
        fixed_now = datetime(2026, 4, 8, 21, 0, 0)

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = fixed_now

            # First call: emits event, sets _last_state_contradiction_time = fixed_now
            asyncio.run(coord._async_update_data())
            first_count = sum(1 for c in coord._emit_event.call_args_list if c[0][0] == "state_contradiction_warning")
            assert first_count == 1

            # Second call at the same time (0 min elapsed) — dedup should suppress
            asyncio.run(coord._async_update_data())
            second_count = sum(1 for c in coord._emit_event.call_args_list if c[0][0] == "state_contradiction_warning")
            assert second_count == 1, "Dedup should suppress the second emission within 30 min"
