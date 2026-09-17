"""Tests for Issue #906 — extend chart forecast beyond met.no's 48-hour hourly horizon.

Covers the two new pure module-level functions in ``coordinator.py``:

- ``_synthesize_hourly_day(high, low, day_date)`` — extracted from the former
  inline cosine-fallback block inside ``_build_predicted_indoor_future()``.
- ``_build_extended_hourly_forecast(hourly_forecast, daily_forecast, now, unit)`` —
  new: builds a chart-only extended hourly list from real hourly entries plus
  synthetic days derived from the (longer-horizon) daily forecast.

Also retrofits a dedicated boundary test for ``_compute_day_hvac_modes()``,
which had none before this issue, using the shared
``tests/helpers/date_boundary_fixtures.py`` hazard set (see that module's
docstring for the Issue #190 background this all traces back to).
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from tests.helpers.date_boundary_fixtures import DATE_BOUNDARY_CASES  # noqa: E402


def _get_coordinator_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _hourly_entry(dt_str: str, temp: float) -> dict:
    return {"datetime": dt_str, "temperature": temp}


def _daily_entry(dt_str: str, high: float, low: float) -> dict:
    return {"datetime": dt_str, "temperature": high, "templow": low}


def _as_local_stub(case):
    """Faithful stand-in for dt_util.as_local() under a case's fixed-offset local_tz.

    Mirrors real HA behavior: an aware datetime is converted to local_tz; a naive
    datetime is treated as already-local and gets local_tz attached. (The simpler
    `side_effect=lambda x: x` identity stub used elsewhere in this suite is only
    valid when every datetime in play is naive and no real tz conversion needs to
    be exercised — these boundary-fixture tests need the real promotion/conversion
    behavior since they mix naive synthetic-entry datetimes with aware real-entry
    datetimes, exactly as production does.)
    """

    def _convert(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(case.local_tz)
        return dt.replace(tzinfo=case.local_tz)

    return _convert


def _classification(**overrides):
    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "hot",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 85.0,
        "today_low": 60.0,
        "tomorrow_high": 84.0,
        "tomorrow_low": 59.0,
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


# ---------------------------------------------------------------------------
# _synthesize_hourly_day()
# ---------------------------------------------------------------------------


class TestSynthesizeHourlyDay:
    def test_exact_24_entry_output(self):
        coord_mod = _get_coordinator_module()
        day_date = date(2026, 6, 10)
        high, low = 85.0, 60.0

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._synthesize_hourly_day(high, low, day_date)

        expected_cosine = coord_mod._cosine_outdoor_curve(high, low)
        assert len(result) == 24
        for h, entry in enumerate(result):
            expected_dt = datetime.combine(day_date, time(hour=h))
            assert entry["datetime"] == expected_dt.isoformat()
            assert entry["temperature"] == expected_cosine[h]["temp"]

    def test_datetime_construction_uses_combine_not_replace(self):
        """Regression guard: must call dt_util.as_local(datetime.combine(...)), not
        build via now-relative .replace()/timedelta arithmetic — the whole point of
        extracting this into a day_date-parametrized function (Issue #906) is that
        it works for an arbitrary future day_date, not just today/tomorrow."""
        coord_mod = _get_coordinator_module()
        far_future_date = date(2027, 9, 1)

        captured: list[datetime] = []

        def _capture_as_local(dt_obj):
            captured.append(dt_obj)
            return dt_obj

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=_capture_as_local,
        ):
            result = coord_mod._synthesize_hourly_day(80.0, 55.0, far_future_date)

        assert len(result) == 24
        assert len(captured) == 24
        for h, dt_obj in enumerate(captured):
            assert dt_obj.date() == far_future_date
            assert dt_obj.hour == h
            assert dt_obj.tzinfo is None  # combine(...).replace(tzinfo=None) — naive in

    @pytest.mark.parametrize("case", DATE_BOUNDARY_CASES, ids=lambda c: c.label)
    def test_boundary_fixture_lands_on_correct_date(self, case):
        """Every hazard case: 24 synthetic entries must all land on day_date, and
        hour h must land on wall-clock hour h — DST/year/month/leap-day arithmetic
        must never shift the date or hour."""
        coord_mod = _get_coordinator_module()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=_as_local_stub(case),
        ):
            result = coord_mod._synthesize_hourly_day(80.0, 55.0, case.now.date())

        assert len(result) == 24
        for h, entry in enumerate(result):
            entry_dt = datetime.fromisoformat(entry["datetime"])
            assert entry_dt.date() == case.now.date(), f"{case.label}: hour {h} landed on wrong date"
            assert entry_dt.hour == h, f"{case.label}: hour index {h} landed on wall-clock hour {entry_dt.hour}"

    def test_real_dst_transition_calls_as_local_fresh_per_hour_with_correct_offsets(self):
        """Issue #906 verification round, Fix 4: the fixed-offset DST cases above
        can never distinguish a correct per-hour datetime.combine()+as_local() call
        from a forbidden timedelta/now-relative construction, because a fixed
        offset never transitions -- both approaches happen to produce identical
        output when the offset never changes. A REAL IANA zone spanning an actual
        DST transition (America/Los_Angeles, 2026-03-08 spring-forward, 2am->3am)
        can distinguish them: the correct implementation must call
        dt_util.as_local() once per hour, each time with a fresh naive datetime
        already anchored to day_date -- not reuse a single localized reference
        point via timedelta/replace arithmetic. This is proven two ways: (1) the
        exact call pattern (count + input shape) is asserted directly, and (2) the
        resulting UTC offsets must show the real PST->PDT jump within this single
        day (hour 0-1 = -08:00, hour 3-23 = -07:00).

        Verified as load-bearing (Craftsman-4, Issue #906 fix round): temporarily
        reverting _synthesize_hourly_day() to call dt_util.as_local() once for
        day_date's midnight and build the remaining 23 hours via
        `midnight_local + timedelta(hours=h)` makes this test's call-count
        assertion fail immediately (1 call instead of 24) -- confirmed by hand,
        then reverted back to the correct per-hour implementation."""
        coord_mod = _get_coordinator_module()
        tz = ZoneInfo("America/Los_Angeles")
        day_date = date(2026, 3, 8)  # actual US spring-forward date

        calls: list[datetime] = []

        def _real_as_local(dt_obj: datetime) -> datetime:
            calls.append(dt_obj)
            if dt_obj.tzinfo is not None:
                return dt_obj.astimezone(tz)
            return dt_obj.replace(tzinfo=tz)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=_real_as_local,
        ):
            result = coord_mod._synthesize_hourly_day(80.0, 55.0, day_date)

        assert len(result) == 24
        assert len(calls) == 24, (
            "must call dt_util.as_local() once per hour, not once for the whole day "
            f"(got {len(calls)} calls) — a single call + timedelta/replace arithmetic "
            "cannot be distinguished by the fixed-offset boundary cases above"
        )
        for h, dt_obj in enumerate(calls):
            assert dt_obj.tzinfo is None, f"hour {h}: as_local() must receive a naive datetime"
            assert dt_obj.date() == day_date, f"hour {h}: as_local() must receive a datetime already on day_date"
            assert dt_obj.hour == h, f"hour {h}: as_local() must receive a datetime for wall-clock hour {h}"

        # Real DST jump within this single day: hour 2 (nonexistent wall time,
        # Python resolves via fold=0 default) and hours 0-1 stay PST; hours 3-23
        # are PDT.
        for h, entry in enumerate(result):
            entry_dt = datetime.fromisoformat(entry["datetime"])
            expected_offset = timedelta(hours=-8) if h <= 2 else timedelta(hours=-7)
            assert entry_dt.utcoffset() == expected_offset, (
                f"hour {h}: expected UTC offset {expected_offset}, got {entry_dt.utcoffset()}"
            )


# ---------------------------------------------------------------------------
# _build_predicted_indoor_future() fallback — refactor behavior parity
# ---------------------------------------------------------------------------


class TestPredictedIndoorFutureFallbackParity:
    """Proves the Issue #906 refactor (inline cosine block -> _synthesize_hourly_day())
    is behavior-preserving for the non-DST-transition case: the new combination of
    two _synthesize_hourly_day() calls + today/tomorrow rollover selection reproduces
    the exact arithmetic the old now_local.replace(hour=h, ...) inline block used.
    """

    def _old_formula(self, cosine, now_local):
        """Pre-refactor inline logic, reproduced verbatim for comparison."""
        synthetic = []
        for entry in cosine:
            h = entry["hour"]
            future_dt = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
            if future_dt <= now_local:
                future_dt += timedelta(days=1)
            synthetic.append({"datetime": future_dt.isoformat(), "temperature": entry["temp"]})
        return synthetic

    def test_new_combination_matches_old_replace_formula(self):
        coord_mod = _get_coordinator_module()
        high, low = 85.0, 60.0
        now_local = datetime(2026, 6, 10, 14, 0, 0)  # naive; mid-afternoon, non-DST-hazard date

        cosine = coord_mod._cosine_outdoor_curve(high, low)
        expected = self._old_formula(cosine, now_local)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            today_entries = coord_mod._synthesize_hourly_day(high, low, now_local.date())
            tomorrow_entries = coord_mod._synthesize_hourly_day(high, low, now_local.date() + timedelta(days=1))

        actual = []
        for today_entry, tomorrow_entry in zip(today_entries, tomorrow_entries, strict=True):
            future_dt = datetime.fromisoformat(today_entry["datetime"])
            actual.append(tomorrow_entry if future_dt <= now_local else today_entry)

        assert actual == expected

    def test_build_predicted_indoor_future_fallback_still_produces_output(self):
        """End-to-end smoke test: the fallback path still runs successfully and
        produces a non-empty predicted-indoor curve after the refactor."""
        coord_mod = _get_coordinator_module()
        config = {"comfort_heat": 68, "comfort_cool": 76, "setback_heat": 60, "setback_cool": 80}
        classification = _classification(today_high=85.0, today_low=60.0, hvac_mode="cool")
        now_local = datetime(2026, 6, 10, 14, 0, 0)

        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.as_local",
                side_effect=lambda x: x,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now",
                return_value=now_local,
            ),
        ):
            result = coord_mod._build_predicted_indoor_future(
                hourly_forecast=None,
                config=config,
                now=now_local,
                current_indoor_temp=72.0,
                thermal_model=None,
                classification=classification,
            )

        assert result, "Expected non-empty predicted-indoor curve from the cosine fallback path"


# ---------------------------------------------------------------------------
# _build_extended_hourly_forecast()
# ---------------------------------------------------------------------------


class TestBuildExtendedHourlyForecast:
    _NOW = datetime(2026, 6, 1, 8, 0, 0)

    def _real_48h_forecast(self, start: date = date(2026, 6, 1)) -> list[dict]:
        """48 hourly entries spanning 2 real days."""
        entries = []
        for i in range(48):
            dt = datetime.combine(start, time(hour=0)) + timedelta(hours=i)
            entries.append(_hourly_entry(dt.isoformat(), 70.0 + (i % 24) * 0.1))
        return entries

    def _daily_6day_forecast(self, start: date = date(2026, 6, 1), unit_scale=1.0) -> list[dict]:
        """6-day daily forecast, day-spaced (~24h apart), starting the same day
        as the real hourly forecast."""
        entries = []
        for i in range(6):
            d = start + timedelta(days=i)
            entries.append(
                _daily_entry(f"{d.isoformat()}T00:00:00+00:00", (80.0 + i) * unit_scale, (55.0 + i) * unit_scale)
            )
        return entries

    def test_extends_with_correct_synthetic_count_and_dates(self):
        coord_mod = _get_coordinator_module()
        hourly = self._real_48h_forecast()
        daily = self._daily_6day_forecast()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        # 48 real hours cover June 1 + June 2 (last real local date = June 2).
        # Daily forecast covers June 1-6 → June 3, 4, 5, 6 are beyond → 4 synthetic days * 24h.
        synthetic = [e for e in result if e.get("synthetic")]
        real = [e for e in result if not e.get("synthetic")]
        assert len(real) == 48
        assert len(synthetic) == 4 * 24

        synthetic_dates = {datetime.fromisoformat(e["datetime"]).date() for e in synthetic}
        assert synthetic_dates == {date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 6)}

        # No overlap with real days.
        real_dates = {datetime.fromisoformat(e["datetime"]).date() for e in real}
        assert not (synthetic_dates & real_dates)

        # Result is sorted by datetime.
        parsed = [datetime.fromisoformat(e["datetime"]) for e in result]
        assert parsed == sorted(parsed)

    def test_celsius_unit_conversion_matches_hand_computed_fahrenheit(self):
        coord_mod = _get_coordinator_module()
        from custom_components.climate_advisor.temperature import to_fahrenheit

        hourly = self._real_48h_forecast()
        # Daily forecast in Celsius: day 3 (2026-06-03) high=30C, low=18C.
        daily = [
            _daily_entry("2026-06-01T00:00:00+00:00", 27.0, 15.0),
            _daily_entry("2026-06-02T00:00:00+00:00", 28.0, 16.0),
            _daily_entry("2026-06-03T00:00:00+00:00", 30.0, 18.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "celsius")

        synthetic = [e for e in result if e.get("synthetic")]
        assert synthetic, "Expected synthetic entries for 2026-06-03"

        expected_high_f = to_fahrenheit(30.0, "celsius")
        expected_low_f = to_fahrenheit(18.0, "celsius")
        temps = [e["temperature"] for e in synthetic]
        assert max(temps) == pytest.approx(expected_high_f, abs=0.15)
        assert min(temps) == pytest.approx(expected_low_f, abs=0.15)

    def test_empty_hourly_forecast_anchors_to_now(self):
        """No real hourly data at all — extension anchors to `now`'s local date
        rather than synthesizing already-past daily-forecast days."""
        coord_mod = _get_coordinator_module()
        daily = self._daily_6day_forecast()  # 2026-06-01 .. 2026-06-06
        now = datetime(2026, 6, 3, 8, 0, 0)  # local "today" = June 3

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(None, daily, now, "fahrenheit")

        synthetic_dates = {datetime.fromisoformat(e["datetime"]).date() for e in result if e.get("synthetic")}
        # June 1 and June 2 are in the past relative to `now` — excluded.
        assert date(2026, 6, 1) not in synthetic_dates
        assert date(2026, 6, 2) not in synthetic_dates
        assert synthetic_dates == {date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 6)}

    def test_daily_forecast_none_is_noop(self):
        coord_mod = _get_coordinator_module()
        hourly = self._real_48h_forecast()

        result = coord_mod._build_extended_hourly_forecast(hourly, None, self._NOW, "fahrenheit")
        assert result == hourly

    def test_daily_forecast_empty_is_noop(self):
        coord_mod = _get_coordinator_module()
        hourly = self._real_48h_forecast()

        result = coord_mod._build_extended_hourly_forecast(hourly, [], self._NOW, "fahrenheit")
        assert result == hourly

    def test_hourly_spaced_daily_forecast_triggers_spacing_guard(self):
        """The deprecated `forecast` attribute fallback isn't guaranteed to be
        day-spaced — simulate it with hourly-spaced entries and confirm the
        function fails safe (returns the real list unchanged) instead of
        fabricating bad synthetic days."""
        coord_mod = _get_coordinator_module()
        hourly = self._real_48h_forecast()
        # "daily_forecast" that's actually hourly-spaced (median gap ~1h << 20h guard).
        hourly_spaced_daily = [
            _daily_entry(
                (datetime.combine(date(2026, 6, 1), time(hour=0)) + timedelta(hours=i)).isoformat(),
                75.0,
                60.0,
            )
            for i in range(10)
        ]

        result = coord_mod._build_extended_hourly_forecast(hourly, hourly_spaced_daily, self._NOW, "fahrenheit")
        assert result == hourly
        assert not any(e.get("synthetic") for e in result)

    def test_hourly_already_covers_every_daily_day_yields_zero_synthetic(self):
        coord_mod = _get_coordinator_module()
        # Real hourly forecast spans 6 full days — matches every daily-forecast day.
        entries = []
        for i in range(6 * 24):
            dt = datetime.combine(date(2026, 6, 1), time(hour=0)) + timedelta(hours=i)
            entries.append(_hourly_entry(dt.isoformat(), 70.0))
        daily = self._daily_6day_forecast()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(entries, daily, self._NOW, "fahrenheit")

        assert not any(e.get("synthetic") for e in result)
        assert len(result) == len(entries)

    def test_does_not_mutate_input_lists(self):
        coord_mod = _get_coordinator_module()
        hourly = self._real_48h_forecast()
        daily = self._daily_6day_forecast()
        hourly_copy = [dict(e) for e in hourly]
        daily_copy = [dict(e) for e in daily]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        assert hourly == hourly_copy
        assert daily == daily_copy

    @pytest.mark.parametrize("case", DATE_BOUNDARY_CASES, ids=lambda c: c.label)
    def test_boundary_fixture_synthetic_day_lands_on_intended_date(self, case):
        """The synthesized day for a daily-forecast entry beyond the real hourly
        horizon must land on the date the source data intended — raw date for the
        daily entry, regardless of DST/year/month/leap-day hazards.

        Since the boundary day (`real_day`) here also has a matching daily-forecast
        entry, Fix 2 (partial-day seam fill) also synthesizes its remaining hours —
        so synthetic entries may land on `real_day` too, not only the fully-beyond
        target day. The full target day must always be entirely synthetic (24h)."""
        coord_mod = _get_coordinator_module()

        # One real hourly entry, on the day before the daily-forecast's target day,
        # so last_real_local_date < daily entry's date and it counts as "beyond".
        real_day = case.daily_expected_raw_date - timedelta(days=1)
        real_day_utc = datetime.combine(real_day, time(hour=12), tzinfo=UTC)
        hourly = [_hourly_entry(real_day_utc.isoformat(), 65.0)]

        daily = [
            _daily_entry(datetime.combine(real_day, time(hour=0), tzinfo=UTC).isoformat(), 70.0, 50.0),
            _daily_entry(case.daily_entry_utc.isoformat(), 80.0, 55.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=_as_local_stub(case),
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, case.now, "fahrenheit")

        synthetic = [e for e in result if e.get("synthetic")]
        assert synthetic, f"{case.label}: expected synthetic entries for the beyond-horizon day"
        synthetic_dates = {datetime.fromisoformat(e["datetime"]).date() for e in synthetic}
        assert case.daily_expected_raw_date in synthetic_dates, (
            f"{case.label}: expected the fully-beyond target day {case.daily_expected_raw_date} in "
            f"synthetic dates, got {synthetic_dates}"
        )
        assert synthetic_dates <= {real_day, case.daily_expected_raw_date}, (
            f"{case.label}: synthetic entries landed on unexpected dates {synthetic_dates}"
        )

        full_day_hours = {
            datetime.fromisoformat(e["datetime"]).hour
            for e in synthetic
            if datetime.fromisoformat(e["datetime"]).date() == case.daily_expected_raw_date
        }
        assert full_day_hours == set(range(24)), (
            f"{case.label}: expected all 24 hours synthetic for the fully-beyond day, got {full_day_hours}"
        )


# ---------------------------------------------------------------------------
# Fix 1 (verification round) — crash-safety: mixed naive/aware sort, dropped
# entries with unparseable/missing datetime.
# ---------------------------------------------------------------------------


class TestBuildExtendedHourlyForecastCrashSafety:
    _NOW = datetime(2026, 6, 1, 8, 0, 0)

    def _daily_6day_forecast(self, start: date = date(2026, 6, 1)) -> list[dict]:
        entries = []
        for i in range(6):
            d = start + timedelta(days=i)
            entries.append(_daily_entry(f"{d.isoformat()}T00:00:00+00:00", 80.0 + i, 55.0 + i))
        return entries

    def test_mixed_naive_and_aware_hourly_entries_sort_without_raising(self):
        """A weather integration whose hourly entries use naive ISO datetimes
        (real-world case) mixed with aware synthetic entries (always aware via
        dt_util.as_local()) must not raise TypeError at sort time, and must
        produce correct chronological order."""
        coord_mod = _get_coordinator_module()

        naive_hourly = [
            _hourly_entry((datetime(2026, 6, 1, 0, 0, 0) + timedelta(hours=i)).isoformat(), 70.0) for i in range(48)
        ]
        # A few entries with an explicit UTC offset mixed in (aware) — simulates a
        # weather integration that's internally inconsistent, or a provider that
        # switched formats mid-window.
        aware_hourly = [
            _hourly_entry(
                (datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC) + timedelta(hours=i)).isoformat(),
                71.0,
            )
            for i in range(2)
        ]
        hourly = naive_hourly + aware_hourly
        daily = self._daily_6day_forecast()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x if x.tzinfo else x.replace(tzinfo=UTC),
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        assert result, "expected non-empty merged result"
        parsed = [
            dt_obj if dt_obj.tzinfo else dt_obj.replace(tzinfo=UTC)
            for dt_obj in (datetime.fromisoformat(e["datetime"]) for e in result)
        ]
        assert parsed == sorted(parsed), "merged result must be in chronological order"

    def test_entry_with_missing_datetime_field_is_dropped_not_crashed_on(self):
        coord_mod = _get_coordinator_module()
        hourly = [
            _hourly_entry((datetime(2026, 6, 1, 0, 0, 0) + timedelta(hours=i)).isoformat(), 70.0) for i in range(48)
        ]
        # Corrupt a few entries: missing datetime entirely, and malformed datetime.
        hourly.append({"temperature": 72.0})  # no "datetime"/"time" key at all
        hourly.append({"datetime": "not-a-real-timestamp", "temperature": 73.0})
        daily = self._daily_6day_forecast()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x if x.tzinfo else x.replace(tzinfo=UTC),
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        # Must not raise, and the two corrupt entries must not appear in the output.
        assert not any(e.get("temperature") == 72.0 and "datetime" not in e for e in result)
        assert not any(e.get("datetime") == "not-a-real-timestamp" for e in result)
        real_count = sum(1 for e in result if not e.get("synthetic"))
        assert real_count == 48, f"expected the 2 corrupt entries dropped, kept {real_count} real entries"


# ---------------------------------------------------------------------------
# Fix 2 (verification round) — partial-day seam fill at the 48h boundary.
# ---------------------------------------------------------------------------


class TestBuildExtendedHourlyForecastPartialDaySeamFill:
    _NOW = datetime(2026, 6, 1, 8, 0, 0)

    def test_partial_day_fill_closes_gap_with_no_seam(self):
        """Real hourly data ends mid-day (through hour 10 of 2026-06-01). A
        matching daily-forecast entry exists for that same date -> hours 11-23
        must be filled with synthetic entries, immediately following the last
        real hour with no gap."""
        coord_mod = _get_coordinator_module()
        boundary_day = date(2026, 6, 1)

        hourly = [
            _hourly_entry((datetime.combine(boundary_day, time(hour=h))).isoformat(), 70.0) for h in range(11)
        ]  # hours 0-10 only
        daily = [
            _daily_entry(f"{boundary_day.isoformat()}T00:00:00+00:00", 85.0, 60.0),
            _daily_entry(f"{(boundary_day + timedelta(days=1)).isoformat()}T00:00:00+00:00", 86.0, 61.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        boundary_day_entries = sorted(
            (e for e in result if datetime.fromisoformat(e["datetime"]).date() == boundary_day),
            key=lambda e: datetime.fromisoformat(e["datetime"]).hour,
        )
        assert [datetime.fromisoformat(e["datetime"]).hour for e in boundary_day_entries] == list(range(24)), (
            "expected boundary day to have all 24 hours present (0-10 real, 11-23 synthetic) with no gap"
        )
        for e in boundary_day_entries[:11]:
            assert not e.get("synthetic"), f"hour {datetime.fromisoformat(e['datetime']).hour} should be real"
        for e in boundary_day_entries[11:]:
            assert e.get("synthetic") is True, f"hour {datetime.fromisoformat(e['datetime']).hour} should be synthetic"

    def test_no_partial_fill_without_matching_daily_entry_for_boundary_day(self):
        """No daily-forecast entry for the exact boundary date -> no partial
        fill is fabricated for it (no high/low to extrapolate from)."""
        coord_mod = _get_coordinator_module()
        boundary_day = date(2026, 6, 1)

        hourly = [_hourly_entry((datetime.combine(boundary_day, time(hour=h))).isoformat(), 70.0) for h in range(11)]
        # Daily forecast starts the NEXT day — no entry for boundary_day itself.
        daily = [
            _daily_entry(f"{(boundary_day + timedelta(days=1)).isoformat()}T00:00:00+00:00", 86.0, 61.0),
            _daily_entry(f"{(boundary_day + timedelta(days=2)).isoformat()}T00:00:00+00:00", 87.0, 62.0),
        ]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = coord_mod._build_extended_hourly_forecast(hourly, daily, self._NOW, "fahrenheit")

        boundary_day_hours = {
            datetime.fromisoformat(e["datetime"]).hour
            for e in result
            if datetime.fromisoformat(e["datetime"]).date() == boundary_day
        }
        assert boundary_day_hours == set(range(11)), (
            f"expected no partial fill for boundary day without a matching daily entry, got {boundary_day_hours}"
        )


# ---------------------------------------------------------------------------
# _compute_day_hvac_modes() — new dedicated boundary test (Issue #906)
# ---------------------------------------------------------------------------


class TestComputeDayHvacModesBoundary:
    """_compute_day_hvac_modes() previously had no dedicated date-boundary test —
    this is the first time it's fed data spanning more than ~2 real days (Issue #906).
    Verifies its hourly-entry local-date convention (dt_util.as_local().date()) holds
    across the same shared hazard set used for the new forecast-extension functions.
    """

    @pytest.mark.parametrize("case", DATE_BOUNDARY_CASES, ids=lambda c: c.label)
    def test_hourly_entry_grouped_under_correct_local_date(self, case):
        coord_mod = _get_coordinator_module()
        hourly = [_hourly_entry(case.hourly_entry_utc.isoformat(), 72.0)]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=_as_local_stub(case),
        ):
            result = coord_mod._compute_day_hvac_modes(hourly, case.now, classification=None)

        assert case.hourly_expected_local_date in result, (
            f"{case.label}: expected day {case.hourly_expected_local_date} in {sorted(result)}"
        )
