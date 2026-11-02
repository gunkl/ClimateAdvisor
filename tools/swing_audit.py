#!/usr/bin/env python3
"""Check the learned HVAC swing against what your own thermostat really does (Issue #1233).

Occupant framing: the dashboard says "Heat swing 1.4 F below setpoint". Is that true of YOUR
equipment? This tool measures the real firing offsets - how far below (above) the setpoint the room
actually was when the thermostat started the equipment - from Home Assistant's recorder history
(or a saved chart_log), and prints them next to the value Climate Advisor learned.

Every cycle is judged by ``swing.evaluate_swing_cycle`` from the integration itself, so the audit
and the live learner apply exactly the same rule (setpoint/mode-change window, flap guard,
direction, bounds). Nothing here re-implements it.

Sources (any one):
    --climate-file F [--sensor-file S]   saved ``tools/ha_logs.py --history`` output (offline)
    --climate-entity E [--sensor-entity S] --hours N
                                         fetched now through ``tools/ha_logs.py`` (REST history;
                                         needs the HA_API_TOKEN already set up for ha_logs.py; it is never printed)
    --chart-log FILE.json                a saved climate_advisor_chart_log.json (accepted cycles only)

A dedicated fine sensor (``--sensor-*``) gives the true start point; without one the thermostat's
own whole-degree ``current_temperature`` is used and the result is marked as quantised.

Compare with what Climate Advisor learned:
    --learned-heat 1.4 --learned-cool 1.5      by hand
    --db-json FILE                             a saved learning DB
    --from-db [--entry-id ID]                  read over SSH like tools/learning_db.py

Examples:
    python tools/ha_logs.py --history --entity climate.hall --hours 72 > /tmp/climate.txt
    python tools/ha_logs.py --history --entity sensor.hall_temp --hours 72 > /tmp/sensor.txt
    python tools/swing_audit.py --climate-file /tmp/climate.txt --sensor-file /tmp/sensor.txt --from-db
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from learning_db import _load_swing_module  # noqa: E402  (one shared loader for swing.py)

_swing = _load_swing_module()

# A fine-sensor reading older than this is not "the room at that moment" (the sensor may have gone quiet).
_MAX_SENSOR_AGE_S = 1800.0
_ACTIVE = {"heating": "heat", "cooling": "cool"}


# ---------------------------------------------------------------------------
# Parsing ``ha_logs.py --history`` output
# ---------------------------------------------------------------------------


def _to_f(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    return value * 1.8 + 32.0 if unit == "celsius" else value


def _num(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "", "None") else None
    except ValueError:
        return None


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf"(?:^|, ){re.escape(name)}=([^,]*)", attrs)
    return m.group(1).strip() if m else None


def _ts(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_climate_history(text: str, unit: str = "fahrenheit") -> list[dict[str, Any]]:
    """Climate rows ``{ts, mode, current_f, sp, action}`` (temperatures in F), oldest first.

    ``sp`` maps each of ``swing.SETPOINT_CHANGE_ATTRS`` to its value in F - the same attributes the
    coordinator compares to detect a setpoint change and reads the setpoint from."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split(" | ", 2)
        if len(parts) < 3 or line.startswith(("===", "(")):
            continue
        ts = _ts(parts[0])
        if ts is None:
            continue
        attrs = parts[2]
        rows.append(
            {
                "ts": ts,
                "mode": parts[1].strip(),
                "current_f": _to_f(_num(_attr(attrs, "current_temperature")), unit),
                "sp": {a: _to_f(_num(_attr(attrs, a)), unit) for a in _swing.SETPOINT_CHANGE_ATTRS},
                "action": (_attr(attrs, "hvac_action") or "").lower(),
            }
        )
    rows.sort(key=lambda r: r["ts"])
    return rows


def parse_sensor_history(text: str, unit: str = "fahrenheit") -> list[tuple[datetime, float]]:
    """Fine-sensor readings ``[(ts, value_f)]``, oldest first; non-numeric states are skipped."""
    out: list[tuple[datetime, float]] = []
    for line in text.splitlines():
        parts = line.split(" | ")
        if len(parts) < 2 or line.startswith(("===", "(")):
            continue
        ts, val = _ts(parts[0]), _num(parts[1])
        if ts is not None and val is not None:
            out.append((ts, _to_f(val, unit)))  # type: ignore[arg-type]
    out.sort(key=lambda r: r[0])
    return out


# ---------------------------------------------------------------------------
# Cycles -> verdicts
# ---------------------------------------------------------------------------


def _setpoint(row: dict[str, Any], mode: str) -> float | None:
    """The coordinator's own preference (``swing.pick_setpoint``): single setpoint, else the low/high bound."""
    return _swing.pick_setpoint(row["sp"].get, mode)


def _changed(prev: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    """The coordinator's setpoint/mode-change test (``swing.setpoint_or_mode_changed``)."""
    return prev is not None and _swing.setpoint_or_mode_changed(
        prev["mode"], prev["sp"].get, row["mode"], row["sp"].get
    )


def cycles_from_climate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Real hvac_action cycles (heating/cooling start -> stop) with their setpoint age and flap gap."""
    cycles: list[dict[str, Any]] = []
    last_change: datetime | None = None
    last_off: dict[str, datetime] = {}
    open_cycle: dict[str, Any] | None = None
    prev: dict[str, Any] | None = None
    for row in rows:
        if _changed(prev, row):
            last_change = row["ts"]
        old_dir = _ACTIVE.get(prev["action"]) if prev else None
        new_dir = _ACTIVE.get(row["action"])
        if old_dir != new_dir:
            if old_dir is not None:
                last_off[old_dir] = row["ts"]  # every real off edge, like the coordinator's flap guard
            if open_cycle is not None and old_dir == open_cycle["mode"]:
                # the reading AT the off event, as the coordinator reads it
                open_cycle.update(
                    off_ts=row["ts"], off_setpoint_f=_setpoint(row, old_dir), off_thermostat_f=row["current_f"]
                )
                cycles.append(open_cycle)
                open_cycle = None
            if new_dir is not None:
                prev_off = last_off.get(new_dir)
                open_cycle = {
                    "mode": new_dir,
                    "on_ts": row["ts"],
                    "setpoint_f": _setpoint(row, new_dir),
                    "setpoint_age_s": (row["ts"] - last_change).total_seconds() if last_change else None,
                    "since_prev_off_s": (row["ts"] - prev_off).total_seconds() if prev_off else None,
                    "on_thermostat_f": row["current_f"],
                }
        prev = row
    return cycles


def _reading_at(
    sensor: list[tuple[datetime, float]],
    times: list[datetime],
    at: datetime,
    max_age_s: float | None = _MAX_SENSOR_AGE_S,
) -> float | None:
    """The fine sensor's last reading at or before ``at`` (None if there is none, or none fresh enough)."""
    i = bisect.bisect_right(times, at) - 1
    if i < 0 or (max_age_s is not None and (at - times[i]).total_seconds() > max_age_s):
        return None
    return sensor[i][1]


def _judge(cycles: list[dict[str, Any]], resolution_f: float, unit: str) -> dict[str, dict[str, Any]]:
    """Run every cycle through the production rule; group the verdicts per mode."""
    out: dict[str, dict[str, Any]] = {
        m: {"rows": [], "reject_counts": {}, "summary": None, "coast": None} for m in ("heat", "cool")
    }
    samples: dict[str, list[dict[str, Any]]] = {"heat": [], "cool": []}
    for c in cycles:
        verdict = _swing.evaluate_swing_cycle(
            mode=c["mode"],
            ts=str(c["on_ts"]),
            setpoint_f=c["setpoint_f"],
            on_indoor_f=c["on_indoor_f"],
            off_indoor_f=c["off_indoor_f"],
            off_setpoint_f=c.get("off_setpoint_f"),
            seconds_since_setpoint_change=c["setpoint_age_s"],
            resolution_f=resolution_f,
            seconds_since_previous_off=c["since_prev_off_s"],
            reading_unit=unit,
        )
        record = _swing.build_swing_audit_record(
            c["mode"],
            verdict,
            ts=str(c["on_ts"]),
            setpoint_f=c["setpoint_f"],
            setpoint_age_s=c["setpoint_age_s"],
            on_indoor_f=c["on_indoor_f"],
            off_indoor_f=c["off_indoor_f"],
            indoor_entity=c.get("source"),
            resolution_f=resolution_f,
            reading_unit=unit,
            since_prev_off_s=c["since_prev_off_s"],
        )
        out[c["mode"]]["rows"].append({**record, "off_ts": str(c.get("off_ts"))})
        if verdict.sample is not None:
            samples[c["mode"]].append(verdict.sample)
        else:
            counts = out[c["mode"]]["reject_counts"]
            counts[verdict.reason] = counts.get(verdict.reason, 0) + 1
    for mode in ("heat", "cool"):
        if samples[mode]:
            s = _swing.summarize_swing_samples(samples[mode])
            out[mode]["summary"] = {
                **s,
                "n": len(samples[mode]),
                "min_f": min(x["value_f"] for x in samples[mode]),
                "max_f": max(x["value_f"] for x in samples[mode]),
            }
        else:
            out[mode]["summary"] = {
                "value_f": None,
                "resolution_f": resolution_f,
                "off_point_f": None,
                "n": 0,
                "min_f": None,
                "max_f": None,
            }
    return out


def audit_cycles(cycles: list[dict[str, Any]], unit: str = "fahrenheit") -> dict[str, dict[str, Any]]:
    """Judge pre-measured cycles (``on_ts, off_ts, mode, setpoint_f, setpoint_age_s, on_indoor_f, off_indoor_f,
    since_prev_off_s``) with the production rule. Used directly by tests and by ``audit_history``."""
    prepared = [
        {**c, "mode": c.get("mode", "heat"), "off_setpoint_f": c.get("off_setpoint_f", c["setpoint_f"])} for c in cycles
    ]
    return _judge(prepared, 0.0, unit)


def coast_from_history(
    climate_rows: list[dict[str, Any]], sensor_rows: list[tuple[datetime, float]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Coast windows exactly as the live coordinator records them, by replaying the history through the
    SAME driver (``swing.coast_thermostat_event`` / ``swing.coast_feed``).

    Each climate row is one thermostat state change; each fine-sensor row is one sensor tick (the live
    sensor listener's input). The indoor reading of an event is the fine sensor's latest value (any age,
    as live) when a sensor is given, else the thermostat's own ``current_temperature`` (climate_fallback).
    Windows still open at the end of the history are not judged (live would not have judged them yet).
    """
    sensor = sensor_rows or []
    times = [t for t, _ in sensor]
    events: list[tuple[datetime, int, Any]] = [(t, 0, v) for t, v in sensor] + [
        (r["ts"], 1, i) for i, r in enumerate(climate_rows)
    ]
    events.sort(key=lambda e: (e[0], e[1]))
    windows: dict[str, dict[str, Any]] = {}
    samples: dict[str, list[dict[str, Any]]] = {"heat": [], "cool": []}
    last_change: datetime | None = None
    for ts, kind, payload in events:
        if kind == 0:
            closed = _swing.coast_feed(windows, ts, payload)
        else:
            row = climate_rows[payload]
            prev = climate_rows[payload - 1] if payload > 0 else None
            changed = _changed(prev, row)
            if changed:
                last_change = ts
            old_dir = _ACTIVE.get(prev["action"]) if prev else None
            new_dir = _ACTIVE.get(row["action"])
            reading = _reading_at(sensor, times, ts, max_age_s=None) if sensor else row["current_f"]
            closed = _swing.coast_thermostat_event(
                windows,
                now=ts,
                reading=reading,
                changed=changed,
                old_dir=old_dir,
                new_dir=new_dir,
                seconds_since_setpoint_change=(ts - last_change).total_seconds() if last_change else None,
                off_setpoint_f=_setpoint(row, old_dir) if old_dir else None,
            )
        for mode, _ended_by, verdict in closed:
            if verdict.sample is not None:
                samples[mode].append(verdict.sample)
    return samples


def audit_history(
    climate_rows: list[dict[str, Any]],
    sensor_rows: list[tuple[datetime, float]] | None = None,
    unit: str = "fahrenheit",
) -> dict[str, Any]:
    """Audit thermostat history. With a fine sensor the start point is read from it; without, from the
    thermostat's own whole-degree ``current_temperature`` (quantised, marked as such)."""
    cycles = cycles_from_climate(climate_rows)
    sensor = sensor_rows or []
    times = [t for t, _ in sensor]
    whole_f = _swing.whole_degree_resolution_f(unit)
    use_sensor = bool(sensor)
    measured: list[dict[str, Any]] = []
    for c in cycles:
        if use_sensor:
            on_f = _reading_at(sensor, times, c["on_ts"])
            off_f = _reading_at(sensor, times, c["off_ts"])
            source = "fine sensor"
        else:
            on_f, off_f, source = c["on_thermostat_f"], c["off_thermostat_f"], "thermostat current_temperature"
        measured.append({**c, "on_indoor_f": on_f, "off_indoor_f": off_f, "source": source})
    result: dict[str, Any] = _judge(measured, 0.0 if use_sensor else whole_f, unit)
    result["basis"] = "fine sensor" if use_sensor else "thermostat current_temperature"
    coast = coast_from_history(climate_rows, sensor if use_sensor else None)
    for mode in ("heat", "cool"):
        result[mode]["coast_samples"] = coast[mode]
        if coast[mode]:
            ended: dict[str, int] = {}
            for smp in coast[mode]:
                ended[smp["ended_by"]] = ended.get(smp["ended_by"], 0) + 1
            result[mode]["coast"] = {
                "median_f": round(statistics.median(smp["coast_f"] for smp in coast[mode]), 2),
                "n": len(coast[mode]),
                "ended_by": ended,
            }
    if use_sensor:  # what CA would learn from the thermostat's own whole-degree reading (climate_fallback)
        thermo = _judge(
            [{**m, "on_indoor_f": m["on_thermostat_f"], "off_indoor_f": m["off_thermostat_f"]} for m in measured],
            whole_f,
            unit,
        )
        for mode in ("heat", "cool"):
            result[mode]["thermostat_summary"] = thermo[mode]["summary"]
    return result


def audit_chart_log(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Accepted cycles from a chart_log, judged by the same function ``thermal_replay`` backfills with."""
    import importlib.util

    path = Path(__file__).resolve().parent / "thermal_replay.py"
    spec = importlib.util.spec_from_file_location("_ca_thermal_replay_for_audit", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    samples = mod.compute_swing_samples_from_chart_log(entries)
    result: dict[str, Any] = {"basis": "chart_log indoor_primary (accepted cycles only)"}
    for mode in ("heat", "cool"):
        s = samples.get(mode, [])
        summary = (
            _swing.summarize_swing_samples(s) if s else {"value_f": None, "resolution_f": 0.0, "off_point_f": None}
        )
        result[mode] = {
            "rows": [],
            "reject_counts": {},
            "coast": None,
            "summary": {
                **summary,
                "n": len(s),
                "min_f": min((x["value_f"] for x in s), default=None),
                "max_f": max((x["value_f"] for x in s), default=None),
            },
        }
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _f(v: Any, d: int = 2) -> str:
    return "-" if v is None else f"{float(v):.{d}f}"


def format_report(audit: dict[str, Any], learned: dict[str, float | None] | None = None, last_n: int = 20) -> str:
    """Per-mode measured swing next to the learned value, the reject histogram and the cycles."""
    learned = learned or {}
    out = [
        "Swing audit - the thermostat's real firing offsets",
        "===================================================",
        _swing.SWING_DEFINITION_TEXT,
        f"basis: {audit.get('basis', '?')}",
        "",
    ]
    for mode, side in (("heat", "below"), ("cool", "above")):
        block = audit[mode]
        summ = block["summary"]
        title = f"{mode.capitalize()}: {summ['n']} usable cycle(s), {sum(block['reject_counts'].values())} rejected"
        out += [title, "-" * len(title)]
        if summ["value_f"] is None:
            out.append("  measured: no usable cycle (every cycle was rejected, or none were found)")
        else:
            res = summ.get("resolution_f") or 0.0
            interval = f" +/-{res / 2:.2f}" if res else ""
            out.append(
                f"  measured {_f(summ['value_f'])}{interval} F {side} setpoint (median; range "
                f"{_f(summ['min_f'])}-{_f(summ['max_f'])}), stops {_f(summ['off_point_f'])} F short of setpoint"
            )
            thermo = block.get("thermostat_summary")
            if thermo and thermo.get("value_f") is not None:
                out.append(
                    f"  from the thermostat's own current_temperature (what Climate Advisor sees without a "
                    f"fine sensor): {_f(thermo['value_f'])} F, whole-degree readings"
                )
        if block.get("coast"):
            word = "peak" if mode == "heat" else "trough"
            ended = ", ".join(f"{k} x{v}" for k, v in sorted((block["coast"].get("ended_by") or {}).items()))
            out.append(
                f"  coast after the stop (not part of the swing; the live rules): {_f(block['coast']['median_f'])} F "
                f"{word} beyond the off reading, {block['coast']['n']} window(s)"
                + (f" (ended by {ended})" if ended else "")
            )
        want = learned.get(mode)
        if want is not None and summ["value_f"] is not None:
            tol = max(0.25, (summ.get("resolution_f") or 0.0) / 2 + 0.1)
            verdict = "matches" if abs(want - summ["value_f"]) <= tol else "DIFFERS"
            out.append(
                f"  learned {_f(want)} F vs measured {_f(summ['value_f'])} F -> {verdict} (tolerance {tol:.2f} F)"
            )
        elif mode in learned:
            out.append(f"  learned {_f(want)} F (nothing to compare with)")
        if block["reject_counts"]:
            hist = ", ".join(f"{k} x{v}" for k, v in sorted(block["reject_counts"].items(), key=lambda kv: -kv[1]))
            out.append(f"  rejected: {hist}")
        rows = block["rows"][-last_n:]
        if rows:
            out.append(f"  last {len(rows)} cycle(s):")
            for r in rows:
                out.append(
                    f"    {str(r['ts'])[:19]}  {('ok' if r['accepted'] else r['reason']):<16}"
                    f" value {_f(r['value_f']):>5}"
                    f"  sp {_f(r['setpoint_f'], 1):>5}  sp age {_f(r['setpoint_age_s'], 0):>6}s"
                    f"  on {_f(r['on_indoor_f']):>6}  off {_f(r['off_indoor_f']):>6}"
                )
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_files(paths: list[str]) -> str:
    return "\n".join(Path(p).read_text(encoding="utf-8") for p in paths)


def _learned_from_cache(cache: dict[str, Any]) -> dict[str, float | None]:
    return {"heat": cache.get("swing_heat_f"), "cool": cache.get("swing_cool_f")}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check the learned swing against the thermostat's real firing offsets")
    p.add_argument("--climate-file", action="append", default=[], help="saved ha_logs.py --history output (repeatable)")
    p.add_argument("--sensor-file", action="append", default=[], help="saved history of a fine indoor sensor")
    p.add_argument("--climate-entity", help="fetch this climate entity's history now (needs HA_API_TOKEN)")
    p.add_argument("--sensor-entity", help="fetch this fine indoor sensor's history now")
    p.add_argument("--hours", type=int, default=72, help="hours of history to fetch (default 72; >24 supported)")
    p.add_argument("--chart-log", help="a saved climate_advisor_chart_log.json (accepted cycles only)")
    p.add_argument("--unit", choices=("fahrenheit", "celsius"), default="fahrenheit", help="unit HA reports history in")
    p.add_argument("--learned-heat", type=float, help="the learned heat swing to compare (F)")
    p.add_argument("--learned-cool", type=float, help="the learned cool swing to compare (F)")
    p.add_argument("--db-json", help="a saved learning DB to read the learned values from")
    p.add_argument("--from-db", action="store_true", help="read the learned values over SSH (like learning_db.py)")
    p.add_argument("--entry-id", default=None, help="zone config entry_id (multi-zone installs)")
    p.add_argument("--last", type=int, default=20, metavar="N", help="cycles to list per mode (default 20)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    learned: dict[str, float | None] = {}
    if args.learned_heat is not None:
        learned["heat"] = args.learned_heat
    if args.learned_cool is not None:
        learned["cool"] = args.learned_cool
    if args.db_json:
        learned.update(
            _learned_from_cache((json.loads(Path(args.db_json).read_text()).get("thermal_model_cache")) or {})
        )

    config = None
    if args.chart_log:
        raw = json.loads(Path(args.chart_log).read_text())
        audit = audit_chart_log(raw.get("entries", []) if isinstance(raw, dict) else raw)
    else:
        if args.climate_entity:
            from ha_logs import fetch_sensor_history, load_config

            config = load_config()
            clim_text = fetch_sensor_history(config, args.climate_entity, hours_back=args.hours)
            sens_text = (
                fetch_sensor_history(config, args.sensor_entity, hours_back=args.hours) if args.sensor_entity else ""
            )
        elif args.climate_file:
            clim_text = _read_files(args.climate_file)
            sens_text = _read_files(args.sensor_file) if args.sensor_file else ""
        else:
            print("ERROR: give --climate-file or --climate-entity (or --chart-log).", file=sys.stderr)
            return 2
        clim_rows = parse_climate_history(clim_text, args.unit)
        if not clim_rows:
            print(
                "ERROR: no climate history rows found (is the entity id right, and is the window recent?).",
                file=sys.stderr,
            )
            return 1
        sensor_rows = parse_sensor_history(sens_text, args.unit) if sens_text else None
        audit = audit_history(clim_rows, sensor_rows, args.unit)

    if args.from_db:
        from learning_db import fetch_learning_db
        from learning_db import load_config as _load

        db = fetch_learning_db(config or _load(), entry_id=args.entry_id)
        learned.update(_learned_from_cache(db.get("thermal_model_cache") or {}))
    print(format_report(audit, learned, last_n=args.last))
    return 0


if __name__ == "__main__":
    sys.exit(main())
