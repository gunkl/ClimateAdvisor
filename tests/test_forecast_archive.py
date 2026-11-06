"""Tests for ForecastArchive (Issue #1230 R0) and its coordinator wiring."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import stat
import sys
import types
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.const import (
    FORECAST_ARCHIVE_FILE,
    FORECAST_ARCHIVE_HORIZON_HOURS,
    FORECAST_ARCHIVE_MAX_DAYS,
)
from custom_components.climate_advisor.forecast_archive import ForecastArchive

MST = timezone(timedelta(hours=-7))
T0 = datetime(2026, 11, 6, 14, 5, tzinfo=MST)


def _hours(start: datetime, n: int, base: float = 50.0) -> list[tuple[datetime, float]]:
    first = start.replace(minute=0, second=0, microsecond=0)
    return [(first + timedelta(hours=i), base + i) for i in range(n)]


def _archive(tmp_path, **kw) -> ForecastArchive:
    return ForecastArchive(tmp_path, **kw)


class TestPathAndConstants:
    def test_unscoped_path_without_entry_id(self, tmp_path):
        assert _archive(tmp_path)._path == tmp_path / FORECAST_ARCHIVE_FILE

    def test_entry_scoped_path(self, tmp_path):
        a = _archive(tmp_path, entry_id="abc123")
        assert a._path == tmp_path / "climate_advisor_forecast_archive_abc123.json"

    def test_two_zones_do_not_collide(self, tmp_path):
        assert _archive(tmp_path, entry_id="a")._path != _archive(tmp_path, entry_id="b")._path

    def test_contract_constants(self):
        assert FORECAST_ARCHIVE_HORIZON_HOURS == 72
        assert FORECAST_ARCHIVE_MAX_DAYS == 365


class TestRoundTrip:
    def test_save_load_round_trip(self, tmp_path):
        a = _archive(tmp_path)
        assert a.record(T0, "weather.home", _hours(T0, 5)) is True
        a.save()
        b = _archive(tmp_path)
        b.load()
        got = b.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))
        assert got == a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))
        assert got[0]["source"] == "weather.home"
        assert got[0]["issued"] == "2026-11-06T14:05-07:00"
        assert got[0]["hours"][0] == ["2026-11-06T14:00-07:00", 50.0]
        assert len(got[0]["hours"]) == 5

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_saved_file_is_0600_and_no_tmp_left(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "weather.home", _hours(T0, 3))
        a.save()
        assert stat.S_IMODE(a._path.stat().st_mode) == 0o600
        assert list(tmp_path.glob("*.tmp")) == []

    def test_missing_file_starts_empty(self, tmp_path):
        a = _archive(tmp_path)
        a.load()
        assert a._snapshots == []

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"snapshots": "x"}', '{"other": []}'])
    def test_corrupt_file_starts_empty_and_warns(self, tmp_path, content, caplog):
        (tmp_path / FORECAST_ARCHIVE_FILE).write_text(content, encoding="utf-8")
        a = _archive(tmp_path)
        with caplog.at_level(logging.WARNING):
            a.load()
        assert a._snapshots == []
        assert "forecast_archive" in caplog.text

    def test_invalid_snapshots_dropped_on_load(self, tmp_path):
        now = datetime.now(UTC).isoformat(timespec="minutes")
        good = {"i": now, "s": "weather.home", "h0": now, "t": [50.0]}
        bad = [
            "not a dict",
            {"i": "garbage", "s": "w", "h0": now, "t": [1]},
            {"i": now, "s": "w", "h0": "garbage", "t": [1]},
            {"i": now, "s": 5, "h0": now, "t": [1]},
            {"i": now, "s": "w", "h0": now, "t": "nope"},
        ]
        (tmp_path / FORECAST_ARCHIVE_FILE).write_text(json.dumps({"snapshots": [*bad, good]}), encoding="utf-8")
        a = _archive(tmp_path)
        a.load()
        assert a._snapshots == [good]

    def test_load_logs_info_count_and_span(self, tmp_path, caplog):
        a = _archive(tmp_path)
        now = datetime.now(UTC)
        a.record(now - timedelta(hours=3), "weather.home", _hours(now, 2))
        a.record(now, "weather.home", _hours(now, 2))
        a.save()
        with caplog.at_level(logging.INFO):
            _archive(tmp_path).load()
        assert "loaded 2 snapshots" in caplog.text

    def test_save_failure_is_swallowed_and_logged(self, tmp_path, caplog):
        a = _archive(tmp_path / "does-not-exist")
        a._snapshots = [{"i": "x", "s": "w", "h0": "x", "t": []}]
        with caplog.at_level(logging.ERROR):
            a.save()  # mkstemp fails (no such dir): must not raise
        assert "failed to save" in caplog.text


class TestRetention:
    def test_load_evicts_older_than_max_days(self, tmp_path):
        now = datetime.now(UTC)
        old = (now - timedelta(days=FORECAST_ARCHIVE_MAX_DAYS + 1)).isoformat(timespec="minutes")
        new = (now - timedelta(days=FORECAST_ARCHIVE_MAX_DAYS - 1)).isoformat(timespec="minutes")
        snaps = [{"i": ts, "s": "w", "h0": ts, "t": [50.0]} for ts in (old, new)]
        (tmp_path / FORECAST_ARCHIVE_FILE).write_text(json.dumps({"snapshots": snaps}), encoding="utf-8")
        a = _archive(tmp_path)
        a.load()
        assert [s["i"] for s in a._snapshots] == [new]

    def test_record_prunes_but_at_most_hourly(self, tmp_path):
        a = _archive(tmp_path, max_days=10)
        now = datetime.now(UTC)
        old_ts = (now - timedelta(days=11)).isoformat(timespec="minutes")
        a._snapshots = [{"i": old_ts, "s": "w", "h0": old_ts, "t": [1.0]}]
        a.record(now, "w", _hours(now, 2))
        assert len(a._snapshots) == 1  # old one pruned on the first pass
        old2 = {"i": old_ts, "s": "w", "h0": old_ts, "t": [1.0]}
        a._snapshots.insert(0, old2)
        a.record(now + timedelta(hours=1), "w", _hours(now + timedelta(hours=1), 2))
        assert old2 in a._snapshots  # gate: second pass within the hour is skipped


class TestRecordRules:
    def test_same_local_hour_replaces_latest_wins(self, tmp_path):
        a = _archive(tmp_path)
        assert a.record(T0, "w", _hours(T0, 4, base=50.0)) is True
        later = T0 + timedelta(minutes=30)
        assert a.record(later, "w", _hours(later, 4, base=60.0)) is True
        assert len(a._snapshots) == 1
        snap = a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert snap["issued"] == "2026-11-06T14:35-07:00"
        assert snap["hours"][0][1] == 60.0

    def test_identical_refetch_reports_no_change(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", _hours(T0, 4))
        assert a.record(T0, "w", _hours(T0, 4)) is False

    def test_next_hour_appends(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", _hours(T0, 4))
        a.record(T0 + timedelta(hours=1), "w", _hours(T0 + timedelta(hours=1), 4))
        assert len(a._snapshots) == 2

    def test_horizon_cap(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", _hours(T0, 200))
        snap = a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert len(snap["hours"]) <= FORECAST_ARCHIVE_HORIZON_HOURS
        last = datetime.fromisoformat(snap["hours"][-1][0])
        assert last < T0 + timedelta(hours=FORECAST_ARCHIVE_HORIZON_HOURS)

    def test_past_hours_excluded(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", _hours(T0 - timedelta(hours=5), 8))
        snap = a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert snap["hours"][0][0] == "2026-11-06T14:00-07:00"

    def test_gap_hours_omitted_and_offgrid_skipped(self, tmp_path):
        a = _archive(tmp_path)
        pts = [(T0.replace(minute=0), 50.0), (T0.replace(minute=0) + timedelta(hours=2), 52.0)]
        pts.append((T0.replace(minute=0) + timedelta(minutes=90), 99.0))
        a.record(T0, "w", pts)
        snap = a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert [h[1] for h in snap["hours"]] == [50.0, 52.0]

    def test_empty_and_non_finite_not_recorded(self, tmp_path):
        a = _archive(tmp_path)
        assert a.record(T0, "w", []) is False
        assert a.record(T0, "w", [(T0.replace(minute=0), float("nan"))]) is False
        assert a._snapshots == []

    def test_temps_stored_as_given_one_decimal(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", [(T0.replace(minute=0), 68.4567)])
        assert a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]["hours"][0][1] == 68.5

    def test_naive_forecast_hours_read_in_issue_timezone(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", [(datetime(2026, 11, 6, 14, 0), 55.0)])
        assert a.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]["hours"][0][0] == (
            "2026-11-06T14:00-07:00"
        )

    def test_record_never_raises(self, tmp_path):
        a = _archive(tmp_path)
        assert a.record(T0, "w", [("bad", "bad")]) is False  # type: ignore[list-item]

    def test_debug_log_per_snapshot(self, tmp_path, caplog):
        a = _archive(tmp_path)
        with caplog.at_level(logging.DEBUG):
            a.record(T0, "weather.home", _hours(T0, 3))
        assert "recorded snapshot" in caplog.text and "weather.home" in caplog.text


class TestGetSnapshots:
    def test_range_is_half_open_and_ordered(self, tmp_path):
        a = _archive(tmp_path)
        for h in range(3):
            t = T0 + timedelta(hours=h)
            a.record(t, "w", _hours(t, 2))
        got = a.get_snapshots(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
        assert [g["issued"] for g in got] == ["2026-11-06T15:05-07:00"]
        assert len(a.get_snapshots(T0, T0 + timedelta(hours=3))) == 3

    def test_empty_range(self, tmp_path):
        assert _archive(tmp_path).get_snapshots(T0, T0 + timedelta(days=1)) == []

    def test_naive_bounds_read_as_utc(self, tmp_path):
        a = _archive(tmp_path)
        a.record(T0, "w", _hours(T0, 2))  # 21:05 UTC
        assert len(a.get_snapshots(datetime(2026, 11, 6, 21, 0), datetime(2026, 11, 6, 22, 0))) == 1


class TestSize:
    def test_full_year_of_72h_snapshots_is_small(self, tmp_path):
        a = _archive(tmp_path)
        base = datetime(2026, 1, 1, 0, 5, tzinfo=MST)
        for h in range(365 * 24):
            t = base + timedelta(hours=h)
            a._snapshots.append(
                {
                    "i": t.isoformat(timespec="minutes"),
                    "s": "weather.home",
                    "h0": t.replace(minute=0).isoformat(timespec="minutes"),
                    "t": [round(50 + (i % 30) * 0.7, 1) for i in range(72)],
                }
            )
        a.save()
        assert a._path.stat().st_size < 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Coordinator wiring
# ---------------------------------------------------------------------------


def _coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _stub(tmp_path, fetched, *, temp_unit="fahrenheit"):
    coord = MagicMock()
    coord.config = {"weather_entity": "weather.home", "temp_unit": temp_unit}
    coord._get_hourly_forecast_data = AsyncMock(return_value=fetched)
    coord._hourly_forecast_temps = []
    coord._forecast_archive = ForecastArchive(tmp_path)
    coord._executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    cls = _coordinator_class()
    coord._refresh_hourly_forecast = types.MethodType(cls._refresh_hourly_forecast, coord)
    coord._record_forecast_archive = types.MethodType(cls._record_forecast_archive, coord)
    return coord


def _entries(start: datetime, n: int, base: float = 60.0, **extra):
    first = start.replace(minute=0, second=0, microsecond=0)
    return [{"datetime": (first + timedelta(hours=i)).isoformat(), "temperature": base + i, **extra} for i in range(n)]


class TestCoordinatorWiring:
    def test_fetch_with_entries_records_and_saves_via_executor(self, tmp_path, frozen_now):
        frozen_now(T0)
        coord = _stub(tmp_path, _entries(T0, 4))
        asyncio.run(coord._refresh_hourly_forecast())
        assert len(coord._forecast_archive._snapshots) == 1
        assert coord._forecast_archive._path.exists()
        # the file write went through _executor_job, passed by reference
        assert coord._executor_job.await_args.args[0] == coord._forecast_archive.save

    def test_outage_kept_forecast_is_not_recorded(self, tmp_path, frozen_now):
        frozen_now(T0)
        coord = _stub(tmp_path, [])
        coord._hourly_forecast_temps = _entries(T0, 4)  # kept from before the outage
        asyncio.run(coord._refresh_hourly_forecast())
        assert coord._forecast_archive._snapshots == []
        coord._executor_job.assert_not_awaited()
        assert not coord._forecast_archive._path.exists()

    def test_synthetic_entries_excluded(self, tmp_path, frozen_now):
        frozen_now(T0)
        fetched = _entries(T0, 2) + _entries(T0 + timedelta(hours=2), 3, base=99.0, synthetic=True)
        coord = _stub(tmp_path, fetched)
        asyncio.run(coord._refresh_hourly_forecast())
        snap = coord._forecast_archive.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert len(snap["hours"]) == 2
        assert all(h[1] < 90 for h in snap["hours"])

    def test_celsius_install_stores_fahrenheit_without_double_conversion(self, tmp_path, frozen_now):
        """_get_hourly_forecast_data() already normalised C->F; the archive stores that value verbatim."""
        frozen_now(T0)
        c = 20.0  # provider reports 20 C
        f = c * 9 / 5 + 32  # 68.0, what _get_hourly_forecast_data hands back
        coord = _stub(tmp_path, [{"datetime": T0.replace(minute=0).isoformat(), "temperature": f}], temp_unit="celsius")
        asyncio.run(coord._refresh_hourly_forecast())
        snap = coord._forecast_archive.get_snapshots(T0 - timedelta(days=1), T0 + timedelta(days=1))[0]
        assert snap["hours"][0][1] == 68.0

    def test_archive_failure_never_breaks_the_cycle(self, tmp_path, frozen_now):
        frozen_now(T0)
        fetched = _entries(T0, 3)
        coord = _stub(tmp_path, fetched)
        coord._forecast_archive = MagicMock()
        coord._forecast_archive.record.side_effect = RuntimeError("boom")
        asyncio.run(coord._refresh_hourly_forecast())  # must not raise
        assert coord._hourly_forecast_temps is fetched
        assert coord.automation_engine._hourly_forecast_temps is fetched

    def test_save_failure_in_executor_never_breaks_the_cycle(self, tmp_path, frozen_now):
        frozen_now(T0)
        fetched = _entries(T0, 3)
        coord = _stub(tmp_path, fetched)
        coord._executor_job = AsyncMock(side_effect=OSError("disk full"))
        asyncio.run(coord._refresh_hourly_forecast())
        assert coord._hourly_forecast_temps is fetched

    def test_forecast_still_installed_before_archive_runs(self, tmp_path, frozen_now):
        frozen_now(T0)
        fetched = _entries(T0, 3)
        coord = _stub(tmp_path, fetched)
        asyncio.run(coord._refresh_hourly_forecast())
        assert coord._hourly_forecast_temps is fetched
        assert coord._hourly_forecast_cache_state == "live"
