"""Tests for temperature sensor entities and coordinator.data population (Issue #94).

TDD: these tests are written BEFORE implementation. They verify:
- coordinator.data includes indoor_temp, outdoor_temp, forecast_high/low after _async_update_data
- get_chart_data() sources actual_indoor/actual_outdoor from chart_log for multi-day ranges
- New sensor entities have correct device_class, state_class, native_unit_of_measurement
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 4, 9, 14, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    coro.close()


def _make_state(state_value: str, hvac_action: str = "") -> MagicMock:
    s = MagicMock()
    s.state = state_value
    s.attributes = {"hvac_action": hvac_action, "temperature": 70.0, "fan_mode": "auto"}
    return s


def _make_forecast_snapshot(
    *,
    indoor: float = 72.0,
    outdoor: float = 55.0,
    today_high: float = 78.0,
    today_low: float = 58.0,
    tomorrow_high: float = 79.0,
    tomorrow_low: float = 57.0,
):
    """Return a real ForecastSnapshot so classify_day() can run without crashing."""
    from custom_components.climate_advisor.classifier import ForecastSnapshot

    return ForecastSnapshot(
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=tomorrow_high,
        tomorrow_low=tomorrow_low,
        current_outdoor_temp=outdoor,
        current_indoor_temp=indoor,
    )


def _make_classification_stub(
    *,
    today_high: float = 78.0,
    today_low: float = 58.0,
    tomorrow_high: float = 79.0,
    tomorrow_low: float = 57.0,
):
    """Build a DayClassification stub with forecast temp attributes."""
    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "warm",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": today_high,
            "today_low": today_low,
            "tomorrow_high": tomorrow_high,
            "tomorrow_low": tomorrow_low,
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
    return c


def _make_update_data_coord(
    *,
    hvac_mode: str = "off",
    hvac_action: str = "",
    indoor_temp: float = 72.0,
    outdoor_temp: float = 55.0,
    today_high: float = 78.0,
    today_low: float = 58.0,
    tomorrow_high: float = 79.0,
    tomorrow_low: float = 57.0,
    forecast_returns_none: bool = False,
):
    """Coordinator stub with real _async_update_data bound.

    Optionally returns a mock forecast object so coordinator.data can populate
    outdoor_temp (which comes from forecast.current_outdoor_temp).
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
    climate_state = _make_state(hvac_mode, hvac_action)
    hass.states.get = MagicMock(return_value=climate_state)
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
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

    coord._today_record = DailyRecord(date="2026-04-09", day_type="warm", trend_direction="stable")
    coord._hvac_on_since = None
    coord._last_state_contradiction_time = None

    ae = MagicMock()
    ae._fan_active = False
    ae._last_action_time = None
    ae._last_action_reason = ""
    ae._fan_override_time = None
    ae._get_fan_runtime_minutes = MagicMock(return_value=0.0)
    # apply_classification and check_* are awaited inside _async_update_data
    ae.apply_classification = AsyncMock()
    ae.check_window_cooling_opportunity = AsyncMock()
    ae.check_natural_vent_conditions = AsyncMock()
    ae.update_outdoor_temp = MagicMock()
    coord.automation_engine = ae

    coord._emit_event = MagicMock()
    coord._async_save_state = AsyncMock()
    coord.claude_client = None

    # Sensor resolution
    coord._resolved_sensors = []
    coord._resolve_monitored_sensors = MagicMock(return_value=[])
    coord._unsubscribe_door_window_listeners = MagicMock()
    coord._subscribe_door_window_listeners = MagicMock()

    # Forecast — use a real ForecastSnapshot so classify_day() runs cleanly.
    # When forecast_returns_none=True, skip the whole if-forecast block.
    if forecast_returns_none:
        coord._get_forecast = AsyncMock(return_value=None)
    else:
        coord._get_forecast = AsyncMock(
            return_value=_make_forecast_snapshot(
                indoor=indoor_temp,
                outdoor=outdoor_temp,
                today_high=today_high,
                today_low=today_low,
                tomorrow_high=tomorrow_high,
                tomorrow_low=tomorrow_low,
            )
        )

    coord._get_hourly_forecast_data = AsyncMock(return_value=MagicMock())
    coord._hourly_forecast_temps = MagicMock()
    coord._startup_retries_remaining = 0
    coord._startup_retry_delay = 30
    coord._briefing_sent_today = False
    coord._briefing_day_type = None
    coord._first_run = False
    coord._occupancy_mode = "home"
    coord._last_violation_check = None

    coord._update_forecast = MagicMock()
    coord._current_forecast = None
    coord._classify_day = MagicMock()
    coord._build_briefing_text = MagicMock(return_value=("", ""))
    coord._check_startup_override = MagicMock()

    coord._current_classification = _make_classification_stub(
        today_high=today_high,
        today_low=today_low,
        tomorrow_high=tomorrow_high,
        tomorrow_low=tomorrow_low,
    )

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
    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)

    coord._chart_log = MagicMock()
    coord._last_violation_check = None
    coord._check_comfort_violations = MagicMock()

    # Thermal event pipeline (v2)
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._update_pre_heat_buffer = MagicMock()
    coord._sample_thermal_event = MagicMock()
    coord._check_stabilization = AsyncMock()

    coord._async_update_data = types.MethodType(ClimateAdvisorCoordinator._async_update_data, coord)
    return coord


def _make_chart_data_coord(
    *,
    indoor_history: list | None = None,
    outdoor_history: list | None = None,
    chart_log_entries_by_range: dict | None = None,
):
    """Coordinator stub with real get_chart_data bound.

    chart_log_entries_by_range: maps range_str → list of entries returned by
    _chart_log.get_entries(range_str).
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    coord.config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "temp_unit": "fahrenheit",
    }

    coord._current_classification = _make_classification_stub()

    coord._hourly_forecast_temps = MagicMock()

    ae = MagicMock()
    ae._thermal_model = None
    coord.automation_engine = ae

    learning = MagicMock()
    learning.get_thermal_model = MagicMock(return_value={})
    coord.learning = learning

    coord._indoor_temp_history = indoor_history if indoor_history is not None else []
    coord._outdoor_temp_history = outdoor_history if outdoor_history is not None else []

    chart_log = MagicMock()
    entries_map = chart_log_entries_by_range or {}

    def _get_entries(range_str="24h"):
        return entries_map.get(range_str, [])

    chart_log.get_entries = MagicMock(side_effect=_get_entries)
    coord._chart_log = chart_log

    coord._thermal_factors = None
    coord._get_indoor_temp = MagicMock(return_value=None)
    coord._occupancy_mode = "home"

    coord.get_chart_data = types.MethodType(ClimateAdvisorCoordinator.get_chart_data, coord)
    return coord


# ---------------------------------------------------------------------------
# Group A: coordinator.data population (Issue #94, keys missing until Phase 2)
# ---------------------------------------------------------------------------


class TestCoordinatorDataTempKeys:
    """Verify coordinator.data includes temperature keys after _async_update_data.

    These tests FAIL until Phase 2 adds the keys to the result dict.
    """

    def test_coordinator_data_has_indoor_temp(self):
        coord = _make_update_data_coord(indoor_temp=72.0)
        result = asyncio.run(coord._async_update_data())
        assert "indoor_temp" in result, f"'indoor_temp' key missing from coordinator.data; keys={list(result)}"
        assert result["indoor_temp"] == 72.0

    def test_coordinator_data_has_outdoor_temp(self):
        coord = _make_update_data_coord(outdoor_temp=55.0)
        result = asyncio.run(coord._async_update_data())
        assert "outdoor_temp" in result, f"'outdoor_temp' key missing from coordinator.data; keys={list(result)}"
        assert result["outdoor_temp"] == 55.0

    def test_coordinator_data_has_forecast_high(self):
        coord = _make_update_data_coord(today_high=78.0)
        result = asyncio.run(coord._async_update_data())
        assert "forecast_high" in result, f"'forecast_high' key missing; keys={list(result)}"
        assert result["forecast_high"] == 78.0

    def test_coordinator_data_has_forecast_low(self):
        coord = _make_update_data_coord(today_low=58.0)
        result = asyncio.run(coord._async_update_data())
        assert "forecast_low" in result, f"'forecast_low' key missing; keys={list(result)}"
        assert result["forecast_low"] == 58.0

    def test_coordinator_data_has_forecast_high_tomorrow(self):
        coord = _make_update_data_coord(tomorrow_high=79.0)
        result = asyncio.run(coord._async_update_data())
        assert "forecast_high_tomorrow" in result, f"'forecast_high_tomorrow' key missing; keys={list(result)}"
        assert result["forecast_high_tomorrow"] == 79.0

    def test_coordinator_data_has_forecast_low_tomorrow(self):
        coord = _make_update_data_coord(tomorrow_low=57.0)
        result = asyncio.run(coord._async_update_data())
        assert "forecast_low_tomorrow" in result, f"'forecast_low_tomorrow' key missing; keys={list(result)}"
        assert result["forecast_low_tomorrow"] == 57.0

    def test_indoor_temp_key_present_when_none(self):
        """When _get_indoor_temp returns None, key must still appear (value=None is OK)."""
        coord = _make_update_data_coord(indoor_temp=72.0)
        coord._get_indoor_temp = MagicMock(return_value=None)
        result = asyncio.run(coord._async_update_data())
        assert "indoor_temp" in result, "'indoor_temp' key must be present even when temp is None; keys={list(result)}"


# ---------------------------------------------------------------------------
# Group B: get_chart_data() uses chart_log entries for multi-day ranges
# ---------------------------------------------------------------------------

_RAW_7D_ENTRIES = [
    {
        "ts": "2026-04-02T10:00:00+00:00",
        "hvac": "heat",
        "fan": False,
        "indoor": 72.0,
        "outdoor": 55.0,
        "windows_open": False,
        "windows_recommended": False,
    }
]

_DAILY_1Y_ENTRIES = [
    {
        "ts": "2026-04-01T00:00:00+00:00",
        "hvac": "heat",
        "fan_minutes": 120,
        "indoor_avg": 71.5,
        "outdoor_avg": 54.0,
        "indoor_min": 69.0,
        "indoor_max": 74.0,
        "outdoor_min": 50.0,
        "outdoor_max": 58.0,
        "windows_open": False,
        "windows_recommended": False,
    }
]


class TestGetChartDataUsesChartLog:
    """Verify get_chart_data() sources actual temps from chart_log for multi-day ranges.

    These tests FAIL until Phase 2 updates get_chart_data() to use chart_log entries
    instead of _indoor_temp_history/_outdoor_temp_history.
    """

    def test_get_chart_data_7d_uses_chartlog_indoor(self):
        """For 7d range, actual_indoor entries come from chart_log, not in-memory history."""
        coord = _make_chart_data_coord(
            indoor_history=[],
            outdoor_history=[],
            chart_log_entries_by_range={"7d": _RAW_7D_ENTRIES},
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 9, 14, 0, 0)
            result = coord.get_chart_data("7d")

        actual_indoor = result["actual_indoor"]
        assert len(actual_indoor) > 0, "actual_indoor should have entries from chart_log for 7d range"
        assert actual_indoor[0]["temp"] == 72.0, f"Expected temp=72.0, got {actual_indoor[0]}"

    def test_get_chart_data_7d_uses_chartlog_outdoor(self):
        """For 7d range, actual_outdoor entries come from chart_log."""
        coord = _make_chart_data_coord(
            indoor_history=[],
            outdoor_history=[],
            chart_log_entries_by_range={"7d": _RAW_7D_ENTRIES},
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 9, 14, 0, 0)
            result = coord.get_chart_data("7d")

        actual_outdoor = result["actual_outdoor"]
        assert len(actual_outdoor) > 0, "actual_outdoor should have entries from chart_log for 7d range"
        assert actual_outdoor[0]["temp"] == 55.0, f"Expected temp=55.0, got {actual_outdoor[0]}"

    def test_get_chart_data_1y_uses_indoor_avg(self):
        """For 1y range, daily-bucket entries use indoor_avg as the temp value."""
        coord = _make_chart_data_coord(
            indoor_history=[],
            outdoor_history=[],
            chart_log_entries_by_range={"1y": _DAILY_1Y_ENTRIES},
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 9, 14, 0, 0)
            result = coord.get_chart_data("1y")

        actual_indoor = result["actual_indoor"]
        assert len(actual_indoor) > 0, "actual_indoor should have entries from chart_log for 1y range"
        assert actual_indoor[0]["temp"] == 71.5, f"Expected temp=71.5 (indoor_avg), got {actual_indoor[0]}"

    def test_get_chart_data_excludes_none_indoor(self):
        """Entries with indoor=None should be excluded from actual_indoor."""
        entries_with_none = [
            {
                "ts": "2026-04-02T10:00:00+00:00",
                "hvac": "heat",
                "fan": False,
                "indoor": None,
                "outdoor": 55.0,
                "windows_open": False,
                "windows_recommended": False,
            },
            {
                "ts": "2026-04-02T11:00:00+00:00",
                "hvac": "heat",
                "fan": False,
                "indoor": 73.0,
                "outdoor": 56.0,
                "windows_open": False,
                "windows_recommended": False,
            },
        ]
        coord = _make_chart_data_coord(
            indoor_history=[],
            outdoor_history=[],
            chart_log_entries_by_range={"7d": entries_with_none},
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 9, 14, 0, 0)
            result = coord.get_chart_data("7d")

        actual_indoor = result["actual_indoor"]
        assert len(actual_indoor) == 1, f"Expected 1 entry (None excluded), got {len(actual_indoor)}: {actual_indoor}"
        assert actual_indoor[0]["temp"] == 73.0

    def test_get_chart_data_in_memory_fallback_for_24h(self):
        """For 24h range, in-memory history entries appear in actual_indoor/outdoor.

        This test documents the current (pre-Phase-2) behavior as a baseline.
        It may PASS or FAIL depending on whether the implementation preserves
        the in-memory path for short ranges.
        """
        in_memory_indoor = [("14:00", 71.0), ("14:30", 71.5)]
        in_memory_outdoor = [("14:00", 54.0), ("14:30", 54.5)]
        coord = _make_chart_data_coord(
            indoor_history=in_memory_indoor,
            outdoor_history=in_memory_outdoor,
            chart_log_entries_by_range={"24h": []},
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 9, 14, 0, 0)
            result = coord.get_chart_data("24h")

        # Either source is acceptable for 24h; just verify the arrays are present
        assert "actual_indoor" in result
        assert "actual_outdoor" in result


# ---------------------------------------------------------------------------
# Group C: sensor class attributes (import will FAIL until Phase 2)
# ---------------------------------------------------------------------------


def test_indoor_temp_sensor_has_measurement_state_class():
    from homeassistant.components.sensor import SensorStateClass

    from custom_components.climate_advisor.sensor import ClimateAdvisorIndoorTempSensor

    assert ClimateAdvisorIndoorTempSensor._attr_state_class == SensorStateClass.MEASUREMENT


def test_indoor_temp_sensor_has_temperature_device_class():
    from custom_components.climate_advisor.sensor import ClimateAdvisorIndoorTempSensor, SensorDeviceClass

    assert ClimateAdvisorIndoorTempSensor._attr_device_class == SensorDeviceClass.TEMPERATURE


def test_indoor_temp_sensor_has_fahrenheit_unit():
    from homeassistant.const import UnitOfTemperature

    from custom_components.climate_advisor.sensor import ClimateAdvisorIndoorTempSensor

    assert ClimateAdvisorIndoorTempSensor._attr_native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT


def test_outdoor_temp_sensor_has_measurement_state_class():
    from homeassistant.components.sensor import SensorStateClass

    from custom_components.climate_advisor.sensor import ClimateAdvisorOutdoorTempSensor

    assert ClimateAdvisorOutdoorTempSensor._attr_state_class == SensorStateClass.MEASUREMENT


def test_forecast_high_sensor_has_temperature_device_class():
    from custom_components.climate_advisor.sensor import ClimateAdvisorForecastHighSensor, SensorDeviceClass

    assert ClimateAdvisorForecastHighSensor._attr_device_class == SensorDeviceClass.TEMPERATURE


def test_forecast_low_sensor_has_measurement_state_class():
    from homeassistant.components.sensor import SensorStateClass

    from custom_components.climate_advisor.sensor import ClimateAdvisorForecastLowSensor

    assert ClimateAdvisorForecastLowSensor._attr_state_class == SensorStateClass.MEASUREMENT
