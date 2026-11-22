"""StatePersistence failure paths (Silver coverage).

Occupant view: when the disk misbehaves, Climate Advisor keeps the last good saved state (so a restart does not
forget today's record and classification), cleans up its temp files, and never crashes the integration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from custom_components.climate_advisor.const import STATE_FILE
from custom_components.climate_advisor.state import STATE_VERSION, StatePersistence


def _tmp_files(path: Path) -> list[Path]:
    return list(path.glob("climate_advisor_state_*.tmp"))


class TestSaveFailures:
    def test_unserializable_state_is_not_written(self, tmp_path: Path, caplog):
        sp = StatePersistence(tmp_path)
        state: dict = {"a": 1}
        state["self"] = state  # circular -> ValueError from json.dumps
        with caplog.at_level(logging.ERROR):
            sp.save(state)
        assert not (tmp_path / STATE_FILE).exists()
        assert _tmp_files(tmp_path) == []
        assert "Failed to serialize state" in caplog.text

    def test_replace_failure_keeps_previous_file_and_removes_tmp(self, tmp_path: Path, monkeypatch, caplog):
        sp = StatePersistence(tmp_path)
        sp.save({"day": "old"})

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with caplog.at_level(logging.ERROR):
            sp.save({"day": "new"})
        monkeypatch.undo()

        assert sp.load()["day"] == "old"
        assert _tmp_files(tmp_path) == []
        assert "Failed to save state file" in caplog.text

    def test_save_without_chmod_still_writes(self, tmp_path: Path, monkeypatch):
        monkeypatch.delattr(os, "chmod")
        sp = StatePersistence(tmp_path)
        sp.save({"day": "x"})
        monkeypatch.undo()
        assert sp.load() == {"day": "x", "version": STATE_VERSION}


class TestDeleteFailure:
    def test_delete_oserror_is_logged_not_raised(self, tmp_path: Path, caplog):
        sp = StatePersistence(tmp_path)
        (tmp_path / STATE_FILE).mkdir()  # unlink() on a directory raises an OSError
        with caplog.at_level(logging.WARNING):
            sp.delete()
        assert "Failed to delete state file" in caplog.text
        assert (tmp_path / STATE_FILE).is_dir()


class TestDeleteLeftovers:
    def test_delete_removes_leftover_tmp_files(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        sp.save({"day": "x"})
        leftover = tmp_path / "climate_advisor_state_crashed.tmp"
        leftover.write_text("partial", encoding="utf-8")
        sp.delete()
        assert not (tmp_path / STATE_FILE).exists()
        assert not leftover.exists()
