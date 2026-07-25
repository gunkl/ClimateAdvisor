"""Tests for outdoor-temperature interpolation (Issue #511).

_parse_forecast_entries() is the shared field-extraction helper used by
_extract_current_hour_forecast_temp, _build_future_forecast_outdoor, and the
new _interpolate_hourly_outdoor_temp. _interpolate_hourly_outdoor_temp()
estimates current outdoor temp by linearly interpolating between the two
hourly-forecast entries bracketing `now`, instead of nearest-neighbor-picking
a single (possibly up-to-59-min-stale) entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from custom_components.climate_advisor.coordinator import (
    _interpolate_hourly_outdoor_temp,
    _parse_forecast_entries,
)


def _entry(dt: datetime, temp: float) -> dict:
    return {"datetime": dt.isoformat(), "temperature": temp}


class TestParseForecastEntries:
    """Shared parsing helper: field extraction and validation only."""

    def test_empty_and_none_return_empty_list(self):
        assert _parse_forecast_entries(None) == []
        assert _parse_forecast_entries([]) == []

    def test_extracts_datetime_and_temperature_fields(self):
        dt = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        result = _parse_forecast_entries([_entry(dt, 70.0)])
        assert result == [(dt, 70.0)]

    def test_falls_back_to_time_and_temp_keys(self):
        result = _parse_forecast_entries([{"time": "2026-05-11T12:00:00+00:00", "temp": 65.0}])
        assert result == [(datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC), 65.0)]

    def test_skips_entries_missing_datetime_or_temperature(self):
        entries = [
            {"datetime": "2026-05-11T12:00:00+00:00"},  # no temp
            {"temperature": 70.0},  # no datetime
            {},
        ]
        assert _parse_forecast_entries(entries) == []

    def test_skips_unparseable_datetime(self):
        entries = [{"datetime": "not-a-date", "temperature": 70.0}]
        assert _parse_forecast_entries(entries) == []

    def test_preserves_original_order_not_sorted(self):
        dt1 = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC)
        dt2 = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        result = _parse_forecast_entries([_entry(dt1, 75.0), _entry(dt2, 65.0)])
        assert result == [(dt1, 75.0), (dt2, 65.0)]


class TestInterpolateHourlyOutdoorTemp:
    """Linear interpolation between bracketing hourly-forecast entries."""

    def test_empty_and_none_return_unavailable(self):
        now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        assert _interpolate_hourly_outdoor_temp(None, now) == (None, "unavailable")
        assert _interpolate_hourly_outdoor_temp([], now) == (None, "unavailable")

    def test_all_entries_unparseable_returns_unavailable(self):
        now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
        entries = [{"datetime": "garbage", "temperature": 70.0}]
        assert _interpolate_hourly_outdoor_temp(entries, now) == (None, "unavailable")

    def test_exact_midpoint_interpolation(self):
        """now exactly halfway between two 1h-apart entries → average of the two temps."""
        now = datetime(2026, 5, 11, 13, 30, 0, tzinfo=UTC)
        entries = [
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),
            _entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 74.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 72.0
        assert method == "interpolated"

    def test_quarter_point_interpolation(self):
        """now 15 min into a 1h span → 25% of the way from first temp to second."""
        now = datetime(2026, 5, 11, 13, 15, 0, tzinfo=UTC)
        entries = [
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 60.0),
            _entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 68.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 62.0
        assert method == "interpolated"

    def test_non_hour_aligned_gap_interpolation(self):
        """Bracketing entries need not be exactly 1h apart."""
        now = datetime(2026, 5, 11, 13, 20, 0, tzinfo=UTC)
        entries = [
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),
            _entry(datetime(2026, 5, 11, 13, 40, 0, tzinfo=UTC), 74.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        # 20 of 40 minutes elapsed = 50% of the way from 70 to 74
        assert result == 72.0
        assert method == "interpolated"

    def test_before_first_entry_within_2h_clamps_to_edge(self):
        now = datetime(2026, 5, 11, 11, 0, 0, tzinfo=UTC)
        entries = [_entry(datetime(2026, 5, 11, 12, 30, 0, tzinfo=UTC), 65.0)]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 65.0
        assert method == "edge-nearest"

    def test_before_first_entry_beyond_2h_unavailable(self):
        now = datetime(2026, 5, 11, 9, 0, 0, tzinfo=UTC)
        entries = [_entry(datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC), 65.0)]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result is None
        assert method == "unavailable"

    def test_after_last_entry_within_2h_clamps_to_edge(self):
        now = datetime(2026, 5, 11, 15, 30, 0, tzinfo=UTC)
        entries = [_entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 80.0)]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 80.0
        assert method == "edge-nearest"

    def test_after_last_entry_beyond_2h_unavailable(self):
        now = datetime(2026, 5, 11, 20, 0, 0, tzinfo=UTC)
        entries = [_entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 80.0)]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result is None
        assert method == "unavailable"

    def test_unsorted_input_still_interpolates_correctly(self):
        """Entries out of order in the raw list must still bracket correctly."""
        now = datetime(2026, 5, 11, 13, 30, 0, tzinfo=UTC)
        entries = [
            _entry(datetime(2026, 5, 11, 15, 0, 0, tzinfo=UTC), 90.0),
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),
            _entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 74.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 72.0
        assert method == "interpolated"

    def test_duplicate_timestamp_entries_do_not_divide_by_zero(self):
        now = datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC)
        entries = [
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 71.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result is not None
        assert method in ("interpolated", "edge-nearest")

    def test_naive_now_treated_as_utc(self):
        """A naive `now` (no tzinfo) is treated as UTC, matching _extract_current_hour_forecast_temp."""
        now = datetime(2026, 5, 11, 13, 30, 0)  # naive
        entries = [
            _entry(datetime(2026, 5, 11, 13, 0, 0, tzinfo=UTC), 70.0),
            _entry(datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC), 74.0),
        ]
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert result == 72.0
        assert method == "interpolated"

    def test_dst_spring_forward_transition_is_absolute_time_safe(self):
        """Simulates a US-style spring-forward: wall clock jumps 02:00 -> 03:00,
        so a naive local wall-clock diff would see 2 local "hours" between entries
        that are actually only 1 hour apart in real (UTC) time.

        Uses fixed UTC-offset `timezone` objects rather than ZoneInfo, so this
        test doesn't depend on the platform having an IANA tzdata database
        installed (not guaranteed on Windows without the `tzdata` package).
        Interpolation must use absolute elapsed seconds, not wall-clock hour
        arithmetic, so the fraction computed across the offset change is still
        correct.
        """
        pst = timezone(timedelta(hours=-8))
        pdt = timezone(timedelta(hours=-7))
        # 01:00 PST and 04:00 PDT differ by 3 "wall-clock hours" but only 2 real
        # hours (01:00 PST = 09:00 UTC, 04:00 PDT = 11:00 UTC) across the gap.
        before = datetime(2026, 3, 8, 1, 0, 0, tzinfo=pst)
        after = datetime(2026, 3, 8, 4, 0, 0, tzinfo=pdt)
        entries = [_entry(before, 50.0), _entry(after, 58.0)]

        # now = 1 real hour after `before` (10:00 UTC) = halfway through the 2h real span
        now = datetime(2026, 3, 8, 10, 0, 0, tzinfo=UTC)
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert method == "interpolated"
        assert result == 54.0  # exact midpoint of 50.0 and 58.0

    def test_dst_fall_back_transition_is_absolute_time_safe(self):
        """Simulates a US-style fall-back: wall clock repeats 01:00-02:00,
        so a naive local wall-clock diff would see fewer local "hours" between
        entries than actually elapsed in real (UTC) time.
        """
        pdt = timezone(timedelta(hours=-7))
        pst = timezone(timedelta(hours=-8))
        before = datetime(2026, 11, 1, 0, 0, 0, tzinfo=pdt)  # 00:00 PDT = 07:00 UTC
        after = datetime(2026, 11, 1, 3, 0, 0, tzinfo=pst)  # 03:00 PST = 11:00 UTC (real span = 4h)
        entries = [_entry(before, 50.0), _entry(after, 66.0)]

        # now = 2 real hours after `before` (09:00 UTC) = halfway through the 4h real span
        now = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)
        result, method = _interpolate_hourly_outdoor_temp(entries, now)
        assert method == "interpolated"
        assert result == 58.0  # exact midpoint of 50.0 and 66.0
