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

Each engine is scored on its own points (Issue #1400): v3 from its logged ``pred_indoor``, the "no change" baseline
from the chart log's own readings, v4 from its archive and the backtest. A zone whose v4 archive is still empty
therefore still shows v3's and "no change"'s scores, and the actual line.
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
        for field in ("actual", "v4", "v3", "flat"):
            avg = _mean([m[field] for m in member if m.get(field) is not None])
            if avg is not None:
                row[field] = avg
        out.append(row)
    return out


def _stats(points: list[dict[str, Any]], flat: dict[float, float]) -> dict[str, Any]:
    """Each engine over its OWN points (Issue #1400): ``n`` / ``n_v3`` / ``n_flat`` say how many each was scored on."""
    v4_err = [p["v4"] - p["actual"] for p in points if p.get("v4") is not None and p.get("actual") is not None]
    v3_err = [p["v3"] - p["actual"] for p in points if p.get("v3") is not None and p.get("actual") is not None]
    flat_err = [flat[p["_t"]] - p["actual"] for p in points if p["_t"] in flat and p.get("actual") is not None]

    def mae(errs: list[float]) -> float | None:
        return _mean([abs(e) for e in errs])

    return {
        "n": len(v4_err),
        "n_v3": len(v3_err),
        "n_flat": len(flat_err),
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
    primary_engine: str = "v4",
    shadow_engine: str = "none",
    sleep_sensor: str | None = None,
    sleep_time: Any = None,
    wake_time: Any = None,
) -> dict[str, Any]:
    """The endpoint payload. ``lead``/``range_str`` must already be valid (the view resets bad values to defaults).

    ``sleep_sensor``/``sleep_time``/``wake_time`` are the zone's ``sleep_indoor_temp_entity``/``sleep_time``/
    ``wake_time`` settings: with them ``ca_swaps`` (Issue #1431) lists when the reading Climate Advisor uses swapped
    sensor in the window (``ca_swap_epochs``, the rule behind the Status chart's ``ca_swaps``), so the page breaks its
    ``_ca`` lines there. Without a sleep sensor only the chart log's recorded ``band_sensor`` changes count.
    """
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

    # One point per 30-minute slot. Every chart row in the window gives one (its actual, v3's logged value, the "no
    # change" value), so v3 and the baseline are scored even while v4 has nothing (Issue #1400); v4 joins its slots.
    points: dict[float, dict[str, Any]] = {}
    for row in chart_rows:
        t = _epoch(row.get("ts"))
        if t is None or not (start <= t < end) or row.get("event") is not None:
            continue
        if _num(actual_of(row)) is None and not (has_v3 and _num(row.get("pred_indoor")) is not None):
            continue
        points.setdefault(float(slot_key(datetime.fromtimestamp(t, tz))), {})
    # v4: live archive first, then backtest points for slots the live archive does not cover.
    live = archive.series(sensor, lead, start, end)
    for slot, v4 in live.items():
        points.setdefault(float(slot), {})["v4"] = v4
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
        key = float(slot_key(datetime.fromtimestamp(t, tz)))
        if key in live_slots:
            continue
        points.pop(key, None)  # the backtest's own (hourly) time replaces the chart-row slot it falls on
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
        row: dict[str, Any] = {"t": _iso(t, tz), "_t": t}
        if p.get("v4") is not None:
            row["v4"] = p["v4"]
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
            row["flat"] = f
        if len(row) > 2:
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
        for field in ("actual", "v4", "v3", "flat"):
            if p.get(field) is not None:
                o[field] = display_temp(p[field], unit, 1)
        return o

    out_stats = {
        "n": stats["n"],
        "n_v3": stats["n_v3"],
        "n_flat": stats["n_flat"],
        "v4_mae": display_delta(stats["v4_mae"], unit, 2),
        "v3_mae": display_delta(stats["v3_mae"], unit, 2),
        "flat_mae": display_delta(stats["flat_mae"], unit, 2),
        "v4_bias": display_delta(stats["v4_bias"], unit, 2),
        "v3_bias": display_delta(stats["v3_bias"], unit, 2),
    }

    known: set[str] = set()
    sensors_out: list[dict[str, Any]] = []
    for s in sensors_meta:  # one entry per id: a _ca-only model also lists "_ca" among its sensors (Issue #1400)
        sid = str(s.get("id"))
        if sid not in known:
            known.add(sid)
            sensors_out.append(dict(s))
    bt_points = backtest.get("points") if isinstance(backtest, Mapping) else None
    for extra in [*archive.sensor_ids(), *(bt_points if isinstance(bt_points, Mapping) else {})]:
        if isinstance(extra, str) and extra not in known:
            known.add(extra)
            sensors_out.append({"id": extra, "name": extra, "role": ""})
    # Issue #1431: whether each sensor has anything to draw at all (any time, any look-ahead), so the page can label a
    # sensor v4 has not learned ("no v4 data yet") and disable one with nothing recorded instead of an empty chart.
    v4_ids = _v4_sensor_ids(archive, bt_points)
    read_ids = _read_sensor_ids(chart_rows)
    for s in sensors_out:
        sid = str(s.get("id"))
        s["has_v4"] = sid in v4_ids
        s["has_readings"] = sid in read_ids

    # Issue #1431: when the reading CA uses swapped sensor (bedtime / wake-up) in this window, same rule as the Status
    # chart's ``ca_swaps``; none at daily resolution (a daily mean is no one sensor's reading).
    swaps: list[float] = []
    if downsample != "daily":
        try:
            swaps = ca_swap_epochs(
                chart_rows,
                start,
                end,
                tz=tz,
                sleep_sensor=sleep_sensor,
                sleep_time=sleep_time,
                wake_time=wake_time,
            )
        except Exception:  # noqa: BLE001 - display only: an unbroken line beats no page
            _LOGGER.debug("prediction accuracy: sensor swap times failed", exc_info=True)
            swaps = []

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
        "primary_engine": primary_engine,
        "shadow_engine": shadow_engine,
        "ca_swaps": [_iso(t, tz) for t in swaps],
        "gate_summary": dict(backtest["gate_summary"])
        if isinstance(backtest, Mapping) and isinstance(backtest.get("gate_summary"), Mapping)
        else None,
    }


def _v4_sensor_ids(archive: V4Archive, bt_points: Any) -> set[str]:
    """Sensor ids with at least one v4 prediction: in the live archive or in the published backtest (any lead)."""
    out: set[str] = set()
    try:
        out.update(sid for sid, n in archive.slot_count().items() if n)
    except Exception:  # noqa: BLE001 - a label only
        _LOGGER.debug("prediction accuracy: archive slot count failed", exc_info=True)
    if isinstance(bt_points, Mapping):
        for sid, by_lead in bt_points.items():
            if not isinstance(sid, str) or not isinstance(by_lead, Mapping):
                continue
            for rows in by_lead.values():
                if isinstance(rows, list) and any(
                    isinstance(r, Mapping) and _num(r.get("v4")) is not None for r in rows
                ):
                    out.add(sid)
                    break
    return out


def _read_sensor_ids(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    """Sensor ids the chart log has a reading for (``_ca`` = the ``indoor`` column), any time."""
    out: set[str] = set()
    for r in rows:
        if CA_SENSOR not in out and _num(r.get("indoor")) is not None:
            out.add(CA_SENSOR)
        readings = r.get("indoor_sensors")
        if isinstance(readings, dict):
            out.update(k for k, v in readings.items() if isinstance(k, str) and k not in out and _num(v) is not None)
    return out


# ---------------------------------------------------------------------------
# Status chart (Issue #1230 R2)
# ---------------------------------------------------------------------------

CHART_LEADS = ("1h", "4h", "12h", "24h")


def backtest_scores(backtest: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The published backtest's row counts as INFORMATION for the chart payload, or None without a backtest.

    Nothing is gated on it: which model the Status chart shows is the occupant's setting, not a pass mark. The counts
    are descriptive (rows where v4 beats both v3 and "no change", of the rows scored).
    """
    summary = backtest.get("gate_summary") if isinstance(backtest, Mapping) else None
    if not isinstance(summary, Mapping):
        return None
    names = {"passed": "v4_best_rows", "scored": "scored_rows", "total": "rows"}
    return {out: summary[k] for k, out in names.items() if k in summary}


def _downsample_pairs(pairs: list[tuple[float, float]], range_days: float, tz: tzinfo) -> list[tuple[float, float]]:
    """Same buckets as the chart log: raw to 3 days, local-hour means to 30 days, local-day means beyond."""
    if range_days <= _RAW_MAX_DAYS or not pairs:
        return sorted(pairs)
    hourly = range_days <= _HOURLY_MAX_DAYS
    groups: dict[datetime, list[float]] = defaultdict(list)
    for t, v in pairs:
        dt = datetime.fromtimestamp(t, tz)
        key = dt.replace(minute=0, second=0, microsecond=0)
        if not hourly:
            key = key.replace(hour=0)
        groups[key].append(v)
    return [(k.timestamp(), sum(vs) / len(vs)) for k, vs in sorted(groups.items())]


def _out_pairs(pairs: Sequence[tuple[float, float]], tz: tzinfo, unit: str) -> list[dict[str, Any]]:
    return [{"ts": _iso(t, tz), "temp": display_temp(v, unit, 1)} for t, v in pairs]


def _hhmm(value: Any) -> tuple[int, int] | None:
    try:
        h, m = (int(x) for x in str(value).split(":")[:2])
    except TypeError, ValueError:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def _sleep_boundaries(lo: float, hi: float, tz: tzinfo, sleep_time: Any, wake_time: Any) -> list[float]:
    """Epochs of every bedtime and wake time in ``[lo, hi]`` (local clock; an empty window has none)."""
    st, wt = _hhmm(sleep_time), _hhmm(wake_time)
    if st is None or wt is None or st == wt:
        return []
    out: list[float] = []
    day = datetime.fromtimestamp(lo, tz).date()
    last = datetime.fromtimestamp(hi, tz).date()
    while day <= last:
        for h, m in (st, wt):
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()
            if lo <= t <= hi:
                out.append(t)
        day = day.fromordinal(day.toordinal() + 1)
    return sorted(out)


def ca_swap_epochs(
    rows: Sequence[Mapping[str, Any]],
    lo: float,
    hi: float,
    *,
    tz: tzinfo,
    sleep_sensor: str | None,
    sleep_time: Any = None,
    wake_time: Any = None,
) -> list[float]:
    """When the reading Climate Advisor uses (``_ca``) changed sensor in ``[lo, hi]`` (epochs, ascending; Issue #1400).

    Occupant view: the hallway and the bedroom read differently, so a ``_ca`` line drawn straight through bedtime or
    wake-up would show a jump that never happened in either room. The chart breaks the line at these times instead.

    * Recorded rows: wherever the chart log's ``band_sensor`` (the sensor CA read on that row) changes from one row to
      the next - the configured bedtime/wake time between the two rows when there is one (the exact moment), else
      the first row on the new sensor. This also catches an unplanned fall-back (a stale bedroom sensor).
    * Outside the span those rows cover (the future, and rows written before ``band_sensor`` existed): every bedtime
      and wake time while a sleep sensor is configured (``sleep_indoor_temp_entity``); none without one.
    """
    swaps: list[float] = []
    prev: tuple[float, str] | None = None
    first: float | None = None
    last: float | None = None
    timed = sorted(
        ((t, r.get("band_sensor")) for r in rows if (t := _epoch(r.get("ts"))) is not None),
        key=lambda x: x[0],
    )
    boundaries = _sleep_boundaries(lo - 86400, hi + 86400, tz, sleep_time, wake_time) if sleep_sensor else []
    for t, sid in timed:
        if not isinstance(sid, str) or not sid:
            continue
        first = t if first is None else first
        last = t
        if prev is not None and sid != prev[1]:
            between = [b for b in boundaries if prev[0] < b <= t]
            swaps.append(between[-1] if between else t)
        prev = (t, sid)
    for b in boundaries:
        if first is not None and last is not None and first <= b <= last:
            continue  # the recorded rows know better for this span
        swaps.append(b)
    return sorted({s for s in swaps if lo <= s <= hi})


def build_chart_v4(
    *,
    now: datetime,
    start: float,
    end: float,
    range_str: str,
    unit: str,
    historical: bool,
    archive: V4Archive | None,
    backtest: Mapping[str, Any] | None,
    model: Mapping[str, Any] | None,
    last_future: Mapping[str, Any] | None,
    sensor_ids: Sequence[str],
    corrected_past: Sequence[tuple[datetime, float]] = (),
    ca_swaps: Sequence[float] = (),
) -> dict[str, Any]:
    """The ``v4`` block of ``get_chart_data()``: availability, per-sensor past (archived) and future lines.

    Pure and blocking-safe (called from the chart-data executor job). Internal degrees F in, ``unit`` out. Past lines
    are what v4 said ``lead`` ahead for each 30-minute slot (live archive first, the published backtest for older),
    for every look-ahead; the page picks one. There is no accuracy gate: the lines are always supplied when v4 has
    anything to say (the coordinator does not call this while v4 is neither primary nor shadow), and the page draws
    them as the main prediction when v4 is the primary engine, as the comparison line when it is the shadow.
    ``_ca`` (the reading Climate Advisor uses) is drawn too (Issue #1400) - for a ``_ca``-only model it is v4's only
    line. It follows the sensor Climate Advisor reads, so it jumps when that swaps; the owner's no-swap rule is kept by
    ``breaks["_ca"]``: the swap times (``ca_swaps``, epochs, see ``ca_swap_epochs``) at which the page breaks every
    ``_ca`` line (past, future and baseline), so no single segment crosses a swap. Per-sensor lines draw as before.
    A model without a ``_ca`` future (a node model with no sleep node) gets it from its primary node's line.
    ``past_span`` (``{first, last}`` or None) spans every past prediction of those lines, in or out of the window, so
    the page can tell "none in this view" from "none yet" (0.9.6).
    """
    tz = now.tzinfo or datetime.now().astimezone().tzinfo
    assert tz is not None
    model_out: dict[str, Any] = {"confidence": "none", "fitted_at": None}
    if isinstance(model, Mapping) and model.get("has_model"):
        model_out = {"confidence": model.get("confidence", "none"), "fitted_at": model.get("fitted_at")}
    out: dict[str, Any] = {
        "ready": False,
        "reason": "no_model",
        "scores": backtest_scores(backtest),
        "model": model_out,
        "leads": list(CHART_LEADS),
        "sensors": [],
        "future": {},
        "future_at": None,
        "baseline": {},
        "past": {},
        "past_span": None,  # {"first", "last", "sensors"}: every past v4 prediction, in or out of this window
        "breaks": {},
        "corrected_outdoor": {"past": [], "future": []},
    }
    has_model = bool(isinstance(model, Mapping) and model.get("has_model"))
    range_days = RANGES.get(range_str, 1.0)
    bt_points = backtest.get("points") if isinstance(backtest, Mapping) else None
    ids = [CA_SENSOR, *(s for s in dict.fromkeys(sensor_ids) if s != CA_SENSOR)]
    model_primary = model.get("primary") if isinstance(model, Mapping) else None
    # 0.9.6 Lane F: the span of EVERY past v4 prediction (any of these sensors, any look-ahead, live archive or
    # backtest), not just this window's, so the page can say "none in this view" instead of "none yet". It rides the
    # loops below (series() already walks every slot of a line), so it costs no extra pass.
    span: list[float] = []
    span_sensors: list[str] = []

    def _seen(t: float) -> None:
        if not span:
            span.extend((t, t))
        elif t < span[0]:
            span[0] = t
        elif t > span[1]:
            span[1] = t

    for sid in ids:
        per_lead: dict[str, list[dict[str, Any]]] = {}
        for lead in CHART_LEADS:
            every = archive.series(sid, lead, float("-inf"), float("inf")) if archive is not None else {}
            for slot in every:
                _seen(float(slot))
            if every and sid not in span_sensors:
                span_sensors.append(sid)
            live = {slot: v for slot, v in every.items() if start <= slot < end}
            pairs: list[tuple[float, float]] = [(float(slot), v) for slot, v in live.items()]
            live_slots = set(live)
            by_sensor = bt_points.get(sid) if isinstance(bt_points, Mapping) else None
            rows = by_sensor.get(lead) if isinstance(by_sensor, Mapping) else None
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, Mapping):
                    continue
                t = _epoch(item.get("t"))
                v4 = _num(item.get("v4"))
                if t is None or v4 is None:
                    continue
                _seen(t)
                if sid not in span_sensors:
                    span_sensors.append(sid)
                if not (start <= t < end):
                    continue
                if slot_key(datetime.fromtimestamp(t, tz)) in live_slots:
                    continue
                pairs.append((t, v4))
            if pairs:
                per_lead[lead] = _out_pairs(_downsample_pairs(pairs, range_days, tz), tz, unit)
        if per_lead:
            out["past"][sid] = per_lead
    if span and span[0] <= now.timestamp():
        out["past_span"] = {
            "first": _iso(span[0], tz),
            "last": _iso(min(span[1], now.timestamp()), tz),
            "sensors": span_sensors,  # the lines with any past prediction, so a chip is not taken away by the window
        }

    def _line(lines: Any, sid: str) -> Sequence[tuple[datetime, float]]:
        if not isinstance(lines, Mapping):
            return []
        got = lines.get(sid)
        if not got and sid == CA_SENSOR and isinstance(model_primary, str):
            got = lines.get(model_primary)  # no sleep node: "_ca" is the primary node's line (v4_archive rule)
        return got or []

    future = last_future.get("predictions") if isinstance(last_future, Mapping) else None
    if not historical and isinstance(future, Mapping):
        now_ts = now.timestamp()
        for sid in ids:
            pts = [(t.timestamp(), v) for t, v in _line(future, sid) if t.timestamp() > now_ts and math.isfinite(v)]
            if pts:
                out["future"][sid] = _out_pairs(pts, tz, unit)
        out["future_at"] = last_future.get("at")
        base = last_future.get("baseline")
        for sid in ids:
            pts = [(t.timestamp(), v) for t, v in _line(base, sid) if t.timestamp() > now_ts and math.isfinite(v)]
            if pts:
                out["baseline"][sid] = _out_pairs(pts, tz, unit)
        corrected = last_future.get("corrected") if isinstance(last_future, Mapping) else None
        if isinstance(corrected, list):
            out["corrected_outdoor"]["future"] = _out_pairs(
                [(t.timestamp(), v) for t, v in corrected if t.timestamp() > now_ts], tz, unit
            )
    out["corrected_outdoor"]["past"] = _out_pairs(
        _downsample_pairs([(t.timestamp(), v) for t, v in corrected_past], range_days, tz), tz, unit
    )
    out["sensors"] = sorted(set(out["past"]) | set(out["future"]) | set(out["baseline"]), key=ids.index)
    if CA_SENSOR in out["sensors"] and range_days <= _HOURLY_MAX_DAYS:  # a daily mean is no one sensor's reading
        out["breaks"][CA_SENSOR] = [_iso(t, tz) for t in sorted(set(ca_swaps))]
    out["ready"] = has_model or bool(out["sensors"])  # "has something to draw"; not an accuracy verdict
    out["reason"] = "ok" if out["ready"] else "no_model"
    return out
