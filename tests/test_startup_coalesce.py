"""Tests for startup coalescing behavior (Bug 1, Issue #321).

The startup coalescing window suppresses override detection for 5 minutes after
restart, then evaluates nat-vent and HVAC conditions once to apply the correct
operating mode. These are TDD tests — written before C1's implementation ships.

Occupant framing: after every HA restart, the user previously saw a spurious
"manual override" grace period triggered by the thermostat echoing its last mode
back to CA. That 30-minute grace period blocked automation entirely. The coalescing
window delays override detection until sensors and temps have stabilised.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 6, 12, 14, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _get_coordinator_class():
    """Return current ClimateAdvisorCoordinator class — avoids stale __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__ validation."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 58,
        "tomorrow_high": 79,
        "tomorrow_low": 59,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_today_record(**overrides) -> DailyRecord:
    kwargs = dict(date="2026-06-12", day_type="warm", trend_direction="stable")
    kwargs.update(overrides)
    return DailyRecord(**kwargs)


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = attributes or {}
    return s


def _make_event(data: dict) -> MagicMock:
    event = MagicMock()
    event.data = data
    return event


def _make_thermostat_coord_stub(**overrides):
    """Build a minimal coordinator stub for _async_thermostat_changed tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "natural_vent_delta": 3.0,
    }

    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._temp_command_pending = False
    ae._temp_command_time = None
    ae._last_commanded_hvac_mode = None
    ae._last_commanded_hvac_time = None
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.clear_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae.handle_door_window_open = AsyncMock()
    ae.apply_classification = AsyncMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = _make_today_record()
    coord._async_save_state = AsyncMock()
    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._is_recent_temp_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._flush_hvac_runtime = MagicMock()
    coord._start_hvac_observation = AsyncMock()
    coord._end_hvac_active_phase = AsyncMock()
    coord._abandon_observation = AsyncMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)
    coord._last_outdoor_temp = 65.0
    coord._last_predicted_indoor = None
    coord._resolved_sensors = []
    coord._is_sensor_open = MagicMock(return_value=False)

    # Startup coalescing state — set defaults, overrides applied below
    coord._startup_coalesce_active = False
    coord._startup_timer_fired = False
    coord._startup_coalesce_expiry = None

    for k, v in overrides.items():
        setattr(coord, k, v)

    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)
    return coord


def _make_coalesce_coord_stub(**overrides):
    """Build a minimal coordinator stub for _do_startup_coalesce tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "weather_entity": "weather.forecast_home",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "natural_vent_delta": 3.0,
    }

    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae.handle_door_window_open = AsyncMock()
    ae.apply_classification = AsyncMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._startup_coalesce_active = True
    coord._startup_timer_fired = True
    coord._startup_coalesce_expiry = "2026-06-12T14:05:00"
    coord._resolved_sensors = []
    coord._is_sensor_open = MagicMock(return_value=False)
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._last_outdoor_temp = 65.0
    coord._last_predicted_indoor = None
    coord._emit_event = MagicMock()
    coord._async_save_state = AsyncMock()

    climate_state = _make_state("cool")
    hass.states.get = MagicMock(return_value=climate_state)

    for k, v in overrides.items():
        setattr(coord, k, v)

    coord._do_startup_coalesce = types.MethodType(ClimateAdvisorCoordinator._do_startup_coalesce, coord)
    return coord


# ---------------------------------------------------------------------------
# TestStartupCoalesceActive: override detection suppressed during window
# ---------------------------------------------------------------------------


class TestStartupCoalesceActive:
    """Startup coalescing window suppresses override detection."""

    def test_no_override_during_coalesce_window(self):
        """During coalescing, a mode-mismatch thermostat event must NOT set _manual_override_active.

        Occupant impact: without this fix, HA restart triggered a spurious 30-min
        grace period blocking all automation while the user slept or was away.
        """
        coord = _make_thermostat_coord_stub(_startup_coalesce_active=True)
        # classification wants cool; thermostat echoes heat (mismatch — would normally detect override)
        coord._current_classification = _make_classification(hvac_mode="cool")

        old_state = _make_state("cool", {"hvac_action": ""})
        new_state = _make_state("heat", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        # Override detection suppressed
        coord.automation_engine.handle_manual_override.assert_not_called()
        coord.automation_engine.clear_manual_override.assert_not_called()

    def test_override_detection_works_after_coalesce(self):
        """After coalescing window closes, normal override detection runs.

        Occupant impact: genuine user thermostat changes after startup must still
        be recognised and start the grace period.
        """
        coord = _make_thermostat_coord_stub(_startup_coalesce_active=False)
        coord._current_classification = _make_classification(hvac_mode="cool")

        old_state = _make_state("cool", {"hvac_action": ""})
        new_state = _make_state("heat", {"hvac_action": ""})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        # Normal path: override detection fires
        coord.automation_engine.handle_manual_override.assert_called_once()


# ---------------------------------------------------------------------------
# TestStartupCoalesceCompute: _compute_next_automation_action during window
# ---------------------------------------------------------------------------


class TestStartupCoalesceCompute:
    """_compute_next_automation_action surfaces coalescing state."""

    def test_next_automation_shows_coalescing_during_window(self):
        """During startup coalescing, next automation returns 'Startup coalescing' label."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = MagicMock()
        coord.config = {
            "climate_entity": _THERMOSTAT_ID,
            "comfort_heat": 70,
            "comfort_cool": 75,
        }
        coord._startup_coalesce_active = True
        coord._startup_coalesce_expiry = "2026-06-12T14:05:00"
        # Minimal ae stub
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_active = False
        ae._fan_override_active = False
        coord.automation_engine = ae

        coord._compute_next_automation_action = types.MethodType(
            ClimateAdvisorCoordinator._compute_next_automation_action, coord
        )

        result = coord._compute_next_automation_action(None)
        action, expiry = result
        assert action == "Startup coalescing"
        assert expiry == "2026-06-12T14:05:00"

    def test_next_automation_normal_after_coalesce(self):
        """After coalescing ends, _compute_next_automation_action returns normal result."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = MagicMock()
        coord.config = {
            "climate_entity": _THERMOSTAT_ID,
            "comfort_heat": 70,
            "comfort_cool": 75,
        }
        coord._startup_coalesce_active = False
        coord._startup_coalesce_expiry = None

        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_active = False
        ae._fan_override_active = False
        ae.is_paused_by_door = False
        ae._grace_active = False
        ae._is_within_planned_window_period = MagicMock(return_value=False)
        coord.automation_engine = ae
        coord._door_open_timers = {}
        coord._door_open_timer_expiry = {}
        coord._resolved_sensors = []
        coord._is_sensor_open = MagicMock(return_value=False)

        coord._compute_next_automation_action = types.MethodType(
            ClimateAdvisorCoordinator._compute_next_automation_action, coord
        )

        result = coord._compute_next_automation_action(None)
        action, _ = result
        # Should NOT be the coalescing label
        assert action != "Startup coalescing"


# ---------------------------------------------------------------------------
# TestStartupCoalesceDoCoalesce: _do_startup_coalesce() logic
# ---------------------------------------------------------------------------


class TestStartupCoalesceDoCoalesce:
    """_do_startup_coalesce() proactively evaluates and applies the correct mode."""

    def test_nat_vent_activated_when_conditions_met(self):
        """With sensors open and outdoor < indoor, nat-vent is activated.

        Occupant impact: windows left open after restart now correctly resume
        nat-vent instead of idling with HVAC in an unknown state.
        """
        coord = _make_coalesce_coord_stub(
            _resolved_sensors=["binary_sensor.front_window"],
            _get_indoor_temp=MagicMock(return_value=73.0),
            _last_outdoor_temp=65.0,  # outdoor < indoor, conditions met
        )
        coord._is_sensor_open = MagicMock(return_value=True)  # sensor open

        asyncio.run(coord._do_startup_coalesce())

        coord.automation_engine.handle_door_window_open.assert_called_once_with("binary_sensor.front_window")
        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "startup_coalesced"
        assert event_data["nat_vent_activated"] is True
        assert event_data["hvac_commanded"] is False
        assert coord._startup_coalesce_active is False

    def test_classification_applied_when_no_sensors_open(self):
        """With no sensors open, apply_classification is called.

        Occupant impact: after restart with no windows open, the correct
        HVAC mode is restored immediately rather than waiting 30 minutes.
        """
        coord = _make_coalesce_coord_stub()
        # No sensors open (default _resolved_sensors=[])

        asyncio.run(coord._do_startup_coalesce())

        coord.automation_engine.apply_classification.assert_called_once()
        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "startup_coalesced"
        assert event_data["hvac_commanded"] is True
        assert event_data["nat_vent_activated"] is False
        assert coord._startup_coalesce_active is False

    def test_nat_vent_conditions_not_met_applies_classification(self):
        """Outdoor warmer than indoor → nat-vent gate fails → apply_classification called."""
        coord = _make_coalesce_coord_stub(
            _resolved_sensors=["binary_sensor.front_window"],
            _get_indoor_temp=MagicMock(return_value=72.0),
            _last_outdoor_temp=80.0,  # outdoor > indoor — nat-vent gate fails
        )
        coord._is_sensor_open = MagicMock(return_value=True)

        asyncio.run(coord._do_startup_coalesce())

        coord.automation_engine.handle_door_window_open.assert_not_called()
        coord.automation_engine.apply_classification.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "startup_coalesced"
        assert event_data["nat_vent_activated"] is False
        assert event_data["hvac_commanded"] is True

    def test_startup_coalesced_event_emitted(self):
        """startup_coalesced event always emitted with required fields."""
        coord = _make_coalesce_coord_stub()

        asyncio.run(coord._do_startup_coalesce())

        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "startup_coalesced"
        assert "nat_vent_activated" in event_data
        assert "hvac_commanded" in event_data
        assert "sensors_open_count" in event_data

    def test_startup_coalesce_active_cleared_after(self):
        """_startup_coalesce_active is False after _do_startup_coalesce runs."""
        coord = _make_coalesce_coord_stub()
        assert coord._startup_coalesce_active is True

        asyncio.run(coord._do_startup_coalesce())

        assert coord._startup_coalesce_active is False

    def test_sensors_open_count_in_event(self):
        """sensors_open_count reflects the number of open sensors at coalesce time."""
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        coord = _make_coalesce_coord_stub(
            _resolved_sensors=sensors,
            _get_indoor_temp=MagicMock(return_value=72.0),
            _last_outdoor_temp=80.0,  # no nat-vent so we can check count independently
        )
        coord._is_sensor_open = MagicMock(return_value=True)

        asyncio.run(coord._do_startup_coalesce())

        _, event_data = coord._emit_event.call_args[0]
        assert event_data["sensors_open_count"] == 2
