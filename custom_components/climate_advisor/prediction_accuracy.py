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
  [{t, actual, flat, v4, v3}]. Every key is optional. A backtest point is used only for a 30-minute slot the live
  archive does not cover. The file's own ``gate`` / ``gate_summary`` are NOT read here any more (Issue #1492).

The "How each model has scored" table (payload ``gate`` / ``gate_summary``) is computed live on every request by
``live_scorecard`` over the last ``SCORECARD_WINDOW_DAYS`` (30) days ending now, from the live v4 archive, v3's logs and
the chart log; it no longer changes only when the owner deploys (Issue #1492). Information only: it switches nothing.

v3 exists only for the reading Climate Advisor uses (``_ca``): at 4 h its first-write-wins archive stored as chart-log
``pred_indoor``; at 1/4/12/24 h its own ``V3_ARCHIVE_FILE`` archive (Issue #1492, filled from 0.9.x on), read where the
chart log has no 4 h value.

Each engine is scored on its own points (Issue #1400): v3 from its logged ``pred_indoor``, the "no change" baseline
from the chart log's own readings, v4 from its archive and the backtest. A zone whose v4 archive is still empty
therefore still shows v3's and "no change"'s scores, and the actual line.
"""

from __future__ import annotations

import bisect
import functools
import json
import logging
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from . import hvac_season, i18n
from .chart_log import epoch_range
from .const import (
    ENGINE_V3,
    SCORECARD_BIAS_LIMIT_F,
    SCORECARD_LEADS,
    SCORECARD_MIN_COVERAGE,
    SCORECARD_MIN_N,
    SCORECARD_WINDOW_DAYS,
    V4_ARCHIVE_LEADS,
    V4_BACKTEST_FILE,
)
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


@functools.lru_cache(maxsize=65536)
def _epoch_of_str(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else dt.replace(tzinfo=None).timestamp()


def _epoch(value: Any) -> float | None:
    """A row timestamp as epoch seconds. Memoised on the string (Issue #1730): a page reads each row's ``ts`` ~10 times
    (every series, the scorecard, the heat runs, the swaps) and across requests; a pure function of the string."""
    return _epoch_of_str(value) if isinstance(value, str) else None


class _ChartRows:
    """The chart rows a page reads, with the window of each read found by bisection when the log is time-sorted.

    Issue #1730. ``rows`` is every row (a copy taken on the event loop). ``view`` is the log's own ``(rows, n)`` when it
    knows them to be time-sorted (``ChartStateLog.sorted_rows_view``, Issue #1598), else ``None``: then every window is
    all of ``rows`` and the caller's own filter does the work, as before. A window is a SUPERSET of what the caller
    keeps (inclusive of both ends, log order); the caller re-applies its exact predicate.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]], log: Any = None) -> None:
        self.rows = rows
        self.view: tuple[list[dict[str, Any]], int] | None = None
        if log is not None:
            try:
                self.view = log.sorted_rows_view()
            except Exception:  # noqa: BLE001 - a log that cannot say is a log to scan
                self.view = None

    def window(self, lo: float, hi: float) -> Sequence[Mapping[str, Any]]:
        if self.view is not None:
            got = epoch_range(self.view[0], self.view[1], lo, hi)
            if got is not None:
                return self.view[0][got[0] : got[1]]
        return self.rows

    def swap_rows(self, lo: float, hi: float) -> Sequence[Mapping[str, Any]]:
        """The rows ``ca_swap_epochs`` needs for ``[lo, hi]``: the window and the nearest ``band_sensor`` row each side.

        A swap is a change between two consecutive rows that have a ``band_sensor``, and the pair that straddles an
        edge of the window decides it (the first/last such row of the log only decide a bedtime or wake-up that lies
        outside the window, which is dropped). Every row when the log is not time-sorted.
        """
        if self.view is None:
            return self.rows
        rows, n = self.view
        got = epoch_range(rows, n, lo, hi)
        if got is None:
            return self.rows

        def has_band(i: int) -> bool:
            sid = rows[i].get("band_sensor")
            return isinstance(sid, str) and bool(sid)

        i, j = got
        i0 = i - 1
        while i0 >= 0 and not has_band(i0):
            i0 -= 1
        j0 = j
        while j0 < n and not has_band(j0):
            j0 += 1
        return rows[max(i0, 0) : min(j0 + 1, n)]


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
    v3_archive: V4Archive | None = None,
    chart_log: Any = None,
) -> dict[str, Any]:
    """The endpoint payload. ``lead``/``range_str`` must already be valid (the view resets bad values to defaults).

    ``sleep_sensor``/``sleep_time``/``wake_time`` are the zone's ``sleep_indoor_temp_entity``/``sleep_time``/
    ``wake_time`` settings: with them ``ca_swaps`` (Issue #1431) lists when the reading Climate Advisor uses swapped
    sensor in the window (``ca_swap_epochs``, the rule behind the Status chart's ``ca_swaps``), so the page breaks its
    ``_ca`` lines there. Without a sleep sensor only the chart log's recorded ``band_sensor`` changes count.

    ``chart_log`` (Issue #1730, optional) is the ``ChartStateLog`` ``chart_rows`` was copied from: while it is
    time-sorted every read of the rows finds its window by bisection instead of parsing the whole year; the payload is
    the same without it.
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

    chart = _ChartRows(chart_rows, chart_log)
    near_rows = chart.window(start - lead_s - _MATCH_TOLERANCE_S, end + _MATCH_TOLERANCE_S)  # every read below is in it
    actual = _row_series(near_rows, actual_of, start - lead_s - _MATCH_TOLERANCE_S, end + _MATCH_TOLERANCE_S)
    has_v3 = sensor == CA_SENSOR and lead == DEFAULT_LEAD
    v3_series = _row_series(near_rows, lambda r: r.get("pred_indoor"), start, end) if has_v3 else None
    # Issue #1492: v3's own look-ahead archive answers the leads the chart log never had (and 4 h slots it lacks).
    v3_extra: dict[int, float] = {}
    if sensor == CA_SENSOR and v3_archive is not None:
        try:
            v3_extra = v3_archive.series(CA_SENSOR, lead, start, end)
        except Exception:  # noqa: BLE001 - a missing comparison line beats no page
            _LOGGER.debug("prediction accuracy: v3 archive read failed", exc_info=True)
    has_v3 = has_v3 or bool(v3_extra)

    # One point per 30-minute slot. Every chart row in the window gives one (its actual, v3's logged value, the "no
    # change" value), so v3 and the baseline are scored even while v4 has nothing (Issue #1400); v4 joins its slots.
    points: dict[float, dict[str, Any]] = {}
    for row in near_rows:
        t = _epoch(row.get("ts"))
        if t is None or not (start <= t < end) or row.get("event") is not None:
            continue
        if _num(actual_of(row)) is None and not (v3_series is not None and _num(row.get("pred_indoor")) is not None):
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
                v3 = v3_extra.get(int(t))
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
    read_ids = _read_sensor_ids(chart_rows, {str(s.get("id")) for s in sensors_out})
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
                chart.swap_rows(start, end),
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

    # Issue #1492: the scorecard is live (last 30 days ending now, whatever window the chart above shows).
    gate_out: list[dict[str, Any]] = []
    gate_summary: dict[str, Any] | None = None
    try:
        gate_out, gate_summary = live_scorecard(
            zone=zone,
            now=now,
            unit=unit,
            chart_rows=chart_rows,
            archive=archive,
            v3_archive=v3_archive,
            v3_running=ENGINE_V3 in (primary_engine, shadow_engine),
            chart=chart,
        )
    except Exception:  # noqa: BLE001 - information only: an empty table beats no page
        _LOGGER.warning("prediction accuracy: the live scorecard failed; the table is empty", exc_info=True)

    model_out = {"confidence": "none", "fitted_at": None, "span_days": None}
    if isinstance(model, Mapping) and model.get("has_model"):
        model_out = {
            "confidence": model.get("confidence", "none"),
            "fitted_at": model.get("fitted_at"),
            "span_days": model.get("span_days"),
        }

    heat_runs = [] if downsample == "daily" else _heat_runs(near_rows, start, end, tz)

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
        "gate_summary": gate_summary,
    }


# ---------------------------------------------------------------------------
# Live scorecard: "How each model has scored" (Issue #1492)
# ---------------------------------------------------------------------------


def live_scorecard(
    *,
    zone: str,
    now: datetime,
    unit: str,
    chart_rows: Sequence[Mapping[str, Any]],
    archive: V4Archive,
    v3_archive: V4Archive | None = None,
    v3_running: bool = True,
    window_days: float = SCORECARD_WINDOW_DAYS,
    chart: _ChartRows | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """DP-8 rows for the reading Climate Advisor uses (``_ca``), scored live over ``window_days`` ending ``now``.

    Occupant view: the table on the Prediction accuracy page shows how each prediction engine has done lately; before
    this it changed only when a new version was deployed (it was copied from the deploy-time backtest). Information
    only: nothing reads it to switch an engine or a decision.

    One row per scored season (``hvac_season.SCORED_SEASONS``: heating, summer) and look-ahead (``SCORECARD_LEADS``).
    A point is a 30-minute slot ``t`` inside the window with an actual reading at ``t`` and at ``t - lead`` (the "no
    change" guess); its season is that of the local day the prediction was made (``t - lead``), by what the HVAC did
    that day (``hvac_season.day_seasons``, the backtest's own rule). Engines:

    * v4: the live archive (``archive``), value written ``lead`` ahead;
    * v3: at 4 h the chart log's ``pred_indoor`` (its first-write-wins 4 h archive), else / at other leads its own
      look-ahead archive (``v3_archive``). With no v3 point at all in the row the row is scored on v4 and "no change"
      only and v3 reads ``collecting`` (``off`` when v3 is neither the main nor the comparison engine).

    As in the backtest, a row's MAEs are taken over the SAME points (v4, v3 and "no change" all present, or v4 and
    "no change" while v3 is collecting); ``coverage`` is the share of the row's points v4 answered. ``pass`` =
    scored AND v4 < v3 AND v4 < "no change" AND |bias| <= ``SCORECARD_BIAS_LIMIT_F`` AND coverage >=
    ``SCORECARD_MIN_COVERAGE``; ``scored`` needs n >= ``SCORECARD_MIN_N`` and all three MAEs, else ``reasons`` says
    why ("not enough data yet", "v3 still collecting"). Errors are predicted - actual (positive = too warm); the
    row values are converted to ``unit`` here, the pass rule is judged in degrees F. Pure; never reads a file.

    ``chart`` (Issue #1730, optional) is the page's ``_ChartRows``: the reads below then cover only their window of the
    log, found by bisection; without it every row is read, with the same answer.
    """
    tz = now.tzinfo or datetime.now().astimezone().tzinfo
    assert tz is not None
    end = now.timestamp()
    start = end - window_days * 86400
    max_lead_s = max(lead_hours(ld) for ld in SCORECARD_LEADS) * 3600
    lo = start - max_lead_s - _MATCH_TOLERANCE_S
    near_rows = (chart or _ChartRows(chart_rows)).window(lo - 86400, end + _MATCH_TOLERANCE_S)  # all three reads below
    actual = _row_series(near_rows, lambda r: r.get("indoor"), lo, end + _MATCH_TOLERANCE_S)
    logged_v3 = _row_series(near_rows, lambda r: r.get("pred_indoor"), start - _MATCH_TOLERANCE_S, end)
    window_rows = [r for r in near_rows if (t := _epoch(r.get("ts"))) is not None and lo - 86400 <= t <= end]
    seasons = hvac_season.day_seasons(window_rows, tz)
    slots = sorted(
        {
            slot_key(datetime.fromtimestamp(t, tz))
            for r in window_rows
            if r.get("event") is None
            and (t := _epoch(r.get("ts"))) is not None
            and start <= t < end
            and _num(r.get("indoor")) is not None
        }
    )
    slots = [s for s in slots if start <= s < end]  # a row a few seconds before the window can round into it

    by_key: dict[tuple[str, str], list[tuple[float, float, float | None, float | None]]] = {
        (season, lead): [] for season in hvac_season.SCORED_SEASONS for lead in SCORECARD_LEADS
    }
    for lead in SCORECARD_LEADS:
        lead_s = lead_hours(lead) * 3600
        v4_line = archive.series(CA_SENSOR, lead, start, end)
        v3_line: dict[int, float] = {}
        if v3_archive is not None:
            v3_line = v3_archive.series(CA_SENSOR, lead, start, end)
        for t in slots:
            season = seasons.get(datetime.fromtimestamp(t - lead_s, tz).date(), hvac_season.SHOULDER)
            key = (season, lead)
            if key not in by_key:
                continue
            a = actual.nearest(t)
            flat = actual.nearest(t - lead_s)
            if a is None or flat is None:
                continue
            v3 = logged_v3.nearest(t) if lead == "4h" else None
            if v3 is None:
                v3 = v3_line.get(t)
            by_key[key].append((a, flat, v4_line.get(t), v3))
    rows = [_score_row(zone, season, lead, by_key[(season, lead)], v3_running) for season, lead in by_key]

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for field in ("v4", "v3", "flat", "bias"):
            if item.get(field) is not None:
                item[field] = display_delta(float(item[field]), unit, 2)
        out.append(item)
    summary = {
        "live": True,
        "window_days": window_days,
        "start": _iso(start, tz),
        "end": _iso(end, tz),
        "min_n": SCORECARD_MIN_N,
        "passed": sum(1 for r in rows if r["pass"]),
        "scored": sum(1 for r in rows if r["scored"]),
        "total": len(rows),
    }
    return out, summary


def _score_row(
    zone: str,
    season: str,
    lead: str,
    points: list[tuple[float, float, float | None, float | None]],
    v3_running: bool,
) -> dict[str, Any]:
    """One DP-8 row from ``(actual, flat, v4, v3)`` points (degrees F); see ``live_scorecard``."""
    with_v3 = [p for p in points if p[3] is not None]
    v3_state = "ok" if with_v3 else ("collecting" if v3_running else "off")
    base = with_v3 if with_v3 else points
    common = [p for p in base if p[2] is not None]
    n = len(common)
    coverage = round(n / len(base), 3) if base else None

    def mae(idx: int) -> float | None:
        errs = [abs(p[idx] - p[0]) for p in common if p[idx] is not None]  # type: ignore[operator]
        return round(sum(errs) / len(errs), 3) if errs else None

    v4, flat = mae(2), mae(1)
    v3 = mae(3) if with_v3 else None
    bias = round(sum(p[2] - p[0] for p in common) / n, 3) if n else None  # type: ignore[operator]
    beats_v3 = (v4 < v3) if v4 is not None and v3 is not None else None
    beats_flat = (v4 < flat) if v4 is not None and flat is not None else None
    bias_ok = abs(bias) <= SCORECARD_BIAS_LIMIT_F if bias is not None else None
    scored = n >= SCORECARD_MIN_N and v4 is not None and flat is not None and v3 is not None
    # Issue #1341: each reason is a catalog message (status.pa_reason.*); `reasons` stays the English text of the same
    # messages and `reasons_msg` carries them for the dashboard to render in the viewer's language.
    reason_msgs: list[i18n.Msg] = []
    if n < SCORECARD_MIN_N:
        reason_msgs.append(i18n.Msg("status.pa_reason.not_enough_data", n=n, need=SCORECARD_MIN_N))
    if v3_state == "collecting":
        reason_msgs.append(i18n.Msg("status.pa_reason.v3_collecting"))
    elif v3_state == "off":
        reason_msgs.append(i18n.Msg("status.pa_reason.v3_off"))
    if scored:
        if not beats_v3:
            reason_msgs.append(i18n.Msg("status.pa_reason.v4_not_better_v3"))
        if not beats_flat:
            reason_msgs.append(i18n.Msg("status.pa_reason.v4_not_better_flat"))
        if not bias_ok:
            reason_msgs.append(
                i18n.Msg("status.pa_reason.bias_over", bias=abs(bias or 0.0), limit=SCORECARD_BIAS_LIMIT_F)
            )
        if coverage is not None and coverage < SCORECARD_MIN_COVERAGE:
            reason_msgs.append(i18n.Msg("status.pa_reason.coverage_low", coverage=coverage, min=SCORECARD_MIN_COVERAGE))
    reasons: list[str] = [i18n.render_en(m) for m in reason_msgs]
    return {
        "zone": zone,
        "sensor": CA_SENSOR,
        "season": season,
        "lead": lead,
        "v4": v4,
        "v3": v3,
        "flat": flat,
        "bias": bias,
        "n": n,
        "coverage": coverage,
        "v3_state": v3_state,
        "beats_v3": beats_v3,
        "beats_flat": beats_flat,
        "bias_ok": bias_ok,
        "scored": scored,
        "pass": scored and not reasons,
        "reasons": reasons,
        "reasons_msg": [m.to_wire() for m in reason_msgs],
    }


def _v4_sensor_ids(archive: V4Archive, bt_points: Any) -> set[str]:
    """Sensor ids with at least one v4 prediction: in the live archive or in the published backtest (any lead)."""
    out: set[str] = set()
    try:
        out.update(archive.sensors_with_slots())  # one slot per line, not every slot of a year (Issue #1730)
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


def _read_sensor_ids(rows: Sequence[Mapping[str, Any]], wanted: set[str] | None = None) -> set[str]:
    """Sensor ids the chart log has a reading for (``_ca`` = the ``indoor`` column), any time.

    ``wanted`` (Issue #1730): the caller only asks about these ids, so the read stops once all of them are found; the
    answer for each of them is the same as reading every row (the set may then lack ids nobody asked about)."""
    out: set[str] = set()
    for r in rows:
        before = len(out)
        if CA_SENSOR not in out and _num(r.get("indoor")) is not None:
            out.add(CA_SENSOR)
        readings = r.get("indoor_sensors")
        if isinstance(readings, dict):
            out.update(k for k, v in readings.items() if isinstance(k, str) and k not in out and _num(v) is not None)
        if wanted is not None and len(out) != before and wanted <= out:
            break
    return out


# ---------------------------------------------------------------------------
# Status chart (Issue #1230 R2)
# ---------------------------------------------------------------------------

CHART_LEADS = V4_ARCHIVE_LEADS  # Issue #1483: 1 h to 5 days; the backtest publishes 1-24 h only


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


class _BucketMemo:
    """Epoch -> ``(group id, bucket start)`` for :func:`_downsample_pairs`, shared by every line of one window.

    Issue #1730. Every look-ahead of every sensor sits on the same 30-minute slots, so a year is bucketed once, and a
    pair is grouped by a small integer instead of by hashing a ``datetime`` (equal bucket starts share one id, as they
    shared one dict key).
    """

    __slots__ = ("by_epoch", "ids")

    def __init__(self) -> None:
        self.by_epoch: dict[float, tuple[int, datetime]] = {}
        self.ids: dict[datetime, int] = {}


def _downsample_pairs(
    pairs: list[tuple[float, float]], range_days: float, tz: tzinfo, memo: _BucketMemo | None = None
) -> list[tuple[float, float]]:
    """Same buckets as the chart log: raw to 3 days, local-hour means to 30 days, local-day means beyond.

    ``memo`` (optional; one per ``range_days``) lets the callers of one window share the bucket of each epoch.
    """
    if range_days <= _RAW_MAX_DAYS or not pairs:
        return sorted(pairs)
    hourly = range_days <= _HOURLY_MAX_DAYS
    memo = memo or _BucketMemo()
    by_epoch, ids = memo.by_epoch, memo.ids
    groups: dict[int, tuple[datetime, list[float]]] = {}
    cur_id = -1
    cur: list[float] = []
    for t, v in pairs:
        got = by_epoch.get(t)
        if got is None:
            key = datetime.fromtimestamp(t, tz).replace(minute=0, second=0, microsecond=0)
            if not hourly:
                key = key.replace(hour=0)
            got = by_epoch[t] = (ids.setdefault(key, len(ids)), key)
        if got[0] == cur_id:  # slots arrive in time order: a bucket is mostly one run
            cur.append(v)
            continue
        group = groups.get(got[0])
        if group is None:
            group = groups[got[0]] = (got[1], [])  # the first bucket start met in THIS call stands for the group
        cur_id, cur = got[0], group[1]
        cur.append(v)
    return [(k.timestamp(), sum(vs) / len(vs)) for k, vs in sorted(groups.values(), key=lambda g: g[0])]


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
        "hourly_until": None,  # Issue #1483: last real hourly forecast hour; past it the line is from daily highs/lows
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
    # backtest), not just this window's, so the page can say "none in this view" instead of "none yet". It comes
    # from V4Archive.span (first and last valid slot), so no whole-line read.
    span: list[float] = []
    span_sensors: list[str] = []

    def _seen(t: float) -> None:
        if not span:
            span.extend((t, t))
        elif t < span[0]:
            span[0] = t
        elif t > span[1]:
            span[1] = t

    bucket_memo = _BucketMemo()  # Issue #1730: shared by every line of this window
    for sid in ids:
        per_lead: dict[str, list[dict[str, Any]]] = {}
        # one pass over the window for every look-ahead (Issue #1730)
        window = archive.series_leads(sid, CHART_LEADS, start, end) if archive is not None else {}
        for lead in CHART_LEADS:
            # Issue #1730: only the window is read; the whole-archive span comes from the first/last valid slot.
            ends = archive.span(sid, lead) if archive is not None else None
            if ends is not None:
                _seen(float(ends[0]))
                _seen(float(ends[1]))
                if sid not in span_sensors:
                    span_sensors.append(sid)
            live = window.get(lead, {}) if ends is not None else {}
            pairs: list[tuple[float, float]] = list(live.items())  # (slot, value): epochs, whole numbers
            live_slots = live  # membership of a slot only
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
                per_lead[lead] = _out_pairs(_downsample_pairs(pairs, range_days, tz, bucket_memo), tz, unit)
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
        out["hourly_until"] = last_future.get("hourly_until")
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
