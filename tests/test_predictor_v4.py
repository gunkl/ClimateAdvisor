"""Tests for the v4 indoor-temperature predictor (Issue #1230 R1, lane A).

A synthetic house with KNOWN parameters is simulated by an independent implementation of the physics
(in this file, not the module under test); the fitter must recover the parameters and predict the
held-out days. Real-data reproduction lives in ``test_predictor_v4_real.py``.
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.predictor_v4 import V4Input, V4Sample, V4Thermostat

TZ = ZoneInfo("America/Los_Angeles")
HALL = "sensor.hall"
BED = "sensor.bed"
DT = 0.25

TRUE_HOUSE = {
    "a_o": 0.05,
    "a_w": 0.15,
    "a_f": 0.5,
    "c_hm": 0.8,
    "c_mh": 0.3,
    "s": 0.6,
    "phase": 1.0,
    "q_h": 9.0,
    "q_c": 11.0,
}
TRUE_BED = {"b_h": 0.35, "b_o": 0.06, "b_w": 0.2, "s_b": 0.3, "q_b": 7.0}


def _sun(hour: float, phase: float) -> float:
    e = hour - phase
    return math.sin((e - 8.0) / 10.0 * math.pi) if 8.0 <= e < 18.0 else 0.0


class Plant:
    """Independent reference implementation of the house + thermostat (does not call the module)."""

    def __init__(self, house, bed=None, *, on_below=1.0, on_above=1.0, burst=10.0, burst_c=20.0, rstep=0.0):
        self.hp, self.bp = house, bed
        self.on_below, self.on_above, self.burst, self.burst_c, self.rstep = on_below, on_above, burst, burst_c, rstep
        self.h = self.m = 70.0
        self.b = 70.0
        self.left = 0.0
        self.mode = 0

    def step(self, ts, out, hsp, csp, win, fan):
        hp = self.hp
        if hsp is None and csp is None:
            self.left, self.mode = 0.0, 0
        elif self.left <= 0:
            rd = self.h if self.rstep <= 0 else math.floor(self.h / self.rstep + 0.5) * self.rstep
            if hsp is not None and rd <= hsp - self.on_below + 1e-9:
                self.left, self.mode = self.burst, 1
            elif csp is not None and rd >= csp + self.on_above - 1e-9:
                self.left, self.mode = self.burst_c, -1
            else:
                self.mode = 0
        uh = uc = 0.0
        if self.left > 0:
            dur = min(self.left, 15.0)
            self.left -= dur
            if self.mode == 1:
                uh = dur / 15.0
            elif self.mode == -1:
                uc = dur / 15.0
        lt = ts.astimezone(TZ)
        sun = _sun(lt.hour + lt.minute / 60.0, hp["phase"])
        leak = hp["a_o"] + hp["a_w"] * win + hp["a_f"] * fan
        dh = leak * (out - self.h) + hp["c_hm"] * (self.m - self.h) + hp["s"] * sun + hp["q_h"] * uh - hp["q_c"] * uc
        dm = hp["c_mh"] * (self.h - self.m)
        if self.bp:
            bp = self.bp
            ratio = hp["q_c"] / hp["q_h"]
            db = (
                bp["b_h"] * (self.h - self.b)
                + (bp["b_o"] + bp["b_w"] * win) * (out - self.b)
                + bp["s_b"] * sun
                + bp["q_b"] * (uh - ratio * uc)
            )
            self.b += db * DT
        self.h += dh * DT
        self.m += dm * DT
        return uh, uc


def outdoor_fn(base, amp, ts):
    lt = ts.astimezone(TZ)
    h = lt.hour + lt.minute / 60.0
    day = (ts - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds() / 86400.0
    return (
        base
        + amp * math.sin((h - 9.0) / 24.0 * 2 * math.pi)
        + 5.0 * math.sin(day / 3.7 * 2 * math.pi)
        + 2.0 * math.sin(day * 5.1)
    )


def band(ts, heat=True, cool=False, heat_day=68.0, heat_night=64.0, cool_sp=75.0):
    h = ts.astimezone(TZ).hour
    night = h >= 22 or h < 6
    return (heat_day if not night else heat_night) if heat else None, cool_sp if cool else None


def simulate(
    start,
    days,
    *,
    house=None,
    bed=None,
    base=42.0,
    amp=8.0,
    heat=True,
    cool=False,
    rstep=0.0,
    win_fn=None,
    fan_fn=None,
    noise=0.0,
    seed=7,
    heat_day=68.0,
    cool_sp=75.0,
    plant_kw=None,
):
    """15-min samples of the reference plant. Returns (samples, truth dict ts -> (hall, bed), plant)."""
    rng = random.Random(seed)
    plant = Plant(house or TRUE_HOUSE, bed, rstep=rstep, **(plant_kw or {}))
    plant.h = plant.m = plant.b = 66.0
    n = int(days * 96)
    out_samples = []
    truth = {}
    for k in range(1, n + 1):
        ts = (start.astimezone(UTC) + timedelta(minutes=15 * k)).astimezone(start.tzinfo)
        o = outdoor_fn(base(k / 96.0) if callable(base) else base, amp, ts)
        hsp, csp = band(ts, heat, cool, heat_day=heat_day, cool_sp=cool_sp)
        win = 1.0 if win_fn and win_fn(ts) else 0.0
        fan = 1.0 if fan_fn and fan_fn(ts) else 0.0
        uh, uc = plant.step(ts, o, hsp, csp, win, fan)
        rh = math.floor(plant.h / rstep + 0.5) * rstep if rstep >= 0.5 else round(plant.h + rng.gauss(0, noise), 2)
        rb = round(plant.b + rng.gauss(0, noise), 2) if bed else None
        readings = {HALL: rh}
        if bed:
            readings[BED] = rb
        out_samples.append(
            V4Sample(
                ts=ts,
                outdoor_f=round(o, 2),
                forecast_outdoor_f=None,
                hvac_action="heating" if uh > 0 else "cooling" if uc > 0 else "idle",
                fan_running=bool(fan),
                windows_open=bool(win),
                heat_sp_f=hsp,
                cool_sp_f=csp,
                readings=readings,
                heat_duty=uh,
                cool_duty=uc,
            )
        )
        truth[ts] = (plant.h, plant.b)
    return out_samples, truth, plant


T0 = datetime(2026, 1, 10, 0, 0, tzinfo=TZ)


def _inputs(samples, i, n=48, raw=None):
    return [
        V4Input(
            ts=s.ts,
            outdoor_f=s.outdoor_f if raw is None else raw[i + 1 + k],
            heat_sp_f=s.heat_sp_f,
            cool_sp_f=s.cool_sp_f,
            windows_open=s.windows_open,
            fan_running=s.fan_running,
        )
        for k, s in enumerate(samples[i + 1 : i + 1 + n])
    ]


def _holdout_mae(model, samples, truth, split, sensor=HALL, lead_steps=16, stride=4, mass_warm=48):
    errs, flat = [], []
    mass = None
    for j in range(split - mass_warm, split):
        mass = p4.step_mass(model, mass_f=mass, readings=samples[j].readings)
    for i in range(split, len(samples) - 49, stride):
        for j in range(max(split, i - stride + 1), i + 1):
            mass = p4.step_mass(model, mass_f=mass, readings=samples[j].readings)
        pred = p4.predict(
            model, now=samples[i].ts, readings=samples[i].readings, mass_f=mass, inputs=_inputs(samples, i), tz=TZ
        )
        assert pred is not None
        tgt = samples[i + lead_steps]
        actual = tgt.readings[sensor]
        errs.append(abs(pred[sensor][lead_steps - 1][1] - actual))
        flat.append(abs(samples[i].readings[sensor] - actual))
    return sum(errs) / len(errs), sum(flat) / len(flat)


# ----------------------------------------------------------------------------- shared fits (module scope)
@pytest.fixture(scope="module")
def heat_fit():
    samples, truth, _ = simulate(T0, 16, noise=0.03)
    split = 12 * 96
    model = p4.fit(samples[:split], primary=HALL, sensors=[HALL], tz=TZ, now=samples[split].ts)
    return model, samples, truth, split


@pytest.fixture(scope="module")
def node_fit():
    samples, truth, _ = simulate(T0, 16, bed=TRUE_BED, noise=0.03)
    split = 12 * 96
    model = p4.fit(samples[:split], primary=HALL, sensors=[HALL, BED], tz=TZ, now=samples[split].ts)
    return model, samples, truth, split


# ------------------------------------------------------------------------------------------ recovery
class TestHeatOnly:
    def test_recovers_parameters_and_predicts_holdout(self, heat_fit):
        model, samples, _truth, split = heat_fit
        assert model.confidence in ("low", "ok")
        assert model.fit["heat_seen"] is True and model.fit["cool_seen"] is False
        h = model.house
        assert h["a_o"] == pytest.approx(TRUE_HOUSE["a_o"], rel=0.3)
        assert h["q_h"] == pytest.approx(TRUE_HOUSE["q_h"], rel=0.2)
        assert h["s"] == pytest.approx(TRUE_HOUSE["s"], rel=0.4)
        # unseen cooling is tied to heating by the production ratio, never invented from nothing
        assert h["q_c"] == pytest.approx(h["q_h"] * p4._COOL_HEAT_RATIO, rel=0.01)
        mae, flat = _holdout_mae(model, samples, _truth, split)
        # held-out MAE is dominated by thermostat burst timing (learned threshold is within ~0.15 F)
        assert mae < 0.6
        assert mae < 0.5 * flat

    def test_fit_summary_fields(self, heat_fit):
        model = heat_fit[0]
        f = model.fit
        assert f["n_anchors"] >= 50
        assert f["span_days"] > 11
        assert set(f["mae_by_lead"]) == {"1h", "4h", "12h"}
        assert f["mae_by_lead"]["4h"] < f["flat_mae_by_lead"]["4h"]
        assert "sensor.hall" in f["per_sensor"]
        assert model.primary == HALL and model.sensors == (HALL,) and model.nodes == {}

    def test_deterministic(self, heat_fit):
        _model, samples, _t, split = heat_fit
        a = p4.fit(samples[: 6 * 96], primary=HALL, sensors=[HALL], tz=TZ, now=samples[0].ts, max_iterations=8)
        b = p4.fit(samples[: 6 * 96], primary=HALL, sensors=[HALL], tz=TZ, now=samples[0].ts, max_iterations=8)
        assert p4.to_dict(a) == p4.to_dict(b)

    def test_warm_start_converges_faster(self, heat_fit):
        model, samples, _t, split = heat_fit
        warm = p4.fit(samples[:split], primary=HALL, sensors=[HALL], tz=TZ, now=samples[split].ts, warm_start=model)
        assert warm.fit["iterations"] <= max(12, model.fit["iterations"] // 2)
        assert warm.fit["cost"] <= model.fit["cost"] * 1.02

    def test_stays_inside_bounds(self, heat_fit):
        for k, (lo, hi) in p4.HOUSE_BOUNDS.items():
            assert lo - 1e-9 <= heat_fit[0].house[k] <= hi + 1e-9


class TestCoolOnly:
    def test_recovers_cooling_gain(self):
        samples, truth, _ = simulate(
            T0 + timedelta(days=170), 16, base=84.0, amp=9.0, heat=False, cool=True, noise=0.03
        )
        split = 12 * 96
        model = p4.fit(samples[:split], primary=HALL, sensors=[HALL], tz=TZ, now=samples[split].ts)
        assert model.fit["cool_seen"] is True and model.fit["heat_seen"] is False
        assert model.confidence == "low"  # one mode unseen
        assert model.house["q_c"] == pytest.approx(TRUE_HOUSE["q_c"], rel=0.25)
        # mirror of the heat-only tie: the unseen heating gain follows the fitted cooling gain
        assert model.house["q_h"] == pytest.approx(model.house["q_c"] / p4._COOL_HEAT_RATIO, rel=0.01)
        mae, flat = _holdout_mae(model, samples, truth, split)
        assert mae < 0.7
        assert mae < flat

    def test_cool_only_second_sensor_node(self):
        samples, truth, _ = simulate(
            T0 + timedelta(days=170), 16, bed=TRUE_BED, base=84.0, amp=9.0, heat=False, cool=True, noise=0.03
        )
        split = 12 * 96
        model = p4.fit(samples[:split], primary=HALL, sensors=[HALL, BED], tz=TZ, now=samples[split].ts)
        mae_b, flat_b = _holdout_mae(model, samples, truth, split, sensor=BED)
        assert mae_b < 0.8 and mae_b < flat_b

    def test_cooling_lowers_temperature_in_rollout(self):
        house = [TRUE_HOUSE[k] for k in p4.HOUSE_KEYS]
        n = 8
        hot = p4.rollout(
            house, [], h0=78.0, m0=78.0, b0=[], out=[80.0] * n, win=[0.0] * n, fan=[0.0] * n, hours=[2.0] * n,
            heat_u=[0.0] * n, cool_u=[1.0] * n,
        )  # fmt: skip
        idle = p4.rollout(
            house, [], h0=78.0, m0=78.0, b0=[], out=[80.0] * n, win=[0.0] * n, fan=[0.0] * n, hours=[2.0] * n,
            heat_u=[0.0] * n, cool_u=[0.0] * n,
        )  # fmt: skip
        assert hot.primary[-1] < idle.primary[-1] - 5.0
        assert all(b < a for a, b in zip(hot.primary, hot.primary[1:], strict=False))


class TestMixed:
    def test_both_modes_recovered_and_gate_ok(self):
        samples, truth, _ = simulate(
            T0 + timedelta(days=90),
            26,
            base=lambda d: 40.0 + 2.2 * d,
            amp=12.0,
            heat=True,
            cool=True,
            heat_day=66.0,
            cool_sp=74.0,
            noise=0.03,
        )  # outdoor warms from heating weather (40 F) to cooling weather (97 F)
        split = 20 * 96
        model = p4.fit(samples[:split], primary=HALL, sensors=[HALL], tz=TZ, now=samples[split].ts)
        assert model.fit["heat_seen"] and model.fit["cool_seen"]
        assert model.confidence == "ok"
        assert model.house["q_h"] == pytest.approx(TRUE_HOUSE["q_h"], rel=0.25)
        assert model.house["q_c"] == pytest.approx(TRUE_HOUSE["q_c"], rel=0.25)
        mae, flat = _holdout_mae(model, samples, truth, split)
        assert mae < 0.6 and mae < flat


class TestWindowsAndFan:
    def test_window_and_fan_effects_recovered(self):
        def win(ts):
            lt = ts.astimezone(TZ)
            return lt.hour in (13, 14, 15) and lt.day % 3 == 0

        def fan(ts):
            lt = ts.astimezone(TZ)
            return lt.hour in (19, 20) and lt.day % 2 == 0

        samples, truth, _ = simulate(T0, 18, base=48.0, amp=10.0, win_fn=win, fan_fn=fan, noise=0.03)
        split = 14 * 96
        model = p4.fit(samples[:split], primary=HALL, sensors=[HALL], tz=TZ, now=samples[split].ts)
        assert "h/a_w" not in model.fit["frozen"] and "h/a_f" not in model.fit["frozen"]
        assert model.house["a_f"] == pytest.approx(TRUE_HOUSE["a_f"], rel=0.35)
        assert model.house["a_w"] == pytest.approx(TRUE_HOUSE["a_w"], abs=0.1)
        mae, flat = _holdout_mae(model, samples, truth, split)
        assert mae < 0.55 and mae < flat

    def test_predict_window_open_cools_a_cold_day(self, heat_fit):
        model, samples, _t, split = heat_fit
        i = split
        closed = _inputs(samples, i, 24)
        opened = [V4Input(c.ts, c.outdoor_f - 15.0, None, None, windows_open=True) for c in closed]
        shut = [V4Input(c.ts, c.outdoor_f - 15.0, None, None, windows_open=False) for c in closed]
        a = p4.predict(model, now=samples[i].ts, readings=samples[i].readings, mass_f=None, inputs=opened, tz=TZ)
        b = p4.predict(model, now=samples[i].ts, readings=samples[i].readings, mass_f=None, inputs=shut, tz=TZ)
        assert a[HALL][-1][1] < b[HALL][-1][1]

    def test_unobserved_effects_stay_frozen(self, heat_fit):
        model = heat_fit[0]
        # the heat-only fit never saw a window or the fan: both stay at their priors, flagged as frozen
        assert {"h/a_w", "h/a_f"} <= set(model.fit["frozen"])
        assert model.house["a_w"] == p4.HOUSE_START["a_w"] and model.house["a_f"] == p4.HOUSE_START["a_f"]


class TestSecondSensorNode:
    def test_node_recovered_and_predicted(self, node_fit):
        model, samples, truth, split = node_fit
        assert model.sensors == (HALL, BED)
        nd = model.nodes[BED]
        assert nd["b_h"] == pytest.approx(TRUE_BED["b_h"], rel=0.4)
        assert nd["b_o"] == pytest.approx(TRUE_BED["b_o"], abs=0.04)
        mae_h, flat_h = _holdout_mae(model, samples, truth, split, sensor=HALL)
        mae_b, flat_b = _holdout_mae(model, samples, truth, split, sensor=BED)
        assert mae_h < 0.6 and mae_b < 0.6
        assert mae_b < flat_b
        assert set(model.fit["per_sensor"]) == {HALL, BED}

    def test_predict_returns_every_sensor_and_omits_missing_one(self, node_fit):
        model, samples, _t, split = node_fit
        ins = _inputs(samples, split, 8)
        full = p4.predict(
            model, now=samples[split].ts, readings=samples[split].readings, mass_f=None, inputs=ins, tz=TZ
        )
        assert set(full) == {HALL, BED} and len(full[BED]) == 8
        part = p4.predict(
            model, now=samples[split].ts, readings={HALL: 68.0, BED: None}, mass_f=None, inputs=ins, tz=TZ
        )
        assert set(part) == {HALL}


# --------------------------------------------------------------------------- thermostat learning
class TestThermostatLearning:
    def test_whole_degree_house_recovers_on_below_and_burst(self):
        samples, _t, _ = simulate(T0, 12, rstep=1.0, heat_day=68.0, base=44.0, amp=7.0)
        th = p4.learn_thermostat(samples, primary=HALL)
        assert th.reading_step_f == 1.0
        assert th.on_below_f == pytest.approx(1.0, abs=0.01)
        assert th.burst_minutes == pytest.approx(10.0, abs=1.0)

    def test_fine_house_reports_fine_step(self):
        samples, _t, _ = simulate(T0, 8, noise=0.03)
        th = p4.learn_thermostat(samples, primary=HALL)
        assert th.reading_step_f == 0.0
        assert 0.0 <= th.on_below_f <= 4.0

    def test_cooling_offsets_and_longer_burst(self):
        samples, _t, _ = simulate(
            T0 + timedelta(days=170), 12, base=86.0, amp=9.0, heat=False, cool=True, rstep=1.0, cool_sp=75.0
        )
        th = p4.learn_thermostat(samples, primary=HALL)
        assert th.on_above_f == pytest.approx(1.0, abs=0.01)
        assert th.burst_cool_minutes is not None and th.burst_cool_minutes > 15.0

    def test_too_few_starts_falls_back_to_defaults(self):
        samples, _t, _ = simulate(T0, 1, base=70.0, amp=1.0, heat_day=60.0)  # never heats
        th = p4.learn_thermostat(samples, primary=HALL)
        assert (th.on_below_f, th.on_above_f, th.burst_minutes) == (1.0, 1.0, 10.0)

    def test_starts_right_after_setpoint_change_are_skipped(self):
        # a heating start right after a setpoint change is the occupant's, not the thermostat's own threshold
        base = datetime(2026, 1, 10, 6, 0, tzinfo=TZ)
        rows = []
        for k in range(160):
            ts = base + timedelta(minutes=15 * k)
            cyc = k % 8
            heating = cyc == 4
            # deliberately weird: setpoint jumps on every start sample, so every start is skipped
            sp = 66.0 + (k % 2) * 0 if not heating else 70.0
            rows.append(
                V4Sample(ts, 40.0, None, "heating" if heating else "idle", False, False, sp, None,
                         {HALL: 60.0}, heat_duty=0.67 if heating else 0.0, cool_duty=0.0)
            )  # fmt: skip
        th = p4.learn_thermostat(rows, primary=HALL)
        assert th.on_below_f == 1.0  # default: every start was skipped, so nothing learned

    def test_burst_start_offset_uses_pre_start_reading(self):
        # reading 66 then start with sp 68 -> offset 2.0
        base = datetime(2026, 1, 10, 6, 0, tzinfo=TZ)
        rows = []
        for k in range(200):
            ts = base + timedelta(minutes=15 * k)
            heating = k % 10 == 5
            rd = 66.0 if k % 10 in (4, 5) else 69.0
            rows.append(
                V4Sample(ts, 40.0, None, "heating" if heating else "idle", False, False, 68.0, None,
                         {HALL: rd}, heat_duty=0.67 if heating else 0.0, cool_duty=0.0)
            )  # fmt: skip
        th = p4.learn_thermostat(rows, primary=HALL)
        assert th.on_below_f == pytest.approx(2.0)
        assert th.burst_minutes == pytest.approx(10.0, abs=0.5)


# ----------------------------------------------------------------------------- thermostat emulation
class TestEmulation:
    def _roll(self, thermo, h0, sp, n=24, out=40.0):
        return p4.rollout(
            [TRUE_HOUSE[k] for k in p4.HOUSE_KEYS],
            [],
            h0=h0,
            m0=h0,
            b0=[],
            out=[out] * n,
            win=[0.0] * n,
            fan=[0.0] * n,
            hours=[2.0] * n, thermostat=thermo, heat_sp=[sp] * n, cool_sp=[None] * n,
        )  # fmt: skip

    def test_whole_degree_quantisation_gates_the_start(self):
        th = V4Thermostat(1.0, 1.0, 10.0, 1.0)
        # 67.4 reads 67 -> 67 <= 68 - 1 -> starts; 67.6 reads 68 -> does not
        assert self._roll(th, 67.4, 68.0).heat_duty[0] > 0
        assert self._roll(th, 67.6, 68.0).heat_duty[0] == 0

    def test_burst_length_sets_duty(self):
        th = V4Thermostat(0.0, 1.0, 10.0, 0.0)
        r = self._roll(th, 60.0 - 0.0, 62.0)
        assert r.heat_duty[0] == pytest.approx(1.0)  # far below -> stays on
        r2 = self._roll(th, 61.9, 62.0)
        assert r2.heat_duty[0] == pytest.approx(10.0 / 15.0)

    def test_no_setpoint_no_heat(self):
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0)
        r = self._roll(th, 50.0, None)
        assert sum(r.heat_duty) == 0 and sum(r.cool_duty) == 0

    def test_longer_cool_burst_is_used(self):
        th = V4Thermostat(1.0, 1.0, 10.0, 0.0, burst_cool_minutes=30.0)
        r = p4.rollout(
            [TRUE_HOUSE[k] for k in p4.HOUSE_KEYS], [], h0=75.6, m0=75.6, b0=[], out=[90.0] * 6, win=[0.0] * 6,
            fan=[0.0] * 6, hours=[2.0] * 6, thermostat=th, heat_sp=[None] * 6, cool_sp=[74.0] * 6,
        )  # fmt: skip
        assert r.cool_duty[0] == pytest.approx(1.0) and r.cool_duty[1] == pytest.approx(1.0)


# ----------------------------------------------------------------------------------- forecast correction
class TestForecastCorrection:
    def test_recovers_injected_hourly_bias(self):
        bias = [2.0 * math.sin((h - 6) / 24 * 2 * math.pi) + 1.0 for h in range(24)]
        samples = []
        rng = random.Random(3)
        for k in range(14 * 96):
            ts = T0 + timedelta(minutes=15 * k)
            actual = 50.0 + 6 * math.sin(k / 96 * 2 * math.pi)
            lt = ts.astimezone(TZ)
            fc = actual - bias[lt.hour] + rng.gauss(0, 0.2)
            samples.append(V4Sample(ts, actual, fc, "idle", False, False, None, None, {HALL: 68.0}))
        corr = p4.learn_forecast_correction(samples, tz=TZ)
        assert len(corr.hourly_bias_f) == 24
        for h in range(24):
            assert corr.hourly_bias_f[h] == pytest.approx(bias[h], abs=0.35)

    def test_nowcast_tau_tracks_persistence_of_the_miss(self):
        samples = []
        rng = random.Random(5)
        err = 0.0
        for k in range(30 * 96):
            ts = T0 + timedelta(minutes=15 * k)
            err = err * math.exp(-0.25 / 6.0) + rng.gauss(0, 0.35)  # AR(1), tau = 6 h
            actual = 55.0
            samples.append(V4Sample(ts, actual + err, actual, "idle", False, False, None, None, {HALL: 68.0}))
        corr = p4.learn_forecast_correction(samples, tz=TZ)
        assert corr.nowcast_tau_h in (4.0, 6.0, 8.0)

    def test_no_pairs_is_neutral(self):
        samples, _t, _ = simulate(T0, 3)
        corr = p4.learn_forecast_correction(samples, tz=TZ)
        assert corr.hourly_bias_f == (0.0,) * 24 and corr.nowcast_tau_h == 3.0

    def test_predict_applies_hourly_bias_and_decaying_nowcast(self, heat_fit):
        model, samples, _t, split = heat_fit
        from dataclasses import replace

        bias = tuple(2.0 for _ in range(24))
        m2 = replace(model, forecast=p4.V4ForecastCorrection(bias, 2.0))
        outs = p4._corrected_outdoor(
            m2.forecast, raw=[50.0] * 4, hours=[10.0] * 4, lead_h=[0.25, 1.0, 4.0, 12.0], now_hour=10.0,
            outdoor_now=60.0, forecast_now=50.0,
        )  # fmt: skip
        # hourly bias 2 everywhere; now miss = 60 - (50 + 2) = 8, decaying exp(-lead/2)
        assert outs[0] == pytest.approx(52.0 + 8.0 * math.exp(-0.125))
        assert outs[3] == pytest.approx(52.0 + 8.0 * math.exp(-6.0))
        assert outs[0] > outs[1] > outs[2] > outs[3]

    def test_bias_interpolates_around_the_clock(self):
        b = tuple(float(h) for h in range(24))
        assert p4._bias_interp(b, 0.5) == pytest.approx(0.0)
        assert p4._bias_interp(b, 1.0) == pytest.approx(0.5)
        # wraps 23 -> 0 smoothly
        assert p4._bias_interp(b, 0.0) == pytest.approx(11.5)


# ----------------------------------------------------------------------------------------- the gates
class TestConfidenceGates:
    def test_under_three_days_is_none_and_predict_returns_none(self):
        samples, _t, _ = simulate(T0, 2)
        m = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts)
        assert m.confidence == "none"
        ins = _inputs(samples, 10, 4)
        assert p4.predict(m, now=samples[10].ts, readings={HALL: 68.0}, mass_f=None, inputs=ins, tz=TZ) is None

    def test_empty_and_tiny_input_do_not_raise(self):
        assert p4.fit([], primary=HALL, sensors=[HALL], tz=TZ, now=T0).confidence == "none"
        one, _t, _ = simulate(T0, 0.1)
        assert p4.fit(one, primary=HALL, sensors=[HALL], tz=TZ, now=T0).confidence == "none"

    def test_early_none_says_why_on_the_model(self):
        """The Debug tab and the fit log line read ``model.fit``: an early "none" must carry its reason and count."""
        one, _t, _ = simulate(T0, 0.1)
        m = p4.fit(one, primary=HALL, sensors=[HALL], tz=TZ, now=T0)
        assert m.fit["reason"] == "too few samples" and "too few samples" in p4.summary_line(m)
        samples, _t, _ = simulate(T0, 6, noise=0.03)
        m = p4.fit(samples, primary="sensor.not_in_history", sensors=["sensor.not_in_history"], tz=TZ, now=T0)
        assert m.confidence == "none" and m.fit["reason"] == "too few anchors" and m.fit["n_anchors"] == 0

    def test_between_three_and_ten_days_is_low(self):
        samples, _t, _ = simulate(T0, 6, noise=0.03)
        m = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts)
        assert m.confidence == "low"
        assert "10 days" in m.fit["reason"]

    def test_no_skill_over_no_change_is_none(self):
        # a house with no HVAC and no outdoor swing: nothing to predict beyond "stays put"
        samples = [
            V4Sample(T0 + timedelta(minutes=15 * k), 70.0, None, "idle", False, False, None, None, {HALL: 70.0})
            for k in range(12 * 96)
        ]
        m = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts)
        assert m.confidence == "none"

    def test_predict_needs_primary_reading(self, heat_fit):
        model, samples, _t, split = heat_fit
        ins = _inputs(samples, split, 4)
        assert p4.predict(model, now=samples[split].ts, readings={HALL: None}, mass_f=None, inputs=ins, tz=TZ) is None
        assert p4.predict(model, now=samples[split].ts, readings={}, mass_f=None, inputs=ins, tz=TZ) is None
        assert p4.predict(model, now=samples[split].ts, readings={HALL: 68.0}, mass_f=None, inputs=[], tz=TZ) is None


# ----------------------------------------------------------------------------------- serialisation
class TestSerialisation:
    def test_round_trip_is_lossless_and_json_safe(self, node_fit):
        model = node_fit[0]
        d = p4.to_dict(model)
        again = json.loads(json.dumps(d))
        back = p4.from_dict(again)
        assert back is not None
        assert p4.to_dict(back) == d
        assert back.thermostat == model.thermostat
        assert back.forecast == model.forecast
        assert back.sensors == model.sensors and back.confidence == model.confidence
        assert d["schema_version"] == p4.V4_SCHEMA_VERSION == 1

    def test_restored_model_predicts_identically(self, node_fit):
        model, samples, _t, split = node_fit
        back = p4.from_dict(json.loads(json.dumps(p4.to_dict(model))))
        ins = _inputs(samples, split, 16)
        a = p4.predict(model, now=samples[split].ts, readings=samples[split].readings, mass_f=None, inputs=ins, tz=TZ)
        b = p4.predict(back, now=samples[split].ts, readings=samples[split].readings, mass_f=None, inputs=ins, tz=TZ)
        assert a == b

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(schema_version=2),
            lambda d: d.pop("schema_version"),
            lambda d: d.update(primary=5),
            lambda d: d.update(sensors=[]),
            lambda d: d.update(sensors=["other", HALL]),
            lambda d: d["house"].pop("a_o"),
            lambda d: d["house"].update(q_h="hot"),
            lambda d: d["house"].update(a_o=float("nan")),
            lambda d: d["house"].update(q_h=1e6),
            lambda d: d["nodes"].pop(BED),
            lambda d: d["thermostat"].update(burst_minutes=0),
            lambda d: d["thermostat"].pop("on_below_f"),
            lambda d: d["forecast"].update(hourly_bias_f=[0.0] * 23),
            lambda d: d["forecast"].update(nowcast_tau_h=0),
            lambda d: d.update(confidence="great"),
            lambda d: d.update(fitted_at=None),
            lambda d: d.update(fit="oops"),
        ],
    )
    def test_malformed_returns_none(self, node_fit, mutate):
        d = json.loads(json.dumps(p4.to_dict(node_fit[0])))
        mutate(d)
        assert p4.from_dict(d) is None

    @pytest.mark.parametrize("junk", [None, 5, "x", [], {}, {"schema_version": 1}])
    def test_junk_returns_none(self, junk):
        assert p4.from_dict(junk) is None


# --------------------------------------------------------------------------- step_mass and time
class TestStepMassAndDst:
    def test_step_mass_relaxes_toward_reading(self, heat_fit):
        model = heat_fit[0]
        assert p4.step_mass(model, mass_f=None, readings={HALL: 68.0}) == 68.0
        m1 = p4.step_mass(model, mass_f=60.0, readings={HALL: 70.0})
        assert 60.0 < m1 < 70.0
        assert m1 == pytest.approx(60.0 + model.house["c_mh"] * 0.25 * 10.0)
        # missing reading leaves the mass alone
        assert p4.step_mass(model, mass_f=61.0, readings={HALL: None}) == 61.0

    def test_dst_spring_forward_day(self):
        # 2026-03-08 02:00 -> 03:00 local does not exist; the grid is absolute time so it holds 92 steps
        start = datetime(2026, 3, 8, 0, 0, tzinfo=TZ)
        rows = [
            {
                "ts": (start.astimezone(UTC) + timedelta(minutes=30 * k)).astimezone(TZ).isoformat(),
                "hvac": "idle",
                "fan": False,
                "indoor": 68.0,
                "outdoor": 55.0,
            }
            for k in range(0, 48)
        ]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        local_day = [x for x in s if x.ts.astimezone(TZ).date() == start.date()]
        assert len(local_day) == 92  # a 23-hour local day: the four 02:xx grid points do not exist
        assert all(_contig(a.ts, b.ts) for a, b in zip(s, s[1:], strict=False))
        # local hour used by the sun term skips 2 h on that day
        hours = [p4._local_hour(x.ts, TZ) for x in local_day]
        assert 2.5 not in hours and 3.0 in hours

    def test_predict_across_dst_keeps_timestamps(self, heat_fit):
        model = heat_fit[0]
        now = datetime(2026, 3, 8, 0, 0, tzinfo=TZ)
        ins = [V4Input(ts, 50.0, 68.0, None) for ts in p4.grid_steps(now, 40)]
        out = p4.predict(model, now=now, readings={HALL: 66.0}, mass_f=None, inputs=ins, tz=TZ)
        assert [t for t, _ in out[HALL]] == [i.ts for i in ins]
        assert all(math.isfinite(v) for _, v in out[HALL])

    def test_dst_fall_back_day_fits_without_error(self):
        start = datetime(2026, 10, 30, 0, 0, tzinfo=TZ)
        samples, _t, _ = simulate(start, 6, noise=0.03)
        m = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts, max_iterations=10)
        assert m.confidence in ("low", "none", "ok")
        assert all(math.isfinite(v) for v in m.house.values())


def _contig(a, b):
    return b.timestamp() - a.timestamp() == 900


# ------------------------------------------------------------------------------- chart_log resampling
def _row(ts, **kw):
    r = {"ts": ts.isoformat(), "hvac": "idle", "fan": False, "indoor": 68.0, "outdoor": 50.0}
    r.update(kw)
    return r


class TestSamplesFromChartLog:
    def test_grid_and_fields_from_indoor_sensors(self):
        t0 = datetime(2026, 11, 3, 8, 0, tzinfo=TZ)
        rows = [
            _row(
                t0 + timedelta(minutes=30 * k),
                indoor=67.0 + k * 0.1,
                indoor_sensors={HALL: 68.0 + k * 0.1, BED: 66.0},
                pred_outdoor=49.0,
                windows_open=(k == 2),
                fan_running=(k == 1),
                setpoint=68.0,
            )
            for k in range(6)
        ]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        assert [x.ts for x in s][:3] == [t0, t0 + timedelta(minutes=15), t0 + timedelta(minutes=30)]
        assert all(_contig(a.ts, b.ts) for a, b in zip(s, s[1:], strict=False))
        x = s[0]
        assert x.readings[HALL] == 68.0 and x.readings[BED] == 66.0 and x.readings["_ca"] == 67.0
        assert x.forecast_outdoor_f == 49.0 and x.outdoor_f == 50.0
        by = {x.ts: x for x in s}
        assert by[t0 + timedelta(minutes=60)].windows_open is True and by[t0].windows_open is False
        assert any(y.fan_running for y in s)

    def test_old_rows_without_indoor_sensors_use_ca_key(self):
        t0 = datetime(2026, 4, 3, 8, 0, tzinfo=TZ)
        s = p4.samples_from_chart_log([_row(t0 + timedelta(minutes=30 * k)) for k in range(4)], tz=TZ)
        assert set(s[0].readings) == {"_ca"} and s[0].readings["_ca"] == 68.0

    def test_gap_over_two_hours_breaks_continuity(self):
        t0 = datetime(2026, 11, 3, 8, 0, tzinfo=TZ)
        rows = [_row(t0 + timedelta(minutes=30 * k)) for k in range(4)]
        rows += [_row(t0 + timedelta(hours=6, minutes=30 * k)) for k in range(4)]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        jumps = [(a.ts, b.ts) for a, b in zip(s, s[1:], strict=False) if not _contig(a.ts, b.ts)]
        assert len(jumps) == 1
        a, b = jumps[0]
        assert (b - a) > timedelta(hours=2)
        # fits never roll across it
        prob = p4._Problem(s, primary="_ca", extra=[], tz=TZ, horizon_steps=48, max_anchors=1000)
        for anchor in prob.anchors:
            span = [prob.ts[anchor.i + j] for j in range(anchor.length + 1)]
            assert all(_contig(x, y) for x, y in zip(span, span[1:], strict=False))

    def test_gap_under_two_hours_is_carried_forward(self):
        t0 = datetime(2026, 11, 3, 8, 0, tzinfo=TZ)
        rows = [_row(t0), _row(t0 + timedelta(minutes=105), indoor=70.0)]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        assert len(s) == 8 and s[3].readings["_ca"] == 68.0 and s[-1].readings["_ca"] == 70.0

    def test_duty_integrates_action_change_rows(self):
        t0 = datetime(2026, 11, 3, 6, 0, tzinfo=TZ)
        rows = [
            _row(t0, hvac="idle", setpoint=68.0),
            _row(t0 + timedelta(minutes=17), hvac="heating", setpoint=68.0, event="hvac_action_change"),
            _row(t0 + timedelta(minutes=27), hvac="idle", setpoint=68.0, event="hvac_action_change"),
            _row(t0 + timedelta(minutes=60), hvac="idle", setpoint=68.0),
        ]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        by = {x.ts: x for x in s}
        assert by[t0 + timedelta(minutes=30)].heat_duty == pytest.approx(10.0 / 15.0, abs=0.01)
        assert by[t0 + timedelta(minutes=15)].heat_duty == pytest.approx(0.0)
        assert by[t0 + timedelta(minutes=45)].heat_duty == pytest.approx(0.0)
        assert by[t0 + timedelta(minutes=30)].heat_sp_f == 68.0 and by[t0 + timedelta(minutes=30)].cool_sp_f is None

    def test_setpoint_goes_to_cool_when_cooling(self):
        t0 = datetime(2026, 7, 3, 15, 0, tzinfo=TZ)
        rows = [_row(t0 + timedelta(minutes=30 * k), hvac="cooling", setpoint=74.0, outdoor=95.0) for k in range(3)]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        assert s[0].cool_sp_f == 74.0 and s[0].heat_sp_f is None
        assert s[1].cool_duty == pytest.approx(1.0)

    def test_idle_mode_follows_last_active_action_and_off_has_no_setpoint(self):
        t0 = datetime(2026, 7, 3, 15, 0, tzinfo=TZ)
        rows = [_row(t0, hvac="cooling", setpoint=74.0), _row(t0 + timedelta(minutes=30), hvac="idle", setpoint=74.0)]
        rows.append(_row(t0 + timedelta(minutes=60), hvac="off", setpoint=74.0))
        s = p4.samples_from_chart_log(rows, tz=TZ)
        by = {x.ts: x for x in s}
        assert by[t0 + timedelta(minutes=30)].cool_sp_f == 74.0
        assert by[t0 + timedelta(minutes=60)].cool_sp_f is None and by[t0 + timedelta(minutes=60)].heat_sp_f is None

    def test_archive_forecast_is_used_six_hours_back(self):
        t0 = datetime(2026, 11, 3, 12, 0, tzinfo=TZ)
        rows = [_row(t0 + timedelta(minutes=30 * k), pred_outdoor=40.0) for k in range(4)]
        hour = t0.replace(minute=0)
        snaps = [
            {"issued": (t0 - timedelta(hours=7)).isoformat(), "source": "w", "hours": [[hour.isoformat(), 55.5]]},
            {"issued": (t0 - timedelta(hours=1)).isoformat(), "source": "w", "hours": [[hour.isoformat(), 99.0]]},
        ]
        s = p4.samples_from_chart_log(rows, tz=TZ, archive_snapshots=snaps)
        assert s[0].forecast_outdoor_f == 55.5
        # no archive -> the row's pred_outdoor
        assert p4.samples_from_chart_log(rows, tz=TZ)[0].forecast_outdoor_f == 40.0

    def test_junk_rows_are_ignored(self):
        t0 = datetime(2026, 11, 3, 12, 0, tzinfo=TZ)
        rows = [{"ts": "garbage"}, {"nots": 1}, _row(t0), _row(t0 + timedelta(minutes=30))]
        assert len(p4.samples_from_chart_log(rows, tz=TZ)) == 3  # type: ignore[arg-type]
        assert p4.samples_from_chart_log([], tz=TZ) == []

    def test_naive_timestamps_are_read_in_tz(self):
        rows = [{"ts": "2026-11-03T08:00:00", "hvac": "idle", "fan": False, "indoor": 68.0, "outdoor": 50.0}]
        s = p4.samples_from_chart_log(rows, tz=TZ)
        assert s[0].ts == datetime(2026, 11, 3, 8, 0, tzinfo=TZ)

    def test_fit_runs_on_resampled_synthetic_chart_log(self):
        samples, _t, _ = simulate(T0, 8, noise=0.03)
        rows = []
        for k, x in enumerate(samples):
            if k % 2:
                continue
            rows.append(
                {"ts": x.ts.isoformat(), "hvac": x.hvac_action, "fan": False, "indoor": x.readings[HALL],
                 "outdoor": x.outdoor_f, "setpoint": x.heat_sp_f, "indoor_sensors": {HALL: x.readings[HALL]},
                 "windows_open": False}
            )  # fmt: skip
        s = p4.samples_from_chart_log(rows, tz=TZ)
        m = p4.fit(s, primary=HALL, sensors=[HALL], tz=TZ, now=s[-1].ts, max_iterations=25)
        assert m.confidence in ("low", "ok")
        assert m.fit["mae_by_lead"]["4h"] < m.fit["flat_mae_by_lead"]["4h"]


# --------------------------------------------------------------------------------------- performance
class TestPerformance:
    def test_thirty_day_fit_under_budget_and_predict_is_cheap(self):
        samples, _t, _ = simulate(T0, 30, bed=TRUE_BED, noise=0.03, rstep=0.0)
        t0 = time.perf_counter()
        m = p4.fit(samples, primary=HALL, sensors=[HALL, BED], tz=TZ, now=samples[-1].ts)
        elapsed = time.perf_counter() - t0
        assert elapsed < 60.0, f"30-day fit took {elapsed:.1f}s"
        assert m.confidence in ("low", "ok")
        ins = _inputs(samples, 100, 96)
        t1 = time.perf_counter()
        for _ in range(20):
            p4.predict(m, now=samples[100].ts, readings=samples[100].readings, mass_f=None, inputs=ins, tz=TZ)
        per_call = (time.perf_counter() - t1) / 20
        assert per_call < 0.05, f"predict took {per_call * 1000:.1f} ms"
