"""The v4 predictions archive (Issue #1230 R1): first write wins, persistence, retention, corrupt files.

Occupant view: v4 runs in shadow, so what it said 1/4/12/24 h ahead must be kept exactly as first written - a later,
better-informed prediction for the same slot must never overwrite it, or the accuracy page would score hindsight.
A damaged or missing file must start empty and never stop the zone from starting.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor.const import ENTRY_SCOPED_FILES, V4_ARCHIVE_FILE, V4_ARCHIVE_MAX_DAYS
from custom_components.climate_advisor.storage_paths import resolve_entry_scoped_path
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key

_NOW = datetime(2026, 11, 6, 10, 0, 2, tzinfo=UTC)  # a cycle a couple of seconds after the half hour boundary


def _points(now: datetime, base: float, hours: int = 25, step_min: int = 15) -> list[tuple[datetime, float]]:
    return [(now + timedelta(minutes=step_min * k), base + 0.01 * k) for k in range(1, hours * 60 // step_min + 1)]


def _archive(tmp_path: Path, entry_id: str = "zoneA") -> V4Archive:
    return V4Archive(tmp_path, entry_id=entry_id)


def test_file_is_entry_scoped_and_listed_for_zone_removal(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    assert arch._path == resolve_entry_scoped_path(tmp_path, V4_ARCHIVE_FILE, "zoneA")
    assert arch._path.name == "climate_advisor_v4_archive_zoneA.json"
    assert V4_ARCHIVE_FILE in ENTRY_SCOPED_FILES


def test_records_1_4_12_24h_per_sensor_and_ca_alias(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    added = arch.record(_NOW, {"climate.a": _points(_NOW, 68.0), "sensor.b": _points(_NOW, 66.0)}, primary="climate.a")
    assert added == 8  # 2 stored lines x 4 leads: "_ca" is derived on read, never stored
    assert set(arch.data) == {"climate.a", "sensor.b", "_ca"}
    for lead_h in (1, 4, 12, 24):
        slot = slot_key(_NOW + timedelta(hours=lead_h))
        assert slot == int((_NOW + timedelta(hours=lead_h)).replace(second=0).timestamp())  # nearest half hour
        assert f"{lead_h}h" in arch.data["climate.a"][slot]
    # _ca is the primary's line
    assert arch.data["_ca"] == arch.data["climate.a"]
    assert arch.dirty is True


def test_first_write_wins_per_sensor_slot_and_lead(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")
    slot_4h = slot_key(_NOW + timedelta(hours=4))
    first_4h = arch.data["climate.a"][slot_4h]["4h"]
    # 30 minutes later the same slot is 3.5 h ahead: nothing recorded for it (not a tracked lead), and a different
    # prediction for the very same (slot, lead) must not replace the first one.
    later = _NOW + timedelta(minutes=30)
    arch.record(later, {"climate.a": _points(later, 80.0)}, primary="climate.a")
    assert arch.data["climate.a"][slot_4h]["4h"] == first_4h
    # Same cycle replayed with other numbers: still the first values.
    added = arch.record(_NOW, {"climate.a": _points(_NOW, 90.0)}, primary="climate.a")
    assert added == 0
    assert arch.data["climate.a"][slot_4h]["4h"] == first_4h
    # Different leads of one slot are written by different cycles and coexist.
    slot = slot_key(_NOW + timedelta(hours=1))
    arch.record(_NOW - timedelta(hours=3), {"climate.a": _points(_NOW - timedelta(hours=3), 55.0)}, primary="climate.a")
    assert set(arch.data["climate.a"][slot]) >= {"1h", "4h"}


def test_missing_lead_point_is_skipped_not_invented(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    short = _points(_NOW, 68.0, hours=2)  # only reaches 2 h ahead
    arch.record(_NOW, {"climate.a": short}, primary="climate.a")
    values = {lead for slot in arch.data["climate.a"].values() for lead in slot}
    assert values == {"1h"}


def test_save_and_load_round_trip_atomic_and_private(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")
    arch.save()
    assert arch.dirty is False
    assert not list(tmp_path.glob("*.tmp")), "atomic write left a temp file behind"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(arch._path).st_mode) == 0o600
    raw = json.loads(arch._path.read_text())
    assert raw["leads"] == ["1h", "4h", "12h", "24h", "48h", "72h", "120h"] and set(raw["sensors"]) == {"climate.a"}
    assert raw["ca"]["primary"] == "climate.a"  # "_ca" is derived on read, not stored twice

    reloaded = _archive(tmp_path)
    reloaded.load()
    assert reloaded.data == arch.data
    assert reloaded.dirty is False


@pytest.mark.parametrize(
    "content",
    ["", "not json{", "[]", '{"sensors": 5, "leads": []}', '{"sensors": {"_ca": {"x": [1]}}, "leads": ["1h"]}'],
)
def test_corrupt_or_odd_file_starts_empty(tmp_path: Path, content: str) -> None:
    arch = _archive(tmp_path)
    arch._path.write_text(content, encoding="utf-8")
    arch.load()  # must not raise
    assert arch.data == {}
    # and it is usable afterwards
    assert arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a") > 0


def test_missing_file_starts_empty(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    arch.load()
    assert arch.data == {}


def test_load_drops_expired_and_non_finite(tmp_path: Path) -> None:
    now_ts = datetime.now(UTC).timestamp()
    old = int(now_ts - (V4_ARCHIVE_MAX_DAYS + 5) * 86400)
    fresh = int(now_ts - 3600)
    payload = {
        "v": 1,
        "leads": ["1h", "4h", "12h", "24h"],
        "sensors": {"_ca": {str(old): [1, 2, 3, 4], str(fresh): [68.0, None, "x", True]}, "empty": {str(old): [1]}},
    }
    arch = _archive(tmp_path)
    arch._path.write_text(json.dumps(payload), encoding="utf-8")
    arch.load()
    assert arch.data == {"_ca": {fresh: {"1h": 68.0}}}


def test_retention_prunes_old_slots_once_an_hour(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    ancient = _NOW - timedelta(days=V4_ARCHIVE_MAX_DAYS + 10)
    old = int(ancient.timestamp())
    arch.put("climate.a", old, "4h", 60.0)
    arch.put("gone", old, "4h", 1.0)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")
    assert old not in arch.data["climate.a"]
    assert "gone" not in arch.data  # a sensor line with nothing left is dropped
    # a second record inside the hour does not prune again (cheap path)
    arch.put("climate.a", old, "4h", 60.0)
    arch.record(_NOW + timedelta(minutes=30), {"climate.a": _points(_NOW, 70.0)}, primary="climate.a")
    assert old in arch.data["climate.a"]
    arch.record(_NOW + timedelta(hours=2), {"climate.a": _points(_NOW + timedelta(hours=2), 70.0)}, primary="climate.a")
    assert old not in arch.data["climate.a"]


def test_save_failure_is_logged_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = _archive(tmp_path)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("custom_components.climate_advisor.v4_archive.os.replace", boom)
    arch.save()  # must not raise
    assert arch.dirty is True  # still dirty: the next save retries
    assert not list(tmp_path.glob("*.tmp"))


def test_record_never_raises_on_bad_input(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    assert arch.record(_NOW, {"climate.a": [("not a datetime", 1.0)]}, primary="climate.a") == 0  # type: ignore[list-item]


def test_series_reads_a_window(tmp_path: Path) -> None:
    arch = _archive(tmp_path)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")
    lo = (_NOW + timedelta(hours=3)).timestamp()
    hi = (_NOW + timedelta(hours=5)).timestamp()
    got = arch.series("climate.a", "4h", lo, hi)
    assert list(got) == [slot_key(_NOW + timedelta(hours=4))]
    assert arch.series("nope", "4h", lo, hi) == {}


def test_oldest_slot_is_the_earliest_slot_across_sensors_or_none_when_empty(tmp_path: Path) -> None:
    """Issue #1399: the Debug tab says "N slots since <date>" from this."""
    arch = _archive(tmp_path)
    assert arch.oldest_slot() is None
    t0 = int(_NOW.timestamp()) // 1800 * 1800
    arch.put("climate.a", t0, "4h", 60.0)
    arch.put("sensor.b", t0 - 3600, "1h", 61.0)
    assert arch.oldest_slot() == t0 - 3600
