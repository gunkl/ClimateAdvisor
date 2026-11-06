"""Deleting a zone removes its own storage files and nothing else.

Occupant view: after deleting a zone, its state, learning, chart log and forecast archive files are gone from
the HA config dir (several MB per zone on an SD card), while every other zone's files, and a legacy unscoped
file that may still belong to an unmigrated entry, stay untouched. A reload or settings save never deletes
anything: only ``async_remove_entry`` calls ``remove_entry_scoped_files``.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from custom_components.climate_advisor.chart_log import ChartStateLog
from custom_components.climate_advisor.const import (
    CHART_LOG_FILE,
    ENTRY_SCOPED_FILES,
    FORECAST_ARCHIVE_FILE,
    LEARNING_DB_FILE,
    STATE_FILE,
)
from custom_components.climate_advisor.forecast_archive import ForecastArchive
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.state import StatePersistence
from custom_components.climate_advisor.storage_paths import (
    remove_entry_scoped_files,
    resolve_entry_scoped_path,
)

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_ALL_BASES = (STATE_FILE, CHART_LOG_FILE, LEARNING_DB_FILE, FORECAST_ARCHIVE_FILE)


def _write_zone(config_dir: Path, entry_id: str) -> list[Path]:
    paths = [resolve_entry_scoped_path(config_dir, base, entry_id) for base in _ALL_BASES]
    for p in paths:
        p.write_text("{}", encoding="utf-8")
    return paths


def test_removes_every_file_of_the_removed_zone(tmp_path: Path) -> None:
    paths = _write_zone(tmp_path, "zoneA")
    removed = remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES)
    assert sorted(removed) == sorted(p.name for p in paths)
    assert not any(p.exists() for p in paths)


def test_other_zone_and_legacy_files_are_kept(tmp_path: Path) -> None:
    _write_zone(tmp_path, "zoneA")
    kept = _write_zone(tmp_path, "zoneB")
    legacy = [tmp_path / base for base in _ALL_BASES]
    for p in legacy:
        p.write_text("{}", encoding="utf-8")
    unrelated = tmp_path / "climate_advisor_investigation_reports.json"
    unrelated.write_text("[]", encoding="utf-8")
    tmp_leftover = tmp_path / "climate_advisor_state_zoneA_x1.tmp"
    tmp_leftover.write_text("", encoding="utf-8")

    remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES)

    assert all(p.exists() for p in kept)
    assert all(p.exists() for p in legacy)
    assert unrelated.exists()
    assert tmp_leftover.exists()  # mkstemp names cannot be tied to one zone; documented, not deleted


def test_missing_files_are_skipped(tmp_path: Path) -> None:
    only = resolve_entry_scoped_path(tmp_path, CHART_LOG_FILE, "zoneA")
    only.write_text("{}", encoding="utf-8")
    assert remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES) == [only.name]
    assert remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES) == []


@pytest.mark.parametrize("entry_id", ["", None])
def test_no_entry_id_deletes_nothing(tmp_path: Path, entry_id) -> None:
    """A falsy id resolves to the shared unscoped names; deleting those could wipe another zone's data."""
    legacy = [tmp_path / base for base in _ALL_BASES]
    for p in legacy:
        p.write_text("{}", encoding="utf-8")
    assert remove_entry_scoped_files(tmp_path, entry_id, ENTRY_SCOPED_FILES) == []
    assert all(p.exists() for p in legacy)


def test_one_failed_delete_does_not_stop_the_rest(tmp_path: Path, monkeypatch) -> None:
    paths = _write_zone(tmp_path, "zoneA")
    blocked = paths[0]
    real_unlink = Path.unlink

    def _unlink(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)
    removed = remove_entry_scoped_files(tmp_path, "zoneA", ENTRY_SCOPED_FILES)
    assert blocked.exists()
    assert sorted(removed) == sorted(p.name for p in paths[1:])


def test_targets_the_paths_the_real_storage_classes_write(tmp_path: Path) -> None:
    """The deleted names are the ones each storage class actually writes, not a parallel guess."""
    entry_id = "zoneA"
    written = [
        StatePersistence(tmp_path, entry_id=entry_id)._path,
        ChartStateLog(tmp_path, entry_id=entry_id)._path,
        LearningEngine(tmp_path, entry_id=entry_id)._db_path,
        ForecastArchive(tmp_path, entry_id=entry_id)._path,
    ]
    for p in written:
        p.write_text("{}", encoding="utf-8")
    remove_entry_scoped_files(tmp_path, entry_id, ENTRY_SCOPED_FILES)
    assert not any(p.exists() for p in written)


def test_every_entry_scoped_file_in_the_package_is_cleaned_up() -> None:
    """Structural guard: a new resolve_entry_scoped_path() caller must add its base name to ENTRY_SCOPED_FILES."""
    found: dict[str, str] = {}
    for src in sorted(_PKG.glob("*.py")):
        if src.name == "storage_paths.py":
            continue  # the helper's own internal use (migration) is not a storage file
        tree = ast.parse(src.read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "resolve_entry_scoped_path"
        ]
        if not calls:
            continue  # only import modules that resolve a path (platform modules need HA platforms)
        module = importlib.import_module(f"custom_components.climate_advisor.{src.stem}")
        for node in calls:
            base_arg = node.args[1]
            assert isinstance(base_arg, ast.Name), f"{src.name}:{node.lineno}: base name must be a named constant"
            found[f"{src.name}:{node.lineno}"] = getattr(module, base_arg.id)
    assert found, "no resolve_entry_scoped_path() callers found; the guard is not looking at the package"
    missing = {where: base for where, base in found.items() if base not in ENTRY_SCOPED_FILES}
    assert not missing, f"entry-scoped files a removed zone would leave behind: {missing}"
