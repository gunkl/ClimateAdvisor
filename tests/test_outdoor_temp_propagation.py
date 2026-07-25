"""Coordinator-level tests for outdoor-temp propagation (Issue #511).

Exercises the real ClimateAdvisorCoordinator._apply_outdoor_temp(),
_refresh_weather_service_outdoor_temp(), _get_outdoor_temp(), and
_async_end_of_day() via the established object.__new__() + types.MethodType()
partial-instantiation pattern (see test_contact_status.py, test_daily_record_accuracy.py)
rather than replicating their logic — per project doctrine, mirroring the logic
under test can't catch a bug in that logic.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (
    ATTR_OUTDOOR_TEMP,
    TEMP_SOURCE_INPUT_NUMBER,
    TEMP_SOURCE_SENSOR,
    TEMP_SOURCE_WEATHER_SERVICE,
)


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class (see test_daily_record_accuracy.py
    for why this must be a fresh import rather than a module-level reference)."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_coordinator(outdoor_temp_source: str = TEMP_SOURCE_WEATHER_SERVICE, **config_overrides) -> object:
    """Build a bare coordinator bound to the real outdoor-temp-propagation methods."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {
        "outdoor_temp_source": outdoor_temp_source,
        "outdoor_temp_entity": "sensor.outdoor_temp",
        "weather_entity": "weather.forecast_home",
        "temp_unit": "fahrenheit",
        **config_overrides,
    }
    coord.hass = MagicMock()
    coord.automation_engine = MagicMock()
    coord._last_outdoor_temp = None
    coord._current_classification = None
    coord._outdoor_temp_history = []
    coord.data = None
    coord.async_update_listeners = MagicMock()
    coord._apply_outdoor_windows_gate = types.MethodType(ClimateAdvisorCoordinator._apply_outdoor_windows_gate, coord)
    coord._apply_outdoor_temp = types.MethodType(ClimateAdvisorCoordinator._apply_outdoor_temp, coord)
    coord._get_outdoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_outdoor_temp, coord)
    coord._refresh_weather_service_outdoor_temp = types.MethodType(
        ClimateAdvisorCoordinator._refresh_weather_service_outdoor_temp, coord
    )
    coord._async_thermal_sample_tick = types.MethodType(ClimateAdvisorCoordinator._async_thermal_sample_tick, coord)
    coord._sample_all_observations = MagicMock()
    return coord


def _fc_entry(iso_dt: str, temp: float) -> dict:
    return {"datetime": iso_dt, "temperature": temp}


class TestApplyOutdoorTemp:
    def test_sets_last_outdoor_temp_and_mirrors_to_automation_engine(self):
        coord = _make_coordinator()
        coord._apply_outdoor_temp(72.0, record_history=False)
        assert coord._last_outdoor_temp == 72.0
        coord.automation_engine.update_outdoor_temp.assert_called_once_with(72.0)

    def test_none_value_is_a_no_op(self):
        coord = _make_coordinator()
        coord._last_outdoor_temp = 70.0
        coord._apply_outdoor_temp(None, record_history=True)
        assert coord._last_outdoor_temp == 70.0
        coord.automation_engine.update_outdoor_temp.assert_not_called()
        assert coord._outdoor_temp_history == []

    def test_record_history_true_appends_history(self):
        coord = _make_coordinator()
        coord._apply_outdoor_temp(68.0, record_history=True)
        assert len(coord._outdoor_temp_history) == 1
        assert coord._outdoor_temp_history[0][1] == 68.0

    def test_record_history_false_does_not_append(self):
        coord = _make_coordinator()
        coord._apply_outdoor_temp(68.0, record_history=False)
        assert coord._outdoor_temp_history == []

    def test_patches_coordinator_data_and_notifies_listeners_when_data_exists(self):
        coord = _make_coordinator()
        coord.data = {ATTR_OUTDOOR_TEMP: 60.0, "other_key": "unchanged"}
        coord._apply_outdoor_temp(75.0, record_history=False)
        assert coord.data[ATTR_OUTDOOR_TEMP] == 75.0
        assert coord.data["other_key"] == "unchanged"
        coord.async_update_listeners.assert_called_once()

    def test_no_listener_notification_when_data_is_none(self):
        coord = _make_coordinator()
        assert coord.data is None
        coord._apply_outdoor_temp(75.0, record_history=False)
        coord.async_update_listeners.assert_not_called()


class TestRefreshWeatherServiceOutdoorTemp:
    def test_weather_service_source_updates_last_outdoor_temp(self):
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_WEATHER_SERVICE)
        weather_state = MagicMock()
        weather_state.attributes = {"temperature": 71.0}
        coord.hass.states.get = MagicMock(return_value=weather_state)
        coord._hourly_forecast_temps = []  # forces fallback to the live attribute
        coord._refresh_weather_service_outdoor_temp()
        assert coord._last_outdoor_temp == 71.0
        coord.automation_engine.update_outdoor_temp.assert_called_once_with(71.0)

    def test_sensor_source_is_skipped_entirely(self):
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_SENSOR)
        coord.hass.states.get = MagicMock(side_effect=AssertionError("must not be called for sensor source"))
        coord._refresh_weather_service_outdoor_temp()
        assert coord._last_outdoor_temp is None
        coord.automation_engine.update_outdoor_temp.assert_not_called()

    def test_input_number_source_is_skipped_entirely(self):
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_INPUT_NUMBER)
        coord.hass.states.get = MagicMock(side_effect=AssertionError("must not be called for input_number source"))
        coord._refresh_weather_service_outdoor_temp()
        assert coord._last_outdoor_temp is None

    def test_missing_weather_entity_state_is_a_silent_no_op(self):
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_WEATHER_SERVICE)
        coord.hass.states.get = MagicMock(return_value=None)
        coord._refresh_weather_service_outdoor_temp()
        assert coord._last_outdoor_temp is None
        coord.automation_engine.update_outdoor_temp.assert_not_called()

    def test_two_consecutive_ticks_with_different_forecasts_produce_different_values(self):
        """Simulates two 5-min ticks where the hourly forecast bracket changes —
        _last_outdoor_temp must actually vary, not stay pinned to a single stale value."""
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_WEATHER_SERVICE)
        weather_state = MagicMock()
        weather_state.attributes = {"temperature": 999.0}  # would only be used if interpolation unavailable
        coord.hass.states.get = MagicMock(return_value=weather_state)

        with patch("custom_components.climate_advisor.coordinator.dt_util.now") as mock_now:
            mock_now.return_value = datetime(2026, 5, 11, 13, 10, 0, tzinfo=UTC)
            coord._hourly_forecast_temps = [
                _fc_entry("2026-05-11T13:00:00+00:00", 70.0),
                _fc_entry("2026-05-11T14:00:00+00:00", 74.0),
            ]
            coord._refresh_weather_service_outdoor_temp()
            first = coord._last_outdoor_temp

            mock_now.return_value = datetime(2026, 5, 11, 13, 40, 0, tzinfo=UTC)
            coord._refresh_weather_service_outdoor_temp()
            second = coord._last_outdoor_temp

        assert first != second
        assert first == pytest.approx(70.7, abs=0.05)  # 10/60 of the way from 70 to 74
        assert second == pytest.approx(72.7, abs=0.05)  # 40/60 of the way from 70 to 74

    def test_async_thermal_sample_tick_calls_refresh_then_sample(self):
        """The 5-min tick must refresh outdoor temp before sampling observations.

        _async_thermal_sample_tick is decorated with @callback, which the test
        stub layer replaces with a MagicMock (homeassistant.core is a MagicMock
        module) — so the class attribute captured at import time is already a
        swallowed mock, not the real function (per project doctrine on invoking
        @callback-decorated methods directly). Patching coordinator.callback
        directly doesn't survive a reload (the module's `from homeassistant.core
        import callback` re-fetches it), so the source (homeassistant.core.callback)
        must be patched instead, before reloading. Reload again (unpatched)
        immediately after so every other test in this session keeps seeing the
        normal swallowed-by-default behavior.
        """
        coordinator_module = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch("homeassistant.core.callback", side_effect=lambda fn: fn):
            importlib.reload(coordinator_module)
            real_tick = coordinator_module.ClimateAdvisorCoordinator._async_thermal_sample_tick
        importlib.reload(coordinator_module)  # restore default (swallowed) state for other tests

        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_WEATHER_SERVICE)
        weather_state = MagicMock()
        weather_state.attributes = {"temperature": 65.0}
        coord.hass.states.get = MagicMock(return_value=weather_state)
        coord._hourly_forecast_temps = []
        coord._async_thermal_sample_tick = types.MethodType(real_tick, coord)

        coord._async_thermal_sample_tick(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC))
        assert coord._last_outdoor_temp == 65.0
        coord._sample_all_observations.assert_called_once()


class TestChartMachineryNonRegressionGuardrail:
    """Issue #511 must not touch _pred_archive, chart_log, _build_predicted_indoor_future,
    or _compute_target_band_schedule — the outdoor-temp refresh path is a fully separate
    code path from chart-prediction machinery (verified during design; this test guards
    against future accidental coupling)."""

    def test_apply_outdoor_temp_does_not_touch_pred_archive(self):
        coord = _make_coordinator()
        coord._pred_archive = {"sentinel": 123.0}
        coord._apply_outdoor_temp(70.0, record_history=False)
        assert coord._pred_archive == {"sentinel": 123.0}

    def test_refresh_weather_service_outdoor_temp_does_not_call_build_predicted_indoor_future(self):
        coord = _make_coordinator(outdoor_temp_source=TEMP_SOURCE_WEATHER_SERVICE)
        weather_state = MagicMock()
        weather_state.attributes = {"temperature": 65.0}
        coord.hass.states.get = MagicMock(return_value=weather_state)
        coord._hourly_forecast_temps = []
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        with (
            patch.object(mod, "_build_predicted_indoor_future") as mock_ode,
            patch.object(mod, "_compute_target_band_schedule") as mock_band,
        ):
            coord._refresh_weather_service_outdoor_temp()
            mock_ode.assert_not_called()
            mock_band.assert_not_called()


class TestAsyncEndOfDayRefetchesHourlyForecast:
    """Issue #511: the midnight reset must not leave a data gap for interpolation."""

    def test_hourly_forecast_repopulated_immediately_after_clear(self):
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = {"learning_enabled": True}
        coord._today_record = None
        coord._briefing_sent_today = True
        coord._briefing_day_type = "warm"
        coord._hvac_on_since = None
        coord._last_violation_check = None
        coord._outdoor_temp_history = [("2026-05-11T12:00:00", 70.0)]
        coord._indoor_temp_history = [("2026-05-11T12:00:00", 71.0)]
        coord._hourly_forecast_temps = [_fc_entry("2026-05-11T13:00:00+00:00", 70.0)]
        coord.automation_engine = MagicMock()
        coord._pre_cool_trigger_cancel = None
        coord._pre_cool_trigger_scheduled = True
        coord._pre_cool_status = "scheduled"
        coord._pre_cool_trigger_dt = None
        coord._pre_cool_target = None

        new_forecast = [_fc_entry("2026-05-12T00:00:00+00:00", 60.0)]
        coord._get_hourly_forecast_data = AsyncMock(return_value=new_forecast)
        coord._async_save_state = AsyncMock()

        coord._async_end_of_day = types.MethodType(ClimateAdvisorCoordinator._async_end_of_day, coord)

        from datetime import UTC, datetime

        asyncio.run(coord._async_end_of_day(datetime(2026, 5, 11, 23, 59, 0, tzinfo=UTC)))

        assert coord._hourly_forecast_temps == new_forecast
        assert coord._outdoor_temp_history == []
        coord._get_hourly_forecast_data.assert_awaited_once()
        coord._async_save_state.assert_awaited_once()
