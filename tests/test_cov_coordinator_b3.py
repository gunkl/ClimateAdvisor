"""Coverage for the coordinator's chart forecast helpers (Silver test-coverage, #1644).

What the person at home gets from this code: the chart's outdoor forecast, humidity and feels-like lines reach past
the weather service's short hourly window, built from its daily highs and lows, and a malformed forecast entry is
dropped rather than breaking the chart. These tests check the entries each helper keeps, fills and skips.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import coordinator as _coord_mod  # noqa: E402


def _hourly(start: datetime, n: int, temp: float = 60.0, **extra) -> list[dict]:
    return [{"datetime": (start + timedelta(hours=i)).isoformat(), "temperature": temp, **extra} for i in range(n)]


def _daily(d: date, high: float | None, low: float | None) -> dict:
    e: dict = {"datetime": f"{d.isoformat()}T00:00:00+00:00"}
    if high is not None:
        e["temperature"] = high
    if low is not None:
        e["templow"] = low
    return e


class TestDailyEntryDate:
    def test_reads_raw_date_or_none(self):
        assert _coord_mod._daily_entry_date({"datetime": "2026-01-03T00:00:00+00:00"}) == date(2026, 1, 3)
        assert _coord_mod._daily_entry_date({"datetime": "nope"}) is None
        assert _coord_mod._daily_entry_date("not a dict") is None


class TestUnexpiredHourlyForecast:
    def test_drops_bad_and_old_and_reads_naive_as_local(self):
        now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        entries = [
            {"datetime": "garbage", "temperature": 50},
            {"datetime": "2026-01-02T13:00:00", "temperature": 51},  # naive: read in now's zone
            {"datetime": "2026-01-02T09:00:00+00:00", "temperature": 52},  # over an hour old
        ]
        kept = _coord_mod._unexpired_hourly_forecast(entries, now)
        assert [e["temperature"] for e in kept] == [51]

    def test_naive_now_compares_naive(self):
        now = datetime(2026, 1, 2, 12, 0)
        entries = [{"datetime": "2026-01-02T12:30:00+00:00", "temperature": 50}]
        assert _coord_mod._unexpired_hourly_forecast(entries, now) == entries
        assert _coord_mod._unexpired_hourly_forecast([], now) == []


class TestExtendedHourlyForecast:
    _NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def test_too_few_daily_entries_keeps_real_only(self):
        hourly = _hourly(self._NOW, 3) + [{"temperature": 1}, {"datetime": "bad", "temperature": 1}]
        daily = [{"temperature": 60}, {"datetime": "bad"}, _daily(date(2026, 1, 3), 60, 40)]
        out = _coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")
        assert out == hourly[:3]

    def test_days_without_high_low_are_skipped(self):
        """The boundary day and one later day lack a low: only the complete day is synthesized."""
        hourly = _hourly(self._NOW + timedelta(hours=24), 20)  # Jan 2, 00:00 .. 19:00
        daily = [
            _daily(date(2026, 1, 2), 60, None),
            _daily(date(2026, 1, 3), 61, None),
            _daily(date(2026, 1, 4), 62, 40),
        ]
        out = _coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")
        synthetic = [e for e in out if e.get("synthetic")]
        assert len(synthetic) == 24
        assert all(e["datetime"].startswith("2026-01-04") for e in synthetic)
        assert out[: len(hourly)] == hourly

    def test_hourly_ending_at_23h_needs_no_partial_fill(self):
        hourly = _hourly(self._NOW, 24)  # Jan 1, 00:00 .. 23:00
        daily = [_daily(date(2026, 1, 1), 60, 40), _daily(date(2026, 1, 2), 62, 41)]
        out = _coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")
        synthetic = [e for e in out if e.get("synthetic")]
        assert len(synthetic) == 24
        assert {e["datetime"][:10] for e in synthetic} == {"2026-01-02"}

    def test_nothing_beyond_the_hourly_horizon_returns_real(self):
        hourly = _hourly(self._NOW, 48)
        daily = [_daily(date(2026, 1, 1), 60, 40), _daily(date(2026, 1, 2), 60, 40)]
        hourly[-1] = {**hourly[-1], "datetime": "2026-01-02T23:00:00+00:00"}
        out = _coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")
        assert out == hourly


class TestHumidityFeelsLikeForecast:
    def test_skips_malformed_past_and_out_of_range(self):
        now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        future = "2026-01-02T15:00:00+00:00"
        entries = [
            "not a dict",
            {"datetime": "bad", "humidity": 50},
            {"datetime": "2026-01-02T14:00:00", "humidity": 50, "temperature": 60},  # naive vs aware now
            {"datetime": future, "humidity": "wet", "temperature": 60},
            {"datetime": future, "humidity": 150, "temperature": 60},
            {"datetime": future, "humidity": True, "temperature": 60},
            {"datetime": "2026-01-02T16:00:00+00:00", "humidity": 40, "temperature": 85, "wind_speed": 3},
        ]
        humidity, feels = _coord_mod._build_outdoor_humidity_feelslike_forecast(entries, now, "mph")
        assert humidity == [{"ts": "2026-01-02T16:00:00+00:00", "value": 40.0}]
        assert len(feels) == 1 and feels[0]["ts"] == "2026-01-02T16:00:00+00:00"

    def test_no_humidity_anywhere_is_none(self):
        now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
        entries = [{"datetime": "2026-01-02T16:00:00+00:00", "temperature": 60}]
        assert _coord_mod._build_outdoor_humidity_feelslike_forecast(entries, now, "mph") is None
        assert _coord_mod._build_outdoor_humidity_feelslike_forecast([], now, "mph") is None


class TestParseTime:
    def test_good_and_bad_times(self, caplog):
        from datetime import time

        assert _coord_mod._parse_time("06:30") == time(6, 30)
        with caplog.at_level("WARNING", logger="custom_components.climate_advisor.coordinator"):
            assert _coord_mod._parse_time("7") == time(6, 0)
            assert _coord_mod._parse_time(None) == time(6, 0)
        assert caplog.text.count("defaulting to 06:00") == 2


class TestBandEntryAt:
    def test_skips_unstamped_and_unreadable_entries(self):
        now = datetime(2026, 1, 2, 12, 0)
        schedule = [
            {"ts": "", "lower": 1},
            {"ts": "garbage", "lower": 2},
            {"ts": "2026-01-02T08:00:00", "lower": 3},
            {"ts": "2026-01-02T11:00:00", "lower": 4},
            {"ts": "2026-01-02T13:00:00", "lower": 5},
        ]
        assert _coord_mod._band_entry_at(schedule, now)["lower"] == 4
