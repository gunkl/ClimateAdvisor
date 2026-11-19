"""Issue #1598: ``ChartStateLog.get_entries`` must not parse a whole year of rows to return the last week.

Occupant view: every 30-minute cycle (and every chart, diagnostics download and AI Investigator run) Home Assistant
paused while Climate Advisor parsed the timestamp of every row of a 365-day chart log to pick out the last 7 days.
The rows are kept in time order, so the window is found by bisection; these tests count timestamp parses (not wall
clock) and pin the result to the old full scan on randomized logs, including out-of-order and unparseable rows,
where the log falls back to the full scan.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.climate_advisor import chart_log as chart_log_mod
from custom_components.climate_advisor.chart_log import ChartStateLog

RANGES = ("6h", "12h", "24h", "3d", "7d", "30d", "1y")
PDT = timezone(timedelta(hours=-7))
PST = timezone(timedelta(hours=-8))


def _now() -> datetime:
    # load() prunes against the real clock, so the synthetic log is anchored to it (minute-aligned).
    return datetime.now(UTC).replace(second=0, microsecond=0)


def _row(ts: datetime | str, i: int) -> dict:
    return {
        "ts": ts if isinstance(ts, str) else ts.isoformat(),
        "hvac": "idle" if i % 3 else "cooling",
        "fan": False,
        "indoor": 70.0 + (i % 7) * 0.5,
        "outdoor": 60.0 + (i % 11),
    }


def _loaded(tmp_path: Path, rows: list[dict]) -> ChartStateLog:
    log = ChartStateLog(tmp_path, max_days=365)
    log._path.write_text(json.dumps({"entries": rows}), encoding="utf-8")
    log.load()
    return log


def _year_log(tmp_path: Path, now: datetime, step_min: int = 30) -> ChartStateLog:
    n = 365 * 24 * 60 // step_min - 1
    rows = [_row(now - timedelta(minutes=step_min * k), k) for k in range(n, 0, -1)]
    return _loaded(tmp_path, rows)


def _full_scan(log: ChartStateLog, range_str: str, before: datetime | None, anchor_now: datetime) -> list[dict]:
    """The pre-#1598 ``get_entries`` (every row parsed), kept here as the equality reference."""
    range_days = log._range_str_to_days(range_str)
    anchor = before if before is not None else anchor_now
    cutoff = anchor - timedelta(days=range_days)
    filtered = [e for e in log._entries if log._entry_after(e, cutoff)]
    if before is not None:
        filtered = [e for e in filtered if not log._entry_after(e, before)]
    if range_days <= 3:
        return filtered
    if range_days <= 30:
        return log._bucket_hourly(filtered)
    return log._bucket_daily(filtered)


class _ParseCounter:
    def __init__(self) -> None:
        self.calls = 0
        self._orig = chart_log_mod._parse_ts

    def __call__(self, ts_str):
        self.calls += 1
        return self._orig(ts_str)


class TestParseCount:
    """Deterministic work counts: how many timestamps one query parses on a 365-day, 30-minute log."""

    def test_seven_day_query_parses_about_a_week_of_rows_not_a_year(self, tmp_path: Path, frozen_now) -> None:
        now = _now()
        frozen_now(now)
        log = _year_log(tmp_path, now)
        total = log.entry_count
        week_rows = 7 * 48
        assert total > 17_000
        counter = _ParseCounter()
        with patch.object(chart_log_mod, "_parse_ts", counter):
            out = log.get_entries("7d")
        assert 160 <= len(out) <= 7 * 24 + 1  # hourly buckets of the last week
        # the week's rows are parsed once more for the hourly buckets; the cutoff itself takes ~log2(n) parses
        assert counter.calls <= 2 * week_rows + 64, f"{counter.calls} parses for a {total}-row log"

    def test_historical_window_parses_only_its_own_rows(self, tmp_path: Path, frozen_now) -> None:
        now = _now()
        frozen_now(now)
        log = _year_log(tmp_path, now)
        counter = _ParseCounter()
        with patch.object(chart_log_mod, "_parse_ts", counter):
            out = log.get_entries("24h", before=now - timedelta(days=200))
        assert len(out) == 48
        assert counter.calls <= 64, f"{counter.calls} parses for one day of history"

    def test_rows_appended_after_load_are_checked_once_not_rescanned(self, tmp_path: Path, frozen_now) -> None:
        now = _now()
        frozen_now(now)
        log = _year_log(tmp_path, now)
        log.get_entries("24h")
        for k in range(3):
            log._entries.append(_row(now + timedelta(seconds=k + 1), k))  # an append() without its prune pass
        counter = _ParseCounter()
        with patch.object(chart_log_mod, "_parse_ts", counter):
            out = log.get_entries("3d", before=now + timedelta(minutes=1))
        assert out[-3:] == log._entries[-3:]
        assert counter.calls <= 3 + 64


def _random_rows(rng: random.Random, now: datetime, n: int) -> list[dict]:
    rows = []
    t = now - timedelta(days=rng.uniform(5, 400))
    for i in range(n):
        t += timedelta(minutes=rng.choice((1, 5, 30, 30, 30, 90, 600)))
        if t > now:
            break
        tz = rng.choice((UTC, UTC, PDT, PST))  # DST offsets: string order differs from instant order
        rows.append(_row(t.astimezone(tz), i))
    return rows


def _disorder(rng: random.Random, rows: list[dict], now: datetime) -> list[dict]:
    rows = list(rows)
    kind = rng.choice(("clock_step_back", "swap", "garbage_ts", "missing_ts", "future_row", "duplicate"))
    k = rng.randrange(len(rows))
    if kind == "clock_step_back":  # NTP correction: later rows written with an earlier clock
        back = timedelta(hours=rng.uniform(1, 48))
        rows[k:] = [{**r, "ts": (datetime.fromisoformat(r["ts"]) - back).isoformat()} for r in rows[k:]]
    elif kind == "swap":
        j = rng.randrange(len(rows))
        rows[k], rows[j] = rows[j], rows[k]
    elif kind == "garbage_ts":
        rows.insert(k, {**rows[k], "ts": "not-a-time"})
    elif kind == "missing_ts":
        rows.insert(k, {key: v for key, v in rows[k].items() if key != "ts"})
    elif kind == "future_row":
        rows.insert(k, {**rows[k], "ts": (now + timedelta(hours=rng.uniform(1, 30))).isoformat()})
    else:
        rows.insert(k, dict(rows[k]))
    return rows


def _queries(rng: random.Random, now: datetime) -> list[tuple[str, datetime | None]]:
    qs: list[tuple[str, datetime | None]] = [(r, None) for r in RANGES]
    for _ in range(6):
        qs.append((rng.choice(RANGES), now - timedelta(minutes=rng.randrange(0, 400 * 24 * 60))))
    return qs


class TestEqualityWithFullScan:
    """The bisected query returns exactly what the full scan returned, sorted or not."""

    @pytest.mark.parametrize("seed", range(12))
    def test_sorted_logs(self, tmp_path: Path, frozen_now, seed: int) -> None:
        rng = random.Random(seed)
        now = _now()
        frozen_now(now)
        log = _loaded(tmp_path, _random_rows(rng, now, rng.randrange(1, 3000)))
        for range_str, before in _queries(rng, now):
            assert log.get_entries(range_str, before=before) == _full_scan(log, range_str, before, now)

    @pytest.mark.parametrize("seed", range(24))
    def test_out_of_order_and_unparseable_rows(self, tmp_path: Path, frozen_now, seed: int) -> None:
        rng = random.Random(1000 + seed)
        now = _now()
        frozen_now(now)
        rows = _random_rows(rng, now, rng.randrange(2, 2000))
        rows = _disorder(rng, rows, now)
        # direct assignment (a list the log has never inspected) and a loaded file both have to work
        log = ChartStateLog(tmp_path, max_days=365)
        log._entries = rows
        (tmp_path / "file").mkdir()
        loaded = _loaded(tmp_path / "file", rows)
        for range_str, before in _queries(rng, now):
            assert log.get_entries(range_str, before=before) == _full_scan(log, range_str, before, now)
            assert loaded.get_entries(range_str, before=before) == _full_scan(loaded, range_str, before, now)

    @pytest.mark.parametrize("seed", range(6))
    def test_appends_and_prune_keep_results_equal(self, tmp_path: Path, frozen_now, seed: int) -> None:
        rng = random.Random(2000 + seed)
        now = _now()
        log = _loaded(tmp_path, _random_rows(rng, now - timedelta(hours=2), 500))
        t = now - timedelta(hours=2)
        for _i in range(40):
            t += timedelta(minutes=3)
            if rng.random() < 0.1:
                t -= timedelta(minutes=rng.randrange(5, 120))  # an occasional backwards clock step
            frozen_now(t)
            log._last_prune = None if rng.random() < 0.2 else log._last_prune  # force some prune passes
            log.append(hvac="idle", fan=False, indoor=70.0, outdoor=60.0, ts=t.isoformat())
            for range_str, before in _queries(rng, t)[:4]:
                assert log.get_entries(range_str, before=before) == _full_scan(log, range_str, before, t)

    def test_empty_log(self, tmp_path: Path) -> None:
        log = ChartStateLog(tmp_path)
        for r in RANGES:
            assert log.get_entries(r) == []


class TestCoordinatorCycleDoesNotRescanTheWeek:
    """Issue #1598: the 30-minute cycle recomputed ``_thermal_factors`` from ``get_entries("7d")`` on the event loop.

    Nothing has read ``_thermal_factors`` since #132 (8ce05cd0) removed its last consumer, so the cycle paid for a
    full chart-log scan whose result was thrown away. The call is gone; this pins that no coordinator method brings
    it back (the module-level helper stays for its unit tests).
    """

    def test_no_coordinator_method_computes_thermal_factors(self) -> None:
        import ast
        import inspect

        from custom_components.climate_advisor import coordinator as coordinator_mod

        tree = ast.parse(inspect.getsource(coordinator_mod))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        offenders = [
            f"{cls.name}:{node.lineno}"
            for cls in classes
            for node in ast.walk(cls)
            if (isinstance(node, ast.Name) and node.id == "_compute_thermal_factors")
            or (isinstance(node, ast.Attribute) and node.attr == "_thermal_factors")
        ]
        assert offenders == []
