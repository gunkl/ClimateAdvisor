"""One-element-per-line JSON stores (Issue #1730).

Occupant view: Home Assistant stays responsive while Climate Advisor reads and writes its multi-megabyte history
files. A whole-file ``json.loads`` / ``json.dumps`` is one C call that holds the GIL (and so the event loop) for
84-230 ms on x86; the same document written one element per line is handled one small call at a time. The files stay
plain JSON, so an older release (which reads them with ``json.loads``) loads them unchanged and an older file (one
line, possibly with NaN tokens) still loads here.

Guards: (1) ``store_json`` round trip and layout; (2) per store: new writer -> stdlib reader (the downgrade
guarantee), old writer -> new reader, new writer -> new reader, all giving the same in-memory state; (3) the
max-hold check: no single encode/decode call over a large member; (4) the columnar archive load and the window
reads give exactly what the per-value load and the whole-line scan gave.
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from custom_components.climate_advisor import store_json
from custom_components.climate_advisor import v4_archive as va
from custom_components.climate_advisor.chart_log import ChartStateLog
from custom_components.climate_advisor.const import CYCLE_STORE_VERSION, V3_ARCHIVE_LEADS, V4_ARCHIVE_LEADS
from custom_components.climate_advisor.cycle_store import CycleStore, _encode
from custom_components.climate_advisor.forecast_archive import ForecastArchive, decode_snapshot
from custom_components.climate_advisor.learning import DailyRecord, LearningEngine
from custom_components.climate_advisor.v4_archive import CA_LINE, V3Archive, V4Archive, _Line

SLOT = 1800
INF = math.inf

# --------------------------------------------------------------------------------------------- store_json


def _doc() -> dict[str, Any]:
    return {
        "v": 1,
        "leads": ["1h", "4h"],
        "sensors": {
            "sensor.hall": {str(1_768_000_000 + i * SLOT): [i * 0.1, None] for i in range(40)},
            "sensor.empty": {},
        },
        "text": 'café °F, { [ " ,\n',
        "list": [[1, 2], {"a": [None, True]}],
    }


@pytest.mark.parametrize("depth", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("lossless", [False, True])
def test_dumps_is_plain_json_and_loads_gives_the_same_structure(depth: int, lossless: bool) -> None:
    doc = _doc()
    data = store_json.dumps(doc, depth=depth, lossless=lossless)
    assert json.loads(data) == doc  # what every older release's reader does
    assert store_json.loads(data) == doc
    assert store_json.loads(data.decode()) == doc  # str too


@pytest.mark.parametrize("depth", [1, 2, 3, 4])
@pytest.mark.parametrize("lossless", [False, True])
def test_the_line_reader_itself_reads_what_dumps_wrote(depth: int, lossless: bool) -> None:
    """Not the whole-file fallback: ``loads`` would hide a broken line reader behind it."""
    doc = _doc() | {
        "entries": [{"ts": "x", "a": [1, 2], "b": {"c": None}, "s": "{ [ ,"} for _ in range(30)],
        "tail": [[1], []],
    }
    data = store_json.dumps(doc, depth=depth, lossless=lossless)
    assert data.count(b"\n") > 1
    assert store_json._loads_lines(data) == doc
    assert store_json._loads_lines(json.dumps(doc, indent=2).encode()) == doc


def test_dumps_puts_one_element_per_line_above_the_depth() -> None:
    lines = store_json.dumps(_doc(), depth=3).decode().split("\n")
    assert '"sensor.hall":{' in lines
    assert sum(1 for line in lines if line.startswith('"17680')) == 40  # one slot per line
    assert store_json.dumps({"a": 1, "b": "x"}, depth=3) == b'{"a":1,"b":"x"}'  # nothing nested or large: one line
    assert store_json.dumps({"x": {"y": [1] * 5}}, depth=0) == b'{"x":{"y":[1,1,1,1,1]}}'


def test_non_string_keys_follow_the_stdlib() -> None:
    doc = {1: "a", 2.5: "b", False: "c", None: [1] * 2000}
    for lossless in (False, True):
        assert json.loads(store_json.dumps(doc, depth=2, lossless=lossless)) == json.loads(json.dumps(doc))


def test_orjson_mode_writes_null_for_nan_and_lossless_keeps_the_tokens() -> None:
    doc = {"rows": [{"x": float("nan"), "y": 1.5}, {"x": float("inf")}, {"x": -float("inf")}]}
    plain = store_json.dumps(doc)
    assert [r.get("x") for r in json.loads(plain)["rows"]] == [None, None, None]
    kept = store_json.dumps(doc, lossless=True)
    assert b"NaN" in kept and b"Infinity" in kept
    got = store_json.loads(kept)["rows"]
    assert math.isnan(got[0]["x"]) and got[1]["x"] == INF and got[2]["x"] == -INF


def test_loads_reads_whatever_an_older_release_wrote() -> None:
    doc = _doc()
    assert store_json.loads(json.dumps(doc)) == doc  # compact, one line
    assert store_json.loads(json.dumps(doc, indent=2)) == doc  # pretty (the learning file)
    assert store_json.loads(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n") == doc
    nan = {"a": [{"x": float("nan")}] * 3, "n": float("inf")}
    for text in (json.dumps(nan), json.dumps(nan, indent=2), json.dumps(nan, separators=(",", ":"))):
        got = store_json.loads(text)  # NaN / Infinity tokens: orjson refuses them, the stdlib fallback reads them
        assert math.isnan(got["a"][0]["x"]) and got["n"] == INF
    # a NaN on one line of a line-per-element file falls back to the whole-file read
    lossless = store_json.dumps({"rows": [{"x": float("nan")}] * 3, "tail": list(range(5))}, lossless=True)
    assert lossless.count(b"\n") > 1 and math.isnan(store_json.loads(lossless)["rows"][0]["x"])


@pytest.mark.parametrize(
    "bad", ['{\n"a":[\n1,\n2\n]\n', '{\n"a":1\n]\n', "[\n1\n}\n", '{\n"a":\n}\n', "not json\nat all\n"]
)
def test_a_damaged_file_raises_value_error_as_before(bad: str) -> None:
    with pytest.raises(ValueError):
        store_json.loads(bad)
    with pytest.raises(ValueError):
        json.loads(bad)


def test_max_hold_no_single_encode_or_decode_call_covers_a_large_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic (not a timing test): every call that holds the GIL is over one small element."""
    big = {"v": 1, "sensors": {f"s{i}": {str(k): [k * 0.5, None, 1.0] for k in range(3000)} for i in range(3)}}
    whole = len(json.dumps(big))
    sizes: list[int] = []
    real_orjson_dumps, real_json_dumps = orjson.dumps, json.dumps

    def spy_orjson(obj: Any, *a: Any, **k: Any) -> bytes:
        out = real_orjson_dumps(obj, *a, **k)
        sizes.append(len(out))
        return out

    def spy_json(obj: Any, *a: Any, **k: Any) -> str:
        out = real_json_dumps(obj, *a, **k)
        sizes.append(len(out))
        return out

    parsed: list[int] = []
    real_loads = orjson.loads

    def spy_loads(b: bytes) -> Any:
        parsed.append(len(b))
        return real_loads(b)

    real_stdlib = store_json._STDLIB

    class _SpyStdlib:  # the lossless encoder (one shared JSONEncoder)
        def encode(self, obj: Any) -> str:
            out = real_stdlib.encode(obj)
            sizes.append(len(out))
            return out

    monkeypatch.setattr(orjson, "dumps", spy_orjson)
    monkeypatch.setattr(json, "dumps", spy_json)
    monkeypatch.setattr(store_json, "_STDLIB", _SpyStdlib())
    for lossless in (False, True):
        sizes.clear()
        data = store_json.dumps(big, depth=3, lossless=lossless)
        assert len(sizes) > 9000 and max(sizes) < 200 and whole > 100_000, (lossless, max(sizes), len(sizes))
        parsed.clear()
        monkeypatch.setattr(orjson, "loads", spy_loads)
        assert store_json.loads(data) == big
        monkeypatch.setattr(orjson, "loads", real_loads)
        assert len(parsed) > 9000 and max(parsed) < 200, (lossless, max(parsed))


@pytest.fixture
def parsed_sizes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Sizes of every ``orjson.loads`` / ``json.loads`` input: a load that falls back to the whole file shows up."""
    sizes: list[int] = []
    real_o, real_j = orjson.loads, json.loads
    monkeypatch.setattr(orjson, "loads", lambda b, *a, **k: (sizes.append(len(b)), real_o(b, *a, **k))[1])
    monkeypatch.setattr(json, "loads", lambda b, *a, **k: (sizes.append(len(b)), real_j(b, *a, **k))[1])
    return sizes


# --------------------------------------------------------------------------------------------- v4 archive


def _fill(arc: V4Archive, *, sensors: int = 3, days: int = 400, seed: int = 1) -> None:
    rnd = random.Random(seed)
    now = int(time.time()) // SLOT * SLOT
    start = now - days * 86400 + 3600
    leads = arc._leads
    for n in range(sensors):
        line = arc._lines.setdefault(f"sensor.s{n}", _Line(leads))
        for e in range(start, now, SLOT):
            for k, lead in enumerate(leads):
                if rnd.random() < 0.97 and not (k == len(leads) - 1 and e < start + 5 * 86400):
                    line.put_if_absent(e, lead, round(65 + rnd.random() * 10, 2))
    arc.set_ca_source("sensor.s0", sleep="sensor.s1", sleep_time="22:30", wake_time="07:00", tz="America/Chicago")
    arc._last = (float(now), {"sensor.s0": [(float(now + 300 * k), 70.0 + k * 0.01) for k in range(300)]})


def _state(arc: V4Archive) -> dict[str, Any]:
    return {
        "lines": {s: (ln.base, {k: c.tobytes() for k, c in ln.cols.items()}) for s, ln in arc._lines.items()},
        "ca": dict(arc.ca),
        "last": arc._last,
    }


def _old_payload(arc: V4Archive) -> dict[str, Any]:
    """The document the previous release's ``save`` built (verbatim)."""
    leads = list(arc._leads)
    sensors = {}
    for sensor, line in list(arc._lines.items()):
        sensors[sensor] = {str(epoch): [line.get(epoch, lead) for lead in leads] for epoch in line.slots()}
    payload: dict = {"v": 1, "leads": leads, "sensors": sensors}
    if arc.ca:
        payload["ca"] = dict(arc.ca)
    last = arc._last
    if last is not None:
        payload["last"] = {
            "at": round(last[0], 3),
            "sensors": {sid: [[round(t, 3), v] for t, v in pts] for sid, pts in list(last[1].items())},
        }
    return payload


def _old_reader_lines(raw: dict, my_leads: tuple[str, ...], cutoff: float) -> tuple[dict[str, _Line], int]:
    """The previous release's ``_load`` loop (verbatim): one ``put_if_absent`` per value."""
    lines: dict[str, _Line] = {}
    slots = 0
    leads = raw["leads"]
    for sensor, rows in raw["sensors"].items():
        if not isinstance(sensor, str) or not isinstance(rows, dict):
            continue
        line = _Line(my_leads)
        for key, vals in rows.items():
            try:
                epoch = int(key)
            except TypeError, ValueError:
                continue
            if epoch < cutoff or not isinstance(vals, list):
                continue
            wrote = False
            for lead, v in zip(leads, vals, strict=False):
                if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v):
                    wrote |= line.put_if_absent(epoch, str(lead), float(v))
            slots += 1 if wrote else 0
        if len(line):
            lines[sensor] = line
    return lines, slots


def _lines_state(lines: dict[str, _Line]) -> dict[str, Any]:
    return {s: (ln.base, {k: c.tobytes() for k, c in ln.cols.items()}) for s, ln in lines.items()}


@pytest.fixture(scope="module")
def full_archive(tmp_path_factory: pytest.TempPathFactory) -> V4Archive:
    arc = V4Archive(tmp_path_factory.mktemp("v4full"), entry_id="z")
    _fill(arc)  # 3 sensors x 7 leads x 400 days
    return arc


def test_v4_new_file_is_the_same_document_the_old_writer_wrote(full_archive: V4Archive) -> None:
    assert full_archive.save()
    on_disk = json.loads(full_archive._path.read_bytes())  # stdlib: what an older release does
    assert on_disk == json.loads(json.dumps(_old_payload(full_archive), separators=(",", ":")))
    assert full_archive._path.read_bytes().count(b"\n") > 3 * 19000  # one slot per line


def test_v4_round_trips_and_an_old_file_still_loads(full_archive: V4Archive, tmp_path: Path) -> None:
    assert full_archive.save()
    reloaded = V4Archive(full_archive._path.parent, entry_id="z")
    reloaded.load()
    assert _state(reloaded) == _state(full_archive)

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = V4Archive(old_dir, entry_id="z")
    old._path.write_text(json.dumps(_old_payload(full_archive), separators=(",", ":")), encoding="utf-8")  # old writer
    old.load()
    assert _state(old) == _state(full_archive)


def test_v4_load_parses_one_slot_at_a_time(full_archive: V4Archive, parsed_sizes: list[int]) -> None:
    assert full_archive.save()
    size = full_archive._path.stat().st_size
    V4Archive(full_archive._path.parent, entry_id="z").load()
    assert len(parsed_sizes) > 3 * 19000 and max(parsed_sizes) < size / 500


def test_v4_downgrade_the_previous_reader_loads_the_new_file(full_archive: V4Archive) -> None:
    assert full_archive.save()
    raw = json.loads(full_archive._path.read_text(encoding="utf-8"))  # exactly the previous release's parse
    lines, _ = _old_reader_lines(raw, full_archive._leads, 0.0)
    assert _lines_state(lines) == _state(full_archive)["lines"]


def test_v3_archive_uses_the_same_layout(tmp_path: Path) -> None:
    arc = V3Archive(tmp_path, entry_id="z")
    arc.put(CA_LINE, 1_768_000_000, "1h", 61.5)
    arc.put(CA_LINE, 1_768_001_800, "24h", 62.5)
    assert arc.save()
    again = V3Archive(tmp_path, entry_id="z")
    again.load()
    assert again.data == arc.data and tuple(json.loads(arc._path.read_text())["leads"]) == V3_ARCHIVE_LEADS


def test_v4_save_encodes_one_slot_at_a_time(full_archive: V4Archive, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    real = orjson.dumps
    monkeypatch.setattr(orjson, "dumps", lambda o, *a, **k: (sizes.append(len(r := real(o, *a, **k))), r)[1])
    assert full_archive.save()
    assert len(sizes) > 3 * 19000 and max(sizes) < 8000  # the largest call is the 288-point "last" curve


_NOW = int(time.time()) // SLOT * SLOT


def _rows_case(name: str) -> dict[str, Any]:
    base = _NOW - 50 * 86400
    e = [base + i * SLOT for i in range(60)]
    rows: dict[str, Any] = {str(x): [60.0 + k, 61.0 + k, None, 63.0, 64.0, 65.0, 66.0] for k, x in enumerate(e)}
    if name == "all_invalid_edges":  # an all-invalid row at each end must not grow the line
        rows = {str(e[0] - 4 * SLOT): [None, "x", True, float("nan"), None, None, None], **rows}
        rows[str(e[-1] + 9 * SLOT)] = [float("inf"), None, None, None, None, None, None]
    elif name == "gaps":
        for x in e[10:25]:
            del rows[str(x)]
        for x in e[30:33]:
            rows[str(x)] = [None] * 7
    elif name == "dropped_days":  # slots before the cutoff are dropped; earlier than the data's own start
        rows = {str(base - 90 * 86400 + i * SLOT): [50.0 + i] * 7 for i in range(30)} | rows
    elif name == "odd_values":
        rows[str(e[3])] = [True, 1, "2", 3.5, [4], {"a": 5}, -1.25]
        rows[str(e[4])] = [1] * 12  # longer than the file's lead list
        rows[str(e[5])] = [1]  # shorter
        rows[str(e[6])] = "not a list"
        rows["not-an-epoch"] = [1] * 7
        rows[str(e[7] + 0)] = rows[str(e[7])]  # same epoch again below via another spelling
        rows["+" + str(e[7])] = [99.0] * 7  # same epoch: the first write wins
        rows[str(e[8]) + ".5"] = [5.0] * 7  # not an int key
    elif name == "off_grid":  # a hand-edited file: epochs off the 30-minute grid (order-dependent per-value path)
        rows = {str(base + 7): [1.0] * 7, str(base + 1000): [2.0] * 7, str(base - 500): [3.0] * 7, **rows}
    elif name == "late_start_lead":  # a lead with no value for the first days (a short column start)
        for x in e[:20]:
            rows[str(x)][6] = None
            rows[str(x)][5] = None
    return rows


@pytest.mark.parametrize(
    "case", ["plain", "all_invalid_edges", "gaps", "dropped_days", "odd_values", "off_grid", "late_start_lead"]
)
def test_columnar_load_equals_the_per_value_load(case: str) -> None:
    rows = _rows_case(case)
    file_leads = list(V4_ARCHIVE_LEADS)
    cutoff = float(_NOW - 60 * 86400)
    want, want_slots = _old_reader_lines({"leads": file_leads, "sensors": {"s": rows}}, V4_ARCHIVE_LEADS, cutoff)
    line, got_slots = va._parse_line(rows, file_leads, V4_ARCHIVE_LEADS, cutoff)
    assert got_slots == want_slots
    assert _lines_state({"s": line} if line is not None else {}) == _lines_state(want)
    if case == "all_invalid_edges":  # the line is exactly the 60 real slots
        assert line is not None and len(line) == 60


def test_columnar_load_with_unknown_and_repeated_leads_and_a_leading_all_invalid_row() -> None:
    rows = {
        str(_NOW - 5 * SLOT): [None, None],
        str(_NOW): [70.0, 71.0, 72.0, 73.0],
        str(_NOW + SLOT): [None, 5.0, 6.0, 7.0],
    }
    for file_leads in (["1h", "9h", "4h", "1h"], ["4h", "1h", "4h", "4h"], ["1h", "4h", "12h"]):
        want, w = _old_reader_lines({"leads": file_leads, "sensors": {"s": rows}}, V4_ARCHIVE_LEADS, 0.0)
        line, g = va._parse_line(rows, file_leads, V4_ARCHIVE_LEADS, 0.0)
        assert g == w and _lines_state({"s": line} if line else {}) == _lines_state(want), file_leads


def test_load_with_the_columnar_parse_equals_the_old_load_on_a_whole_file(
    full_archive: V4Archive, tmp_path: Path
) -> None:
    payload = _old_payload(full_archive)
    path = tmp_path / "climate_advisor_v4_archive_x.json"
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    arc = V4Archive(tmp_path, entry_id="x")
    arc.load()
    want, _ = _old_reader_lines(
        json.loads(path.read_text()), arc._leads, datetime.now().timestamp() - arc._max_days * 86400
    )
    assert _lines_state(arc._lines) == _lines_state(want)
    assert arc.ca == payload["ca"] and arc._last is not None


# ---- window reads


def _old_series(arc: V4Archive, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
    """The previous release's ``series`` (verbatim): scans the whole line."""
    if sensor == CA_LINE and arc._ca_active():
        out: dict[int, float] = {}
        for epoch in arc._ca_slots():
            if start <= epoch < end:
                v = arc._ca_value(epoch, lead)
                if v is not None:
                    out[epoch] = v
        return out
    line = arc._lines.get(sensor)
    if line is None:
        return {}
    out = {}
    for epoch in line.slots():
        if start <= epoch < end:
            v = line.get(epoch, lead)
            if v is not None:
                out[epoch] = v
    return out


def _window_archive(tmp_path: Path) -> V4Archive:
    arc = V4Archive(tmp_path, entry_id="w")
    _fill(arc, sensors=2, days=14, seed=7)
    # a stored "_ca" line older than the primary node line (a _ca-only model's history, Issue #1400)
    first = min(ln.base for ln in arc._lines.values() if ln.base is not None)
    for k in range(40):
        arc.put(CA_LINE, first - (k + 3) * SLOT, "1h", 60.0 + k * 0.1)
        arc.put(CA_LINE, first - (k + 3) * SLOT, "12h", 61.0 + k * 0.1)
    arc.set_ca_source("sensor.s0", sleep="sensor.s1", sleep_time="22:30", wake_time="07:00", tz="America/Chicago")
    return arc


def test_window_series_equals_the_whole_line_filter_across_edges(tmp_path: Path) -> None:
    arc = _window_archive(tmp_path)
    node = arc._lines["sensor.s0"]
    assert node.base is not None and CA_LINE in arc._lines
    top = node.base + len(node) * SLOT
    edges = [
        -INF,
        INF,
        0.0,
        node.base - 10 * SLOT,
        node.base - 1,
        node.base,
        node.base + 1,
        node.base + SLOT - 1,
        node.base + SLOT,
        node.base + 5 * SLOT + 0.5,
        node.base + 100 * SLOT,
        (node.base + top) / 2,
        top - SLOT,
        top - 1,
        top,
        top + 1,
        top + 10 * SLOT,
    ]
    for sensor in ("sensor.s0", "sensor.s1", CA_LINE, "sensor.unknown"):
        for lead in (*V4_ARCHIVE_LEADS[:2], V4_ARCHIVE_LEADS[-1], "99h"):
            for start in edges:
                for end in edges:
                    assert arc.series(sensor, lead, start, end) == _old_series(arc, sensor, lead, start, end), (
                        sensor,
                        lead,
                        start,
                        end,
                    )


def test_window_series_ca_only_model_and_empty_archive(tmp_path: Path) -> None:
    arc = V4Archive(tmp_path, entry_id="c")
    assert arc.series("x", "1h", -INF, INF) == {} and arc.series(CA_LINE, "1h", -INF, INF) == {}
    for k in range(20):
        arc.put(CA_LINE, _NOW + k * SLOT, "1h", 60.0 + k)  # primary == "_ca": no node line, the stored line answers
    arc.set_ca_source(CA_LINE)
    for start, end in ((-INF, INF), (_NOW + 3 * SLOT, _NOW + 9 * SLOT), (_NOW + 3 * SLOT, _NOW + 3 * SLOT)):
        assert arc.series(CA_LINE, "1h", start, end) == _old_series(arc, CA_LINE, "1h", start, end)


def test_span_is_the_first_and_last_slot_with_a_value(tmp_path: Path) -> None:
    arc = _window_archive(tmp_path)
    for sensor in ("sensor.s0", "sensor.s1", CA_LINE, "sensor.unknown"):
        for lead in (*V4_ARCHIVE_LEADS, "99h"):
            full = _old_series(arc, sensor, lead, -INF, INF)
            assert arc.span(sensor, lead) == ((min(full), max(full)) if full else None), (sensor, lead)
    assert arc.oldest_slot() == min(min(ln.slots()) for ln in arc._lines.values())
    assert V4Archive(tmp_path / "none", entry_id="n").oldest_slot() is None


# --------------------------------------------------------------------------------------------- cycle store


def _cycles(store: CycleStore, n: int, samples: int = 60) -> None:
    rnd = random.Random(3)
    t0 = 1_768_000_000.0
    for i in range(n):
        mode = "heat" if i % 2 else "cool"
        t_on = t0 + i * 3600.0
        store.add(
            {
                "id": f"{mode}-{i}",
                "mode": mode,
                "t_on": t_on,
                "t_off": t_on + 1000,
                "cut": None,
                "res": 0.01,
                "samples": [[t_on + k * 40.0, 68.0 + rnd.random(), 1 if k < 30 else 0] for k in range(samples)],
                "outdoor": [[t_on, 41.3], [t_on + 600, 41.5]],
                "setpoint_f": 70.0,
                "entity": "sensor.hall",
            },
            now=t0 + n * 3600.0,
        )


def _old_cycle_bytes(store: CycleStore) -> bytes:
    payload = {"v": CYCLE_STORE_VERSION, "cycles": [_encode(c) for c in list(store._cycles)]}
    return json.dumps(payload, separators=(",", ":")).encode()


def test_cycle_store_new_file_old_file_and_downgrade(tmp_path: Path) -> None:
    now = 1_768_000_000.0 + 2000 * 3600.0
    store = CycleStore(tmp_path / "cycles.json", max_days=100000)
    _cycles(store, 1500)
    assert store.save()
    new_bytes = store._path.read_bytes()
    assert json.loads(new_bytes) == json.loads(_old_cycle_bytes(store))  # same document (stdlib reader)
    assert new_bytes.count(b"\n") >= 1500  # one cycle per line

    again = CycleStore(store._path, max_days=100000)
    again.load(now=now)
    assert again.cycles() == store.cycles()

    old = CycleStore(tmp_path / "old.json", max_days=100000)
    old._path.write_bytes(_old_cycle_bytes(store))
    old.load(now=now)
    assert old.cycles() == store.cycles()

    # downgrade: the previous release's parse (json.loads of the text) of the new file, then the same decode
    raw = json.loads(new_bytes.decode("utf-8"))
    assert raw["cycles"] == json.loads(_old_cycle_bytes(store))["cycles"]


def test_cycle_store_load_parses_one_cycle_at_a_time(tmp_path: Path, parsed_sizes: list[int]) -> None:
    store = CycleStore(tmp_path / "cycles.json", max_days=100000)
    _cycles(store, 800, samples=100)
    assert store.save()
    CycleStore(store._path, max_days=100000).load(now=1_768_000_000.0 + 2000 * 3600.0)
    assert len(parsed_sizes) >= 800 and max(parsed_sizes) < store._path.stat().st_size / 100


def test_cycle_store_save_encodes_one_cycle_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CycleStore(tmp_path / "cycles.json", max_days=100000)
    _cycles(store, 800, samples=400)
    sizes: list[int] = []
    real = orjson.dumps
    monkeypatch.setattr(orjson, "dumps", lambda o, *a, **k: (sizes.append(len(r := real(o, *a, **k))), r)[1])
    assert store.save()
    whole = store._path.stat().st_size
    assert len(sizes) >= 800 and max(sizes) < whole / 500


# --------------------------------------------------------------------------------------------- forecast archive


def _forecast(tmp_path: Path, n: int = 400) -> ForecastArchive:
    arch = ForecastArchive(tmp_path, entry_id="f")
    t = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=n)
    for i in range(n):
        issued = t + timedelta(hours=i)
        arch.record(issued, "met.no", [(issued + timedelta(hours=h), 50.0 + (i + h) % 17 * 0.5) for h in range(96)])
    return arch


def test_forecast_archive_formats(tmp_path: Path) -> None:
    arch = _forecast(tmp_path)
    assert len(arch._snapshots) == 400
    arch.save()
    new_bytes = arch._path.read_bytes()
    assert json.loads(new_bytes) == json.loads(json.dumps({"snapshots": arch._snapshots}, separators=(",", ":")))
    assert new_bytes.count(b"\n") >= 400
    again = ForecastArchive(tmp_path, entry_id="f")
    again.load()
    assert again._snapshots == arch._snapshots
    old = ForecastArchive(tmp_path / "o", entry_id="f")
    (tmp_path / "o").mkdir()
    old._path.write_text(json.dumps({"snapshots": arch._snapshots}, separators=(",", ":")), encoding="utf-8")
    old.load()
    assert old._snapshots == arch._snapshots
    # the module function is what the method does (a worker decodes compact snapshots with it)
    snap = arch._snapshots[5]
    assert decode_snapshot(snap) == ForecastArchive._decode(snap)
    assert decode_snapshot(snap)["hours"][0][0] == snap["h0"]


def test_forecast_archive_load_parses_one_snapshot_at_a_time(tmp_path: Path, parsed_sizes: list[int]) -> None:
    arch = _forecast(tmp_path)
    arch.save()
    ForecastArchive(tmp_path, entry_id="f").load()
    assert len(parsed_sizes) >= 400 and max(parsed_sizes) < arch._path.stat().st_size / 100


def test_forecast_archive_save_encodes_one_snapshot_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = _forecast(tmp_path)
    sizes: list[int] = []
    real = orjson.dumps
    monkeypatch.setattr(orjson, "dumps", lambda o, *a, **k: (sizes.append(len(r := real(o, *a, **k))), r)[1])
    arch.save()
    assert len(sizes) >= 400 and max(sizes) < arch._path.stat().st_size / 100


# --------------------------------------------------------------------------------------------- chart log


def _chart(tmp_path: Path, n: int = 600) -> ChartStateLog:
    log = ChartStateLog(tmp_path, max_days=365, entry_id="c")
    t = datetime.now(UTC) - timedelta(minutes=5 * n)
    for i in range(n):
        log.append(
            hvac="cool" if i % 3 else "off",
            fan=bool(i % 2),
            indoor=70.0 + (i % 9) * 0.1,
            outdoor=80.0 - (i % 5),
            ts=(t + timedelta(minutes=5 * i)).isoformat(),
            setpoint=72.0,
            indoor_sensors={"sensor.a": 70.5, "sensor.b": None},
            event="café" if i == 5 else None,
        )
    return log


def test_chart_log_formats_and_old_files(tmp_path: Path) -> None:
    log = _chart(tmp_path)
    log.save()
    new_bytes = log._path.read_bytes()
    assert json.loads(new_bytes) == json.loads(json.dumps({"entries": log._entries}, separators=(",", ":")))
    assert new_bytes.count(b"\n") >= 600
    again = ChartStateLog(tmp_path, max_days=365, entry_id="c")
    again.load()
    assert again._entries == log._entries
    (tmp_path / "o").mkdir()
    old = ChartStateLog(tmp_path / "o", max_days=365, entry_id="c")
    old._path.write_text(json.dumps({"entries": log._entries}, separators=(",", ":")), encoding="utf-8")
    old.load()
    assert old._entries == log._entries


def test_chart_log_nan_is_written_as_null_and_an_old_nan_file_loads(tmp_path: Path) -> None:
    """Documented difference: a NaN reading is stored as ``null`` (every reader filters ``is not None``)."""
    log = _chart(tmp_path, 3)
    log._entries[1]["indoor"] = float("nan")
    log.save()
    assert json.loads(log._path.read_bytes())["entries"][1]["indoor"] is None
    (tmp_path / "o").mkdir()
    old = ChartStateLog(tmp_path / "o", max_days=365, entry_id="c")
    old._path.write_text(json.dumps({"entries": log._entries}, separators=(",", ":")), encoding="utf-8")  # NaN token
    old.load()
    assert math.isnan(old._entries[1]["indoor"])


def test_chart_log_load_parses_one_row_at_a_time(tmp_path: Path, parsed_sizes: list[int]) -> None:
    log = _chart(tmp_path)
    log.save()
    ChartStateLog(tmp_path, max_days=365, entry_id="c").load()
    assert len(parsed_sizes) >= 600 and max(parsed_sizes) < log._path.stat().st_size / 100


def test_chart_log_save_encodes_one_row_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _chart(tmp_path)
    sizes: list[int] = []
    real = orjson.dumps
    monkeypatch.setattr(orjson, "dumps", lambda o, *a, **k: (sizes.append(len(r := real(o, *a, **k))), r)[1])
    log.save()
    assert len(sizes) >= 600 and max(sizes) < log._path.stat().st_size / 100


# --------------------------------------------------------------------------------------------- learning state


def _learning(tmp_path: Path) -> LearningEngine:
    eng = LearningEngine(tmp_path, entry_id="l")
    day = datetime.now().date()
    for i in range(30):
        eng.record_day(
            DailyRecord(date=(day - timedelta(days=i)).isoformat(), day_type="mild", trend_direction="stable")
        )
    eng._state.thermal_observations = [{"k": 0.01 * i, "t": "café", "nan": float("nan")} for i in range(300)]
    return eng


def test_learning_state_keeps_the_stdlib_encoding_one_entry_per_line(tmp_path: Path) -> None:
    """Lossless: NaN tokens and ASCII escapes are kept (the previous reader reads the file without a codec)."""
    from dataclasses import asdict

    eng = _learning(tmp_path)
    eng.save_state()
    data = eng._db_path.read_bytes()
    assert data.isascii() and b"NaN" in data and b"\\u00e9" in data and data.count(b"\n") > 300
    old = json.loads(data.decode())
    assert math.isnan(old["thermal_observations"][0]["nan"])  # the previous release's parse
    again = LearningEngine(tmp_path, entry_id="l")
    again.load_state()
    got, want = asdict(again._state), asdict(eng._state)
    assert [r["date"] for r in got["records"]] == [r["date"] for r in want["records"]]
    assert got["thermal_observations"][3]["t"] == "café" and math.isnan(got["thermal_observations"][3]["nan"])
    # an older (indent=2, whole-file) file still loads
    eng._db_path.write_text(json.dumps(asdict(eng._state), indent=2), encoding="utf-8")
    third = LearningEngine(tmp_path, entry_id="l")
    third.load_state()
    assert [r["date"] for r in third._state.records] == [r["date"] for r in want["records"]]
    assert len(third._state.thermal_observations) == 300


def test_learning_load_parses_one_entry_at_a_time(tmp_path: Path, parsed_sizes: list[int]) -> None:
    eng = _learning(tmp_path)
    for obs in eng._state.thermal_observations:
        del obs["nan"]  # a NaN token makes the line reader hand over to the whole-file stdlib read (tested above)
    eng.save_state()
    LearningEngine(tmp_path, entry_id="l").load_state()
    assert len(parsed_sizes) > 300 and max(parsed_sizes) < eng._db_path.stat().st_size / 20


# --------------------------------------------------------------------------------------------- worker pipe


def test_worker_pipe_payload_keeps_floats_and_is_one_element_per_line() -> None:
    from custom_components.climate_advisor import process_offload as po

    payload = {"rows": [{"t": i * 1.5, "v": float("nan") if i == 3 else i * 0.1} for i in range(40)], "xs": [1, 2]}
    raw = po._encode("v4_fit", payload, 10)
    assert raw.count(b"\n") > 40 and b"NaN" in raw
    msg = po._decode(raw)
    assert msg["job"] == "v4_fit" and msg["log_level"] == 10 and math.isnan(msg["payload"]["rows"][3]["v"])
    assert json.loads(raw)["payload"]["xs"] == [1, 2]


def test_worker_pipe_decode_parses_one_row_at_a_time(parsed_sizes: list[int]) -> None:
    from custom_components.climate_advisor import process_offload as po

    raw = po._encode("v4_fit", {"rows": [{"t": i, "v": [i * 0.5] * 5} for i in range(2000)]}, 20)
    parsed_sizes.clear()
    assert len(po._decode(raw)["payload"]["rows"]) == 2000
    assert len(parsed_sizes) >= 2000 and max(parsed_sizes) < len(raw) / 100
