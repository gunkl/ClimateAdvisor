"""Edge branches of the forecast archive (Silver test-coverage).

Occupant view: a damaged archive file or an odd forecast must never stop Climate Advisor starting or running its
cycle; the archive starts fresh and the house is controlled as usual.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import forecast_archive as fa
from custom_components.climate_advisor.forecast_archive import ForecastArchive


def _now_hour() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def test_parse_iso_rejects_non_strings() -> None:
    assert fa._parse_iso(None) is None and fa._parse_iso(123) is None


def test_load_unexpected_error_starts_fresh(tmp_path, monkeypatch, caplog) -> None:
    arch = ForecastArchive(tmp_path)
    arch._snapshots = [{"i": "x"}]

    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(arch, "_load", boom)
    with caplog.at_level(logging.WARNING):
        arch.load()
    assert arch._snapshots == []
    assert "unexpected error loading archive" in caplog.text


def test_load_of_only_expired_snapshots_logs_zero(tmp_path, caplog) -> None:
    old = (datetime.now(UTC) - timedelta(days=4000)).isoformat(timespec="minutes")
    arch = ForecastArchive(tmp_path)
    arch._path.write_text(json.dumps({"snapshots": [{"i": old, "s": "nws", "h0": old, "t": [60.0]}]}))
    with caplog.at_level(logging.INFO, logger=fa.__name__):
        arch.load()
    assert arch._snapshots == []
    assert "loaded 0 snapshots" in caplog.text


def test_save_unserializable_snapshot_is_logged_not_raised(tmp_path, caplog) -> None:
    arch = ForecastArchive(tmp_path)
    arch._snapshots = [{"i": object()}]
    with caplog.at_level(logging.ERROR):
        arch.save()
    assert not arch._path.exists()
    assert "failed to serialize" in caplog.text


def test_save_replace_failure_removes_the_temp_file(tmp_path, monkeypatch, caplog) -> None:
    arch = ForecastArchive(tmp_path)
    h = _now_hour()
    assert arch.record(h, "nws", [(h, 60.0)])

    def bad_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(fa.os, "replace", bad_replace)
    with caplog.at_level(logging.ERROR):
        arch.save()
    assert "failed to save archive file" in caplog.text
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_save_on_windows_skips_chmod(tmp_path, monkeypatch) -> None:
    arch = ForecastArchive(tmp_path)
    h = _now_hour()
    arch.record(h, "nws", [(h, 60.0)])
    monkeypatch.setattr(fa.sys, "platform", "win32")
    monkeypatch.setattr(fa.os, "chmod", lambda *a: pytest.fail("chmod on win32"))
    arch.save()
    assert json.loads(arch._path.read_text())["snapshots"][0]["s"] == "nws"


def test_get_snapshots_failure_returns_empty(tmp_path, caplog) -> None:
    arch = ForecastArchive(tmp_path)
    arch._snapshots = [{"no_i": 1}]  # a malformed in-memory snapshot (KeyError)
    with caplog.at_level(logging.WARNING):
        assert arch.get_snapshots(datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)) == []
    assert "get_snapshots failed" in caplog.text


def test_decode_with_bad_grid_start_has_no_hours() -> None:
    out = ForecastArchive._decode({"i": "2026-01-01T00:00+00:00", "s": "nws", "h0": "bad", "t": [60.0]})
    assert out == {"issued": "2026-01-01T00:00+00:00", "source": "nws", "hours": []}
