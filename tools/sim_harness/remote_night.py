"""remote_night - a closed-loop sleep-sensor night against the REAL coordinator and engine (Issue #1194 e).

Occupant outcome: the hallway thermostat only sees the hallway, so on a cold night the bedroom (the room the person
sleeps in, measured by the sleep sensor) can sit below the sleep floor while the furnace never starts.  This module
plays that night: a two-node house (``world.ToyHouse`` with a ``BedroomNode``) answers whatever the thermostat is
set to, the bedroom sensor REPORTS on change (with ``last_reported``), the hallway thermostat cycles on its own
asymmetric deadband (Issue #1234) on the HALLWAY reading, and Climate Advisor's remote-sensor outer loop (the real
``AutomationEngine.remote_offset_check``, driven by the real coordinator listener and its 1-minute timer) nudges the
setpoint.  Nothing here is Climate Advisor logic: the plant knows nothing about the offset, the swing or the caps.

Two uses: ``tools/simulate.py``-style reporting (writes, furnace starts, starts per hour, shortest off time -- the
POC v2 columns) and recording the realised events as an ordinary scenario file (``scenario``), so a pending scenario
can be replayed without this module.

Pure harness: imports no Climate Advisor code at import time (the session installs the stubs when built).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tools.sim_harness.world import BedroomParams, HouseParams, ToyHouse

BEDROOM_ENTITY = "sensor.bedroom_temp"
CLIMATE_ENTITY = "climate.test_thermostat"

# The owner's home, measured (POC v2, scratchpad poc-F2): hallway heats 5.68 F/h with the burner on and loses 0.0228/h;
# the thermostat starts at setpoint - 1.5 and stops at setpoint - 0.5 (then a 2-minute blower overrun, in the node).
# A small 0.05 F/h internal gain lets the hallway drift from 69.4 to about 66.5 F over a 54 -> 45 F night with
# no heat, inside the recorded 66-69 F.  The bedroom (world.BedroomParams defaults) trails it by about 3 F.
OWNER_HOUSE = HouseParams(
    k_env_per_hr=0.0228,
    solar_f_hr=0.05,
    heat_gain_f_hr=5.68,
    cool_gain_f_hr=5.68,
    swing_f=1.5,
    overshoot_f=0.0,
    whole_degree=False,
    off_offset_f=-0.5,
    bedroom=BedroomParams(),
)


@dataclass
class NightSpec:
    mode: str = "heat"  # thermostat mode ("heat" or "cool")
    swing_f: float | None = 1.5  # the LEARNED swing the loop uses as its hysteresis; None = leave the engine default
    start: datetime = datetime(2026, 11, 2, 20, 30, tzinfo=UTC)  # sleep window 20:30 -> 06:30
    hours: float = 10.0
    house: HouseParams = OWNER_HOUSE
    hall_start_f: float = 69.4
    bedroom_start_f: float = 67.2
    outdoor_start_f: float = 54.0
    outdoor_end_f: float = 45.0
    sleep_target_f: float = 66.0
    wake_target_f: float = 68.0
    config: dict[str, Any] = field(default_factory=dict)  # merged over the night's scenario config
    fan_mode: str = "hvac_fan"
    silent_after_min: float | None = None  # the bedroom sensor stops reporting this many minutes in
    window_open_min: tuple[float, float] | None = None  # (opens, closes) minutes in for a monitored window


@dataclass
class NightResult:
    spec: NightSpec
    scenario: dict
    writes: list[tuple[datetime, float]]  # every climate.set_temperature, in order
    runs: list[tuple[datetime, datetime]]  # furnace/compressor runs: (start, stop)
    room_min_f: float
    room_max_f: float
    reading_max_f: float
    minutes_short: int  # minutes the ROOM (not the reading) sat below the sleep target
    deficit_f_min: float  # degree-minutes the ROOM sat on the wrong side of the sleep target (POC 'F-min short')
    callback_errors: int
    action_log: list[dict]
    event_log: list[tuple[str, dict, datetime | None]]
    bedroom_trace: list[tuple[datetime, float, float, float, float]]  # (t, room, reading, hall, commanded setpoint)

    @property
    def starts(self) -> int:
        return len(self.runs)

    @property
    def max_starts_per_hour(self) -> int:
        best = 0
        for a, _ in self.runs:
            best = max(best, sum(1 for b, _ in self.runs if a <= b < a + timedelta(minutes=60)))
        return best

    @property
    def min_off_min(self) -> float | None:
        gaps = [(b[0] - a[1]).total_seconds() / 60 for a, b in zip(self.runs, self.runs[1:], strict=False)]
        return min(gaps) if gaps else None

    @property
    def run_minutes(self) -> list[float]:
        return [(b - a).total_seconds() / 60 for a, b in self.runs]


def _iso(t: datetime) -> str:
    return t.replace(tzinfo=None).isoformat(timespec="seconds")


def night_config(spec: NightSpec) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "climate_entity": CLIMATE_ENTITY,
        "comfort_heat": spec.wake_target_f,
        "comfort_cool": 75.0,
        "sleep_heat": spec.sleep_target_f,
        "sleep_cool": 72.0 if spec.mode == "heat" else spec.sleep_target_f,
        "sleep_time": "20:30",
        "wake_time": "06:30",
        "sleep_indoor_temp_entity": BEDROOM_ENTITY,
        "initial_thermostat_mode": spec.mode,
        "fan_mode": spec.fan_mode,
        "fan_min_runtime_per_hour": 0,
        "thermostat_hvac_modes": ["off", "heat", "cool"],
    }
    if spec.fan_mode == "whole_house_fan":
        cfg.update({"fan_entity": "fan.whf", "fan_state_entity": "fan.whf", "fan_state_feedback": True})
    if spec.window_open_min is not None or spec.fan_mode == "whole_house_fan":
        cfg["door_window_sensors"] = ["binary_sensor.bedroom_window"]
        cfg["sensor_debounce_seconds"] = 300
    cfg.update(spec.config)
    return cfg


def run_night(spec: NightSpec) -> NightResult:
    """Play one night closed-loop and return what happened (and the realised events as a scenario)."""
    from tools.sim_harness.session import make_session  # noqa: PLC0415  (installs the HA stubs)

    cfg = night_config(spec)
    house = ToyHouse(spec.house, spec.hall_start_f, spec.bedroom_start_f)
    assert house.bedroom is not None, "the night needs a house with a bedroom node"
    cold = spec.mode == "heat"
    scenario: dict[str, Any] = {
        "name": f"remote_night_{spec.mode}_swing_{spec.swing_f}",
        "description": "Closed-loop sleep-sensor night (tools/sim_harness/remote_night.py); realised events.",
        "config": cfg,
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "events": [],
    }
    if spec.swing_f is not None:
        scenario["thermal_model"] = {"swing_heat_f": spec.swing_f, "swing_cool_f": spec.swing_f}
    events: list[dict] = []

    def classification(t: datetime) -> dict:
        return {
            "time": _iso(t),
            "type": "classification",
            "day_type": "cold" if cold else "hot",
            "hvac_mode": spec.mode,
            "windows_recommended": False,
            "today_high": 48.0 if cold else 95.0,
            "today_low": 38.0 if cold else 72.0,
            "tomorrow_high": 48.0 if cold else 95.0,
            "tomorrow_low": 38.0 if cold else 72.0,
        }

    t0 = spec.start - timedelta(minutes=2)
    construct = dict(scenario)
    construct["events"] = [{"time": _iso(t0), "type": "temp_update", "indoor_f": round(house.reported_f(), 1)}]
    session = make_session(construct, engine="fakehass", use_coordinator=True)
    writes: list[tuple[datetime, float]] = []
    runs: list[tuple[datetime, datetime]] = []
    trace: list[tuple[datetime, float, float, float, float]] = []
    try:
        view = session.thermostat()
        coord = getattr(session, "coordinator", None)
        if coord is not None and not hasattr(coord, "async_refresh") and hasattr(coord, "async_request_refresh"):
            coord.async_refresh = coord.async_request_refresh  # harness stub gap, as the generator does
        last_action: list[str | None] = [None]
        last_hall: list[float | None] = [None]
        run_start: list[datetime | None] = [None]
        room_min, room_max, reading_max, short, deficit = 1e9, -1e9, -1e9, 0, 0.0
        window_open = False

        def feed(ev: dict) -> None:
            session.advance_to(datetime.fromisoformat(ev["time"]).replace(tzinfo=UTC))
            session.feed(ev)
            events.append(ev)

        def outdoor_at(minute: float) -> float:
            frac = minute / (spec.hours * 60.0)
            return round(spec.outdoor_start_f + (spec.outdoor_end_f - spec.outdoor_start_f) * frac, 1)

        def report_hall(t: datetime, minute: float, force: bool = False) -> None:
            shown = house.reported_f()
            if force or shown != last_hall[0]:
                last_hall[0] = shown
                feed({"time": _iso(t), "type": "temp_update", "indoor_f": shown, "outdoor_f": outdoor_at(minute)})

        def report_bedroom(t: datetime) -> None:
            feed(
                {
                    "time": _iso(t),
                    "type": "remote_sensor_report",
                    "entity": BEDROOM_ENTITY,
                    "value_f": round(house.bedroom.reported_f, 2),
                }
            )

        def report_action(t: datetime) -> None:
            prev = house.running
            sp = view.setpoint_f
            action = house.decide_action(view.mode, sp)
            house.coast(prev)
            if action != last_action[0]:
                last_action[0] = action
                feed(
                    {
                        "time": _iso(t),
                        "type": "thermostat_state_changed",
                        "hvac_mode": view.mode or "off",
                        "hvac_action": action,
                    }
                )
                if action in ("heating", "cooling") and run_start[0] is None:
                    run_start[0] = t
                elif action not in ("heating", "cooling") and run_start[0] is not None:
                    runs.append((run_start[0], t))
                    run_start[0] = None

        # --- the night begins: the thermostat reports, the bedroom reports, then the sleep band is applied ---
        report_hall(spec.start - timedelta(minutes=1), 0, force=True)
        report_bedroom(spec.start - timedelta(minutes=1))
        report_action(spec.start - timedelta(minutes=1))
        feed(classification(spec.start))
        feed({"time": _iso(spec.start + timedelta(seconds=5)), "type": "bedtime"})
        report_action(spec.start + timedelta(seconds=10))

        n_min = int(spec.hours * 60)
        silent = False
        for m in range(1, n_min + 1):
            t = spec.start + timedelta(minutes=m)
            if spec.silent_after_min is not None and not silent and m >= spec.silent_after_min:
                silent = True
                house.bedroom.silent = True
            if spec.window_open_min is not None:
                opens, closes = spec.window_open_min
                if not window_open and m >= opens and m < closes:
                    window_open = True
                    feed({"time": _iso(t), "type": "sensor_open", "entity": "binary_sensor.bedroom_window"})
                elif window_open and m >= closes:
                    window_open = False
                    feed({"time": _iso(t), "type": "sensor_close", "entity": "binary_sensor.bedroom_window"})
            session.advance_to(t)
            outdoor = outdoor_at(m)
            before_age = house.bedroom.last_reported_min
            house.step(1, outdoor, 1.0, window_open)
            report_hall(t, m)
            if house.bedroom.last_reported_min != before_age and not house.bedroom.silent:
                report_bedroom(t)
            report_action(t)
            sp = view.setpoint_f
            room = house.bedroom.room_f
            room_min, room_max = min(room_min, room), max(room_max, room)
            reading_max = max(reading_max, house.bedroom.reported_f)
            short += room < spec.sleep_target_f if cold else room > spec.sleep_target_f
            deficit += max(0.0, (spec.sleep_target_f - room) if cold else (room - spec.sleep_target_f))
            if m % 5 == 0:
                trace.append(
                    (t, room, house.bedroom.reported_f, house.indoor_f, sp if sp is not None else float("nan"))
                )
        if run_start[0] is not None:
            runs.append((run_start[0], spec.start + timedelta(minutes=n_min)))
        result = session.finish()
        errors = len(result.callback_errors)
        action_log, event_log = list(result.action_log), list(result.event_log)
    finally:
        session.close()
    for a in action_log:
        if (
            a.get("domain") == "climate"
            and a.get("service") == "set_temperature"
            and "temperature" in a.get("data", {})
        ):
            ts = a.get("ts")
            writes.append((datetime.fromisoformat(ts) if isinstance(ts, str) else ts, a["data"]["temperature"]))
    scenario["events"] = events
    return NightResult(
        spec,
        scenario,
        writes,
        runs,
        room_min,
        room_max,
        reading_max,
        short,
        deficit,
        errors,
        action_log,
        event_log,
        trace,
    )


def format_row(label: str, r: NightResult) -> str:
    moff = r.min_off_min
    runs = ", ".join(f"{x:.0f}" for x in r.run_minutes) or "-"
    return (
        f"{label:<22} writes {len(r.writes):3d}  starts {r.starts:2d}  max starts/h {r.max_starts_per_hour}  "
        f"min off {'-' if moff is None else f'{moff:.0f} min':>7}  runs {runs}  "
        f"room min {r.room_min_f:.1f}  <target {r.minutes_short} min ({r.deficit_f_min:.0f} F-min)  "
        f"reading max {r.reading_max_f:.1f}"
    )


if __name__ == "__main__":  # python -I -m tools.sim_harness.remote_night [swing ...]
    import sys

    for sw in [float(x) for x in sys.argv[1:]] or [0.59, 1.0, 1.5]:
        print(format_row(f"H={sw}", run_night(NightSpec(swing_f=sw))))
    json.dumps({})
