"""Cycle store persistence (Issues #1512 / #1516).

Occupant view: the heating/cooling rates survive restarts and keep learning from the last 90 days of runs, while the
file stays small enough for a Raspberry Pi SD card, never breaks startup when damaged, and is private (0600).
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
from pathlib import Path

import pytest

from custom_components.climate_advisor.const import CYCLE_MAX_STORED, CYCLE_RETENTION_DAYS
from custom_components.climate_advisor.cycle_recorder import CycleRecorder
from custom_components.climate_advisor.cycle_store import CycleStore

T0 = 1_768_000_000.0
DAY = 86400.0


def _cycle(t_on: float, mode: str = "heat", n: int = 30, flags_tail: int = 0) -> dict:
    sign = 1.0 if mode == "heat" else -1.0
    samples = [[t_on + i * 40.5, 68.0 + sign * 0.013 * i, 1 if i <= n // 2 else flags_tail] for i in range(n)]
    return {
        "id": f"{mode}-{int(t_on)}",
        "mode": mode,
        "t_on": t_on,
        "t_off": t_on + (n // 2) * 40.5,
        "cut": None,
        "res": 0.01,
        "samples": samples,
        "outdoor": [[t_on, 41.34], [t_on + 600, 41.5]],
        "setpoint_f": 70.0,
        "entity": "sensor.hall",
    }


def test_round_trip_is_exact_after_rounding(tmp_path: Path) -> None:
    path = tmp_path / "climate_advisor_cycles.json"
    s = CycleStore(path)
    a, b = _cycle(T0), _cycle(T0 + 3600, "cool")
    b["cut"], b["t_off"] = "restart", None
    b["samples"][3][2] |= 16
    assert s.add(b, now=T0 + 7200) and s.add(a, now=T0 + 7200)
    assert [c["mode"] for c in s.cycles()] == ["heat", "cool"]  # kept in t_on order
    before = s.cycles()
    assert s.save() and not s.dirty
    r = CycleStore(path)
    r.load(now=T0 + 7200)
    assert r.cycles() == before
    assert r.cycles("cool")[0]["cut"] == "restart" and r.cycles("cool")[0]["t_off"] is None
    assert r.cycles("heat")[0]["samples"][0] == [T0, 68.0, 1]
    assert [c["id"] for c in r.cycles("heat", T0 + 1)] == []
    # recorded -> stored differs only by the documented rounding
    raw_s = a["samples"][7]
    got = r.cycles("heat")[0]["samples"][7]
    # Issue #1512 finding 6: readings keep their native resolution (0.013 F steps here; was rounded to 0.01 F, which
    # coarsened a fine sensor's steps and could mislead the model's resolution inference); times to the second
    assert abs(got[0] - raw_s[0]) <= 0.5 and abs(got[1] - raw_s[1]) <= 1e-9 and got[2] == raw_s[2]
    assert r.cycles("heat")[0]["outdoor"] == [[T0, 41.34], [T0 + 600, 41.5]]


def test_file_is_compact_versioned_and_delta_encoded(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    s = CycleStore(path)
    s.add(_cycle(T0), now=T0 + 3600)
    s.save()
    text = path.read_text()
    assert " " not in text and "\n" not in text  # compact separators
    raw = json.loads(text)
    # v2 (Issue #1512 finding 6): each series stores its own native decimal places instead of fixed centi-degrees
    assert raw["v"] == 2
    (row,) = raw["cycles"]
    assert row["t"][0] == int(T0) and max(abs(x) for x in row["t"][1:]) < 100  # second deltas
    assert row["p"] == 3 and row["y"][0] == 68000 and max(abs(x) for x in row["y"][1:]) < 20  # milli-degree deltas
    assert row["op"] == 2 and row["oy"][0] == 4134
    assert row["f"] == [[1, 16], [0, 14]]  # run-length flags


def test_replaces_same_id_and_rejects_unusable(tmp_path: Path) -> None:
    s = CycleStore(tmp_path / "c.json")
    assert s.add(_cycle(T0), now=T0)
    c2 = _cycle(T0)
    c2["setpoint_f"] = 71.0
    assert s.add(c2, now=T0)
    assert len(s) == 1 and s.cycles()[0]["setpoint_f"] == 71.0
    assert not s.add({"id": "x"}, now=T0)
    assert not s.add({**_cycle(T0 + 5), "samples": []}, now=T0)
    assert not s.add({**_cycle(T0 + 5), "mode": "off"}, now=T0)
    assert len(s) == 1


def test_retention_prunes_old_cycles_at_most_hourly(tmp_path: Path) -> None:
    s = CycleStore(tmp_path / "c.json")
    old = T0 - (CYCLE_RETENTION_DAYS + 1) * DAY
    s.add(_cycle(old), now=old)
    s.add(_cycle(T0 - 10 * DAY), now=old)
    s.add(_cycle(T0), now=T0)  # the add prunes (first call at/after the previous prune hour)
    assert [c["t_on"] for c in s.cycles()] == [T0 - 10 * DAY, T0]
    s.add(_cycle(T0 - (CYCLE_RETENTION_DAYS + 5) * DAY), now=T0)
    s.add(_cycle(T0 + 100), now=T0 + 100)  # within the hour: no prune
    assert len(s) == 4
    assert s.prune(T0 + 200) == 0  # throttled
    assert s.prune(T0 + 200, force=True) == 1
    assert s.prune(T0 + 4000) == 0 and len(s) == 3


def test_load_prunes_expired(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    s = CycleStore(path)
    s.add(_cycle(T0 - 100 * DAY), now=T0 - 100 * DAY)
    s.add(_cycle(T0 - DAY), now=T0 - 100 * DAY)
    s.save()
    r = CycleStore(path)
    r.load(now=T0)
    assert [c["t_on"] for c in r.cycles()] == [T0 - DAY]


def test_cap_drops_oldest_first(tmp_path: Path) -> None:
    """Each mode keeps half the cap, its own oldest dropped first."""
    s = CycleStore(tmp_path / "c.json", max_cycles=10)
    for i in range(8):
        s.add(_cycle(T0 + i * 3600, n=4), now=T0 + 100)
    assert [c["t_on"] for c in s.cycles()] == [T0 + i * 3600 for i in range(3, 8)]
    assert CYCLE_MAX_STORED == 3000


def test_a_busy_cooling_summer_keeps_last_winters_heating_cycles(tmp_path: Path) -> None:
    """Issue #1512 final verification (the #1523 rule for the cycle file): the cap used to drop the oldest cycles
    whatever their mode, so a summer of cooling cycles left none of the winter's heating cycles for the first refit
    in autumn (0 of 3000 at 60 cycles a day). Each mode now keeps its own half."""
    s = CycleStore(tmp_path / "c.json", max_cycles=20)
    for i in range(10):
        s.add(_cycle(T0 + i * 3600, "heat", n=4), now=T0 + i * 3600)
    for i in range(10, 60):
        s.add(_cycle(T0 + i * 3600, "cool", n=4), now=T0 + i * 3600)
    assert len(s.cycles("heat")) == 10  # old behaviour: 0
    assert [c["t_on"] for c in s.cycles("cool")] == [T0 + i * 3600 for i in range(50, 60)]
    assert len(s) <= 20


def test_cycles_filters_by_mode_and_since(tmp_path: Path) -> None:
    s = CycleStore(tmp_path / "c.json")
    for i, m in enumerate(["heat", "cool", "heat", "cool"]):
        s.add(_cycle(T0 + i * 3600, m, n=6), now=T0)
    assert [c["mode"] for c in s.cycles("cool")] == ["cool", "cool"]
    assert len(s.cycles("heat", T0 + 3600)) == 1
    assert len(s.cycles(None, T0 + 7200)) == 2


@pytest.mark.parametrize(
    "content",
    ["not json {", "[]", '{"v":1}', '{"v":1,"cycles":"x"}', ""],
)
def test_corrupt_file_starts_empty_and_logs_once(tmp_path: Path, caplog, content: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(content)
    s = CycleStore(path)
    with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.cycle_store"):
        s.load()
        s.load()
    assert len(s) == 0
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert s.add(_cycle(T0), now=T0) and s.save()  # and it recovers by overwriting


def test_bad_rows_are_dropped_good_rows_kept(tmp_path: Path, caplog) -> None:
    path = tmp_path / "c.json"
    s = CycleStore(path)
    s.add(_cycle(T0), now=T0)
    s.save()
    raw = json.loads(path.read_text())
    raw["cycles"] += [{"i": "bad"}, {**raw["cycles"][0], "i": "badlen", "y": [1]}, 5]
    path.write_text(json.dumps(raw))
    r = CycleStore(path)
    with caplog.at_level(logging.WARNING):
        r.load(now=T0)
    assert [c["id"] for c in r.cycles()] == [f"heat-{int(T0)}"]


def test_version_1_file_is_migrated_on_load(tmp_path: Path) -> None:
    """A v1 file (fixed centi-degree indoor, deci-degree outdoor, no places) reads unchanged and is saved as v2."""
    path = tmp_path / "c.json"
    v1 = {
        "v": 1,
        "cycles": [
            {
                "i": "heat-1",
                "m": "h",
                "a": int(T0),
                "b": int(T0) + 600,
                "x": None,
                "r": 0.1,
                "s": 70.0,
                "e": "sensor.hall",
                "t": [int(T0), 60, 60, 480],
                "y": [6810, 10, 10, 0],
                "f": [[1, 3], [0, 1]],
                "ot": [int(T0)],
                "oy": [413],
            }
        ],
    }
    path.write_text(json.dumps(v1))
    s = CycleStore(path)
    s.load(now=T0 + 3600)
    (c,) = s.cycles()
    assert [x[1] for x in c["samples"]] == [68.1, 68.2, 68.3, 68.3] and c["outdoor"] == [[T0, 41.3]]
    assert s.dirty and s.save()
    raw = json.loads(path.read_text())
    assert raw["v"] == 2 and raw["cycles"][0]["p"] == 1 and raw["cycles"][0]["y"][0] == 681
    r = CycleStore(path)
    r.load(now=T0 + 3600)
    assert r.cycles() == s.cycles()


@pytest.mark.parametrize(
    ("values", "places"),
    [
        ([68.0, 69.0, 70.0], 0),  # a whole-degree thermostat stores whole degrees
        ([68.0, 68.5, 69.0], 1),
        ([68.018, 68.036, 68.054], 3),  # 0.01 C steps (0.018 F) are kept, never rounded to 0.01 F
        ([20.1 * 9 / 5 + 32, 20.2 * 9 / 5 + 32], 2),  # float noise from the C->F conversion is not "resolution"
    ],
)
def test_native_resolution_is_kept(tmp_path: Path, values: list, places: int) -> None:
    path = tmp_path / "c.json"
    s = CycleStore(path)
    cyc = _cycle(T0, n=len(values))
    cyc["samples"] = [[T0 + 60 * i, v, 1] for i, v in enumerate(values)]
    assert s.add(cyc, now=T0) and s.save()
    assert json.loads(path.read_text())["cycles"][0]["p"] == places
    got = [x[1] for x in s.cycles()[0]["samples"]]
    assert got == pytest.approx(values, abs=1e-9)


def test_replacing_a_cycle_among_many_with_the_same_start(tmp_path: Path) -> None:
    """add() finds the replaced cycle through its id (O(1)) and the t_on bisect, never a scan of the whole store."""
    s = CycleStore(tmp_path / "c.json")
    for i in range(5):
        c = _cycle(T0)
        c["id"] = f"heat-x{i}"
        s.add(c, now=T0)
    c = _cycle(T0)
    c["id"] = "heat-x2"
    c["setpoint_f"] = 75.0
    s.add(c, now=T0)
    assert len(s) == 5 and sorted(x["id"] for x in s.cycles()) == [f"heat-x{i}" for i in range(5)]
    assert [x["setpoint_f"] for x in s.cycles() if x["id"] == "heat-x2"] == [75.0]


def test_compact_memory_per_reading(tmp_path: Path) -> None:
    """Each stored reading costs about 9 bytes in memory (int32 offset, int32 value, one flag byte)."""
    import tracemalloc

    s = CycleStore(tmp_path / "c.json")
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    n = 0
    for i in range(200):
        c = _cycle(T0 + i * 3600, n=100)
        n += len(c["samples"])
        s.add(c, now=T0 + i * 3600)
    used = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    assert used / n < 30, used / n


def test_missing_file_starts_empty(tmp_path: Path) -> None:
    s = CycleStore(tmp_path / "nope.json")
    s.load()
    assert len(s) == 0 and not s.dirty


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_saved_file_is_private_and_no_temp_left(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    s = CycleStore(path)
    s.add(_cycle(T0), now=T0)
    assert s.save()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_save_failure_never_raises_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    s = CycleStore(tmp_path / "c.json")
    s.add(_cycle(T0), now=T0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    assert s.save() is False
    assert s.dirty
    assert list(tmp_path.iterdir()) == []
    # unwritable directory
    assert CycleStore(tmp_path / "missing_dir" / "c.json").save() is False


def test_size_budget_3000_cycles(tmp_path: Path) -> None:
    """3000 realistic cycles (about 40 readings with seconds, outdoor, flags; half heat, half cool, the full cap)
    stay well under 3 MB."""
    rng = random.Random(7)
    s = CycleStore(tmp_path / "c.json")
    t = T0
    for i in range(CYCLE_MAX_STORED):
        t += 1800
        n = rng.randint(25, 55)
        temp, ts, samples = 66.0 + rng.random() * 4, t, []
        for j in range(n):
            ts += rng.randint(20, 90)
            temp += rng.choice([0.0, 0.02, 0.05, 0.1, -0.02])
            samples.append([ts, temp, 1 if j < n // 2 else (2 if rng.random() < 0.05 else 0)])
        s.add(
            {
                "id": f"heat-{i}",
                "mode": "heat" if i % 2 else "cool",  # a full store: each mode holds its half of the cap
                "t_on": t,
                "t_off": samples[n // 2][0],
                "cut": None,
                "res": 0.02,
                "samples": samples,
                "outdoor": [[t + 300 * k, 40.0 + k * 0.1] for k in range(8)],
                "setpoint_f": 70.0,
                "entity": "sensor.hall_temperature",
            },
            now=t,
        )
    assert len(s) == CYCLE_MAX_STORED
    assert s.save()
    size = (tmp_path / "c.json").stat().st_size
    assert size < 3_000_000, size
    r = CycleStore(tmp_path / "c.json")
    r.load(now=t)
    assert len(r) == CYCLE_MAX_STORED


def test_recorder_output_feeds_the_store_and_reloads(tmp_path: Path) -> None:
    rec = CycleRecorder(True)
    closed = []
    closed += rec.on_edge(T0, "heat", 68.0, 40.0, 70.0)
    for m in range(1, 7):
        closed += rec.reading(T0 + 60 * m + 7, 68.0 + 0.1 * m, "sensor.hall")
    closed += rec.off_edge(T0 + 400, 68.6)
    closed += rec.tick(T0 + 400 + 2700)
    (c,) = closed
    s = CycleStore(tmp_path / "c.json")
    assert s.add(c, now=T0 + 4000) and s.save()
    r = CycleStore(tmp_path / "c.json")
    r.load(now=T0 + 4000)
    assert r.cycles() == s.cycles()


def test_retention_counts_back_from_the_newest_cycle_of_the_mode(tmp_path: Path) -> None:
    """Owner decision 2026-11-11: a quiet season prunes nothing. Occupant: last winter's heating cycles are still
    there for the first refit in autumn; they leave only as newer heating cycles arrive (was: pruned 90 days after
    they ran, whatever came after)."""
    s = CycleStore(tmp_path / "c.json")
    s.add(_cycle(T0 - 5 * DAY, "heat"), now=T0)
    s.add(_cycle(T0, "heat"), now=T0)
    later = T0 + 200 * DAY  # a summer later: cooling cycles only
    s.add(_cycle(later, "cool"), now=later)
    assert s.prune(later + 1, force=True) == 0
    assert [c["t_on"] for c in s.cycles("heat")] == [T0 - 5 * DAY, T0]
    s2 = CycleStore(tmp_path / "c.json")
    s.save()
    s2.load(now=later + 1)  # a reload a season later keeps them too
    assert len(s2.cycles("heat")) == 2
    # the first heating cycle of the next season: cycles more than 90 days older than it leave
    s.add(_cycle(later + DAY, "heat"), now=later + DAY)  # add() prunes (hourly)
    assert [c["t_on"] for c in s.cycles("heat")] == [later + DAY] and len(s.cycles("cool")) == 1
    # a clock jump into the future never prunes more: the anchor is capped at now
    s.add(_cycle(later + 400 * DAY, "cool"), now=later + 2 * DAY)
    assert s.prune(later + 2 * DAY, force=True) == 0 and len(s.cycles("cool")) == 2
