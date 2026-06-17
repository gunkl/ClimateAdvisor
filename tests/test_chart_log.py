"""Tests for ChartStateLog in chart_log.py."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub out homeassistant before importing chart_log
# ---------------------------------------------------------------------------


def _build_ha_stubs() -> None:
    """Insert minimal HA stubs into sys.modules so chart_log imports cleanly."""
    if "homeassistant" in sys.modules:
        return  # already loaded by another test file — use what's there

    ha = types.ModuleType("homeassistant")
    ha_util = types.ModuleType("homeassistant.util")
    ha_util_dt = types.ModuleType("homeassistant.util.dt")

    # dt_util.now() returns a timezone-aware datetime
    ha_util_dt.now = lambda: datetime.now(UTC)

    ha_util.dt = ha_util_dt
    ha.util = ha_util

    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.util", ha_util)
    sys.modules.setdefault("homeassistant.util.dt", ha_util_dt)


_build_ha_stubs()

# chart_log.py binds dt_util via `from homeassistant.util import dt as dt_util`, which
# resolves to sys.modules["homeassistant.util"].dt.  When conftest MagicMock stubs are
# installed first (full suite run), _build_ha_stubs() exits early and that attribute is
# a MagicMock whose .now() returns another MagicMock — incompatible with datetime math.
# Unconditionally set a real now() on the actual bound object so _maybe_prune() works.
# Uses datetime.now(UTC) — real wall-clock time — because test_chart_log.py helpers
# also use real wall-clock via _ago(hours=...) so no fixed fake time is needed.
import custom_components.climate_advisor.chart_log as _chart_log_mod_init  # noqa: E402

_chart_log_mod_init.dt_util.now = lambda: datetime.now(UTC)

# Now it's safe to import the module under test
from custom_components.climate_advisor.chart_log import ChartStateLog  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def _ago(**kwargs) -> datetime:
    return _now() - timedelta(**kwargs)


def _make_log(tmp_path: Path, max_days: int = 365) -> ChartStateLog:
    return ChartStateLog(tmp_path, max_days=max_days)


# ---------------------------------------------------------------------------
# 1. append() adds entries correctly
# ---------------------------------------------------------------------------


class TestAppend:
    def test_single_entry_appears(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="heating", fan=False, indoor=70.0, outdoor=40.0)
        assert log.entry_count == 1
        entry = log._entries[0]
        assert entry["hvac"] == "heating"
        assert entry["fan"] is False
        assert entry["indoor"] == 70.0
        assert entry["outdoor"] == 40.0
        assert "ts" in entry

    def test_multiple_entries_accumulate(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        for i in range(5):
            log.append(hvac="off", fan=False, indoor=float(68 + i), outdoor=50.0)
        assert log.entry_count == 5

    def test_event_entry_includes_event_key(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, event="classification_change")
        entry = log._entries[0]
        assert entry["event"] == "classification_change"

    def test_non_event_entry_has_no_event_key(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="cooling", fan=True, indoor=76.0, outdoor=90.0)
        assert "event" not in log._entries[0]

    def test_explicit_ts_is_used(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        ts = "2026-01-01T12:00:00+00:00"
        log.append(hvac="off", fan=False, indoor=None, outdoor=None, ts=ts)
        assert log._entries[0]["ts"] == ts

    def test_null_indoor_outdoor_stored(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=None, outdoor=None)
        entry = log._entries[0]
        assert entry["indoor"] is None
        assert entry["outdoor"] is None


# ---------------------------------------------------------------------------
# 2. load() prunes entries older than max_days
# ---------------------------------------------------------------------------


class TestLoadPruning:
    def test_old_entries_pruned(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, max_days=7)
        entries = [
            {"ts": _iso(_ago(days=10)), "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0},
            {"ts": _iso(_ago(days=3)), "hvac": "heating", "fan": False, "indoor": 68.0, "outdoor": 35.0},
            {"ts": _iso(_ago(hours=1)), "hvac": "off", "fan": False, "indoor": 72.0, "outdoor": 55.0},
        ]
        (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")

        log.load()
        assert log.entry_count == 2  # 10-day-old entry pruned

    def test_no_entries_pruned_when_all_recent(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, max_days=30)
        entries = [
            {"ts": _iso(_ago(days=1)), "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0},
            {"ts": _iso(_ago(days=2)), "hvac": "heating", "fan": False, "indoor": 68.0, "outdoor": 38.0},
        ]
        (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
        log.load()
        assert log.entry_count == 2

    def test_entries_exactly_at_cutoff_kept(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, max_days=7)
        # Entry at exactly 7 days ago (within cutoff — should be kept)
        ts = _iso(_now() - timedelta(days=7) + timedelta(seconds=10))
        entries = [{"ts": ts, "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0}]
        (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
        log.load()
        assert log.entry_count == 1


# ---------------------------------------------------------------------------
# 3. get_entries("24h") returns only entries within 24 hours
# ---------------------------------------------------------------------------


class TestGetEntries24h:
    def _populate(self, log: ChartStateLog) -> None:
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, ts=_iso(_ago(hours=1)))
        log.append(hvac="heating", fan=False, indoor=68.0, outdoor=38.0, ts=_iso(_ago(hours=12)))
        log.append(hvac="off", fan=False, indoor=72.0, outdoor=55.0, ts=_iso(_ago(hours=30)))  # >24h

    def test_only_24h_entries_returned(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        self._populate(log)
        result = log.get_entries("24h")
        assert len(result) == 2

    def test_raw_entries_returned_for_24h(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        self._populate(log)
        result = log.get_entries("24h")
        # Raw entries have "hvac" as a string (not dominant computation)
        assert isinstance(result[0].get("hvac"), str)


# ---------------------------------------------------------------------------
# 4. get_entries("7d") returns hourly-bucketed entries
# ---------------------------------------------------------------------------


class TestGetEntries7d:
    def test_hourly_buckets_returned(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        # Add two entries in the same hour, two hours ago
        base = _ago(hours=2).replace(minute=5, second=0, microsecond=0)
        log.append(hvac="heating", fan=False, indoor=68.0, outdoor=35.0, ts=_iso(base))
        log.append(hvac="heating", fan=True, indoor=70.0, outdoor=36.0, ts=_iso(base.replace(minute=30)))
        # One entry in a different hour
        log.append(
            hvac="off",
            fan=False,
            indoor=72.0,
            outdoor=40.0,
            ts=_iso(_ago(hours=5).replace(minute=0, second=0, microsecond=0)),
        )
        result = log.get_entries("7d")
        # Should be 2 buckets
        assert len(result) == 2
        # Find the bucket with 2 source entries
        bucket = next(r for r in result if r.get("indoor") == 69.0)
        assert bucket["fan"] is True  # any True → True
        assert bucket["hvac"] == "heating"

    def test_indoor_average_computed(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=60.0, outdoor=40.0, ts=_iso(base))
        log.append(hvac="off", fan=False, indoor=80.0, outdoor=40.0, ts=_iso(base.replace(minute=30)))
        result = log.get_entries("7d")
        assert result[0]["indoor"] == 70.0

    def test_events_collected_in_bucket(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, event="window_rec", ts=_iso(base))
        log.append(hvac="off", fan=False, indoor=71.0, outdoor=55.0, ts=_iso(base.replace(minute=30)))
        result = log.get_entries("7d")
        assert "event" in result[0]
        assert "window_rec" in result[0]["event"]

    def test_dominant_hvac(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(hvac="heating", fan=False, indoor=68.0, outdoor=35.0, ts=_iso(base))
        log.append(hvac="heating", fan=False, indoor=69.0, outdoor=35.0, ts=_iso(base.replace(minute=20)))
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=36.0, ts=_iso(base.replace(minute=40)))
        result = log.get_entries("7d")
        assert result[0]["hvac"] == "heating"


# ---------------------------------------------------------------------------
# 5. get_entries("1y") returns daily-bucketed entries
# ---------------------------------------------------------------------------


class TestGetEntries1y:
    def test_daily_summary_keys_present(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="heating", fan=True, indoor=68.0, outdoor=32.0, ts=_iso(_ago(days=10)))
        result = log.get_entries("1y")
        assert len(result) == 1
        day = result[0]
        assert "indoor_avg" in day
        assert "indoor_min" in day
        assert "indoor_max" in day
        assert "outdoor_avg" in day
        assert "fan_minutes" in day
        assert "hvac" in day

    def test_fan_minutes_calculation(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(days=10).replace(hour=12, minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=True, indoor=70.0, outdoor=50.0, ts=_iso(base))
        log.append(hvac="off", fan=True, indoor=71.0, outdoor=51.0, ts=_iso(base.replace(hour=13)))
        log.append(hvac="off", fan=False, indoor=72.0, outdoor=52.0, ts=_iso(base.replace(hour=14)))
        result = log.get_entries("1y")
        assert result[0]["fan_minutes"] == 60  # 2 entries * 30 min

    def test_daily_min_max(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(days=5).replace(hour=8, minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=60.0, outdoor=30.0, ts=_iso(base))
        log.append(hvac="off", fan=False, indoor=75.0, outdoor=50.0, ts=_iso(base.replace(hour=14)))
        result = log.get_entries("1y")
        day = result[0]
        assert day["indoor_min"] == 60.0
        assert day["indoor_max"] == 75.0
        assert day["indoor_avg"] == 67.5

    def test_events_in_daily_summary(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(days=5).replace(hour=8, minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, event="override", ts=_iso(base))
        result = log.get_entries("1y")
        assert "events" in result[0]
        assert "override" in result[0]["events"]


# ---------------------------------------------------------------------------
# 6. save() + load() round-trips correctly
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_basic_round_trip(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        ts = _iso(_ago(hours=1))
        log.append(hvac="cooling", fan=False, indoor=78.0, outdoor=92.0, ts=ts)
        log.save()

        log2 = _make_log(tmp_path)
        log2.load()
        assert log2.entry_count == 1
        entry = log2._entries[0]
        assert entry["hvac"] == "cooling"
        assert entry["indoor"] == 78.0
        assert entry["ts"] == ts

    def test_event_round_trip(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        ts = _iso(_ago(hours=2))
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, event="window_rec", ts=ts)
        log.save()

        log2 = _make_log(tmp_path)
        log2.load()
        assert log2._entries[0].get("event") == "window_rec"

    def test_multiple_entries_round_trip(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        for i in range(10):
            log.append(hvac="off", fan=False, indoor=float(68 + i), outdoor=50.0, ts=_iso(_ago(hours=i + 1)))
        log.save()

        log2 = _make_log(tmp_path)
        log2.load()
        assert log2.entry_count == 10

    def test_file_contains_entries_key(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, ts=_iso(_ago(hours=1)))
        log.save()

        raw = json.loads((tmp_path / "climate_advisor_chart_log.json").read_text())
        assert "entries" in raw
        assert isinstance(raw["entries"], list)


# ---------------------------------------------------------------------------
# 7. load() on missing file is silent
# ---------------------------------------------------------------------------


class TestLoadMissingFile:
    def test_no_exception_on_missing_file(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.load()  # should not raise
        assert log.entry_count == 0

    def test_entries_empty_after_missing_file(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.load()
        assert log._entries == []


# ---------------------------------------------------------------------------
# 8. load() on corrupt file is silent
# ---------------------------------------------------------------------------


class TestLoadCorruptFile:
    def test_corrupt_json_silent(self, tmp_path: Path) -> None:
        path = tmp_path / "climate_advisor_chart_log.json"
        path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        log = _make_log(tmp_path)
        log.load()  # should not raise
        assert log.entry_count == 0

    def test_json_not_dict_silent(self, tmp_path: Path) -> None:
        path = tmp_path / "climate_advisor_chart_log.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        log = _make_log(tmp_path)
        log.load()
        assert log.entry_count == 0

    def test_missing_entries_key_silent(self, tmp_path: Path) -> None:
        path = tmp_path / "climate_advisor_chart_log.json"
        path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        log = _make_log(tmp_path)
        log.load()
        assert log.entry_count == 0

    def test_entries_not_list_silent(self, tmp_path: Path) -> None:
        path = tmp_path / "climate_advisor_chart_log.json"
        path.write_text(json.dumps({"entries": "oops"}), encoding="utf-8")
        log = _make_log(tmp_path)
        log.load()
        assert log.entry_count == 0

    def test_entries_with_bad_items_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "climate_advisor_chart_log.json"
        ts = _iso(_ago(hours=1))
        entries = [
            "not a dict",
            {"ts": "bad-ts", "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0},
            {"ts": ts, "hvac": "heating", "fan": False, "indoor": 68.0, "outdoor": 35.0},
        ]
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        log = _make_log(tmp_path)
        log.load()
        assert log.entry_count == 1


# ---------------------------------------------------------------------------
# 9. Event entries preserved in output
# ---------------------------------------------------------------------------


class TestEventPreservation:
    def test_event_in_raw_output(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(
            hvac="off", fan=False, indoor=70.0, outdoor=55.0, event="classification_change", ts=_iso(_ago(hours=1))
        )
        log.append(hvac="off", fan=False, indoor=71.0, outdoor=55.0, ts=_iso(_ago(hours=2)))
        result = log.get_entries("24h")
        event_entries = [e for e in result if "event" in e]
        assert len(event_entries) == 1
        assert event_entries[0]["event"] == "classification_change"

    def test_multiple_event_types_preserved(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        for event_type in ("classification_change", "window_rec", "override"):
            log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, event=event_type, ts=_iso(base))
        # Use 7d range to get hourly buckets
        result = log.get_entries("7d")
        assert len(result) == 1
        bucket = result[0]
        assert "event" in bucket
        for event_type in ("classification_change", "window_rec", "override"):
            assert event_type in bucket["event"]


# ---------------------------------------------------------------------------
# 10. entry_count property works
# ---------------------------------------------------------------------------


class TestEntryCount:
    def test_empty_log(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        assert log.entry_count == 0

    def test_count_after_appends(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        for _ in range(7):
            log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0)
        assert log.entry_count == 7

    def test_count_after_load(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        for i in range(3):
            log.append(hvac="off", fan=False, indoor=float(68 + i), outdoor=50.0, ts=_iso(_ago(hours=i + 1)))
        log.save()

        log2 = _make_log(tmp_path)
        log2.load()
        assert log2.entry_count == 3

    def test_count_decreases_after_pruning(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path, max_days=7)
        entries = [
            {"ts": _iso(_ago(days=10)), "hvac": "off", "fan": False, "indoor": 70.0, "outdoor": 50.0},
            {"ts": _iso(_ago(hours=1)), "hvac": "heating", "fan": False, "indoor": 68.0, "outdoor": 35.0},
        ]
        (tmp_path / "climate_advisor_chart_log.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
        log.load()
        assert log.entry_count == 1


# ---------------------------------------------------------------------------
# 7. pred_outdoor / pred_indoor fields
# ---------------------------------------------------------------------------


class TestPredFields:
    def test_append_stores_pred_fields(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, pred_outdoor=65.5, pred_indoor=72.0)
        entry = log._entries[0]
        assert entry["pred_outdoor"] == 65.5
        assert entry["pred_indoor"] == 72.0

    def test_append_pred_fields_default_none(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0)
        entry = log._entries[0]
        assert entry["pred_outdoor"] is None
        assert entry["pred_indoor"] is None

    def test_get_entries_exposes_pred_fields(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(
            hvac="off",
            fan=False,
            indoor=70.0,
            outdoor=50.0,
            pred_outdoor=60.0,
            pred_indoor=71.5,
            ts=_iso(_ago(hours=1)),
        )
        result = log.get_entries("24h")
        assert len(result) == 1
        assert result[0]["pred_outdoor"] == 60.0
        assert result[0]["pred_indoor"] == 71.5

    def test_bucket_hourly_averages_pred_fields(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        # Two entries in the same hour bucket: one with pred values, one without
        base_ts = _ago(days=5).replace(minute=0, second=0, microsecond=0)
        ts_a = _iso(base_ts.replace(minute=10))
        ts_b = _iso(base_ts.replace(minute=40))
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, pred_outdoor=65.0, pred_indoor=70.0, ts=ts_a)
        log.append(hvac="off", fan=False, indoor=72.0, outdoor=52.0, ts=ts_b)  # no pred vals
        result = log.get_entries("7d")
        assert len(result) == 1
        assert result[0]["pred_outdoor"] == 65.0
        assert result[0]["pred_indoor"] == 70.0

    def test_bucket_hourly_averages_two_pred_values(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base_ts = _ago(days=5).replace(minute=0, second=0, microsecond=0)
        ts_a = _iso(base_ts.replace(minute=10))
        ts_b = _iso(base_ts.replace(minute=50))
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, pred_outdoor=64.0, pred_indoor=70.0, ts=ts_a)
        log.append(hvac="off", fan=False, indoor=72.0, outdoor=52.0, pred_outdoor=66.0, pred_indoor=72.0, ts=ts_b)
        result = log.get_entries("7d")
        assert len(result) == 1
        assert result[0]["pred_outdoor"] == 65.0  # (64+66)/2
        assert result[0]["pred_indoor"] == 71.0  # (70+72)/2

    def test_bucket_daily_averages_pred_fields(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base_ts = _ago(days=60).replace(hour=12, minute=0, second=0, microsecond=0)
        ts_a = _iso(base_ts.replace(hour=10))
        ts_b = _iso(base_ts.replace(hour=14))
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, pred_outdoor=64.0, pred_indoor=70.0, ts=ts_a)
        log.append(hvac="off", fan=False, indoor=72.0, outdoor=52.0, pred_outdoor=66.0, pred_indoor=72.0, ts=ts_b)
        result = log.get_entries("1y")
        assert len(result) == 1
        assert result[0]["pred_outdoor_avg"] == 65.0
        assert result[0]["pred_indoor_avg"] == 71.0

    def test_bucket_daily_none_when_no_pred_values(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        base_ts = _ago(days=60).replace(hour=12, minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=50.0, ts=_iso(base_ts))
        result = log.get_entries("1y")
        assert len(result) == 1
        assert result[0]["pred_outdoor_avg"] is None
        assert result[0]["pred_indoor_avg"] is None

    def test_pred_fields_survive_save_load(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(
            hvac="heating",
            fan=False,
            indoor=68.0,
            outdoor=35.0,
            pred_outdoor=40.5,
            pred_indoor=69.0,
            ts=_iso(_ago(hours=1)),
        )
        log.save()
        log2 = _make_log(tmp_path)
        log2.load()
        assert log2.entry_count == 1
        entry = log2._entries[0]
        assert entry["pred_outdoor"] == 40.5
        assert entry["pred_indoor"] == 69.0


# ---------------------------------------------------------------------------
# 11. fan_running / nat_vent_active fields (Issue #331)
# ---------------------------------------------------------------------------


class TestFanRunningNatVentFields:
    """Tests for fan_running and nat_vent_active fields added in Issue #331.

    These two booleans let the frontend draw one "Vent" bar that distinguishes:
    - fan physically spinning (fan_running=True)
    - nat-vent session armed but fan between cycles (nat_vent_active=True, fan_running=False)
    - idle (both False)
    """

    # ---- append() persists both fields when explicitly passed ---------------

    def test_append_stores_fan_running_true(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=True, indoor=70.0, outdoor=55.0, fan_running=True)
        assert log._entries[0]["fan_running"] is True

    def test_append_stores_nat_vent_active_true(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, nat_vent_active=True)
        assert log._entries[0]["nat_vent_active"] is True

    def test_append_stores_both_true(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=True, indoor=70.0, outdoor=55.0, fan_running=True, nat_vent_active=True)
        entry = log._entries[0]
        assert entry["fan_running"] is True
        assert entry["nat_vent_active"] is True

    # ---- back-compat: both default to False when omitted -------------------

    def test_append_fan_running_defaults_false(self, tmp_path: Path) -> None:
        """Caller omits fan_running → stored as False (back-compat with old callers)."""
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0)
        assert log._entries[0]["fan_running"] is False

    def test_append_nat_vent_active_defaults_false(self, tmp_path: Path) -> None:
        """Caller omits nat_vent_active → stored as False (back-compat with old callers)."""
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0)
        assert log._entries[0]["nat_vent_active"] is False

    # ---- raw (24h) entries expose both fields -------------------------------

    def test_get_entries_24h_exposes_fan_running(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(
            hvac="off",
            fan=True,
            indoor=70.0,
            outdoor=55.0,
            fan_running=True,
            nat_vent_active=True,
            ts=_iso(_ago(hours=1)),
        )
        result = log.get_entries("24h")
        assert len(result) == 1
        assert result[0]["fan_running"] is True
        assert result[0]["nat_vent_active"] is True

    def test_get_entries_24h_both_false_when_omitted(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, ts=_iso(_ago(hours=1)))
        result = log.get_entries("24h")
        assert result[0]["fan_running"] is False
        assert result[0]["nat_vent_active"] is False

    # ---- hourly-bucketed (7d) entries: OR-aggregation ----------------------

    def test_bucket_hourly_fan_running_or_aggregated(self, tmp_path: Path) -> None:
        """fan_running is OR-aggregated across entries in a 7d hourly bucket (Issue #331)."""
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        # One entry with fan_running=False, one with fan_running=True in the same hour
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, fan_running=False, ts=_iso(base))
        log.append(
            hvac="off",
            fan=True,
            indoor=71.0,
            outdoor=55.0,
            fan_running=True,
            nat_vent_active=False,
            ts=_iso(base.replace(minute=30)),
        )
        result = log.get_entries("7d")
        assert len(result) == 1, "Two entries in same hour should produce one bucket"
        # OR-aggregated: any True → True
        assert result[0].get("fan_running") is True, (
            "fan_running not OR-aggregated in hourly bucket — "
            "flag: _bucket_hourly() missing fan_running propagation (Issue #331)"
        )

    def test_bucket_hourly_nat_vent_active_or_aggregated(self, tmp_path: Path) -> None:
        """nat_vent_active is OR-aggregated across entries in a 7d hourly bucket (Issue #331)."""
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, nat_vent_active=False, ts=_iso(base))
        log.append(
            hvac="off",
            fan=False,
            indoor=71.0,
            outdoor=55.0,
            nat_vent_active=True,
            ts=_iso(base.replace(minute=30)),
        )
        result = log.get_entries("7d")
        assert len(result) == 1
        assert result[0].get("nat_vent_active") is True, (
            "nat_vent_active not OR-aggregated in hourly bucket — "
            "flag: _bucket_hourly() missing nat_vent_active propagation (Issue #331)"
        )

    def test_bucket_hourly_fan_running_all_false_stays_false(self, tmp_path: Path) -> None:
        """When all entries in a bucket have fan_running=False, OR → False."""
        log = _make_log(tmp_path)
        base = _ago(hours=2).replace(minute=0, second=0, microsecond=0)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, fan_running=False, ts=_iso(base))
        log.append(
            hvac="off",
            fan=False,
            indoor=71.0,
            outdoor=55.0,
            fan_running=False,
            ts=_iso(base.replace(minute=30)),
        )
        result = log.get_entries("7d")
        assert len(result) == 1
        # Production gap: field may be absent. Use .get(k, False) so this sub-case
        # passes even before the fix — the important failure is the True case above.
        assert result[0].get("fan_running", False) is False

    # ---- round-trip: both fields survive save/load -------------------------

    def test_fan_running_nat_vent_survive_save_load(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(
            hvac="off",
            fan=True,
            indoor=70.0,
            outdoor=55.0,
            fan_running=True,
            nat_vent_active=True,
            ts=_iso(_ago(hours=1)),
        )
        log.save()
        log2 = _make_log(tmp_path)
        log2.load()
        assert log2.entry_count == 1
        entry = log2._entries[0]
        assert entry["fan_running"] is True
        assert entry["nat_vent_active"] is True

    def test_fan_running_false_survives_save_load(self, tmp_path: Path) -> None:
        log = _make_log(tmp_path)
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, ts=_iso(_ago(hours=1)))
        log.save()
        log2 = _make_log(tmp_path)
        log2.load()
        entry = log2._entries[0]
        assert entry["fan_running"] is False
        assert entry["nat_vent_active"] is False
