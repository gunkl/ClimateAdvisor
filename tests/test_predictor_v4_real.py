"""Reproduce the v4 prototype on REAL Zone 1 data (Issue #1230 R1, lane A).

``fixtures/v4_zone1_replay.json`` is the Zone 1 hallway + bedroom history 2026-10-23 .. 2026-11-02 on a
15-minute grid (thermostat reading, bedroom sensor, outdoor sensor, hourly forecast, commanded setpoint,
the band floor CA arms, heating duty). The model is fitted on the first part (to 10-30) and scored on
the rest on the reading CA shows (bedroom in the 20:30-06:30 sleep window, hallway otherwise).

Prototype reference (``out/compare_data2.json`` lead_stats, numpy/scipy, 5-min grid): v4 MAE 0.61 / 0.76 /
0.88 F at 1 / 4 / 12 h against "no change" 0.67 / 1.51 / 2.73 and v3 1.08 / 1.60 / 2.42.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import predictor_v4 as p4

TZ = ZoneInfo("America/Los_Angeles")
HALL = "sensor.hall"
BED = "sensor.bedroom"
FIXTURE = Path(__file__).parent / "fixtures" / "v4_zone1_replay.json"
CUT = datetime(2026, 10, 31, 0, 0, tzinfo=TZ)

PROTOTYPE = {1: (0.61, 0.67), 4: (0.76, 1.51), 12: (0.88, 2.73)}  # (v4 MAE, no-change MAE)


def _asleep(ts: datetime) -> bool:
    lt = ts.astimezone(TZ)
    x = lt.hour + lt.minute / 60.0
    return x >= 20.5 or x < 6.5


@pytest.fixture(scope="module")
def zone1():
    data = json.loads(FIXTURE.read_text())
    cols = data["meta"]["columns"]
    rows = [dict(zip(cols, r, strict=True)) for r in data["rows"]]
    samples = [
        p4.V4Sample(
            ts=datetime.fromisoformat(r["t"]),
            outdoor_f=r["out"],
            forecast_outdoor_f=r["fc"],
            hvac_action=r["action"],
            fan_running=bool(r["fan"]),
            windows_open=bool(r["win"]),
            heat_sp_f=r["sp"],
            cool_sp_f=None,
            readings={HALL: r["hall"], BED: r["bed"]},
            heat_duty=r["heat_duty"],
            cool_duty=0.0,
        )
        for r in rows
    ]
    train = [s for s in samples if s.ts < CUT]
    model = p4.fit(train, primary=HALL, sensors=[HALL, BED], tz=TZ, now=CUT)
    return model, samples, rows


def _score(model, samples, rows, plan_key):
    idx = {s.ts: i for i, s in enumerate(samples)}
    err = {1: [], 4: [], 12: []}
    flat = {1: [], 4: [], 12: []}
    bias = {1: [], 4: [], 12: []}
    start = next(i for i, s in enumerate(samples) if s.ts >= CUT)
    for i in range(start, len(samples) - 48, 4):
        window = samples[i : i + 49]
        if any((b.ts - a.ts).total_seconds() != 900 for a, b in zip(window, window[1:], strict=False)):
            continue
        mass = None
        for j in range(i - 48, i + 1):
            mass = p4.step_mass(model, mass_f=mass, readings=samples[j].readings)
        now = samples[i]
        inputs = [
            p4.V4Input(
                ts=s.ts,
                outdoor_f=s.forecast_outdoor_f if s.forecast_outdoor_f is not None else s.outdoor_f,
                heat_sp_f=rows[idx[s.ts]][plan_key],
                cool_sp_f=None,
            )
            for s in samples[i + 1 : i + 49]
        ]
        pred = p4.predict(
            model,
            now=now.ts,
            readings=now.readings,
            mass_f=mass,
            inputs=inputs,
            tz=TZ,
            outdoor_now_f=now.outdoor_f,
            forecast_now_f=now.forecast_outdoor_f,
        )
        assert pred is not None
        ca_now = now.readings[BED] if _asleep(now.ts) else now.readings[HALL]
        for lead in (1, 4, 12):
            k = lead * 4 - 1
            tgt = samples[i + 1 + k]
            sid = BED if _asleep(tgt.ts) else HALL
            truth = tgt.readings[sid]
            err[lead].append(abs(pred[sid][k][1] - truth))
            bias[lead].append(pred[sid][k][1] - truth)
            flat[lead].append(abs(ca_now - truth))
    mean = lambda v: sum(v) / len(v)  # noqa: E731
    return {lead: (mean(err[lead]), mean(flat[lead]), mean(bias[lead]), len(err[lead])) for lead in err}


class TestZone1Reproduction:
    def test_thermostat_learned_like_the_prototype(self, zone1):
        model = zone1[0]
        th = model.thermostat
        assert th.reading_step_f == 1.0  # whole-degree thermostat
        assert th.on_below_f == pytest.approx(1.0, abs=0.5)  # prototype: on at setpoint - 1
        assert 5.0 <= th.burst_minutes <= 15.0  # prototype: 10-minute bursts

    def test_gate_is_not_none_and_fit_beats_no_change(self, zone1):
        model = zone1[0]
        assert model.confidence == "low"  # 8 days, heating only
        assert model.fit["heat_seen"] and not model.fit["cool_seen"]
        assert model.fit["mae_by_lead"]["4h"] < 0.7 * model.fit["flat_mae_by_lead"]["4h"]

    def test_four_hour_mae_on_the_ca_reading(self, zone1):
        model, samples, rows = zone1
        res = _score(model, samples, rows, "lower")  # plan setpoint = the band floor CA arms (as the prototype scored)
        mae4, flat4, bias4, n = res[4]
        assert n >= 50
        assert mae4 <= 1.0
        assert mae4 < flat4
        assert abs(bias4) <= 0.5
        assert res[1][0] < 1.0 and res[12][0] < 1.5 and res[12][0] < res[12][1]

    def test_four_hour_mae_with_commanded_setpoint_is_still_good(self, zone1):
        model, samples, rows = zone1
        mae4, flat4, _bias, _n = _score(model, samples, rows, "sp")[4]
        assert mae4 <= 1.0 and mae4 < flat4

    def test_report_numbers_for_the_record(self, zone1, capsys):
        model, samples, rows = zone1
        res = _score(model, samples, rows, "lower")
        with capsys.disabled():
            print("\nZone 1 hold-out (10-31 -> 11-02) vs prototype, MAE F on the CA reading")
            for lead in (1, 4, 12):
                m, f, b, n = res[lead]
                pm, pf = PROTOTYPE[lead]
                print(
                    f"  {lead:>2} h  v4 {m:.2f} (proto {pm:.2f})  no-change {f:.2f} (proto {pf:.2f})"
                    f"  bias {b:+.2f}  n={n}"
                )
        assert all(res[lead][0] < res[lead][1] for lead in (4, 12))
