"""Issue #1337: a weather outage keeps the last good forecast instead of wiping it.

Occupant view: during an outage the outdoor reading, the chart's future outdoor curve, the indoor
prediction and the nat-vent plan keep using the real forecast hours Climate Advisor already has.
The cosine model is used only once those hours have run out.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

_TZ = timezone(timedelta(hours=-8))
_NOW = datetime(2026, 11, 4, 17, 0, tzinfo=_TZ)
_WEATHER = "weather.forecast_home"


def _mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _hourly(start: datetime, hours: int, base: float = 60.0) -> list[dict]:
    return [{"datetime": (start + timedelta(hours=h)).isoformat(), "temperature": base + h} for h in range(hours)]


def _coord(fetch_results: list[list[dict]], kept: list[dict] | None = None, source: str = "weather_service"):
    mod = _mod()
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.config = {"weather_entity": _WEATHER, "outdoor_temp_source": source}
    coord.automation_engine = MagicMock()
    coord._hourly_forecast_temps = kept if kept is not None else []
    coord._get_hourly_forecast_data = AsyncMock(side_effect=fetch_results)
    coord._refresh_hourly_forecast = types.MethodType(mod.ClimateAdvisorCoordinator._refresh_hourly_forecast, coord)
    return coord


def _refresh(coord, now: datetime = _NOW) -> None:
    with patch.object(_mod().dt_util, "now", return_value=now):
        asyncio.run(coord._refresh_hourly_forecast())


class TestHourlyForecastKeptThroughOutage:
    def test_a_failed_fetch_keeps_the_future_hours_and_drops_the_past(self):
        kept = _hourly(_NOW - timedelta(hours=3), 51)  # fetched 3 h before the outage
        coord = _coord([[]], kept=kept)
        _refresh(coord)
        times = [datetime.fromisoformat(e["datetime"]) for e in coord._hourly_forecast_temps]
        assert times[0] == _NOW - timedelta(hours=1)  # one point behind now kept for interpolation
        assert times[-1] == _NOW + timedelta(hours=47)
        assert len(times) == 49
        assert coord.automation_engine._hourly_forecast_temps is coord._hourly_forecast_temps

    def test_a_successful_fetch_replaces_the_kept_forecast(self):
        fresh = _hourly(_NOW, 48, base=40.0)
        coord = _coord([fresh], kept=_hourly(_NOW, 48))
        _refresh(coord)
        assert coord._hourly_forecast_temps == fresh

    def test_the_kept_forecast_runs_out_and_only_then_is_empty(self):
        coord = _coord([[], []], kept=_hourly(_NOW, 3))
        _refresh(coord)
        assert len(coord._hourly_forecast_temps) == 3
        _refresh(coord, _NOW + timedelta(hours=4))
        assert coord._hourly_forecast_temps == []

    def test_nothing_kept_before_the_outage_stays_empty(self):
        coord = _coord([[]], kept=[])
        _refresh(coord)
        assert coord._hourly_forecast_temps == []

    def test_logs_once_when_kept_and_once_when_it_runs_out(self, caplog):
        coord = _coord([[], [], [], [_hourly(_NOW, 48)][0]], kept=_hourly(_NOW, 3))
        with caplog.at_level(logging.INFO):
            _refresh(coord)
            _refresh(coord, _NOW + timedelta(minutes=30))
            _refresh(coord, _NOW + timedelta(hours=4))
            _refresh(coord, _NOW + timedelta(hours=4, minutes=30))
        kept_lines = [r for r in caplog.records if "using the last forecast, covering until" in r.message]
        out_lines = [r for r in caplog.records if "last forecast has run out" in r.message]
        assert len(kept_lines) == 1 and kept_lines[0].levelno == logging.INFO
        assert len(out_lines) == 1 and out_lines[0].levelno == logging.WARNING
        assert coord._hourly_forecast_cache_state == "live"

    def test_midnight_reset_does_not_clear_the_hourly_forecast(self):
        src = inspect.getsource(_mod().ClimateAdvisorCoordinator._async_end_of_day)
        assert "_hourly_forecast_temps.clear()" not in src
        assert "await self._refresh_hourly_forecast()" in src


class TestConsumersUseTheKeptForecast:
    def test_the_chart_future_curve_uses_kept_hours_not_the_cosine_model(self):
        mod = _mod()
        coord = _coord([[]], kept=_hourly(_NOW - timedelta(hours=1), 49, base=50.0))
        _refresh(coord)
        classification = MagicMock(today_high=90.0, today_low=30.0)
        with patch.object(mod.dt_util, "now", return_value=_NOW):
            curve = mod._build_future_forecast_outdoor(coord._hourly_forecast_temps, classification)
        assert curve[0]["temp"] == 51.0  # the forecast's own value at now, not a 30-90 cosine day
        assert len(curve) == 48

    def test_weather_service_outdoor_comes_from_the_kept_forecast(self):
        coord = _coord([], kept=_hourly(_NOW - timedelta(hours=1), 3, base=50.0))
        with patch.object(_mod().dt_util, "now", return_value=_NOW + timedelta(minutes=30)):
            assert coord._outage_outdoor_temp() == 51.5

    def test_weather_service_outdoor_is_unknown_once_the_kept_forecast_is_gone(self):
        coord = _coord([], kept=[])
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._outage_outdoor_temp() is None

    def test_a_dedicated_sensor_install_still_reads_its_sensor(self):
        coord = _coord([], kept=_hourly(_NOW, 3), source="sensor")
        coord._live_dedicated_outdoor_temp = MagicMock(return_value=44.0)
        assert coord._outage_outdoor_temp() == 44.0


def _daily(start: datetime, days: int, high: float = 70.0, low: float = 50.0) -> list[dict]:
    return [
        {"datetime": (start + timedelta(days=d)).isoformat(), "temperature": high, "templow": low} for d in range(days)
    ]


def _outage_coord(source: str = "weather_service", daily=None, hourly=None):
    mod = _mod()
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.config = {"weather_entity": _WEATHER, "outdoor_temp_source": source, "outdoor_temp_entity": "sensor.out"}
    coord._daily_forecast_full = daily if daily is not None else []
    coord._hourly_forecast_temps = hourly if hourly is not None else []
    coord._build_forecast_snapshot = MagicMock(return_value="snapshot")
    return coord


class TestKeptForecastSnapshot:
    def test_built_from_the_kept_daily_forecast_with_a_real_outdoor_reading(self):
        coord = _outage_coord(daily=_daily(_NOW, 5), hourly=_hourly(_NOW - timedelta(hours=1), 3))
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._kept_forecast_snapshot() == "snapshot"
        kept, outdoor, attrs = coord._build_forecast_snapshot.call_args.args
        assert kept == coord._daily_forecast_full and outdoor == 61.0 and attrs == {}
        assert coord._build_forecast_snapshot.call_args.kwargs == {"from_kept_forecast": True}

    def test_none_when_nothing_kept_is_for_today(self):
        coord = _outage_coord(daily=_daily(_NOW + timedelta(days=1), 4), hourly=_hourly(_NOW, 3))
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._kept_forecast_snapshot() is None

    def test_none_without_a_real_outdoor_reading(self):
        coord = _outage_coord(daily=_daily(_NOW, 5), hourly=[])
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._kept_forecast_snapshot() is None


class TestNoPlaceholderDuringOutage:
    def test_weather_service_install_with_nothing_kept_is_unknown_not_65(self):
        coord = _outage_coord()
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._get_outdoor_temp({}, weather_down=True) is None

    def test_weather_service_install_reads_the_kept_forecast(self):
        coord = _outage_coord(hourly=_hourly(_NOW - timedelta(hours=1), 3, base=80.0))
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._get_outdoor_temp({}, weather_down=True) == 81.0

    def test_a_failed_sensor_falls_back_to_the_kept_forecast_and_says_so(self):
        coord = _outage_coord(source="sensor", hourly=_hourly(_NOW - timedelta(hours=1), 3, base=80.0))
        coord._read_outdoor_sensor = MagicMock(return_value=(None, "unavailable"))
        coord._note_outdoor_sensor_state = MagicMock()
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            assert coord._get_outdoor_temp({}, weather_down=True) == 81.0
        coord._note_outdoor_sensor_state.assert_called_with("unavailable", "sensor.out", "kept_forecast", 81.0)

    def test_the_live_service_path_is_unchanged(self):
        coord = _outage_coord()
        coord._get_weather_outdoor_temp = MagicMock(return_value=(65.0, "weather_attribute"))
        assert coord._get_outdoor_temp({"temperature": 65}) == 65.0

    def test_the_5_minute_refresh_keeps_the_last_value_when_nothing_real_exists(self):
        coord = _outage_coord()
        coord.hass = MagicMock()
        coord.hass.states.get.return_value = MagicMock(state="unavailable", attributes={})
        coord._apply_outdoor_temp = MagicMock()
        with patch.object(_mod().dt_util, "now", return_value=_NOW):
            coord._refresh_outdoor_temp()
        coord._apply_outdoor_temp.assert_not_called()


class TestWeatherIndependentCycle:
    def _coord(self, *, outdoor=None, classification=True):
        mod = _mod()
        coord = object.__new__(mod.ClimateAdvisorCoordinator)
        coord._current_classification = MagicMock() if classification else None
        coord._startup_timer_fired = True
        coord._startup_coalesce_active = True
        for name in (
            "_run_first_cycle_recovery",
            "_check_climate_unit",
            "_check_unit_sources",
            "_ensure_today_record",
            "_refresh_thermal_model_cache",
            "_resolve_tou_schedule_state",
            "_resolve_target_band_schedule",
            "_check_stuck_grace",
            "_check_orphaned_grace",
            "_track_indoor_comfort",
        ):
            setattr(coord, name, MagicMock())
        coord._do_startup_coalesce = AsyncMock()
        coord._apply_tou_schedule = AsyncMock()
        coord._async_save_state = AsyncMock()
        coord._get_indoor_temp = MagicMock(return_value=66.0)
        coord._outage_outdoor_temp = MagicMock(return_value=outdoor)
        coord._should_run_regular_cycle_window_cooling_check = MagicMock(return_value=False)
        coord._should_run_regular_cycle_nat_vent_check = MagicMock(return_value=True)
        coord.automation_engine = MagicMock()
        coord.automation_engine.check_natural_vent_conditions = AsyncMock()
        return coord

    def test_runs_everything_that_needs_no_forecast(self):
        coord = self._coord()
        asyncio.run(coord._run_weather_independent_cycle())
        coord._do_startup_coalesce.assert_awaited_once()  # ends the post-restart window
        coord._ensure_today_record.assert_called_once_with(coord._current_classification)
        coord._track_indoor_comfort.assert_called_once_with(66.0)
        coord._check_stuck_grace.assert_called_once()
        coord._check_orphaned_grace.assert_called_once()
        coord._apply_tou_schedule.assert_awaited_once()
        coord._async_save_state.assert_awaited_once()

    def test_ventilation_rechecks_wait_for_a_real_outdoor_reading(self):
        coord = self._coord(outdoor=None)
        asyncio.run(coord._run_weather_independent_cycle())
        coord.automation_engine.check_natural_vent_conditions.assert_not_awaited()
        coord = self._coord(outdoor=58.0)
        asyncio.run(coord._run_weather_independent_cycle())
        coord.automation_engine.check_natural_vent_conditions.assert_awaited_once()

    def test_the_startup_window_ends_even_without_a_saved_day_type(self):
        coord = self._coord(classification=False)
        asyncio.run(coord._run_weather_independent_cycle())
        coord._do_startup_coalesce.assert_awaited_once()
        coord._ensure_today_record.assert_not_called()
