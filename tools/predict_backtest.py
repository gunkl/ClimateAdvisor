#!/usr/bin/env python3
"""Walk-forward backtest of the v4 indoor predictor against v3 and "no change" (Issue #1230 R1, DP-8).

WHY: the owner sees v4's line on the dashboard only once this tool shows, over all recorded
history, that v4 predicts the room better than today's (v3) line and better than "nothing will
change", in the heating season AND in summer, with an average bias inside +-0.5 F. A house whose
v4 line is not proven keeps showing v3 only. (Since 0.9.4 it decides nothing; since Issue #1492 the
Prediction accuracy page's table is scored live over 30 days and the published file only fills
the chart's history from before live v4. The ``gate`` rows stay in this tool's report.)

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
  * v4   - ``predictor_v4.fit`` / ``predict``  (the integration module, pure Python)
  * v3   - v3 AS IT WAS (``--v3-model honest``, the default and the gate): at 4 h the logged
           chart_log ``pred_indoor`` (exactly the line the occupant saw) where present; at 1/12 h (and
           4 h where nothing was logged) the PRODUCTION ``coordinator._build_predicted_indoor_future``
           imported headless (HA stubs from tools/sim_harness/ha_stubs.py) replayed with the thermal
           model rebuilt by production's own ``LearningEngine`` from observations dated before the
           anchor's day. Anchors the retained observations cannot rebuild (fewer than
           ``--v3-asof-min-obs``) are EXCLUDED from the v3 comparison and its n, never replayed with
           today's model. ``--v3-model current`` (today's cache: hindsight, flatters v3) and ``asof``
           (rebuilt once per week) are diagnostics; the hindsight replay is always shown as ``v3cur``.
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
    true at A, then open over the times the engine planned that day (the learning DB daily record's
    ``window_open_time``/``window_close_time``; the live shadow reads the same plan from the classification)
    through ``v4_shadow.planned_window_span``, the ONE rule both share; no record for the day -> the
    first ``--window-plan-hours`` (default 4) from A. Both models get the same span (v3 through the
    classification's window times). Fan is never planned. A "v4 with actual inputs" variant (actual
    outdoor, windows, fan) is reported apart as a diagnostic, never in the gate.

SEASONS are by what the HVAC did that local day (owner decision, reversible), not by calendar month:
a day with any heating is "heating"; else a day with cooling, or the whole-house fan running
(fan_running with nat_vent_active, or an hvac "fan" action), is "summer" (the cooling/fan season);
else "shoulder" (reported, not gated). The label only groups scores; no model sees it.

v3 THERMAL MODEL: ``current`` is the honest default. The learning DB keeps only the newest ~200
observations (about 2 weeks) while its EWMA cache carries the whole history, so an as-of rebuild
cannot recreate the model v3 had in April..October (it would show an immature model and the
fallback ramp, which v3 never showed then). ``current`` gives v3 hindsight (parameters learned
after the week), which can only flatter v3: conservative for the claim "v4 beats v3". ``asof`` is
kept for DBs with long observation history.

COOLING SETPOINT: pre-September rows can have ``setpoint`` null while cooling; the cooling setpoint
is then taken from the band ceiling ``upper`` (heating: ``lower``) so the model can learn from and
predict summer cycles.

SCORING: error = predicted - actual (positive bias = too warm). Gate (DP-8) on the ``_ca`` line, per
season (heating / summer as defined above; shoulder reported apart), at 1/4/12 h:
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
import copy
import importlib
import importlib.util
import json
import math
import shlex
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path, PurePosixPath
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


def _load_pure_module(name: str) -> Any:
    """A stdlib-only integration module loaded by path (no Home Assistant, no package import)."""
    spec = importlib.util.spec_from_file_location(f"_ca_{name}_standalone", _PKG_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# One season rule for the backtest and the live scorecard (Issue #1492): ``hvac_season`` in the integration.
_HVAC_SEASON = _load_pure_module("hvac_season")
season_of_day = _HVAC_SEASON.season_of_day
day_seasons = _HVAC_SEASON.day_seasons


def prepare_rows(rows: list[dict]) -> list[dict]:
    """Copy-on-change: an active heating/cooling row with a null ``setpoint`` gets the band edge
    (``lower`` when heating, ``upper`` when cooling), so pre-September cooling has a setpoint."""
    out = []
    for r in rows:
        if isinstance(r, dict) and _num(r.get("setpoint")) is None:
            hv = str(r.get("hvac") or "").lower()
            edge = _num(r.get("upper")) if hv == "cooling" else _num(r.get("lower")) if hv == "heating" else None
            if edge is not None:
                r = {**r, "setpoint": edge}
        out.append(r)
    return out


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
    bad = [i for i in items if key(i).timestamp() >= cutoff.timestamp()]  # epoch: wall-clock compare breaks on DST
    if bad:
        raise LeakageError(f"{what}: {len(bad)} item(s) at/after the test-week start {cutoff.isoformat()}")


# ---------------------------------------------------------------------------
# Chart log index
# ---------------------------------------------------------------------------


class ChartIndex:
    """Sorted chart_log rows with the series the backtest reads (all internal F)."""

    def __init__(
        self,
        rows: list[dict],
        tz: tzinfo,
        sensors: list[str],
        primary: str | None = None,
        aliases: tuple[str, ...] = (),
        in_sleep: Any = None,
    ) -> None:
        # ``primary``: the live fit's primary node. Rows that lack it (older logs, the climate-attribute era) read
        # it through predictor_v4's shared row mapping, exactly as ``samples_from_chart_log`` does.
        self._primary = primary
        self._aliases = aliases
        self._in_sleep = in_sleep
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
                v = self._primary_raw(r) if (primary and key == primary and key != CA) else self._raw(key, r)
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

    def _primary_raw(self, r: dict) -> float | None:
        fn = getattr(_PREDICTOR, "_row_primary_value", None)
        if fn is None:
            return self._raw(self._primary or CA, r)
        return fn(r, self._primary, self._aliases, self._in_sleep, self.tz)

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

    def set_commanded(self, seasons: dict) -> None:
        """Alternative band for ``--setpoint-source commanded``: the logged thermostat ``setpoint`` replaces
        the band edge of the day's mode (heating day: lower; cooling/fan day: upper)."""
        self.band_cmd = []
        for (lo, hi), dt, r in zip(self.band, self.dts, self.rows, strict=True):
            sp = _num(r.get("setpoint"))
            season = seasons.get(dt.astimezone(self.tz).date())
            if sp is not None and season == "heating":
                lo = sp
            elif sp is not None and season == "summer":
                hi = sp
            self.band_cmd.append((lo, hi))

    def band_at(self, t: float, commanded: bool = False) -> tuple[float | None, float | None]:
        i = self._idx_at_or_before(t)
        if i < 0 or t - self.t[i] > 2 * 3600:
            return (None, None)
        return (self.band_cmd if commanded and hasattr(self, "band_cmd") else self.band)[i]

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
    if min_cov <= 0:
        good = sorted(counts)
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
    """``predictor_v4`` (integration module). Pure Python, loaded by path; else the stubbed package import."""
    global _PREDICTOR  # noqa: PLW0603
    if _PREDICTOR is not None:
        return _PREDICTOR
    path = _PKG_DIR / "predictor_v4.py"
    if not path.exists():
        raise SystemExit(f"ERROR: {path} not found - custom_components/climate_advisor/predictor_v4.py is required.")
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


def _learning_module() -> Any:
    _install_ha_stubs()
    return importlib.import_module("custom_components.climate_advisor.learning")


def prod_model_from_cache(cache: dict) -> dict:
    """The ``thermal_model`` dict production hands the engine: ``LearningEngine.get_thermal_model`` itself."""
    stub = SimpleNamespace(_state=SimpleNamespace(thermal_model_cache=copy.deepcopy(cache)))
    return _learning_module().LearningEngine.get_thermal_model(stub)


def prod_rebuild_cache(observations: list[dict]) -> dict | None:
    """Rebuild the v3 cache from these observations as production does: fold them (oldest first) through
    ``_update_thermal_model_cache`` (counts, solar, vent, swing), then set the three aged rates with production's
    ``aged_rates`` as of the newest observation's date (Issue #1512: the rates are no longer folded one by one)."""
    learning = _learning_module()
    engine = learning.LearningEngine
    stub = SimpleNamespace(_state=SimpleNamespace(thermal_model_cache=None))
    ordered = sorted(observations, key=lambda o: str(o.get("timestamp") or o.get("date")))
    for o in ordered:
        engine._update_thermal_model_cache(stub, o)
    cache = stub._state.thermal_model_cache
    if cache is None or not ordered:
        return cache
    try:
        as_of = date.fromisoformat(str(ordered[-1].get("date") or ordered[-1].get("timestamp"))[:10])
    except ValueError:
        return cache
    cache.update(learning.aged_rates(ordered, as_of, cache.get("rate_prior")))
    return cache


V3_ASOF_MIN_OBS = 30  # retained observations needed before an anchor for an honest as-of v3 rebuild


class V3Models:
    """The v3 thermal model per test week: the DB's current cache, or rebuilt as-of the week."""

    def __init__(self, db: dict | None, mode: str, min_obs: int = V3_ASOF_MIN_OBS) -> None:
        self.db = db or {}
        self.mode = mode
        self.min_obs = min_obs
        self._asof_cache: dict[str, dict | None] = {}
        self.cache = self.db.get("thermal_model_cache") or {}
        self.current = prod_model_from_cache(self.cache) if self.cache else None
        self.obs = [o for o in self.db.get("thermal_observations") or [] if isinstance(o, dict) and o.get("date")]
        self.used: dict[str, int] = {}

    @property
    def available(self) -> bool:
        return self.current is not None or bool(self.obs)

    def for_anchor(self, anchor: datetime) -> dict | None:
        """The v3 thermal model AS IT WAS at ``anchor`` (``--v3-model honest``): production's EWMA rebuilt from the
        retained observations dated before the anchor's day, cached per day. ``None`` when fewer than ``min_obs``
        retained observations precede the anchor (the DB keeps only ~200, about two weeks, so older anchors cannot
        be rebuilt) - the caller excludes those anchors; it never falls back to today's model."""
        day = anchor.date().isoformat()
        if day in self._asof_cache:
            return self._asof_cache[day]
        prior = [o for o in self.obs if str(o["date"])[:10] < day]
        model: dict | None = None
        if len(prior) >= self.min_obs:
            model = prod_model_from_cache(prod_rebuild_cache(prior) or {}) or None
        self._asof_cache[day] = model
        return model

    def for_week(self, week_start: datetime) -> tuple[dict, str]:
        if self.mode == "asof" and self.obs:
            cutoff = week_start.date().isoformat()
            prior = [o for o in self.obs if str(o["date"])[:10] < cutoff]
            if prior:
                self.used["asof"] = self.used.get("asof", 0) + 1
                return prod_model_from_cache(prod_rebuild_cache(prior) or {}), f"asof({len(prior)} obs)"
        self.used["current"] = self.used.get("current", 0) + 1
        return (self.current or {}), "current" if self.current else "none"

    def drift_note(self) -> str | None:
        """Sanity: the production-EWMA rebuild from every retained observation vs the DB's own cache."""
        if not (self.obs and self.cache):
            return None
        rebuilt = (prod_rebuild_cache(self.obs) or {}).get("k_passive")
        cur = self.cache.get("k_passive")
        if rebuilt is None or cur is None or cur == 0:
            return None
        rel = abs(rebuilt - cur) / abs(cur)
        dates = sorted(str(o["date"])[:10] for o in self.obs)
        return (
            f"obs rebuild k_passive={rebuilt:.4f} vs cache {cur:.4f} ({rel:.0%} apart; "
            f"{len(self.obs)} obs only cover {dates[0]}..{dates[-1]})"
        )


TOMORROW_PLAN_KNOWN_HOUR = 18.0  # local hour from which the next day's record counts as the plan known at the anchor


def windows_plan(idx: ChartIndex, anchor: datetime, plan_hours: float) -> bool:
    row = idx.row_at(anchor.timestamp())
    return bool(row and row.get("windows_recommended") and plan_hours > 0)


def _shadow_module() -> Any:
    """``v4_shadow``: the module that owns the window-plan rule the live shadow also uses (one shared builder)."""
    _install_ha_stubs()
    return importlib.import_module("custom_components.climate_advisor.v4_shadow")


def window_records(learning_db: dict | None) -> dict[str, tuple[dtime, dtime]]:
    """``{local date: (planned open, planned close)}`` from the learning DB's daily records: the times the engine's
    classification actually planned that day (``DailyRecord.window_open_time`` / ``window_close_time``)."""
    out: dict[str, tuple[dtime, dtime]] = {}
    for rec in (learning_db or {}).get("records") or []:
        try:
            o, c = rec.get("window_open_time"), rec.get("window_close_time")
            if o and c:
                out[str(rec["date"])[:10]] = (dtime.fromisoformat(o), dtime.fromisoformat(c))
        except KeyError, TypeError, ValueError:
            continue
    return out


def tomorrow_window_plan(
    anchor: datetime,
    args: argparse.Namespace,
    records: dict[str, tuple[dtime, dtime]],
    tz: Any,
) -> tuple[bool, dtime | None, dtime | None] | None:
    """Tomorrow's planned window times as KNOWN at ``anchor`` (Issue #1391), else ``None`` (unknown -> closed).

    No hindsight: the day record is written at day end, so the next day's record is used only for anchors at or after
    ``--tomorrow-plan-hour`` local (default 18, after the day's forecast refresh has classified tomorrow; 24 = never)
    and only with ``--window-plan-source record`` and a record holding both planned times (the engine sets them only
    when it recommended windows). Anchors earlier in the day, a missing record or the fallback source: tomorrow closed.
    """
    if getattr(args, "window_plan_source", "record") != "record":
        return None
    local = anchor.astimezone(tz)
    if local.hour + local.minute / 60.0 < getattr(args, "tomorrow_plan_hour", TOMORROW_PLAN_KNOWN_HOUR):
        return None
    open_t, close_t = records.get((local.date() + timedelta(days=1)).isoformat(), (None, None))
    if open_t is None or close_t is None:
        return None
    return (True, open_t, close_t)


def window_plan_span(
    idx: ChartIndex,
    anchor: datetime,
    args: argparse.Namespace,
    records: dict[str, tuple[dtime, dtime]],
) -> tuple[datetime, datetime] | None:
    """The window span both models predict under at ``anchor``: ``v4_shadow.planned_window_span`` (the live shadow's
    own rule). Recommended = the live flag at the anchor; times = the day's record when ``--window-plan-source record``
    (the default) and one exists, else the documented fallback of ``--window-plan-hours`` from the anchor."""
    recommended = windows_plan(idx, anchor, args.window_plan_hours)
    local = anchor.astimezone(idx.tz)
    open_t = close_t = None
    if getattr(args, "window_plan_source", "record") == "record":
        open_t, close_t = records.get(local.date().isoformat(), (None, None))
    return _shadow_module().planned_window_span(
        local, recommended, open_t, close_t, fallback_hours=args.window_plan_hours
    )


def v3_curve(
    coord: Any,
    *,
    idx: ChartIndex,
    anchor: datetime,
    hourly: list[float],
    model: dict,
    band_fn,
    span: tuple[datetime, datetime] | None,
    tomorrow_plan: tuple[bool, dtime | None, dtime | None] | None = None,
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
    if span is not None:  # v3 takes one time-of-day span on the anchor's day (clipped at midnight)
        s0, s1 = span[0].astimezone(tz), span[1].astimezone(tz)
        open_t = s0.time() if s0.date() == a_local.date() else dtime(0, 0)
        close_t = s1.time() if s1.date() == a_local.date() else dtime(23, 59, 59)
    classification = SimpleNamespace(
        hvac_mode=modes.get(a_local.date(), "off"),
        windows_recommended=span is not None,
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
        tomorrow_window=tomorrow_plan,  # known at the anchor only (tomorrow_window_plan), the rule v4 shares (#1391)
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


def infer_sleep(rows: list[dict], primary: str, tz: tzinfo) -> tuple[str | None, Any]:
    """(sleep sensor, ``in_sleep(local_dt)``) inferred from the log, since the backtest has no HA config.

    The comfort-facing ``indoor`` is the sleep sensor's reading inside the sleep window (Issue #964), so a sensor
    other than ``primary`` that EQUALS ``indoor`` while ``primary`` differs from it, on at least 100 rows, is the
    sleep sensor, and the local hours where that holds on most such rows are the window. ``(None, None)`` when
    there is no such sensor (the live shadow then follows the primary node).
    """
    if primary == CA:
        return None, None
    hits: dict[str, int] = {}
    per_hour: dict[str, dict[int, list[int]]] = {}
    for r in rows:
        sens = r.get("indoor_sensors")
        ind, prim = _num(r.get("indoor")), (_num(sens.get(primary)) if isinstance(sens, dict) else None)
        if ind is None or prim is None or not isinstance(sens, dict):
            continue
        try:
            hr = parse_ts(r.get("ts"), tz).astimezone(tz).hour
        except TypeError, ValueError:
            continue
        for k, v in sens.items():
            fv = _num(v)
            if k == primary or fv is None or abs(fv - prim) < 0.1:
                continue  # only rows where this sensor and the primary disagree can tell them apart
            ph = per_hour.setdefault(k, {}).setdefault(hr, [0, 0])
            ph[1] += 1
            if abs(fv - ind) < 0.05:
                ph[0] += 1
                hits[k] = hits.get(k, 0) + 1
    if not hits:
        return None, None
    node = max(hits, key=lambda k: hits[k])
    if hits[node] < 100:
        return None, None
    hours = {h for h, (a, b) in per_hour[node].items() if b >= 20 and a / b >= 0.5}
    if not hours:
        return None, None
    return node, (lambda dt, _h=frozenset(hours): dt.hour in _h)


def week_nodes(
    train_rows: list[dict],
    cand_primary: str,
    cand_nodes: list[str],
    forced: bool,
    *,
    aliases: tuple[str, ...] = (),
    tz: tzinfo = UTC,
    in_sleep: Any = None,
) -> tuple[str, list[str]]:
    """Primary + extra nodes usable for a fit on ``train_rows``: judged on the TRAINING rows only.

    Delegates to ``predictor_v4.resolve_nodes`` - the one rule the live shadow also uses (Issue #1230 R1b, M1):
    the entity primary counts every row that reads it directly, through a climate-attribute alias,
    ``indoor_primary`` or ``indoor``; at least 80 % coverage keeps it, else the model is a single ``_ca`` node.
    Extra entities need 50 % coverage.
    """
    return load_predictor().resolve_nodes(
        train_rows, primary=cand_primary, sensors=cand_nodes, aliases=aliases, tz=tz, forced=forced, in_sleep=in_sleep
    )


def fit_week_shared(
    pred: Any,
    train_rows: list[dict],
    snaps: list[dict],
    *,
    W: datetime,
    cand_primary: str,
    cand_nodes: list[str],
    sleep_node: str | None,
    aliases: tuple[str, ...],
    in_sleep: Any,
    tz: tzinfo,
    min_train_days: float,
    info: dict[str, Any],
) -> tuple[Any, str, list[str]] | None:
    """One week's fit through ``predictor_v4.choose_control_model`` - the live fit's own rule and gate (Issue #1432):
    A (the stitched ``_ca`` reading) or B (the primary entity + the inferred sleep sensor as a node), B only once both
    have 3+ days of their own readings and its ``_ca`` replay on the training rows is no worse than A's. Display-only
    nodes are added after, the same way the live fit adds them. ``(model, primary, nodes)``, or ``None`` when the
    week is skipped (too little history: ``info["skipped"]``) or the fit failed (``info["fit_error"]``)."""
    W_ep = W.timestamp()

    def no_leak(smp: list[Any]) -> list[Any]:
        kept = [x for x in smp if x.ts.timestamp() < W_ep]
        assert_no_leak(kept, W, "fit samples", key=lambda x: x.ts)
        return kept

    pre = no_leak(
        pred.samples_from_chart_log(
            train_rows, tz=tz, archive_snapshots=snaps or None, primary_aliases=aliases, in_sleep=in_sleep
        )
    )
    span_days = (pre[-1].ts - pre[0].ts).total_seconds() / 86400 if len(pre) > 1 else 0.0
    if span_days < min_train_days:
        info["skipped"] = f"only {span_days:.1f} days of training samples"
        return None
    try:
        chosen = pred.choose_control_model(
            train_rows,
            tz=tz,
            now=W,
            primary=cand_primary,
            sensors=list(cand_nodes),
            sleep_sensor=sleep_node,
            aliases=aliases,
            in_sleep=in_sleep,
            archive_snapshots=snaps or None,
            horizon_h=12,
            sample_filter=no_leak,
        )
        model, primary, nodes = chosen.model, chosen.primary, list(chosen.nodes)
        info["primary"], info["nodes"] = primary, list(nodes)
        info["node_choice"] = _jsonable((model.fit or {}).get("node_choice"))
        info["n_samples"] = len(chosen.samples)
        disp_fn = getattr(pred, "resolve_display_nodes", None)
        display = (
            list(disp_fn(train_rows, nodes=list(nodes), primary=cand_primary, sensors=list(cand_nodes), tz=tz))
            if disp_fn is not None
            else []
        )
        info["display_nodes"] = display
        if display and model.confidence != "none":
            model = pred.with_display_nodes(model, chosen.samples, display_sensors=display, tz=tz)
    except LeakageError:
        raise
    except Exception as exc:  # noqa: BLE001
        info["fit_error"] = f"{type(exc).__name__}: {exc}"
        return None
    return model, primary, nodes


def grid_steps(anchor: datetime, count: int) -> list[datetime]:
    """The predictor's own DST-safe UTC-stepped grid when it has one, else the same by epoch."""
    fn = getattr(_PREDICTOR, "grid_steps", None)
    return fn(anchor, count) if fn else [at(anchor, STEP_S * k) for k in range(1, count + 1)]


def _v4_value(v4, src_key, sensor, t_ep, T, sleep_node, in_sleep, sensors_model, tz):
    """The v4 value scored for ``sensor`` at ``t_ep``. ``_ca`` is the reading CA used: the sleep node's prediction
    inside the sleep window when that node was modelled (Issue #1230 R1b, M3), else the primary node's.

    Issue #1432 (the live ``ca_line`` guard): with a room node as the primary, a sleep hour the sleep node does not
    answer (not modelled, or no value) is not scored at all - never from the thermostat's room."""
    if not v4:
        return None
    if sensor == CA and sleep_node and in_sleep is not None and in_sleep(T.astimezone(tz)):
        v = v4.get(sleep_node, {}).get(t_ep) if sleep_node in sensors_model else None
        if v is not None:
            return v
        if src_key != CA:
            return None
    return v4.get(src_key, {}).get(t_ep)


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
    rows = prepare_rows(rows)
    cand_primary, cand_nodes = select_sensors(rows, getattr(args, "primary", None), min_cov=0.0)
    aliases = tuple(sorted({k for r in rows for k in (r.get("indoor_sensors") or {}) if str(k).startswith("climate.")}))
    sleep_node, in_sleep = infer_sleep(rows, cand_primary, tz)
    idx = ChartIndex(rows, tz, cand_nodes, cand_primary, aliases, in_sleep)
    seasons_by_day = day_seasons(rows, tz)
    idx.set_commanded(seasons_by_day)
    commanded = getattr(args, "setpoint_source", "band") == "commanded"
    primary, nodes = cand_primary, cand_nodes
    seen_nodes: dict[str, None] = {}
    if not idx.t:
        raise SystemExit(f"ERROR: zone {zone!r}: chart_log has no usable rows")
    ca_xs = idx.series[CA][0]
    first = datetime.fromtimestamp(ca_xs[0] if ca_xs else idx.t[0], tz)
    last = idx.dts[-1].astimezone(tz)
    weeks = plan_weeks(first, last, args)
    forecasts = ForecastIndex(archive_snapshots, idx)
    win_records = window_records(learning_db)
    snaps_sorted = archive_snapshots or []

    coord = None
    v3_models = V3Models(learning_db, args.v3_model, getattr(args, "v3_asof_min_obs", V3_ASOF_MIN_OBS))
    honest = args.v3_model == "honest"
    asof_unavailable = 0
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
    skipped = {"no_reading": 0, "no_forecast": 0, "no_setpoint": 0, "v4_none": 0, "v4_error": 0, "v3_error": 0}
    hygiene_by_season: dict[str, int] = {}
    hygiene_on = not getattr(args, "include_unknown_setpoint", False)
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
            snaps = [s for s in snaps_sorted if parse_ts(s["issued"], tz).timestamp() < W_ep]
            assert_no_leak(train_rows, W, "fit rows", key=lambda r: parse_ts(r["ts"], tz))
            assert_no_leak(snaps, W, "fit forecast snapshots", key=lambda s: parse_ts(s["issued"], tz))
            choose = None if getattr(args, "primary", None) else getattr(pred, "choose_control_model", None)
            if choose is not None:
                # Issue #1432: the live fit's own choice (A stitched / B per room, with the backtest gate), so the
                # accuracy page scores the model control would have read that week.
                fitted = fit_week_shared(
                    pred,
                    train_rows,
                    snaps,
                    W=W,
                    cand_primary=cand_primary,
                    cand_nodes=cand_nodes,
                    sleep_node=sleep_node,
                    aliases=aliases,
                    in_sleep=in_sleep,
                    tz=tz,
                    min_train_days=args.min_train_days,
                    info=info,
                )
                if fitted is None:
                    if info.get("fit_error"):
                        model = None
                        log(f"  week {W.date()} fit failed: {info['fit_error']}")
                    week_info.append(info)
                    continue
                model, primary, nodes = fitted
                seen_nodes.update(dict.fromkeys(nodes))
                seen_nodes.update(dict.fromkeys(getattr(model, "display_nodes", None) or {}))
                last_fit_week = wi
                info["fitted"] = True
            else:
                primary, nodes = week_nodes(
                    train_rows,
                    cand_primary,
                    cand_nodes,
                    bool(getattr(args, "primary", None)),
                    aliases=aliases,
                    tz=tz,
                    in_sleep=in_sleep,
                )
                info["primary"], info["nodes"] = primary, list(nodes)
                # Issue #1422: the live fit's display-only nodes (3+ days of a sensor's OWN history), same shared rule.
                disp_fn = getattr(pred, "resolve_display_nodes", None)
                display = (
                    list(disp_fn(train_rows, nodes=list(nodes), primary=cand_primary, sensors=list(cand_nodes), tz=tz))
                    if disp_fn is not None
                    else []
                )
                info["display_nodes"] = display
                seen_nodes.update(dict.fromkeys(nodes))
                samples = pred.samples_from_chart_log(
                    train_rows,
                    tz=tz,
                    archive_snapshots=snaps or None,
                    primary=None if primary == CA else primary,
                    primary_aliases=aliases,
                    in_sleep=in_sleep,
                )
                kept = [s for s in samples if s.ts.timestamp() < W_ep]
                info["dropped_future_samples"] = len(samples) - len(kept)
                assert_no_leak(kept, W, "fit samples", key=lambda s: s.ts)
                info["n_samples"] = len(kept)
                span_days = (kept[-1].ts - kept[0].ts).total_seconds() / 86400 if len(kept) > 1 else 0.0
                if span_days < args.min_train_days:
                    info["skipped"] = f"only {span_days:.1f} days of training samples"
                    week_info.append(info)
                    continue
                try:
                    disp_kw = {"display_sensors": display} if display else {}
                    model = pred.fit(kept, primary=primary, sensors=list(nodes), tz=tz, now=W, horizon_h=12, **disp_kw)
                    seen_nodes.update(dict.fromkeys(getattr(model, "display_nodes", None) or {}))
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
                primary=None if primary == CA else primary,
                primary_aliases=aliases,
                in_sleep=in_sleep,
            ),
            key=lambda s: s.ts,
        )
        si = 0
        mass: float | None = None
        prev_ep: float | None = None
        sensors_model = list(getattr(model, "sensors", nodes))
        # display-only nodes are predicted and scored as their own lines; ``_ca`` and the primary never read them
        display_model = [s for s in (getattr(model, "display_nodes", None) or {}) if s not in sensors_model]
        week_records = 0
        a_ep = W_ep
        while a_ep < w_end_ep and a_ep <= idx.t[-1] - 3600:
            A = datetime.fromtimestamp(a_ep, tz)
            a_ep += args.anchor_step_hours * 3600
            anchors_total += 1
            # hidden-mass filter: one step_mass() per 15-min grid step up to and including the anchor's
            # own sample (the fit's anchor state includes the anchor reading); a non-contiguous step
            # restarts the filter at the reading
            while si < len(test_samples) and test_samples[si].ts.timestamp() <= A.timestamp():
                s = test_samples[si]
                s_ep = s.ts.timestamp()
                if prev_ep is not None and s_ep - prev_ep != STEP_S:
                    mass = None
                try:
                    mass = pred.step_mass(model, mass_f=mass, readings=s.readings, sample=s)
                except Exception:  # noqa: BLE001
                    mass = None
                prev_ep = s_ep
                si += 1
            if hygiene_on:
                # Data hygiene (Issue #1230 R1b): an anchor whose setpoint in force is unknown (no logged setpoint, no
                # target band) cannot be predicted or judged honestly by ANY model, so it leaves v3, v4 and "no
                # change" alike. A band could be reconstructed only from later rows, which would be hindsight.
                lo_a, hi_a = idx.band_at(A.timestamp(), commanded)
                if lo_a is None and hi_a is None:
                    skipped["no_setpoint"] += 1
                    sea = seasons_by_day.get(A.astimezone(tz).date(), "shoulder")
                    hygiene_by_season[sea] = hygiene_by_season.get(sea, 0) + 1
                    continue
            readings = {k: idx.last_at(k, A.timestamp()) for k in [*sensors_model, *display_model]}
            primary_reading = readings.get(sensors_model[0]) if sensors_model else None
            if primary_reading is None:
                skipped["no_reading"] += 1
                continue
            hourly, src = forecasts.hourly(A, HORIZON_H + 1)
            if hourly is None:
                skipped["no_forecast"] += 1
                continue
            src_counts[src] += 1
            plan_span = window_plan_span(idx, A, args, win_records)
            tomorrow_plan = tomorrow_window_plan(A, args, win_records, tz)
            # v4's window input: today's span plus tomorrow's when known at A (Issue #1391); v3 gets the same
            # tomorrow plan through v3_curve(tomorrow_plan=...).
            v4_spans = tuple(
                sp
                for sp in (
                    plan_span,
                    _shadow_module().planned_window_span(A.astimezone(tz), *tomorrow_plan, day_offset=1)
                    if tomorrow_plan
                    else None,
                )
                if sp is not None
            )

            def band_fn(anchor_ep: float, t_ep: float) -> tuple[float | None, float | None]:
                return idx.band_at(anchor_ep if args.setpoints == "hold" else t_ep, commanded)

            def build_inputs(actual: bool, A=A, hourly=hourly, plan_span=v4_spans):
                out = []
                last_lo = last_hi = None
                steps = grid_steps(A, HORIZON_H * 3600 // STEP_S)
                for k, t in enumerate(steps, start=1):
                    lo, hi = band_fn(A.timestamp(), t.timestamp())
                    lo, hi = pred.plan_setpoints(lo, hi, (last_lo, last_hi))  # the rule the live shadow shares
                    last_lo, last_hi = lo, hi
                    if actual:
                        row = idx.row_at(t.timestamp(), 1800) or {}
                        outdoor = idx.value_at("outdoor", t.timestamp())
                        outdoor = outdoor if outdoor is not None else interp_hourly(hourly, STEP_S * k)
                        win = bool(row.get("windows_open"))
                        fan = bool(row.get("fan_running", row.get("fan")))
                    else:
                        outdoor = interp_hourly(hourly, STEP_S * k)
                        win = _shadow_module().window_open_at(plan_span, t)
                        fan = False
                    out.append(
                        pred.V4Input(
                            ts=t, outdoor_f=outdoor, heat_sp_f=lo, cool_sp_f=hi, windows_open=win, fan_running=fan
                        )
                    )
                return out

            run_state = pred.run_state_at(test_samples, si - 1) if si > 0 else None
            act_now = None if run_state is None else ("heating" if run_state[0] == 1 else "cooling")
            run_min = None if run_state is None else run_state[1]

            def run_v4(
                inputs,
                prior=True,
                act_now=act_now,
                run_min=run_min,
                A=A,
                readings=readings,
                mass=mass,
                model=model,
                hourly=hourly,
                display=bool(display_model),
            ):
                for inp in inputs:
                    if inp.ts.timestamp() <= A.timestamp():
                        raise LeakageError("a predict() input is not in the future")
                res = pred.predict(
                    model,
                    now=A,
                    readings=readings,
                    mass_f=mass,
                    inputs=inputs,
                    tz=tz,
                    outdoor_now_f=idx.last_at("outdoor", A.timestamp()),
                    forecast_now_f=hourly[0],
                    hvac_action_now=act_now,
                    run_elapsed_min=run_min,
                    openness_prior=prior,
                    **({"display_nodes": True} if display else {}),
                )
                if res is None:
                    return None
                return {s: {int(round(ts.timestamp())): float(v) for ts, v in pts} for s, pts in res.items()}

            v4 = v4a = None
            try:
                v4 = run_v4(build_inputs(False))
                if v4 is None:
                    skipped["v4_none"] += 1
                elif not args.no_diagnostic:
                    v4a = run_v4(build_inputs(True), prior=False)
            except LeakageError:
                raise
            except Exception:  # noqa: BLE001
                skipped["v4_error"] += 1
                v4 = None
            v3c: dict[int, float] | None = None
            v3c_asof: dict[int, float] | None = None
            asof_model = v3_models.for_anchor(A) if (v3_enabled and honest) else None
            if v3_enabled and honest and asof_model is None:
                asof_unavailable += 1
            if v3_enabled and asof_model is not None:
                try:
                    v3c_asof = v3_curve(
                        coord,
                        idx=idx,
                        anchor=A,
                        hourly=hourly,
                        model=asof_model,
                        band_fn=band_fn,
                        span=plan_span,
                        tomorrow_plan=tomorrow_plan,
                    )
                except Exception:  # noqa: BLE001
                    skipped["v3_error"] += 1
            if v3_enabled:
                try:
                    v3c = v3_curve(
                        coord,
                        idx=idx,
                        anchor=A,
                        hourly=hourly,
                        model=v3_model,
                        band_fn=band_fn,
                        span=plan_span,
                        tomorrow_plan=tomorrow_plan,
                    )
                except Exception:  # noqa: BLE001
                    skipped["v3_error"] += 1
            for sensor in dict.fromkeys([CA, *sensors_model, *display_model]):
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
                    logged = idx.value_at("_logged", T.timestamp(), 600) if (sensor == CA and lead == 4) else None
                    v3_hind = v3c.get(t_ep) if (v3c is not None and sensor == CA) else None
                    v3_unavail = False
                    if honest and sensor == CA:
                        # v3 as it was: the line the occupant saw at 4 h, else the as-of replay, else excluded
                        if logged is not None:
                            v3_val = logged
                        elif v3c_asof is not None:
                            v3_val = v3c_asof.get(t_ep)
                        else:
                            v3_val = None
                        v3_unavail = v3_val is None
                    else:
                        v3_val = v3_hind
                    records.append(
                        {
                            "sensor": sensor,
                            "lead": lead,
                            "anchor": A,
                            "target": T,
                            "season": seasons_by_day.get(A.astimezone(tz).date(), "shoulder"),
                            "actual": actual,
                            "flat": base,
                            "v4": _v4_value(v4, src_key, sensor, t_ep, T, sleep_node, in_sleep, sensors_model, tz),
                            "v4a": v4a.get(src_key, {}).get(t_ep) if v4a else None,
                            "v3": v3_val,
                            "v3cur": v3_hind,
                            "v3_unavail": v3_unavail,
                            "logged": logged,
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
                for i, s in enumerate(seen_nodes or nodes)
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
            "setpoint_hygiene": {
                "enabled": hygiene_on,
                "excluded_anchors": skipped["no_setpoint"],
                "by_season": hygiene_by_season,
            },
            "v3": {
                "enabled": v3_enabled,
                "model_mode": args.v3_model,
                "weeks_by_source": v3_models.used,
                "asof_unavailable_anchors": asof_unavailable,
                "asof_min_obs": v3_models.min_obs,
                "drift_check": v3_models.drift_note() if v3_enabled else None,
            },
            "inputs": {
                "setpoints": args.setpoints,
                "setpoint_source": getattr(args, "setpoint_source", "band"),
                "window_plan_hours": args.window_plan_hours,
                "window_plan_source": getattr(args, "window_plan_source", "record"),
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
    unavailable = sum(1 for r in recs if r.get("v3_unavail"))
    common = [r for r in base if r["v4"] is not None]
    out: dict[str, Any] = {"n": len(common), "base_n": len(base), "v3_unavailable_n": unavailable}
    out["coverage"] = _r(len(common) / len(base)) if base else None
    for name in ("v4", "v3", "flat"):
        mae, bias = _mae_bias([(r[name], r["actual"]) for r in common if r[name] is not None])
        out[f"{name}_mae"], out[f"{name}_bias"] = _r(mae), _r(bias)
    cur = [r for r in common if r.get("v3cur") is not None]
    mae, bias = _mae_bias([(r["v3cur"], r["actual"]) for r in cur])
    out["v3_current_mae"], out["v3_current_bias"], out["v3_current_n"] = _r(mae), _r(bias), len(cur)
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
        if not (st.get("base_n") or 0):
            reasons.append("no anchors in this season (HVAC never ran that way in the recorded history)")
        else:
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
        (
            f"setpoint hygiene: {run['setpoint_hygiene']['excluded_anchors']} anchors with no known setpoint excluded "
            f"for every model {run['setpoint_hygiene']['by_season']}"
            if run.get("setpoint_hygiene", {}).get("enabled")
            else "setpoint hygiene: OFF (unknown-setpoint anchors are scored)"
        ),
        "error = predicted - actual (F); bias>0 = too warm. MAEs share the same anchors.",
        "seasons by the day's HVAC activity: heating = any heating, summer = cooling or whole-house fan, "
        "shoulder = neither",
        "",
        f"{'sensor':<34}{'season':<9}{'lead':>5}{'n':>6}{'flat':>7}{'v3':>7}{'v4':>7}{'v4bias':>8}"
        f"{'v4+act':>8}{'logged':>8}{'v3cur':>7}{'v3ex':>7}  gate",
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
                    f"{_f(st.get('logged_pred_indoor_mae'), 8)}{_f(st.get('v3_current_mae'), 7)}"
                    f"{str(st.get('v3_unavailable_n') or '-').rjust(7)}  {mark}"
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


# Batched SSH (Issue #1400): ``tools/deploy.py`` runs this tool right after a deploy, which already used 3 of the few
# connections the HA SSH add-on allows in a short window (docs/SSH-SETUP.md). ``--batch-ssh`` reads every input in ONE
# connection and publishes every zone's file in ONE more, instead of 2 + 3 per zone + 1 per zone.
_BATCH_ZONE = "___CA_ZONE "
_BATCH_FILE = "___CA_FILE "
_BATCH_TZ = "___CA_TZ"
_BATCH_KINDS = {"chart": CHART_LOG_FILE, "learning": LEARNING_DB_FILE, "archive": FORECAST_ARCHIVE_FILE}


def batch_fetch_script(zone: str | None) -> str:
    """One remote shell script that prints every zone's inputs (base64, marker lines) and the HA time zone.

    Zones are the entry-scoped chart logs (``zone`` limits it to one); with none, the legacy unscoped files.
    """

    def files_for(entry_expr: str, scoped: bool) -> str:
        parts = []
        for kind, base in _BATCH_KINDS.items():
            stem, ext = base.rsplit(".", 1)
            p = f"/config/{stem}_{entry_expr}.{ext}" if scoped else f"/config/{base}"
            parts.append(f'echo "{_BATCH_FILE}{kind}"; if [ -f "{p}" ]; then base64 < "{p}" | tr -d "\\n"; fi; echo')
        return "; ".join(parts)

    stem, ext = CHART_LOG_FILE.rsplit(".", 1)
    pattern = f"/config/{stem}_{shlex.quote(zone)}.{ext}" if zone else f"/config/{stem}_*.{ext}"
    return (
        "found=0; "
        f'for f in {pattern}; do [ -f "$f" ] || continue; found=1; '
        f'e=${{f#/config/{stem}_}}; e=${{e%.{ext}}}; echo "{_BATCH_ZONE}$e"; {files_for("$e", True)}; done; '
        f'if [ "$found" = 0 ] && [ -f /config/{CHART_LOG_FILE} ]; then '
        f'echo "{_BATCH_ZONE}"; {files_for("", False)}; fi; '
        f'echo "{_BATCH_TZ}"; if [ -f /config/.storage/core.config ]; then '
        'base64 < /config/.storage/core.config | tr -d "\\n"; fi; echo'
    )


def parse_batch_output(text: str) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """``(tz_name, {zone: {"chart"|"learning"|"archive": parsed JSON or None}})`` from ``batch_fetch_script``."""
    import base64  # noqa: PLC0415

    zones: dict[str, dict[str, Any]] = {}
    tz_name: str | None = None
    zone: str | None = None
    kind: str | None = None
    in_tz = False

    def decode(blob: str) -> Any:
        blob = blob.strip()
        if not blob:
            return None
        try:
            return json.loads(base64.b64decode(blob))
        except ValueError:
            return None

    for line in text.splitlines():
        if line.startswith(_BATCH_ZONE) or line == _BATCH_ZONE.strip():
            zone, kind, in_tz = line[len(_BATCH_ZONE) :].strip(), None, False
            zones[zone] = {k: None for k in _BATCH_KINDS}
        elif line.startswith(_BATCH_FILE):
            kind = line[len(_BATCH_FILE) :].strip()
        elif line == _BATCH_TZ:
            in_tz, kind = True, None
        elif in_tz:
            cfg = decode(line)
            tz_name = (cfg or {}).get("data", {}).get("time_zone") if isinstance(cfg, dict) else None
            in_tz = False
        elif zone is not None and kind in _BATCH_KINDS:
            zones[zone][kind] = decode(line)
            kind = None
    return tz_name, zones


def batch_fetch(config: dict, zone: str | None) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Every zone's inputs and the HA time zone over ONE SSH connection."""
    result = subprocess.run(ssh_cmd(config, batch_fetch_script(zone)), capture_output=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"SSH batch read failed: {result.stderr.decode(errors='replace').strip()}")
    return parse_batch_output(result.stdout.decode(errors="replace"))


def publish_remote_batch(config: dict, payloads: dict[str, dict]) -> list[str]:
    """Every zone's backtest file over ONE SSH connection: a tar on stdin, unpacked beside the targets, then each file
    ``chmod 600`` and ``mv``-ed into place (same filesystem, so each replace is atomic). Returns the remote paths."""
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    buf = io.BytesIO()
    dests = []
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry_id, payload in payloads.items():
            dest = remote_path(BACKTEST_FILE, entry_id)
            dests.append(dest)
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
            info = tarfile.TarInfo(PurePosixPath(dest).name)
            info.size, info.mode = len(body), 0o600
            tar.addfile(info, io.BytesIO(body))
    stage = "/config/.ca_backtest_publish"
    cmd = (
        f"umask 077 && rm -rf {stage} && mkdir -p {stage} && tar xf - -C {stage} && "
        f'for f in {stage}/*.json; do chmod 600 "$f" && mv "$f" /config/; done && rmdir {stage}'
    )
    result = subprocess.run(ssh_cmd(config, cmd), input=buf.getvalue(), capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"SSH batch write failed: {result.stderr.decode(errors='replace').strip()}")
    return dests


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chart-log", help="local chart_log JSON ({'entries': [...]}); omit to read over SSH")
    p.add_argument(
        "--local-dir",
        help="directory of copied files: every climate_advisor_chart_log_<entry>.json (+ the matching learning DB "
        "and forecast archive) is one zone",
    )
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
    p.add_argument("--train-days", type=int, default=30, help="training window before each week (0 = all history)")
    p.add_argument("--min-train-days", type=float, default=5.0, help="no test week without this much training data")
    p.add_argument("--refit-every-weeks", type=int, default=1)
    p.add_argument("--anchor-step-hours", type=float, default=1.0)
    p.add_argument("--setpoints", choices=("scheduled", "hold"), default="scheduled")
    p.add_argument(
        "--setpoint-source",
        choices=("band", "commanded"),
        default="band",
        help="band: the plan's lower/upper (what the live shadow knows); commanded: the logged thermostat "
        "setpoint of the day's mode (diagnostic - the commanded value is typically 1-2 F above the band floor)",
    )
    p.add_argument(
        "--window-plan-hours",
        type=float,
        default=4.0,
        help="fallback span: windows recommended but no planned times for the day -> open this long from the anchor "
        "(0 = never plan windows)",
    )
    p.add_argument(
        "--window-plan-source",
        choices=("record", "fallback"),
        default="record",
        help="record (default, same rule as the live shadow): the windows are open over the times the engine planned "
        "that day (learning DB daily record window_open_time/window_close_time); fallback: always the first "
        "--window-plan-hours from the anchor (the pre-R6 backtest rule)",
    )
    p.add_argument(
        "--tomorrow-plan-hour",
        type=float,
        default=TOMORROW_PLAN_KNOWN_HOUR,
        help="local hour from which the NEXT day's planned window times (its learning DB record) count as known at the "
        "anchor and open v4's windows over tomorrow's hours (Issue #1391); earlier anchors keep tomorrow closed "
        "(no hindsight). 24 = never",
    )
    p.add_argument(
        "--v3-model",
        choices=("honest", "asof", "current"),
        default="honest",
        help="honest (default, the gate): v3 as it was - the logged 4 h pred_indoor where present, else production's "
        "model rebuilt from observations dated before the anchor; anchors that cannot be rebuilt are EXCLUDED from "
        "the v3 comparison (v3ex column). current: today's learned model (hindsight, flatters v3; always shown as "
        "the v3cur column). asof: rebuilt once per test week",
    )
    p.add_argument(
        "--v3-asof-min-obs",
        type=int,
        default=V3_ASOF_MIN_OBS,
        help="honest v3: retained observations needed before an anchor to rebuild its as-of model",
    )
    p.add_argument(
        "--include-unknown-setpoint",
        action="store_true",
        help="diagnostic: also score anchors whose setpoint in force is unknown (no logged setpoint, no target band); "
        "by default they are excluded for every model",
    )
    p.add_argument("--no-v3", action="store_true")
    p.add_argument("--no-diagnostic", action="store_true", help="skip the v4-with-actual-inputs variant")
    p.add_argument("--min-n", type=int, default=72, help="anchors needed before a gate row is scored")
    p.add_argument("--min-coverage", type=float, default=0.8, help="share of anchors v4 must answer")
    p.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    p.add_argument("--json-points", action="store_true", help="include the point series in --json")
    p.add_argument("--publish", action="store_true", help="write climate_advisor_v4_backtest_<entry>.json over SSH")
    p.add_argument(
        "--batch-ssh",
        action="store_true",
        help="SSH mode: read all inputs in one connection and publish all zones in one more (deploy.py uses it)",
    )
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

    if args.local_dir:
        for path in sorted(Path(args.local_dir).glob("climate_advisor_chart_log_*.json")):
            entry = path.stem.removeprefix("climate_advisor_chart_log_")
            if args.zone and entry != args.zone:
                continue
            ldb = path.with_name(f"climate_advisor_learning_{entry}.json")
            arch = path.with_name(f"climate_advisor_forecast_archive_{entry}.json")
            data = _load_local_json(str(path))
            snaps = decode_archive(_load_local_json(str(arch))) if arch.exists() and not args.no_archive else None
            db = _load_local_json(str(ldb)) if ldb.exists() and not args.no_v3 else None
            jobs.append((entry, data.get("entries", []), db, snaps, None))
    elif args.chart_log:
        data = _load_local_json(args.chart_log)
        rows = data.get("entries", []) if isinstance(data, dict) else data
        db = _load_local_json(args.learning_db) if args.learning_db else None
        snaps = None
        if args.forecast_archive and not args.no_archive:
            snaps = decode_archive(_load_local_json(args.forecast_archive))
        jobs.append((args.zone or "local", rows, db, snaps, None))
    elif args.batch_ssh:
        config = load_config()
        tz_name, fetched = batch_fetch(config, args.zone)
        if not args.zone and not args.all_zones and len(fetched) > 1:
            print(f"ERROR: multiple zones found: {', '.join(fetched)}. Pass --zone or --all-zones.", file=sys.stderr)
            return 2
        for z, files in fetched.items():
            data = files.get("chart")
            if not isinstance(data, dict):
                log(f"zone {z or 'default'}: no readable chart log; skipped")
                continue
            raw = None if args.no_archive else files.get("archive")
            db = None if args.no_v3 else files.get("learning")
            jobs.append((z, data.get("entries", []), db, decode_archive(raw) if raw else None, args.tz or tz_name))
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
    batched: dict[str, dict] = {}
    for z, rows, db, snaps, tz_name in jobs:
        first_ts = next((r.get("ts") for r in rows if isinstance(r, dict) and r.get("ts")), None)
        tz = resolve_tz(args.tz or tz_name, first_ts)
        log(f"zone {z or 'default'}: {len(rows)} chart_log rows, tz={tz}, archive={'yes' if snaps else 'no'}")
        rep = run_backtest(rows, args, tz=tz, zone=z, learning_db=db, archive_snapshots=snaps, log=log)
        reports[z] = rep
        print(format_report(rep))
        if args.publish and args.batch_ssh:
            batched[z] = build_publish_payload(rep)
        elif args.publish:
            if config is None:
                config = load_config()
            dest = publish_remote(config, z, build_publish_payload(rep))
            print(f"published {dest}")
    if batched:
        for dest in publish_remote_batch(config or load_config(), batched):
            print(f"published {dest}")
    if args.json:
        write_json(args.json, reports, args.json_points)
        print(f"wrote {args.json}")
    print(f"\ntotal wall time: {time.monotonic() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
