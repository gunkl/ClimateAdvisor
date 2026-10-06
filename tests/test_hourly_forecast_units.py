"""Issue #1015 Defect B / #1016 — hourly forecast unit normalisation.

The weather entity's HOURLY forecast arrives in the provider's native unit. Before
this fix it was stored as-is in ``_hourly_forecast_temps`` while every consumer
(ODE/ceiling-guard cache, nat-vent guard/plan, Next Automation card, chart forecast
series) treats it as internal °F — so on a Celsius install the units were mixed.
``ClimateAdvisorCoordinator._get_hourly_forecast_data()`` now returns a normalised
COPY in °F, and ``_refresh_hourly_forecast()`` is the single choke point that
assigns it to the coordinator AND the engine (#1016: the briefing path used to skip
the engine mirror).

Every behavioural case is parametrised for "celsius" and "fahrenheit"; the
Fahrenheit run must show NO change from the provider's values.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import math
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from tests.test_forecast_chart_integration import (  # noqa: E402
    _daily_entry,
    _get_coordinator_module,
    _make_full_chart_coord,
    _patched_dt_util,
)
from tests.test_nat_vent_activation import _DT_NOW_PATH, _make_engine  # noqa: E402

_WEATHER = "weather.forecast_home"
_UNITS = ["celsius", "fahrenheit"]


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _native(temp_c: float, unit: str) -> float:
    """What the weather provider reports for a true temperature of *temp_c* °C."""
    return temp_c if unit == "celsius" else temp_c * 9 / 5 + 32


def _to_c(temp_f: float) -> float:
    return (temp_f - 32) * 5 / 9


def _make_pipeline_coord(unit: str, provider_entries: list[dict]):
    """Coordinator whose REAL _get_hourly_forecast_data/_refresh_hourly_forecast run
    against a stubbed weather.get_forecasts service returning *provider_entries*."""
    cls = _get_coordinator_class()
    coord = _make_full_chart_coord(daily_forecast_full=None, temp_unit=unit)
    coord.config["weather_entity"] = _WEATHER
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=MagicMock())
    coord.hass.services.async_call = AsyncMock(return_value={_WEATHER: {"forecast": provider_entries}})
    coord._get_hourly_forecast_data = types.MethodType(cls._get_hourly_forecast_data, coord)
    coord._refresh_hourly_forecast = types.MethodType(cls._refresh_hourly_forecast, coord)
    return coord


# ---------------------------------------------------------------------------
# a. _get_hourly_forecast_data normalisation
# ---------------------------------------------------------------------------


class TestGetHourlyForecastDataNormalisation:
    @pytest.mark.parametrize("unit", _UNITS)
    def test_converts_to_internal_fahrenheit(self, unit):
        entries = [
            {"datetime": "2026-06-01T10:00:00+00:00", "temperature": _native(15.0, unit)},
            {"datetime": "2026-06-01T11:00:00+00:00", "temperature": _native(22.0, unit)},
        ]
        coord = _make_pipeline_coord(unit, entries)
        result = asyncio.run(coord._get_hourly_forecast_data())
        assert [e["temperature"] for e in result] == pytest.approx([59.0, 71.6])

    def test_celsius_literal_values(self):
        entries = [{"datetime": "x", "temperature": 15.0}, {"datetime": "y", "temperature": 22.0}]
        coord = _make_pipeline_coord("celsius", entries)
        result = asyncio.run(coord._get_hourly_forecast_data())
        assert [e["temperature"] for e in result] == pytest.approx([59.0, 71.6])

    def test_fahrenheit_passthrough_values_identical(self):
        entries = [{"datetime": "x", "temperature": 59.0}, {"datetime": "y", "temperature": 71.6}]
        coord = _make_pipeline_coord("fahrenheit", entries)
        result = asyncio.run(coord._get_hourly_forecast_data())
        assert [e["temperature"] for e in result] == [59.0, 71.6]

    @pytest.mark.parametrize("unit", _UNITS)
    def test_input_not_mutated_and_returns_copy(self, unit):
        entries = [{"datetime": "x", "temperature": _native(15.0, unit), "condition": "sunny"}]
        snapshot = copy.deepcopy(entries)
        coord = _make_pipeline_coord(unit, entries)
        result = asyncio.run(coord._get_hourly_forecast_data())
        assert entries == snapshot
        assert result[0] is not entries[0]
        assert result[0]["condition"] == "sunny"

    @pytest.mark.parametrize("unit", _UNITS)
    def test_missing_none_and_temp_key_entries(self, unit):
        entries = [
            {"datetime": "a", "temperature": None},
            {"datetime": "b"},
            {"datetime": "c", "temp": _native(10.0, unit)},
            {"datetime": "d", "temperature": "not-a-number"},
        ]
        coord = _make_pipeline_coord(unit, entries)
        result = asyncio.run(coord._get_hourly_forecast_data())
        assert result[0] == {"datetime": "a", "temperature": None}
        assert result[1] == {"datetime": "b"}
        assert result[2]["temp"] == pytest.approx(50.0)
        assert result[3]["temperature"] == "not-a-number"

    @pytest.mark.parametrize("unit", _UNITS)
    def test_exception_path_still_returns_empty_list(self, unit):
        coord = _make_pipeline_coord(unit, [])
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        assert asyncio.run(coord._get_hourly_forecast_data()) == []

    @pytest.mark.parametrize("unit", _UNITS)
    def test_empty_response(self, unit):
        coord = _make_pipeline_coord(unit, [])
        assert asyncio.run(coord._get_hourly_forecast_data()) == []


# ---------------------------------------------------------------------------
# b. _refresh_hourly_forecast mirrors to the engine
# ---------------------------------------------------------------------------


class TestRefreshHourlyForecast:
    @pytest.mark.parametrize("unit", _UNITS)
    def test_sets_coordinator_and_engine_to_same_normalised_list(self, unit):
        entries = [{"datetime": "a", "temperature": _native(20.0, unit)}]
        coord = _make_pipeline_coord(unit, entries)
        coord.automation_engine._hourly_forecast_temps = None
        asyncio.run(coord._refresh_hourly_forecast())
        assert coord._hourly_forecast_temps[0]["temperature"] == pytest.approx(68.0)
        assert coord.automation_engine._hourly_forecast_temps is coord._hourly_forecast_temps

    def test_calls_get_hourly_forecast_data(self):
        cls = _get_coordinator_class()
        coord = _make_full_chart_coord(daily_forecast_full=None)
        coord._get_hourly_forecast_data = AsyncMock(return_value=[{"temperature": 1.0}])
        coord._refresh_hourly_forecast = types.MethodType(cls._refresh_hourly_forecast, coord)
        asyncio.run(coord._refresh_hourly_forecast())
        coord._get_hourly_forecast_data.assert_awaited_once()
        assert coord.automation_engine._hourly_forecast_temps == [{"temperature": 1.0}]


# ---------------------------------------------------------------------------
# c. End-to-end chart on real pipeline
# ---------------------------------------------------------------------------

_CHART_NOW = datetime(2026, 6, 1, 8, 0, 0)


def _native_hourly_48h(unit: str) -> list[dict]:
    entries = []
    for i in range(48):
        dt = datetime(2026, 6, 1, 0, 0, 0) + timedelta(hours=i)
        temp_c = 19.0 - 3.0 * math.cos(2 * math.pi * (i % 24) / 24)  # smooth 16..22 C
        entries.append({"datetime": dt.isoformat(), "temperature": _native(temp_c, unit)})
    return entries


def _native_daily_6d(unit: str) -> list[dict]:
    out = []
    for i in range(6):
        d = datetime(2026, 6, 1) + timedelta(days=i)
        out.append(_daily_entry(d.isoformat(), _native(22.0 + i * 0.5, unit), _native(15.0, unit)))
    return out


class TestChartEndToEnd:
    @pytest.mark.parametrize("unit", _UNITS)
    def test_forecast_series_in_true_display_unit_without_seam_step(self, unit):
        coord_mod = _get_coordinator_module()
        coord = _make_pipeline_coord(unit, _native_hourly_48h(unit))
        coord._daily_forecast_full = _native_daily_6d(unit)
        coord._get_indoor_temp = MagicMock(return_value=70.0)  # internal F

        asyncio.run(coord._refresh_hourly_forecast())

        now_p, as_local_p = _patched_dt_util(coord_mod, _CHART_NOW)
        with now_p, as_local_p:
            result = coord.get_chart_data("7d")

        fo = sorted(result["forecast_outdoor"], key=lambda e: e["ts"])
        temps = [e["temp"] for e in fo]
        assert temps, "forecast_outdoor must be populated"
        # Display unit equals the install unit; true range is 15..24 C.
        lo_c, hi_c = (15.0, 24.5) if unit == "celsius" else (59.0, 76.1)
        assert min(temps) >= lo_c - 1.0, (unit, min(temps))
        assert max(temps) <= hi_c + 1.0, (unit, max(temps))
        # Real hourly entries are a smooth 16..22 C sinusoid.
        step_limit = 4.0 if unit == "celsius" else 7.2
        diffs = [abs(b - a) for a, b in zip(temps, temps[1:], strict=False)]
        assert max(diffs) <= step_limit, f"step at real->synthetic seam: {max(diffs)} ({unit})"
        # Synthetic days present (past the 48h real horizon).
        assert datetime.fromisoformat(fo[-1]["ts"]).date() > datetime(2026, 6, 2).date()

        pi = [e["temp"] for e in result["predicted_indoor"]]
        assert pi, "predicted_indoor must be populated"
        lo_i, hi_i = (5.0, 32.0) if unit == "celsius" else (41.0, 90.0)
        assert all(lo_i <= t <= hi_i for t in pi), (unit, min(pi), max(pi))


# ---------------------------------------------------------------------------
# d. _get_outdoor_temp: no double conversion
# ---------------------------------------------------------------------------


class TestOutdoorTempNoDoubleConversion:
    @pytest.mark.parametrize("unit", _UNITS)
    def test_weather_service_interpolation_returns_correct_fahrenheit(self, unit):
        cls = _get_coordinator_class()
        coord_mod = _get_coordinator_module()
        entries = [
            {"datetime": "2026-06-01T10:00:00+00:00", "temperature": _native(10.0, unit)},
            {"datetime": "2026-06-01T11:00:00+00:00", "temperature": _native(20.0, unit)},
        ]
        coord = _make_pipeline_coord(unit, entries)
        coord.config["outdoor_temp_source"] = "weather_service"
        coord._get_outdoor_temp = types.MethodType(cls._get_outdoor_temp, coord)
        asyncio.run(coord._refresh_hourly_forecast())
        with patch.object(coord_mod.dt_util, "now", return_value=datetime(2026, 6, 1, 10, 30, tzinfo=UTC)):
            result = coord._get_outdoor_temp({"temperature": 0})
        # midpoint of 10 C (50 F) and 20 C (68 F) = 59 F
        assert result == pytest.approx(59.0, abs=0.01)


# ---------------------------------------------------------------------------
# e. Nat-vent forecast-peak guard on the REAL engine path
# ---------------------------------------------------------------------------


class TestNatVentForecastGuardUnits:
    def _run(self, unit: str, peak_c: float):
        # Provider reports the 11:00 peak in its native unit; pipeline normalises it.
        entries = [{"datetime": "2026-04-20T11:00:00+00:00", "temperature": _native(peak_c, unit)}]
        coord = _make_pipeline_coord(unit, entries)
        asyncio.run(coord._refresh_hourly_forecast())

        # nat_vent_threshold = comfort_cool(73) + delta(2) = 75 F (23.9 C)
        engine = _make_engine(comfort_heat=70.0, comfort_cool=73.0, nat_vent_delta=2.0, indoor_f=73.0)
        engine._last_outdoor_temp = 68.0
        engine._natural_vent_active = False
        engine._fan_override_active = False
        engine._hourly_forecast_temps = coord.automation_engine._hourly_forecast_temps
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))
        now_aware = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
        with patch(_DT_NOW_PATH, return_value=now_aware):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        return engine, events

    @pytest.mark.parametrize("unit", _UNITS)
    def test_peak_above_threshold_skips_activation(self, unit):
        # 24.4 C == 75.9 F > 75 F threshold. Un-normalised, 24.4 < 75 would activate.
        engine, events = self._run(unit, 24.4)
        assert not engine._natural_vent_active
        assert [e for e in events if e[0] == "nat_vent_forecast_skip"]

    @pytest.mark.parametrize("unit", _UNITS)
    def test_peak_below_threshold_allows_activation(self, unit):
        # 23.0 C == 73.4 F < 75 F threshold.
        engine, events = self._run(unit, 23.0)
        assert engine._natural_vent_active
        assert not [e for e in events if e[0] == "nat_vent_forecast_skip"]


# ---------------------------------------------------------------------------
# f. #1016: all three refresh sites go through _refresh_hourly_forecast
# ---------------------------------------------------------------------------


class TestAllRefreshSitesUseHelper:
    """Source-inspection guard over all three sites. The briefing site is also driven
    behaviourally in tests/test_coordinator.py::TestBriefingNotificationSplit::
    test_briefing_refetch_mirrors_hourly_forecast_to_engine; the helper's
    mirror-to-engine behaviour is covered in TestRefreshHourlyForecast."""

    @pytest.mark.parametrize("method", ["_async_update_data_impl", "_async_send_briefing", "_async_end_of_day"])
    def test_site_calls_helper_and_never_assigns_directly(self, method):
        src = inspect.getsource(getattr(_get_coordinator_class(), method))
        assert "await self._refresh_hourly_forecast()" in src
        assert "self._hourly_forecast_temps = " not in src

    def test_no_other_package_assignment_outside_helper_and_init(self):
        src = inspect.getsource(_get_coordinator_module())
        lines = [ln.strip() for ln in src.splitlines() if "self._hourly_forecast_temps =" in ln]
        assert "self._hourly_forecast_temps = await self._get_hourly_forecast_data()" in lines
        assert len(lines) == 1, lines


# ---------------------------------------------------------------------------
# g. Data-shape problem vs real service failure
# ---------------------------------------------------------------------------


class TestNoneForecastIsNotAServiceFailure:
    @pytest.mark.parametrize("unit", _UNITS)
    def test_none_forecast_returns_empty_without_unsupported_flag(self, unit):
        coord = _make_pipeline_coord(unit, [])
        coord.hass.services.async_call = AsyncMock(return_value={_WEATHER: {"forecast": None}})
        coord._hourly_forecast_confirmed_unsupported = False
        with patch.object(_get_coordinator_module()._LOGGER, "error") as err:
            assert asyncio.run(coord._get_hourly_forecast_data()) == []
        err.assert_not_called()
        assert coord._hourly_forecast_confirmed_unsupported is False

    @pytest.mark.parametrize("unit", _UNITS)
    def test_real_service_exception_still_sets_flag_and_logs_error(self, unit):
        coord = _make_pipeline_coord(unit, [])
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        coord._hourly_forecast_confirmed_unsupported = False
        with patch.object(_get_coordinator_module()._LOGGER, "error") as err:
            assert asyncio.run(coord._get_hourly_forecast_data()) == []
        err.assert_called_once()
        assert coord._hourly_forecast_confirmed_unsupported is True
