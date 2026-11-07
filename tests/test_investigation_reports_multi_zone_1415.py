"""AI investigation history is kept per zone (Issue #1415).

Occupant view: with two zones, running an AI investigation in one zone must never make the other zone's
reports vanish from History, and each zone's History shows only that zone's reports. Before the fix both
zones wrote their whole in-memory list to one shared file, so the last zone to save erased the others.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor.const import (
    ENTRY_SCOPED_FILES,
    INVESTIGATION_REPORTS_FILE,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.storage_paths import (
    remove_entry_scoped_files,
    resolve_entry_scoped_path,
)

ZONES = ("zoneA", "zoneB")


def _zone(config_dir: Path, entry_id: str, all_ids=ZONES) -> ClimateAdvisorCoordinator:
    """A coordinator with only what the report store/load/delete paths touch."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    hass = MagicMock()
    hass.config.config_dir = str(config_dir)
    hass.config.path = lambda *parts: str(Path(config_dir, *parts))
    hass.config_entries.async_entries = MagicMock(return_value=[MagicMock(entry_id=e) for e in all_ids])
    coord.hass = hass
    coord._entry_id = entry_id
    coord._investigation_report_history = []

    async def _executor_job(fn, *args):
        return fn(*args)

    coord._executor_job = _executor_job
    return coord


def _load(coord: ClimateAdvisorCoordinator) -> list[dict]:
    asyncio.run(coord.async_load_investigation_reports())
    return coord.get_investigation_report_history()


def _store(coord: ClimateAdvisorCoordinator, summary: str) -> None:
    asyncio.run(coord.async_store_investigation_report({"summary": summary}))


def _summaries(reports: list[dict]) -> list[str]:
    return [r["result"]["summary"] for r in reports]


def _entry(summary: str, days_ago: float = 0.0) -> dict:
    ts = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()
    return {"timestamp": ts, "report_type": "investigation", "result": {"summary": summary}}


def test_two_zones_saving_in_turn_keep_both_reports(tmp_path: Path) -> None:
    """The reproduction: A stores, B stores, reload; A's report must still be in A's History."""
    zone_a, zone_b = _zone(tmp_path, "zoneA"), _zone(tmp_path, "zoneB")
    _load(zone_a)
    _load(zone_b)

    _store(zone_a, "A report")
    _store(zone_b, "B report")

    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["A report"]
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["B report"]


def test_each_zone_writes_its_own_file(tmp_path: Path) -> None:
    zone_a = _zone(tmp_path, "zoneA")
    _load(zone_a)
    _store(zone_a, "A report")
    assert resolve_entry_scoped_path(tmp_path, INVESTIGATION_REPORTS_FILE, "zoneA").exists()
    assert not (tmp_path / INVESTIGATION_REPORTS_FILE).exists()
    assert not resolve_entry_scoped_path(tmp_path, INVESTIGATION_REPORTS_FILE, "zoneB").exists()


def test_delete_in_one_zone_leaves_the_other_zone_alone(tmp_path: Path) -> None:
    """Mirror of the store path: delete then save (api.py delete_report) only rewrites its own zone."""
    zone_a, zone_b = _zone(tmp_path, "zoneA"), _zone(tmp_path, "zoneB")
    _load(zone_a)
    _load(zone_b)
    _store(zone_a, "A1")
    _store(zone_a, "A2")
    _store(zone_b, "B1")

    target = zone_a.get_investigation_report_history()[0]["timestamp"]
    assert zone_a.delete_investigation_report(target) is True
    zone_a._save_investigation_reports()
    assert zone_a.delete_investigation_report("no-such-timestamp") is False

    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["A2"]
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["B1"]


def test_shared_file_is_migrated_to_every_zone_and_removed(tmp_path: Path) -> None:
    """The old file has no zone tag, so no report can be attributed; each zone keeps all of them.

    That is what each zone's History already showed after a restart before the fix, so nothing the
    occupant could see is lost. Reports older than 30 days are dropped by the normal load filter.
    """
    legacy = tmp_path / INVESTIGATION_REPORTS_FILE
    legacy.write_text(json.dumps([_entry("old 1", 3), _entry("old 2", 1), _entry("stale", 40)]), encoding="utf-8")

    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["old 1", "old 2"]
    assert not legacy.exists()
    # The other zone's file was written in the same pass, so it does not depend on start order.
    assert resolve_entry_scoped_path(tmp_path, INVESTIGATION_REPORTS_FILE, "zoneB").exists()
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["old 1", "old 2"]

    # Second start: nothing left to migrate; a zone's own later changes are kept.
    zone_a = _zone(tmp_path, "zoneA")
    _load(zone_a)
    _store(zone_a, "A new")
    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["old 1", "old 2", "A new"]
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["old 1", "old 2"]


def test_migration_never_overwrites_an_existing_zone_file(tmp_path: Path) -> None:
    own = resolve_entry_scoped_path(tmp_path, INVESTIGATION_REPORTS_FILE, "zoneB")
    own.write_text(json.dumps([_entry("B own")]), encoding="utf-8")
    (tmp_path / INVESTIGATION_REPORTS_FILE).write_text(json.dumps([_entry("old")]), encoding="utf-8")

    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["old"]
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["B own"]
    assert not (tmp_path / INVESTIGATION_REPORTS_FILE).exists()


@pytest.mark.parametrize("content", ["{not json", json.dumps({"a": 1})])
def test_corrupt_shared_file_starts_fresh_and_is_removed(tmp_path: Path, content: str) -> None:
    legacy = tmp_path / INVESTIGATION_REPORTS_FILE
    legacy.write_text(content, encoding="utf-8")
    assert _load(_zone(tmp_path, "zoneA")) == []
    assert not legacy.exists()
    zone_a = _zone(tmp_path, "zoneA")
    _load(zone_a)
    _store(zone_a, "A report")
    assert _summaries(_load(_zone(tmp_path, "zoneA"))) == ["A report"]


def test_missing_files_start_fresh(tmp_path: Path) -> None:
    assert _load(_zone(tmp_path, "zoneA")) == []
    assert list(tmp_path.iterdir()) == []


def test_no_entry_id_keeps_the_unscoped_file(tmp_path: Path) -> None:
    """Harness/test contexts with no config entry keep using the plain name; nothing to migrate."""
    legacy = tmp_path / INVESTIGATION_REPORTS_FILE
    legacy.write_text(json.dumps([_entry("only")]), encoding="utf-8")
    assert _summaries(_load(_zone(tmp_path, "", all_ids=()))) == ["only"]
    assert legacy.exists()


def test_zone_list_unavailable_migrates_only_this_zone_and_keeps_the_shared_file(tmp_path: Path) -> None:
    legacy = tmp_path / INVESTIGATION_REPORTS_FILE
    legacy.write_text(json.dumps([_entry("old")]), encoding="utf-8")
    coord = _zone(tmp_path, "zoneA")
    coord.hass.config_entries.async_entries = MagicMock(side_effect=RuntimeError("no registry"))
    assert _summaries(_load(coord)) == ["old"]
    # Other zones are unknown, so the shared file stays for them.
    assert legacy.exists()


def test_zone_removal_deletes_its_reports_file_only(tmp_path: Path) -> None:
    for zone in ZONES:
        coord = _zone(tmp_path, zone)
        _load(coord)
        _store(coord, f"{zone} report")
    assert INVESTIGATION_REPORTS_FILE in ENTRY_SCOPED_FILES

    remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES)

    assert not resolve_entry_scoped_path(tmp_path, INVESTIGATION_REPORTS_FILE, "zoneA").exists()
    assert _summaries(_load(_zone(tmp_path, "zoneB"))) == ["zoneB report"]


def test_concurrent_zone_starts_migrate_once(tmp_path: Path) -> None:
    """Both zones' loads run in executor threads; the split must happen once, under a lock."""
    import threading

    (tmp_path / INVESTIGATION_REPORTS_FILE).write_text(json.dumps([_entry("old")]), encoding="utf-8")
    zones = [_zone(tmp_path, z) for z in ZONES]
    results: dict[str, list[str]] = {}

    def _run(c):
        results[c._entry_id] = _summaries(_load(c))

    threads = [threading.Thread(target=_run, args=(c,)) for c in zones]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"zoneA": ["old"], "zoneB": ["old"]}
    assert not (tmp_path / INVESTIGATION_REPORTS_FILE).exists()
    assert isinstance(coord_mod._INVESTIGATION_MIGRATION_LOCK, type(threading.Lock()))
