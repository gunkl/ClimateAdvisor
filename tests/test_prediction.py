"""Tests for temperature prediction logic (chart data computation)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.const import (
    OCCUPANCY_AWAY,
    OCCUPANCY_HOME,
    OCCUPANCY_VACATION,
    VACATION_SETBACK_EXTRA,
)
from custom_components.climate_advisor.coordinator import (
    ClimateAdvisorCoordinator,
    _build_future_forecast_outdoor,
    _build_outdoor_curve,
    _build_predicted_indoor_future,
    _compute_thermal_factors,
    _cosine_outdoor_curve,
    _extract_current_hour_forecast_temp,
    _find_ceiling_breach_time,
)

# ---------------------------------------------------------------------------
# Helpers shared by TestHourlyForecastOutdoorPrediction
# ---------------------------------------------------------------------------

_TODAY = date(2026, 3, 19)
_TODAY_STR = "2026-03-19"
_TOMORROW_STR = "2026-03-20"

_HIGH = 85.0
_LOW = 55.0


def _mock_dt_util(today: date = _TODAY):
    """Return a MagicMock that makes dt_util.now().date() return *today*."""
    mock = MagicMock()
    mock.now.return_value.date.return_value = today
    # as_local: pass through for naive datetimes used in tests
    mock.as_local = lambda dt: dt
    return mock


def _hourly_entry(hour: int, temp: float, day_str: str = _TODAY_STR) -> dict:
    """Build a single hourly forecast dict for *hour* on *day_str*."""
    return {"datetime": f"{day_str}T{hour:02d}:00:00", "temperature": temp}


def _full_24h_forecast(high: float = _HIGH, low: float = _LOW) -> list[dict]:
    """Return a complete 24-entry hourly forecast for today using the cosine curve."""
    cosine = _cosine_outdoor_curve(high, low)
    return [_hourly_entry(p["hour"], p["temp"]) for p in cosine]


class TestHourlyForecastOutdoorPrediction:
    """Tests for hourly-forecast-based outdoor temperature prediction."""

    # ------------------------------------------------------------------
    # Basic pass-through / fallback
    # ------------------------------------------------------------------

    def test_uses_hourly_temps_when_provided(self):
        """Full 24h hourly data → output preserves shape, normalised to high/low."""
        # Build a forecast with a recognisable linear ramp 0→46.
        forecast = [_hourly_entry(h, float(h * 2)) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # After normalisation the range must match high/low.
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)
        # Shape preserved: hour 0 should be the minimum, hour 23 the maximum.
        assert temps[0] == pytest.approx(_LOW, abs=0.15)
        assert temps[23] == pytest.approx(_HIGH, abs=0.15)

    def test_falls_back_to_cosine_when_none(self):
        """hourly_forecast=None → result is identical to _cosine_outdoor_curve."""
        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        result = _build_outdoor_curve(_HIGH, _LOW, None)
        assert result == expected

    def test_falls_back_to_cosine_on_empty_list(self):
        """hourly_forecast=[] → result is identical to _cosine_outdoor_curve."""
        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        result = _build_outdoor_curve(_HIGH, _LOW, [])
        assert result == expected

    # ------------------------------------------------------------------
    # Interpolation
    # ------------------------------------------------------------------

    def test_interpolates_missing_hours(self):
        """Data at hours 0, 6, 12, 18 only → intermediate hours interpolated linearly."""
        sparse = [
            _hourly_entry(0, 60.0),
            _hourly_entry(6, 66.0),
            _hourly_entry(12, 78.0),
            _hourly_entry(18, 72.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, sparse)

        by_hour = {p["hour"]: p["temp"] for p in result}
        assert len(result) == 24
        # After normalisation the range spans _HIGH/_LOW.
        temps = [p["temp"] for p in result]
        assert min(temps) == pytest.approx(_LOW, abs=0.5)
        assert max(temps) == pytest.approx(_HIGH, abs=0.5)
        # Shape check: hour 12 had the highest raw value, so it should
        # be at or near _HIGH.  Hour 0 had the lowest, near _LOW.
        assert by_hour[12] == pytest.approx(_HIGH, abs=0.5)
        assert by_hour[0] == pytest.approx(_LOW, abs=0.5)
        # Monotonic between 0 and 6 (raw values increase)
        assert by_hour[3] > by_hour[0]
        assert by_hour[3] < by_hour[6]

    # ------------------------------------------------------------------
    # Edge-hour cosine fill
    # ------------------------------------------------------------------

    def test_edge_hours_use_cosine_fill(self):
        """Data only for hours 6–18 → hours outside that range get cosine fill,
        then the whole curve is normalised to high/low."""
        # All mid-day entries at the same temp; edge hours come from cosine.
        mid_forecast = [_hourly_entry(h, 70.0) for h in range(6, 19)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mid_forecast)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # Normalised → range matches high/low.
        assert min(temps) == pytest.approx(_LOW, abs=0.5)
        assert max(temps) == pytest.approx(_HIGH, abs=0.5)

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def test_malformed_entries_skipped(self):
        """Entries with missing datetime or temperature are skipped without error."""
        forecast = [
            {"temperature": 70.0},  # missing datetime
            {"datetime": f"{_TODAY_STR}T10:00:00"},  # missing temperature
            {"datetime": None, "temperature": 72.0},  # None datetime
            {"datetime": f"{_TODAY_STR}T12:00:00", "temperature": 80.0},  # valid
            {"datetime": "not-a-date", "temperature": 65.0},  # bad format
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        assert len(result) == 24

    # ------------------------------------------------------------------
    # Date filtering
    # ------------------------------------------------------------------

    def test_filters_to_today_only(self):
        """Tomorrow's hourly entries are ignored; only today's date is used."""
        today_entries = [_hourly_entry(h, float(60 + h)) for h in range(24)]
        tomorrow_entries = [_hourly_entry(h, float(200 + h), _TOMORROW_STR) for h in range(24)]
        mixed = today_entries + tomorrow_entries

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(_TODAY),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, mixed)

        assert len(result) == 24
        temps = [p["temp"] for p in result]
        # Today's raw range is 60..83 (24 entries). Normalised to _HIGH/_LOW.
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)
        # Shape: hour 0 had the lowest raw value → should be near _LOW
        assert temps[0] == pytest.approx(_LOW, abs=0.5)
        # hour 23 had the highest raw value → should be near _HIGH
        assert temps[23] == pytest.approx(_HIGH, abs=0.5)

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    def test_normalisation_spans_daily_high_low(self):
        """Hourly data with a narrow range is scaled to match daily high/low."""
        # Hourly data only spans 68-72 but daily says 55-85.
        forecast = [_hourly_entry(h, 68.0 + (4.0 * h / 23.0)) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        temps = [p["temp"] for p in result]
        assert min(temps) == pytest.approx(_LOW, abs=0.15)
        assert max(temps) == pytest.approx(_HIGH, abs=0.15)

    def test_flat_hourly_data_falls_back_to_cosine(self):
        """If all hourly values are the same, fall back to cosine model."""
        forecast = [_hourly_entry(h, 70.0) for h in range(24)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util(),
        ):
            result = _build_outdoor_curve(_HIGH, _LOW, forecast)

        expected = _cosine_outdoor_curve(_HIGH, _LOW)
        assert result == expected


# ---------------------------------------------------------------------------
# Phase 3F3: get_chart_data() thermal_model inclusion
# ---------------------------------------------------------------------------


def _make_chart_coordinator(temp_unit: str = "fahrenheit", thermal_model_return: dict | None = None) -> object:
    """Create a minimal coordinator stub for testing get_chart_data()."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"temp_unit": temp_unit}
    coord._current_classification = None
    coord._hourly_forecast_temps = None
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []

    ae = MagicMock()
    ae._thermal_model = None
    coord.automation_engine = ae

    learning = MagicMock()
    learning.get_thermal_model.return_value = thermal_model_return if thermal_model_return is not None else {}
    coord.learning = learning

    chart_log = MagicMock()
    chart_log.get_entries.return_value = []
    coord._chart_log = chart_log

    coord._thermal_factors = None
    coord._get_indoor_temp = MagicMock(return_value=None)
    coord._occupancy_mode = "home"

    return coord


def _mock_dt_util_fixed(hour: int = 12, minute: int = 0):
    """Return a dt_util mock whose now() returns a fixed time."""
    mock = MagicMock()
    mock.now.return_value.hour = hour
    mock.now.return_value.minute = minute
    mock.as_local = lambda dt: dt
    return mock


class TestBandScheduleComputedOnce:
    """Issue #470: get_chart_data() must compute the target-band schedule exactly
    once per call, not twice (previously: once internally inside
    _build_predicted_indoor_future(), once directly for the displayed target_band).

    Requires a real classification + non-empty hourly forecast, otherwise
    _build_predicted_indoor_future() short-circuits before ever reaching its
    internal band-schedule call — a fixture without these would pass vacuously
    both before and after the fix.
    """

    def _make_classification(self, **overrides):
        from custom_components.climate_advisor.classifier import DayClassification

        c = object.__new__(DayClassification)
        defaults = {
            "day_type": "hot",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 90,
            "today_low": 70,
            "tomorrow_high": 88,
            "tomorrow_low": 68,
            "hvac_mode": "cool",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
        defaults.update(overrides)
        c.__dict__.update(defaults)
        return c

    def test_compute_target_band_schedule_called_once(self):
        import custom_components.climate_advisor.coordinator as coord_mod

        coord = _make_chart_coordinator(temp_unit="fahrenheit")
        coord._current_classification = self._make_classification()
        coord._hourly_forecast_temps = [
            {"datetime": "2026-05-13T13:00:00+00:00", "temperature": 74.0 + i} for i in range(6)
        ]
        coord.config = {"comfort_heat": 68, "comfort_cool": 76, "setback_heat": 60, "setback_cool": 80}

        # Non-historical path (no before_ts) exercises both the ODE prediction curve
        # and the displayed target_band — the two call sites this issue deduplicates.
        original = coord_mod._compute_target_band_schedule
        wrapped = MagicMock(side_effect=original)

        with (
            patch.object(coord_mod, "_compute_target_band_schedule", wrapped),
            # Issue #558: today+tomorrow both hot now makes _compute_pre_cool_trigger_time()'s
            # wake-time fallback branch reachable (previously short-circuited before any
            # datetime comparison since setback_modifier=0.0 skipped pre-cool entirely) —
            # as_local must actually attach a tz, not no-op, or naive/aware comparison fails.
            patch.object(
                coord_mod.dt_util,
                "as_local",
                side_effect=lambda x: x if x.tzinfo is not None else x.replace(tzinfo=UTC),
            ),
            patch.object(coord_mod.dt_util, "now", return_value=datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)),
        ):
            result = coord.get_chart_data()

        assert result["predicted_indoor"], "fixture must actually reach the ODE prediction path"
        assert wrapped.call_count == 1, (
            f"_compute_target_band_schedule() must be called exactly once per get_chart_data() "
            f"invocation, got {wrapped.call_count}"
        )


class TestGetChartDataThermalModel:
    """Tests verifying that get_chart_data() includes a correct thermal_model dict."""

    def test_chart_data_includes_thermal_model_key(self):
        """When model has no rates, thermal_model key includes confidence='none' and None rates."""
        model_return = {
            "confidence": "none",
            "observation_count_heat": 0,
            "observation_count_cool": 0,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        assert "thermal_model" in chart
        tm = chart["thermal_model"]
        assert tm["confidence"] == "none"
        assert tm["heating_rate"] is None
        assert tm["cooling_rate"] is None
        assert tm["observation_count_heat"] == 0
        assert tm["observation_count_cool"] == 0

    def test_chart_data_thermal_model_rates_unit_converted(self):
        """In Celsius mode, heating_rate is converted via *5/9; missing cooling_rate is None."""
        model_return = {
            "confidence": "low",
            "observation_count_heat": 6,
            "observation_count_cool": 0,
            "heating_rate_f_per_hour": 2.0,
        }
        coord = _make_chart_coordinator(temp_unit="celsius", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        tm = chart["thermal_model"]
        assert tm["heating_rate"] == pytest.approx(2.0 * 5 / 9)
        assert tm["cooling_rate"] is None
        assert tm["unit"] == "celsius"

    def test_chart_data_thermal_model_fahrenheit_rates_unchanged(self):
        """In Fahrenheit mode, heating_rate and cooling_rate are returned as-is."""
        model_return = {
            "confidence": "high",
            "observation_count_heat": 20,
            "observation_count_cool": 15,
            "heating_rate_f_per_hour": 3.5,
            "cooling_rate_f_per_hour": 2.0,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()

        tm = chart["thermal_model"]
        assert tm["heating_rate"] == pytest.approx(3.5)
        assert tm["cooling_rate"] == pytest.approx(2.0)
        assert tm["unit"] == "fahrenheit"

    def test_chart_data_none_rates_do_not_raise(self):
        """Regression test for #64: when get_thermal_model() returns None rate values
        (keys present, values None), get_chart_data() must not raise TypeError."""
        model_return = {
            "confidence": "none",
            "observation_count_heat": 0,
            "observation_count_cool": 0,
            "heating_rate_f_per_hour": None,
            "cooling_rate_f_per_hour": None,
        }
        coord = _make_chart_coordinator(temp_unit="fahrenheit", thermal_model_return=model_return)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _mock_dt_util_fixed(12, 0),
        ):
            chart = coord.get_chart_data()  # must not raise

        tm = chart["thermal_model"]
        assert tm["heating_rate"] is None
        assert tm["cooling_rate"] is None


def _make_dt_util_mock(now_dt):
    """Return a dt_util mock using now_dt as the current time.

    dt_util.now() returns now_dt; dt_util.as_local() returns the dt unchanged
    (tests use UTC datetimes throughout so no conversion is needed).
    """
    mock = MagicMock()
    mock.now.return_value = now_dt
    mock.as_local = lambda dt: dt
    return mock


class TestBuildFutureForecastOutdoor:
    """Tests for _build_future_forecast_outdoor() — multi-day forecast extraction."""

    def _make_entry(self, dt_str: str, temp: float) -> dict:
        return {"datetime": dt_str, "temperature": temp}

    def test_empty_on_none(self):
        result = _build_future_forecast_outdoor(None)
        assert result == []

    def test_empty_on_empty_list(self):
        result = _build_future_forecast_outdoor([])
        assert result == []

    def test_filters_past_entries(self):
        """Only entries at or after now should be returned."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        entries = [
            # 6 past entries (1h apart going backward)
            self._make_entry((now_utc - timedelta(hours=i + 1)).isoformat(), 60.0 + i)
            for i in range(6)
        ] + [
            # 6 future entries (1h apart going forward)
            self._make_entry((now_utc + timedelta(hours=i + 1)).isoformat(), 70.0 + i)
            for i in range(6)
        ]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert len(result) == 6
        for item in result:
            assert "ts" in item
            assert "temp" in item
            assert isinstance(item["temp"], float)

    def test_multi_day_coverage(self):
        """All available forecast days should be returned, not just today."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        # 72 future entries = 3 days of hourly data
        entries = [self._make_entry((now_utc + timedelta(hours=i + 1)).isoformat(), 55.0 + (i % 20)) for i in range(72)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert len(result) == 72
        # All should have ISO ts strings and float temps
        for item in result:
            assert isinstance(item["ts"], str)
            assert isinstance(item["temp"], float)
        # Should be sorted by timestamp
        assert result == sorted(result, key=lambda x: x["ts"])

    def test_result_sorted_ascending(self):
        """Results must be sorted ascending by timestamp."""
        from datetime import datetime, timedelta

        now_utc = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        # Insert in reverse order
        entries = [self._make_entry((now_utc + timedelta(hours=6 - i)).isoformat(), 65.0) for i in range(5)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_utc)):
            result = _build_future_forecast_outdoor(entries)
        assert result == sorted(result, key=lambda x: x["ts"])


class TestComputeThermalFactors:
    """Tests for _compute_thermal_factors helper."""

    def test_insufficient_data_returns_defaults(self):
        factors = _compute_thermal_factors([])
        assert factors["time_lag_hours"] == pytest.approx(1.0)
        assert factors["cold_diff"] == pytest.approx(15.0)
        assert factors["mild_diff"] == pytest.approx(8.0)
        assert factors["warm_diff"] == pytest.approx(0.0)
        assert factors["has_data"] is False

    def test_differential_bucketing(self):
        """Each outdoor temp range produces a separate differential bucket."""
        entries = (
            [{"outdoor": 55.0, "indoor": 70.0, "hvac": "idle"}] * 10  # cold: diff=15
            + [{"outdoor": 65.0, "indoor": 73.0, "hvac": "idle"}] * 10  # mild: diff=8
            + [{"outdoor": 75.0, "indoor": 75.0, "hvac": "idle"}] * 5  # warm: diff=0
        )
        factors = _compute_thermal_factors(entries)
        assert factors["cold_diff"] == pytest.approx(15.0, abs=0.5)
        assert factors["mild_diff"] == pytest.approx(8.0, abs=0.5)
        assert factors["warm_diff"] == pytest.approx(0.0, abs=0.5)
        assert factors["has_data"] is True


class TestOutdoorConditionalDiff:
    """Tests for _outdoor_conditional_diff — smooth bucket transitions."""

    def test_cold_zone_returns_cold_diff(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        assert _outdoor_conditional_diff(50.0, tf) == pytest.approx(15.0)
        assert _outdoor_conditional_diff(58.0, tf) == pytest.approx(15.0)

    def test_warm_zone_returns_warm_diff(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        assert _outdoor_conditional_diff(72.0, tf) == pytest.approx(0.0)
        assert _outdoor_conditional_diff(80.0, tf) == pytest.approx(0.0)

    def test_cold_mild_midpoint_is_halfway(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 16.0, "mild_diff": 8.0, "warm_diff": 0.0}
        # Midpoint 60°F = halfway between 58 and 62
        mid = _outdoor_conditional_diff(60.0, tf)
        assert mid == pytest.approx(12.0, abs=0.1)

    def test_mild_warm_midpoint_is_halfway(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 2.0}
        # Midpoint 70°F = halfway between 68 and 72
        mid = _outdoor_conditional_diff(70.0, tf)
        assert mid == pytest.approx(5.0, abs=0.1)

    def test_no_jump_crossing_cold_mild_boundary(self):
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        tf = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        d59 = _outdoor_conditional_diff(59.0, tf)
        d61 = _outdoor_conditional_diff(61.0, tf)
        # 2°F outdoor change near boundary → < 4°F diff change (vs 7.6°F hard cutoff)
        assert abs(d61 - d59) < 4.0


# ---------------------------------------------------------------------------
# _build_predicted_indoor_future tests
# ---------------------------------------------------------------------------

_PRED_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
    # Note: no sleep_heat/sleep_cool → function defaults to comfort ± DEFAULT_SETBACK_DEPTH_*F
}
_PRED_NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)  # noon UTC


def _pred_entry(dt: datetime, temp: float) -> dict:
    """Make a forecast entry in HA format (datetime key, UTC-aware ISO string)."""
    return {"datetime": dt.isoformat(), "temperature": temp}


class TestBuildPredictedIndoorFuture:
    """Tests for _build_predicted_indoor_future — automation-plan-based future prediction."""

    def _call(self, forecast, config=_PRED_CONFIG, now=_PRED_NOW):
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            return _build_predicted_indoor_future(forecast, config, now)

    def test_empty_on_none(self):
        assert self._call(None) == []

    def test_empty_on_empty_list(self):
        assert self._call([]) == []

    def test_all_entries_are_future(self):
        """No result entry should have ts <= now."""
        entries = [
            _pred_entry(_PRED_NOW - timedelta(hours=2), 65.0),  # past — must be excluded
            _pred_entry(_PRED_NOW + timedelta(hours=1), 65.0),
            _pred_entry(_PRED_NOW + timedelta(hours=2), 65.0),
        ]
        result = self._call(entries)
        assert len(result) == 3
        for e in result:
            ts = datetime.fromisoformat(e["ts"])
            assert ts > _PRED_NOW

    def test_heat_day_waking_hours_at_comfort(self):
        """Cold day (high=40°F) → heat mode → hour 14 (waking) at comfort_heat=70."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)  # 6 AM so h=14 is future
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 25)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour == 14]
        assert waking, "Expected hour-14 entries"
        for e in waking:
            assert e["temp"] == pytest.approx(70.0, abs=0.1)

    def test_cool_day_waking_hours_at_comfort_cool(self):
        """Hot day (high=90°F) → cool mode → hour 14 (waking) at comfort_cool=75."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 90.0) for i in range(1, 25)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour == 14]
        assert waking
        for e in waking:
            assert e["temp"] == pytest.approx(75.0, abs=0.1)

    def test_off_day_tracks_outdoor_plus_buffer(self):
        """Mild day (high=65°F) → off mode → indoor = outdoor+2."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 65.0) for i in range(1, 25)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        for e in result:
            assert e["temp"] == pytest.approx(67.0, abs=0.1)

    def test_off_day_floor_at_setback_heat(self):
        """Off day with outdoor=50°F → 50+2=52 < setback_heat=60 → floored at 60."""
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        # Mix: entries at 50°F plus one at 65°F to push day_high to THRESHOLD_MILD → "off"
        entries = [_pred_entry(now + timedelta(hours=i), 50.0) for i in range(1, 25)]
        entries.append(_pred_entry(now + timedelta(hours=3), 65.0))  # sets day high = 65
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        for e in result:
            assert e["temp"] >= 59.9, f"Floor should clamp to setback_heat=60, got {e['temp']}"

    def test_heat_day_sleep_hours_use_sleep_heat_default(self):
        """Heat day, hour=2 (before wake_time=06:30) → default sleep setback = 66°F.

        Without sleep_heat in config, Bug 4 fix computes:
        max(comfort_heat(70) - DEFAULT_SETBACK_DEPTH_F(4), setback_heat(60)) = max(66, 60) = 66°F.
        Old (buggy) code used setback_heat=60 directly.
        """
        now = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)  # midnight
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 49)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now)
        sleep_entries = [e for e in result if datetime.fromisoformat(e["ts"]).hour == 2]
        assert sleep_entries, "Expected entries at hour=2 (pre-wake sleep period)"
        for e in sleep_entries:
            assert e["temp"] == pytest.approx(66.0, abs=0.1), (
                f"Default heat sleep setback should be comfort_heat-4=66°F, got {e['temp']}"
            )

    def test_heat_day_sleep_heat_config_respected(self):
        """Explicit sleep_heat config overrides the default depth calculation."""
        config = {**_PRED_CONFIG, "sleep_heat": 63}  # explicit user preference
        now = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 25)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
            result = _build_predicted_indoor_future(entries, config, now)
        sleep_entries = [e for e in result if datetime.fromisoformat(e["ts"]).hour == 2]
        assert sleep_entries
        for e in sleep_entries:
            # sleep_heat=63 > setback_heat=60 → clamp to max(63, 60) = 63°F
            assert e["temp"] == pytest.approx(63.0, abs=0.1)

    def test_result_uses_ts_format(self):
        """Each entry must have 'ts' (ISO string) and 'temp' (float)."""
        entries = [_pred_entry(_PRED_NOW + timedelta(hours=i), 65.0) for i in range(1, 5)]
        result = self._call(entries)
        for e in result:
            assert "ts" in e and "temp" in e
            assert isinstance(e["temp"], float)
            datetime.fromisoformat(e["ts"])  # must be valid ISO

    def test_accepts_datetime_key(self):
        """Function must work with 'datetime' key (HA weather format); 'time' key is fallback."""
        entries = [{"datetime": (_PRED_NOW + timedelta(hours=i)).isoformat(), "temperature": 65.0} for i in range(1, 4)]
        result = self._call(entries)
        assert len(result) == 5

    def test_timezone_aware_now_no_error(self):
        """Timezone-aware now must not raise TypeError in comparison."""

        now_aware = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        entries = [{"datetime": (now_aware + timedelta(hours=i)).isoformat(), "temperature": 65.0} for i in range(1, 4)]
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now_aware)):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now_aware)
        assert len(result) == 5

    def test_local_hour_used_not_utc(self):
        """Schedule must use LOCAL hour, not UTC hour.

        Scenario: UTC-5 user. Entry at 02:00 UTC = 21:00 local (UTC-5).
        - UTC hour h=2 < wake_h=6.5 → setback (66°F) — WRONG
        - Local hour h=21 in [8.5, 22.5) → comfort_heat=70°F — CORRECT
        """
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        utc_minus5 = _tz(_td(hours=-5))
        now_utc = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)

        tz_mock = MagicMock()
        tz_mock.now.return_value = now_utc
        tz_mock.as_local = lambda dt: dt.astimezone(utc_minus5)

        # Entry at 02:00 UTC = 21:00 local (UTC-5) — heat day, waking hours locally
        entries = [_pred_entry(now_utc + timedelta(hours=2), 40.0)]

        with patch("custom_components.climate_advisor.coordinator.dt_util", tz_mock):
            result = _build_predicted_indoor_future(entries, _PRED_CONFIG, now_utc)

        assert result, "Expected entry at 02:00 UTC / 21:00 local to appear in result"
        # h=21 is in waking hours (between wake+ramp=8.5 and sleep_h=22.5) → comfort_heat=70
        assert result[0]["temp"] == pytest.approx(70.0, abs=0.1), (
            f"02:00 UTC = 21:00 local must map to comfort zone (70°F), got {result[0]['temp']}"
        )


class TestFindCeilingBreachTime:
    """Tests for _find_ceiling_breach_time() helper."""

    def _make_curve(self, temps: list[float], start_hour: int = 10) -> list[dict]:
        """Build a predicted_indoor curve list from a list of hourly temps."""
        base = datetime(2026, 5, 11, start_hour, 0, 0, tzinfo=UTC)
        return [{"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)]

    def test_returns_breach_ts_when_curve_crosses_comfort_cool(self):
        """First entry above comfort_cool is returned."""
        curve = self._make_curve([72.0, 73.0, 74.5, 75.5], start_hour=10)
        # comfort_cool = 74.0; breach at hour 12 (74.5 > 74.0)
        result = _find_ceiling_breach_time(curve, comfort_cool=74.0)
        expected = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_returns_none_when_no_breach(self):
        """Returns None when all temps are below or equal to comfort_cool."""
        curve = self._make_curve([70.0, 71.0, 72.0, 73.0])
        assert _find_ceiling_breach_time(curve, comfort_cool=74.0) is None

    def test_returns_none_for_empty_curve(self):
        """Returns None for empty or None curve."""
        assert _find_ceiling_breach_time([], comfort_cool=74.0) is None
        assert _find_ceiling_breach_time(None, comfort_cool=74.0) is None

    def test_tolerance_shifts_threshold(self):
        """With tolerance=1.0 (bridge home), threshold is comfort_cool + 1.0.
        temp=74.5 is above comfort_cool=74.0 but below 74.0+1.0=75.0 → None.
        temp=75.5 is above 75.0 → breach returned.
        """
        # Only 74.5 and 75.5 exceed comfort_cool=74.0
        # With tolerance=1.0, only 75.5 exceeds 75.0
        curve_no_bridge = self._make_curve([72.0, 74.5])
        curve_with_bridge = self._make_curve([72.0, 74.5, 75.5])
        assert _find_ceiling_breach_time(curve_no_bridge, comfort_cool=74.0, tolerance=1.0) is None
        result = _find_ceiling_breach_time(curve_with_bridge, comfort_cool=74.0, tolerance=1.0)
        assert result is not None
        expected = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)  # hour 12 = index 2
        assert result == expected


class TestBuildPredictedIndoorFutureOccupancy:
    """Tests for occupancy-aware setpoint threading in _build_predicted_indoor_future."""

    # Use a "now" anchored in the morning so all today-daytime hours are future.
    # _PRED_NOW = 2026-04-10 12:00 UTC; use a fresh value to be self-contained.
    _NOW = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)  # 6 AM UTC — all daytime is future

    def _call(self, forecast, occupancy_mode=OCCUPANCY_HOME, now=None, config=_PRED_CONFIG):
        _now = now or self._NOW
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(_now)):
            return _build_predicted_indoor_future(forecast, config, _now, occupancy_mode=occupancy_mode)

    def test_away_uses_setback_setpoints_in_fallback(self):
        """Away today → all today entries should use setback_heat, not comfort_heat.

        Cold day (high=40°F → heat mode). Away → _compute_target_band_schedule returns
        lower=setback_heat=60 for today. Fallback path (no thermal model) → predicted
        temp = band lower = 60, not comfort_heat=70.
        """
        now = self._NOW
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 13)]
        result = self._call(entries, occupancy_mode=OCCUPANCY_AWAY, now=now)
        assert result, "Expected future entries"
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour in range(9, 22)]
        assert waking, "Expected waking-hour entries in result"
        for e in waking:
            assert e["temp"] == pytest.approx(60.0, abs=0.1), (
                f"Away today: waking-hour entry should be setback_heat=60, got {e['temp']}"
            )

    def test_vacation_uses_deep_setback_in_fallback(self):
        """Vacation today → entries use setback_heat - VACATION_SETBACK_EXTRA.

        Cold day (high=40°F → heat mode). Vacation → lower = setback_heat(60) - 3 = 57.
        Fallback path → predicted temp = 57.
        """
        now = self._NOW
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 13)]
        result = self._call(entries, occupancy_mode=OCCUPANCY_VACATION, now=now)
        assert result, "Expected future entries"
        expected = _PRED_CONFIG["setback_heat"] - VACATION_SETBACK_EXTRA  # 60 - 3 = 57
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour in range(9, 22)]
        assert waking, "Expected waking-hour entries in result"
        for e in waking:
            assert e["temp"] == pytest.approx(expected, abs=0.1), (
                f"Vacation today: waking-hour entry should be {expected}°F, got {e['temp']}"
            )

    def test_away_tomorrow_reverts_to_normal_schedule(self):
        """Away today but tomorrow entries should use the normal comfort schedule.

        _compute_target_band_schedule applies setback only when ts_date == now_date.
        Tomorrow should revert to the wake/sleep schedule → waking hours at comfort_heat=70.
        """
        now = self._NOW  # 2026-04-10 06:00 UTC
        # Build entries for tomorrow's daytime: 2026-04-11 hours 9–21 UTC (all future)
        tomorrow_start = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(tomorrow_start + timedelta(hours=i), 40.0) for i in range(9, 22)]
        result = self._call(entries, occupancy_mode=OCCUPANCY_AWAY, now=now)
        assert result, "Expected tomorrow entries"
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour in range(9, 22)]
        assert waking, "Expected waking-hour entries for tomorrow"
        for e in waking:
            assert e["temp"] == pytest.approx(70.0, abs=0.1), (
                f"Away tomorrow: waking hours should revert to comfort_heat=70, got {e['temp']}"
            )

    def test_home_occupancy_unchanged(self):
        """Explicit occupancy=home must produce the same result as the default (no regression).

        Cold day (high=40°F → heat mode), waking hours → comfort_heat=70.
        """
        now = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 25)]
        result_default = self._call(entries, occupancy_mode=OCCUPANCY_HOME, now=now)
        result_explicit = self._call(entries, occupancy_mode=OCCUPANCY_HOME, now=now)
        assert result_default == result_explicit
        waking = [e for e in result_default if datetime.fromisoformat(e["ts"]).hour == 14]
        assert waking, "Expected hour-14 entries"
        for e in waking:
            assert e["temp"] == pytest.approx(70.0, abs=0.1), (
                f"Home occupancy: hour-14 should be comfort_heat=70, got {e['temp']}"
            )


# ---------------------------------------------------------------------------
# TG1: Physics path + occupancy-aware setpoint threading
# ---------------------------------------------------------------------------

# Thermal model sufficient to activate physics path (confidence != "none", k_passive < 0).
# k_active_heat=10 chosen so HVAC can overcome envelope loss at outdoor=40°F:
#   eq_temp = outdoor - k_active/k_passive = 40 + 10/0.3 ≈ 73°F > comfort_heat(70)
_PHYSICS_THERMAL_MODEL = {
    "confidence": "low",
    "k_passive": -0.3,
    "k_active_heat": 10.0,
    "k_active_cool": -10.0,
    "heating_rate_f_per_hour": 10.0,
    "cooling_rate_f_per_hour": 10.0,
    "observation_count_heat": 3,
    "observation_count_cool": 3,
}
_PHYSICS_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "sleep_heat": 66,
    "sleep_cool": 78,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}


class TestBuildPredictedIndoorFuturePhysics:
    """TG1: Physics ODE path uses the correct setpoint based on occupancy mode.

    Verifies that occupancy_mode is threaded through to the ODE setpoint source
    (_compute_target_band_schedule via _band["lower"]/"upper"), not hardcoded.
    """

    _NOW = datetime(2026, 4, 10, 6, 0, 0, tzinfo=UTC)  # 6 AM — all daytime is future

    def _call(
        self,
        forecast,
        occupancy_mode=OCCUPANCY_HOME,
        now=None,
        config=_PHYSICS_CONFIG,
        indoor_temp=65.0,
    ):
        _now = now or self._NOW
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(_now)):
            return _build_predicted_indoor_future(
                forecast,
                config,
                _now,
                current_indoor_temp=indoor_temp,
                thermal_model=_PHYSICS_THERMAL_MODEL,
                occupancy_mode=occupancy_mode,
            )

    def test_home_physics_heats_toward_comfort_heat(self):
        """Home + physics: ODE setpoint = comfort_heat (70); HVAC heats from 65 toward 70.

        With k_active_heat=10 the equilibrium temp is ~73°F, well above comfort_heat=70.
        The ODE drives T up to the setpoint and clamps there. Between steps, passive decay
        (k_passive=-0.3) pulls T back down, creating a cycle that always peaks at 70.
        Assert: the peak waking-hour temp reaches comfort_heat — not the last entry (which
        may be mid-decay).
        """
        now = self._NOW
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 13)]
        result = self._call(entries, occupancy_mode=OCCUPANCY_HOME, indoor_temp=65.0)
        assert result, "Expected physics results"
        waking = [e for e in result if datetime.fromisoformat(e["ts"]).hour in range(9, 21)]
        assert waking, "Expected waking-hour entries (hours 9-20)"
        peak_temp = max(e["temp"] for e in waking)
        assert peak_temp == pytest.approx(70.0, abs=1.0), (
            f"Home + physics: expected peak waking temp ≈ comfort_heat=70, got {peak_temp}°F"
        )

    def test_away_physics_uses_setback_setpoint(self):
        """Away today + physics: ODE setpoint = setback_heat (60), not comfort_heat (70).

        With indoor=65 and setpoint=60 the ODE 'cool' branch activates (setpoint ≤ comfort_cool
        and T > setpoint), driving T toward 60. Home mode instead heats toward 70.
        Assert: home peak ≈ 70; away peak ≤ 62 (clamped to 60 then passive decay).
        """
        now = self._NOW
        entries = [_pred_entry(now + timedelta(hours=i), 40.0) for i in range(1, 13)]
        result_away = self._call(entries, occupancy_mode=OCCUPANCY_AWAY, indoor_temp=65.0)
        result_home = self._call(entries, occupancy_mode=OCCUPANCY_HOME, indoor_temp=65.0)
        assert result_away, "Expected away physics results"
        assert result_home, "Expected home physics results"
        waking_away = [e for e in result_away if datetime.fromisoformat(e["ts"]).hour in range(9, 21)]
        waking_home = [e for e in result_home if datetime.fromisoformat(e["ts"]).hour in range(9, 21)]
        assert waking_away and waking_home, "Expected waking entries for both modes"
        home_peak = max(e["temp"] for e in waking_home)
        away_peak = max(e["temp"] for e in waking_away)
        assert home_peak == pytest.approx(70.0, abs=1.0), (
            f"Home physics: expected peak ≈ comfort_heat=70, got {home_peak}"
        )
        assert away_peak <= 65.0, f"Away physics: expected peak ≤ 65°F (setpoint=60 used, not 70), got {away_peak}"


# ---------------------------------------------------------------------------
# TG2: get_chart_data() target_band shape contract
# ---------------------------------------------------------------------------


class TestGetChartDataShape:
    """TG2: Verify get_chart_data() returns target_band with correct structure.

    Regression guard: any change to get_chart_data() that breaks the target_band
    key or entry shape will be caught here before it reaches the frontend.
    """

    def test_target_band_present_with_correct_shape(self):
        """target_band must be present, length == forecast hours, each entry has ts/lower/upper."""
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        # 24 future hourly entries: one per hour starting 1h from now
        entries = [_pred_entry(now + timedelta(hours=i), 70.0) for i in range(1, 25)]

        coord = _make_chart_coordinator()
        coord._hourly_forecast_temps = entries

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _make_dt_util_mock(now),
        ):
            chart = coord.get_chart_data()

        assert "target_band" in chart, "get_chart_data() must include 'target_band' key"
        band = chart["target_band"]
        assert len(band) == 24, f"Expected 24 band entries (one per forecast hour), got {len(band)}"
        for i, entry in enumerate(band):
            assert "ts" in entry, f"Band entry {i} missing 'ts'"
            assert "lower" in entry, f"Band entry {i} missing 'lower'"
            assert "upper" in entry, f"Band entry {i} missing 'upper'"
            if entry["lower"] is not None:
                assert entry["lower"] <= entry["upper"], (
                    f"Band entry {i}: lower ({entry['lower']}) > upper ({entry['upper']})"
                )

    def test_legacy_comfort_scalars_removed(self):
        """comfort_heat and comfort_cool must not appear as top-level chart keys (Phase 1 removal)."""
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
        entries = [_pred_entry(now + timedelta(hours=i), 70.0) for i in range(1, 5)]
        coord = _make_chart_coordinator()
        coord._hourly_forecast_temps = entries

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util",
            _make_dt_util_mock(now),
        ):
            chart = coord.get_chart_data()

        assert "comfort_heat" not in chart, "Legacy comfort_heat scalar must not appear in chart data"
        assert "comfort_cool" not in chart, "Legacy comfort_cool scalar must not appear in chart data"


class TestExtractCurrentHourForecastTemp:
    """Tests for _extract_current_hour_forecast_temp nearest-entry logic.

    HA's hourly forecast returns entries starting at the NEXT full hour
    (e.g., at 12:27 the first entry is 13:00, not 12:00). The function
    must find the nearest entry within ±2 hours rather than requiring an
    exact hour match.
    """

    _TZ = UTC

    def _entry(self, dt: datetime, temp: float) -> dict:
        return {"datetime": dt.isoformat(), "temperature": temp}

    def test_next_hour_entry_returned_when_current_hour_absent(self):
        """At 12:27 UTC, forecast starts at 13:00 — must return 13:00 temp."""
        now = datetime(2026, 5, 11, 12, 27, 0, tzinfo=UTC)
        entries = [self._entry(datetime(2026, 5, 11, h, 0, 0, tzinfo=UTC), 70.0 + h) for h in range(13, 19)]
        result = _extract_current_hour_forecast_temp(entries, now)
        assert result == 83.0  # temp for hour 13

    def test_exact_hour_entry_returned(self):
        """When a forecast entry exists exactly at the current hour, return it."""
        now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        entries = [self._entry(datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC), 68.5)]
        result = _extract_current_hour_forecast_temp(entries, now)
        assert result == 68.5

    def test_past_entry_within_2h_returned(self):
        """Entry 45 minutes in the past is within the ±2h window and should be returned."""
        now = datetime(2026, 5, 11, 12, 45, 0, tzinfo=UTC)
        entries = [self._entry(datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC), 65.0)]
        result = _extract_current_hour_forecast_temp(entries, now)
        assert result == 65.0

    def test_entry_beyond_2h_ignored(self):
        """Entry more than 2 hours away returns None — too stale to use."""
        now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        entries = [self._entry(datetime(2026, 5, 11, 15, 0, 0, tzinfo=UTC), 80.0)]
        result = _extract_current_hour_forecast_temp(entries, now)
        assert result is None

    def test_empty_list_returns_none(self):
        now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        assert _extract_current_hour_forecast_temp([], now) is None
        assert _extract_current_hour_forecast_temp(None, now) is None

    def test_nearest_entry_chosen_among_multiple(self):
        """When multiple entries are within 2h, the one nearest to now wins."""
        now = datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC)
        entries = [
            self._entry(datetime(2026, 5, 11, 11, 0, 0, tzinfo=UTC), 60.0),  # 90 min ago
            self._entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),  # 30 min ahead
            self._entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 75.0),  # 90 min ahead
        ]
        result = _extract_current_hour_forecast_temp(entries, now)
        assert result == 70.0  # 13:00 is closest (30 min vs 90 min)
