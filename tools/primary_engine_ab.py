#!/usr/bin/env python3
"""A/B one recorded day with v3 and with v4 as the primary prediction engine (Issue #1230 R6b).

Occupant view: the primary engine now decides when pre-heat/banking starts, whether the ceiling guard switches the AC
on ahead of the heat and when the windows are planned to close. This replays a REAL recorded day (Zone 1's chart log)
twice, identical in every input but ``config.primary_engine``, and reports what the house would have been told to do.

Two modes (both drive the production coordinator headless; the replay can also be written out for real Home Assistant):

* ``replay``: open loop. Every 30 minutes the recorded indoor/outdoor readings are fed with ``plan_cycle``; the
  thermostat never feeds back, so the DECISION stream is compared (commands, mode flips, ceiling-guard firings, TOU
  banking). The household promises (P1-P10) are judged by the production checks (``PromiseProbe``) on the same run.
* ``toy``: closed loop. A first-order house (``world.ToyHouse``, equipment gains and envelope halfway between the
  two engines' own estimates so neither is favoured) answers the thermostat while the recorded outdoor drives it;
  comfort minutes (outside the active band by more than 1 F), equipment minutes and mode flips are compared.

    python tools/primary_engine_ab.py --chart CHART_LOG.json --learning LEARNING.json --day 2026-10-28 --mode replay
    python tools/primary_engine_ab.py ... --write-scenarios DIR     # replay scenarios for the real-HA tier

Not part of the integration; reads files, writes nothing unless ``--write-scenarios`` is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMFORT = {
    "comfort_heat": 68,
    "comfort_cool": 74,
    "setback_heat": 60,
    "setback_cool": 80,
    "sleep_heat": 66,
    "sleep_cool": 72,
}
SLEEP_TIME, WAKE_TIME = "22:30", "06:30"
TICK_MIN = 5


def _naive(ts: str) -> datetime:
    """The recorded local wall-clock time, offset dropped: the harness reads naive times as its own clock."""
    return datetime.fromisoformat(ts).replace(tzinfo=None)


def load_rows(chart_path: str) -> list[dict]:
    return json.loads(Path(chart_path).read_text(encoding="utf-8"))["entries"]


def v3_thermal_model(learning_path: str) -> dict:
    """The learning engine's own thermal model for the zone, read from its saved cache (no learning engine needed)."""
    from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

    install_ha_stubs()
    from custom_components.climate_advisor.learning import LearningEngine  # noqa: PLC0415

    cache = json.loads(Path(learning_path).read_text(encoding="utf-8")).get("thermal_model_cache")
    return LearningEngine.get_thermal_model(SimpleNamespace(_state=SimpleNamespace(thermal_model_cache=cache)))  # type: ignore[arg-type]


def fit_v4(rows: list[dict], day: str) -> dict:
    """A v4 model fitted from the 30 days of chart log BEFORE ``day`` (the production fit, node = the CA reading)."""
    from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

    install_ha_stubs()
    from custom_components.climate_advisor import v4_shadow  # noqa: PLC0415
    from custom_components.climate_advisor.const import V4_FIT_TRAIN_DAYS  # noqa: PLC0415

    first = datetime.fromisoformat(next(r["ts"] for r in rows if r["ts"][:10] == day))
    prior = [r for r in rows if datetime.fromisoformat(r["ts"]) < first]
    model, seconds, line = v4_shadow.run_fit(
        chart_entries=prior,
        snapshots_fn=None,
        tz=first.tzinfo,
        now=first,
        primary="_ca",
        sensors=["_ca"],
        train_days=V4_FIT_TRAIN_DAYS,
    )
    print(f"# v4 fit before {day}: {line} ({seconds:.1f}s)", file=sys.stderr)
    return model


def _hourly_outdoor(rows: list[dict], start: datetime, hours: int) -> list[dict]:
    """Recorded outdoor temperature as an hourly forecast (a perfect forecast: both engines get the same weather)."""
    pts = sorted((_naive(r["ts"]), r["outdoor"]) for r in rows if r.get("outdoor") is not None)
    out = []
    for h in range(hours):
        t = start + timedelta(hours=h)
        near = [v for ts, v in pts if abs((ts - t).total_seconds()) <= 1800]
        if near:
            out.append({"datetime": t.isoformat() + "+00:00", "temperature": round(sum(near) / len(near), 1)})
    return out


class RecordedWeather:
    """Outdoor temperature interpolated from the recorded rows; sun is a daytime half sine."""

    def __init__(self, rows: list[dict]) -> None:
        self.pts = sorted((_naive(r["ts"]), float(r["outdoor"])) for r in rows if r.get("outdoor") is not None)

    def outdoor_f(self, t: datetime) -> float:
        pts = self.pts
        lo, hi = 0, len(pts) - 1
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[hi][0]:
            return pts[hi][1]
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if pts[mid][0] <= t else (lo, mid)
        (t0, v0), (t1, v1) = pts[lo], pts[hi]
        return v0 if t1 <= t0 else v0 + (v1 - v0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    @staticmethod
    def solar_factor(t: datetime) -> float:
        h = t.hour + t.minute / 60.0
        return math.sin(math.pi * (h - 8.0) / 10.0) if 8.0 <= h < 18.0 else 0.0


def _classification(day: str, rows: list[dict], at: datetime, indoor: float | None) -> dict:
    from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: PLC0415

    install_ha_stubs()
    from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day  # noqa: PLC0415

    def hl(d: str) -> tuple[float, float]:
        vals = [r["outdoor"] for r in rows if r["ts"][:10] == d and r.get("outdoor") is not None]
        return (max(vals), min(vals)) if vals else (70.0, 55.0)

    nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    (high, low), (t_high, t_low) = hl(day), hl(nxt)
    c = classify_day(
        ForecastSnapshot(
            today_high=high,
            today_low=low,
            tomorrow_high=t_high,
            tomorrow_low=t_low,
            current_outdoor_temp=low,
            current_indoor_temp=indoor,
            timestamp=at.replace(tzinfo=UTC),
        )
    )

    def hm(t: Any) -> str | None:
        return t.strftime("%H:%M") if t is not None else None

    return {
        "time": at.isoformat(timespec="seconds"),
        "type": "classification",
        "day_type": c.day_type,
        "hvac_mode": c.hvac_mode,
        "windows_recommended": c.windows_recommended,
        "today_high": c.today_high,
        "today_low": c.today_low,
        "tomorrow_high": c.tomorrow_high,
        "tomorrow_low": c.tomorrow_low,
        "pre_condition": c.pre_condition,
        "pre_condition_target": c.pre_condition_target,
        "window_open_time": hm(c.window_open_time),
        "window_close_time": hm(c.window_close_time),
    }


def base_scenario(rows: list[dict], day: str, primary: str, v3: dict, v4: dict) -> dict:
    start = datetime.fromisoformat(day + "T00:00:00")
    return {
        "name": f"ab_{day}_{primary}",
        "description": f"Recorded Zone 1 day {day}, primary engine {primary}.",
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "config": {
            **COMFORT,
            "sleep_time": SLEEP_TIME,
            "wake_time": WAKE_TIME,
            "primary_engine": primary,
            "temp_unit": "fahrenheit",
        },
        "thermal_model": v3,
        "v4_model": {"model": v4},
        "hourly_forecast": _hourly_outdoor(rows, start, 48),
        "events": [],
        "_start": start,
    }


def replay_scenario(rows: list[dict], day: str, primary: str, v3: dict, v4: dict) -> dict:
    sc = base_scenario(rows, day, primary, v3, v4)
    start: datetime = sc.pop("_start")
    todays = [r for r in rows if r["ts"][:10] == day and r.get("indoor") is not None and r.get("outdoor") is not None]
    first_indoor = todays[0]["indoor"]
    events = [
        {
            "time": start.isoformat(timespec="seconds"),
            "type": "temp_update",
            "indoor_f": first_indoor,
            "outdoor_f": todays[0]["outdoor"],
        },
        _classification(day, rows, start + timedelta(minutes=1), first_indoor),
    ]
    for k in range(1, 48):
        t = start + timedelta(minutes=30 * k)
        near = min(todays, key=lambda r: abs((_naive(r["ts"]) - t).total_seconds()))
        events.append(
            {
                "time": t.isoformat(timespec="seconds"),
                "type": "temp_update",
                "indoor_f": near["indoor"],
                "outdoor_f": near["outdoor"],
                "plan_cycle": True,
            }
        )
    sc["events"] = events
    return sc


class _GuardLog(logging.Handler):
    """Counts the ceiling guard's own log lines during a run (what it predicted, held back, or did)."""

    KEYS = (
        "breach predicted",
        "standing by",
        "dormant",
        "skipped",
        "switch",
        "escalat",
        "Decisions now use",
        "no usable fit",
    )

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.counts: dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "ODE ceiling guard" in msg or "Decisions now use" in msg or "no usable fit" in msg:
            for k in self.KEYS:
                if k in msg:
                    self.counts[k] = self.counts.get(k, 0) + 1
                    break


def summarise(result: Any) -> dict:
    cmds = [
        (
            a.get("service"),
            json.dumps({k: v for k, v in (a.get("data") or {}).items() if k != "entity_id"}, sort_keys=True),
        )
        for a in result.action_log
        if a.get("domain") == "climate"
    ]
    modes = [json.loads(d).get("hvac_mode") for s, d in cmds if s == "set_hvac_mode"]
    flips = sum(1 for a, b in zip(modes, modes[1:], strict=False) if a != b)
    kinds: dict[str, int] = {}
    for ev_type, _payload, _ts in result.event_log:
        if ev_type in ("ceiling_guard_fired", "tou_precondition_applied", "comfort_band_applied", "nat_vent_started"):
            kinds[ev_type] = kinds.get(ev_type, 0) + 1
    report = result.promise_report
    promises = None
    if report is not None:
        promises = {
            "hard": [f"{h.key}" for h in report.hard],
            "warnings": [f"{h.key}" for h in report.warnings],
            "hits_by_promise": dict(sorted(report.stats.items())),
        }
    return {
        "commands": len(cmds),
        "set_temperature": sum(1 for s, _ in cmds if s == "set_temperature"),
        "set_hvac_mode": len(modes),
        "mode_flips": flips,
        "events": kinds,
        "promises": promises,
        "command_list": cmds,
    }


def run_replay(rows: list[dict], day: str, v3: dict, v4: dict) -> dict[str, dict]:
    from tools.sim_harness.promise_probe import PromiseProbe  # noqa: PLC0415
    from tools.sim_harness.run_production import run_production_scenario  # noqa: PLC0415

    out = {}
    for primary in ("v3", "v4"):
        sc = replay_scenario(rows, day, primary, v3, v4)
        guard = _GuardLog()
        logging.getLogger("custom_components.climate_advisor").addHandler(guard)
        logging.getLogger("custom_components.climate_advisor").setLevel(logging.DEBUG)
        try:
            result = run_production_scenario(sc, use_coordinator=True, probe=PromiseProbe(scenario_name=sc["name"]))
        finally:
            logging.getLogger("custom_components.climate_advisor").removeHandler(guard)
        out[primary] = {**summarise(result), "guard_log": dict(sorted(guard.counts.items()))}
    out["identical_commands"] = out["v3"]["command_list"] == out["v4"]["command_list"]  # type: ignore[assignment]
    return out


def run_toy(rows: list[dict], day: str, v3: dict, v4: dict) -> dict[str, dict]:
    from custom_components.climate_advisor import predictor_v4  # noqa: PLC0415
    from tools.sim_harness.promise_probe import PromiseProbe  # noqa: PLC0415
    from tools.sim_harness.session import make_session  # noqa: PLC0415
    from tools.sim_harness.world import HouseParams, ToyHouse  # noqa: PLC0415

    rates = predictor_v4.control_rates(predictor_v4.from_dict(v4))
    mean = lambda a, b: (abs(a) + abs(b)) / 2.0  # noqa: E731
    params = HouseParams(
        k_env_per_hr=round(mean(v3["k_passive"], -rates["k_passive"]), 4),
        solar_f_hr=round(v3.get("k_solar") or 0.5, 2),
        heat_gain_f_hr=round(mean(v3["k_active_heat"], rates["k_active_heat"]), 2),
        cool_gain_f_hr=round(mean(v3["k_active_cool"], rates["k_active_cool"]), 2),
        swing_f=1.0,
        overshoot_f=0.3,
        whole_degree=True,
    )
    weather = RecordedWeather(rows)
    out: dict[str, dict] = {}
    for primary in ("v3", "v4"):
        sc = replay_scenario(rows, day, primary, v3, v4)
        start = datetime.fromisoformat(day + "T00:00:00")
        first_indoor = float(sc["events"][0]["indoor_f"])
        house = ToyHouse(params, first_indoor)
        construct = {**sc, "events": [sc["events"][0]]}
        probe = PromiseProbe(scenario_name=sc["name"])
        session = make_session(construct, engine="fakehass", probe=probe, use_coordinator=True)
        coord = getattr(session, "coordinator", None)
        if coord is not None and not hasattr(coord, "async_refresh") and hasattr(coord, "async_request_refresh"):
            coord.async_refresh = coord.async_request_refresh
        session.fake_hass.set_action_deadband(session.climate_entity, None)
        view = session.thermostat()
        last_action: list[str | None] = [None]
        stats = {"cold_min": 0, "hot_min": 0, "heat_min": 0, "cool_min": 0}

        def feed(ev: dict, _s: Any = session) -> None:
            _s.advance_to(datetime.fromisoformat(ev["time"]).replace(tzinfo=UTC))
            _s.feed(ev)

        def report_action(t: datetime, _h: ToyHouse = house, _v: Any = view, _last: list = last_action) -> None:
            prev = _h.running
            action = _h.decide_action(_v.mode, _v.setpoint_f)
            _h.coast(prev)
            if action != _last[0]:
                _last[0] = action
                feed(
                    {
                        "time": t.isoformat(timespec="seconds"),
                        "type": "thermostat_state_changed",
                        "hvac_mode": _v.mode or "off",
                        "hvac_action": action,
                    }
                )

        try:
            feed(sc["events"][0])
            report_action(start)
            feed(sc["events"][1])
            report_action(start + timedelta(minutes=1))
            for i in range(1, 24 * 60 // TICK_MIN + 1):
                t = start + timedelta(minutes=i * TICK_MIN)
                session.advance_to(t.replace(tzinfo=UTC))
                house.step(TICK_MIN, weather.outdoor_f(t), weather.solar_factor(t), False)
                minute = t.hour * 60 + t.minute
                asleep = minute >= 22 * 60 + 30 or minute < 6 * 60 + 30
                lo, hi = (
                    (COMFORT["sleep_heat"], COMFORT["sleep_cool"])
                    if asleep
                    else (COMFORT["comfort_heat"], COMFORT["comfort_cool"])
                )
                shown = house.reported_f()
                stats["cold_min"] += TICK_MIN if shown < lo - 1.0 else 0
                stats["hot_min"] += TICK_MIN if shown > hi + 1.0 else 0
                stats["heat_min"] += TICK_MIN if house.running == "heating" else 0
                stats["cool_min"] += TICK_MIN if house.running == "cooling" else 0
                ev = {
                    "time": t.isoformat(timespec="seconds"),
                    "type": "temp_update",
                    "indoor_f": shown,
                    "outdoor_f": round(weather.outdoor_f(t), 1),
                }
                if t.minute % 30 == 0:
                    ev["plan_cycle"] = True
                feed(ev)
                report_action(t)
            result = session.finish()
        finally:
            session.close()
        out[primary] = {**summarise(result), "comfort": stats, "plant": params.to_dict()}
    out["identical_commands"] = out["v3"]["command_list"] == out["v4"]["command_list"]  # type: ignore[assignment]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chart", required=True)
    ap.add_argument("--learning", required=True)
    ap.add_argument("--day", required=True, help="local date YYYY-MM-DD present in the chart log")
    ap.add_argument("--mode", choices=("replay", "toy"), default="replay")
    ap.add_argument("--write-scenarios", metavar="DIR")
    ap.add_argument("--verbose", action="store_true", help="include the full command lists")
    args = ap.parse_args()
    rows = load_rows(args.chart)
    v3 = v3_thermal_model(args.learning)
    v4 = fit_v4(rows, args.day)
    if args.write_scenarios:
        out = Path(args.write_scenarios)
        out.mkdir(parents=True, exist_ok=True)
        for primary in ("v3", "v4"):
            sc = replay_scenario(rows, args.day, primary, v3, v4)
            (out / f"ab_{args.day}_{primary}.json").write_text(json.dumps(sc, indent=1), encoding="utf-8")
        print(f"wrote scenarios to {out}")
        return 0
    res = (run_replay if args.mode == "replay" else run_toy)(rows, args.day, v3, v4)
    if not args.verbose:
        for r in res.values():
            if isinstance(r, dict):
                r.pop("command_list", None)
    print(json.dumps({"day": args.day, "mode": args.mode, "result": res}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
