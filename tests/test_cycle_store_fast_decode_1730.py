"""Cycle store fast decode (Issue #1730).

Occupant view: Home Assistant starts without a long stall while the cycle history loads. The rows of a version 2 file
are decoded straight into the compact columns; the result must be exactly what the older decode (public sample lists,
then ``_compact``'s checks and re-derived decimal places) built, and every row it rejected is still rejected.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import cycle_store as cs
from custom_components.climate_advisor.cycle_store import CycleStore

T0 = 1_768_000_000.0
SLOTS = cs._Cycle.__slots__


def _old_decode(d: Any, version: int) -> cs._Cycle | None:
    """The previous release's ``_decode`` (verbatim): public sample lists, then ``_compact``."""
    try:
        t = cs._undelta([int(x) for x in d["t"]])
        y = cs._undelta([int(x) for x in d["y"]])
        flags: list[int] = []
        for f, n in d["f"]:
            flags.extend([int(f)] * int(n))
        ot = cs._undelta([int(x) for x in d.get("ot") or []])
        oy = cs._undelta([int(x) for x in d.get("oy") or []])
        if not (len(t) == len(y) == len(flags)) or len(ot) != len(oy):
            return None
        mode = cs._CODE_MODE.get(d["m"])
        if mode is None:
            return None
        if version < 2:
            p, op = cs._V1_PLACES
        else:
            p, op = int(d["p"]), int(d["op"])
            if not (0 <= p <= cs._MAX_PLACES and 0 <= op <= cs._MAX_PLACES):
                return None
        sc, osc = 10**p, 10**op
        return cs._compact(
            {
                "id": d["i"],
                "mode": mode,
                "t_on": d["a"],
                "t_off": d.get("b"),
                "cut": d.get("x"),
                "res": d.get("r"),
                "setpoint_f": d.get("s"),
                "entity": d.get("e"),
                "samples": [[a, b / sc, f] for a, b, f in zip(t, y, flags, strict=True)],
                "outdoor": [[a, b / osc] for a, b in zip(ot, oy, strict=True)],
                "w": d.get("w"),
            }
        )
    except KeyError, TypeError, ValueError:
        return None


def _attrs(c: cs._Cycle | None) -> Any:
    if c is None:
        return None
    out = {}
    for name in SLOTS:
        v = getattr(c, name)
        out[name] = (type(v).__name__, v.tolist() if hasattr(v, "tolist") else v)
        if isinstance(v, float) and math.isnan(v):
            out[name] = (type(v).__name__, "nan")
    return out


def _same(row: dict, version: int) -> None:
    assert _attrs(cs._decode(json.loads(json.dumps(row)), version)) == _attrs(
        _old_decode(json.loads(json.dumps(row)), version)
    )


def _cycle(rnd: random.Random, k: int, n: int, places: int) -> dict:
    t_on = T0 + k * 3600
    v = 68.0
    samples = []
    for i in range(n):
        v += rnd.choice((0, 0, 0.1, -0.1, 0.013, -0.02))
        samples.append([t_on + i * 30 + rnd.randint(0, 5), round(v, places), rnd.choice((0, 1, 1, 17))])
    return {
        "id": f"c{k}",
        "mode": "heat" if k % 2 else "cool",
        "t_on": t_on,
        "t_off": t_on + 100,
        "cut": None if k % 3 else "restart",
        "res": 0.01 * (k % 5),
        "samples": samples,
        "outdoor": [[t_on + j * 600, round(40 + rnd.random() * 5, k % 4)] for j in range(max(1, n // 20))],
        "setpoint_f": 70.0,
        "entity": "sensor.hall",
        "w": None if k % 4 else 0.5,
    }


@pytest.mark.parametrize("n", [400, 150, 72])
def test_decode_equals_the_old_decode_for_every_stored_cycle_at_the_cap(n: int, tmp_path: Path) -> None:
    rnd = random.Random(n)
    store = CycleStore(tmp_path / "c.json", max_days=100000)
    for k in range(3000):
        store.add(_cycle(rnd, k, n, places=k % 4), now=T0 + 4000 * 3600)
    assert len(store) == 3000
    rows = [cs._encode(c) for c in store._cycles]
    assert {r["p"] for r in rows} >= {0, 1, 2, 3}  # every native resolution is present
    for row in rows:
        assert _attrs(cs._decode(row, cs.CYCLE_STORE_VERSION)) == _attrs(_old_decode(row, cs.CYCLE_STORE_VERSION))
    # and the file round-trips to the cycles that were stored
    assert store.save()
    again = CycleStore(store._path, max_days=100000)
    again.load(now=T0 + 4000 * 3600)
    assert [_attrs(c) for c in again._cycles] == [_attrs(c) for c in store._cycles]


def _row(**over: Any) -> dict:
    base = {
        "i": "x",
        "m": "h",
        "a": int(T0),
        "b": int(T0) + 100,
        "x": None,
        "r": 0.01,
        "s": 70.0,
        "e": "sensor.hall",
        "p": 3,
        "t": [int(T0), 30, 31, 29],
        "y": [68000, 10, 0, -20],
        "f": [[1, 2], [0, 2]],
        "op": 2,
        "ot": [int(T0), 600],
        "oy": [4134, 16],
    }
    return base | over


def test_edge_rows_are_accepted_or_rejected_exactly_as_before() -> None:
    cases: dict[str, dict] = {
        "plain": _row(),
        "places rederived down (all multiples of ten)": _row(y=[68000, 10, 10, 10], p=3),
        "places rederived to zero": _row(y=[68000, 1000, 1000, 1000], p=3, oy=[4000, 100], op=2),
        "all zero readings": _row(y=[0, 0, 0, 0], oy=[0, 0]),
        "flag over 255": _row(f=[[300, 2], [0, 2]]),
        "flag negative": _row(f=[[-1, 2], [0, 2]]),
        "flag 255 and 0": _row(f=[[255, 2], [0, 2]]),
        "null reading (a dropped NaN)": _row(y=[68000, None, 0, 0]),
        "float nan reading": _row(y=[68000, float("nan"), 0, 0]),
        "float reading truncated": _row(y=[68000.7, 10.2, 0, -20]),
        "length mismatch": _row(f=[[1, 2], [0, 1]]),
        "outdoor mismatch": _row(oy=[4134]),
        "no samples": _row(t=[], y=[], f=[], ot=[], oy=[]),
        "no outdoor": _row(ot=[], oy=[]),
        "no outdoor keys": {k: v for k, v in _row().items() if k not in ("ot", "oy")},
        "bad mode": _row(m="z"),
        "places out of range": _row(p=4),
        "places negative": _row(op=-1),
        "places missing": {k: v for k, v in _row().items() if k != "p"},
        "id missing": {k: v for k, v in _row().items() if k != "i"},
        "t_on string": _row(a="x"),
        "t_on nan": _row(a=float("nan")),
        "t_on float": _row(a=T0 + 0.5),
        "t_off float and bad": _row(b="x"),
        "res bad": _row(r="x"),
        "res nan": _row(r=float("nan")),
        "setpoint weird": _row(s=[1]),
        "weight in range": _row(w=0.25),
        "weight out of range": _row(w=1.5),
        "weight bool": _row(w=True),
        "cut and entity non-string": _row(x=5, e=7, i=9),
        "huge time": _row(a=2**60, t=[2**60, 5, 5, 5]),
        "huge reading": _row(y=[2**40, 10, 0, 0]),
        "int32 overflow": _row(y=[2**31, 0, 0, 0]),
        "bool values": _row(t=[True, 1, 1, 1], y=[True, 0, 0, 0]),
        "runs not pairs": _row(f=[[1, 2, 3]]),
        "negative run length": _row(f=[[1, -2], [0, 4]]),
        "not a dict": [1, 2],
    }
    for name, row in cases.items():
        for version in (2, 3):
            try:
                _same(row, version)
            except AssertionError as err:  # name the case
                raise AssertionError(f"{name} (version {version}): {err}") from err


def test_version_1_rows_keep_the_original_path() -> None:
    rows = [
        _row(y=[6800, 1, 0, -2], oy=[413, 2]),
        _row(y=[6800, 10, 10, 10], p=0, op=0),
        _row(f=[[300, 4]]),
        _row(m="z"),
    ]
    for row in rows:
        _same(row, 1)
    # and the version-1 reading is centi-degrees: 6800 -> 68.0 and 68.01 -> two places
    c = cs._decode(rows[0], 1)
    assert c is not None and c.p == 2 and c.y[0] == 6800


def test_load_equals_the_old_load_for_mixed_and_damaged_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rnd = random.Random(9)
    store = CycleStore(tmp_path / "src.json", max_days=100000)
    for k in range(60):
        store.add(_cycle(rnd, k, 40, places=k % 4), now=T0 + 400 * 3600)
    rows = [cs._encode(c) for c in store._cycles]
    rows[3]["f"] = [[999, len(rows[3]["t"])]]  # invalid flag: dropped
    rows[7]["y"][2] = None  # a dropped NaN
    rows[11]["m"] = "q"
    rows.append(dict(rows[0]))  # duplicate id: the first is kept
    for version in (cs.CYCLE_STORE_VERSION, 1):
        path = tmp_path / f"v{version}.json"
        path.write_text(json.dumps({"v": version, "cycles": rows}), encoding="utf-8")
        new = CycleStore(path, max_days=100000)
        new.load(now=T0 + 400 * 3600)
        monkeypatch.setattr(cs, "_decode", _old_decode)
        old = CycleStore(path, max_days=100000)
        old.load(now=T0 + 400 * 3600)
        monkeypatch.undo()
        assert [_attrs(c) for c in new._cycles] == [_attrs(c) for c in old._cycles]
        assert len(new) == len(old) and new.dirty == old.dirty
        assert (version == 1) == (len(new) > 0 and new.dirty)  # an older file is migrated on the next save
