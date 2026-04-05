"""Tests for Issue #84 — Daily Record data accuracy fixes.

Covers two bugs fixed in coordinator.py:

Bug A: windows_opened was only set when a contact sensor opened WITHIN the exact
recommended time window.  The time-range guard was removed — now any contact
sensor open on a windows-recommended day earns credit.

Bug B: manual_overrides was incremented on every thermostat temperature change,
including integration-initiated ones.  Now gated by
``not automation_engine._temp_command_pending``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Patch dt_util.now to return a fixed datetime so isoformat() calls are stable
from datetime import datetime

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 5, 10, 0, 0)

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class.

    test_occupancy.py deletes custom_components.climate_advisor.coordinator from
    sys.modules and re-imports it.  If we hold a module-level reference, our
    bound methods have stale __globals__ so patches on the new module don't apply.
    Always importing fresh via importlib ensures method __globals__ == patched module.
    """
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


# ---------------------------------------------------------------------------
# Helpers shared by both test classes
# ---------------------------------------------------------------------------

_SENSOR_ID = "binary_sensor.front_window"
_THERMOSTAT_ID = "climate.thermostat"

_PATCH_CALL_LATER = "custom_components.climate_advisor.coordinator.async_call_later"
_PATCH_CALLBACK = "custom_components.climate_advisor.coordinator.callback"


def _consume_coroutine(coro):
    """Close a coroutine to prevent 'never awaited' RuntimeWarning."""
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
        "hvac_mode": "off",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": True,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_today_record(**overrides) -> DailyRecord:
    """Build a DailyRecord with sensible defaults."""
    kwargs = dict(date="2026-04-05", day_type="warm", trend_direction="stable")
    kwargs.update(overrides)
    return DailyRecord(**kwargs)


def _make_coordinator_stub(*, sensor_open: bool = True, classification=None):
    """Build a minimal coordinator-like object for testing _async_door_window_changed.

    Uses ``object.__new__`` (same pattern as test_startup_override.py) so we
    avoid running __init__ against mocked HA machinery.
    """
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {"sensor_debounce_seconds": 0}  # immediate debounce for tests

    # Automation engine — MagicMock (NOT AsyncMock) per project convention
    ae = MagicMock()
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    ae.handle_door_window_open = AsyncMock()
    ae.handle_all_doors_windows_closed = AsyncMock()
    ae._temp_command_pending = False
    coord.automation_engine = ae

    coord._current_classification = classification
    windows_rec = bool(classification and classification.windows_recommended)
    coord._today_record = _make_today_record(windows_recommended=windows_rec)
    coord._resolved_sensors = [_SENSOR_ID]
    coord._door_open_timers = {}
    coord._async_save_state = AsyncMock()

    # _is_sensor_open reads hass.states — configure based on sensor_open param
    def _is_sensor_open(entity_id: str) -> bool:
        state = coord.hass.states.get(entity_id)
        if not state:
            return False
        return state.state == "on"

    coord._is_sensor_open = _is_sensor_open

    # Set sensor state
    mock_state = MagicMock()
    mock_state.state = "on" if sensor_open else "off"
    coord.hass.states.get = MagicMock(return_value=mock_state)

    # Bind the real method under test (use same class instance for correct __globals__)
    coord._async_door_window_changed = types.MethodType(ClimateAdvisorCoordinator._async_door_window_changed, coord)

    return coord


def _make_thermostat_coordinator_stub(*, temp_command_pending: bool = False):
    """Build a minimal coordinator-like object for testing _async_thermostat_changed."""
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
    }

    # Automation engine — MagicMock (NOT AsyncMock)
    ae = MagicMock()
    ae.is_paused_by_door = False
    ae._hvac_command_pending = False
    ae._manual_override_active = False
    ae._fan_command_pending = False
    ae._fan_override_active = False
    ae._temp_command_pending = temp_command_pending
    ae.handle_manual_override_during_pause = AsyncMock()
    ae.handle_manual_override = MagicMock()
    ae.handle_fan_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = _make_today_record()
    coord._async_save_state = AsyncMock()

    # Helpers used inside _async_thermostat_changed
    coord._is_recent_hvac_command = MagicMock(return_value=False)
    coord._emit_event = MagicMock()
    coord._hvac_on_since = None
    coord._hvac_session_start_indoor_temp = None
    coord._hvac_session_start_outdoor_temp = None
    coord._hvac_session_mode = None
    coord._flush_hvac_runtime = MagicMock()
    coord._record_thermal_observation = MagicMock()
    coord._get_indoor_temp = MagicMock(return_value=72.0)
    coord._get_outdoor_temp = MagicMock(return_value=65.0)

    # Bind the real method under test (use same class instance for correct __globals__)
    coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

    return coord


def _make_event(data: dict) -> MagicMock:
    """Create a mock HA Event with the given data dict."""
    event = MagicMock()
    event.data = data
    return event


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    s = MagicMock()
    s.state = state_value
    s.attributes = attributes or {}
    return s


def _run_door_window_open(coord, entity_id: str = _SENSOR_ID) -> None:
    """Trigger _async_door_window_changed for an open sensor and run the debounce.

    The coordinator uses async_call_later + a @callback-decorated closure.
    We patch both so the callback fires immediately, then run the resulting
    _do_debounce() coroutine synchronously.
    """
    event = _make_event(
        {
            "entity_id": entity_id,
            "new_state": _make_state("on"),
        }
    )

    captured_task: list = []

    def _capture_task(coro):
        """Capture and store the coroutine instead of discarding it."""
        captured_task.append(coro)

    coord.hass.async_create_task = MagicMock(side_effect=_capture_task)

    with (
        patch(_PATCH_CALL_LATER) as mock_call_later,
        patch(_PATCH_CALLBACK, side_effect=lambda fn: fn),
    ):
        mock_call_later.return_value = MagicMock()
        asyncio.run(coord._async_door_window_changed(event))
        # _debounce_expired callback was registered; call it now
        assert mock_call_later.call_count == 1, "async_call_later should be called once"
        debounce_callback = mock_call_later.call_args[0][2]
        debounce_callback(None)  # fires _do_debounce() via async_create_task

    # Run the captured _do_debounce coroutine
    assert len(captured_task) == 1, "_do_debounce() should have been scheduled once"
    asyncio.run(captured_task[0])


# ---------------------------------------------------------------------------
# Tests: windows_opened tracking (Bug A)
# ---------------------------------------------------------------------------


class TestWindowsOpenedTracking:
    """Bug A: windows_opened should be set whenever a sensor opens on a
    windows-recommended day, regardless of time-of-day.
    """

    def test_windows_opened_set_when_sensor_opens_during_recommended_day(self):
        """Sensor opens when windows_recommended=True — windows_opened becomes True."""
        classification = _make_classification(windows_recommended=True)
        coord = _make_coordinator_stub(sensor_open=True, classification=classification)
        coord._today_record.windows_opened = False

        _run_door_window_open(coord)

        assert coord._today_record.windows_opened is True
        assert coord._today_record.window_open_actual_time is not None

    def test_windows_opened_not_set_when_not_recommended(self):
        """Sensor opens but windows_recommended=False — windows_opened stays False."""
        classification = _make_classification(windows_recommended=False)
        coord = _make_coordinator_stub(sensor_open=True, classification=classification)
        coord._today_record.windows_recommended = False
        coord._today_record.windows_opened = False

        _run_door_window_open(coord)

        assert coord._today_record.windows_opened is False

    def test_windows_opened_only_set_once(self):
        """Two sensor opens on same day — windows_opened set once, first time wins."""
        classification = _make_classification(windows_recommended=True)
        coord = _make_coordinator_stub(sensor_open=True, classification=classification)
        coord._today_record.windows_opened = False

        _run_door_window_open(coord)

        first_time = coord._today_record.window_open_actual_time
        assert coord._today_record.windows_opened is True
        assert first_time is not None

        # Second open should not overwrite window_open_actual_time
        _run_door_window_open(coord)

        assert coord._today_record.windows_opened is True
        assert coord._today_record.window_open_actual_time == first_time

    def test_windows_opened_no_record(self):
        """Sensor opens but _today_record is None — no AttributeError raised."""
        classification = _make_classification(windows_recommended=True)
        coord = _make_coordinator_stub(sensor_open=True, classification=classification)
        coord._today_record = None  # simulate record not yet initialised

        # Should complete without raising
        _run_door_window_open(coord)

    def test_windows_opened_no_classification(self):
        """Sensor opens but _current_classification is None — no AttributeError raised."""
        coord = _make_coordinator_stub(sensor_open=True, classification=None)
        coord._current_classification = None
        coord._today_record.windows_opened = False

        # Should complete without raising and NOT set windows_opened
        _run_door_window_open(coord)

        assert coord._today_record.windows_opened is False


# ---------------------------------------------------------------------------
# Tests: manual_overrides tracking (Bug B)
# ---------------------------------------------------------------------------


class TestManualOverridesTracking:
    """Bug B: manual_overrides should only increment when the user (not the
    integration) changes the thermostat setpoint temperature.
    """

    def test_manual_override_counted_when_user_changes_temp(self):
        """Temperature attribute changes with _temp_command_pending=False
        → manual_overrides incremented by 1.
        """
        coord = _make_thermostat_coordinator_stub(temp_command_pending=False)
        coord._today_record.manual_overrides = 0

        old_state = _make_state("cool", {"temperature": 72.0})
        new_state = _make_state("cool", {"temperature": 74.0})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == 1

    def test_manual_override_not_counted_when_automation_changes_temp(self):
        """Same temperature change but _temp_command_pending=True
        → manual_overrides NOT incremented.
        """
        coord = _make_thermostat_coordinator_stub(temp_command_pending=True)
        coord._today_record.manual_overrides = 0

        old_state = _make_state("cool", {"temperature": 72.0})
        new_state = _make_state("cool", {"temperature": 74.0})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.manual_overrides == 0

    def test_manual_override_no_record(self):
        """Temperature changes but _today_record is None — no error raised."""
        coord = _make_thermostat_coordinator_stub(temp_command_pending=False)
        coord._today_record = None

        old_state = _make_state("cool", {"temperature": 72.0})
        new_state = _make_state("cool", {"temperature": 74.0})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        # Should complete without raising
        asyncio.run(coord._async_thermostat_changed(event))

    def test_override_details_populated_on_manual_change(self):
        """override_details list gets one entry on a manual temperature change."""
        coord = _make_thermostat_coordinator_stub(temp_command_pending=False)
        coord._today_record.manual_overrides = 0
        coord._today_record.override_details = []

        old_state = _make_state("cool", {"temperature": 72.0})
        new_state = _make_state("cool", {"temperature": 75.0})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        assert len(coord._today_record.override_details) == 1
        detail = coord._today_record.override_details[0]
        assert detail["old_temp"] == 72.0
        assert detail["new_temp"] == 75.0
        assert detail["direction"] == "up"
        assert detail["magnitude"] == 3.0

    def test_override_details_not_populated_on_automation_change(self):
        """override_details stays empty when _temp_command_pending=True."""
        coord = _make_thermostat_coordinator_stub(temp_command_pending=True)
        coord._today_record.manual_overrides = 0
        coord._today_record.override_details = []

        old_state = _make_state("cool", {"temperature": 72.0})
        new_state = _make_state("cool", {"temperature": 75.0})
        event = _make_event({"new_state": new_state, "old_state": old_state})

        asyncio.run(coord._async_thermostat_changed(event))

        assert coord._today_record.override_details == []
