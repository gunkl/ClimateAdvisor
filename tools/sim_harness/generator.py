"""generator - seeded random days, run closed-loop against the production engine (Issue #1128 I3).

A *plan* (pure data, drawn from ``random.Random(day_seed)``) describes one household day: the
configuration, the weather, who is home, which doors open, what the occupant pokes at.  The
*driver* walks that day in five-minute ticks against a ``Session`` (the production engine under
FakeHass), with a toy house responding to whatever the thermostat is set to, and records every
event it fed.  The recorded events ARE the replay artifact: an ordinary scenario file (same
schema as the goldens) that ``run_production_scenario`` plays back deterministically with no
generator involved.

The generator knows nothing about any promise or past bug.  Promises are judged by the production
registry through ``PromiseProbe`` (``promise_probe.py``); this module only produces days.

Identity: ``day_seed = master_seed * DAYS_PER_MASTER + index``.  ``master_seed`` is what a trial
or CI run is parameterised by; ``index`` is the day within it.  One-command replay of a finding:

    python tools/simulate.py --replay-generated <file>
    python tools/simulate.py --generate-day <day_seed> [--profile ...]
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# promise_probe installs the Home Assistant stubs that the production imports below need.
from tools.sim_harness.promise_probe import PromiseProbe, PromiseReport
from tools.sim_harness.session import make_session
from tools.sim_harness.world import TICK_MIN, HouseParams, ToyHouse, Weather

GENERATOR_VERSION = 1
# Calibrated default for the unit-tier smoke run (Issue #1128 I5): fixed master seeds (derandomized, so an
# unrelated PR is never blocked by a newly lucky seed) x days.  ~0.43 s/day => ~45 s.  On the pre-fix #1073
# tree P1 was found within 50 days on 5 of 5 master seeds (first hits at day index 0, 26, 28, 34, 48).
CI_SMOKE_SEEDS = (101, 102)
CI_SMOKE_DAYS = 50
DAYS_PER_MASTER = 1_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_DIR = REPO_ROOT / "tools" / "simulations" / "generated"
FAILURES_DIR = GENERATED_DIR / "failures"

DAY0 = datetime(2026, 5, 12, 5, 0, 0)  # naive == UTC in the harness; the day starts before dawn
SIM_HOURS = 24
PROFILES = ("coordinator", "engine", "mixed")


def day_seed_of(master_seed: int, index: int) -> int:
    return master_seed * DAYS_PER_MASTER + index


# ═══════════════════════════════════════════════════════════════════════════════
# Plan
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class DayPlan:
    day_seed: int
    profile: str
    use_coordinator: bool
    unit: str  # "fahrenheit" | "celsius"
    config: dict[str, Any]
    house: dict[str, Any]
    forecast: dict[str, float]
    cloud: float
    wobble_f: float
    indoor_start_f: float
    reclassify_every_min: int | None
    # (minute_since_start, kind, args) - sparse exogenous events, all drawn up front
    timed: list[tuple[float, str, dict[str, Any]]] = field(default_factory=list)
    # Issue #1144: the whole-house-fan hazard drawn for this day (None = none).  Records what it replaced
    # so the pinned "earlier draws unchanged" digest can strip it; absent from to_dict() when None.
    whf: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        d = {
            "day_seed": self.day_seed,
            "profile": self.profile,
            "use_coordinator": self.use_coordinator,
            "unit": self.unit,
            "config": self.config,
            "house": self.house,
            "forecast": self.forecast,
            "cloud": self.cloud,
            "wobble_f": self.wobble_f,
            "indoor_start_f": self.indoor_start_f,
            "reclassify_every_min": self.reclassify_every_min,
            "timed": [[m, k, a] for m, k, a in self.timed],
        }
        if self.whf is not None:
            d["whf"] = self.whf
        return d


def _hhmm(minute_of_day: int) -> str:
    minute_of_day %= 1440
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


# Issue #1144: share of coordinator days that carry a whole-house fan, and the occupant orders drawn for it.
WHF_DAY_RATE = 0.35
WHF_ENTITY = "fan.whf"
# Seconds of AC/fan overlap the generated-day mutex check tolerates: one toy-thermostat reporting interval.  The
# toy house reports its equipment state on its own five-minute tick (and right after an exogenous event), so a
# command CA sends from a timer between ticks (a grace expiry re-arming the fan and turning HVAC off) shows as
# AC still running until the next tick.  Production thermostats report within seconds.
WHF_P2_OVERLAP_S = float(TICK_MIN * 60)
WHF_ORDERS = (
    "sealed_then_fan_on",  # every sensor closed, then the occupant switches the fan on
    "fan_on_then_sealed",  # the fan runs with a window open, then every sensor closes
    "fan_on_then_open_fast",  # sealed, fan on, a window opens within 10 s (one motion: must not be fought)
    "fan_on_then_open_late",  # sealed, fan on, a window opens 10-30 s later
)


def _apply_whf_hazard(
    config: dict[str, Any],
    timed: list[tuple[float, str, dict[str, Any]]],
    *,
    mode_roll: float,
    order_roll: float,
    when_roll: float,
    lag_roll: float,
) -> dict[str, Any]:
    """Turn the day into a whole-house-fan day (mutates ``config`` and ``timed``); return what it replaced.

    The fan is switched on by the occupant (HomeKit / wall switch: an external state change with no CA
    context), in one of the ``WHF_ORDERS``.  The house always has at least one door/window sensor here:
    with none, sealed-ness cannot be judged and the safety rule does not apply.
    """
    record = {
        "pre_fan_mode": config["fan_mode"],
        "pre_sensors": list(config["door_window_sensors"]),
    }
    config["fan_mode"] = "whole_house_fan" if mode_roll < 0.6 else "both"
    config["fan_entity"] = WHF_ENTITY
    config["fan_state_feedback"] = True
    config["promise_p2_min_overlap_s"] = WHF_P2_OVERLAP_S
    if not config["door_window_sensors"]:
        config["door_window_sensors"] = ["binary_sensor.door_window_1"]
    sensors = list(config["door_window_sensors"])
    order = WHF_ORDERS[min(int(order_roll * len(WHF_ORDERS)), len(WHF_ORDERS) - 1)]
    record["order"] = order
    t0 = 120.0 + float(int(when_roll * (1200 - 120)))  # whole minute, 02:00 .. 20:00 into the day

    timed.append((0.0, "external_fan_state_change", {"entity": WHF_ENTITY, "state": "off"}))
    if order == "fan_on_then_sealed":
        timed.append((t0 - 1.0, "sensor_open", {"entity": sensors[0]}))
        timed.append((t0, "external_fan_state_change", {"entity": WHF_ENTITY, "state": "on"}))
        lag_min = 1.0 + float(int(lag_roll * 30))
        for entity in sensors:
            timed.append((t0 + lag_min, "sensor_close", {"entity": entity}))
        record["sealed_after_min"] = lag_min
        return record
    for entity in sensors:
        timed.append((t0 - 1.0, "sensor_close", {"entity": entity}))
    timed.append((t0, "external_fan_state_change", {"entity": WHF_ENTITY, "state": "on"}))
    if order == "fan_on_then_open_fast":
        lag_s = 1.0 + float(int(lag_roll * 9))  # 1 .. 9 s
        timed.append((t0 + lag_s / 60.0, "sensor_open", {"entity": sensors[0]}))
        record["window_open_after_s"] = lag_s
    elif order == "fan_on_then_open_late":
        lag_s = 10.0 + float(int(lag_roll * 20))  # 10 .. 29 s
        timed.append((t0 + lag_s / 60.0, "sensor_open", {"entity": sensors[0]}))
        record["window_open_after_s"] = lag_s
    return record


def strip_whf_hazard(plan_dict: dict) -> dict:
    """Undo the whole-house-fan hazard in a ``DayPlan.to_dict()`` (for the pinned earlier-draws digest)."""
    whf = plan_dict.pop("whf", None)
    if whf is None:
        return plan_dict
    plan_dict["config"]["fan_mode"] = whf["pre_fan_mode"]
    plan_dict["config"]["door_window_sensors"] = whf["pre_sensors"]
    plan_dict["config"].pop("fan_entity", None)
    plan_dict["config"].pop("fan_state_feedback", None)
    plan_dict["config"].pop("promise_p2_min_overlap_s", None)
    # the hazard's own events: the fan switches and the extra sensor events it appended
    n_pre = len(whf["pre_sensors"])
    plan_dict["timed"] = _strip_whf_events(plan_dict["timed"], whf, n_pre)
    return plan_dict


def _strip_whf_events(timed: list, whf: dict, n_pre: int) -> list:
    """Remove exactly the events ``_apply_whf_hazard`` appended (they are the trailing ones by construction)."""
    marker = [t for t in timed if t[1] == "external_fan_state_change"]
    out = [t for t in timed if t[1] != "external_fan_state_change"]
    order = whf["order"]
    sensors = whf["pre_sensors"] or ["binary_sensor.door_window_1"]
    drop: list = []
    t_on = next(t[0] for t in marker if t[2]["state"] == "on")
    if order == "fan_on_then_sealed":
        drop.append([t_on - 1.0, "sensor_open", {"entity": sensors[0]}])
        drop += [[t_on + whf["sealed_after_min"], "sensor_close", {"entity": e}] for e in sensors]
    else:
        drop += [[t_on - 1.0, "sensor_close", {"entity": e}] for e in sensors]
        if "window_open_after_s" in whf:
            drop.append([t_on + whf["window_open_after_s"] / 60.0, "sensor_open", {"entity": sensors[0]}])
    for d in drop:
        out.remove(d)
    return out


def build_plan(day_seed: int, profile: str = "coordinator") -> DayPlan:
    """Draw one household day.  Pure function of ``(day_seed, profile)``."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; choose from {PROFILES}")
    rng = random.Random(f"plan:{day_seed}:{profile}")

    use_coordinator = {"engine": False, "coordinator": True, "mixed": rng.random() < 0.8}[profile]
    unit = "celsius" if rng.random() < 0.15 else "fahrenheit"

    # ── forecast: spans every day type (cold ... hot) ──
    high = round(rng.uniform(38.0, 99.0), 1)
    low = round(high - rng.uniform(12.0, 32.0), 1)
    tomorrow_high = round(high + rng.gauss(0.0, 6.0), 1)
    tomorrow_low = round(tomorrow_high - rng.uniform(12.0, 32.0), 1)

    # ── configuration (defaults most of the time, clamps explored the rest) ──
    comfort_heat = float(rng.choice([66, 67, 68, 68, 68, 69, 70, 72]))
    comfort_cool = float(comfort_heat + rng.choice([4, 5, 6, 6, 7, 8, 10]))
    sleep_heat = float(comfort_heat - rng.choice([0, 2, 4, 4, 6]))
    sleep_cool = float(comfort_cool - rng.choice([0, 2, 2, 4]))
    sleep_minute = rng.choice([21 * 60 + 30, 22 * 60, 22 * 60 + 30, 23 * 60, 23 * 60 + 59])
    wake_minute = rng.choice([5 * 60 + 30, 6 * 60, 6 * 60 + 30, 7 * 60, 8 * 60 + 30])
    config: dict[str, Any] = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "sleep_heat": sleep_heat,
        "sleep_cool": sleep_cool,
        "sleep_time": _hhmm(sleep_minute),
        "wake_time": _hhmm(wake_minute),
        "setback_heat": comfort_heat - 8.0,
        "setback_cool": comfort_cool + 6.0,
        "initial_thermostat_mode": rng.choice(["off", "off", "heat", "cool"]),
        "temp_unit": unit,
        "fan_mode": rng.choice(["disabled", "disabled", "hvac_fan"]),
        "manual_grace_seconds": rng.choice([900, 900, 900, 1800, 0]),
    }
    if rng.random() < 0.25:
        for key, lo, hi in (
            ("comfort_deadband_hot_f", 2.0, 8.0),
            ("comfort_deadband_warm_f", 1.0, 5.0),
            ("comfort_deadband_mild_f", 1.0, 5.0),
            ("comfort_deadband_cool_f", 1.0, 5.0),
            ("comfort_deadband_cold_f", 2.0, 8.0),
        ):
            config[key] = float(rng.randint(int(lo), int(hi)))
    if rng.random() < 0.25:
        config["comfort_family_recency_window_min"] = float(rng.choice([30, 60, 120, 240]))
    if rng.random() < 0.2:
        config["aggressive_savings"] = True
    sensors: list[str] = []
    if rng.random() < 0.45:
        sensors = [f"binary_sensor.door_window_{i + 1}" for i in range(rng.randint(1, 3))]
    config["door_window_sensors"] = sensors

    house = HouseParams.draw(rng)
    indoor_start = round(rng.uniform(comfort_heat - 2.0, comfort_cool + 2.0), 1)
    reclassify = rng.choice([None, 30, 30])
    if use_coordinator:
        # Production always runs the regular 30-minute coordinator cycle (apply_classification); a day
        # without it is unrealistic and produced false findings.  The draw above is kept (result ignored)
        # so every other draw of every seed stays identical.
        reclassify = 30

    timed: list[tuple[int, str, dict[str, Any]]] = []
    start_minute = DAY0.hour * 60 + DAY0.minute

    def at_clock(minute_of_day: int) -> int:
        m = minute_of_day - start_minute
        return m if m >= 0 else m + 1440

    timed.append((at_clock(wake_minute), "wakeup", {}))
    timed.append((at_clock(sleep_minute), "bedtime", {}))

    # occupancy: away for a few hours (or, rarely, vacation)
    if rng.random() < 0.3:
        t0 = rng.randint(60, 14 * 60)
        mode = "vacation" if rng.random() < 0.15 else "away"
        timed.append((t0, "occupancy_change", {"mode": mode}))
        timed.append((t0 + rng.randint(90, 480), "occupancy_change", {"mode": "home"}))
    # doors and windows: open for minutes to hours, partial closes
    for entity in sensors:
        for _ in range(rng.choice([0, 0, 1, 2])):
            t0 = rng.randint(30, 20 * 60)
            timed.append((t0, "sensor_open", {"entity": entity}))
            timed.append((t0 + rng.randint(5, 180), "sensor_close", {"entity": entity}))
    if use_coordinator:
        # the occupant touches the thermostat; a restart now and then
        for _ in range(rng.choice([0, 1, 1, 2])):
            t0 = rng.randint(60, 20 * 60)
            mode = rng.choice(["heat", "cool", "off"])
            sp = float(rng.randint(int(comfort_heat) - 3, int(comfort_cool) + 3))
            timed.append((t0, "thermostat_state_changed", {"hvac_mode": mode, "setpoint_f": sp}))
        if rng.random() < 0.1:
            timed.append((rng.randint(120, 18 * 60), "simulate_restart", {}))
    # Drawn LAST (after every draw above, including cloud/wobble below) so every seed's earlier
    # events stay exactly as they were before these hazards existed.
    cloud = round(rng.uniform(0.0, 0.8), 2)
    wobble_f = round(rng.uniform(0.2, 0.9), 2)
    # Hazards at realistic low rates: the occupant flips the automation switch off (and usually
    # back on later); the indoor temperature source drops out for a while.
    if rng.random() < 0.07:
        # Switched off around something the schedule does anyway (wake-up, bedtime, a door, an
        # occupancy change): a switch flipped in a quiet stretch tests nothing.
        anchor = rng.choice([m for m, _, _ in timed])
        t0 = max(1, anchor - rng.randint(5, 120))
        timed.append((t0, "automation_toggle", {"enabled": False}))
        if rng.random() < 0.85:
            timed.append((max(anchor + 1, t0 + 30) + rng.randint(0, 240), "automation_toggle", {"enabled": True}))
    if rng.random() < 0.08:
        # The sensor drops out around something the house does anyway (a window opening, a
        # schedule step): an outage in a quiet stretch tests nothing.
        anchor = rng.choice([m for m, _, _ in timed])
        t0 = max(1, anchor - rng.randint(5, 60))
        timed.append((t0, "indoor_unreadable", {"unreadable": True}))
        timed.append((anchor + rng.randint(10, 180), "indoor_unreadable", {"unreadable": False}))
    # Issue #1144 whole-house-fan hazards: drawn LAST (after the hazards above) so every earlier draw of
    # every seed is unchanged.  A fixed number of draws is consumed whether or not the hazard fires.
    whf_roll, whf_mode_roll, whf_order_roll, whf_when_roll, whf_lag_roll = (rng.random() for _ in range(5))
    whf: dict[str, Any] | None = None
    if use_coordinator and whf_roll < WHF_DAY_RATE:
        whf = _apply_whf_hazard(
            config,
            timed,
            mode_roll=whf_mode_roll,
            order_roll=whf_order_roll,
            when_roll=whf_when_roll,
            lag_roll=whf_lag_roll,
        )
    timed.sort(key=lambda x: x[0])

    return DayPlan(
        day_seed=day_seed,
        profile=profile,
        use_coordinator=use_coordinator,
        unit=unit,
        config=config,
        house=house.to_dict(),
        forecast={"high": high, "low": low, "tomorrow_high": tomorrow_high, "tomorrow_low": tomorrow_low},
        cloud=cloud,
        wobble_f=wobble_f,
        indoor_start_f=indoor_start,
        reclassify_every_min=reclassify,
        timed=timed,
        whf=whf,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Driver
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class RunRecord:
    plan: DayPlan
    scenario: dict
    report: PromiseReport
    seconds: float
    callback_errors: int = 0

    @property
    def hard(self):
        return self.report.hard

    @property
    def found(self) -> list[str]:
        return sorted({h.promise for h in self.report.hard})


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _classification_event(plan: DayPlan, now: datetime, indoor_f: float | None) -> dict:
    """The day's classification, produced by the REAL classifier (no field is hand-set)."""
    from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day  # noqa: PLC0415

    f = plan.forecast
    c = classify_day(
        ForecastSnapshot(
            today_high=f["high"],
            today_low=f["low"],
            tomorrow_high=f["tomorrow_high"],
            tomorrow_low=f["tomorrow_low"],
            current_outdoor_temp=f["low"],
            current_indoor_temp=indoor_f,
            timestamp=now.replace(tzinfo=UTC),
        )
    )

    def hm(t: Any) -> str | None:
        return t.strftime("%H:%M") if t is not None else None

    return {
        "time": _iso(now),
        "type": "classification",
        "day_type": c.day_type,
        "hvac_mode": c.hvac_mode,
        "windows_recommended": c.windows_recommended,
        "setback_modifier": c.setback_modifier,
        "trend_direction": c.trend_direction,
        "trend_magnitude": c.trend_magnitude,
        "today_high": c.today_high,
        "today_low": c.today_low,
        "tomorrow_high": c.tomorrow_high,
        "tomorrow_low": c.tomorrow_low,
        "pre_condition": c.pre_condition,
        "pre_condition_target": c.pre_condition_target,
        "window_open_time": hm(c.window_open_time),
        "window_close_time": hm(c.window_close_time),
        "window_opportunity_morning": c.window_opportunity_morning,
        "window_opportunity_evening": c.window_opportunity_evening,
        "window_opportunity_morning_start": hm(c.window_opportunity_morning_start),
        "window_opportunity_morning_end": hm(c.window_opportunity_morning_end),
        "window_opportunity_evening_start": hm(c.window_opportunity_evening_start),
        "window_opportunity_evening_end": hm(c.window_opportunity_evening_end),
    }


def _to_unit(f: float, unit: str, *, fine: bool) -> float:
    if unit == "fahrenheit":
        return f
    c = (f - 32.0) * 5.0 / 9.0
    return round(c, 1) if fine else round(c * 2) / 2.0


def _from_unit(v: float, unit: str) -> float:
    return v if unit == "fahrenheit" else v * 9.0 / 5.0 + 32.0


def skeleton_scenario(plan: DayPlan) -> dict:
    """Scenario header shared by the live run and the saved replay file."""
    return {
        "name": f"generated_{plan.day_seed}",
        "description": (
            f"Generated household day (seed {plan.day_seed}, profile {plan.profile}, "
            f"high {plan.forecast['high']}F, {plan.unit}); replayable without the generator."
        ),
        "config": dict(plan.config),
        "use_coordinator": plan.use_coordinator,
        "skip_startup_coalesce": True,
        "generated": {
            "generator_version": GENERATOR_VERSION,
            "day_seed": plan.day_seed,
            "master_seed": plan.day_seed // DAYS_PER_MASTER,
            "profile": plan.profile,
            "plan": plan.to_dict(),
        },
        "events": [],
    }


def patch_stub_gaps(session: Any) -> None:
    """The stub ``DataUpdateCoordinator`` has no ``async_refresh`` (only ``async_request_refresh``, see
    run_production's ``coordinator_refresh`` note); production's pre-cool timer calls it.  Alias it so a
    harness gap is not reported as a production callback error.  Touches the stub object only."""
    coord = getattr(session, "coordinator", None)
    if coord is not None and not hasattr(coord, "async_refresh") and hasattr(coord, "async_request_refresh"):
        coord.async_refresh = coord.async_request_refresh


def run_plan(plan: DayPlan, *, probe: bool = True) -> RunRecord:
    """Drive one planned day closed-loop against the production engine; return the record."""
    t_wall = time.perf_counter()
    scenario = skeleton_scenario(plan)
    house = ToyHouse(HouseParams(**plan.house), plan.indoor_start_f)
    weather = Weather(
        random.Random(f"weather:{plan.day_seed}"),
        high=plan.forecast["high"],
        low=plan.forecast["low"],
        tomorrow_high=plan.forecast["tomorrow_high"],
        tomorrow_low=plan.forecast["tomorrow_low"],
        cloud=plan.cloud,
        wobble_f=plan.wobble_f,
        day0=DAY0,
    )
    unit = plan.unit
    fine = not plan.house["whole_degree"]
    events: list[dict] = []

    first = _classification_event(plan, DAY0, plan.indoor_start_f)
    # The session derives its virtual start from the first event; give it one, then feed it for real.
    construct = dict(scenario)
    construct["events"] = [first]
    pr = PromiseProbe(scenario_name=scenario["name"]) if probe else None
    session = make_session(construct, engine="fakehass", probe=pr, use_coordinator=plan.use_coordinator)
    try:
        patch_stub_gaps(session)
        view = session.thermostat()
        open_sensors: set[str] = set()
        last_action: str | None = None
        timed = list(plan.timed)
        ti = 0

        def feed(ev: dict) -> None:
            nonlocal last_action
            session.advance_to(datetime.fromisoformat(ev["time"]).replace(tzinfo=UTC))
            session.feed(ev)
            events.append(ev)

        def setpoint_f() -> float | None:
            sp = view.setpoint_f
            return None if sp is None else _from_unit(sp, unit)

        indoor_down = [False]

        def report_indoor(t: datetime) -> None:
            if indoor_down[0]:
                return  # the sensor is out: nothing reaches Home Assistant
            feed(
                {
                    "time": _iso(t),
                    "type": "temp_update",
                    "indoor_f": _to_unit(house.reported_f(), unit, fine=fine),
                    "outdoor_f": round(weather_now[0], 1),
                }
            )

        def report_action(t: datetime) -> None:
            nonlocal last_action
            prev = house.running
            action = house.decide_action(view.mode, setpoint_f())
            house.coast(prev)
            if action != last_action:
                last_action = action
                feed(
                    {
                        "time": _iso(t),
                        "type": "thermostat_state_changed",
                        "hvac_mode": view.mode or "off",
                        "hvac_action": action,
                    }
                )

        weather_now = [weather.outdoor_f(DAY0)]
        # The thermostat reports its equipment state from the first moment (a real one always does);
        # then the day's classification arrives, as it does on the first coordinator cycle.
        report_indoor(DAY0)
        report_action(DAY0)
        feed({**first})
        report_action(DAY0)

        n_ticks = SIM_HOURS * 60 // TICK_MIN
        for i in range(1, n_ticks + 1):
            t = DAY0 + timedelta(minutes=i * TICK_MIN)
            # exogenous events that fall due before this tick
            while ti < len(timed) and DAY0 + timedelta(minutes=timed[ti][0]) <= t:
                minute, kind, args = timed[ti]
                ti += 1
                et = DAY0 + timedelta(minutes=minute)
                if kind in ("sensor_open", "sensor_close"):
                    (open_sensors.add if kind == "sensor_open" else open_sensors.discard)(args["entity"])
                    feed({"time": _iso(et), "type": kind, "entity": args["entity"]})
                elif kind == "occupancy_change":
                    feed({"time": _iso(et), "type": kind, "mode": args["mode"]})
                elif kind == "external_fan_state_change":
                    feed({"time": _iso(et), "type": kind, "entity": args["entity"], "state": args["state"]})
                elif kind == "thermostat_state_changed":
                    if plan.use_coordinator:
                        feed(
                            {
                                "time": _iso(et),
                                "type": kind,
                                "hvac_mode": args["hvac_mode"],
                                "temperature": _to_unit(args["setpoint_f"], unit, fine=fine),
                                "hvac_action": house.decide_action(args["hvac_mode"], args["setpoint_f"]) or "idle",
                            }
                        )
                        last_action = None  # re-report the equipment state after the poke
                elif kind == "automation_toggle":
                    feed({"time": _iso(et), "type": kind, "enabled": bool(args["enabled"])})
                elif kind == "indoor_unreadable":
                    indoor_down[0] = bool(args["unreadable"])
                    ev = {"time": _iso(et), "type": kind, "unreadable": indoor_down[0]}
                    if not indoor_down[0]:
                        ev["indoor_f"] = _to_unit(house.reported_f(), unit, fine=fine)
                    feed(ev)
                elif kind == "simulate_restart":
                    if plan.use_coordinator:
                        feed({"time": _iso(et), "type": kind})
                else:  # bedtime / wakeup
                    feed({"time": _iso(et), "type": kind})
                if plan.whf is not None:
                    # A real thermostat reports its equipment state within seconds of a command; the toy house
                    # otherwise reports it on the next five-minute tick, which would read as minutes of AC running
                    # beside the whole-house fan that the occupant never had (Issue #1144).
                    report_action(et)
            if plan.reclassify_every_min and (i * TICK_MIN) % plan.reclassify_every_min == 0:
                feed(_classification_event(plan, t, house.reported_f()))
            weather_now[0] = weather.outdoor_f(t)
            house.step(TICK_MIN, weather_now[0], weather.solar_factor(t), bool(open_sensors))
            report_indoor(t)
            report_action(t)

        result = session.finish()
        report = result.promise_report if result.promise_report is not None else PromiseReport()
        errors = len(result.callback_errors)
    finally:
        session.close()
    scenario["events"] = events
    return RunRecord(plan, scenario, report, time.perf_counter() - t_wall, errors)


# ═══════════════════════════════════════════════════════════════════════════════
# Batches, findings, replay
# ═══════════════════════════════════════════════════════════════════════════════
def run_day(day_seed: int, profile: str = "coordinator") -> RunRecord:
    return run_plan(build_plan(day_seed, profile))


@dataclass
class BatchResult:
    master_seed: int
    days: int
    seconds: float
    # promise key -> list of (day_seed, hit)
    hits: dict[str, list[tuple[int, Any]]] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    callback_error_days: list[int] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    coordinator_days: int = 0
    day_types: dict[str, int] = field(default_factory=dict)
    cooling_then_heating_days: int = 0
    seconds_per_day: list[float] = field(default_factory=list)

    def first_hit_day(self, promise: str) -> int | None:
        rows = self.hits.get(promise)
        return rows[0][0] if rows else None

    def to_summary(self) -> dict:
        return {
            "master_seed": self.master_seed,
            "days": self.days,
            "seconds": round(self.seconds, 1),
            "hits_by_promise": {k: len(v) for k, v in sorted(self.hits.items())},
            "days_with_hit": {k: len({d for d, _ in v}) for k, v in sorted(self.hits.items())},
            "first_hit_day": {k: v[0][0] for k, v in sorted(self.hits.items())},
            "warnings_by_promise": dict(sorted(self.warnings.items())),
            "callback_error_days": self.callback_error_days,
            "run_errors": self.errors[:5],
            "coordinator_days": self.coordinator_days,
            "day_types": dict(sorted(self.day_types.items())),
            "days_cooling_then_heating": self.cooling_then_heating_days,
            "mean_seconds_per_day": round(sum(self.seconds_per_day) / max(1, len(self.seconds_per_day)), 3),
        }


def _cooled_then_heated(scenario: dict) -> bool:
    """Coverage only: did the realised day see cooling and later heating inside two hours?"""
    last_cool: datetime | None = None
    for ev in scenario["events"]:
        if ev.get("type") != "thermostat_state_changed":
            continue
        t = datetime.fromisoformat(ev["time"])
        a = ev.get("hvac_action")
        if a == "cooling":
            last_cool = t
        elif a == "heating" and last_cool is not None and t - last_cool <= timedelta(hours=2):
            return True
    return False


def run_batch(
    master_seed: int,
    days: int,
    profile: str = "coordinator",
    *,
    stop_on_promise: str | None = None,
    save_dir: Path | None = None,
    on_record: Any = None,
) -> BatchResult:
    """Run ``days`` generated days for one master seed.  A run that raises is recorded, not fatal."""
    out = BatchResult(master_seed=master_seed, days=0, seconds=0.0)
    t0 = time.perf_counter()
    for index in range(days):
        ds = day_seed_of(master_seed, index)
        try:
            rec = run_day(ds, profile)
        except Exception as exc:  # noqa: BLE001 - a generator/harness fault must not hide the other days
            out.errors.append((ds, f"{type(exc).__name__}: {exc}"))
            continue
        out.days += 1
        out.seconds_per_day.append(rec.seconds)
        if rec.plan.use_coordinator:
            out.coordinator_days += 1
        cls = next((e for e in rec.scenario["events"] if e["type"] == "classification"), None)
        if cls:
            out.day_types[cls["day_type"]] = out.day_types.get(cls["day_type"], 0) + 1
        if _cooled_then_heated(rec.scenario):
            out.cooling_then_heating_days += 1
        if rec.callback_errors:
            out.callback_error_days.append(ds)
        for h in rec.report.hard:
            out.hits.setdefault(h.promise, []).append((ds, h))
        for h in rec.report.warnings:
            out.warnings[h.promise] = out.warnings.get(h.promise, 0) + 1
        if on_record is not None:
            on_record(rec)
        if rec.report.hard and save_dir is not None:
            save_finding(rec, save_dir)
        if stop_on_promise is not None and out.hits.get(stop_on_promise):
            break
    out.seconds = time.perf_counter() - t0
    return out


def save_finding(rec: RunRecord, out_dir: Path | None = None) -> Path:
    """Write a replayable scenario file for a day that broke a hard promise."""
    out_dir = out_dir or FAILURES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    promises = "+".join(rec.found) or "clean"
    scenario = dict(rec.scenario)
    scenario["generated"] = {
        **scenario["generated"],
        "found": [
            {
                "promise": h.promise,
                "ts": h.ts.isoformat(),
                "detail": h.detail,
                "evidence": dict(h.evidence),
            }
            for h in rec.report.hard
        ],
        "replay": "python tools/simulate.py --replay-generated {file}",
    }
    path = out_dir / f"{promises}_{rec.plan.day_seed}.json"
    scenario["generated"]["replay"] = f"python tools/simulate.py --replay-generated {path.as_posix()}"
    path.write_text(json.dumps(scenario, indent=1), encoding="utf-8")
    return path


def replay_scenario(scenario: dict) -> PromiseReport:
    """Play a saved scenario through the standard runner with the promise probe attached."""
    from tools.sim_harness.session import drive_scenario  # noqa: PLC0415

    probe = PromiseProbe(scenario_name=str(scenario.get("name", "replay")))
    session = make_session(
        scenario, engine="fakehass", probe=probe, use_coordinator=bool(scenario.get("use_coordinator", False))
    )
    try:
        patch_stub_gaps(session)
        result = drive_scenario(session, scenario)
    finally:
        session.close()
    return result.promise_report


def replay_file(path: Path) -> PromiseReport:
    return replay_scenario(json.loads(Path(path).read_text(encoding="utf-8")))
