"""Issue #1593: the v4 bedroom forecast at night follows the house's short remote-loop warm-ups.

Occupant view: during the sleep window the chart's (and the Prediction accuracy page's) v4 bedroom line showed one
big late warm-up and a fake warm-up right after bedtime, where the real bedroom drifts down to about 64.5 F, the
remote-sensor loop (#1194) raises the hallway setpoint 2 F, the furnace makes a few 7-8 minute runs and the bedroom
comes back up about 2.7 F, about every 77 minutes. The night card's advice used the same emulation. Four causes:

1. every forecast started the emulated loop at offset 0, forgetting a raise already in force (and the night plan
   re-planned after bedtime from offset 0 with no run in progress);
2. a run shorter than the minimum run was always credited a full 15-minute step of heat, and no emulated run could
   be shorter than 15 minutes;
3. the thermostat was learned against the plain band edge at night while the hallway was actually commanded the
   raised setpoint, so the learned stop point already held part of the raise and the emulator added it again;
4. the forecast used the schedule's one-hour ramp into the sleep floor (interpolated over about two hours), where
   the house arms the flat sleep band at bedtime, so the emulated loop raised the setpoint at bedtime.

Nothing in the house is controlled by the display line; the rollout of cause 2 and the thermostat of cause 3 are
shared with the night strategy and the backtest, whose numbers are reported with the fix.
"""

from __future__ import annotations

import dataclasses
import json
import math
import statistics
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.remote_offset import RemoteLoopEmulator

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v4_sleep_loop_1593_nights.json"
PST = timezone(timedelta(hours=-8))


# ============================================================================================ cause 1: the live offset
def _emu(**kw: Any) -> RemoteLoopEmulator:
    return RemoteLoopEmulator(swing_heat_f=1.44, swing_cool_f=1.44, unit="fahrenheit", **kw)


def _call(emu: RemoteLoopEmulator, bedroom: float, *, running: int = 0) -> tuple[float | None, float | None]:
    return emu.setpoints(in_window=True, target_f=bedroom, heat_sp=66.0, cool_sp=72.0, running=running)


class TestSeededEmulator:
    def test_a_raise_in_force_is_held_while_the_bedroom_is_inside_the_swing(self):
        """The live loop holds +2 F while the bedroom is between 64.56 and 67.44 F; the forecast must too."""
        assert _call(_emu(), 66.5) == (66.0, 72.0)  # unseeded: the raise is forgotten
        assert _call(_emu(offset_f=2.0), 66.5) == (68.0, 72.0)

    def test_a_cooling_offset_is_seeded_on_the_cool_side(self):
        assert _call(_emu(offset_f=-2.0), 71.0) == (66.0, 70.0)

    def test_the_seeded_offset_steps_down_once_the_bedroom_is_past_the_target(self):
        assert _call(_emu(offset_f=2.0), 67.6) == (66.0, 72.0)

    def test_the_startup_wait_counts_from_the_live_raise(self):
        """A raise 60 s ago with a measured 300 s latency: no second step yet (the live loop waits too)."""
        waiting = _emu(offset_f=2.0, seconds_since_raise=60.0, startup_latency_s=300.0)
        assert _call(waiting, 64.0) == (68.0, 72.0)
        assert _call(_emu(offset_f=2.0), 64.0) == (70.0, 72.0)  # no raise clock: a second step at once

    def test_a_short_measured_latency_shortens_the_wait_as_live(self):
        """Measured latency 60 s -> the live wait is 120 s: 200 s after a raise, a second step."""
        assert _call(_emu(offset_f=2.0, seconds_since_raise=200.0, startup_latency_s=60.0), 64.0) == (70.0, 72.0)
        assert _call(_emu(offset_f=2.0, seconds_since_raise=200.0), 64.0) == (68.0, 72.0)

    def test_live_seed_reads_only_an_offset_of_the_sleep_context(self):
        now = datetime(2026, 11, 18, 0, 50, tzinfo=PST)
        eng = _FakeEngine(offset=2.0, context="sleep", raise_at=now - timedelta(minutes=6))
        assert v4_shadow.live_loop_seed(eng, now) == (2.0, 360.0)
        assert v4_shadow.live_loop_seed(_FakeEngine(offset=2.0, context=None), now) == (0.0, None)
        assert v4_shadow.live_loop_seed(_FakeEngine(offset=None, context="sleep"), now) == (0.0, None)
        assert v4_shadow.live_loop_seed(_FakeEngine(state=None), now) == (0.0, None)
        assert v4_shadow.live_loop_seed(object(), now) == (0.0, None)

    def test_live_emulator_takes_swing_and_startup_wait_from_the_engine(self):
        now = datetime(2026, 11, 18, 0, 50, tzinfo=PST)
        eng = _FakeEngine(offset=2.0, context="sleep", raise_at=now - timedelta(seconds=60), latency=300.0, swing=1.44)
        emu = v4_shadow.live_loop_emulator(eng, now=now, unit="fahrenheit", step_minutes=15)
        assert _call(emu, 66.5) == (68.0, 72.0)  # the live +2 F
        assert eng.swing_modes == ["heat", "cool"]  # the engine's own swing (``_remote_swing_f``), one source
        emu2 = v4_shadow.live_loop_emulator(eng, now=now, unit="fahrenheit", step_minutes=15)
        assert _call(emu2, 64.0) == (68.0, 72.0)  # still inside the live startup wait


class _FakeEngine:
    def __init__(self, *, offset=2.0, context="sleep", raise_at=None, latency=None, swing=1.44, state=True):
        from custom_components.climate_advisor.remote_offset import RemoteLoopState, RemoteOffsetState

        self.swing_modes: list[str] = []
        self._swing = swing
        self._remote_raise_at = raise_at
        self._remote_startup_latency_s = latency
        self._state = (
            RemoteOffsetState(
                state=RemoteLoopState.ACTIVE,
                context=context,
                mode="heat",
                comfort_target_f=66.0,
                commanded_f=None,
                offset_f=offset,
                gate_reason=None,
                clamp_limit=None,
                last_reason="raised",
                remote_value_f=65.0,
                remote_age_s=10.0,
                cap_limit_f=4.0,
            )  # fmt: skip
            if state
            else None
        )

    def remote_offset_state(self):
        return self._state

    def _remote_swing_f(self, mode: str) -> float:
        self.swing_modes.append(mode)
        return self._swing


# ========================================================================= cause 2: a run below the minimum run
_HOUSE = [0.05, 0.0, 0.0, 0.3, 0.1, 0.0, 0.0, 8.0, 8.0]  # a_o a_w a_f c_hm c_mh s ph q_h q_c (8 F/h)


def _th(**kw: Any) -> p4.V4Thermostat:
    base = dict(
        on_below_f=0.5, on_above_f=0.5, burst_minutes=7.3, reading_step_f=0.0, off_above_f=0.5, off_below_f=0.5,
        min_run_heat_min=7.3, min_run_cool_min=7.3,
    )  # fmt: skip
    base.update(kw)
    return p4.V4Thermostat(**base)


def _roll(h0: float, *, heat: float | None = 68.0, cool: float | None = None, out: float = 45.0, init=None, th=None):
    n = 4
    return p4.rollout(
        _HOUSE, [], h0=h0, m0=h0, b0=[], out=[out] * n, win=[0] * n, fan=[0] * n, hours=[2.0] * n,
        thermostat=th or _th(), heat_sp=[heat] * n, cool_sp=[cool] * n, init_run=init,
    )  # fmt: skip


class TestPartialStepBelowTheMinimumRun:
    def test_a_run_6_9_minutes_in_lands_on_its_stop_point(self):
        """c2.py: 6.9 min into a 7.3 min minimum run, the room is at the setpoint; the stop point is 68.5 F."""
        r = _roll(68.0, init=(1, 6.9))
        assert r.primary[0] == pytest.approx(68.5, abs=1e-6)  # was 69.71: a full extra step of heat
        assert r.heat_duty[0] < 1.0

    def test_a_run_just_started_still_serves_its_minimum_run(self):
        r = _roll(68.0, init=(1, 0.0))
        assert r.heat_duty[0] == pytest.approx(7.3 / 15.0)  # stop point reached sooner: the minimum run only

    def test_a_new_run_can_be_shorter_than_a_step(self):
        r = _roll(67.4, th=_th(off_above_f=0.0))
        assert 0.0 < r.heat_duty[0] < 1.0  # a 7-8 minute run, as the real furnace makes
        assert r.heat_duty[0] == pytest.approx(7.3 / 15.0)

    def test_a_run_that_needs_more_than_a_step_keeps_running(self):
        r = _roll(66.0, init=(1, 0.0))
        assert r.heat_duty[0] == 1.0 and r.heat_duty[1] < 1.0

    def test_cooling_mirror(self):
        r = _roll(72.0, heat=None, cool=72.0, out=85.0, init=(-1, 6.9))
        assert r.primary[0] == pytest.approx(71.5, abs=1e-6)
        r2 = _roll(72.6, heat=None, cool=72.0, out=85.0, th=_th(off_below_f=0.0))
        assert r2.cool_duty[0] == pytest.approx(7.3 / 15.0)

    def test_the_step_by_step_rollout_reproduces_the_single_rollout_with_short_runs(self):
        """``_rollout_with_sleep_loop`` treats a partial step as the run's end, as the single rollout does."""
        n = 12
        kw = dict(
            h0=67.0, m0=67.0, b0=[66.0], out=[40.0] * n, win=[0] * n, fan=[0] * n, hours=[2.0] * n,
            heat_sp=[68.0] * n, cool_sp=[None] * n, init_run=(1, 3.0),
        )  # fmt: skip
        node = [[0.4, 0.1, 0.0, 0.0, 4.0]]
        single = p4.rollout(_HOUSE, node, thermostat=_th(), **kw)
        stepped = p4._rollout_with_sleep_loop(
            _HOUSE, node, thermostat=_th(), loop=p4.SleepLoop(), target_j=0, in_window=[True] * n, **kw
        )
        assert stepped.heat_duty == pytest.approx(single.heat_duty)
        assert stepped.primary == pytest.approx(single.primary)
        assert any(0.0 < u < 1.0 for u in single.heat_duty)


# ======================================================= cause 3: the thermostat is learned on the commanded setpoint
TZ_LA = PST
DAY = date(2026, 11, 10)


def _row(t: datetime, hvac: str, hall: float, *, sp: float, lower: float, ct: float | None = None, **kw) -> dict:
    r = {
        "ts": t.isoformat(), "hvac": hvac, "indoor": hall, "outdoor": 45.0, "pred_outdoor": 45.0,
        "setpoint": sp, "lower": lower, "upper": kw.pop("upper", lower + 6.0), "fan_running": False,
        "windows_open": False,
        "indoor_sensors": {"sensor.hall": hall},
    }  # fmt: skip
    if ct is not None:
        r["comfort_target"] = ct
    r.update(kw)
    return r


def _cycles(start: datetime, *, edge: float, offset: float, n: int, night: bool) -> list[dict]:
    """``n`` heating cycles on a hallway thermostat that starts 0.5 F below and stops 0.5 F above what it is
    COMMANDED (edge + offset); at night the loop holds ``offset`` (logged with ``comfort_target``)."""
    rows: list[dict] = []
    t = start
    sp = edge + offset
    ct = edge if (night and offset) else None
    for _ in range(n):
        # idle drift from the stop point down to the start point, two 30-min rows
        rows.append(_row(t, "idle", sp + 0.4, sp=sp, lower=edge, ct=ct))
        rows.append(_row(t + timedelta(minutes=30), "idle", sp - 0.1, sp=sp, lower=edge, ct=ct))
        rows.append(_row(t + timedelta(minutes=45), "heating", sp - 0.5, sp=sp, lower=edge, ct=ct))
        rows.append(_row(t + timedelta(minutes=60), "heating", sp + 0.2, sp=sp, lower=edge, ct=ct))
        rows.append(_row(t + timedelta(minutes=75), "idle", sp + 0.5, sp=sp, lower=edge, ct=ct))
        t += timedelta(minutes=90)
    return rows


def _night(t: datetime) -> bool:
    return p4.in_sleep_window(t.astimezone(TZ_LA), "20:30", "06:15")


def _learn(rows: list[dict]) -> p4.V4Thermostat:
    smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
    return p4.learn_thermostat(smp, primary="sensor.hall")


class TestTrainingSetpoint:
    def test_night_cycles_on_a_raised_setpoint_teach_the_same_offsets_as_the_day(self):
        day = _cycles(datetime.combine(DAY, time(9, 0), TZ_LA), edge=68.0, offset=0.0, n=7, night=False)
        night = _cycles(datetime.combine(DAY, time(21, 0), TZ_LA), edge=66.0, offset=2.0, n=6, night=True)
        th_day = _learn(day)
        th_night = _learn(night)
        th_all = _learn(day + night)
        assert th_day.off_above_f == pytest.approx(0.5, abs=0.25)
        # learned against the plain 66 F band edge the night runs looked like a stop 2.5 F above the setpoint
        assert th_night.off_above_f == pytest.approx(th_day.off_above_f, abs=0.25)
        assert th_night.on_below_f == pytest.approx(th_day.on_below_f, abs=0.25)
        assert th_all.off_above_f == pytest.approx(th_day.off_above_f, abs=0.25)

    def test_the_sample_carries_the_commanded_setpoint_in_the_sleep_window(self):
        t = datetime.combine(DAY, time(23, 0), TZ_LA)
        rows = [
            _row(t, "idle", 66.5, sp=68.0, lower=66.0, ct=66.0),  # the loop holds +2 F
            _row(t + timedelta(minutes=30), "idle", 66.2, sp=68.0, lower=66.0, ct=66.0),
        ]
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        assert smp and all(s.heat_sp_f == 68.0 for s in smp)

    def test_the_bedtime_ramp_rows_carry_the_armed_sleep_floor(self):
        """20:51: the schedule's ramp still logs 67 F while the house commands the 66 F sleep floor."""
        t = datetime.combine(DAY, time(20, 51), TZ_LA)
        rows = [
            _row(t, "idle", 67.5, sp=66.0, lower=67.0, last_active="heating"),
            _row(t + timedelta(minutes=30), "idle", 67.2, sp=66.0, lower=67.0),
        ]
        rows.insert(0, _row(t - timedelta(minutes=40), "heating", 67.5, sp=68.0, lower=68.0))
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        night = [s for s in smp if s.ts >= t]  # the house commanded 68 F until it applied the sleep band at 20:51
        assert night and all(s.heat_sp_f == 66.0 for s in night)
        assert all(s.heat_sp_f == 68.0 for s in smp if s.ts < t)

    def test_daytime_and_override_rows_keep_the_band_edge(self):
        t = datetime.combine(DAY, time(9, 0), TZ_LA)
        rows = [
            _row(t, "heating", 68.0, sp=69.5, lower=68.0),
            _row(t + timedelta(minutes=30), "idle", 69.0, sp=69.5, lower=68.0),
        ]
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        assert smp and all(s.heat_sp_f == 68.0 for s in smp)
        n = datetime.combine(DAY, time(22, 0), TZ_LA)
        rows = [
            _row(n, "heating", 66.0, sp=68.5, lower=66.0, override=True),
            _row(n + timedelta(minutes=30), "idle", 67, sp=68.5, lower=66.0, override=True),
        ]
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        assert smp and all(s.heat_sp_f == 66.0 for s in smp)  # the occupant's value: the plan never feeds it

    def test_cooling_mirror_and_a_setpoint_of_the_other_mode_is_not_used(self):
        t = datetime.combine(DAY, time(23, 0), TZ_LA)
        rows = [
            _row(t, "cooling", 72.5, sp=70.0, lower=66.0, upper=72.0, ct=72.0),  # the loop lowers the cool edge 2 F
            _row(t + timedelta(minutes=30), "idle", 70.5, sp=70.0, lower=66.0, upper=72.0, ct=72.0),
        ]
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        assert smp and all(s.cool_sp_f == 70.0 and s.heat_sp_f is None for s in smp)
        # a heat setpoint logged while the samples run in cool mode is the other mode's: the band edge stands
        rows = [
            _row(t, "cooling", 72.5, sp=66.0, lower=66.0, upper=72.0),
            _row(t + timedelta(minutes=30), "idle", 70.5, sp=66.0, lower=66.0, upper=72.0),
        ]
        smp = p4.samples_from_chart_log(rows, tz=TZ_LA, primary="sensor.hall", in_sleep=_night)
        assert smp and all(s.cool_sp_f == 72.0 for s in smp)

    def test_without_a_sleep_sensor_nothing_changes(self):
        night = _cycles(datetime.combine(DAY, time(21, 0), TZ_LA), edge=66.0, offset=2.0, n=2, night=True)
        smp = p4.samples_from_chart_log(night, tz=TZ_LA, primary="sensor.hall")
        assert smp and all(s.heat_sp_f == 66.0 for s in smp)


# ======================================================================= cause 4: the sleep band from bedtime
def _band_at(floor_night: float = 66.0, ceil_night: float = 72.0):
    from custom_components.climate_advisor.automation import band_resolver_for
    from custom_components.climate_advisor.classifier import DayClassification

    c = DayClassification(
        day_type="cool", trend_direction="stable", trend_magnitude=0.0, today_high=58.0, today_low=44.0,
        tomorrow_high=58.0, tomorrow_low=44.0, hvac_mode="heat",
    )  # fmt: skip
    cfg = {
        "sleep_time": "20:30", "wake_time": "06:15", "sleep_heat": floor_night, "sleep_cool": ceil_night,
        "comfort_heat": 68.0, "comfort_cool": 74.0,
    }  # fmt: skip
    return band_resolver_for(c, cfg)


def _ramp_inputs(now: datetime, lower_now: float) -> list:
    d = now.date()
    band = [(now, lower_now, 74.0)] + [
        (datetime.combine(d, time(h), PST), lo, hi)
        for h, lo, hi in ((21, 67.0, 73.0), (22, 66.0, 72.0), (23, 66.0, 72.0))
    ]
    hourly = [(datetime.combine(d, time(20), PST) + timedelta(hours=k), 50.0) for k in range(8)]
    return v4_shadow.build_inputs(now=now, hourly=hourly, band=band, horizon_hours=3, step_min=15)


class TestSleepBandFromBedtime:
    def test_in_window_steps_get_the_armed_sleep_band(self):
        now = datetime(2026, 11, 17, 20, 14, tzinfo=PST)
        inputs = _ramp_inputs(now, 68.0)
        ramp = [(i.ts.strftime("%H:%M"), i.heat_sp_f) for i in inputs]
        assert any(sp > 66.5 for t, sp in ramp if t >= "20:30"), ramp  # the schedule's ramp the bug fed
        armed = v4_shadow.with_armed_sleep_band(inputs, band_at=_band_at(), occupancy_mode="home")
        for a, i in zip(armed, inputs, strict=True):
            if i.ts.time() >= time(20, 30):
                assert (a.heat_sp_f, a.cool_sp_f) == (66.0, 72.0)
            else:
                assert a == i  # awake steps keep the schedule
        # the schedule itself (chart, v3, comfort score) is untouched: a new list
        assert any(sp > 66.5 for t, sp in ramp if t >= "20:30")

    def test_a_lower_ceiling_from_the_schedule_is_kept(self):
        """A pre-cool / TOU overlay lowers the ceiling the engine commands; the sleep band never raises it again."""
        now = datetime(2026, 11, 17, 22, 0, tzinfo=PST)
        inputs = [dataclasses.replace(i, cool_sp_f=70.0) for i in _ramp_inputs(now, 66.0)]
        armed = v4_shadow.with_armed_sleep_band(inputs, band_at=_band_at(), occupancy_mode="home")
        assert all(a.cool_sp_f == 70.0 for a in armed)

    def test_away_keeps_the_schedule(self):
        now = datetime(2026, 11, 17, 20, 14, tzinfo=PST)
        inputs = _ramp_inputs(now, 68.0)
        assert v4_shadow.with_armed_sleep_band(inputs, band_at=_band_at(), occupancy_mode="away") == inputs


# ======================================================== cause 1 mirror: the night plan after bedtime (night card)
def _night_model() -> p4.V4Model:
    return p4.V4Model(
        schema_version=p4.V4_SCHEMA_VERSION, fitted_at="2026-11-18T03:15:00-08:00", primary="_ca",
        sensors=("_ca", "sensor.bedroom"),
        house={"a_o": 0.04, "a_w": 0.15, "a_f": 0.6, "c_hm": 0.4, "c_mh": 0.2, "s": 0.0, "phase": 3.0, "q_h": 9.0,
               "q_c": 10.0},
        nodes={"sensor.bedroom": {"b_h": 0.4, "b_o": 0.1, "b_w": 0.0, "s_b": 0.0, "q_b": 12.0}},
        thermostat=_th(),
        forecast=p4.V4ForecastCorrection(hourly_bias_f=tuple([0.0] * 24), nowcast_tau_h=3.0),
        fit={"frozen": [], "reason": ""}, confidence="ok",
    )  # fmt: skip


def _night_inputs(**kw: Any) -> ns.NightInputs:
    start = datetime(2026, 11, 18, 0, 50, tzinfo=PST)
    end = datetime(2026, 11, 18, 6, 15, tzinfo=PST)
    base: dict[str, Any] = dict(
        model=_night_model(), start=start, end=end, tz=PST, fan_mode="whole_house_fan",
        outdoor=[(start + timedelta(minutes=15 * k), 47.0) for k in range(30)],
        readings={"_ca": 67.0, "sensor.bedroom": 65.8}, floor_f=66.0, ceiling_f=72.0, target_sensor="sensor.bedroom",
        loop_swing_heat_f=1.44, loop_swing_cool_f=1.44,
    )  # fmt: skip
    base.update(kw)
    return ns.NightInputs(**base)


class TestNightPlanAfterBedtime:
    def test_a_replan_carries_the_live_offset_and_the_run_in_progress(self):
        spec = ns._SPECS[ns.OPT_CLOSED]
        plain = ns._simulate(_night_inputs(), spec, "sensor.bedroom")
        seeded = ns._simulate(_night_inputs(loop_offset_f=2.0, run_now=(1, 4.0)), spec, "sensor.bedroom")
        assert plain is not None and seeded is not None
        assert plain.uh[0] == 0.0  # forgot both: the hallway at 67 F sits above the plain 66 F floor
        assert seeded.uh[0] > 0.0  # the run under way on the raised 68 F setpoint continues
        assert seeded.target[1] > plain.target[1] + 0.5

    def test_the_live_offset_alone_and_the_run_alone_each_carry(self):
        spec = ns._SPECS[ns.OPT_CLOSED]
        plain = ns._simulate(_night_inputs(), spec, "sensor.bedroom")
        offset_only = ns._simulate(_night_inputs(loop_offset_f=2.0), spec, "sensor.bedroom")
        assert offset_only.uh[0] > 0.0 and plain.uh[0] == 0.0  # hallway 67 F under the raised 68 F start point
        warm = {"_ca": 68.6, "sensor.bedroom": 66.5}  # above the plain start point: only a run under way heats
        idle = ns._simulate(_night_inputs(readings=warm), spec, "sensor.bedroom")
        running = ns._simulate(_night_inputs(readings=warm, run_now=(1, 2.0)), spec, "sensor.bedroom")
        assert idle.uh[0] == 0.0 and running.uh[0] > 0.0

    def test_night_inputs_seed_only_a_night_already_under_way(self):
        model_dict = p4.to_dict(_night_model())
        now = datetime(2026, 11, 18, 0, 50, tzinfo=PST)
        end = datetime(2026, 11, 18, 6, 15, tzinfo=PST)
        out = [(now + timedelta(minutes=15 * k), 47.0) for k in range(-8, 30)]
        kw = dict(
            model_dict=model_dict, end=end, now=now, fan_mode="whole_house_fan", corrected_outdoor=out,
            predictions=None, current_readings={"_ca": 67.0, "sensor.bedroom": 65.8}, floor_f=66.0, ceiling_f=72.0,
            target_sensor="sensor.bedroom", loop_offset_f=2.0, loop_since_raise_s=300.0, run_now=(1, 4.0),
        )  # fmt: skip
        under_way = v4_shadow.night_inputs(start=now, **kw)
        assert (under_way.loop_offset_f, under_way.loop_since_raise_s, under_way.run_now) == (2.0, 300.0, (1, 4.0))
        bed = now + timedelta(hours=1)
        ahead = v4_shadow.night_inputs(start=bed, **kw)  # planning from bedtime: the live loop restarts at 0
        assert (ahead.loop_offset_f, ahead.loop_since_raise_s, ahead.run_now) == (0.0, None, None)


# ================================================================== the real nights of 2026-11-16 and 2026-11-17
def _fx() -> dict:
    return json.loads(FIXTURE.read_text())


def _hold(series: list, t: float) -> float | None:
    v = None
    for ts, x in series:
        if ts > t:
            break
        v = x
    return v


def _lin(series: list, t: float) -> float | None:
    for (t0, v0), (t1, v1) in zip(series, series[1:], strict=False):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0) if t1 > t0 else v1
    return None


def replay_origin(fx: dict, origin: datetime, *, seed: bool, armed: bool, horizon_h: int = 6) -> dict:
    """One display-cycle forecast from ``origin`` on the recorded inputs (what the coordinator feeds ``predict``)."""
    model = p4.from_dict(fx["model"])
    prim, bed = fx["primary"], fx["bedroom"]
    o = origin.timestamp()
    hall, beds = fx["hallway"], fx["bedroom_readings"]
    # the hidden mass, filtered along the recorded hallway from 12 h before
    mass = None
    for k in range(48, 0, -1):
        h = _hold(hall, o - 900 * k)
        mass = p4.step_mass(model, mass_f=mass, readings={prim: h})
    readings = {prim: _hold(hall, o), bed: _hold(beds, o)}
    hourly = [(datetime.fromtimestamp(t, PST), v) for t, v in fx["outdoor"]]
    sched = fx["band_schedule"]
    now_pt = [p for p in sched if p[0] <= o][-1]
    band = [(origin, now_pt[1], now_pt[2])] + [(datetime.fromtimestamp(t, PST), lo, hi) for t, lo, hi in sched if t > o]
    inputs = v4_shadow.build_inputs(now=origin, hourly=hourly, band=band, horizon_hours=horizon_h, step_min=15)
    if armed:
        inputs = v4_shadow.with_armed_sleep_band(inputs, band_at=_band_at(*fx["sleep_band"]), occupancy_mode="home")
    clim = fx["climate"]
    cur = [c for c in clim if c[0] <= o][-1]
    run = None
    if cur[1] == "heating":
        k = [c for c in clim if c[0] <= o]
        start = o
        for c in reversed(k):
            if c[1] != "heating":
                break
            start = c[0]
        run = (o - start) / 60.0
    loop_kw: dict[str, Any] = {}
    in_window = p4.in_sleep_window(origin, fx["sleep_time"], fx["wake_time"])
    if seed and in_window and cur[2] is not None and cur[2] > fx["sleep_band"][0]:
        changed = o
        for c in reversed([c for c in clim if c[0] <= o]):
            if c[2] != cur[2]:
                break
            changed = c[0]
        loop_kw = {"offset_f": float(cur[2]) - fx["sleep_band"][0], "seconds_since_raise": o - changed}
    emu = RemoteLoopEmulator(swing_heat_f=1.44, swing_cool_f=1.44, unit="fahrenheit", **loop_kw)
    extra: dict[str, Any] = {}
    if run is not None:
        extra.update(hvac_action_now="heating", run_elapsed_min=run)
    return p4.predict(
        model, now=origin, readings=readings, mass_f=mass, inputs=inputs, tz=PST, ca_sleep_node=bed,
        ca_sleep_time=fx["sleep_time"], ca_wake_time=fx["wake_time"], sleep_loop=emu, **extra,
    )  # fmt: skip


def _events(vals: list[float], rise: float = 0.75) -> list[int]:
    """Step indexes where a warm-up starts: a 15-min rise above ``rise`` after a step that did not rise."""
    out = []
    for k in range(1, len(vals)):
        if vals[k] - vals[k - 1] > rise and (k == 1 or vals[k - 1] - vals[k - 2] <= rise):
            out.append(k)
    return out


def night_metrics(fx: dict, *, seed: bool, armed: bool) -> dict[str, float]:
    """Bedroom-line shape over every 30-min origin of both nights (leads 15 min .. 6 h, sleep-window targets)."""
    bed = fx["bedroom"]
    beds = fx["bedroom_readings"]
    errs: list[float] = []
    ratios: list[float] = []
    amps: list[float] = []
    gaps: list[float] = []
    real_amps: list[float] = []
    real_gaps: list[float] = []
    first_err: list[float] = []  # |forecast first warm-up - real first warm-up| (min), both within 4 h
    for night in (date(2026, 11, 16), date(2026, 11, 17)):
        o = datetime.combine(night, time(20, 30), PST)
        end = datetime.combine(night + timedelta(days=1), time(5, 30), PST)
        while o <= end:
            pred = replay_origin(fx, o, seed=seed, armed=armed)
            pts = [(t, v) for t, v in pred[bed] if p4.in_sleep_window(t, fx["sleep_time"], fx["wake_time"])]
            act = [_lin(beds, t.timestamp()) for t, _ in pts]
            pv = [v for _, v in pts]
            for (_t, v), a in zip(pts, act, strict=True):
                if a is not None:
                    errs.append(v - a)
            if len(pv) >= 8 and all(a is not None for a in act[:12]):
                sa = statistics.pstdev(act[:12])
                if sa > 0.1:
                    ratios.append(statistics.pstdev(pv[:12]) / sa)
            start = _lin(beds, o.timestamp())
            shapes = (([start, *pv], amps, gaps), ([start, *(a for a in act if a is not None)], real_amps, real_gaps))
            firsts = []
            for series, amp_l, gap_l in shapes:
                ev = _events(series)
                for k in ev:
                    amp_l.append(max(series[k : k + 4]) - series[k - 1])
                gap_l.extend(15.0 * (b - a) for a, b in zip(ev, ev[1:], strict=False))
                firsts.append(ev[0] if ev else None)
            if firsts[1] is not None and firsts[1] <= 16:
                first_err.append(15.0 * abs(firsts[0] - firsts[1]) if firsts[0] is not None else 360.0)
            o += timedelta(minutes=30)

    def _m(x: list[float]) -> float:
        return statistics.fmean(x) if x else math.nan

    return {
        "bias_f": _m(errs),
        "mae_f": _m([abs(e) for e in errs]),
        "swing_ratio": _m(ratios),
        "warmup_f": _m(amps),
        "warmup_gap_min": _m(gaps),
        "real_warmup_f": _m(real_amps),
        "real_gap_min": _m(real_gaps),
        "events": float(len(amps)),
        "first_warmup_err_min": _m(first_err),
        "first_warmup_within_30min": sum(1 for e in first_err if e <= 30.0) / len(first_err) if first_err else math.nan,
    }


@pytest.fixture(scope="module")
def fixed_metrics() -> dict[str, float]:
    return night_metrics(_fx(), seed=True, armed=True)


class TestRealNights:
    def test_a_raise_in_force_at_00_44_warms_the_forecast_like_the_real_bedroom(self):
        """11-18 00:44: the loop holds +2 F and the furnace just started; the bedroom went 64.9 -> 67.6 F by 00:58."""
        fx = _fx()
        o = datetime(2026, 11, 18, 0, 45, tzinfo=PST)
        seeded = replay_origin(fx, o, seed=True, armed=True)[fx["bedroom"]]
        plain = replay_origin(fx, o, seed=False, armed=True)[fx["bedroom"]]
        start = _hold(fx["bedroom_readings"], o.timestamp())
        first_hour_seeded = max(v for _, v in seeded[:4]) - start
        first_hour_plain = max(v for _, v in plain[:4]) - start
        assert first_hour_seeded > 1.5, (start, seeded[:4])
        assert first_hour_plain < first_hour_seeded - 1.0, (plain[:4], seeded[:4])

    def test_no_emulated_raise_between_bedtime_and_the_first_real_raise(self):
        """11-17: bedtime 20:30, the first real raise at 23:20; the bug's forecast warmed the bedroom at about 20:41."""
        fx = _fx()
        for hm in ((20, 14), (20, 44), (21, 14)):
            o = datetime(2026, 11, 17, *hm, tzinfo=PST)
            pred = replay_origin(fx, o, seed=True, armed=True)
            start = _hold(fx["bedroom_readings"], o.timestamp())
            before = [v for t, v in pred[fx["bedroom"]] if t <= datetime(2026, 11, 17, 22, 45, tzinfo=PST)]
            assert not _events([start, *before], rise=0.3), (hm, start, before)

    def test_cycle_shape_is_close_to_the_real_one(self, fixed_metrics):
        """Loose bounds (the idle cool-down rate is a separate bedroom-model issue, #1594)."""
        m = fixed_metrics
        # before the fix (same fixture, same model): 53 warm-ups, 3.29 F each, 173 min apart, the first one 57 min
        # off and within 30 min of the real one from 17 of 33 origins; bias +0.31 F, swing 1.54x (the issue's numbers)
        assert m["events"] >= 58, m  # more of the real night's warm-ups are forecast
        assert 1.0 <= m["warmup_f"] <= 3.15, m  # the real ones (this measure) are 1.75 F
        assert m["warmup_gap_min"] <= 160.0, m  # the real ones are 90 min apart (this measure)
        assert m["first_warmup_err_min"] <= 45.0, m
        assert m["first_warmup_within_30min"] >= 0.6, m
        # guards, not goals: the idle cool-down is too slow (#1594), so each warm-up stays too warm too long
        assert abs(m["bias_f"]) <= 0.7, m
        assert m["swing_ratio"] <= 1.8, m


# ============================================================================================ coordinator wiring
class TestCoordinatorWiring:
    def _coord(self, rows: list[dict], engine: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            automation_engine=engine,
            config={"temp_unit": "fahrenheit"},
            _chart_log=SimpleNamespace(raw_entries=lambda: rows),
        )

    def test_the_night_plan_reads_the_live_loop_and_the_run_in_progress(self):
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator as C

        now = datetime(2026, 11, 18, 0, 50, tzinfo=PST)
        rows = [{"ts": (now - timedelta(minutes=6)).isoformat(), "hvac": "heating"}]
        eng = _FakeEngine(offset=2.0, context="sleep", raise_at=now - timedelta(minutes=6), latency=120.0)
        got = C._v4_night_loop_now(self._coord(rows, eng), now)
        assert got == {
            "loop_offset_f": 2.0,
            "loop_since_raise_s": 360.0,
            "loop_startup_latency_s": 120.0,
            "run_now": (1, pytest.approx(6.0)),
        }
        assert C._v4_night_loop_now(self._coord(rows, object()), now)["loop_offset_f"] == 0.0

    def test_the_display_cycle_seeds_the_loop_and_arms_the_sleep_band(self):
        import ast

        src = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"
        tree = ast.parse(src.read_text())
        cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator")
        meth = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        calls = {
            n.func.attr
            for n in ast.walk(meth["_v4_shadow_cycle_impl"])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert {"_v4_live_sleep_loop", "with_armed_sleep_band"} <= calls
        assert "RemoteLoopEmulator" not in ast.unparse(meth["_v4_shadow_cycle_impl"])  # never an unseeded loop
        night = ast.unparse(meth["_timeline_night_plan"])
        assert "_v4_night_loop_now" in night
        eng = _FakeEngine(offset=2.0, context="sleep")
        emu = _live_loop(self._coord([], eng))
        assert _call(emu, 66.5) == (68.0, 72.0)


def _live_loop(coord: Any) -> RemoteLoopEmulator:
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator as C

    return C._v4_live_sleep_loop(coord, datetime(2026, 11, 18, 0, 50, tzinfo=PST))
