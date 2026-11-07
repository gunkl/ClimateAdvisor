"""Issue #1230 R1b: the live shadow and the backtest share one node rule; the archive is compact; fits are staggered.

Occupant view: the dashboard's v4 line must come from the same model the evidence was gathered on. A house whose log
has months of history from before the per-sensor map must still get a model (it used to return "none" with 0 anchors),
the line must follow the bedroom sensor while the occupant sleeps (the reading CA itself uses), and eight zones must not
all fit at 03:15:00 and freeze a small host.
"""

from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.const import V4_ARCHIVE_LEADS, V4_FIT_STAGGER_S
from custom_components.climate_advisor.v4_archive import V4Archive, slot_key
from tests.test_predictor_v4 import HALL, T0, TZ, simulate
from tests.v4_fake_predictor import FakeModel, install

_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SLEEP = "sensor.bedroom"


def _rows_from(samples, *, sensor_map_from: int):
    """chart_log rows from simulated samples; the per-sensor map (and the entity primary) only from a row index on."""
    rows = []
    for k, s in enumerate(samples):
        row = {
            "ts": s.ts.isoformat(), "hvac": s.hvac_action or "idle", "fan": False, "indoor": s.readings[HALL],
            "outdoor": s.outdoor_f, "pred_outdoor": s.outdoor_f, "setpoint": s.heat_sp_f, "lower": s.heat_sp_f,
            "upper": 76.0, "windows_open": False, "fan_running": False, "nat_vent_active": False,
        }  # fmt: skip
        if k >= sensor_map_from:
            row["indoor_sensors"] = {HALL: s.readings[HALL], "climate.t": round(s.readings[HALL])}
        rows.append(row)
    return rows


# --------------------------------------------------------------------------------- M1: one rule, live == backtest
def test_live_fit_on_a_log_that_predates_the_sensor_map_gets_a_model():
    samples, _t, _ = simulate(T0, 40, base=44.0, amp=7.0, noise=0.03)
    rows = _rows_from(samples, sensor_map_from=len(samples) - 5 * 96)  # entity primary only in the last 5 days
    now = samples[-1].ts
    # before: the primary entity was read only where indoor_sensors existed -> almost no anchors -> "none"
    raw = p4.samples_from_chart_log(rows, tz=TZ)
    old = p4.fit(raw, primary=HALL, sensors=[HALL], tz=TZ, now=now)
    assert old.fit["primary_samples"] < 6 * 96  # months of history ignored: only the last 5 days had the entity
    model_dict, _secs, _line = v4_shadow.run_fit(
        chart_entries=rows, snapshots_fn=None, tz=TZ, now=now, primary=HALL, sensors=[HALL],
        primary_aliases=("climate.t",),
    )  # fmt: skip
    assert model_dict["primary"] == HALL
    assert model_dict["confidence"] in ("low", "ok")
    assert model_dict["fit"]["n_anchors"] >= 100
    assert model_dict["fit"]["primary_samples"] > 35 * 96


def test_live_and_backtest_share_the_node_rule():
    from tools import predict_backtest as pb

    pb._PREDICTOR = None
    assert pb.load_predictor().resolve_nodes.__code__.co_filename.endswith("predictor_v4.py")
    samples, _t, _ = simulate(T0, 10, noise=0.03)
    rows = _rows_from(samples, sensor_map_from=400)
    live = p4.resolve_nodes(rows, primary=HALL, sensors=[HALL], aliases=("climate.t",), tz=TZ)
    bt = pb.week_nodes(rows, HALL, [HALL], False, aliases=("climate.t",), tz=TZ)
    assert live == bt == (HALL, [HALL])


def test_fit_passes_the_aliases_and_the_sleep_predicate_to_the_engine(monkeypatch):
    install(monkeypatch)
    p = v4_shadow._predictor()
    p.resolve_nodes = lambda entries, **kw: (kw["primary"], [kw["primary"], *kw["sensors"]])
    asleep = lambda dt: dt.hour < 6  # noqa: E731
    v4_shadow.run_fit(
        chart_entries=[{"ts": T0.isoformat()}], snapshots_fn=None, tz=TZ, now=T0, primary="sensor.p",
        sensors=["sensor.p"], primary_aliases=("climate.t",), in_sleep=asleep,
    )  # fmt: skip
    call = p.sample_calls[-1]
    assert call["primary"] == "sensor.p" and call["primary_aliases"] == ("climate.t",)
    assert call["in_sleep"] is asleep


# --------------------------------------------------------------------------------------- run state / live inputs
def test_run_state_from_rows_measures_the_running_burst():
    base = datetime(2026, 1, 10, 6, 0, tzinfo=TZ)
    rows = [
        {"ts": (base + timedelta(minutes=5 * k)).isoformat(), "hvac": "idle" if k < 3 else "heating"} for k in range(10)
    ]
    now = base + timedelta(minutes=45)
    action, minutes = v4_shadow.run_state_from_rows(rows, now)
    assert action == "heating" and minutes == pytest.approx(30.0)  # first heating row at +15 min
    assert v4_shadow.run_state_from_rows(rows[:3], now) == (None, None)
    assert v4_shadow.run_state_from_rows([], now) == (None, None)


def test_run_cycle_passes_the_running_burst_and_the_ca_sleep_node_to_the_engine(monkeypatch):
    fake = install(monkeypatch)
    model = FakeModel(primary=HALL, sensors=(HALL, SLEEP))
    md = fake.to_dict(model)
    inputs = [
        fake.V4Input(ts=_NOW + timedelta(minutes=15 * k), outdoor_f=40.0, heat_sp_f=68.0, cool_sp_f=None)
        for k in range(1, 5)
    ]
    v4_shadow.run_cycle(
        model_dict=md, now=_NOW, tz=UTC, readings={HALL: 70.0, SLEEP: 66.0}, mass_f=69.0, mass_steps=1,
        sample_fields={"outdoor_f": 40.0, "forecast_outdoor_f": 40.0, "hvac_action": "heating", "fan_running": False,
                       "windows_open": False, "heat_sp_f": 68.0, "cool_sp_f": None},
        inputs=inputs, hvac_action_now="heating", run_elapsed_min=25.0, ca_sleep_node=SLEEP,
        ca_sleep_time="22:30", ca_wake_time="07:00",
    )  # fmt: skip
    extra = fake.predict_calls[-1]["extra"]
    assert extra["hvac_action_now"] == "heating" and extra["run_elapsed_min"] == 25.0
    assert extra["ca_sleep_node"] == SLEEP and extra["ca_wake_time"] == "07:00"
    v4_shadow.run_cycle(
        model_dict=md, now=_NOW, tz=UTC, readings={HALL: 70.0}, mass_f=69.0, mass_steps=1,
        sample_fields={"outdoor_f": 40.0, "forecast_outdoor_f": 40.0, "hvac_action": "idle", "fan_running": False,
                       "windows_open": False, "heat_sp_f": 68.0, "cool_sp_f": None},
        inputs=inputs,
    )  # fmt: skip
    assert fake.predict_calls[-1]["extra"] == {}  # idle thermostat, no sleep sensor: nothing extra is sent


# ---------------------------------------------------------------------------------------------- M3: the _ca line
def _predict_both(primary_v: float, sleep_v: float):
    start = datetime(2026, 1, 10, 20, 0, tzinfo=TZ)
    pts = p4.grid_steps(start, 40)
    return start, {HALL: [(t, primary_v) for t in pts], SLEEP: [(t, sleep_v) for t in pts]}


def test_engine_predict_returns_the_ca_composite_when_a_sleep_node_is_modelled():
    samples, _t, _ = simulate(T0, 12, bed=None, noise=0.03)
    model = p4.fit(samples, primary=HALL, sensors=[HALL], tz=TZ, now=samples[-1].ts)
    model = p4.V4Model(**{**model.__dict__, "sensors": (HALL, SLEEP), "nodes": {SLEEP: dict(p4.NODE_START)}})
    ins = [p4.V4Input(ts, 40.0, 68.0, None) for ts in p4.grid_steps(datetime(2026, 1, 25, 20, 0, tzinfo=TZ), 24)]
    out = p4.predict(
        model, now=datetime(2026, 1, 25, 20, 0, tzinfo=TZ), readings={HALL: 68.0, SLEEP: 64.0}, mass_f=None, inputs=ins,
        tz=TZ, ca_sleep_node=SLEEP, ca_sleep_time="22:30", ca_wake_time="07:00",
    )  # fmt: skip
    assert p4.CA_NODE in out
    for (t, ca), (_, prim), (_, bed) in zip(out[p4.CA_NODE], out[HALL], out[SLEEP], strict=True):
        asleep = p4.in_sleep_window(t.astimezone(TZ), "22:30", "07:00")
        assert ca == (bed if asleep else prim)
    plain = p4.predict(
        model,
        now=datetime(2026, 1, 25, 20, 0, tzinfo=TZ),
        readings={HALL: 68.0, SLEEP: 64.0},
        mass_f=None,
        inputs=ins,
        tz=TZ,
    )
    assert p4.CA_NODE not in plain  # without a sleep node _ca is the primary and callers derive it


def test_archive_derives_ca_from_the_sleep_node_inside_the_window(tmp_path):
    arch = V4Archive(tmp_path, entry_id="zoneA")
    arch.set_ca_source(HALL, sleep=SLEEP, sleep_time="22:30", wake_time="07:00", tz="America/Los_Angeles")
    tz = ZoneInfo("America/Los_Angeles")
    day = datetime(2026, 1, 10, 12, 0, tzinfo=tz)
    for k in range(48):  # 24 h of 30-min slots, the 4 h lead
        epoch = slot_key(day + timedelta(minutes=30 * k))
        arch.put(HALL, epoch, "4h", 70.0)
        arch.put(SLEEP, epoch, "4h", 65.0)
    got = arch.series("_ca", "4h", 0, 1e12)
    assert set(arch.sensor_ids()) == {"_ca", HALL, SLEEP}
    for epoch, v in got.items():
        asleep = p4.in_sleep_window(datetime.fromtimestamp(epoch, tz), "22:30", "07:00")
        assert v == (65.0 if asleep else 70.0)
    assert 65.0 in got.values() and 70.0 in got.values()
    assert arch.data["_ca"] == {e: {"4h": v} for e, v in got.items()}  # the legacy view agrees
    # nothing was stored for "_ca": a save holds the two node lines and the derivation recipe
    arch.save()
    import json

    raw = json.loads(arch._path.read_text())
    assert set(raw["sensors"]) == {HALL, SLEEP} and raw["ca"]["sleep"] == SLEEP
    again = V4Archive(tmp_path, entry_id="zoneA")
    again.load()
    assert again.series("_ca", "4h", 0, 1e12) == got


def test_a_legacy_archive_with_a_stored_ca_line_drops_the_duplicate_once_the_source_is_known(tmp_path):
    arch = V4Archive(tmp_path, entry_id="zoneA")
    for k in range(10):
        epoch = slot_key(_NOW + timedelta(minutes=30 * k))
        arch.put(HALL, epoch, "4h", 68.0)
        arch.put("_ca", epoch, "4h", 68.0)
    assert set(arch.data) == {HALL, "_ca"}
    arch.set_ca_source(HALL)
    assert "_ca" not in arch._lines and "_ca" in arch.data  # derived from now on, same values
    assert arch.series("_ca", "4h", 0, 1e12) == arch.series(HALL, "4h", 0, 1e12)


# --------------------------------------------------------------------------------------------- M4: archive memory
def test_archive_memory_is_compact_at_400_days(tmp_path):
    n_slots = 400 * 48
    base = int(_NOW.timestamp()) // 1800 * 1800 - n_slots * 1800
    arch = V4Archive(tmp_path, entry_id="zoneA")
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    for sensor in (HALL, "sensor.b", SLEEP):
        for k in range(n_slots):
            for lead in V4_ARCHIVE_LEADS:
                arch.put(sensor, base + k * 1800, lead, 68.0 + (k % 7) * 0.1)
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    arch.set_ca_source(HALL, sleep=SLEEP, sleep_time="22:30", wake_time="07:00", tz="UTC")
    held = after - before
    assert held < 5 * 1024 * 1024, f"{held / 1e6:.1f} MB for 3 sensor lines x 400 days x 4 leads"
    assert arch.memory_bytes() < 5 * 1024 * 1024
    assert arch.slot_count()[HALL] == n_slots
    # values survive the float32 columns at 2 dp
    assert arch.series(HALL, "1h", 0, 1e12)[base] == pytest.approx(68.0)


def test_first_write_wins_and_prune_work_on_the_columns(tmp_path):
    arch = V4Archive(tmp_path, entry_id="zoneA", max_days=10)
    old = int(_NOW.timestamp()) - 30 * 86400
    old = old // 1800 * 1800
    cur = int(_NOW.timestamp()) // 1800 * 1800
    assert arch.put(HALL, old, "4h", 60.0) and not arch.put(HALL, old, "4h", 99.0)
    assert arch.series(HALL, "4h", 0, 1e12)[old] == 60.0
    arch.put(HALL, cur, "4h", 70.0)
    arch.record(_NOW, {HALL: [(_NOW + timedelta(hours=4), 71.0)]}, primary=HALL)
    assert old not in arch.series(HALL, "4h", 0, 1e12)  # pruned by retention
    assert arch.series(HALL, "4h", 0, 1e12)[cur] == 70.0


# ------------------------------------------------------------------------------------------- M5: stagger and lock
def test_fit_offsets_are_stable_bounded_and_spread():
    ids = [f"01M1YCV4FDN88SGQSM8K8YCV{c}" for c in "ABCDEFGH"]
    offs = [v4_shadow.fit_offset_s(i, V4_FIT_STAGGER_S) for i in ids]
    assert all(0 <= o < V4_FIT_STAGGER_S for o in offs)
    assert offs == [v4_shadow.fit_offset_s(i, V4_FIT_STAGGER_S) for i in ids]  # stable across restarts
    assert len(set(offs)) >= 7  # eight zones do not fit in the same second
    assert max(offs) - min(offs) > 600  # and are spread over many minutes
    assert V4_FIT_STAGGER_S == 30 * 60


# -------------------------------------------------------------------------- bounded training window (live == backtest)
def test_rows_in_window_keeps_the_last_n_days_only():
    base = datetime(2026, 1, 1, tzinfo=TZ)
    rows = [{"ts": (base + timedelta(days=d)).isoformat()} for d in range(0, 100, 5)] + [{"ts": "garbage"}, {}]
    now = base + timedelta(days=95)
    got = v4_shadow.rows_in_window(rows, now, 30)
    assert [r["ts"][:10] for r in got] == [(base + timedelta(days=d)).date().isoformat() for d in range(65, 100, 5)]


def test_run_fit_trains_on_the_bounded_window_like_the_backtest():
    samples, _t, _ = simulate(T0, 40, base=44.0, amp=7.0, noise=0.03)
    rows = _rows_from(samples, sensor_map_from=0)
    now = samples[-1].ts
    full, *_ = v4_shadow.run_fit(chart_entries=rows, snapshots_fn=None, tz=TZ, now=now, primary=HALL, sensors=[HALL])
    last20, *_ = v4_shadow.run_fit(
        chart_entries=rows, snapshots_fn=None, tz=TZ, now=now, primary=HALL, sensors=[HALL], train_days=20
    )
    assert full["fit"]["primary_samples"] > 35 * 96
    assert 19 * 96 <= last20["fit"]["primary_samples"] <= 21 * 96
