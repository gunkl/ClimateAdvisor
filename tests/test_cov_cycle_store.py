"""Edge branches of the cycle store (Silver test-coverage).

Occupant view: a damaged cycle file or one odd recorded run must never break startup or the thermostat path; the
heating/cooling rates keep learning from the cycles that are readable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from custom_components.climate_advisor import cycle_store as cs
from custom_components.climate_advisor.cycle_store import CycleStore
from tests.test_cycle_store_1512 import T0, _cycle


def _store(tmp_path: Path) -> CycleStore:
    return CycleStore(tmp_path / "climate_advisor_cycles.json")


def test_a_flag_outside_one_byte_makes_the_cycle_unusable(tmp_path) -> None:
    c = _cycle(T0)
    c["samples"][2][2] = 256
    s = _store(tmp_path)
    assert s.add(c, now=T0) is False and len(s) == 0


def _file_with(tmp_path: Path, rows: list, v: int = cs.CYCLE_STORE_VERSION) -> CycleStore:
    s = _store(tmp_path)
    s._path.write_text(json.dumps({"v": v, "cycles": rows}))
    return s


def _encoded(tmp_path: Path, t_on: float = T0, mode: str = "heat") -> dict:
    src = CycleStore(tmp_path / "src.json")
    assert src.add(_cycle(t_on, mode), now=t_on)
    return cs._encode(src._cycles[0])


def test_load_drops_unknown_modes_bad_places_and_duplicate_ids(tmp_path, caplog) -> None:
    good = _encoded(tmp_path)
    bad_mode = {**good, "i": "x1", "m": "z"}
    bad_places = {**good, "i": "x2", "p": 9}
    dup = {**good, "y": [v + 100 for v in good["y"]]}  # same id, later in the file: the first one wins
    s = _file_with(tmp_path, [good, bad_mode, bad_places, dup])
    with caplog.at_level(logging.WARNING):
        s.load(now=T0)
    out = s.cycles()
    assert [c["id"] for c in out] == [good["i"]]
    assert out[0]["samples"][0][1] == 68.0  # the first copy, not the shifted duplicate
    assert "dropped 3 unreadable cycles" in caplog.text


def test_load_unexpected_error_starts_empty(tmp_path, monkeypatch, caplog) -> None:
    s = _store(tmp_path)
    assert s.add(_cycle(T0), now=T0)
    monkeypatch.setattr(s, "_load", lambda now: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.WARNING):
        s.load(now=T0)
    assert len(s) == 0 and "unexpected error loading the cycle file" in caplog.text


def test_set_weights_skips_unknown_ids_and_non_numbers_and_never_raises(tmp_path, caplog) -> None:
    s = _store(tmp_path)
    assert s.add(_cycle(T0), now=T0)
    cid = s.cycles()[0]["id"]
    s.dirty = False
    assert s.set_weights({"nope": 0.5, cid: True}) == 0 and not s.dirty
    assert s.set_weights({cid: 2.0}) == 1 and s._by_id[cid].w == 1.0  # clamped
    with caplog.at_level(logging.WARNING):
        assert s.set_weights(["not", "a", "dict"]) == 0  # type: ignore[arg-type]
    assert "set_weights failed" in caplog.text


def test_add_failure_is_logged_and_reported_false(tmp_path, monkeypatch, caplog) -> None:
    s = _store(tmp_path)
    monkeypatch.setattr(s, "_enforce_cap", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.WARNING):
        assert s.add(_cycle(T0), now=T0) is False
    assert "failed to add a cycle" in caplog.text


def test_prune_failure_drops_nothing(tmp_path, caplog) -> None:
    s = _store(tmp_path)
    assert s.add(_cycle(T0), now=T0)
    with caplog.at_level(logging.WARNING):
        assert s.prune("not a time", force=True) == 0  # type: ignore[arg-type]
    assert len(s) == 1 and "prune failed" in caplog.text


def test_save_serialize_failure_returns_false(tmp_path, caplog) -> None:
    s = _store(tmp_path)
    assert s.add(_cycle(T0), now=T0)
    s._cycles[0].mode = "fan"  # no file code for it
    with caplog.at_level(logging.ERROR):
        assert s.save() is False
    assert not s._path.exists() and "failed to serialize" in caplog.text


def test_save_on_windows_skips_chmod(tmp_path, monkeypatch) -> None:
    s = _store(tmp_path)
    assert s.add(_cycle(T0), now=T0)
    monkeypatch.setattr(cs.sys, "platform", "win32")
    monkeypatch.setattr(cs.os, "chmod", lambda *a: pytest.fail("chmod on win32"))
    assert s.save() is True and not s.dirty
