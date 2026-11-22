"""storage_paths failure paths, zone removal and the shared-list split (Issues #796, #1415; Silver coverage).

Occupant view: upgrading or removing a zone never loses another zone's learning, state or AI history, and a disk
error leaves the old file readable so the next start can try again.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from custom_components.climate_advisor.const import STATE_FILE
from custom_components.climate_advisor.storage_paths import (
    migrate_legacy_storage_file,
    remove_entry_scoped_files,
    resolve_entry_scoped_path,
    split_shared_list_file,
)

HISTORY = "climate_advisor_investigations.json"


def _boom(*_a, **_k):
    raise OSError("disk error")


def _tmp_files(path: Path) -> list[Path]:
    return list(path.glob("*.tmp"))


def _unlink_failing_for(monkeypatch, target: Path, exc: type[OSError]) -> None:
    real_unlink = Path.unlink

    def _unlink(self, *a, **k):
        if self == target:
            raise exc("refused")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _unlink)


class TestMigrateFailures:
    def test_unreadable_legacy_is_left_and_nothing_written(self, tmp_path: Path, caplog):
        (tmp_path / STATE_FILE).mkdir()  # exists, but read_bytes() raises
        with caplog.at_level(logging.WARNING):
            migrate_legacy_storage_file(tmp_path, STATE_FILE, "e1")
        assert not resolve_entry_scoped_path(tmp_path, STATE_FILE, "e1").exists()
        assert "failed to read legacy file" in caplog.text

    def test_write_failure_keeps_legacy_and_removes_tmp(self, tmp_path: Path, monkeypatch, caplog):
        legacy = tmp_path / STATE_FILE
        legacy.write_text('{"version": 1}', encoding="utf-8")
        monkeypatch.setattr(os, "replace", _boom)
        with caplog.at_level(logging.ERROR):
            migrate_legacy_storage_file(tmp_path, STATE_FILE, "e1")
        assert legacy.read_text(encoding="utf-8") == '{"version": 1}'
        assert not resolve_entry_scoped_path(tmp_path, STATE_FILE, "e1").exists()
        assert _tmp_files(tmp_path) == []
        assert "failed to write migrated file" in caplog.text

    def test_legacy_unlink_failure_leaves_both_files(self, tmp_path: Path, monkeypatch, caplog):
        legacy = tmp_path / STATE_FILE
        legacy.write_text("data", encoding="utf-8")
        _unlink_failing_for(monkeypatch, legacy, PermissionError)
        with caplog.at_level(logging.WARNING):
            migrate_legacy_storage_file(tmp_path, STATE_FILE, "e1")
        assert resolve_entry_scoped_path(tmp_path, STATE_FILE, "e1").read_text(encoding="utf-8") == "data"
        assert legacy.exists()
        assert "failed to remove legacy file" in caplog.text

    def test_windows_skips_chmod(self, tmp_path: Path, monkeypatch):
        (tmp_path / STATE_FILE).write_text("data", encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "chmod", _boom)  # would fail the migration if called
        migrate_legacy_storage_file(tmp_path, STATE_FILE, "e1")
        assert resolve_entry_scoped_path(tmp_path, STATE_FILE, "e1").read_text(encoding="utf-8") == "data"
        assert not (tmp_path / STATE_FILE).exists()


class TestRemoveEntryScopedFiles:
    def test_no_entry_id_deletes_nothing(self, tmp_path: Path, caplog):
        (tmp_path / STATE_FILE).write_text("shared", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert remove_entry_scoped_files(tmp_path, "", (STATE_FILE,)) == []
        assert (tmp_path / STATE_FILE).exists()
        assert "no entry id" in caplog.text

    def test_deletes_only_this_zones_files_skipping_missing_and_failed(self, tmp_path: Path, caplog):
        own = resolve_entry_scoped_path(tmp_path, STATE_FILE, "e1")
        own.write_text("mine", encoding="utf-8")
        other = resolve_entry_scoped_path(tmp_path, STATE_FILE, "e2")
        other.write_text("theirs", encoding="utf-8")
        stuck = resolve_entry_scoped_path(tmp_path, "climate_advisor_learning.json", "e1")
        stuck.mkdir()  # unlink() raises a non-FileNotFound OSError
        with caplog.at_level(logging.INFO):
            removed = remove_entry_scoped_files(
                tmp_path, "e1", (STATE_FILE, "climate_advisor_learning.json", "climate_advisor_missing.json")
            )
        assert removed == [own.name]
        assert not own.exists()
        assert other.exists()
        assert stuck.exists()
        assert "failed to delete" in caplog.text

    def test_nothing_present_logs_none(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.INFO):
            assert remove_entry_scoped_files(tmp_path, "e1", (STATE_FILE,)) == []
        assert "deleted 0 file(s): none" in caplog.text


class TestSplitSharedListFile:
    def _zone(self, tmp_path: Path, entry_id: str) -> Path:
        return resolve_entry_scoped_path(tmp_path, HISTORY, entry_id)

    def test_missing_shared_file_is_noop(self, tmp_path: Path):
        split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert list(tmp_path.iterdir()) == []

    def test_every_new_zone_gets_the_full_list_and_shared_is_removed(self, tmp_path: Path):
        (tmp_path / HISTORY).write_text(json.dumps([{"q": 1}, {"q": 2}]), encoding="utf-8")
        self._zone(tmp_path, "e2").write_text("[]", encoding="utf-8")  # existing zone file kept
        split_shared_list_file(tmp_path, HISTORY, ["e1", "", "e2"])
        assert json.loads(self._zone(tmp_path, "e1").read_text(encoding="utf-8")) == [{"q": 1}, {"q": 2}]
        assert self._zone(tmp_path, "e2").read_text(encoding="utf-8") == "[]"
        assert not (tmp_path / HISTORY).exists()

    def test_unknown_zones_keep_the_shared_file(self, tmp_path: Path):
        (tmp_path / HISTORY).write_text("[1]", encoding="utf-8")
        split_shared_list_file(tmp_path, HISTORY, ["e1"], all_zones_known=False)
        assert json.loads(self._zone(tmp_path, "e1").read_text(encoding="utf-8")) == [1]
        assert (tmp_path / HISTORY).exists()

    def test_write_failure_keeps_shared_file_and_removes_tmp(self, tmp_path: Path, monkeypatch, caplog):
        (tmp_path / HISTORY).write_text("[1]", encoding="utf-8")
        monkeypatch.setattr(os, "replace", _boom)
        with caplog.at_level(logging.ERROR):
            split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert (tmp_path / HISTORY).exists()
        assert not self._zone(tmp_path, "e1").exists()
        assert _tmp_files(tmp_path) == []
        assert "failed to write" in caplog.text

    def test_mkstemp_failure_has_no_tmp_to_remove(self, tmp_path: Path, monkeypatch):
        import tempfile

        (tmp_path / HISTORY).write_text("[1]", encoding="utf-8")
        monkeypatch.setattr(tempfile, "mkstemp", _boom)
        split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert (tmp_path / HISTORY).exists()
        assert not self._zone(tmp_path, "e1").exists()

    def test_windows_skips_chmod(self, tmp_path: Path, monkeypatch):
        (tmp_path / HISTORY).write_text("[1]", encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "chmod", _boom)
        split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert json.loads(self._zone(tmp_path, "e1").read_text(encoding="utf-8")) == [1]

    def test_invalid_json_is_removed_and_zones_start_fresh(self, tmp_path: Path, caplog):
        (tmp_path / HISTORY).write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            split_shared_list_file(tmp_path, HISTORY, ["e1"], all_zones_known=False)
        assert not (tmp_path / HISTORY).exists()
        assert not self._zone(tmp_path, "e1").exists()
        assert "is unreadable" in caplog.text

    def test_non_list_is_removed_and_zones_start_fresh(self, tmp_path: Path, caplog):
        (tmp_path / HISTORY).write_text('{"a": 1}', encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert not (tmp_path / HISTORY).exists()
        assert not self._zone(tmp_path, "e1").exists()
        assert "unexpected format" in caplog.text

    def test_shared_file_vanishing_before_read_is_noop(self, tmp_path: Path, monkeypatch):
        legacy = tmp_path / HISTORY
        legacy.write_text("[1]", encoding="utf-8")
        real_read = Path.read_text

        def _read(self, *a, **k):
            if self == legacy:
                raise FileNotFoundError("raced")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _read)
        split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert legacy.exists()
        assert not self._zone(tmp_path, "e1").exists()

    def test_shared_file_already_removed_by_another_zone(self, tmp_path: Path, monkeypatch, caplog):
        legacy = tmp_path / HISTORY
        legacy.write_text("[1]", encoding="utf-8")
        _unlink_failing_for(monkeypatch, legacy, FileNotFoundError)
        with caplog.at_level(logging.INFO):
            split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert self._zone(tmp_path, "e1").exists()
        assert "and removed it" not in caplog.text

    def test_shared_file_remove_failure_is_logged(self, tmp_path: Path, monkeypatch, caplog):
        legacy = tmp_path / HISTORY
        legacy.write_text("[1]", encoding="utf-8")
        _unlink_failing_for(monkeypatch, legacy, PermissionError)
        with caplog.at_level(logging.WARNING):
            split_shared_list_file(tmp_path, HISTORY, ["e1"])
        assert legacy.exists()
        assert self._zone(tmp_path, "e1").exists()
        assert "failed to remove" in caplog.text
