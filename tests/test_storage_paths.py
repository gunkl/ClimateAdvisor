"""Tests for entry-scoped storage path resolution and legacy-file migration.

Covers:
- resolve_entry_scoped_path() against the three real storage filenames
  (STATE_FILE, LEARNING_DB_FILE, _CHART_LOG_FILE)
- Empty entry_id falls back to the legacy unscoped path
- migrate_legacy_storage_file() idempotency, no-op conditions, and
  interrupted-migration safety (never leaves neither file readable)
- Integration: StatePersistence/ChartStateLog/LearningEngine construct with
  entry-scoped paths and self-migrate a legacy file on first load
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.climate_advisor.chart_log import _CHART_LOG_FILE, ChartStateLog
from custom_components.climate_advisor.const import LEARNING_DB_FILE, STATE_FILE
from custom_components.climate_advisor.learning import LearningEngine
from custom_components.climate_advisor.state import StatePersistence
from custom_components.climate_advisor.storage_paths import (
    migrate_legacy_storage_file,
    resolve_entry_scoped_path,
)

# ---------------------------------------------------------------------------
# resolve_entry_scoped_path()
# ---------------------------------------------------------------------------


class TestResolveEntryScopedPath:
    def test_state_file_scoped(self, tmp_path: Path):
        result = resolve_entry_scoped_path(tmp_path, STATE_FILE, "abc123")
        assert result == tmp_path / "climate_advisor_state_abc123.json"

    def test_learning_db_file_scoped(self, tmp_path: Path):
        result = resolve_entry_scoped_path(tmp_path, LEARNING_DB_FILE, "abc123")
        assert result == tmp_path / "climate_advisor_learning_abc123.json"

    def test_chart_log_file_scoped(self, tmp_path: Path):
        result = resolve_entry_scoped_path(tmp_path, _CHART_LOG_FILE, "abc123")
        assert result == tmp_path / "climate_advisor_chart_log_abc123.json"

    def test_empty_entry_id_falls_back_to_unscoped(self, tmp_path: Path):
        """Empty entry_id (harness/test contexts with no real config entry)
        must return the plain legacy path, not append a bare underscore —
        ~90 existing tests and the simulation harness depend on this."""
        assert resolve_entry_scoped_path(tmp_path, STATE_FILE, "") == tmp_path / STATE_FILE
        assert resolve_entry_scoped_path(tmp_path, LEARNING_DB_FILE, "") == tmp_path / LEARNING_DB_FILE
        assert resolve_entry_scoped_path(tmp_path, _CHART_LOG_FILE, "") == tmp_path / _CHART_LOG_FILE

    def test_different_entry_ids_produce_different_paths(self, tmp_path: Path):
        """The actual bug this fixes: two zones must never resolve to the same file."""
        p1 = resolve_entry_scoped_path(tmp_path, STATE_FILE, "zone_a")
        p2 = resolve_entry_scoped_path(tmp_path, STATE_FILE, "zone_b")
        assert p1 != p2

    def test_real_ha_entry_id_format(self, tmp_path: Path):
        """HA entry_ids are lowercase hex (e.g. from ulid/uuid) — confirm no
        collision with the '.' split logic for a realistic value."""
        entry_id = "01j8x9z3k4m5n6p7q8r9s0t1u2"
        result = resolve_entry_scoped_path(tmp_path, LEARNING_DB_FILE, entry_id)
        assert result.name == f"climate_advisor_learning_{entry_id}.json"


# ---------------------------------------------------------------------------
# migrate_legacy_storage_file()
# ---------------------------------------------------------------------------


class TestMigrateLegacyStorageFile:
    def test_migrates_legacy_file_to_entry_scoped_name(self, tmp_path: Path):
        legacy = tmp_path / STATE_FILE
        legacy.write_text('{"hello": "world"}', encoding="utf-8")

        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")

        new_path = tmp_path / "climate_advisor_state_zone1.json"
        assert new_path.exists()
        assert not legacy.exists()
        assert json.loads(new_path.read_text(encoding="utf-8")) == {"hello": "world"}

    def test_noop_when_entry_id_empty(self, tmp_path: Path):
        legacy = tmp_path / STATE_FILE
        legacy.write_text("{}", encoding="utf-8")

        migrate_legacy_storage_file(tmp_path, STATE_FILE, "")

        # Nothing to migrate to (resolve_entry_scoped_path("") == legacy path itself)
        assert legacy.exists()

    def test_noop_when_legacy_file_missing(self, tmp_path: Path):
        # No legacy file at all — fresh install, nothing to do.
        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")
        assert not (tmp_path / "climate_advisor_state_zone1.json").exists()

    def test_idempotent_second_call_is_noop(self, tmp_path: Path):
        legacy = tmp_path / STATE_FILE
        legacy.write_text('{"v": 1}', encoding="utf-8")

        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")
        new_path = tmp_path / "climate_advisor_state_zone1.json"
        assert new_path.exists()

        # Second call: legacy is already gone, so this must not raise or alter the new file.
        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")
        assert json.loads(new_path.read_text(encoding="utf-8")) == {"v": 1}

    def test_noop_when_entry_scoped_file_already_exists(self, tmp_path: Path):
        """Simulates a second zone (or a re-run) where migration already happened —
        must not overwrite the entry-scoped file even if a legacy file reappears
        (e.g. a second, never-migrated pre-fix zone writing into the old shared name)."""
        new_path = tmp_path / "climate_advisor_state_zone1.json"
        new_path.write_text('{"already": "migrated"}', encoding="utf-8")
        legacy = tmp_path / STATE_FILE
        legacy.write_text('{"stale": "data"}', encoding="utf-8")

        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")

        assert json.loads(new_path.read_text(encoding="utf-8")) == {"already": "migrated"}
        # Legacy file is left alone (not this migration's job to clean up
        # once the destination already exists).
        assert legacy.exists()

    def test_never_leaves_neither_file_readable_on_write_failure(self, tmp_path: Path, monkeypatch):
        """If the write-to-tmp step fails, the legacy file must remain intact —
        an interrupted migration must never leave a user with no readable file."""
        legacy = tmp_path / STATE_FILE
        legacy.write_text('{"important": "data"}', encoding="utf-8")

        import os as os_module

        real_replace = os_module.replace

        def _boom(*args, **kwargs):
            raise OSError("simulated disk failure during migration")

        monkeypatch.setattr(os_module, "replace", _boom)

        migrate_legacy_storage_file(tmp_path, STATE_FILE, "zone1")

        monkeypatch.setattr(os_module, "replace", real_replace)

        # Legacy file must still be there and readable; new file must not exist.
        assert legacy.exists()
        assert json.loads(legacy.read_text(encoding="utf-8")) == {"important": "data"}
        assert not (tmp_path / "climate_advisor_state_zone1.json").exists()


# ---------------------------------------------------------------------------
# Integration: the three real classes construct entry-scoped and self-migrate
# ---------------------------------------------------------------------------


class TestStatePersistenceEntryScoping:
    def test_default_entry_id_uses_legacy_path(self, tmp_path: Path):
        sp = StatePersistence(tmp_path)
        assert sp._path == tmp_path / STATE_FILE

    def test_explicit_entry_id_scopes_path(self, tmp_path: Path):
        sp = StatePersistence(tmp_path, entry_id="zone1")
        assert sp._path == tmp_path / "climate_advisor_state_zone1.json"

    def test_load_migrates_legacy_file_on_first_call(self, tmp_path: Path):
        legacy = tmp_path / STATE_FILE
        legacy.write_text(json.dumps({"version": 1, "date": "2026-01-01"}), encoding="utf-8")

        sp = StatePersistence(tmp_path, entry_id="zone1")
        state = sp.load()

        assert state["date"] == "2026-01-01"
        assert not legacy.exists()
        assert (tmp_path / "climate_advisor_state_zone1.json").exists()

    def test_two_zones_do_not_collide(self, tmp_path: Path):
        sp1 = StatePersistence(tmp_path, entry_id="zone1")
        sp2 = StatePersistence(tmp_path, entry_id="zone2")

        sp1.save({"date": "zone1-data"})
        sp2.save({"date": "zone2-data"})

        assert sp1.load()["date"] == "zone1-data"
        assert sp2.load()["date"] == "zone2-data"


class TestChartStateLogEntryScoping:
    def test_default_entry_id_uses_legacy_path(self, tmp_path: Path):
        log = ChartStateLog(tmp_path)
        assert log._path == tmp_path / _CHART_LOG_FILE

    def test_explicit_entry_id_scopes_path(self, tmp_path: Path):
        log = ChartStateLog(tmp_path, entry_id="zone1")
        assert log._path == tmp_path / "climate_advisor_chart_log_zone1.json"

    def test_load_migrates_legacy_file_on_first_call(self, tmp_path: Path):
        legacy = tmp_path / _CHART_LOG_FILE
        legacy.write_text(json.dumps({"entries": [{"ts": "2026-01-01T00:00:00+00:00"}]}), encoding="utf-8")

        log = ChartStateLog(tmp_path, entry_id="zone1", max_days=365)
        log.load()

        assert len(log._entries) == 1
        assert not legacy.exists()
        assert (tmp_path / "climate_advisor_chart_log_zone1.json").exists()


class TestLearningEngineEntryScoping:
    def test_default_entry_id_uses_legacy_path(self, tmp_path: Path):
        engine = LearningEngine(tmp_path)
        assert engine._db_path == tmp_path / LEARNING_DB_FILE

    def test_explicit_entry_id_scopes_path(self, tmp_path: Path):
        engine = LearningEngine(tmp_path, entry_id="zone1")
        assert engine._db_path == tmp_path / "climate_advisor_learning_zone1.json"

    def test_load_state_migrates_legacy_file_on_first_call(self, tmp_path: Path):
        legacy = tmp_path / LEARNING_DB_FILE
        legacy.write_text(json.dumps({"records": []}), encoding="utf-8")

        engine = LearningEngine(tmp_path, entry_id="zone1")
        engine.load_state()

        assert not legacy.exists()
        assert (tmp_path / "climate_advisor_learning_zone1.json").exists()

    def test_two_zones_have_independent_thermal_history(self, tmp_path: Path):
        """The occupant-facing outcome Gap 1 fixes: each zone's thermal model
        is independent instead of one zone's writes clobbering the other's."""
        e1 = LearningEngine(tmp_path, entry_id="zone1")
        e2 = LearningEngine(tmp_path, entry_id="zone2")

        e1._state.thermal_model_cache = {"k_passive": -0.5}
        e1.save_state()
        e2._state.thermal_model_cache = {"k_passive": -0.9}
        e2.save_state()

        e1_reload = LearningEngine(tmp_path, entry_id="zone1")
        e1_reload.load_state()
        e2_reload = LearningEngine(tmp_path, entry_id="zone2")
        e2_reload.load_state()

        assert e1_reload._state.thermal_model_cache == {"k_passive": -0.5}
        assert e2_reload._state.thermal_model_cache == {"k_passive": -0.9}
