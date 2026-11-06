"""Data behind ``GET /api/climate_advisor/prediction_accuracy`` (Issue #1230 R1).

Occupant view: a read-only comparison page of what prediction engine v4 said N hours ago against what the house then
did, next to today's (v3) prediction and a "nothing changes" baseline. Reading it changes nothing in the house.

``build_prediction_accuracy`` is pure and blocking (it reads the backtest file and walks the chart log): the view runs
it in the executor. All internal values are degrees F; the payload is converted to the user's unit at this boundary
(absolute temperatures with ``display_temp``, errors with ``display_delta``), like ``get_chart_data``.

Sources, merged with live data preferred:

* live: the v4 archive (``v4_archive.V4Archive``, first write wins) scored against the chart log;
* backtest: ``climate_advisor_v4_backtest_<entry_id>.json`` written by ``tools/predict_backtest.py --publish``
  (history from before the shadow ran). Shape used (lane B): ``points`` = sensor -> lead ->
  [{t, actual, flat, v4, v3}]; ``gate`` rows (sensor, season, lead, v4, v3, flat, bias, n, pass, scored) and
   ``gate_summary``. Every key is optional. A backtest point is used only for a 30-minute slot the live archive
  does not cover.

v3 exists only for look-ahead 4 h of the reading Climate Advisor uses (``_ca``): its first-write-wins archive is
written 4 h ahead and stored as chart-log ``pred_indoor``.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from .const import V4_BACKTEST_FILE
from .storage_paths import resolve_entry_scoped_path
from .temperature import UNIT_SYMBOL, display_delta, display_temp
from .v4_archive import V4Archive, lead_hours, slot_key

_LOGGER = logging.getLogger(__name__)

RANGES: dict[str, float] = {"6h": 0.25, "12h": 0.5, "24h": 1.0, "3d": 3.0, "7d": 7.0, "30d": 30.0, "1y": 365.0}
DEFAULT_RANGE = "24h"
DEFAULT_LEAD = "4h"
CA_SENSOR = "_ca"
_RAW_MAX_DAYS = 3.0  # same thresholds as ChartStateLog.get_entries
_HOURLY_MAX_DAYS = 30.0
_MATCH_TOLERANCE_S = 20 * 60  # a chart row further than this from a slot is not "the actual at that time"


def load_backtest(config_dir: Path, entry_id: str) -> dict[str, Any] | None:
    """The zone's published backtest file, or None when absent/unreadable (blocking: run in the executor)."""
    path = resolve_entry_scoped_path(config_dir, V4_BACKTEST_FILE, entry_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        _LOGGER.warning("prediction_accuracy: backtest file unreadable, ignoring it: %s", err)
        return None
    return data if isinstance(data, dict) else None


class _Series:
    """Sorted ``(epoch, value)`` with nearest-value lookup inside a tolerance."""

    def __init__(self, pairs: list[tuple[float, float]]) -> None:
        pairs.sort()
        self._t = [p[0] for p in pairs]
        self._v = [p[1] for p in pairs]

    def nearest(self, t: float, tolerance: float = _MATCH_TOLERANCE_S) -> float | None:
        if not self._t:
            return None
        i = bisect.bisect_left(self._t, t)
        best: tuple[float, float] | None = None
        for j in (i - 1, i):
            if 0 <= j < len(self._t):
                gap = abs(self._t[j] - t)
                if gap <= tolerance and (best is None or gap < best[0]):
                    best = (gap, self._v[j])
        return best[1] if best else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else dt.replace(tzinfo=None).timestamp()


def _row_series(rows: Sequence[Mapping[str, Any]], getter: Any, lo: float, hi: float) -> _Series:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        t = _epoch(row.get("ts"))
        if t is None or t < lo or t > hi:
            continue
        v = _num(getter(row))
        if v is not None:
            pairs.append((t, v))
    return _Series(pairs)


def _heat_runs(rows: Sequence[Mapping[str, Any]], lo: float, hi: float, tz: tzinfo) -> list[list[str]]:
    """``[[start, end], ...]`` of consecutive chart rows whose hvac action is ``heating``."""
    runs: list[list[str]] = []
    start: float | None = None
    last_heating: float | None = None
    for row in sorted((r for r in rows if _epoch(r.get("ts")) is not None), key=lambda r: _epoch(r["ts"]) or 0.0):
        t = _epoch(row["ts"])
        if t is None or t < lo or t >= hi:
            continue
        if str(row.get("hvac", "")).lower() == "heating":
            if start is None:
                start = t
            last_heating = t
        elif start is not None:
            runs.append([_iso(start, tz), _iso(t, tz)])
            start = last_heating = None
    if start is not None and last_heating is not None:
        runs.append([_iso(start, tz), _iso(last_heating, tz)])
    return runs


def _iso(epoch: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(epoch, tz).isoformat(timespec="seconds")


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _bucket(points: list[dict[str, Any]], mode: str, tz: tzinfo) -> list[dict[str, Any]]:
    """Average ``actual``/``v4``/``v3`` per local hour (``hourly``) or local day (``daily``); ``t`` = bucket start."""
    groups: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for p in points:
        dt = datetime.fromtimestamp(p["_t"], tz)
        key = (
            dt.replace(minute=0, second=0, microsecond=0)
            if mode == "hourly"
            else dt.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        groups[key].append(p)
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        member = groups[key]
        row: dict[str, Any] = {"t": key.isoformat(timespec="seconds"), "_t": key.timestamp()}
        for field in ("actual", "v4", "v3"):
            avg = _mean([m[field] for m in member if m.get(field) is not None])
            if avg is not None:
                row[field] = avg
        out.append(row)
    return out


def _stats(points: list[dict[str, Any]], flat: dict[float, float]) -> dict[str, Any]:
    v4_err = [p["v4"] - p["actual"] for p in points if p.get("v4") is not None and p.get("actual") is not None]
    v3_err = [p["v3"] - p["actual"] for p in points if p.get("v3") is not None and p.get("actual") is not None]
    flat_err = [flat[p["_t"]] - p["actual"] for p in points if p["_t"] in flat and p.get("actual") is not None]

    def mae(errs: list[float]) -> float | None:
        return _mean([abs(e) for e in errs])

    return {
        "n": len(v4_err),
        "v4_mae": mae(v4_err),
        "v3_mae": mae(v3_err),
        "flat_mae": mae(flat_err),
        "v4_bias": _mean(v4_err),
        "v3_bias": _mean(v3_err),
    }


def build_prediction_accuracy(
    *,
    zone: str,
    sensor: str,
    lead: str,
    range_str: str,
    before: datetime | None,
    now: datetime,
    unit: str,
    chart_rows: Sequence[Mapping[str, Any]],
    archive: V4Archive,
    backtest: Mapping[str, Any] | None,
    model: Mapping[str, Any] | None,
    sensors_meta: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The endpoint payload. ``lead``/``range_str`` must already be valid (the view resets bad values to defaults)."""
    tz = now.tzinfo or datetime.now().astimezone().tzinfo
    assert tz is not None
    end = (before or now).timestamp()
    range_days = RANGES[range_str]
    start = end - range_days * 86400
    lead_s = lead_hours(lead) * 3600

    # Actual readings (and v3's pred_indoor) from the chart log; one extra lead before the window for the flat baseline.
    if sensor == CA_SENSOR:

        def actual_of(row: Mapping[str, Any]) -> Any:
            return row.get("indoor")

    else:

        def actual_of(row: Mapping[str, Any]) -> Any:
            readings = row.get("indoor_sensors")
            return readings.get(sensor) if isinstance(readings, dict) else None

    actual = _row_series(chart_rows, actual_of, start - lead_s - _MATCH_TOLERANCE_S, end + _MATCH_TOLERANCE_S)
    has_v3 = sensor == CA_SENSOR and lead == DEFAULT_LEAD
    v3_series = _row_series(chart_rows, lambda r: r.get("pred_indoor"), start, end) if has_v3 else None

    # v4: live archive first, then backtest points for slots the live archive does not cover.
    live = archive.series(sensor, lead, start, end)
    points: dict[float, dict[str, Any]] = {}
    for slot, v4 in live.items():
        points[float(slot)] = {"v4": v4}
    live_slots = set(live)
    bt_rows = []
    if isinstance(backtest, Mapping):
        series = backtest.get("points")
        by_sensor = series.get(sensor) if isinstance(series, Mapping) else None
        rows = by_sensor.get(lead) if isinstance(by_sensor, Mapping) else None
        bt_rows = rows if isinstance(rows, list) else []
    for item in bt_rows:
        if not isinstance(item, Mapping):
            continue
        t = _epoch(item.get("t"))
        v4 = _num(item.get("v4"))
        if t is None or v4 is None or not (start <= t < end):
            continue
        if slot_key(datetime.fromtimestamp(t, tz)) in live_slots:
            continue
        points[t] = {
            "v4": v4,
            "_bt_actual": _num(item.get("actual")),
            "_bt_v3": _num(item.get("v3")),
            "_bt_flat": _num(item.get("flat")),
        }

    raw: list[dict[str, Any]] = []
    flat: dict[float, float] = {}
    for t in sorted(points):
        p = points[t]
        a = actual.nearest(t)
        if a is None:
            a = p.get("_bt_actual")
        row: dict[str, Any] = {"t": _iso(t, tz), "_t": t, "v4": p["v4"]}
        if a is not None:
            row["actual"] = a
        if has_v3:
            v3 = v3_series.nearest(t) if v3_series is not None else None
            if v3 is None:
                v3 = p.get("_bt_v3")
            if v3 is not None:
                row["v3"] = v3
        f = actual.nearest(t - lead_s)
        if f is None:
            f = p.get("_bt_flat")  # the backtest tool's no-change value where the chart log no longer has the row
        if f is not None:
            flat[t] = f
        raw.append(row)

    stats = _stats(raw, flat)

    if range_days <= _RAW_MAX_DAYS:
        downsample = "raw"
        shown = raw
    elif range_days <= _HOURLY_MAX_DAYS:
        downsample = "hourly"
        shown = _bucket(raw, "hourly", tz)
    else:
        downsample = "daily"
        shown = _bucket(raw, "daily", tz)

    def out_point(p: dict[str, Any]) -> dict[str, Any]:
        o: dict[str, Any] = {"t": p["t"]}
        for field in ("actual", "v4", "v3"):
            if p.get(field) is not None:
                o[field] = display_temp(p[field], unit, 1)
        return o

    out_stats = {
        "n": stats["n"],
        "v4_mae": display_delta(stats["v4_mae"], unit, 2),
        "v3_mae": display_delta(stats["v3_mae"], unit, 2),
        "flat_mae": display_delta(stats["flat_mae"], unit, 2),
        "v4_bias": display_delta(stats["v4_bias"], unit, 2),
        "v3_bias": display_delta(stats["v3_bias"], unit, 2),
    }

    known = {str(s.get("id")) for s in sensors_meta}
    sensors_out = [dict(s) for s in sensors_meta]
    for extra in [*archive.sensor_ids(), *(backtest.get("points", {}) if isinstance(backtest, Mapping) else {})]:
        if isinstance(extra, str) and extra not in known:
            known.add(extra)
            sensors_out.append({"id": extra, "name": extra, "role": ""})

    gate_out: list[dict[str, Any]] = []
    raw_gate = backtest.get("gate") if isinstance(backtest, Mapping) else None
    for g in raw_gate if isinstance(raw_gate, list) else []:
        if not isinstance(g, Mapping):
            continue
        item = dict(g)
        for field in ("v4", "v3", "flat", "bias"):
            if _num(item.get(field)) is not None:
                item[field] = display_delta(float(item[field]), unit, 2)
        gate_out.append(item)

    model_out = {"confidence": "none", "fitted_at": None, "span_days": None}
    if isinstance(model, Mapping) and model.get("has_model"):
        model_out = {
            "confidence": model.get("confidence", "none"),
            "fitted_at": model.get("fitted_at"),
            "span_days": model.get("span_days"),
        }

    heat_runs = [] if downsample == "daily" else _heat_runs(chart_rows, start, end, tz)

    return {
        "zone": zone,
        "unit": UNIT_SYMBOL.get(unit, "°F"),
        "sensors": sensors_out,
        "sensor": sensor,
        "lead": lead,
        "range": range_str,
        "start": _iso(start, tz),
        "end": _iso(end, tz),
        "downsample": downsample,
        "points": [out_point(p) for p in shown],
        "heat_runs": heat_runs,
        "stats": out_stats,
        "model": model_out,
        "gate": gate_out,
        "gate_summary": dict(backtest["gate_summary"])
        if isinstance(backtest, Mapping) and isinstance(backtest.get("gate_summary"), Mapping)
        else None,
    }
