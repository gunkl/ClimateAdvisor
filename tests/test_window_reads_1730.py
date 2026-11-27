"""Issue #1730: a page or job that needs a window of a store reads that window and gets the whole read's answer.

Occupant view: the Status chart, the Prediction accuracy page and the nightly fits used to read a year of archive and
chart log to show a day. They read the day now; nothing they show or learn changes. Every test here compares the
windowed read with the whole read on the same data (a dense archive, one with gaps and holes, a ``_ca``-only one, an
empty one; a sorted chart log and an out-of-order one), and the work-count tests pin that the window is all that is read
(each fails if the windowed path is reverted to a whole read).

Reference side: ``reference_build_chart_v4`` is the whole-archive version of ``build_chart_v4`` (it asks the archive for
every slot of every look-ahead and filters), kept here as the oracle; the accuracy page's oracle is the same page built
without the log's sorted view, which reads every row.
"""

from __future__ import annotations

import json
import math
import random
from array import array
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import chart_log as cl
from custom_components.climate_advisor import coordinator as coordinator_mod
from custom_components.climate_advisor import night_replay as nr
from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor import v4_archive as va
from custom_components.climate_advisor import v4_shadow as vs
from custom_components.climate_advisor.const import V3_ARCHIVE_LEADS, V4_ARCHIVE_LEADS
from custom_components.climate_advisor.forecast_archive import ForecastArchive, _parse_iso, decode_snapshot
from custom_components.climate_advisor.v4_archive import V4Archive, _Line
from tests import test_offload_jobs_1722 as oj

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=TZ)
NOW_TS = NOW.timestamp()
NOW_UTC = NOW.astimezone(UTC)
SLOT = 1800
SENSORS = [f"sensor.s{i}" for i in range(5)]
RANGES = ("6h", "12h", "24h", "3d", "7d", "30d", "1y")
NAN = float("nan")
INF = float("inf")


# --------------------------------------------------------------------------------------------------- fixtures
def _col(rnd: random.Random, n: int, *, lo: int = 0, hi: int | None = None, drop: float = 0.0) -> array:
    """One look-ahead column: values from ``lo`` to ``hi`` (NaN outside it and at random ``drop`` share of slots)."""
    hi = n if hi is None else hi
    col = array("f", [NAN]) * n
    for i in range(max(lo, 0), min(hi, n)):
        if drop and rnd.random() < drop:
            continue
        col[i] = round(60 + rnd.random() * 15, 2)
    return col


def _make_line(leads: Sequence[str], base: int, n: int, rnd: random.Random, **kw: Any) -> _Line:
    line = _Line(tuple(leads))
    line.base = base
    for lead in leads:
        line.cols[lead] = _col(rnd, n, **kw)
    return line


def make_archive(tmp_path: Path, shape: str, n_sensors: int = 3, days: int = 40, seed: int = 1) -> V4Archive:
    rnd = random.Random(seed)
    arc = V4Archive(tmp_path / f"a_{shape}_{n_sensors}", entry_id="z")
    top = int(NOW_TS) // SLOT * SLOT
    n = days * 48
    base = top - n * SLOT
    ids = SENSORS[:n_sensors]
    if shape == "dense":
        for sid in ids:
            arc._lines[sid] = _make_line(V4_ARCHIVE_LEADS, base, n, rnd)
    elif shape == "gaps":
        for k, sid in enumerate(ids):
            line = _make_line(V4_ARCHIVE_LEADS, base + k * 5 * 48 * SLOT, n - k * 5 * 48, rnd, drop=0.2)
            for j, lead in enumerate(V4_ARCHIVE_LEADS):  # each look-ahead starts later and ends earlier than the next
                col = line.cols[lead]
                for i in range(min(j * 37, len(col))):
                    col[i] = NAN
                for i in range(max(len(col) - j * 11, 0), len(col)):
                    col[i] = NAN
            if k == 0:  # a 3-day hole in every look-ahead
                for col in line.cols.values():
                    for i in range(600, 744):
                        col[i] = NAN
            if k == 2:  # a line that exists but holds nothing
                line.cols = {lead: array("f", [NAN]) * len(col) for lead, col in line.cols.items()}
            arc._lines[sid] = line
    elif shape == "short":
        for sid in ids:
            arc._lines[sid] = _make_line(V4_ARCHIVE_LEADS, top - 96 * SLOT, 96, rnd)
    elif shape == "ca_only":  # a _ca-only model: one stored "_ca" line, nothing derivable
        arc._lines[va.CA_LINE] = _make_line(V4_ARCHIVE_LEADS, base, n, rnd, drop=0.1)
        return arc
    elif shape == "mixed":  # node lines plus an older stored "_ca" the node lines do not cover (Issue #1400)
        for sid in ids:
            arc._lines[sid] = _make_line(V4_ARCHIVE_LEADS, base + 10 * 48 * SLOT, n - 10 * 48, rnd, drop=0.1)
        arc._lines[va.CA_LINE] = _make_line(V4_ARCHIVE_LEADS, base, 15 * 48, rnd)
    elif shape == "empty":
        return arc
    else:
        raise AssertionError(shape)
    arc.set_ca_source(
        ids[0],
        sleep=ids[1] if len(ids) > 1 else None,
        sleep_time="22:30",
        wake_time="07:00",
        tz="America/Chicago",
    )
    return arc


def make_v3(arc: V4Archive) -> V4Archive:
    """v3's archive: one stored ``_ca`` line at 1/4/12/24 h, borrowed from the v4 archive's first line."""
    v3 = va.V3Archive(Path("."), entry_id="z")
    src = next(iter(arc._lines.values()), None)
    if src is not None and src.base is not None:
        line = _Line(V3_ARCHIVE_LEADS)
        line.base = src.base
        line.cols = {k: array("f", src.cols[k]) for k in V3_ARCHIVE_LEADS}
        v3._lines = {va.CA_LINE: line}
    return v3


def make_rows(
    days: float = 20,
    *,
    seed: int = 2,
    n_sensors: int = 3,
    band: str = "all",
    holes: Sequence[tuple[float, float]] = (),
) -> list[dict[str, Any]]:
    """Time-sorted chart rows (30 min), some event rows, some missing readings, ``holes`` = (from, to) days back."""
    rnd = random.Random(seed)
    rows: list[dict[str, Any]] = []
    n = int(days * 48)
    for k in range(n, -1, -1):
        t = (NOW_UTC - timedelta(minutes=30 * k)).astimezone(TZ)  # UTC steps: a zone's own DST fold is not a step
        if any(a * 48 >= k >= b * 48 for a, b in holes):
            continue
        ind = round(66 + rnd.random() * 6, 2)
        row: dict[str, Any] = {
            "ts": t.isoformat(),
            "hvac": rnd.choice(["idle", "heating", "heating", "cooling"]),
            "fan": False,
            "indoor": None if rnd.random() < 0.05 else ind,
            "outdoor": round(50 + rnd.random() * 30, 1),
            "pred_indoor": round(ind + rnd.random() - 0.5, 2) if rnd.random() < 0.8 else None,
            "indoor_sensors": {s: round(ind + rnd.random() - 0.5, 2) for s in SENSORS[:n_sensors]},
        }
        if band == "all" or (band == "late" and k < n // 2) or (band == "sparse" and rnd.random() < 0.3):
            row["band_sensor"] = SENSORS[1] if (t.hour >= 22 or t.hour < 7) else SENSORS[0]
        if rnd.random() < 0.02:
            row["event"] = "mode_change"
        rows.append(row)
    return rows


def make_log(tmp_path: Path, rows: list[dict[str, Any]]) -> cl.ChartStateLog:
    log = cl.ChartStateLog(tmp_path, max_days=400, entry_id="z")
    log._entries = list(rows)
    return log


def swapped(rows: list[dict[str, Any]], at: int) -> list[dict[str, Any]]:
    """The same rows with two neighbours out of time order (a clock stepped back)."""
    out = list(rows)
    out[at], out[at + 1] = out[at + 1], out[at]
    return out


def backtest_for(sensors: Sequence[str]) -> dict[str, Any]:
    pts = {}
    for k, sid in enumerate([va.CA_LINE, *sensors[:2], "sensor.unknown"]):
        pts[sid] = {
            lead: [
                {
                    "t": (NOW - timedelta(days=d, minutes=10 * k)).isoformat(),
                    "actual": 66.0,
                    "flat": 66.1,
                    "v4": 66.5 + d,
                }
                for d in (0.01, 0.4, 2, 9, 70, 200)
            ]
            for lead in ("1h", "4h", "24h")
        }
    return {"points": pts}


def last_future() -> dict[str, Any]:
    return {
        "at": NOW.isoformat(),
        "hourly_until": (NOW + timedelta(hours=48)).isoformat(),
        "predictions": {va.CA_LINE: [(NOW + timedelta(hours=h), 70.0 + h / 10) for h in range(1, 6)]},
        "baseline": {va.CA_LINE: [(NOW + timedelta(hours=h), 69.0) for h in range(1, 4)]},
    }


# ------------------------------------------------------------------------- the whole-archive build_chart_v4 oracle
REFERENCE_SRC = """
def reference_build_chart_v4(
    *, now, start, end, range_str, unit, historical, archive, backtest, model, last_future, sensor_ids,
    corrected_past=(), ca_swaps=(),
):
    tz = now.tzinfo or datetime.now().astimezone().tzinfo
    model_out = {"confidence": "none", "fitted_at": None}
    if isinstance(model, Mapping) and model.get("has_model"):
        model_out = {"confidence": model.get("confidence", "none"), "fitted_at": model.get("fitted_at")}
    out = {
        "ready": False, "reason": "no_model", "scores": pa.backtest_scores(backtest), "model": model_out,
        "leads": list(pa.CHART_LEADS), "sensors": [], "future": {}, "future_at": None, "hourly_until": None,
        "baseline": {}, "past": {}, "past_span": None, "breaks": {},
        "corrected_outdoor": {"past": [], "future": []},
    }
    has_model = bool(isinstance(model, Mapping) and model.get("has_model"))
    range_days = pa.RANGES.get(range_str, 1.0)
    bt_points = backtest.get("points") if isinstance(backtest, Mapping) else None
    ids = [pa.CA_SENSOR, *(s for s in dict.fromkeys(sensor_ids) if s != pa.CA_SENSOR)]
    model_primary = model.get("primary") if isinstance(model, Mapping) else None
    span = []
    span_sensors = []

    def _seen(t):
        if not span:
            span.extend((t, t))
        elif t < span[0]:
            span[0] = t
        elif t > span[1]:
            span[1] = t

    for sid in ids:
        per_lead = {}
        for lead in pa.CHART_LEADS:
            every = archive.series(sid, lead, float("-inf"), float("inf")) if archive is not None else {}
            for slot in every:
                _seen(float(slot))
            if every and sid not in span_sensors:
                span_sensors.append(sid)
            live = {slot: v for slot, v in every.items() if start <= slot < end}
            pairs = [(float(slot), v) for slot, v in live.items()]
            live_slots = set(live)
            by_sensor = bt_points.get(sid) if isinstance(bt_points, Mapping) else None
            rows = by_sensor.get(lead) if isinstance(by_sensor, Mapping) else None
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, Mapping):
                    continue
                t = pa._epoch(item.get("t"))
                v4 = pa._num(item.get("v4"))
                if t is None or v4 is None:
                    continue
                _seen(t)
                if sid not in span_sensors:
                    span_sensors.append(sid)
                if not (start <= t < end):
                    continue
                if pa.slot_key(datetime.fromtimestamp(t, tz)) in live_slots:
                    continue
                pairs.append((t, v4))
            if pairs:
                per_lead[lead] = pa._out_pairs(pa._downsample_pairs(pairs, range_days, tz), tz, unit)
        if per_lead:
            out["past"][sid] = per_lead
    if span and span[0] <= now.timestamp():
        out["past_span"] = {
            "first": pa._iso(span[0], tz),
            "last": pa._iso(min(span[1], now.timestamp()), tz),
            "sensors": span_sensors,
        }

    def _line(lines, sid):
        if not isinstance(lines, Mapping):
            return []
        got = lines.get(sid)
        if not got and sid == pa.CA_SENSOR and isinstance(model_primary, str):
            got = lines.get(model_primary)
        return got or []

    future = last_future.get("predictions") if isinstance(last_future, Mapping) else None
    if not historical and isinstance(future, Mapping):
        now_ts = now.timestamp()
        for sid in ids:
            pts = [(t.timestamp(), v) for t, v in _line(future, sid) if t.timestamp() > now_ts and math.isfinite(v)]
            if pts:
                out["future"][sid] = pa._out_pairs(pts, tz, unit)
        out["future_at"] = last_future.get("at")
        out["hourly_until"] = last_future.get("hourly_until")
        base = last_future.get("baseline")
        for sid in ids:
            pts = [(t.timestamp(), v) for t, v in _line(base, sid) if t.timestamp() > now_ts and math.isfinite(v)]
            if pts:
                out["baseline"][sid] = pa._out_pairs(pts, tz, unit)
    out["corrected_outdoor"]["past"] = pa._out_pairs(
        pa._downsample_pairs([(t.timestamp(), v) for t, v in corrected_past], range_days, tz), tz, unit
    )
    out["sensors"] = sorted(set(out["past"]) | set(out["future"]) | set(out["baseline"]), key=ids.index)
    if pa.CA_SENSOR in out["sensors"] and range_days <= pa._HOURLY_MAX_DAYS:
        out["breaks"][pa.CA_SENSOR] = [pa._iso(t, tz) for t in sorted(set(ca_swaps))]
    out["ready"] = has_model or bool(out["sensors"])
    out["reason"] = "ok" if out["ready"] else "no_model"
    return out
"""
_ns: dict[str, Any] = {"pa": pa, "datetime": datetime, "Mapping": Mapping, "math": math}
exec(REFERENCE_SRC, _ns)  # noqa: S102 - the oracle, defined above in this file
reference_build_chart_v4 = _ns["reference_build_chart_v4"]


def chart_kwargs(arc: V4Archive | None, sensors: Sequence[str], range_str: str, historical: bool) -> dict[str, Any]:
    end = NOW_TS
    return {
        "now": NOW,
        "start": end - pa.RANGES[range_str] * 86400,
        "end": end,
        "range_str": range_str,
        "unit": "fahrenheit",
        "historical": historical,
        "archive": arc,
        "backtest": backtest_for(sensors),
        "model": {"has_model": True, "confidence": "low", "fitted_at": "x", "primary": sensors[0] if sensors else None},
        "last_future": last_future(),
        "sensor_ids": list(sensors),
        "corrected_past": [(NOW - timedelta(hours=h), 50.0 + h) for h in range(5)],
        "ca_swaps": [NOW_TS - 3600],
    }


# ------------------------------------------------------------------------------------------- the archive reads
@pytest.mark.parametrize("shape", ["dense", "gaps", "short", "ca_only", "mixed", "empty"])
def test_archive_window_span_and_slot_presence_equal_the_whole_read(tmp_path: Path, shape: str) -> None:
    arc = make_archive(tmp_path, shape, n_sensors=3)
    rnd = random.Random(7)
    ids = [*arc.sensor_ids(), "sensor.nothing"]
    for sid in ids:
        for lead in V4_ARCHIVE_LEADS:
            full = arc.series(sid, lead, -INF, INF)
            ends = arc.span(sid, lead)
            assert ends == ((min(full), max(full)) if full else None), (sid, lead)
            for _ in range(8):
                a = NOW_TS - rnd.random() * 45 * 86400
                b = a + rnd.random() * 20 * 86400
                assert arc.series(sid, lead, a, b) == {k: v for k, v in full.items() if a <= k < b}
    assert arc.sensors_with_slots() == {s for s, c in arc.slot_count().items() if c}
    # several look-aheads in one pass = one pass each
    for sid in ids:
        for a, b in ((-INF, INF), (NOW_TS - 9 * 86400, NOW_TS - 2.5 * 86400), (NOW_TS - 86400, NOW_TS)):
            assert arc.series_leads(sid, V4_ARCHIVE_LEADS, a, b) == {
                lead: arc.series(sid, lead, a, b) for lead in V4_ARCHIVE_LEADS
            }, (sid, a, b)
    assert arc.series_leads("_ca", ("nope", "4h"), -INF, INF)["nope"] == {}


def test_the_derived_ca_follows_a_change_of_sleep_settings(tmp_path: Path) -> None:
    """``series_leads`` reads the bedtime, wake-up and zone of the moment, as ``series`` does."""
    arc = make_archive(tmp_path, "dense", n_sensors=3)
    before = arc.series(va.CA_LINE, "4h", -INF, INF)
    for sleep_time, wake_time, tz in (("23:30", "06:00", "America/Chicago"), ("22:30", "07:00", "Asia/Tokyo")):
        arc.set_ca_source(SENSORS[0], sleep=SENSORS[1], sleep_time=sleep_time, wake_time=wake_time, tz=tz)
        fresh = make_archive(tmp_path, "dense", n_sensors=3)
        fresh.set_ca_source(SENSORS[0], sleep=SENSORS[1], sleep_time=sleep_time, wake_time=wake_time, tz=tz)
        got = arc.series(va.CA_LINE, "4h", -INF, INF)
        assert got == fresh.series(va.CA_LINE, "4h", -INF, INF) and got != before
        assert arc.series_leads(va.CA_LINE, ("4h",), -INF, INF)["4h"] == got
    arc.set_ca_source(SENSORS[0], sleep=SENSORS[1], sleep_time="22:30", wake_time="07:00", tz="America/Chicago")
    assert arc.series(va.CA_LINE, "4h", -INF, INF) == before


# ---------------------------------------------------------------------------------------------- build_chart_v4
@pytest.mark.parametrize(
    ("shape", "n_sensors"),
    [("dense", 1), ("dense", 3), ("dense", 5), ("gaps", 1), ("gaps", 3), ("gaps", 5)]
    + [(s, 3) for s in ("short", "ca_only", "mixed", "empty")],
)
def test_build_chart_v4_equals_the_whole_archive_read(tmp_path: Path, shape: str, n_sensors: int) -> None:
    arc = make_archive(tmp_path, shape, n_sensors)
    sensors = SENSORS[:n_sensors]
    for range_str in RANGES:
        for historical in (False, True):
            kw = chart_kwargs(arc, sensors, range_str, historical)
            got = pa.build_chart_v4(**kw)
            want = reference_build_chart_v4(**kw)
            assert got == want, (shape, n_sensors, range_str, historical)
    if shape in ("dense", "gaps"):
        assert got["past"], "the fixture must give the page something to draw"
        assert got["past_span"] is not None


def test_downsample_with_a_shared_bucket_memo_is_the_same_and_buckets_each_slot_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rnd = random.Random(4)
    base = NOW_TS - 400 * 86400
    first = [(base + i * SLOT, rnd.random() * 10) for i in range(2000)]
    second = [(t, v + 1) for t, v in first[::3]]
    for days in (7, 30, 365):
        memo = pa._BucketMemo()
        assert pa._downsample_pairs(first, days, TZ, memo) == pa._downsample_pairs(first, days, TZ)
        assert pa._downsample_pairs(second, days, TZ, memo) == pa._downsample_pairs(second, days, TZ)
        assert len(memo.by_epoch) == len(first)
    calls: list[float] = []
    real = datetime

    class Counting(datetime):
        @classmethod
        def fromtimestamp(cls, t: float, tz: Any = None) -> datetime:
            calls.append(t)
            return real.fromtimestamp(t, tz)

    monkeypatch.setattr(pa, "datetime", Counting)
    memo2 = pa._BucketMemo()
    for _ in range(5):
        pa._downsample_pairs(first, 365, TZ, memo2)
    assert len(calls) == len(first)


def test_build_chart_v4_without_an_archive_or_backtest_is_unchanged() -> None:
    kw = {**chart_kwargs(None, SENSORS[:2], "24h", False), "backtest": None}
    assert pa.build_chart_v4(**kw) == reference_build_chart_v4(**kw)
    kw["archive"] = None
    assert pa.build_chart_v4(**kw)["past_span"] is None


def test_build_chart_v4_reads_only_the_requested_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole-line read is gone: every read is the window, and no line is listed slot by slot."""
    arc = make_archive(tmp_path, "dense", n_sensors=5, days=60)
    calls: list[tuple[str, float, float]] = []
    real = V4Archive.series_leads

    def spy(self: V4Archive, sensor: str, leads: Sequence[str], start: float, end: float) -> dict:
        calls.append((sensor, start, end))
        return real(self, sensor, leads, start, end)

    def no_listing(self: _Line) -> list[int]:
        raise AssertionError("a whole line was listed")

    def no_whole_series(self: V4Archive, sensor: str, lead: str, start: float, end: float) -> dict[int, float]:
        assert math.isfinite(start) and math.isfinite(end), "a whole line was read"
        return V4Archive.__dict__["_real_series"](self, sensor, lead, start, end)

    monkeypatch.setattr(V4Archive, "series_leads", spy)
    monkeypatch.setattr(_Line, "slots", no_listing)
    monkeypatch.setattr(V4Archive, "_real_series", V4Archive.series, raising=False)
    monkeypatch.setattr(V4Archive, "series", no_whole_series)
    kw = chart_kwargs(arc, SENSORS, "24h", False)
    pa.build_chart_v4(**kw)
    assert [c[0] for c in calls] == [va.CA_LINE, *SENSORS]  # once per sensor, all look-aheads together
    assert {(a, b) for _s, a, b in calls} == {(kw["start"], kw["end"])}


def test_the_derived_ca_asks_the_sleep_window_once_per_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A year of ``_ca`` is 7 look-aheads x every slot; the sleep answer is asked per slot, not per value."""
    arc = make_archive(tmp_path, "dense", n_sensors=3, days=40)
    asked: list[int] = []
    real = V4Archive._in_sleep

    def counting(self: V4Archive, epoch: int) -> bool:
        asked.append(epoch)
        return real(self, epoch)

    monkeypatch.setattr(V4Archive, "_in_sleep", counting)
    pa.build_chart_v4(**chart_kwargs(arc, SENSORS[:3], "1y", False))
    slots = len(arc._lines[SENSORS[0]].slots())
    assert slots <= len(asked) < slots + 50  # one per slot, plus a few for the span ends (7 look-aheads would be 7x)


# ------------------------------------------------------------------------------------ ChartStateLog.rows_between
def test_rows_between_is_the_scan_for_sorted_logs_and_none_otherwise(tmp_path: Path) -> None:
    rows = make_rows(10)
    log = make_log(tmp_path, rows)
    rnd = random.Random(3)
    for _ in range(60):
        lo = NOW_TS - rnd.random() * 12 * 86400
        hi = lo + rnd.random() * 5 * 86400
        want = [r for r in rows if lo <= datetime.fromisoformat(r["ts"]).timestamp() <= hi]
        assert log.rows_between(lo, hi) == want
    first, last = (datetime.fromisoformat(rows[i]["ts"]).timestamp() for i in (0, -1))
    assert log.rows_between(first, first) == [rows[0]]  # both ends are inclusive
    assert log.rows_between(last, last + 1) == [rows[-1]]
    assert log.rows_between(last + 1, last + 2) == [] and log.rows_between(2, 1) == []
    assert make_log(tmp_path, []).rows_between(0, INF) == []
    # out of order (a clock stepped back): the caller scans
    assert make_log(tmp_path, swapped(rows, 100)).rows_between(0, INF) is None
    # one row appended later in order keeps the answer; one out of order drops it, as for get_entries (#1598)
    log.append(hvac="idle", fan=False, indoor=70.0, outdoor=50.0, ts=(NOW + timedelta(minutes=30)).isoformat())
    assert log.rows_between(last, INF) == [rows[-1], log._entries[-1]]
    log.append(hvac="idle", fan=False, indoor=70.0, outdoor=50.0, ts=(NOW - timedelta(days=3)).isoformat())
    assert log.rows_between(0, INF) is None


def test_rows_between_leaves_a_row_without_an_offset_to_the_scan(tmp_path: Path) -> None:
    rows = make_rows(1)[:5]
    mid = datetime.fromisoformat(rows[2]["ts"]).astimezone(cl.UTC).replace(tzinfo=None)
    rows[2] = {**rows[2], "ts": mid.isoformat()}  # sorted as UTC, but a caller may read it in another zone
    log = make_log(tmp_path, rows)
    assert log._time_sorted_rows() is not None
    assert log.rows_between(0, INF) is None  # the middle probe meets it
    assert log.rows_between(NOW_TS + 1, NOW_TS + 2) is None


# --------------------------------------------------------------------------------------- the accuracy page
def accuracy_kwargs(arc: V4Archive, rows: list[dict], range_str: str, sensor: str, lead: str, **over: Any) -> dict:
    kw: dict[str, Any] = {
        "zone": "z",
        "sensor": sensor,
        "lead": lead,
        "range_str": range_str,
        "before": None,
        "now": NOW,
        "unit": "fahrenheit",
        "chart_rows": list(rows),
        "archive": arc,
        "backtest": backtest_for(SENSORS),
        "model": {"has_model": True, "confidence": "low", "fitted_at": "x", "span_days": 9},
        "sensors_meta": [{"id": "_ca", "name": "CA", "role": ""}]
        + [{"id": s, "name": s, "role": ""} for s in SENSORS[:4]],
        "v3_archive": make_v3(arc),
        "sleep_sensor": SENSORS[1],
        "sleep_time": "22:30",
        "wake_time": "07:00",
    }
    kw.update(over)
    return kw


@pytest.mark.parametrize("shape", ["dense", "gaps", "ca_only", "empty"])
@pytest.mark.parametrize("order", ["sorted", "out_of_order"])
def test_accuracy_page_is_the_same_with_the_sorted_view_as_without(tmp_path: Path, shape: str, order: str) -> None:
    arc = make_archive(tmp_path, shape, n_sensors=3)
    rows = make_rows(45, band="sparse", holes=[(20, 18.5)])
    rows = swapped(rows, 700) if order == "out_of_order" else rows
    log = make_log(tmp_path, rows)
    assert (log._time_sorted_rows() is not None) == (order == "sorted")
    n = 0
    for range_str in RANGES:
        for sensor, lead in ((va.CA_LINE, "4h"), (SENSORS[0], "1h"), (SENSORS[1], "24h")):
            kw = accuracy_kwargs(arc, rows, range_str, sensor, lead)
            got = pa.build_prediction_accuracy(**kw, chart_log=log)
            want = pa.build_prediction_accuracy(**kw)
            assert got == want, (shape, order, range_str, sensor, lead)
            n += 1
    assert n == len(RANGES) * 3
    # scrolled back, and with no sleep sensor (only the recorded band_sensor changes count)
    for before in (NOW - timedelta(days=6, hours=5), NOW - timedelta(days=100)):
        kw = accuracy_kwargs(arc, rows, "24h", va.CA_LINE, "4h", before=before, sleep_sensor=None)
        assert pa.build_prediction_accuracy(**kw, chart_log=log) == pa.build_prediction_accuracy(**kw)


def test_accuracy_page_has_something_to_compare(tmp_path: Path) -> None:
    """The fixture is not vacuous: v4, v3, actual, flat, heat runs, swaps and the table are all present."""
    arc = make_archive(tmp_path, "dense", 3)
    rows = make_rows(45, n_sensors=4)
    out = pa.build_prediction_accuracy(
        **accuracy_kwargs(arc, rows, "3d", va.CA_LINE, "4h"), chart_log=make_log(tmp_path, rows)
    )
    assert out["points"] and out["heat_runs"] and out["ca_swaps"] and out["stats"]["n"] > 50
    assert {"v4", "v3", "actual", "flat"} <= {k for p in out["points"] for k in p}
    assert out["gate"] and out["gate_summary"]["total"] == len(out["gate"])
    flags = {s["id"]: (s["has_v4"], s["has_readings"]) for s in out["sensors"]}
    assert flags["_ca"] == (True, True) and flags[SENSORS[3]] == (False, True)


def test_accuracy_page_parses_only_the_rows_it_reads(tmp_path: Path) -> None:
    """24 h on a year of rows: the sorted view parses the window and the scorecard's month, not the year."""
    rows = make_rows(365, band="sparse")
    log = make_log(tmp_path, rows)
    arc = make_archive(tmp_path, "dense", 3, days=40)
    kw = accuracy_kwargs(arc, rows, "24h", va.CA_LINE, "4h")
    pa._epoch_of_str.cache_clear()
    got = pa.build_prediction_accuracy(**kw, chart_log=log)
    windowed = pa._epoch_of_str.cache_info().misses
    pa._epoch_of_str.cache_clear()
    want = pa.build_prediction_accuracy(**kw)
    scanned = pa._epoch_of_str.cache_info().misses
    assert got == want
    assert scanned >= len(rows) * 0.95, "the unsorted path reads every row"
    assert windowed <= len(rows) * 0.12, (windowed, len(rows))  # the scorecard's 31 days + the day shown
    pa._epoch_of_str.cache_clear()


def test_accuracy_page_never_lists_a_whole_archive_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sensor flags ask each line for one slot; the series ask for their window: no ``slots()`` of a year."""
    arc = make_archive(tmp_path, "dense", 3, days=60)
    rows = make_rows(30)
    kw = accuracy_kwargs(arc, rows, "24h", va.CA_LINE, "4h")
    want = pa.build_prediction_accuracy(**kw)

    def no_listing(self: _Line) -> list[int]:
        raise AssertionError("a whole line was listed")

    monkeypatch.setattr(_Line, "slots", no_listing)
    assert pa.build_prediction_accuracy(**kw, chart_log=make_log(tmp_path, rows)) == want


def test_epoch_is_read_once_per_row_text() -> None:
    pa._epoch_of_str.cache_clear()
    ts = NOW.isoformat()
    assert [pa._epoch(ts) for _ in range(5)] == [NOW_TS] * 5
    info = pa._epoch_of_str.cache_info()
    assert (info.misses, info.hits) == (1, 4)
    assert pa._epoch(None) is None and pa._epoch("not a time") is None and pa._epoch(12) is None
    naive = "2026-11-24T12:00:00"
    assert pa._epoch(naive) == datetime(2026, 11, 24, 12).timestamp()  # a naive ts is read in the machine's zone


def test_swap_rows_give_the_swaps_of_every_row(tmp_path: Path) -> None:
    """``ca_swap_epochs`` on the window plus its nearest band rows = on every row."""
    rnd = random.Random(11)
    for band in ("all", "sparse", "late", "none"):
        rows = make_rows(30, band=band, holes=[(12, 9.5), (3, 2.6)])
        log = make_log(tmp_path, rows)
        view = pa._ChartRows(rows, log)
        assert view.view is not None
        for sleep_sensor in (SENSORS[1], None):
            for _ in range(80):
                lo = NOW_TS - rnd.random() * 34 * 86400
                hi = lo + rnd.random() * 6 * 86400 * rnd.choice([0.02, 0.2, 1])
                kw = {"tz": TZ, "sleep_sensor": sleep_sensor, "sleep_time": "22:30", "wake_time": "07:00"}
                got = pa.ca_swap_epochs(view.swap_rows(lo, hi), lo, hi, **kw)
                assert got == pa.ca_swap_epochs(rows, lo, hi, **kw), (band, lo, hi)
    # an unsorted log gives every row, exactly the old call
    bad = pa._ChartRows(swapped(rows, 5), make_log(tmp_path, swapped(rows, 5)))
    assert bad.view is None and bad.swap_rows(0, 1) is bad.rows and bad.window(0, 1) is bad.rows
    # the window is not everything: a 6 h window of a month reads a few rows and the rows next to it
    rows = make_rows(30)
    near = pa._ChartRows(rows, make_log(tmp_path, rows)).swap_rows(NOW_TS - 7 * 3600, NOW_TS - 1 * 3600)
    assert 12 <= len(near) <= 16


def test_read_sensor_ids_stops_early_with_the_same_answers() -> None:
    rows = make_rows(5, n_sensors=3)
    for r in rows[:100]:
        r["indoor_sensors"].pop(SENSORS[2])  # the third sensor only reads from row 100 on
    wanted = {va.CA_LINE, *SENSORS[:3], "sensor.never"}
    full = pa._read_sensor_ids(rows)
    assert pa._read_sensor_ids(rows, wanted) == full
    few = {va.CA_LINE, SENSORS[0]}
    assert few <= pa._read_sensor_ids(rows, few) <= full
    seen: list[int] = []

    class Counting(list):
        def __iter__(self):
            for i, r in enumerate(super().__iter__()):
                seen.append(i)
                yield r

    pa._read_sensor_ids(Counting(rows), few)
    assert max(seen) < 3, "everything asked for was found in the first rows"


# ----------------------------------------------------------------------------------------- the forecast archive
def make_forecast_archive(tmp_path: Path, days: int = 6, hours: int = 48) -> ForecastArchive:
    arch = ForecastArchive(tmp_path, entry_id="z")
    rnd = random.Random(5)
    t = NOW - timedelta(days=days)
    while t <= NOW:
        issued = t.replace(minute=7)
        first = issued.replace(minute=0)
        arch.record(
            issued,
            "weather.home",
            [(first + timedelta(hours=h), round(40 + rnd.random() * 20, 1)) for h in range(hours)],
        )
        t += timedelta(hours=1)
    return arch


def test_get_snapshots_raw_is_the_selection_get_snapshots_decodes(tmp_path: Path) -> None:
    arch = make_forecast_archive(tmp_path)
    assert len(arch._snapshots) > 100
    rnd = random.Random(9)
    for _ in range(40):
        a = NOW - timedelta(days=rnd.random() * 8)
        b = a + timedelta(hours=rnd.random() * 90)
        raw = arch.get_snapshots_raw(a, b)
        assert arch.get_snapshots(a, b) == [decode_snapshot(s) for s in raw]
        assert raw == [s for s in arch._snapshots if a <= datetime.fromisoformat(s["i"]) < b]
        assert all(any(s is x for x in arch._snapshots) for s in raw)  # the archive's own dicts, nothing copied
    a, b = NOW - timedelta(days=2), NOW  # naive bounds are read as UTC, as get_snapshots always did
    naive = arch.get_snapshots_raw(a.astimezone(cl.UTC).replace(tzinfo=None), b.astimezone(cl.UTC).replace(tzinfo=None))
    assert naive == arch.get_snapshots_raw(a, b) and naive


def test_get_snapshots_raw_follows_the_archive_through_every_change(tmp_path: Path) -> None:
    """Order tracking as for the chart log (#1598): appended, replaced, pruned and out-of-order snapshots all agree."""
    arch = make_forecast_archive(tmp_path)
    a, b = NOW - timedelta(days=3), NOW + timedelta(hours=3)

    def agree() -> list[dict]:
        want = [s for s in arch._snapshots if (t := _parse_iso(s["i"])) is not None and a <= t < b]
        assert arch.get_snapshots_raw(a, b) == want
        assert arch.get_snapshots(a, b) == [decode_snapshot(s) for s in want]
        return want

    assert arch._time_sorted_snapshots() is not None
    base = len(agree())
    # a new hour appended
    arch.record(
        NOW + timedelta(hours=1),
        "weather.home",
        [(NOW.replace(minute=0) + timedelta(hours=h + 1), 55.0) for h in range(5)],
    )
    assert len(agree()) == base + 1
    # the newest replaced by a refetch of that hour with different values (the order record keeps is unchanged)
    arch.record(
        NOW + timedelta(hours=1, minutes=20),
        "weather.home",
        [(NOW.replace(minute=0) + timedelta(hours=h + 1), 56.0) for h in range(5)],
    )
    assert agree()[-1]["t"][0] == 56.0
    # the newest replaced by a snapshot issued BEFORE its predecessor: no longer sorted, the scan answers
    arch._snapshots[-1] = {**arch._snapshots[-1], "i": (NOW - timedelta(days=1, hours=3)).isoformat(timespec="minutes")}
    assert arch._time_sorted_snapshots() is None
    agree()
    # pruned/reassigned list: checked afresh
    arch._snapshots = sorted(arch._snapshots, key=lambda s: s["i"])
    assert arch._time_sorted_snapshots() is not None
    agree()
    # an unreadable issue time: the scan skips it, exactly as before
    arch._snapshots.insert(40, {**arch._snapshots[40], "i": "garbage"})
    assert arch._time_sorted_snapshots() is None
    agree()
    # a snapshot without an issue time at all: both reads give [] (and warn), as get_snapshots always did
    arch._snapshots.insert(3, {"s": "x", "h0": "2026-01-01T00:00+00:00", "t": [1.0]})
    assert arch.get_snapshots_raw(a, b) == [] and arch.get_snapshots(a, b) == []
    # an archive built without __init__ (a test double) has no record yet: it is checked in full
    bare = ForecastArchive.__new__(ForecastArchive)
    bare._snapshots = list(make_forecast_archive(tmp_path, days=2)._snapshots)
    assert bare.get_snapshots_raw(a, b) == [s for s in bare._snapshots if a <= datetime.fromisoformat(s["i"]) < b]


# --------------------------------------------------------------------------------------------------- the jobs
def fixture_log_and_archive(
    tmp_path: Path, rows: list[dict], snaps: list[dict]
) -> tuple[cl.ChartStateLog, ForecastArchive]:
    log = make_log(tmp_path, rows)
    arch = ForecastArchive(tmp_path, entry_id="z")
    for s in snaps:
        issued = datetime.fromisoformat(s["issued"])
        arch.record(issued, "weather.home", [(datetime.fromisoformat(h), v) for h, v in s["hours"]])
    return log, arch


def test_fit_job_ships_compact_snapshots_and_learns_the_same(tmp_path: Path) -> None:
    log, arch = fixture_log_and_archive(tmp_path, oj.ROWS, oj.ARCHIVE)
    assert len(arch._snapshots) > 5
    kw = oj.fit_kwargs(snapshots_fn=arch.get_snapshots, sleep_window=list(oj.WRAP), sleep_sensor="sensor.b")
    old = vs.fit_job_payload(**kw)
    new = vs.fit_job_payload(**kw, raw_snapshots_fn=arch.get_snapshots_raw, chart_window_fn=log.rows_between)
    assert "snapshots" not in new and old["snapshots"]
    assert new["snapshots_raw"] == arch.get_snapshots_raw(*vs.fit_snapshot_window(oj.NOW, oj.DAYS))
    assert [decode_snapshot(s) for s in new["snapshots_raw"]] == old["snapshots"]
    assert set(new["snapshots_raw"][0]) == {"i", "s", "h0", "t"}  # compact: no decoded hour list per snapshot
    assert new["chart_entries"] == old["chart_entries"]
    # what run_fit receives in the worker is the same either way ...
    seen: list[dict[str, Any]] = []
    real = vs.run_fit
    try:
        vs.run_fit = lambda **k: seen.append(k) or ({}, 0.0, "")  # type: ignore[assignment]
        vs.run_fit_job(oj.wire(old))
        vs.run_fit_job(oj.wire(new))
    finally:
        vs.run_fit = real
    a_kw, b_kw = seen
    assert a_kw["snapshots_fn"](None, None) == b_kw["snapshots_fn"](None, None) and a_kw["snapshots_fn"](None, None)
    assert {k: v for k, v in a_kw.items() if k not in ("snapshots_fn", "in_sleep")} == {
        k: v for k, v in b_kw.items() if k not in ("snapshots_fn", "in_sleep")
    }
    # ... and the fit the worker runs equals the in-process fit with the archive's own callable
    got = vs.run_fit_job(oj.wire(new))
    want = json.loads(json.dumps(oj.direct_fit(**kw)))
    assert got[0]["confidence"] != "none"
    assert oj.canon(got[0]) == oj.canon(want[0]) and got[2] == want[2]


def test_fit_payload_uses_the_requests_clock_not_the_wall_clock(tmp_path: Path) -> None:
    """Rows are found around ``now`` as passed (a replay of last month), never around the wall clock."""
    log, arch = fixture_log_and_archive(tmp_path, oj.ROWS, oj.ARCHIVE)
    past = oj.NOW - timedelta(days=2)
    kw = oj.fit_kwargs(now=past, snapshots_fn=arch.get_snapshots)
    old = vs.fit_job_payload(**kw)
    asked: list[tuple[float, float]] = []

    def window(lo: float, hi: float) -> list[dict] | None:
        asked.append((lo, hi))
        return log.rows_between(lo, hi)

    new = vs.fit_job_payload(**kw, raw_snapshots_fn=arch.get_snapshots_raw, chart_window_fn=window)
    assert asked == [(past.timestamp() - oj.DAYS * 86400.0, past.timestamp() + 1.0)]
    assert new["chart_entries"] == old["chart_entries"] and len(old["chart_entries"]) < len(oj.ROWS)
    assert [decode_snapshot(s) for s in new["snapshots_raw"]] == old["snapshots"]


def test_fit_payload_falls_back_to_the_scan_for_an_out_of_order_log_and_archive(tmp_path: Path) -> None:
    log, arch = fixture_log_and_archive(tmp_path, swapped(oj.ROWS, 40), oj.ARCHIVE)
    arch._snapshots[3], arch._snapshots[4] = arch._snapshots[4], arch._snapshots[3]
    assert log.rows_between(0, INF) is None and arch._time_sorted_snapshots() is None
    kw = oj.fit_kwargs(chart_entries=swapped(oj.ROWS, 40), snapshots_fn=arch.get_snapshots)
    old = vs.fit_job_payload(**kw)
    new = vs.fit_job_payload(**kw, raw_snapshots_fn=arch.get_snapshots_raw, chart_window_fn=log.rows_between)
    assert new["chart_entries"] == old["chart_entries"]
    assert [decode_snapshot(s) for s in new["snapshots_raw"]] == old["snapshots"]


def test_nightly_job_ships_compact_snapshots_and_replays_the_same(tmp_path: Path) -> None:
    log, arch = fixture_log_and_archive(tmp_path, oj.ROWS_NIGHT, oj.ARCHIVE)
    kw = {
        "chart_entries": oj.ROWS_NIGHT,
        "snapshots_fn": arch.get_snapshots,
        "tz": oj.TZ,
        "now": oj.NOW,
        "primary": oj.PRIMARY,
        "sensors": oj.SENSORS,
        **oj.NIGHT,
        "model_fitted_at": "2026-11-24T03:15:00+00:00",
    }
    old = nr.nightly_job_payload(**kw, sleep_window=list(oj.WRAP))
    new = nr.nightly_job_payload(
        **kw, sleep_window=list(oj.WRAP), raw_snapshots_fn=arch.get_snapshots_raw, chart_window_fn=log.rows_between
    )
    assert "snapshots" not in new and [decode_snapshot(s) for s in new["snapshots_raw"]] == old["snapshots"]
    assert new["chart_entries"] == old["chart_entries"]
    assert {k: v for k, v in new.items() if k != "snapshots_raw"} == {k: v for k, v in old.items() if k != "snapshots"}
    # what run_nightly receives in the worker is the same either way (the replay is deterministic in these inputs)
    seen: list[dict[str, Any]] = []
    real = nr.run_nightly
    try:
        nr.run_nightly = lambda **k: seen.append(k) or {}  # type: ignore[assignment]
        nr.run_nightly_job(oj.wire(old))
        nr.run_nightly_job(oj.wire(new))
    finally:
        nr.run_nightly = real
    a_kw, b_kw = seen
    assert a_kw["snapshots_fn"](None, None) == b_kw["snapshots_fn"](None, None) and a_kw["snapshots_fn"](None, None)
    assert {k: v for k, v in a_kw.items() if k not in ("snapshots_fn", "in_sleep")} == {
        k: v for k, v in b_kw.items() if k not in ("snapshots_fn", "in_sleep")
    }
    assert len(a_kw["chart_entries"]) == len(b_kw["chart_entries"]) > 100


def test_display_fit_job_windows_rows_by_bisection_and_learns_the_same(tmp_path: Path) -> None:
    log = make_log(tmp_path, oj.ROWS)
    kw = {
        "chart_entries": oj.ROWS,
        "tz": oj.TZ,
        "now": oj.NOW - timedelta(hours=5),
        "primary": oj.PRIMARY,
        "sensors": oj.SENSORS,
        "model_dict": {"confidence": "low", "primary": oj.PRIMARY, "sensors": oj.SENSORS},
        "train_days": 2,
        "sleep_window": list(oj.WRAP),
    }
    old = vs.display_fit_job_payload(**kw)
    new = vs.display_fit_job_payload(**kw, chart_window_fn=log.rows_between)
    assert new == old and 0 < len(old["chart_entries"]) < len(oj.ROWS)


def test_a_window_function_that_cannot_answer_gives_the_scan(tmp_path: Path) -> None:
    rows = oj.ROWS
    assert vs.window_chart_rows(rows, oj.NOW, 2, lambda _a, _b: None) == vs.rows_in_window(rows, oj.NOW, 2)
    assert vs.window_chart_rows(rows, oj.NOW, 2) == vs.rows_in_window(rows, oj.NOW, 2)
    # the exact predicate is the builder's own: a superset slice is trimmed (the upper end is now + 1 s)
    got = vs.window_chart_rows(rows, oj.NOW, 2, lambda _a, _b: list(rows))
    assert got == vs.rows_in_window(rows, oj.NOW, 2)


def test_worker_decodes_compact_snapshots_and_degrades_like_the_archive_read() -> None:
    good = {"i": "2026-11-24T10:07+00:00", "s": "weather.home", "h0": "2026-11-24T10:00+00:00", "t": [50.0, None, 52.5]}
    payload = {"now": oj.NOW.isoformat(), "tz": None, "snapshots_raw": [good]}
    _tz, _now, fn = vs.job_clock_and_archive(payload)
    assert fn is not None and fn(None, None) == [decode_snapshot(good)]
    assert fn(None, None)[0]["hours"] == [["2026-11-24T10:00+00:00", 50.0], ["2026-11-24T12:00+00:00", 52.5]]
    # one snapshot the decoder cannot read: the whole archive reads as empty (fit from the chart log alone)
    bad = {**payload, "snapshots_raw": [good, {"i": "x", "s": "s", "h0": "2026-11-24T10:00+00:00", "t": 5}]}
    assert vs.job_clock_and_archive(bad)[2](None, None) == []
    # no archive: None; the decoded form of an older payload still works
    assert vs.job_clock_and_archive({"now": oj.NOW.isoformat()})[2] is None
    assert vs.job_clock_and_archive({"now": oj.NOW.isoformat(), "snapshots": [1]})[2](None, None) == [1]
    json.dumps(payload)


# ---------------------------------------------------------------------------------- coordinator._v4_display_pending
@pytest.mark.parametrize("order", ["sorted", "out_of_order"])
def test_display_pending_reads_the_same_30_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str) -> None:
    rows = make_rows(120)
    rows = swapped(rows, 3000) if order == "out_of_order" else rows
    log = make_log(tmp_path, rows)
    seen: dict[str, Any] = {}

    def fake_pending(model_dict: Any, entries: Any, **kw: Any) -> list[str]:
        seen["entries"], seen["kw"] = entries, kw
        return ["sensor.x"]

    monkeypatch.setattr(vs, "display_pending", fake_pending)
    monkeypatch.setattr(coordinator_mod.dt_util, "now", lambda: NOW, raising=False)
    stub = SimpleNamespace(_chart_log=log, _v4_node_inputs=lambda: {"primary": SENSORS[0], "sensors": SENSORS[:2]})
    assert coordinator_mod.ClimateAdvisorCoordinator._v4_display_pending(stub, {"confidence": "low"}) == ["sensor.x"]
    days = coordinator_mod.V4_FIT_TRAIN_DAYS
    # what display_pending does with the rows (it windows them itself) is the same either way
    assert vs.rows_in_window(seen["entries"], NOW, days) == vs.rows_in_window(log.raw_entries(), NOW, days)
    if order == "sorted":
        assert len(seen["entries"]) < len(rows) / 2, "only the last 30 days were handed over"
    else:
        assert len(seen["entries"]) == len(rows)
