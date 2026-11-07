#!/usr/bin/env python3
"""Replay the operation timeline over every recorded day and compare it with what CA actually did (Issue #1230 R3a).

WHY: before the timeline becomes the one plan every surface reads (R3b), it has to be shown against history. For each
recorded day of a zone this tool regenerates the timeline "as at the start of that day" (the moment the per-day latch
would have made it) and diffs it with the day as it happened: chart_log rows (setpoint, hvac action, fan, nat-vent,
windows, band) and the learning DB's daily records (day type, window plan, runtime, overrides).

WHAT A DAY'S DIFF HOLDS
  * matched      - a timeline step with the real event it predicts, and the timing error in minutes
  * timeline_only - a step the timeline has that CA did not do, with a class (why)
  * ca_only      - something CA did that no step explains, with a class (override, nat-vent, unexplained, ...)
  * hvac         - predicted vs actual equipment minutes, and hour-bucket agreement
  * path         - predicted indoor path vs the reading, mean absolute error

INPUT CHOICES (they decide how honest the numbers are; the report states them):
  * config (comfort/sleep band, wake/sleep time) is INFERRED from the logged target band ramps: the chart_log has no
    config. ``--wake/--sleep`` override the inference.
  * classification: the learning record's day type, window plan and recommended mode, with ``classify_day`` filling the
    fields the record lacks. Occupancy is the record's (all zones are 'home' all days in the current data).
  * forecast: the chart_log ``pred_outdoor`` of the day (lead 0: hindsight, so optimistic). The forecast archive is
    empty on these copies.
  * thermal model: the learning DB's current cache (hindsight for older days, flattering to the timeline).
  * pre-cool: read back from the logged band (upper edge below the sleep ceiling at night) because the plan knew it.

Usage:
    python tools/timeline_replay.py --data-dir DIR --all-zones [--json out.json] [--days 30]
    python tools/timeline_replay.py --chart-log C.json --learning-db L.json
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import statistics
import sys
import time as _time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BAND_TOL_MIN = 45  # a band step "matches" a setpoint change within this many minutes
EVENT_TOL_MIN = 90  # window / nat-vent steps match within this many minutes
MIN_ROWS_PER_DAY = 40
DAY_START = time(0, 5)

_COORD: Any = None


def _install_stubs() -> None:
    from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

    install_ha_stubs()


def _modules() -> tuple[Any, Any, Any, Any]:
    """(operation_timeline, automation, classifier, coordinator) with Home Assistant stubbed."""
    global _COORD  # noqa: PLW0603
    import importlib  # noqa: PLC0415

    _install_stubs()
    pkg = "custom_components.climate_advisor"
    if _COORD is None:
        _COORD = importlib.import_module(f"{pkg}.coordinator")
    return (
        importlib.import_module(f"{pkg}.operation_timeline"),
        importlib.import_module(f"{pkg}.automation"),
        importlib.import_module(f"{pkg}.classifier"),
        _COORD,
    )


def _set_ha_timezone(tz: tzinfo) -> None:
    dt_mod = sys.modules.get("homeassistant.util.dt")
    if dt_mod is None:
        return
    if hasattr(dt_mod, "set_default_time_zone"):
        dt_mod.set_default_time_zone(tz)
    else:
        dt_mod.DEFAULT_TIME_ZONE = tz


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Row:
    ts: datetime
    event: str | None
    hvac: str
    indoor: float | None
    outdoor: float | None
    pred_outdoor: float | None
    setpoint: float | None
    lower: float | None
    upper: float | None
    windows_open: bool
    windows_recommended: bool
    nat_vent: bool
    fan_running: bool

    @classmethod
    def of(cls, e: dict, tz: tzinfo) -> Row | None:
        try:
            ts = datetime.fromisoformat(e["ts"]).astimezone(tz)
        except KeyError, ValueError, TypeError:
            return None

        def num(k: str) -> float | None:
            v = e.get(k)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        return cls(
            ts,
            e.get("event"),
            str(e.get("hvac") or ""),
            num("indoor"),
            num("outdoor"),
            num("pred_outdoor"),
            num("setpoint"),
            num("lower"),
            num("upper"),
            bool(e.get("windows_open")),
            bool(e.get("windows_recommended")),
            bool(e.get("nat_vent_active")),
            bool(e.get("fan_running")),
        )


@dataclass
class ZoneData:
    name: str
    rows: list[Row]
    records: dict[date, dict]
    model: dict
    tz: tzinfo
    by_day: dict[date, list[Row]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for r in self.rows:
            self.by_day.setdefault(r.ts.date(), []).append(r)


def load_zone(chart_path: Path, learning_path: Path, tz: tzinfo, name: str | None = None) -> ZoneData:
    chart = json.loads(chart_path.read_text(encoding="utf-8"))
    rows = sorted((r for e in chart.get("entries", []) if (r := Row.of(e, tz)) is not None), key=lambda r: r.ts)
    db = json.loads(learning_path.read_text(encoding="utf-8")) if learning_path.exists() else {}
    records = {}
    for rec in db.get("records", []):
        with contextlib.suppress(KeyError, ValueError, TypeError):
            records[date.fromisoformat(rec["date"])] = rec
    return ZoneData(name or chart_path.stem[-8:], rows, records, dict(db.get("thermal_model_cache") or {}), tz)


# ---------------------------------------------------------------------------
# Config inference from the logged band
# ---------------------------------------------------------------------------


def infer_config(zone: ZoneData, wake: str | None = None, sleep: str | None = None) -> dict[str, Any]:
    """Comfort and sleep edges (plateau modes) and wake/sleep times (from the ramp positions) of a zone."""
    awake, asleep = Counter(), Counter()
    for r in zone.rows:
        if r.event or r.lower is None or r.upper is None:
            continue
        h = r.ts.hour + r.ts.minute / 60
        if 11 <= h < 17:
            awake[(r.lower, r.upper)] += 1
        elif 1 <= h < 5:
            asleep[(r.lower, r.upper)] += 1
    a_lo, a_hi = awake.most_common(1)[0][0] if awake else (68.0, 74.0)
    s_lo, s_hi = asleep.most_common(1)[0][0] if asleep else (a_lo, a_hi)
    # Pre-cool lowers only the sleep ceiling: take the sleep plateau from the floor and the most common ceiling.
    s_hi_counts = Counter(hi for (lo, hi) in asleep.elements() if lo == s_lo)
    if s_hi_counts:
        s_hi = max(s_hi_counts)
    wakes, sleeps = [], []
    use_floor = abs(a_lo - s_lo) >= 1.0
    for r in zone.rows:
        if r.event or r.lower is None or r.upper is None:
            continue
        h = r.ts.hour + r.ts.minute / 60
        if use_floor:
            lo_v, awake_v, sleep_v = r.lower, a_lo, s_lo
        elif abs(a_hi - s_hi) >= 1.0:
            lo_v, awake_v, sleep_v = r.upper, a_hi, s_hi
        else:
            break
        frac = (lo_v - sleep_v) / (awake_v - sleep_v)
        if 0.05 < frac < 0.95:
            if 4 <= h < 12:
                wakes.append(h - frac * 2.0)  # wake ramp is 2 h
            elif 15 <= h < 24:
                sleeps.append(h - (1.0 - frac) * 1.0)  # sleep ramp is 1 h

    def _hhmm(hours: float, fallback: str) -> str:
        if hours is None:
            return fallback
        total = int(round(hours * 12) * 5)  # nearest 5 minutes
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"

    confidence = "inferred"
    if (s_hi - s_lo) > (a_hi - a_lo) + 6.0 and not (wake and sleep):
        # The night rows are an away/vacation setback band (occupancy scenario zones), not a sleep band: the ramps say
        # nothing about wake/sleep. Assume the simulated zones' common schedule instead, and say so.
        s_lo, s_hi, wakes, sleeps, confidence = a_lo - 4.0, a_hi - 2.0, [], [], "assumed"
    cfg = {
        "comfort_heat": a_lo,
        "comfort_cool": a_hi,
        "sleep_heat": s_lo,
        "sleep_cool": s_hi,
        "setback_heat": min(a_lo - 5.0, 62.0),
        "setback_cool": max(a_hi + 5.0, 80.0),
        "wake_time": wake or _hhmm(statistics.median(wakes) if wakes else None, "06:30"),
        "sleep_time": sleep or _hhmm(statistics.median(sleeps) if sleeps else None, "22:30"),
        "aggressive_savings": False,
        "config_confidence": confidence,
        "inferred_from": {"wake_samples": len(wakes), "sleep_samples": len(sleeps)},
    }
    return cfg


# ---------------------------------------------------------------------------
# Snapshot for a day
# ---------------------------------------------------------------------------


def _hourly_forecast(zone: ZoneData, day: date) -> list[tuple[datetime, float]]:
    """Hourly (aware) outdoor curve for ``day`` and ``day+1`` from the logged forecast values."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for d in (day, day + timedelta(days=1)):
        for r in zone.by_day.get(d, ()):
            v = r.pred_outdoor if r.pred_outdoor is not None else r.outdoor
            if v is not None:
                buckets[r.ts.replace(minute=0, second=0, microsecond=0)].append(v)
    pts = sorted((t, sum(v) / len(v)) for t, v in buckets.items())
    if len(pts) < 12:
        return []
    return pts


def _pre_cool_from_band(zone: ZoneData, day: date, cfg: dict[str, Any]) -> tuple[datetime | None, float | None]:
    """The overnight pre-cool the logged band shows: first night row whose ceiling dropped below the sleep ceiling."""
    wake_h = _hm(cfg["wake_time"])
    for d in (day, day + timedelta(days=1)):
        for r in zone.by_day.get(d, ()):
            h = r.ts.hour + r.ts.minute / 60
            if r.event or r.upper is None or h >= wake_h:
                continue
            if r.upper < cfg["sleep_cool"] - 0.4:
                return r.ts.replace(second=0, microsecond=0), r.upper
    return None, None


def _occupancy_at_day_start(zone: ZoneData, day: date) -> str:
    """home / away / vacation from the width of the band logged in the first rows of the day (setbacks are wide)."""
    for r in zone.by_day.get(day, ()):
        if r.lower is not None and r.upper is not None and not r.event:
            width = r.upper - r.lower
            return "vacation" if width >= 20.0 else ("away" if width >= 12.0 else "home")
    return "home"


def _hm(text: str) -> float:
    h, m = text.split(":")[:2]
    return int(h) + int(m) / 60


def build_snapshot(zone: ZoneData, day: date, cfg: dict[str, Any], mods: tuple[Any, Any, Any, Any], *, with_v3: bool):
    ot, automation, classifier, coord = mods
    rec = zone.records.get(day)
    if rec is None:
        return None
    hourly = _hourly_forecast(zone, day)
    if not hourly:
        return None
    now = datetime.combine(day, DAY_START, tzinfo=zone.tz)
    nxt = zone.records.get(day + timedelta(days=1)) or rec
    high = float(rec.get("forecast_high_f") or rec.get("observed_high_f") or max(v for _t, v in hourly))
    low = float(rec.get("forecast_low_f") or rec.get("observed_low_f") or min(v for _t, v in hourly))
    c = classifier.classify_day(
        classifier.ForecastSnapshot(
            today_high=high,
            today_low=low,
            tomorrow_high=float(nxt.get("forecast_high_f") or high),
            tomorrow_low=float(nxt.get("forecast_low_f") or low),
            current_outdoor_temp=low,
        ),
        comfort_heat=cfg["comfort_heat"],
        comfort_cool=cfg["comfort_cool"],
    )
    # The record is what the engine actually classified: it wins over the re-derived classification.
    updates: dict[str, Any] = {"day_type": rec.get("day_type", c.day_type)}
    if rec.get("hvac_mode_recommended"):
        updates["hvac_mode"] = rec["hvac_mode_recommended"]
    if "windows_recommended" in rec:
        updates["windows_recommended"] = bool(rec["windows_recommended"])
        updates["window_open_time"] = _t(rec.get("window_open_time"))
        updates["window_close_time"] = _t(rec.get("window_close_time"))
    c = replace(c, **updates)
    first = next((r for r in zone.by_day.get(day, ()) if r.indoor is not None), None)
    indoor = first.indoor if first else (cfg["comfort_heat"] + cfg["comfort_cool"]) / 2
    tcache = dict(zone.model)
    pre_t, pre_target = _pre_cool_from_band(zone, day, cfg)
    snap = ot.TimelineSnapshot(
        now=now,
        zone=zone.name,
        wake_time=_t(cfg["wake_time"]),
        sleep_time=_t(cfg["sleep_time"]),
        fan_mode="whole_house_fan",
        sleep_sensor_label=None,
        band_at=automation.band_resolver_for(c, cfg),
        occupancy_mode=_occupancy_at_day_start(zone, day),
        day_modes={day: c.hvac_mode, day + timedelta(days=1): (nxt.get("hvac_mode_recommended") or c.hvac_mode)},
        day_type=c.day_type,
        windows_recommended=bool(c.windows_recommended),
        window_open_time=c.window_open_time,
        window_close_time=c.window_close_time,
        opportunity_spans=tuple(
            span
            for span in (
                (c.window_opportunity_morning_start, c.window_opportunity_morning_end)
                if c.window_opportunity_morning
                else None,
                (c.window_opportunity_evening_start, c.window_opportunity_evening_end)
                if c.window_opportunity_evening
                else None,
            )
            if span is not None and None not in span
        ),
        pre_cool_trigger=pre_t,
        pre_cool_target=pre_target,
        indoor=indoor,
        indoor_primary=indoor,
        outdoor=hourly[0][1],
        hourly=hourly,
        thermal_model=tcache or None,
    )
    if with_v3 and tcache:
        _set_ha_timezone(zone.tz)
        entries = [{"datetime": t.isoformat(), "temperature": v} for t, v in hourly]
        band_sched = [
            {"ts": t.isoformat(), "lower": b.floor, "upper": b.ceiling}
            for t, _v in hourly
            for b in [snap.band_at(t, snap.occupancy_mode)]
        ]

        def _path_fn(spans):
            if spans:
                a, b = max(spans, key=lambda ab: ab[1] - ab[0])
                c2 = replace(c, windows_recommended=True, window_open_time=a.time(), window_close_time=b.time())
            else:
                c2 = replace(c, windows_recommended=False)
            out = coord._build_predicted_indoor_future(
                entries,
                cfg,
                now,
                current_indoor_temp=indoor,
                thermal_model=tcache,
                classification=c2,
                band_schedule=band_sched,
            )
            return [(datetime.fromisoformat(e["ts"]), float(e["temp"])) for e in out]

        snap.path_fn = _path_fn
    return snap, c, rec


def _t(text: Any) -> time | None:
    if not text:
        return None
    parts = str(text).split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


# ---------------------------------------------------------------------------
# What actually happened
# ---------------------------------------------------------------------------


@dataclass
class Actual:
    setpoint_changes: list[tuple[datetime, float, float]]
    nat_vent_edges: list[tuple[datetime, bool]]
    window_edges: list[tuple[datetime, bool]]  # physical, sustained
    rec_edges: list[tuple[datetime, bool]]  # windows_recommended
    overrides: list[datetime]
    reclass: list[datetime]
    band_changes: list[datetime]
    hvac_hours: dict[datetime, float]  # hour start -> minutes
    hvac_minutes: float
    indoor: list[tuple[datetime, float]]
    upper_drops: list[tuple[datetime, float]]


def actuals(zone: ZoneData, day: date) -> Actual:
    rows = zone.by_day.get(day, [])
    prev_day = zone.by_day.get(day - timedelta(days=1), [])
    seq = ([prev_day[-1]] if prev_day else []) + rows
    sp: list[tuple[datetime, float, float]] = []
    nv: list[tuple[datetime, bool]] = []
    we: list[tuple[datetime, bool]] = []
    ra: list[tuple[datetime, bool]] = []
    ov, rc, bc = [], [], []
    last_sp = last_nv = last_ra = None
    last_band = None
    hv: dict[datetime, float] = defaultdict(float)
    drops: list[tuple[datetime, float]] = []
    for i, r in enumerate(seq):
        if r.setpoint is not None:
            if last_sp is not None and abs(r.setpoint - last_sp) >= 0.5 and r.ts.date() == day:
                sp.append((r.ts, last_sp, r.setpoint))
            last_sp = r.setpoint
        if last_nv is not None and r.nat_vent != last_nv and r.ts.date() == day:
            nv.append((r.ts, r.nat_vent))
        last_nv = r.nat_vent
        if last_ra is not None and r.windows_recommended != last_ra and r.ts.date() == day:
            ra.append((r.ts, r.windows_recommended))
        last_ra = r.windows_recommended
        if r.event == "override" and r.ts.date() == day:
            ov.append(r.ts)
        if r.event == "classification_change" and r.ts.date() == day:
            rc.append(r.ts)
        band = (r.lower, r.upper)
        if (
            last_band is not None
            and last_band[1] is not None
            and r.upper is not None
            and r.upper <= last_band[1] - 0.4
            and r.ts.date() == day
            and r.ts.hour < 7
        ):
            drops.append((r.ts, r.upper))
        if last_band is not None and r.lower is not None and band != last_band and r.ts.date() == day:
            bc.append(r.ts)
        if r.lower is not None:
            last_band = band
        if i + 1 < len(seq) and r.hvac in ("heating", "cooling") and r.ts.date() == day:
            end = min(seq[i + 1].ts, r.ts + timedelta(minutes=45))
            t = r.ts
            while t < end:
                h0 = t.replace(minute=0, second=0, microsecond=0)
                step_end = min(end, h0 + timedelta(hours=1))
                hv[h0] += (step_end - t).total_seconds() / 60.0
                t = step_end
    # sustained physical window state: >= 15 min
    state = None
    since = None
    for r in seq:
        if r.windows_open != state:
            if state is True and since is not None and (r.ts - since) >= timedelta(minutes=15) and since.date() == day:
                we.append((since, True))
                we.append((r.ts, False))
            state, since = r.windows_open, r.ts
    indoor = [(r.ts, r.indoor) for r in rows if r.indoor is not None and not r.event]
    return Actual(sp, nv, we, ra, ov, rc, bc, dict(hv), sum(hv.values()), indoor, drops)


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


def _near(ts: datetime, candidates: list[datetime], tol_min: float) -> tuple[datetime, float] | None:
    best = None
    for c in candidates:
        d = (c - ts).total_seconds() / 60.0
        if abs(d) <= tol_min and (best is None or abs(d) < abs(best[1])):
            best = (c, d)
    return best


def compare_day(tl: Any, act: Actual, rec: dict, day: date, zone: ZoneData, zone_cfg: dict[str, Any]) -> dict[str, Any]:
    start = datetime.combine(day, DAY_START, tzinfo=zone.tz)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone.tz)
    steps = [s for s in tl.steps if start <= s.ts < end]
    matched: list[dict] = []
    tl_only: list[dict] = []
    used_sp: set[int] = set()
    sp_times = [t for t, _a, _b in act.setpoint_changes]

    def note(step: Any, cls: str, **extra: Any) -> None:
        tl_only.append({"ts": step.ts.isoformat(), "kind": step.kind.value, "class": cls, **extra})

    for s in steps:
        k = s.kind.value
        if k in ("wake_band", "sleep_band"):
            hit = _near(s.ts, sp_times, BAND_TOL_MIN)
            if hit is not None:
                used_sp.add(sp_times.index(hit[0]))
                matched.append({"ts": s.ts.isoformat(), "kind": k, "delta_min": round(hit[1], 1)})
                continue
            if _near(s.ts, act.overrides, 180):
                note(s, "override_held")
                continue
            before = _setpoint_at(zone, s.ts - timedelta(minutes=20))
            after = _setpoint_at(zone, s.ts + timedelta(minutes=20))
            at_row = _row_at(zone, s.ts + timedelta(minutes=5))
            if at_row is not None and at_row.nat_vent:
                note(s, "deferred_nat_vent_owns_hvac")  # handle_bedtime / wake leave the fan session alone
            elif at_row is not None and at_row.windows_open:
                note(s, "paused_by_open_window")  # the door/window pause holds the thermostat Off
            elif before is None or after is None:
                note(s, "thermostat_off_no_setpoint")  # nothing to write while the thermostat is Off
            elif abs(before - after) < 0.5:
                note(s, "setpoint_already_there", setpoint=after)
            else:
                note(s, "setpoint_moved_outside_tolerance")
        elif k in ("nat_vent_start", "nat_vent_stop"):
            want = k == "nat_vent_start"
            hit = _near(s.ts, [t for t, v in act.nat_vent_edges if v == want], EVENT_TOL_MIN)
            if hit:
                matched.append({"ts": s.ts.isoformat(), "kind": k, "delta_min": round(hit[1], 1)})
            else:
                note(s, "no_nat_vent_edge")
        elif k in ("windows_open", "windows_close"):
            want = k == "windows_open"
            hit = _near(s.ts, [t for t, v in act.window_edges if v == want], EVENT_TOL_MIN)
            if hit:
                matched.append({"ts": s.ts.isoformat(), "kind": k, "delta_min": round(hit[1], 1)})
            else:
                note(s, "occupant_did_not")
        elif k == "setback_start":
            row = _row_at(zone, s.ts + timedelta(hours=1))
            if row is not None and row.lower is not None and row.upper is not None and row.upper - row.lower >= 12.0:
                matched.append({"ts": s.ts.isoformat(), "kind": k, "delta_min": 0.0})
            else:
                note(s, "no_wide_band_logged")
        elif k == "precool_start":
            hit = _near(s.ts, [t for t, _u in act.upper_drops], 120)
            if hit:
                matched.append({"ts": s.ts.isoformat(), "kind": k, "delta_min": round(hit[1], 1)})
            else:
                note(s, "no_precool_logged")
        else:
            note(s, "not_observable_in_chart_log")

    ca_only: list[dict] = []
    for i, (ts, a, b) in enumerate(act.setpoint_changes):
        if i in used_sp or not (start <= ts < end):
            continue
        cls = "unexplained"
        edges = _edge_values(zone)
        row = _row_at(zone, ts)
        if _near(ts, act.overrides, 20):
            cls = "occupant_override"
        elif _near(ts, [t for t, _v in act.nat_vent_edges], 15):
            cls = "nat_vent_edge"
        elif _near(ts, act.reclass, 20):
            cls = "reclassification"
        elif row is not None and row.lower is not None and {a, b} == {row.lower, row.upper}:
            cls = "edge_switch_nearest_edge_arming"  # floor <-> ceiling as the room moves: reactive, not planned
        elif a in edges and b in edges:
            cls = "band_edge_change"  # setback / restore / sleep edge reached by a path the timeline has no step for
        elif abs(b - a) <= 2.0 and a not in edges and b in edges:
            cls = "occupant_change_expired_ca_restores"  # grace ended: CA re-applies its band (a RESUME_SCHEDULE)
        elif abs(b - a) <= 2.0 and b not in edges:
            cls = "occupant_adjustment"  # a degree or two off any band edge: someone used the thermostat
        ca_only.append({"ts": ts.isoformat(), "from": a, "to": b, "class": cls})
    # windows the engine recommended / the occupant opened / nat-vent sessions that no step explains
    open_steps = [s.ts for s in steps if s.kind.value == "windows_open"]
    wake_h, sleep_h = _hm(zone_cfg["wake_time"]), _hm(zone_cfg["sleep_time"])

    def asleep(t: datetime) -> bool:
        h = t.hour + t.minute / 60
        return (h >= sleep_h or h < wake_h) if sleep_h > wake_h else (sleep_h <= h < wake_h)

    for t in [t for t, v in act.rec_edges if v]:
        if not _near(t, open_steps, EVENT_TOL_MIN):
            ca_only.append({"ts": t.isoformat(), "class": "engine_recommended_windows_timeline_closed"})
    for t, v in act.window_edges:
        if v and not _near(t, open_steps, EVENT_TOL_MIN):
            row = _row_at(zone, t)
            kind = "occupant_opened_windows_asleep" if asleep(t) else "occupant_opened_windows_awake_unadvised"
            if row is not None and row.windows_recommended and not asleep(t):
                kind = "occupant_followed_engine_windows_timeline_closed"
            ca_only.append({"ts": t.isoformat(), "class": kind})
    last_on: datetime | None = None
    for t, v in act.nat_vent_edges:  # one entry per SESSION: edges less than an hour after the last one are cycling
        if v and (last_on is None or (t - last_on) > timedelta(hours=1)):
            planned = any(
                s.kind.value == "nat_vent_start" and abs((s.ts - t).total_seconds()) < EVENT_TOL_MIN * 60 for s in steps
            )
            if not planned:
                kind = (
                    "nat_vent_session_asleep_night_strategy" if asleep(t) else "nat_vent_session_awake_not_in_timeline"
                )
                ca_only.append({"ts": t.isoformat(), "class": kind})
        if v:
            last_on = t

    # equipment: hour buckets and minutes
    pred_h = {h: m for h, m in getattr(tl, "hvac_hourly", ()) if start <= h < end}
    act_h = {h: m for h, m in act.hvac_hours.items() if start <= h < end}
    hours = [datetime.combine(day, time(h), tzinfo=zone.tz) for h in range(24)]
    tp = sum(1 for h in hours if pred_h.get(h, 0) >= 5 and act_h.get(h, 0) >= 5)
    fp = sum(1 for h in hours if pred_h.get(h, 0) >= 5 and act_h.get(h, 0) < 5)
    fn = sum(1 for h in hours if pred_h.get(h, 0) < 5 and act_h.get(h, 0) >= 5)
    open_hours = {
        r.ts.replace(minute=0, second=0, microsecond=0)
        for r in zone.by_day.get(day, ())
        if r.windows_open or r.nat_vent
    }
    ctp = sum(1 for h in hours if h not in open_hours and pred_h.get(h, 0) >= 5 and act_h.get(h, 0) >= 5)
    cfp = sum(1 for h in hours if h not in open_hours and pred_h.get(h, 0) >= 5 and act_h.get(h, 0) < 5)
    cfn = sum(1 for h in hours if h not in open_hours and pred_h.get(h, 0) < 5 and act_h.get(h, 0) >= 5)
    pred_min = sum(pred_h.values())
    act_min = float(rec.get("hvac_runtime_minutes") or act.hvac_minutes or 0.0)
    # path error against the logged reading, hourly
    err = []
    pts = sorted((p.ts, p.temp_f) for p in tl.path if p.sensor == "_ca")
    keys = [t.timestamp() for t, _v in pts]
    for ts, v in act.indoor:
        if not (start <= ts < end) or not keys:
            continue
        i = bisect.bisect_left(keys, ts.timestamp())
        if 0 < i < len(pts) and abs(pts[i][0] - ts).total_seconds() < 1800:
            err.append(pts[i][1] - v)
    return {
        "date": day.isoformat(),
        "day_type": rec.get("day_type"),
        "steps": len(steps),
        "matched": matched,
        "timeline_only": tl_only,
        "ca_only": ca_only,
        "hvac": {
            "pred_min": round(pred_min, 1),
            "actual_min": round(act_min, 1),
            "hour_tp": tp,
            "hour_fp": fp,
            "hour_fn": fn,
            "closed_tp": ctp,
            "closed_fp": cfp,
            "closed_fn": cfn,
        },
        "path_mae_f": round(sum(abs(e) for e in err) / len(err), 2) if err else None,
        "path_bias_f": round(sum(err) / len(err), 2) if err else None,
        "candidates": [f"{c.kind}:{c.reason}:{c.end_reason}" for c in tl.candidates],
        "windows": {
            "timeline_open_steps": len(open_steps),
            "engine_recommended": bool(rec.get("windows_recommended")),
            "occupant_opened": bool(rec.get("windows_physically_opened")),
        },
    }


def _row_at(zone: ZoneData, ts: datetime) -> Row | None:
    best = None
    for r in zone.by_day.get(ts.date(), []):
        if r.ts <= ts:
            best = r
        else:
            break
    return best


def _edge_values(zone: ZoneData) -> set[float]:
    """Every band edge the zone ever logged: the values a setpoint can legitimately sit on."""
    cached = getattr(zone, "_edges", None)
    if cached is None:
        cached = {v for r in zone.rows for v in (r.lower, r.upper) if v is not None and not r.event}
        counts = Counter(v for r in zone.rows for v in (r.lower, r.upper) if v is not None and not r.event)
        cached = {v for v, n in counts.items() if n >= 20}  # ramps pass through many values once; plateaus repeat
        zone._edges = cached  # type: ignore[attr-defined]
    return cached


def _setpoint_at(zone: ZoneData, ts: datetime) -> float | None:
    """The setpoint in the last logged row at or before ``ts`` (None = no setpoint: the thermostat is Off)."""
    row = _row_at(zone, ts)
    return row.setpoint if row is not None else None


# ---------------------------------------------------------------------------
# Zone run and report
# ---------------------------------------------------------------------------


def replay_zone(zone: ZoneData, cfg: dict[str, Any], *, days: int | None, with_v3: bool = True) -> dict[str, Any]:
    mods = _modules()
    ot = mods[0]
    dates = sorted(d for d, rows in zone.by_day.items() if len(rows) >= MIN_ROWS_PER_DAY and d in zone.records)
    dates = dates[1:-1]  # first and last days are partial
    if days:
        dates = dates[-days:]
    out_days: list[dict] = []
    ms: list[float] = []
    for d in dates:
        built = build_snapshot(zone, d, cfg, mods, with_v3=with_v3)
        if built is None:
            continue
        snap, _c, rec = built
        t0 = _time.perf_counter()
        tl = ot.build_timeline(snap)
        ms.append((_time.perf_counter() - t0) * 1000.0)
        out_days.append(compare_day(tl, actuals(zone, d), rec, d, zone, cfg))
    return {"zone": zone.name, "config": cfg, "days": out_days, "build_ms": _stats(ms), "summary": summarise(out_days)}


def _stats(ms: list[float]) -> dict[str, float | None]:
    if not ms:
        return {"mean": None, "max": None}
    return {"mean": round(sum(ms) / len(ms), 1), "max": round(max(ms), 1)}


def summarise(days: list[dict]) -> dict[str, Any]:
    n_steps = sum(len(d["matched"]) + len(d["timeline_only"]) for d in days)
    matched = [m for d in days for m in d["matched"]]
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"matched": 0, "timeline_only": 0})
    for d in days:
        for m in d["matched"]:
            by_kind[m["kind"]]["matched"] += 1
        for t in d["timeline_only"]:
            by_kind[t["kind"]]["timeline_only"] += 1
    tl_only = Counter((t["kind"], t["class"]) for d in days for t in d["timeline_only"])
    ca_only = Counter(c["class"] for d in days for c in d["ca_only"])
    deltas = [m["delta_min"] for m in matched if m["kind"] in ("wake_band", "sleep_band")]
    tp = sum(d["hvac"]["hour_tp"] for d in days)
    fp = sum(d["hvac"]["hour_fp"] for d in days)
    fn = sum(d["hvac"]["hour_fn"] for d in days)
    ctp = sum(d["hvac"]["closed_tp"] for d in days)
    cfp = sum(d["hvac"]["closed_fp"] for d in days)
    cfn = sum(d["hvac"]["closed_fn"] for d in days)
    mae = [d["path_mae_f"] for d in days if d["path_mae_f"] is not None]
    pred = sum(d["hvac"]["pred_min"] for d in days)
    act = sum(d["hvac"]["actual_min"] for d in days)
    return {
        "days": len(days),
        "steps": n_steps,
        "match_rate": round(len(matched) / n_steps, 3) if n_steps else None,
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "band_timing_min": {
            "median_abs": round(statistics.median(abs(x) for x in deltas), 1) if deltas else None,
            "max_abs": round(max(abs(x) for x in deltas), 1) if deltas else None,
        },
        "timeline_only": {f"{k}:{c}": n for (k, c), n in tl_only.most_common()},
        "ca_only": dict(ca_only.most_common()),
        "hvac_hours": {
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "windows_closed_precision": round(ctp / (ctp + cfp), 3) if ctp + cfp else None,
            "windows_closed_recall": round(ctp / (ctp + cfn), 3) if ctp + cfn else None,
        },
        "hvac_minutes_ratio": round(pred / act, 2) if act else None,
        "path_mae_f": round(sum(mae) / len(mae), 2) if mae else None,
        "window_candidates": dict(Counter(c for d in days for c in d.get("candidates", [])).most_common()),
        "windows_vs_engine": {
            "days_engine_recommended": sum(1 for d in days if d["windows"]["engine_recommended"]),
            "days_occupant_opened": sum(1 for d in days if d["windows"]["occupant_opened"]),
            "days_timeline_offered": sum(1 for d in days if d["windows"]["timeline_open_steps"]),
        },
    }


def render(result: dict[str, Any]) -> str:
    s = result["summary"]
    cfg = result["config"]
    bt, hh = s["band_timing_min"], s["hvac_hours"]
    band = f"band {cfg['comfort_heat']}-{cfg['comfort_cool']} / sleep {cfg['sleep_heat']}-{cfg['sleep_cool']}"
    lines = [
        f"== zone {result['zone']}: {s['days']} days, {s['steps']} timeline steps, match rate {s['match_rate']}"
        f"  (build {result['build_ms']['mean']} ms mean, {result['build_ms']['max']} max)",
        f"   inferred: {band}, wake {cfg['wake_time']} sleep {cfg['sleep_time']}",
        f"   band step timing: median |d| {bt['median_abs']} min, max {bt['max_abs']} min",
        f"   by kind: {s['by_kind']}",
        f"   timeline-only classes: {s['timeline_only']}",
        f"   CA-only classes:       {s['ca_only']}",
        f"   equipment: hour precision {hh['precision']}, recall {hh['recall']} "
        f"(windows-closed hours: {hh['windows_closed_precision']}/{hh['windows_closed_recall']}), "
        f"minutes predicted/actual {s['hvac_minutes_ratio']}",
        f"   path MAE vs reading: {s['path_mae_f']} F",
        f"   windows: {s['windows_vs_engine']}; candidates {s['window_candidates']}",
    ]
    return "\n".join(lines)


def find_zones(data_dir: Path) -> list[tuple[str, Path, Path]]:
    zones = []
    for chart in sorted(data_dir.glob("climate_advisor_chart_log_*.json")):
        zid = chart.stem.removeprefix("climate_advisor_chart_log_")
        zones.append((zid, chart, data_dir / f"climate_advisor_learning_{zid}.json"))
    return zones


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--all-zones", action="store_true")
    ap.add_argument("--zone", help="entry id (suffix match) inside --data-dir")
    ap.add_argument("--chart-log", type=Path)
    ap.add_argument("--learning-db", type=Path)
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--days", type=int, help="only the most recent N days")
    ap.add_argument("--wake")
    ap.add_argument("--sleep")
    ap.add_argument("--no-v3", action="store_true", help="skip the v3 path callback (faster)")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    from zoneinfo import ZoneInfo  # noqa: PLC0415

    tz = ZoneInfo(args.tz)
    jobs: list[tuple[str, Path, Path]] = []
    if args.chart_log:
        jobs.append(("zone", args.chart_log, args.learning_db or Path("/nonexistent")))
    elif args.data_dir:
        jobs = [z for z in find_zones(args.data_dir) if args.all_zones or (args.zone and z[0].endswith(args.zone))]
    if not jobs:
        ap.error("nothing to replay: give --chart-log, or --data-dir with --all-zones / --zone")
    results = []
    for zid, chart, learning in jobs:
        zone = load_zone(chart, learning, tz, name=zid)
        cfg = infer_config(zone, args.wake, args.sleep)
        result = replay_zone(zone, cfg, days=args.days, with_v3=not args.no_v3)
        print(render(result))
        results.append(result)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
