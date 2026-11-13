"""Verification script for the HVAC profile (hvac_profile.py): rate presets, precision, sensor lag, blower, coast.

NOT a pytest test — plain script, run directly: `python test_hvac_profile.py` (exit 1 on any failure).

Unlike test_sim_math.py / test_compressor_state.py this imports the REAL code under test: hvac_profile.py is pure
Python with no Home Assistant import and no relative import, so it loads here without the `homeassistant` package.
The tick loop below (`_Zone.tick`) is the glue from climate.py's ``_async_tick`` and ``hvac_action``, transcribed;
the ODE step and the compressor decision come from the sibling scripts' transcriptions (see their docstrings).
The real entity, inside real Home Assistant, is covered by tests_ha/test_dev_sim_hvac_profile.py.
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hvac_profile as hp  # noqa: E402
from test_sim_math import _reference_simulate_indoor_physics as ode  # noqa: E402

_FAILURES: list[str] = []
T0 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
TICK = timedelta(seconds=30)
TICK_H = TICK.total_seconds() / 3600.0


def _check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}: {label}")
    if not condition:
        _FAILURES.append(label)


def _decide(mode, current, target, start, stop, on, last_on, last_off, now, min_run=0.0, min_off=0.0):
    """climate.py::_decide_compressor_state, transcribed (see test_compressor_state.py for its own checks)."""
    if mode is None:
        return False, last_on, last_off
    if not on:
        wants = (mode == "heat" and current <= target - start) or (mode == "cool" and current >= target + start)
        if wants and (last_off is None or (now - last_off).total_seconds() >= min_off):
            return True, now, last_off
        return on, last_on, last_off
    wants_off = (mode == "heat" and current >= target + stop) or (mode == "cool" and current <= target - stop)
    if wants_off and (last_on is None or (now - last_on).total_seconds() >= min_run):
        return False, last_on, now
    return on, last_on, last_off


class _Zone:
    """climate.py's tick and hvac_action glue around the real hvac_profile functions."""

    def __init__(
        self,
        *,
        mode="heat",
        air=70.0,
        target=70.0,
        outdoor=50.0,
        k_passive=-0.15,
        rate=6.0,
        start=1.5,
        stop=-0.9,
        lag_min=0.0,
        blower=None,
        min_run=0.0,
        min_off=0.0,
        legacy=False,
    ):
        self.mode, self.air, self.target, self.outdoor, self.k_passive = mode, air, target, outdoor, k_passive
        self.rate, self.start, self.stop, self.lag_min = rate, start, stop, lag_min
        self.blower = blower or {"heat": hp.BlowerConfig(), "cool": hp.BlowerConfig()}
        self.min_run, self.min_off, self.legacy = min_run, min_off, legacy
        self.reading = air
        self.on, self.last_on, self.last_off = False, None, None
        self.eq = hp.EquipmentState()
        self.now = T0

    def reported(self):
        return self.air if self.lag_min <= 0 else self.reading

    def action(self) -> str:
        if self.eq.phase in (hp.PHASE_LEAD_IN, hp.PHASE_OVERRUN):
            return "fan"
        if self.mode in ("heat", "cool"):
            running = self.on if self.legacy else self.eq.phase == hp.PHASE_RUNNING
            return ("heating" if self.mode == "heat" else "cooling") if running else "idle"
        return "off"

    def set_mode(self, mode):
        if mode != self.mode:
            self.on, self.last_on, self.last_off = False, None, None
            hp.stop_equipment(self.eq, self.blower.get(self.eq.direction or "", hp.BlowerConfig()), self.now)
        self.mode = mode

    def tick(self):
        """climate.py ``_async_tick`` (Issue #1512 reopened order): advance the room over the elapsed interval with
        the equipment as it WAS, then decide at the new time, then report. ``legacy=True`` is the old order (decide
        first, then apply the new state to the elapsed interval), kept only to show what changed."""
        start = self.now
        self.now += TICK
        dt = TICK.total_seconds() / 3600.0
        if self.legacy:
            self._decide()
            running, coast, direction = self.on, 0.0, self.mode
            self._step(running, coast, direction, dt)
            return self.action()
        running = self.eq.phase == hp.PHASE_RUNNING
        direction = self.eq.direction or self.mode
        coast = hp.coast_average_f_per_h(self.eq, start, dt)
        self._step(running, coast, direction, dt)
        self._decide()
        cfg = self.blower.get(self.mode or self.eq.direction or "", hp.BlowerConfig())
        last = self.blower.get(self.eq.direction or "", hp.BlowerConfig())
        hp.advance_equipment(self.eq, call_on=self.on, direction=self.mode, now=self.now, cfg=cfg, cfg_last=last)
        return self.action()

    def _decide(self):
        self.on, self.last_on, self.last_off = _decide(
            self.mode,
            self.reported(),
            self.target,
            self.start,
            self.stop,
            self.on,
            self.last_on,
            self.last_off,
            self.now,
            self.min_run,
            self.min_off,
        )

    def _step(self, running, coast, direction, dt):
        if coast == 0.0:
            k = (self.rate if direction == "heat" else -self.rate) if running else None
            drive = (math.inf if direction == "heat" else -math.inf) if running else self.target
            pmode = direction if running else self.mode
        else:
            q = coast + ((self.rate if direction == "heat" else -self.rate) if running else 0.0)
            k, drive, pmode = abs(q), (math.inf if q > 0 else -math.inf), ("heat" if q > 0 else "cool")
        self.air = ode(
            self.air, self.outdoor, self.k_passive, k, dt, drive, comfort_heat=68, comfort_cool=76, hvac_mode=pmode
        )
        self.reading = hp.lag_step(self.reading, self.air, dt, self.lag_min)


def _collapse(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if not out or out[-1] != s:
            out.append(s)
    return out


def test_presets_and_defaults() -> None:
    _check(
        "presets: undersized 2, extreme weather 5, normal 12, oversized 18 F/h",
        hp.RATE_PRESETS == {"undersized": 2.0, "extreme_weather": 5.0, "normal": 12.0, "oversized": 18.0},
    )
    _check("preset options end with custom", hp.RATE_PRESET_OPTIONS[-1] == "custom")
    _check("default preset is custom (entry's own k_active)", hp.DEFAULT_RATE_PRESET == hp.RATE_PRESET_CUSTOM)
    _check("custom keeps k_active_heat 6.0", hp.resolve_rate("custom", 6.0, cooling=False) == 6.0)
    _check("missing preset keeps k_active_cool -6.0", hp.resolve_rate(None, -6.0, cooling=True) == -6.0)
    _check("unknown preset keeps k_active", hp.resolve_rate("bogus", 4.2, cooling=False) == 4.2)
    _check("normal heat = +12", hp.resolve_rate("normal", 6.0, cooling=False) == 12.0)
    _check("normal cool = -12 (cooling sign kept)", hp.resolve_rate("normal", -6.0, cooling=True) == -12.0)
    _check("preset_for_rate(18) = oversized", hp.preset_for_rate(18.0) == "oversized")
    _check("preset_for_rate(-5) = extreme_weather", hp.preset_for_rate(-5.0) == "extreme_weather")
    _check("preset_for_rate(6) = custom", hp.preset_for_rate(6.0) == "custom")
    _check("default precision = HA default (None)", hp.precision_value(hp.DEFAULT_PRECISION) is None)
    _check("missing precision = HA default (None)", hp.precision_value(None) is None)
    _check("default lag 0", hp.DEFAULT_SENSOR_LAG_MIN == 0.0)
    _check("default blower/coast all zero", hp.BlowerConfig() == hp.BlowerConfig(0.0, 0.0, 0.0, 0.0))


def test_defaults_report_edges_on_time() -> None:
    """With every profile option at its default the room follows the old tick's physics, but each on/off edge is
    reported at the tick it is decided, before its heat (Issue #1512 reopened).

    This replaces "defaults reproduce the old tick exactly": the old order decided at ``t`` and applied the new state
    to the interval ``[t - 30 s, t]`` that had already elapsed, so the room had been heating (or coasting) for a whole
    tick before ``hvac_action`` changed. That was a discretisation artifact, not a behaviour to preserve: it made every
    run look 30 s late to Climate Advisor (about 16 % on a 4-minute run). A directly connected thermostat reports its
    edge as it happens; ``edge_report_delay_s`` models a late (cloud-polled) one on purpose.

    Same run lengths and cycle count as before; the reading at each reported on edge is the pre-start level (the old
    order had already added a tick of heat), and the room still crosses the stop point by at most one tick."""
    for mode, air, target, outdoor in (("heat", 70.0, 70.0, 50.0), ("cool", 76.0, 76.0, 92.0)):
        new, old = (
            _Zone(mode=mode, air=air, target=target, outdoor=outdoor),
            _Zone(mode=mode, air=air, target=target, outdoor=outdoor, legacy=True),
        )
        sgn = 1.0 if mode == "heat" else -1.0
        runs_new, runs_old, edge_ok = [], [], True
        prev_new = prev_old = "idle"
        for _ in range(6 * 120):
            before = new.air
            a_new, a_old = new.tick(), old.tick()
            if a_new != prev_new and a_new in ("heating", "cooling"):
                runs_new.append(new.now)
                edge_ok = edge_ok and abs(new.air - before) < 0.05  # no heat yet at the reported start
            if a_old != prev_old and a_old in ("heating", "cooling"):
                runs_old.append(old.now)
            prev_new, prev_old = a_new, a_old
        _check(
            f"{mode}: same number of runs as the old tick ({len(runs_new)} vs {len(runs_old)})",
            abs(len(runs_new) - len(runs_old)) <= 1,
        )
        _check(f"{mode}: at each reported start the room has not moved yet (no tick of heat before the edge)", edge_ok)
        overshoot = max(0.0, sgn * (new.air - target))
        _check(f"{mode}: the room stays within a tick of the stop point", overshoot < 0.2)


def test_rate_physics() -> None:
    """Gross capacity minus the envelope loss: measured climb over 30 min matches the ODE's closed form."""
    for preset, rate in hp.RATE_PRESETS.items():
        z = _Zone(mode="heat", air=60.0, target=80.0, outdoor=50.0, rate=rate, start=0.0, stop=5.0)
        for _ in range(60):  # 30 min, the call is on throughout (stop at 85 F is never reached)
            z.tick()
        k = -0.15
        # the call is decided (and reported) at the first tick, 30 s in: heat flows from then on (Issue #1512
        # reopened: edges on time), so the first 30 s are passive and the climb is 29.5 min of heat
        t1 = 50.0 + (60.0 - 50.0) * math.exp(k * TICK_H)
        exp_kt = math.exp(k * (0.5 - TICK_H))
        expected = 50.0 + (t1 - 50.0) * exp_kt + (rate / k) * (exp_kt - 1)
        _check(
            f"{preset}: 30 min climb {z.air - 60:.3f} F matches closed form {expected - 60:.3f} F",
            abs(z.air - expected) < 1e-6,
        )
    # cooling mirror
    z = _Zone(mode="cool", air=80.0, target=60.0, outdoor=90.0, rate=12.0, start=0.0, stop=5.0)
    for _ in range(120):
        z.tick()
    t1 = 90.0 + (80.0 - 90.0) * math.exp(-0.15 * TICK_H)  # the first 30 s before the call (edges on time)
    exp_kt = math.exp(-0.15 * (1.0 - TICK_H))
    expected = 90.0 + (t1 - 90.0) * exp_kt + (-12.0 / -0.15) * (exp_kt - 1)
    _check(
        f"cool normal: 1 h drop {80 - z.air:.3f} F matches closed form {80 - expected:.3f} F",
        abs(z.air - expected) < 1e-6,
    )


def test_precision() -> None:
    _check("whole -> 1.0", hp.precision_value("whole") == 1.0)
    _check("half -> 0.5", hp.precision_value("half") == 0.5)
    _check("tenth -> 0.1", hp.precision_value("tenth") == 0.1)
    _check("options are default/whole/half/tenth", hp.PRECISION_OPTIONS == ("default", "whole", "half", "tenth"))
    _check("68.74 whole -> 69", hp.round_to_precision(68.74, 1.0) == 69.0)
    _check("68.74 half -> 68.5", hp.round_to_precision(68.74, 0.5) == 68.5)
    _check("68.76 half -> 69.0", hp.round_to_precision(68.76, 0.5) == 69.0)
    _check("68.74 tenth -> 68.7", hp.round_to_precision(68.74, 0.1) == 68.7)


def test_lag() -> None:
    _check("lag 0: reading is the air", hp.lag_step(60.0, 70.0, 0.01, 0.0) == 70.0)
    _check("no previous reading: the air", hp.lag_step(None, 70.0, 0.01, 5.0) == 70.0)
    _check("no air: keeps the reading", hp.lag_step(60.0, None, 0.01, 5.0) == 60.0)
    _check("dt 0: unchanged", hp.lag_step(60.0, 70.0, 0.0, 5.0) == 60.0)
    reading = 60.0
    for _ in range(10):  # 10 x 30 s = one 5-min time constant
        reading = hp.lag_step(reading, 70.0, 30 / 3600, 5.0)
    _check(
        f"step 60->70, tau 5 min: 63.2 % after 5 min (reading {reading:.3f})",
        abs((reading - 60.0) / 10.0 - (1 - math.exp(-1))) < 1e-9,
    )
    # in the loop: the thermostat decides on the lagged reading, so the air overshoots the stop point
    plain, lagged = _Zone(air=68.4, rate=12.0), _Zone(air=68.4, rate=12.0, lag_min=3.0)
    plain_max = lagged_max = 0.0
    for _ in range(120):
        plain.tick()
        lagged.tick()
        plain_max, lagged_max = max(plain_max, plain.air), max(lagged_max, lagged.air)
    _check(
        f"lag 3 min: air peaks higher ({lagged_max:.2f}) than with no lag ({plain_max:.2f})",
        lagged_max > plain_max + 0.2,
    )
    _check("lag 3 min: reading trails the air while heating", lagged.reading != lagged.air)


def _cycle(mode: str, blower: dict, ticks: int = 120) -> list[str]:
    heat = mode == "heat"
    z = _Zone(
        mode=mode,
        air=68.4 if heat else 77.6,
        target=70.0 if heat else 76.0,
        outdoor=50.0 if heat else 92.0,
        rate=12.0,
        blower=blower,
    )
    seq = ["idle"]
    while len(seq) <= ticks:  # one cycle: stop at the first tick back at idle after the run
        seq.append(z.tick())
        if seq[-1] == "idle" and any(s in ("heating", "cooling") for s in seq):
            break
    return seq


def test_blower_overrun_sequence() -> None:
    heat = {"heat": hp.BlowerConfig(overrun_min=3.0), "cool": hp.BlowerConfig()}
    seq = _cycle("heat", heat, ticks=40)
    _check(
        f"heat overrun 3 min: idle -> heating -> fan -> idle ({_collapse(seq)})",
        _collapse(seq) == ["idle", "heating", "fan", "idle"],
    )
    _check(f"heat overrun lasts 3 min (6 ticks of 30 s; got {seq.count('fan')})", seq.count("fan") == 6)
    cool = {"heat": hp.BlowerConfig(), "cool": hp.BlowerConfig(overrun_min=2.0)}
    seq = _cycle("cool", cool, ticks=40)
    _check(
        f"AC overrun 2 min: idle -> cooling -> fan -> idle ({_collapse(seq)})",
        _collapse(seq) == ["idle", "cooling", "fan", "idle"],
    )
    _check(f"AC overrun lasts 2 min (got {seq.count('fan')} ticks)", seq.count("fan") == 4)
    seq = _cycle("heat", cool, ticks=40)  # the cool overrun must not apply to heat
    _check(
        f"cool-only overrun leaves heat as idle -> heating -> idle ({_collapse(seq)})",
        _collapse(seq) == ["idle", "heating", "idle"],
    )
    lead = {"heat": hp.BlowerConfig(lead_in_min=1.0, overrun_min=1.0), "cool": hp.BlowerConfig()}
    seq = _cycle("heat", lead, ticks=40)
    _check(
        f"lead-in 1 min + overrun: idle -> fan -> heating -> fan -> idle ({_collapse(seq)})",
        _collapse(seq) == ["idle", "fan", "heating", "fan", "idle"],
    )
    # no heat during the lead-in: the room keeps falling until the burner starts
    z = _Zone(air=68.4, rate=12.0, blower=lead)
    z.tick()
    before = z.air
    z.tick()
    _check("lead-in delivers no heat (room still falling)", z.air < before and z.action() == "fan")


def test_mode_change_stops_into_overrun() -> None:
    z = _Zone(air=68.4, rate=12.0, blower={"heat": hp.BlowerConfig(overrun_min=2.0), "cool": hp.BlowerConfig()})
    z.tick()
    _check("heating before the mode change", z.action() == "heating")
    z.set_mode(None)
    _check("mode off mid-run: the blower overruns (fan)", z.action() == "fan")
    seq = [z.tick() for _ in range(6)]
    _check(f"then off once the overrun ends ({_collapse(seq)})", _collapse(seq) == ["fan", "off"])
    st = hp.EquipmentState(phase=hp.PHASE_LEAD_IN, direction="heat")
    hp.stop_equipment(st, hp.BlowerConfig(overrun_min=2.0, coast_rate_f_per_h=5, coast_tau_min=2), T0)
    _check(
        "a lead-in that never ran stops with no overrun and no coast",
        st.phase == hp.PHASE_OFF and st.coast_start is None,
    )


def test_coast() -> None:
    st = hp.EquipmentState(phase=hp.PHASE_RUNNING, direction="heat")
    hp.stop_equipment(st, hp.BlowerConfig(coast_rate_f_per_h=6.0, coast_tau_min=4.0), T0)
    total, now, dt = 0.0, T0, 30 / 3600
    first = hp.coast_average_f_per_h(st, now, dt)
    for _ in range(200):
        total += hp.coast_average_f_per_h(st, now, dt) * dt
        now += TICK
    _check(
        f"heat coast total {total:.4f} F = rate x tau = {hp.coast_total_f(6.0, 4.0):.4f} F",
        abs(total - hp.coast_total_f(6.0, 4.0)) < 1e-4,
    )
    _check(f"first tick average {first:.3f} F/h is just under the 6 F/h at the stop", 5.0 < first < 6.0)
    _check("spent coast is dropped", st.coast_start is None)
    st = hp.EquipmentState(phase=hp.PHASE_RUNNING, direction="cool")
    hp.stop_equipment(st, hp.BlowerConfig(coast_rate_f_per_h=4.0, coast_tau_min=2.0), T0)
    _check("cool coast is negative (cools)", hp.coast_average_f_per_h(st, T0, dt) < 0)
    # in the loop: with coast the room keeps rising after the burner stops
    plain = _Zone(air=68.4, rate=12.0)
    coast = _Zone(
        air=68.4,
        rate=12.0,
        blower={
            "heat": hp.BlowerConfig(overrun_min=3.0, coast_rate_f_per_h=8.0, coast_tau_min=2.0),
            "cool": hp.BlowerConfig(),
        },
    )
    p_max = c_max = 0.0
    for _ in range(40):
        plain.tick()
        coast.tick()
        p_max, c_max = max(p_max, plain.air), max(c_max, coast.air)
    # 8 F/h decaying over 2 min beats the ~3 F/h envelope loss for ~2 min: about +0.07 F at the peak
    _check(f"furnace coast lifts the peak ({c_max:.3f} vs {p_max:.3f})", c_max > p_max + 0.05)
    st = hp.EquipmentState(
        phase=hp.PHASE_OVERRUN,
        direction="heat",
        coast_direction="heat",
        coast_start=T0,
        coast_rate_f_per_h=5.0,
        coast_tau_min=3.0,
    )
    hp.advance_equipment(st, call_on=True, direction="heat", now=T0 + TICK, cfg=hp.BlowerConfig())
    _check(
        "a new run during overrun goes straight to running and clears the coast",
        st.phase == hp.PHASE_RUNNING and st.coast_start is None,
    )


def test_rise_per_cycle_arithmetic() -> None:
    _check("default swing: rise per cycle = 1.5 - 0.9 = 0.6 F", abs(hp.rise_per_cycle_f(1.5, 0.9) - 0.6) < 1e-12)
    _check("start 1.5 below, stop at target: 1.5 F", hp.rise_per_cycle_f(1.5, 0.0) == 1.5)
    _check("0.6 F at 12 F/h net = 3 min", abs(hp.run_minutes(0.6, 12.0) - 3.0) < 1e-12)
    _check("0.6 F at 2 F/h net = 18 min", abs(hp.run_minutes(0.6, 2.0) - 18.0) < 1e-12)
    _check("net rate 0: never finishes", hp.run_minutes(0.6, 0.0) == math.inf)
    # simulated: normal preset, default swing, 20 F indoor-outdoor gap -> net ~9 F/h; one run moves ~0.6 F
    z = _Zone(air=68.4, outdoor=50.0, rate=12.0)
    seq = [z.tick() for _ in range(40)]
    runs = seq.count("heating")
    # the first run starts at 68.4 (0.1 F past the start point) and each crossing overshoots by up to one tick
    _check(
        f"normal preset, default swing: one run is {runs * 0.5:.1f} min (~0.7 F at ~9 F/h net, 4-6 min)",
        8 <= runs <= 12,
    )


def test_edge_report_delay() -> None:
    """``edge_report_delay_s`` (a cloud-polled thermostat): hvac_action shows each change that many seconds late."""
    h = [(T0, "idle"), (T0 + timedelta(minutes=1), "heating"), (T0 + timedelta(minutes=5), "idle")]
    at = T0 + timedelta(minutes=1, seconds=20)
    _check("delay 0: the current action", hp.reported_action(h, at, 0.0, "heating") == "heating")
    _check("delay 30 s, 20 s after the start: still idle", hp.reported_action(h, at, 30.0, "heating") == "idle")
    later = T0 + timedelta(minutes=1, seconds=31)
    _check("delay 30 s, 31 s after the start: heating", hp.reported_action(h, later, 30.0, "heating") == "heating")
    stop = T0 + timedelta(minutes=5, seconds=10)
    _check("delay 30 s, 10 s after the stop: still heating", hp.reported_action(h, stop, 30.0, "idle") == "heating")
    _check("default delay is 0 (on time)", hp.DEFAULT_EDGE_REPORT_DELAY_S == 0.0)


def main() -> None:
    print(__doc__.splitlines()[0])
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n-- {name}")
            fn()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED:")
        for f in _FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
