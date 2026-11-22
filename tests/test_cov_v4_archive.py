"""Edge branches of the v4 predictions archive and model store (Silver test-coverage).

Occupant view: the Prediction accuracy page scores what v4 said ahead of time. Odd stored data, a strange time-zone
name or a failing disk must never break the zone or invent a prediction; the archive and the model file start fresh
or keep what they had.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import predictor_v4
from custom_components.climate_advisor import v4_archive as va
from custom_components.climate_advisor.v4_archive import V4Archive, V4ModelStore, _Line, _parse_last, slot_key

_NOW = datetime(2026, 11, 6, 10, 0, 2, tzinfo=UTC)
E0 = slot_key(_NOW)


def _points(now: datetime, base: float, hours: int = 25) -> list[tuple[datetime, float]]:
    return [(now + timedelta(minutes=15 * k), base + 0.01 * k) for k in range(1, hours * 4 + 1)]


# ------------------------------------------------------------------------------------------------ _Line
def test_an_empty_line_answers_nothing_and_drops_nothing() -> None:
    line = _Line()
    assert line.get(E0, "1h") is None
    assert line.drop_before(E0 + 10**6) == 0 and line.drop_from(0) == 0


def test_an_unknown_lead_is_not_stored_and_a_late_cutoff_drops_nothing() -> None:
    line = _Line()
    assert line.put_if_absent(E0, "7h", 70.0) is False and line.base is None
    assert line.put_if_absent(E0, "1h", 70.0) is True
    assert line.drop_from(E0 + 3600) == 0 and line.get(E0, "1h") == 70.0


# ------------------------------------------------------------------------------------------------ sleep composite
def test_in_sleep_needs_times_and_a_real_zone_and_survives_a_broken_engine(tmp_path, monkeypatch) -> None:
    arch = V4Archive(tmp_path)
    arch.set_ca_source("climate.a", sleep="sensor.bed", sleep_time="22:00", wake_time="06:00")  # no tz
    assert arch._in_sleep(E0) is False
    arch.set_ca_source("climate.a", sleep="sensor.bed", sleep_time="22:00", wake_time="06:00", tz="Not/AZone")
    assert arch._in_sleep(E0) is False
    arch.set_ca_source("climate.a", sleep="sensor.bed", sleep_time="00:00", wake_time="23:59", tz="UTC")
    assert arch._in_sleep(E0) is True
    monkeypatch.setattr(predictor_v4, "in_sleep_window", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert arch._in_sleep(E0) is False


# ------------------------------------------------------------------------------------------------ legacy view
def test_data_skips_empty_lines_and_an_empty_derived_ca(tmp_path) -> None:
    arch = V4Archive(tmp_path)
    arch.data = {"climate.a": {}}  # a line with no values
    arch.set_ca_source("climate.a")
    assert arch._ca_active() is True
    assert arch.data == {} and arch.snapshot() == {}


# ------------------------------------------------------------------------------------------------ persistence
def test_load_unexpected_error_starts_fresh(tmp_path, monkeypatch, caplog) -> None:
    arch = V4Archive(tmp_path)
    arch.put("climate.a", E0, "1h", 70.0)
    monkeypatch.setattr(arch, "_load", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.WARNING):
        arch.load()
    assert arch.data == {} and "unexpected error loading archive" in caplog.text


def test_load_skips_a_sensor_whose_rows_are_not_an_object(tmp_path) -> None:
    arch = V4Archive(tmp_path)
    recent = int(datetime.now().timestamp()) // 1800 * 1800
    payload = {"v": 1, "leads": ["1h"], "sensors": {"climate.bad": [1, 2], "climate.a": {str(recent): [70.0]}}}
    arch._path.write_text(json.dumps(payload))
    arch.load()
    assert arch.sensor_ids() == ["climate.a"]


def test_save_serialize_failure_returns_false(tmp_path, caplog) -> None:
    arch = V4Archive(tmp_path)
    arch._last = ("not a time", {})  # type: ignore[assignment]
    with caplog.at_level(logging.ERROR):
        assert arch.save() is False
    assert not arch._path.exists() and "failed to serialize" in caplog.text


def test_save_into_a_missing_folder_fails_cleanly(tmp_path, caplog) -> None:
    arch = V4Archive(tmp_path, path=tmp_path / "missing" / "a.json")
    with caplog.at_level(logging.ERROR):
        assert arch.save() is False
    assert "failed to save archive file" in caplog.text


def test_save_on_windows_skips_chmod(tmp_path, monkeypatch) -> None:
    arch = V4Archive(tmp_path)
    arch.put("climate.a", E0, "1h", 70.0)
    monkeypatch.setattr(va.sys, "platform", "win32")
    monkeypatch.setattr(va.os, "chmod", lambda *a: pytest.fail("chmod on win32"))
    assert arch.save() is True and not arch.dirty


# ------------------------------------------------------------------------------------------------ recording
def test_record_ignores_curves_with_no_finite_value_and_changes_nothing(tmp_path) -> None:
    arch = V4Archive(tmp_path)
    nan = [(_NOW + timedelta(hours=1), float("nan"))]
    assert arch.record(_NOW, {"climate.a": nan, "sensor.b": []}, primary=None) == 0
    assert arch._last is None and arch.data == {}


def test_record_follows_a_new_primary_once_its_line_exists(tmp_path) -> None:
    arch = V4Archive(tmp_path)
    arch.set_ca_source("climate.a", sleep="sensor.bed", sleep_time="22:00", wake_time="06:00", tz="UTC")
    arch.record(_NOW, {"climate.b": _points(_NOW, 66.0)}, primary="climate.b")
    assert arch.ca["primary"] == "climate.b"
    assert arch.ca["sleep"] == "sensor.bed" and arch.ca["tz"] == "UTC"  # the rest of the composite is kept


def test_series_of_the_derived_ca_keeps_only_slots_in_range(tmp_path) -> None:
    arch = V4Archive(tmp_path)
    arch.record(_NOW, {"climate.a": _points(_NOW, 68.0)}, primary="climate.a")
    all_ca = arch.series("_ca", "1h", 0, 10**12)
    assert all_ca
    first = min(all_ca)
    assert arch.series("_ca", "1h", first + 1, 10**12) == {k: v for k, v in all_ca.items() if k > first}


def test_parse_last_skips_odd_sensor_entries() -> None:
    raw = {"at": 1.0, "sensors": {"climate.a": "x", "climate.b": [[2.0, 70.0], ["bad"]]}}
    assert _parse_last(raw) == (1.0, {"climate.b": [(2.0, 70.0)]})


# ------------------------------------------------------------------------------------------------ model store
def test_model_store_save_on_windows_skips_chmod(tmp_path, monkeypatch) -> None:
    store = V4ModelStore(tmp_path)
    store.v4_model = {"confidence": "low"}
    monkeypatch.setattr(va.sys, "platform", "win32")
    monkeypatch.setattr(va.os, "chmod", lambda *a: pytest.fail("chmod on win32"))
    assert store.save() is True


def test_model_store_rejects_a_non_finite_mass_and_keeps_the_old_file(tmp_path, caplog) -> None:
    store = V4ModelStore(tmp_path)
    store.v4_model = {"confidence": "low"}
    assert store.save()
    store.v4_mass_f = float("nan")
    store.dirty = True
    with caplog.at_level(logging.ERROR):
        assert store.save() is False
    assert store.dirty and "failed to save the model file" in caplog.text
    assert json.loads(store._path.read_text())["mass_f"] is None


def test_model_store_replace_failure_removes_the_temp_file(tmp_path, monkeypatch) -> None:
    store = V4ModelStore(tmp_path)
    monkeypatch.setattr(va.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert store.save() is False
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []
