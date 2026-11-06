#!/usr/bin/env python3
"""Walk-forward backtest of the v4 indoor predictor against v3 and "no change" (Issue #1230 R1, DP-8).

WHY: the owner sees v4's line on the dashboard only once this tool shows, over all recorded
history, that v4 predicts the room better than today's (v3) line and better than "nothing will
change", in the heating season AND in summer, with an average bias inside +-0.5 F. A house whose
v4 line is not proven keeps showing v3 only.

HOW (walk-forward, no future leakage)
  * Test weeks start Monday 00:00 local. Before each test week the v4 model is fitted on chart_log
    rows STRICTLY BEFORE that week (``--train-days`` back). Every fit input is checked by
    ``assert_no_leak`` (``LeakageError``) - the fit can never see a row, sample or forecast snapshot
    from the test week.
  * Anchors are hourly (``--anchor-step-hours``) inside the week. At an anchor A only what a live
    prediction would know is used: the readings at-or-before A, the forecast issued at-or-before A
    (forecast archive snapshot when available, else chart_log ``pred_outdoor``), the planned
    setpoints and the planned windows/fan.
  * Scored at 1 / 4 / 12 / 24 h against the sensor reading at A+lead (linear between log rows).

MODELS COMPARED ON THE SAME INPUTS (same anchors, forecast, setpoints, windows plan)
  * v4   - ``predictor_v4.fit`` / ``predict``  (lane A, pure Python)
  * v3   - the PRODUCTION ``coordinator._build_predicted_indoor_future`` imported headless (HA stubs
           from tools/sim_harness/ha_stubs.py), fed the thermal model rebuilt from the learning DB
           observations dated before the week (``--v3-model asof``) or the current cache (``current``)
  * flat - persistence: the reading at A
  * logged ``pred_indoor`` - the real ~4 h archive, a sanity row for the v3 replay (``_ca`` only)

INPUT CHOICES (documented because they decide how honest the numbers are)
  * forecast: ``--forecast-archive`` snapshot issued at-or-before A (lead-correct); hours it lacks
    fall back to chart_log ``pred_outdoor`` of that hour. chart_log keeps only the forecast made AT
    that hour (lead 0), so a run without the archive scores both models with a forecast better than
    a real 12 h-ahead one: absolute errors are optimistic, the comparison stays fair.
  * setpoints: the logged target band (``lower``/``upper``, else ``setpoint`` by mode) at each future
    step ("as scheduled"), or frozen at A with ``--setpoints hold``. An occupant change after A that
    moves the band leaks a little; identical for v3 and v4.
  * windows/fan: closed/off, like v3 and the live shadow, UNLESS chart_log ``windows_recommended`` is
    true at A, then open for ``--window-plan-hours`` (default 4) for BOTH models (v3 through the
    classification's window times). Fan is never planned. A "v4 with actual inputs" variant (actual
    outdoor, windows, fan) is reported apart as a diagnostic, never in the gate.

SCORING: error = predicted - actual (positive bias = too warm). Gate (DP-8) on the ``_ca`` line, per
season (heating Nov-Mar, summer Jun-Sep; shoulder Apr/May/Oct reported apart), at 1/4/12 h:
v4 MAE < v3 MAE and < flat MAE, |v4 bias| <= 0.5 F, n >= --min-n, v4 coverage >= --min-coverage.
All MAEs of a row are taken over the SAME anchors (v4, v3 and flat all present).

Usage:
    python tools/predict_backtest.py --chart-log chart.json --learning-db learning.json
    python tools/predict_backtest.py --zone <entry_id> --json out.json --publish
    python tools/predict_backtest.py --all-zones --since 2026-04-01 --publish
Needs tools/.deploy.env for SSH (never printed).
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import importlib
import importlib.util
import json
import math
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
_ROOT = _TOOLS_DIR.parent
_PKG_DIR = _ROOT / "custom_components" / "climate_advisor"
sys.path.insert(0, str(_TOOLS_DIR))
from _zone_storage import CHART_LOG_FILE, LEARNING_DB_FILE, discover_entry_ids, remote_path  # noqa: E402

BACKTEST_FILE = "climate_advisor_v4_backtest.json"  # entry-scoped: climate_advisor_v4_backtest_<entry_id>.json
FORECAST_ARCHIVE_FILE = "climate_advisor_forecast_archive.json"
SCHEMA_VERSION = 1

LEADS_H = (1, 4, 12, 24)
GATE_LEADS_H = (1, 4, 12)
GATE_SEASONS = ("heating", "summer")
SEASONS = ("heating", "summer", "shoulder", "all")
BIAS_LIMIT_F = 0.5
HEATING_MONTHS = frozenset({11, 12, 1, 2, 3})
SUMMER_MONTHS = frozenset({6, 7, 8, 9})
HORIZON_H = max(LEADS_H)
STEP_S = 900  # v4 grid: V4_STEP_MINUTES
CA = "_ca"

# Predictor override: tests (and callers) set this to a module-like object implementing the contract.
_PREDICTOR: Any = None
_COORDINATOR: Any = None


class LeakageError(RuntimeError):
    """A fit input contained data from the test week or later."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def season_of(dt: datetime) -> str:
    if dt.month in HEATING_MONTHS:
        return "heating"
    if dt.month in SUMMER_MONTHS:
        return "summer"
    return "shoulder"


def at(anchor: datetime, seconds: float) -> datetime:
    """anchor + real elapsed seconds (never wall-clock arithmetic across a DST change)."""
    return datetime.fromtimestamp(anchor.timestamp() + seconds, anchor.tzinfo)


def hour_key(dt: datetime) -> int:
    return int(dt.replace(minute=0, second=0, microsecond=0).timestamp())


def parse_ts(value: Any, tz: tzinfo) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except TypeError, ValueError:
        return None
    return f if math.isfinite(f) else None


def resolve_tz(name: str | None, fallback_row_ts: str | None = None) -> tzinfo:
    """--tz name; else TZ env / /etc/timezone; else the fixed offset of the first row."""
    import os  # noqa: PLC0415
    import zoneinfo  # noqa: PLC0415

    candidates = [name, os.environ.get("TZ")]
    with contextlib.suppress(OSError):
        candidates.append(Path("/etc/timezone").read_text().strip())
    for cand in candidates:
        if cand:
            try:
                return zoneinfo.ZoneInfo(cand)
            except Exception:  # noqa: BLE001, S112
                continue
    if fallback_row_ts:
        try:
            dt = datetime.fromisoformat(fallback_row_ts)
            if dt.tzinfo is not None:
                return dt.tzinfo
        except ValueError:
            pass
    return UTC


def assert_no_leak(items: list, cutoff: datetime, what: str, key=lambda x: x) -> None:
    """Raise ``LeakageError`` when any item is at or after ``cutoff`` (the test-week start)."""
    bad = [i for i in items if key(i) >= cutoff]
    if bad:
        raise LeakageError(f"{what}: {len(bad)} item(s) at/after the test-week start {cutoff.isoformat()}")


# ---------------------------------------------------------------------------
# Chart log index
# ---------------------------------------------------------------------------


class ChartIndex:
    """Sorted chart_log rows with the series the backtest reads (all internal F)."""

    def __init__(self, rows: list[dict], tz: tzinfo, sensors: list[str]) -> None:
        parsed = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                dt = parse_ts(r.get("ts"), tz)
            except TypeError, ValueError:
                continue
            parsed.append((dt.timestamp(), dt, r))
        parsed.sort(key=lambda p: p[0])
        self.t = [p[0] for p in parsed]
        self.dts = [p[1] for p in parsed]
        self.rows = [p[2] for p in parsed]
        self.tz = tz
        self.series: dict[str, tuple[list[float], list[float]]] = {}
        for key in dict.fromkeys([CA, "outdoor", "_logged", *sensors]):
            xs: list[float] = []
            vs: list[float] = []
            for t, r in zip(self.t, self.rows, strict=True):
                v = self._raw(key, r)
                if v is not None:
                    xs.append(t)
                    vs.append(v)
            self.series[key] = (xs, vs)
        self.band: list[tuple[float | None, float | None]] = []
        self._build_band()
        self.pred_by_hour: dict[int, float] = {}
        for dt, r in zip(self.dts, self.rows, strict=True):
            v = _num(r.get("pred_outdoor"))
            if v is not None:
                self.pred_by_hour.setdefault(hour_key(dt), v)

    @staticmethod
    def _raw(key: str, r: dict) -> float | None:
        if key == CA:
            return _num(r.get("indoor"))
        if key == "_logged":
            return _num(r.get("pred_indoor"))
        if key == "outdoor":
            return _num(r.get("outdoor"))
        sens = r.get("indoor_sensors")
        return _num(sens.get(key)) if isinstance(sens, dict) else None

    def _build_band(self) -> None:
        lo_c: float | None = None
        hi_c: float | None = None
        last_t = -1e18
        for t, r in zip(self.t, self.rows, strict=True):
            lo, hi = _num(r.get("lower")), _num(r.get("upper"))
            if lo is None and hi is None:
                sp = _num(r.get("setpoint"))
                mode = str(r.get("hvac") or "").lower()
                if sp is not None and mode.startswith("heat"):
                    lo = sp
                elif sp is not None and mode.startswith("cool"):
                    hi = sp
            if lo is not None or hi is not None:
                lo_c = lo if lo is not None else lo_c
                hi_c = hi if hi is not None else hi_c
                last_t = t
            self.band.append((lo_c, hi_c) if t - last_t <= 3 * 3600 else (None, None))

    def _idx_at_or_before(self, t: float) -> int:
        return bisect.bisect_right(self.t, t) - 1

    def last_at(self, key: str, t: float, max_age_s: float = 2400.0) -> float | None:
        """Causal reading: the latest value at-or-before ``t`` no older than ``max_age_s``."""
        xs, vs = self.series.get(key, ([], []))
        i = bisect.bisect_right(xs, t) - 1
        if i < 0 or t - xs[i] > max_age_s:
            return None
        return vs[i]

    def value_at(self, key: str, t: float, max_gap_s: float = 2700.0) -> float | None:
        """Actual value at ``t``: linear between the bracketing rows (gap <= max_gap_s)."""
        xs, vs = self.series.get(key, ([], []))
        if not xs:
            return None
        i = bisect.bisect_left(xs, t)
        if i < len(xs) and abs(xs[i] - t) <= 120:
            return vs[i]
        if i == 0 or i >= len(xs):
            j = min(max(i, 0), len(xs) - 1)
            return vs[j] if abs(xs[j] - t) <= 600 else None
        x0, x1 = xs[i - 1], xs[i]
        if x1 - x0 > max_gap_s:
            return None
        return vs[i - 1] + (vs[i] - vs[i - 1]) * (t - x0) / (x1 - x0)

    def band_at(self, t: float) -> tuple[float | None, float | None]:
        i = self._idx_at_or_before(t)
        if i < 0 or t - self.t[i] > 2 * 3600:
            return (None, None)
        return self.band[i]

    def row_at(self, t: float, max_age_s: float = 3600.0) -> dict | None:
        i = self._idx_at_or_before(t)
        if i < 0 or t - self.t[i] > max_age_s:
            return None
        return self.rows[i]


def select_sensors(rows: list[dict], primary_arg: str | None, min_cov: float = 0.3) -> tuple[str, list[str]]:
    """(primary, nodes primary-first). ``_ca`` alone for logs without ``indoor_sensors``."""
    n_rows = 0
    counts: dict[str, int] = {}
    match: dict[str, int] = {}
    for r in rows:
        sens = r.get("indoor_sensors")
        if not isinstance(sens, dict):
            continue
        n_rows += 1
        ind = _num(r.get("indoor"))
        for k, v in sens.items():
            fv = _num(v)
            if fv is None:
                continue
            counts[k] = counts.get(k, 0) + 1
            if ind is not None and abs(fv - ind) < 0.05:
                match[k] = match.get(k, 0) + 1
    good = sorted(k for k, c in counts.items() if n_rows and c / n_rows >= min_cov)
    if primary_arg:
        primary = primary_arg
    elif good:
        primary = max(good, key=lambda k: (match.get(k, 0), -good.index(k)))
        # `_ca` (the reading CA used) is the primary when no entity tracks it well
        if match.get(primary, 0) / max(counts.get(primary, 1), 1) < 0.5:
            primary = CA
    else:
        primary = CA
    nodes = [primary] + [k for k in good if k != primary]
    return primary, nodes


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


def decode_archive(data: Any) -> list[dict]:
    """Raw ``climate_advisor_forecast_archive`` file -> read-shape snapshots (the ForecastArchive contract)."""
    out: list[dict] = []
    snaps = data.get("snapshots") if isinstance(data, dict) else None
    for s in snaps or []:
        try:
            issued = datetime.fromisoformat(s["i"])
            h0 = datetime.fromisoformat(s["h0"])
            hours = [
                [(h0 + timedelta(hours=k)).isoformat(timespec="minutes"), float(v)]
                for k, v in enumerate(s["t"])
                if v is not None
            ]
        except KeyError, TypeError, ValueError:
            continue
        out.append({"issued": issued.isoformat(timespec="minutes"), "source": s.get("s", ""), "hours": hours})
    out.sort(key=lambda s: s["issued"])
    return out


class ForecastIndex:
    """The forecast as known at an anchor: archive snapshot first, chart_log ``pred_outdoor`` second."""

    def __init__(self, snapshots: list[dict] | None, idx: ChartIndex) -> None:
        self.idx = idx
        items = []
        for s in snapshots or []:
            try:
                issued = parse_ts(s["issued"], idx.tz).timestamp()
                hours = {int(parse_ts(h, idx.tz).timestamp()): float(v) for h, v in s["hours"]}
            except KeyError, TypeError, ValueError:
                continue
            items.append((issued, hours))
        items.sort(key=lambda x: x[0])
        self.issued = [i[0] for i in items]
        self.maps = [i[1] for i in items]

    def snapshot(self, anchor_ep: float, max_age_s: float = 6 * 3600) -> dict[int, float] | None:
        i = bisect.bisect_right(self.issued, anchor_ep) - 1
        if i < 0 or anchor_ep - self.issued[i] > max_age_s:
            return None
        return self.maps[i]

    def hourly(self, anchor: datetime, hours: int) -> tuple[list[float] | None, str]:
        """Forecast for anchor+0..hours (hourly), plus which source supplied it."""
        snap = self.snapshot(anchor.timestamp())
        vals: list[float | None] = []
        n_arch = 0
        last: float | None = None
        for h in range(hours + 1):
            key = int(at(anchor, 3600 * h).timestamp())
            v = snap.get(key) if snap is not None else None
            if v is not None:
                n_arch += 1
            else:
                v = self.idx.pred_by_hour.get(key)
            if v is None:
                v = last
            vals.append(v)
            last = v if v is not None else last
        first = next((v for v in vals if v is not None), None)
        if first is None:
            return None, ""
        filled = [first if v is None else v for v in vals]
        return filled, ("archive" if n_arch >= (hours + 1) / 2 else "chart_log")


def interp_hourly(vals: list[float], step_s: float) -> float:
    pos = step_s / 3600.0
    i = min(int(pos), len(vals) - 2)
    frac = pos - i
    return vals[i] + (vals[i + 1] - vals[i]) * frac


# ---------------------------------------------------------------------------
# Module loading (predictor_v4 and the production coordinator, headless)
# ---------------------------------------------------------------------------


def load_predictor() -> Any:
    """``predictor_v4`` (lane A). Pure Python, loaded by path; falls back to the stubbed package import."""
    global _PREDICTOR  # noqa: PLW0603
    if _PREDICTOR is not None:
        return _PREDICTOR
    path = _PKG_DIR / "predictor_v4.py"
    if not path.exists():
        raise SystemExit(f"ERROR: {path} not found - predictor_v4 (lane A) is required.")
    try:
        spec = importlib.util.spec_from_file_location("_ca_predictor_v4_standalone", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolves string annotations via sys.modules
        spec.loader.exec_module(module)
    except ImportError:  # relative import of a package sibling: use the package with HA stubbed
        sys.modules.pop("_ca_predictor_v4_standalone", None)
        _install_ha_stubs()
        module = importlib.import_module("custom_components.climate_advisor.predictor_v4")
    _PREDICTOR = module
    return module


def _install_ha_stubs() -> None:
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

    install_ha_stubs()


def load_coordinator() -> Any:
    """The production coordinator module, imported with homeassistant.* stubbed (the sim-harness pattern)."""
    global _COORDINATOR  # noqa: PLW0603
    if _COORDINATOR is not None:
        return _COORDINATOR
    _install_ha_stubs()
    _COORDINATOR = importlib.import_module("custom_components.climate_advisor.coordinator")
    return _COORDINATOR


def _set_ha_timezone(tz: tzinfo) -> None:
    """Make ``dt_util.as_local`` land in the zone's own tz (stub: DEFAULT_TIME_ZONE; real HA: setter)."""
    dt_mod = sys.modules.get("homeassistant.util.dt")
    if dt_mod is None:
        return
    if hasattr(dt_mod, "set_default_time_zone"):
        dt_mod.set_default_time_zone(tz)
    else:
        dt_mod.DEFAULT_TIME_ZONE = tz


def _thermal_replay() -> Any:
    import thermal_replay  # noqa: PLC0415

    return thermal_replay


# ---------------------------------------------------------------------------
# v3 replay with production code
# ---------------------------------------------------------------------------


def v3_model_from_cache(cache: dict, phase_h: float | None) -> dict:
    """The ``thermal_model`` dict ``LearningEngine.get_thermal_model`` hands the engine, from a cache."""
    conf = _thermal_replay()._confidence
    return {
        "confidence": conf.grade_hvac_confidence(cache),
        "confidence_k_passive": conf.grade_passive_confidence(cache),
        "k_passive": cache.get("k_passive"),
        "k_active_heat": cache.get("k_active_heat"),
        "k_active_cool": cache.get("k_active_cool"),
        "k_solar": cache.get("k_solar"),
        "k_vent_window": cache.get("k_vent_window"),
        "k_vent_fan": cache.get("k_vent_fan"),
        "solar_phase_offset_h": phase_h if phase_h is not None else 2.0,
    }


class V3Models:
    """The v3 thermal model per test week: rebuilt from observations dated before the week (as-of)."""

    def __init__(self, db: dict | None, mode: str) -> None:
        self.db = db or {}
        self.mode = mode
        cache = self.db.get("thermal_model_cache") or {}
        phase = cache.get("solar_phase_offset_h")
        self.phase = float(phase) if isinstance(phase, int | float) else None
        self.current = v3_model_from_cache(cache, self.phase) if cache else None
        self.obs = [o for o in self.db.get("thermal_observations") or [] if isinstance(o, dict) and o.get("date")]
        self.used: dict[str, int] = {}

    @property
    def available(self) -> bool:
        return self.current is not None or bool(self.obs)

    def for_week(self, week_start: datetime) -> tuple[dict, str]:
        if self.mode == "asof" and self.obs:
            cutoff = week_start.date().isoformat()
            prior = [o for o in self.obs if str(o["date"])[:10] < cutoff]
            if prior:
                cache = _thermal_replay().rebuild_model_cache(prior)
                self.used["asof"] = self.used.get("asof", 0) + 1
                return v3_model_from_cache(cache, self.phase), f"asof({len(prior)} obs)"
        self.used["current"] = self.used.get("current", 0) + 1
        return (self.current or {}), "current" if self.current else "none"

    def drift_note(self) -> str | None:
        """Sanity: the as-of rebuild at 'now' vs the DB's own current cache (k_passive)."""
        if not (self.obs and self.current):
            return None
        rebuilt = _thermal_replay().rebuild_model_cache(self.obs).get("k_passive")
        cur = self.current.get("k_passive")
        if rebuilt is None or cur is None or cur == 0:
            return None
        rel = abs(rebuilt - cur) / abs(cur)
        return f"as-of rebuild k_passive={rebuilt:.4f} vs cache {cur:.4f} ({rel:.0%} apart)"


def windows_plan(idx: ChartIndex, anchor: datetime, plan_hours: float) -> bool:
    row = idx.row_at(anchor.timestamp())
    return bool(row and row.get("windows_recommended") and plan_hours > 0)


def v3_curve(
    coord: Any,
    *,
    idx: ChartIndex,
    anchor: datetime,
    hourly: list[float],
    model: dict,
    band_fn,
    plan_windows: bool,
    plan_hours: float,
) -> dict[int, float]:
    """The production v3 predicted-indoor curve at ``anchor`` -> {epoch: temp_f} (hourly + :30 points)."""
    tz = idx.tz
    _set_ha_timezone(tz)
    entries = []
    a_local = anchor.astimezone(tz)
    for key, val in sorted(idx.pred_by_hour.items()):  # forecasts already logged earlier today (known at A)
        d = datetime.fromtimestamp(key, tz)
        if d.date() == a_local.date() and key < anchor.timestamp():
            entries.append({"datetime": d.isoformat(), "temperature": val})
    for h, val in enumerate(hourly):
        entries.append({"datetime": at(anchor, 3600 * h).astimezone(tz).isoformat(), "temperature": val})
    lo0, hi0 = band_fn(anchor.timestamp(), anchor.timestamp())
    comfort_heat = lo0 if lo0 is not None else float(getattr(coord, "DEFAULT_COMFORT_HEAT", 68))
    comfort_cool = hi0 if hi0 is not None else float(getattr(coord, "DEFAULT_COMFORT_COOL", 74))
    band_schedule = []
    for h in range(1, len(hourly)):
        t = at(anchor, 3600 * h)
        lo, hi = band_fn(anchor.timestamp(), t.timestamp())
        band_schedule.append(
            {
                "ts": t.astimezone(tz).isoformat(),
                "lower": lo if lo is not None else comfort_heat,
                "upper": hi if hi is not None else comfort_cool,
            }
        )
    modes = coord._compute_day_hvac_modes(entries, anchor, None)
    open_t = close_t = None
    if plan_windows:
        open_t = a_local.time()
        end = at(anchor, plan_hours * 3600).astimezone(tz)
        close_t = end.time() if end.date() == a_local.date() else dtime(23, 59, 59)
    classification = SimpleNamespace(
        hvac_mode=modes.get(a_local.date(), "off"),
        windows_recommended=plan_windows,
        window_open_time=open_t,
        window_close_time=close_t,
    )
    result = coord._build_predicted_indoor_future(
        entries,
        {"comfort_heat": comfort_heat, "comfort_cool": comfort_cool},
        anchor,
        current_indoor_temp=idx.last_at(CA, anchor.timestamp()),
        thermal_model=model,
        classification=classification,
        band_schedule=band_schedule,
    )
    out: dict[int, float] = {}
    for p in result:
        try:
            out[int(round(datetime.fromisoformat(p["ts"]).timestamp()))] = float(p["temp"])
        except KeyError, TypeError, ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# The walk-forward
# ---------------------------------------------------------------------------


def monday_midnight(dt: datetime) -> datetime:
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def plan_weeks(first: datetime, last: datetime, args: argparse.Namespace) -> list[datetime]:
    earliest = first + timedelta(days=args.min_train_days)
    w = monday_midnight(earliest)
    if w < earliest:
        w = monday_midnight(w + timedelta(days=7))
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=first.tzinfo)
        s = monday_midnight(since)
        w = max(w, s if s >= since else monday_midnight(s + timedelta(days=7)))
    weeks = []
    while w < last:
        weeks.append(w)
        w = monday_midnight(w + timedelta(days=7))
    if args.max_weeks:
        weeks = weeks[-args.max_weeks :]
    return weeks


def run_backtest(
    rows: list[dict],
    args: argparse.Namespace,
    *,
    tz: tzinfo,
    zone: str,
    learning_db: dict | None = None,
    archive_snapshots: list[dict] | None = None,
    log=lambda msg: None,
) -> dict:
    """Walk-forward one zone. Returns the zone report (stats, gate, points, weeks, run info)."""
    t_start = time.monotonic()
    pred = load_predictor()
    primary, nodes = select_sensors(rows, getattr(args, "primary", None))
    idx = ChartIndex(rows, tz, nodes)
    if not idx.t:
        raise SystemExit(f"ERROR: zone {zone!r}: chart_log has no usable rows")
    ca_xs = idx.series[CA][0]
    first = datetime.fromtimestamp(ca_xs[0] if ca_xs else idx.t[0], tz)
    last = idx.dts[-1].astimezone(tz)
    weeks = plan_weeks(first, last, args)
    forecasts = ForecastIndex(archive_snapshots, idx)
    snaps_sorted = archive_snapshots or []

    coord = None
    v3_models = V3Models(learning_db, args.v3_model)
    if not args.no_v3:
        try:
            coord = load_coordinator()
        except Exception as exc:  # noqa: BLE001
            log(f"  v3 replay disabled: coordinator import failed ({type(exc).__name__}: {exc})")
    v3_enabled = coord is not None and v3_models.available
    if coord is not None and not v3_models.available:
        log("  v3 replay disabled: no learning DB thermal model (pass --learning-db or use SSH)")

    records: list[dict] = []
    week_info: list[dict] = []
    src_counts = {"archive": 0, "chart_log": 0}
    skipped = {"no_reading": 0, "no_forecast": 0, "v4_none": 0, "v4_error": 0, "v3_error": 0}
    model = None
    last_fit_week = -999
    anchors_total = 0
    for wi, W in enumerate(weeks):
        W_ep = W.timestamp()
        info: dict[str, Any] = {"week_start": W.isoformat(), "fitted": False}
        t_week = time.monotonic()
        # ---- fit on data strictly before the week
        if model is None or (wi - last_fit_week) >= args.refit_every_weeks:
            lo_ep = W_ep - args.train_days * 86400 if args.train_days else -1e18
            train_rows = [r for t, r in zip(idx.t, idx.rows, strict=True) if lo_ep <= t < W_ep]
            snaps = [s for s in snaps_sorted if parse_ts(s["issued"], tz) < W]
            assert_no_leak(train_rows, W, "fit rows", key=lambda r: parse_ts(r["ts"], tz))
            assert_no_leak(snaps, W, "fit forecast snapshots", key=lambda s: parse_ts(s["issued"], tz))
            samples = pred.samples_from_chart_log(train_rows, tz=tz, archive_snapshots=snaps or None)
            kept = [s for s in samples if s.ts < W]
            info["dropped_future_samples"] = len(samples) - len(kept)
            assert_no_leak(kept, W, "fit samples", key=lambda s: s.ts)
            info["n_samples"] = len(kept)
            span_days = (kept[-1].ts - kept[0].ts).total_seconds() / 86400 if len(kept) > 1 else 0.0
            if span_days < args.min_train_days:
                info["skipped"] = f"only {span_days:.1f} days of training samples"
                week_info.append(info)
                continue
            try:
                model = pred.fit(kept, primary=primary, sensors=list(nodes), tz=tz, now=W, horizon_h=12)
                last_fit_week = wi
                info["fitted"] = True
            except LeakageError:
                raise
            except Exception as exc:  # noqa: BLE001
                info["fit_error"] = f"{type(exc).__name__}: {exc}"
                model = None
                week_info.append(info)
                log(f"  week {W.date()} fit failed: {info['fit_error']}")
                continue
        if model is None:
            week_info.append(info)
            continue
        info["confidence"] = getattr(model, "confidence", None)
        info["fitted_at"] = getattr(model, "fitted_at", None)
        info["train_fit"] = _jsonable(getattr(model, "fit", None))
        v3_model, v3_src = v3_models.for_week(W) if v3_enabled else ({}, "off")
        info["v3_model"] = v3_src

        w_end_ep = (W + timedelta(days=7)).timestamp()
        test_samples = sorted(
            pred.samples_from_chart_log(
                [r for t, r in zip(idx.t, idx.rows, strict=True) if W_ep - 12 * 3600 <= t < w_end_ep],
                tz=tz,
                archive_snapshots=None,
            ),
            key=lambda s: s.ts,
        )
        si = 0
        mass: float | None = None
        prev_ep: float | None = None
        sensors_model = list(getattr(model, "sensors", nodes))
        week_records = 0
        a_ep = W_ep
        while a_ep < w_end_ep and a_ep <= idx.t[-1] - 3600:
            A = datetime.fromtimestamp(a_ep, tz)
            a_ep += args.anchor_step_hours * 3600
            anchors_total += 1
            # mass state from samples strictly before the anchor (live: carried between cycles)
            while si < len(test_samples) and test_samples[si].ts.timestamp() < A.timestamp():
                s = test_samples[si]
                s_ep = s.ts.timestamp()
                if prev_ep is not None and s_ep - prev_ep > 7200:
                    mass = None
                try:
                    mass = pred.step_mass(model, mass_f=mass, readings=s.readings, sample=s)
                except Exception:  # noqa: BLE001
                    mass = None
                prev_ep = s_ep
                si += 1
            readings = {k: idx.last_at(k, A.timestamp()) for k in sensors_model}
            primary_reading = readings.get(sensors_model[0]) if sensors_model else None
            if primary_reading is None:
                skipped["no_reading"] += 1
                continue
            hourly, src = forecasts.hourly(A, HORIZON_H + 1)
            if hourly is None:
                skipped["no_forecast"] += 1
                continue
            src_counts[src] += 1
            plan_win = windows_plan(idx, A, args.window_plan_hours)

            def band_fn(anchor_ep: float, t_ep: float) -> tuple[float | None, float | None]:
                return idx.band_at(anchor_ep if args.setpoints == "hold" else t_ep)

            def build_inputs(actual: bool, A=A, hourly=hourly, plan_win=plan_win):
                out = []
                last_lo = last_hi = None
                for k in range(1, HORIZON_H * 3600 // STEP_S + 1):
                    t = at(A, STEP_S * k)
                    lo, hi = band_fn(A.timestamp(), t.timestamp())
                    lo = lo if lo is not None else last_lo
                    hi = hi if hi is not None else last_hi
                    last_lo, last_hi = lo, hi
                    if actual:
                        row = idx.row_at(t.timestamp(), 1800) or {}
                        outdoor = idx.value_at("outdoor", t.timestamp())
                        outdoor = outdoor if outdoor is not None else interp_hourly(hourly, STEP_S * k)
                        win = bool(row.get("windows_open"))
                        fan = bool(row.get("fan_running", row.get("fan")))
                    else:
                        outdoor = interp_hourly(hourly, STEP_S * k)
                        win = plan_win and STEP_S * k <= args.window_plan_hours * 3600
                        fan = False
                    out.append(
                        pred.V4Input(
                            ts=t, outdoor_f=outdoor, heat_sp_f=lo, cool_sp_f=hi, windows_open=win, fan_running=fan
                        )
                    )
                return out

            def run_v4(inputs, A=A, readings=readings, mass=mass, model=model):
                for inp in inputs:
                    if inp.ts <= A:
                        raise LeakageError("a predict() input is not in the future")
                res = pred.predict(model, now=A, readings=readings, mass_f=mass, inputs=inputs, tz=tz)
                if res is None:
                    return None
                return {s: {int(round(ts.timestamp())): float(v) for ts, v in pts} for s, pts in res.items()}

            v4 = v4a = None
            try:
                v4 = run_v4(build_inputs(False))
                if v4 is None:
                    skipped["v4_none"] += 1
                elif not args.no_diagnostic:
                    v4a = run_v4(build_inputs(True))
            except LeakageError:
                raise
            except Exception:  # noqa: BLE001
                skipped["v4_error"] += 1
                v4 = None
            v3c: dict[int, float] | None = None
            if v3_enabled:
                try:
                    v3c = v3_curve(
                        coord,
                        idx=idx,
                        anchor=A,
                        hourly=hourly,
                        model=v3_model,
                        band_fn=band_fn,
                        plan_windows=plan_win,
                        plan_hours=args.window_plan_hours,
                    )
                except Exception:  # noqa: BLE001
                    skipped["v3_error"] += 1
            for sensor in dict.fromkeys([CA, *sensors_model]):
                base = idx.last_at(sensor, A.timestamp())
                if base is None:
                    continue
                src_key = sensors_model[0] if sensor == CA else sensor
                for lead in LEADS_H:
                    T = at(A, lead * 3600)
                    t_ep = int(round(T.timestamp()))
                    actual = idx.value_at(sensor, T.timestamp())
                    if actual is None:
                        continue
                    records.append(
                        {
                            "sensor": sensor,
                            "lead": lead,
                            "anchor": A,
                            "target": T,
                            "season": season_of(A),
                            "actual": actual,
                            "flat": base,
                            "v4": v4.get(src_key, {}).get(t_ep) if v4 else None,
                            "v4a": v4a.get(src_key, {}).get(t_ep) if v4a else None,
                            "v3": v3c.get(t_ep) if (v3c is not None and sensor == CA) else None,
                            "logged": (
                                idx.value_at("_logged", T.timestamp(), 600) if (sensor == CA and lead == 4) else None
                            ),
                        }
                    )
                    week_records += 1
        info["records"] = week_records
        info["seconds"] = round(time.monotonic() - t_week, 1)
        week_info.append(info)
        log(
            f"  week {W.date()}  fit={'new' if info['fitted'] else 'kept'}  "
            f"conf={info['confidence']}  records={week_records}  {info['seconds']}s"
        )

    stats = aggregate(records, v3_enabled=v3_enabled)
    gate, gate_other, summary = build_gate(
        stats, zone, v3_enabled=v3_enabled, min_n=args.min_n, min_coverage=args.min_coverage
    )
    final = next((w for w in reversed(week_info) if w.get("confidence")), {})
    return {
        "schema_version": SCHEMA_VERSION,
        "zone": zone,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "unit": "F",
        "range": {"start": first.isoformat(), "end": last.isoformat()},
        "sensors": [
            {"id": CA, "name": "Indoor (as used by Climate Advisor)", "role": "line v3 predicts"},
            *[
                {"id": s, "name": s, "role": "thermostat's room reading" if i == 0 else "indoor sensor"}
                for i, s in enumerate(nodes)
                if s != CA
            ],
        ],
        "primary": primary,
        "model": {
            "confidence": final.get("confidence"),
            "fitted_at": final.get("fitted_at"),
            "span_days": (final.get("train_fit") or {}).get("span_days"),
            "weeks_fitted": sum(1 for w in week_info if w.get("fitted")),
        },
        "stats": stats,
        "gate": gate,
        "gate_sensors": gate_other,
        "gate_summary": summary,
        "weeks": week_info,
        "run": {
            "wall_seconds": round(time.monotonic() - t_start, 1),
            "weeks_planned": len(weeks),
            "anchors": anchors_total,
            "forecast_source_anchors": src_counts,
            "forecast_note": (
                "forecast archive used"
                if src_counts["archive"] and src_counts["archive"] >= src_counts["chart_log"]
                else "chart_log pred_outdoor (lead-0 forecast: absolute errors optimistic, comparison fair)"
            ),
            "skipped": skipped,
            "v3": {
                "enabled": v3_enabled,
                "model_mode": args.v3_model,
                "weeks_by_source": v3_models.used,
                "drift_check": v3_models.drift_note() if v3_enabled else None,
            },
            "inputs": {
                "setpoints": args.setpoints,
                "window_plan_hours": args.window_plan_hours,
                "train_days": args.train_days,
                "refit_every_weeks": args.refit_every_weeks,
                "anchor_step_hours": args.anchor_step_hours,
            },
        },
        "points": build_points(records),
    }


def _jsonable(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, default=str))
    except TypeError, ValueError:
        return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _mae_bias(pairs: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if not pairs:
        return None, None
    errs = [p - a for p, a in pairs]
    return sum(abs(e) for e in errs) / len(errs), sum(errs) / len(errs)


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def _season_stats(recs: list[dict], sensor: str, v3_enabled: bool) -> dict:
    need_v3 = v3_enabled and sensor == CA
    base = [r for r in recs if (not need_v3 or r["v3"] is not None)]
    common = [r for r in base if r["v4"] is not None]
    out: dict[str, Any] = {"n": len(common), "base_n": len(base)}
    out["coverage"] = _r(len(common) / len(base)) if base else None
    for name in ("v4", "v3", "flat"):
        mae, bias = _mae_bias([(r[name], r["actual"]) for r in common if r[name] is not None])
        out[f"{name}_mae"], out[f"{name}_bias"] = _r(mae), _r(bias)
    diag = [r for r in common if r["v4a"] is not None]
    mae, bias = _mae_bias([(r["v4a"], r["actual"]) for r in diag])
    out["v4_actual_inputs_mae"], out["v4_actual_inputs_bias"] = _r(mae), _r(bias)
    logged = [r for r in common if r["logged"] is not None]
    if logged:
        mae, bias = _mae_bias([(r["logged"], r["actual"]) for r in logged])
        out["logged_pred_indoor_mae"], out["logged_pred_indoor_bias"], out["logged_n"] = _r(mae), _r(bias), len(logged)
        v3l = [r for r in logged if r["v3"] is not None]
        mae, _ = _mae_bias([(r["v3"], r["actual"]) for r in v3l])
        out["v3_replay_mae_on_logged"] = _r(mae)
    return out


def aggregate(records: list[dict], *, v3_enabled: bool) -> dict:
    """stats[sensor]['4h'][season] = {...}; ``all`` spans every season."""
    groups: dict[tuple[str, int, str], list[dict]] = {}
    for r in records:
        for season in (r["season"], "all"):
            groups.setdefault((r["sensor"], r["lead"], season), []).append(r)
    stats: dict[str, dict] = {}
    for (sensor, lead, season), recs in sorted(groups.items()):
        stats.setdefault(sensor, {}).setdefault(f"{lead}h", {})[season] = _season_stats(recs, sensor, v3_enabled)
    return stats


def gate_row(
    zone: str,
    sensor: str,
    season: str,
    lead: int,
    st: dict | None,
    *,
    require_v3: bool,
    min_n: int,
    min_cov: float,
) -> dict:
    st = st or {}
    n = st.get("n") or 0
    v4, v3, flat, bias = st.get("v4_mae"), st.get("v3_mae"), st.get("flat_mae"), st.get("v4_bias")
    cov = st.get("coverage")
    scored = n >= min_n and v4 is not None and flat is not None and (not require_v3 or v3 is not None)
    reasons: list[str] = []
    if not scored:
        reasons.append(f"not scored (n={n}, need {min_n}{', v3 missing' if require_v3 and v3 is None else ''})")
    else:
        if require_v3 and not v4 < v3:
            reasons.append("v4 not better than v3")
        if not v4 < flat:
            reasons.append("v4 not better than no-change")
        if abs(bias) > BIAS_LIMIT_F:
            reasons.append(f"|bias| {abs(bias):.2f} > {BIAS_LIMIT_F}")
        if cov is not None and cov < min_cov:
            reasons.append(f"coverage {cov:.2f} < {min_cov}")
    return {
        "zone": zone,
        "sensor": sensor,
        "season": season,
        "lead": f"{lead}h",
        "v4": v4,
        "v3": v3,
        "flat": flat,
        "bias": bias,
        "n": n,
        "coverage": cov,
        "pass": scored and not reasons,
        "scored": scored,
        "reasons": reasons,
    }


def build_gate(stats: dict, zone: str, *, v3_enabled: bool, min_n: int, min_coverage: float) -> tuple[list, list, dict]:
    """DP-8 gate rows for ``_ca`` (v4 vs v3 vs flat), the other sensors (v4 vs flat), and the verdict."""

    def row(sensor: str, season: str, lead: int, require_v3: bool) -> dict:
        st = stats.get(sensor, {}).get(f"{lead}h", {}).get(season)
        return gate_row(zone, sensor, season, lead, st, require_v3=require_v3, min_n=min_n, min_cov=min_coverage)

    gate = [row(CA, s, ld, True) for s in GATE_SEASONS for ld in GATE_LEADS_H]
    others = [row(sn, s, ld, False) for sn in stats if sn != CA for s in GATE_SEASONS for ld in GATE_LEADS_H]
    passed = sum(1 for g in gate if g["pass"])
    summary = {
        "ready": passed == len(gate) and v3_enabled,
        "passed": passed,
        "scored": sum(1 for g in gate if g["scored"]),
        "total": len(gate),
        "other_sensors_failing": sum(1 for g in others if g["scored"] and not g["pass"]),
    }
    return gate, others, summary


def build_points(records: list[dict]) -> dict:
    """points[sensor]['4h'] = [{t, actual, v4, v3, flat}] in target-time order (hourly anchors)."""
    pts: dict[str, dict[str, list]] = {}
    for r in sorted(records, key=lambda r: (r["sensor"], r["lead"], r["target"])):
        p: dict[str, Any] = {
            "t": r["target"].isoformat(timespec="minutes"),
            "actual": round(r["actual"], 2),
            "flat": round(r["flat"], 2),
        }
        if r["v4"] is not None:
            p["v4"] = round(r["v4"], 2)
        if r["v3"] is not None:
            p["v3"] = round(r["v3"], 2)
        pts.setdefault(r["sensor"], {}).setdefault(f"{r['lead']}h", []).append(p)
    return pts


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _f(v: float | None, w: int = 6, nd: int = 2, sign: bool = False) -> str:
    if v is None:
        return "-".rjust(w)
    return f"{v:+.{nd}f}".rjust(w) if sign else f"{v:.{nd}f}".rjust(w)


def format_report(report: dict) -> str:
    z = report["zone"] or "default"
    run = report["run"]
    lines = [
        "",
        f"=== Zone {z}   {report['range']['start'][:10]} .. {report['range']['end'][:10]}   "
        f"{run['weeks_planned']} test weeks, {run['anchors']} anchors, {run['wall_seconds']} s ===",
        f"forecast: {run['forecast_note']} {run['forecast_source_anchors']}",
        f"v3 replay: {'production _build_predicted_indoor_future' if run['v3']['enabled'] else 'OFF'} "
        f"model={run['v3']['model_mode']} {run['v3']['weeks_by_source']}"
        + (f"  [{run['v3']['drift_check']}]" if run["v3"].get("drift_check") else ""),
        f"skipped anchors: {run['skipped']}",
        "error = predicted - actual (F); bias>0 = too warm. MAEs share the same anchors.",
        "",
        f"{'sensor':<34}{'season':<9}{'lead':>5}{'n':>6}{'flat':>7}{'v3':>7}{'v4':>7}{'v4bias':>8}"
        f"{'v4+act':>8}{'logged':>8}  gate",
    ]
    gate_idx = {(g["sensor"], g["season"], g["lead"]): g for g in report["gate"] + report["gate_sensors"]}
    for sensor, per_lead in report["stats"].items():
        for lead in (f"{h}h" for h in LEADS_H):
            for season in SEASONS:
                st = per_lead.get(lead, {}).get(season)
                if not st or (not st["n"] and not st["base_n"]):
                    continue
                g = gate_idx.get((sensor, season, lead))
                mark = ""
                if g is not None:
                    mark = "PASS" if g["pass"] else ("FAIL" if g["scored"] else "n/s")
                lines.append(
                    f"{sensor[:33]:<34}{season:<9}{lead:>5}{st['n']:>6}{_f(st['flat_mae'], 7)}{_f(st['v3_mae'], 7)}"
                    f"{_f(st['v4_mae'], 7)}{_f(st['v4_bias'], 8, sign=True)}{_f(st['v4_actual_inputs_mae'], 8)}"
                    f"{_f(st.get('logged_pred_indoor_mae'), 8)}  {mark}"
                )
    s = report["gate_summary"]
    lines += ["", f"DP-8 gate (_ca, heating+summer x 1/4/12 h): {s['passed']}/{s['total']} pass, {s['scored']} scored"]
    for g in report["gate"]:
        if not g["pass"]:
            lines.append(f"  {g['season']:<8}{g['lead']:>4}  " + "; ".join(g["reasons"] or ["failed"]))
    lines.append(f"ZONE {z}: {'READY - v4 may be shown' if s['ready'] else 'NOT READY - keep showing v3 only'}")
    return "\n".join(lines)


def write_json(path: str, reports: dict[str, dict], with_points: bool) -> None:
    out = {}
    for zone, rep in reports.items():
        out[zone or "default"] = {k: v for k, v in rep.items() if with_points or k != "points"}
    Path(path).write_text(json.dumps({"zones": out}, indent=1, default=str))


def build_publish_payload(report: dict) -> dict:
    """The per-zone file the REST endpoint merges: points / stats / gate (+ model, sensors)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "zone": report["zone"],
        "generated_at": report["generated_at"],
        "unit": "F",
        "resolution": "hourly",
        "range": report["range"],
        "sensors": report["sensors"],
        "model": report["model"],
        "points": report["points"],
        "stats": report["stats"],
        "gate": report["gate"],
        "gate_sensors": report["gate_sensors"],
        "gate_summary": report["gate_summary"],
    }


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


def load_config() -> dict:
    from ha_logs import load_config as _lc  # noqa: PLC0415

    return _lc()


def ssh_cmd(config: dict, remote_cmd: str) -> list[str]:
    from ha_logs import ssh_args, ssh_target  # noqa: PLC0415

    return [*ssh_args(config), ssh_target(config), remote_cmd]


def ssh_cat_json(config: dict, remote: str, *, required: bool) -> Any:
    result = subprocess.run(ssh_cmd(config, f"cat {shlex.quote(remote)}"), capture_output=True, timeout=180)
    if result.returncode != 0:
        if required:
            raise RuntimeError(f"SSH read of {remote} failed: {result.stderr.decode(errors='replace').strip()}")
        return None
    return json.loads(result.stdout)


def ssh_timezone(config: dict) -> str | None:
    try:
        data = ssh_cat_json(config, "/config/.storage/core.config", required=False)
        return (data or {}).get("data", {}).get("time_zone")
    except Exception:  # noqa: BLE001
        return None


def discover_zones(config: dict) -> list[str]:
    return discover_entry_ids(config, CHART_LOG_FILE, ssh_cmd)


def publish_remote(config: dict, entry_id: str, payload: dict) -> str:
    """Atomic write (temp + mv), mode 600, of the per-zone backtest file. Returns the remote path."""
    dest = remote_path(BACKTEST_FILE, entry_id)
    tmp = f"{dest}.tmp"
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    q_tmp, q_dest = shlex.quote(tmp), shlex.quote(dest)
    cmd = f"umask 077 && cat > {q_tmp} && chmod 600 {q_tmp} && mv {q_tmp} {q_dest}"
    result = subprocess.run(ssh_cmd(config, cmd), input=body, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"SSH write of {dest} failed: {result.stderr.decode(errors='replace').strip()}")
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chart-log", help="local chart_log JSON ({'entries': [...]}); omit to read over SSH")
    p.add_argument("--zone", help="config entry id (SSH mode: which zone; local mode: label)")
    p.add_argument("--all-zones", action="store_true", help="SSH: every entry-scoped chart log on the host")
    p.add_argument("--learning-db", help="local learning DB JSON (v3 thermal model); SSH mode fetches it")
    p.add_argument("--forecast-archive", help="local forecast archive JSON; SSH mode fetches it if present")
    p.add_argument("--no-archive", action="store_true", help="ignore the forecast archive (pred_outdoor only)")
    p.add_argument("--tz", help="IANA zone (default: HA core.config over SSH, TZ env, /etc/timezone, row offset)")
    p.add_argument(
        "--primary", help="primary sensor entity (default: the sensor tracking chart_log 'indoor', else _ca)"
    )
    p.add_argument("--since", help="YYYY-MM-DD: first test week on/after this date")
    p.add_argument("--max-weeks", type=int, default=0, help="only the newest N test weeks (quick runs)")
    p.add_argument("--train-days", type=int, default=60, help="training window before each week (0 = all history)")
    p.add_argument("--min-train-days", type=float, default=5.0, help="no test week without this much training data")
    p.add_argument("--refit-every-weeks", type=int, default=1)
    p.add_argument("--anchor-step-hours", type=float, default=1.0)
    p.add_argument("--setpoints", choices=("scheduled", "hold"), default="scheduled")
    p.add_argument("--window-plan-hours", type=float, default=4.0)
    p.add_argument("--v3-model", choices=("asof", "current"), default="asof")
    p.add_argument("--no-v3", action="store_true")
    p.add_argument("--no-diagnostic", action="store_true", help="skip the v4-with-actual-inputs variant")
    p.add_argument("--min-n", type=int, default=72, help="anchors needed before a gate row is scored")
    p.add_argument("--min-coverage", type=float, default=0.8, help="share of anchors v4 must answer")
    p.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    p.add_argument("--json-points", action="store_true", help="include the point series in --json")
    p.add_argument("--publish", action="store_true", help="write climate_advisor_v4_backtest_<entry>.json over SSH")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def _load_local_json(path: str) -> Any:
    return json.loads(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    t0 = time.monotonic()
    config: dict | None = None
    jobs: list[tuple[str, list[dict], dict | None, list[dict] | None, str | None]] = []

    if args.chart_log:
        data = _load_local_json(args.chart_log)
        rows = data.get("entries", []) if isinstance(data, dict) else data
        db = _load_local_json(args.learning_db) if args.learning_db else None
        snaps = None
        if args.forecast_archive and not args.no_archive:
            snaps = decode_archive(_load_local_json(args.forecast_archive))
        jobs.append((args.zone or "local", rows, db, snaps, None))
    else:
        config = load_config()
        zones = [args.zone] if args.zone else discover_zones(config)
        if not args.zone and not args.all_zones and len(zones) > 1:
            print(f"ERROR: multiple zones found: {', '.join(zones)}. Pass --zone or --all-zones.", file=sys.stderr)
            return 2
        if not zones:
            zones = [""]  # legacy unscoped files
        tz_name = args.tz or ssh_timezone(config)
        for z in zones:
            log(f"fetching zone {z or 'default'} over SSH ...")
            data = ssh_cat_json(config, remote_path(CHART_LOG_FILE, z), required=True)
            rows = data.get("entries", [])
            db = None if args.no_v3 else ssh_cat_json(config, remote_path(LEARNING_DB_FILE, z), required=False)
            snaps = None
            if not args.no_archive:
                raw = ssh_cat_json(config, remote_path(FORECAST_ARCHIVE_FILE, z), required=False)
                snaps = decode_archive(raw) if raw else None
                if raw is None:
                    log("  no forecast archive on the host: using chart_log pred_outdoor (lead-0 forecast)")
            jobs.append((z, rows, db, snaps, tz_name))

    reports: dict[str, dict] = {}
    for z, rows, db, snaps, tz_name in jobs:
        first_ts = next((r.get("ts") for r in rows if isinstance(r, dict) and r.get("ts")), None)
        tz = resolve_tz(args.tz or tz_name, first_ts)
        log(f"zone {z or 'default'}: {len(rows)} chart_log rows, tz={tz}, archive={'yes' if snaps else 'no'}")
        rep = run_backtest(rows, args, tz=tz, zone=z, learning_db=db, archive_snapshots=snaps, log=log)
        reports[z] = rep
        print(format_report(rep))
        if args.publish:
            if config is None:
                config = load_config()
            dest = publish_remote(config, z, build_publish_payload(rep))
            print(f"published {dest}")
    if args.json:
        write_json(args.json, reports, args.json_points)
        print(f"wrote {args.json}")
    print(f"\ntotal wall time: {time.monotonic() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
