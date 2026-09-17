"""Tests for _get_forecast() date-keyed dict matching (Issues #143, #190).

Covers:
1. API starts from tomorrow — forecast array has no today entry
2. Normal full forecast — both today and tomorrow present
3. Core regression guard — today_high != tomorrow_high when API starts from tomorrow
4. Empty forecast — all temperatures fall back to current_outdoor
5. UTC midnight datetimes — entries timestamped at UTC midnight matched by raw date
   against local today (not UTC date, not local-converted date) — fixes #190
6. Evening UTC rollover — 7pm PDT where UTC date is already tomorrow; tomorrow's
   forecast must still show local tomorrow, not day-after-tomorrow
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.temperature import to_fahrenheit  # noqa: E402
from tests.helpers.date_boundary_fixtures import DATE_BOUNDARY_CASES  # noqa: E402

_TODAY = date(2026, 5, 15)
_TOMORROW = _TODAY + timedelta(days=1)
_CURRENT_OUTDOOR = 65.0

# PDT = UTC-7
_PDT = timezone(-timedelta(hours=7))

# dt_util.now() mock — 6am PDT on today (UTC date == local date, no rollover)
_NOW_LOCAL = datetime(2026, 5, 15, 6, 0, 0, tzinfo=_PDT)

# dt_util.now() mock for the evening rollover scenario — 7pm PDT May 15
# UTC equivalent: 2am May 16 — UTC date is already May 16, but local date is still May 15
_NOW_LOCAL_EVENING = datetime(2026, 5, 15, 19, 0, 0, tzinfo=_PDT)


def _make_entry(d: date, temp: float, *, utc_midnight: bool = False) -> dict:
    """Forecast entry for the given date.

    utc_midnight=True: timestamps at UTC midnight (e.g. 2026-05-16T00:00:00+00:00).
    The raw date portion is the API's intended forecast date. In PDT (UTC-7) this
    shifts to 5pm the previous local day if converted — which is why we use raw date.

    utc_midnight=False (default): timestamps at local noon with -07:00 offset.
    Raw date == local date in this case.
    """
    dt_str = f"{d.isoformat()}T00:00:00+00:00" if utc_midnight else f"{d.isoformat()}T12:00:00-07:00"
    return {
        "datetime": dt_str,
        "temperature": temp,
        "templow": temp - 15,
    }


def _make_coordinator_stub(forecast_data: list, *, temp_unit: str = "fahrenheit") -> MagicMock:
    """Build a minimal coordinator-like stub for testing _get_forecast().

    Uses the types.MethodType pattern consistent with test_coordinator.py.
    ``temp_unit`` selects "fahrenheit" (default) or "celsius" — Issue #903 added
    Celsius coverage since the reported crash only manifested in Celsius-configured
    installs (to_fahrenheit()'s `value * 9.0 / 5.0` with value=None raises there;
    Fahrenheit mode's plain `float(None)` produces a different, also-crashing error).
    """
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = MagicMock()
    coord.hass = MagicMock()

    # Weather state: available with a temperature attribute
    weather_state = MagicMock()
    weather_state.state = "sunny"
    weather_state.attributes = {
        "temperature": _CURRENT_OUTDOOR,
        "temperature_unit": "°F",
    }
    coord.hass.states.get = MagicMock(return_value=weather_state)

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "temp_unit": temp_unit,
        "learning_enabled": False,  # skip bias correction
    }

    coord._outdoor_temp_history = []

    # Bind the real _get_forecast method so we test the actual code
    coord._get_forecast = types.MethodType(ClimateAdvisorCoordinator._get_forecast, coord)
    coord._get_forecast_data = AsyncMock(return_value=forecast_data)

    # Stub helpers that _get_forecast delegates to
    coord._get_outdoor_temp = MagicMock(return_value=_CURRENT_OUTDOOR)
    coord._get_indoor_temp = MagicMock(return_value=72.0)

    # learning stub for bias check
    coord.learning = MagicMock()
    coord.learning.get_weather_bias = MagicMock(return_value={"confidence": "none"})

    return coord


def _run_get_forecast(coord, *, now_local: datetime = _NOW_LOCAL) -> object:
    """Run _get_forecast() with dt_util.now patched to the given local datetime."""

    async def run():
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now",
            return_value=now_local,
        ):
            return await coord._get_forecast()

    return asyncio.run(run())


class TestForecastDateMatching:
    """Tests for the date-keyed dict matching in _get_forecast()."""

    def test_api_starts_from_tomorrow_returns_correct_tomorrow(self, tmp_path: Path):
        """When API omits today, tomorrow_high comes from tomorrow's entry not today's."""
        forecast = [
            _make_entry(_TOMORROW, 72.0),
            _make_entry(_TOMORROW + timedelta(days=1), 68.0),
        ]
        coord = _make_coordinator_stub(forecast)
        result = _run_get_forecast(coord)

        assert result is not None
        # Today has no forecast entry — falls back to current_outdoor
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)
        # Tomorrow correctly reads from its own entry, NOT from forecast[0] via blind fallback
        assert result.tomorrow_high == pytest.approx(72.0)

    def test_normal_forecast_both_days_found(self, tmp_path: Path):
        """Normal forecast with both today and tomorrow entries — both correctly extracted."""
        forecast = [
            _make_entry(_TODAY, 78.0),
            _make_entry(_TOMORROW, 72.0),
            _make_entry(_TOMORROW + timedelta(days=1), 68.0),
        ]
        coord = _make_coordinator_stub(forecast)
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(78.0)
        assert result.tomorrow_high == pytest.approx(72.0)

    def test_no_fallback_collision_when_api_starts_from_tomorrow(self, tmp_path: Path):
        """Core regression: today_high must NOT equal tomorrow_high when API starts from tomorrow."""
        # Pre-fix: both today_fc and tomorrow_fc pointed to forecast[0] (72.0 each).
        # Post-fix: today_fc = None → current_outdoor (65.0); tomorrow_fc = forecast[0] (72.0).
        forecast = [
            _make_entry(_TOMORROW, 72.0),
            _make_entry(_TOMORROW + timedelta(days=1), 65.0),
        ]
        coord = _make_coordinator_stub(forecast)
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high != pytest.approx(72.0), (
            "REGRESSION: today_high equals tomorrow's forecast value — blind-index fallback collision not fixed"
        )
        assert result.tomorrow_high == pytest.approx(72.0)
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)

    def test_empty_forecast_returns_current_outdoor(self, tmp_path: Path):
        """Empty forecast array — all temperatures fall back to current_outdoor."""
        coord = _make_coordinator_stub([])
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)
        assert result.tomorrow_high == pytest.approx(_CURRENT_OUTDOOR)

    def test_empty_forecast_celsius_fallback_not_double_converted(self, tmp_path: Path):
        """Empty forecast in a Celsius-configured install (e.g. right after HA restart,
        before the weather integration has forecast data yet) must fall back to
        current_outdoor as-is — current_outdoor is already internal Fahrenheit and must
        not be run through to_fahrenheit() a second time. Regression test for a sibling
        of the Issue #903 crash: the fallback path itself was never unit-safe."""
        coord = _make_coordinator_stub([], temp_unit="celsius")
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)
        assert result.today_low == pytest.approx(_CURRENT_OUTDOOR - 15)
        assert result.tomorrow_high == pytest.approx(_CURRENT_OUTDOOR)
        assert result.tomorrow_low == pytest.approx(_CURRENT_OUTDOOR - 15)

    def test_utc_midnight_entries_matched_by_raw_date(self, tmp_path: Path):
        """UTC midnight timestamps are matched by their raw date, not local-converted date.

        Weather APIs like Open-Meteo return entries at UTC midnight. The raw date
        portion (2026-05-16 in 2026-05-16T00:00:00+00:00) is the API's intended
        forecast date. This is compared directly against the local calendar date.

        If we converted to local PDT first: 2026-05-16T00:00:00Z → 2026-05-15T17:00 PDT
        → local date 2026-05-15 (today) — wrong, this is tomorrow's forecast.

        Raw date: 2026-05-16 compared against local today 2026-05-15 → tomorrow. ✓
        """
        forecast = [
            _make_entry(_TODAY, 72.0, utc_midnight=True),
            _make_entry(_TOMORROW, 79.0, utc_midnight=True),
            _make_entry(_TOMORROW + timedelta(days=1), 68.0, utc_midnight=True),
        ]
        coord = _make_coordinator_stub(forecast)
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(72.0), "UTC midnight today entry must match today via raw date"
        assert result.tomorrow_high == pytest.approx(79.0), (
            "UTC midnight tomorrow entry must match tomorrow via raw date"
        )

    def test_evening_utc_rollover_no_off_by_one(self, tmp_path: Path):
        """Evening scenario: 7pm PDT May 15 — UTC has rolled to May 16 but local is still May 15.

        Issue #190: using dt_util.utcnow() at this time returns date May 16, causing
        the May 16 forecast entry (local tomorrow) to be labeled 'today' and the
        May 17 entry (local day-after-tomorrow) to be labeled 'tomorrow'.

        Fix: dt_util.now().date() = May 15 (local). Raw date May 16 = local tomorrow. ✓
        """
        # UTC midnight entries: the production format
        forecast = [
            _make_entry(_TODAY, 72.0, utc_midnight=True),  # May 15 — local today
            _make_entry(_TOMORROW, 91.0, utc_midnight=True),  # May 16 — local tomorrow
            _make_entry(_TOMORROW + timedelta(days=1), 68.0, utc_midnight=True),  # May 17
        ]
        coord = _make_coordinator_stub(forecast)
        # Simulate 7pm PDT May 15 (UTC = 2am May 16 — UTC date already rolled over)
        result = _run_get_forecast(coord, now_local=_NOW_LOCAL_EVENING)

        assert result is not None
        assert result.today_high == pytest.approx(72.0), (
            "REGRESSION #190: today_high should be May 15 entry (72°F), not May 16 (91°F)"
        )
        assert result.tomorrow_high == pytest.approx(91.0), (
            "REGRESSION #190: tomorrow_high should be May 16 entry (91°F), not May 17 (68°F)"
        )


class TestForecastDateMatchingBoundaryFixtures:
    """Retrofit (Issue #906): _get_forecast()'s raw-date matching, re-exercised
    against the shared DST/rollover/calendar-boundary hazard set in
    tests/helpers/date_boundary_fixtures.py — so a hazard added to that shared
    list automatically re-exercises this older function too, not just the two
    new Issue #906 functions.
    """

    @pytest.mark.parametrize("case", DATE_BOUNDARY_CASES, ids=lambda c: c.label)
    def test_today_and_tomorrow_matched_by_raw_date(self, case, tmp_path: Path):
        today_date = case.now.date()
        tomorrow_date = today_date + timedelta(days=1)
        forecast = [
            _make_entry(today_date, 72.0, utc_midnight=True),
            _make_entry(tomorrow_date, 79.0, utc_midnight=True),
        ]
        coord = _make_coordinator_stub(forecast)
        result = _run_get_forecast(coord, now_local=case.now)

        assert result is not None
        assert result.today_high == pytest.approx(72.0), (
            f"{case.label}: today_high should match the {today_date} entry via raw date"
        )
        assert result.tomorrow_high == pytest.approx(79.0), (
            f"{case.label}: tomorrow_high should match the {tomorrow_date} entry via raw date"
        )


class TestForecastNullFieldHandling:
    """Regression tests for Issue #903: a forecast entry with a field PRESENT but
    ``None`` (a real weather-API pattern, e.g. today's ``templow`` once today's low
    has already passed) must fall back gracefully, not crash.

    Pre-fix, ``dict.get(key, default)`` only substitutes ``default`` when ``key`` is
    ABSENT — a present-but-None value passed straight through to ``to_fahrenheit()``,
    which (post Phase-1 None-guard) raises ``ValueError`` instead of the old cryptic
    ``TypeError``. Either way it crashed the entire coordinator update cycle. At
    least one of these tests would have raised before the ``_first_non_none()`` fix
    in ``_get_forecast()``; all must pass afterward.
    """

    def _entry_with_null(self, d: date, *, null_field: str, other_value: float) -> dict:
        """Build a forecast entry where ``null_field`` is present but None, and the
        other of temperature/templow carries ``other_value``."""
        entry = {
            "datetime": f"{d.isoformat()}T12:00:00-07:00",
            "temperature": other_value,
            "templow": other_value - 15,
        }
        entry[null_field] = None
        return entry

    def test_null_temperature_present_fahrenheit_falls_back(self, tmp_path: Path):
        """today.temperature present-but-None (Fahrenheit mode) → falls back to
        current_outdoor, does not crash."""
        forecast = [self._entry_with_null(_TODAY, null_field="temperature", other_value=50.0)]
        coord = _make_coordinator_stub(forecast, temp_unit="fahrenheit")
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)
        # templow was a real (non-null) value and must still be honored
        assert result.today_low == pytest.approx(35.0)

    def test_null_templow_present_fahrenheit_falls_back(self, tmp_path: Path):
        """today.templow present-but-None (Fahrenheit mode) → falls back to
        current_outdoor - 15, does not crash. This is the exact reported shape:
        today's low goes null once today's actual low has already passed."""
        forecast = [self._entry_with_null(_TODAY, null_field="templow", other_value=78.0)]
        coord = _make_coordinator_stub(forecast, temp_unit="fahrenheit")
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(78.0)
        assert result.today_low == pytest.approx(_CURRENT_OUTDOOR - 15)

    def test_null_temperature_present_celsius_falls_back_no_crash(self, tmp_path: Path):
        """today.temperature present-but-None (Celsius mode) is the reported crash
        shape: to_fahrenheit(None, 'celsius') raised 'NoneType * float'. Must not
        crash and must still convert the real (non-null) templow value correctly."""
        forecast = [self._entry_with_null(_TODAY, null_field="temperature", other_value=10.0)]
        coord = _make_coordinator_stub(forecast, temp_unit="celsius")
        result = _run_get_forecast(coord)

        assert result is not None
        # today_high falls back to current_outdoor, which is ALREADY internal
        # Fahrenheit (from _get_outdoor_temp()) — it must not be converted a second
        # time. (A prior version of this fix double-converted fallback values here;
        # this assertion is the regression test for that sibling bug.)
        assert result.today_high == pytest.approx(_CURRENT_OUTDOOR)
        # today_low (10.0 - 15 = -5.0 °C) is a real forecast-sourced value and must
        # still convert correctly.
        assert result.today_low == pytest.approx(to_fahrenheit(-5.0, "celsius"))

    def test_null_templow_present_celsius_falls_back_no_crash(self, tmp_path: Path):
        """today.templow present-but-None (Celsius mode) → falls back, no crash;
        the real (non-null) temperature value still converts correctly."""
        forecast = [self._entry_with_null(_TODAY, null_field="templow", other_value=25.0)]
        coord = _make_coordinator_stub(forecast, temp_unit="celsius")
        result = _run_get_forecast(coord)

        assert result is not None
        assert result.today_high == pytest.approx(to_fahrenheit(25.0, "celsius"))
        # today_low falls back to current_outdoor - 15, already internal Fahrenheit —
        # must not be converted a second time (see the sibling test above).
        assert result.today_low == pytest.approx(_CURRENT_OUTDOOR - 15)

    def test_null_field_logs_warning(self, tmp_path: Path, caplog):
        """A present-but-null field logs a WARNING distinct from the missing-entry
        warning, surfacing the underlying weather-provider data-quality issue."""
        import logging

        forecast = [self._entry_with_null(_TODAY, null_field="templow", other_value=78.0)]
        coord = _make_coordinator_stub(forecast, temp_unit="fahrenheit")
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.coordinator"):
            result = _run_get_forecast(coord)

        assert result is not None
        assert any("null templow field" in rec.message for rec in caplog.records)
