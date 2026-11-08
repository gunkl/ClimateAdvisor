"""Issue #1422: each indoor sensor gets its own v4 line once it has a few days of ITS OWN history (display only).

Occupant view: on the Status chart the thermostat and the bedroom sensor said "v4 is still learning" for about two
weeks after per-sensor logging began, although each already had days of readings: the node rule judged coverage over
the whole 30-day window. Now a sensor is judged over the span since its own first ``indoor_sensors`` row (at least
``NODE_MIN_HISTORY_DAYS``), fitted after the control fit with the house held, and drawn on the chart. Nothing that
decides reads these nodes: pre-heat/pre-cool timing, the ceiling guard, the nat-vent close time, the setback depth and
tonight's bedroom plan keep reading ``_ca`` exactly as before - pinned below by a parity test on the control accessors.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.climate_advisor import prediction_accuracy as pa
from custom_components.climate_advisor import prediction_engines, v4_shadow
from custom_components.climate_advisor import predictor_v4 as p4
from tests.test_predictor_v4 import TZ, Plant, band, outdoor_fn

THERMO = "climate.hallway"
BED = "sensor.bedroom_temperature"
SPARSE = "sensor.flaky"
OLD = "sensor.office"  # a sensor with the whole window: the CONTROL rule already takes it
END = datetime(2026, 11, 7, 12, 0, tzinfo=TZ)
BED_HOUSE = {"a_o": 0.05, "a_w": 0.15, "a_f": 0.5, "c_hm": 0.8, "c_mh": 0.3, "s": 0.6, "phase": 1.0, "q_h": 9.0,
             "q_c": 11.0}  # fmt: skip
BED_NODE = {"b_h": 1.2, "b_o": 0.08, "b_w": 0.2, "s_b": 0.2, "q_b": 3.0}


def _asleep(dt: datetime) -> bool:
    h = dt.astimezone(TZ).hour
    return h >= 22 or h < 6


# --------------------------------------------------------------------------------------------- coverage rule
def _rows(sensor_days: dict[str, tuple[float, int]], *, days: float = 30.0) -> list[dict[str, Any]]:
    """30-minute chart rows over ``days`` ending at END. ``sensor_days[sid] = (days of history, every)``: the sensor
    appears in ``indoor_sensors`` for its last N days and reads on one row in ``every`` (null on the others)."""
    n = int(days * 48)
    rows = []
    for k in range(n):
        ts = END - timedelta(minutes=30 * (n - 1 - k))
        row: dict[str, Any] = {"ts": ts.isoformat(), "hvac": "idle", "fan": False, "indoor": 69.0, "outdoor": 50.0}
        sens = {}
        for sid, (hist, every) in sensor_days.items():
            if (END - ts).total_seconds() <= hist * 86400.0:
                sens[sid] = 69.5 if k % every == 0 else None
        if sens:
            row["indoor_sensors"] = sens
        rows.append(row)
    return rows


class TestCoverageRule:
    def test_four_days_of_good_rows_qualify(self):
        rows = _rows({BED: (4.0, 1)})
        assert p4.resolve_display_nodes(rows, nodes=["_ca"], primary=THERMO, sensors=[BED], tz=TZ) == [BED]

    def test_two_days_do_not(self):
        rows = _rows({BED: (2.0, 1)})
        assert p4.resolve_display_nodes(rows, nodes=["_ca"], primary=THERMO, sensors=[BED], tz=TZ) == []
        assert p4.NODE_MIN_HISTORY_DAYS == 3.0

    def test_a_sparse_sensor_fails_coverage(self):
        rows = _rows({SPARSE: (10.0, 3)})  # 10 days of history, a reading on a third of its rows
        assert p4.resolve_display_nodes(rows, nodes=["_ca"], primary=THERMO, sensors=[SPARSE], tz=TZ) == []
        rows = _rows({SPARSE: (10.0, 1)})  # the same sensor reading on every row qualifies
        assert p4.resolve_display_nodes(rows, nodes=["_ca"], primary=THERMO, sensors=[SPARSE], tz=TZ) == [SPARSE]
        rows = _rows({SPARSE: (10.0, 2)})  # every other row, null on the first: just under the 50 % bar
        assert p4.resolve_display_nodes(rows, nodes=["_ca"], primary=THERMO, sensors=[SPARSE], tz=TZ, min_cov=0.49) == [
            SPARSE
        ]

    def test_the_old_whole_window_rule_left_these_sensors_out(self):
        """The occupant's problem, at the rule level: 6 days of a 30-day window is 20 %, under the 50 % bar."""
        rows = _rows({THERMO: (6.0, 1), BED: (6.0, 1)})
        for r in rows:
            r["indoor"] = None if _asleep(datetime.fromisoformat(r["ts"])) else r["indoor"]
        prim, nodes = p4.resolve_nodes(rows, primary=THERMO, sensors=[THERMO, BED], in_sleep=_asleep, tz=TZ)
        assert (prim, nodes) == ("_ca", ["_ca"])
        display = p4.resolve_display_nodes(rows, nodes=nodes, primary=THERMO, sensors=[THERMO, BED], tz=TZ)
        assert display == [THERMO, BED]  # the thermostat (the primary entity the model fell back from) too

    def test_ca_and_the_control_nodes_are_unchanged(self):
        """``resolve_nodes`` is untouched, and a sensor the control rule takes stays a control node (never display)."""
        rows = _rows({OLD: (30.0, 1), BED: (5.0, 1)})
        prim, nodes = p4.resolve_nodes(rows, primary=THERMO, sensors=[OLD, BED], tz=TZ)
        assert prim == THERMO  # every row reads ``indoor`` outside a sleep window: the entity primary stays
        assert nodes == [THERMO, OLD]  # BED is 5/30 of the window: not a control node, exactly as before
        assert p4.resolve_display_nodes(rows, nodes=nodes, primary=THERMO, sensors=[OLD, BED], tz=TZ) == [BED]
        # no indoor_sensors at all: ``_ca`` alone, no display node
        bare = _rows({})
        assert p4.resolve_nodes(bare, primary=THERMO, sensors=[BED], tz=TZ, in_sleep=lambda _d: True)[0] == "_ca"
        assert p4.resolve_display_nodes(bare, nodes=["_ca"], primary=THERMO, sensors=[BED], tz=TZ) == []


# ------------------------------------------------------------------------------- a live-like chart log
def _chart_log(*, days: int = 30, sensor_days: float = 6.0) -> list[dict[str, Any]]:
    """15-minute chart rows of a simulated heated house with a bedroom: ``_ca`` (``indoor``: the hallway awake, the
    bedroom asleep) for ``days``, and the thermostat + bedroom per-sensor readings for the last ``sensor_days``."""
    plant = Plant(BED_HOUSE, BED_NODE)
    plant.h = plant.m = plant.b = 66.0
    start = END - timedelta(days=days)
    rows = []
    for k in range(1, days * 96 + 1):
        ts = (start.astimezone(UTC) + timedelta(minutes=15 * k)).astimezone(TZ)
        o = outdoor_fn(42.0, 8.0, ts)
        hsp, _csp = band(ts, True, False)
        uh, _uc = plant.step(ts, o, hsp, None, 0.0, 0.0)
        hall, bed = round(plant.h, 2), round(plant.b, 2)
        row: dict[str, Any] = {
            "ts": ts.isoformat(),
            "hvac": "heating" if uh > 0 else "idle",
            "fan": False,
            "fan_running": False,
            "windows_open": False,
            "indoor": bed if _asleep(ts) else hall,
            "outdoor": round(o, 2),
            "pred_outdoor": round(o, 2),
            "setpoint": hsp,
            "lower": hsp,
            "upper": 76.0,
        }
        if (END - ts).total_seconds() <= sensor_days * 86400.0:
            row["indoor_sensors"] = {THERMO: hall, BED: bed}
        rows.append(row)
    return rows


def _fit(rows, *, display: bool = True, monkeypatch: pytest.MonkeyPatch | None = None) -> dict:
    if not display:
        assert monkeypatch is not None
        monkeypatch.setattr(v4_shadow, "display_candidates", lambda *a, **k: [])
    model_dict, _s, _line = v4_shadow.run_fit(
        chart_entries=rows,
        snapshots_fn=None,
        tz=TZ,
        now=END,
        primary=THERMO,
        sensors=[THERMO, BED],
        in_sleep=_asleep,
        train_days=30,
    )
    if not display:
        monkeypatch.undo()
    return model_dict


def _cycle_kwargs(model_dict: dict, rows: list[dict]) -> dict[str, Any]:
    last = rows[-1]
    inputs = [
        p4.V4Input(ts=t, outdoor_f=outdoor_fn(42.0, 8.0, t), heat_sp_f=band(t, True, False)[0], cool_sp_f=None)
        for t in p4.grid_steps(END, 96)
    ]
    return {
        "model_dict": model_dict,
        "now": END,
        "tz": TZ,
        "readings": {**last["indoor_sensors"], "_ca": last["indoor"]},
        "mass_f": None,
        "mass_steps": 1,
        "sample_fields": {
            "outdoor_f": last["outdoor"],
            "forecast_outdoor_f": last["pred_outdoor"],
            "hvac_action": last["hvac"],
            "fan_running": False,
            "windows_open": False,
            "heat_sp_f": last["setpoint"],
            "cool_sp_f": None,
        },
        "inputs": inputs,
        "outdoor_now_f": last["outdoor"],
        "forecast_now_f": last["pred_outdoor"],
        "ca_sleep_node": BED if BED in model_dict["sensors"] else None,
        "ca_sleep_time": "22:00",
        "ca_wake_time": "06:00",
    }


@pytest.fixture(scope="module")
def live_like():
    rows = _chart_log()
    with pytest.MonkeyPatch.context() as mp:
        plain = _fit(rows, display=False, monkeypatch=mp)
    return rows, _fit(rows), plain


def _control_keys(d: dict) -> dict:
    out = {k: v for k, v in d.items() if k != "display_nodes"}
    out["fit"] = {k: v for k, v in d["fit"].items() if k != "display"}
    return out


class TestOccupantScenario:
    def test_thermostat_and_bedroom_both_get_v4_lines(self, live_like):
        """30 days of ``_ca``, thermostat and bedroom for the last 6 days: both are drawn, future and no-plan."""
        rows, model_dict, _plain = live_like
        assert model_dict["confidence"] != "none"
        assert model_dict["primary"] == "_ca" and model_dict["sensors"] == ["_ca"]  # control model: as live today
        assert list(model_dict["display_nodes"]) == [THERMO, BED], model_dict["fit"].get("display")
        kw = _cycle_kwargs(model_dict, rows)
        preds, mass = v4_shadow.run_cycle(display=True, **kw)
        assert preds is not None and THERMO in preds and BED in preds and "_ca" in preds
        corrected, baseline = v4_shadow.chart_extras(
            model_dict=model_dict,
            now=END,
            tz=TZ,
            readings=kw["readings"],
            mass_f=mass,
            inputs=kw["inputs"],
            outdoor_now_f=kw["outdoor_now_f"],
            forecast_now_f=kw["forecast_now_f"],
        )
        v4 = pa.build_chart_v4(
            now=END,
            start=(END - timedelta(hours=24)).timestamp(),
            end=(END + timedelta(hours=24)).timestamp(),
            range_str="24h",
            unit="fahrenheit",
            historical=False,
            archive=None,
            backtest=None,
            model={"has_model": True, "confidence": model_dict["confidence"], "primary": "_ca"},
            last_future={"at": END.isoformat(), "predictions": preds, "baseline": baseline, "corrected": corrected},
            sensor_ids=[THERMO, BED],
        )
        for sid in (THERMO, BED, "_ca"):
            assert v4["future"].get(sid), sid
            assert v4["baseline"].get(sid), sid
        assert v4["sensors"] == ["_ca", THERMO, BED]

    def test_display_lines_track_their_own_sensor(self, live_like):
        """The bedroom line is the bedroom's, not a copy of the CA reading: it beats "no change" at 4 h on training."""
        _rows_, model_dict, _plain = live_like
        per = model_dict["fit"]["display"]["per_sensor"]
        for sid in (THERMO, BED):
            assert per[sid]["4h"] <= 0.9 * per[sid]["flat"]


class TestControlUnchanged:
    """Nothing control-facing reads a display node: the model, its rates and its control path are byte-identical."""

    def test_the_control_model_is_identical_with_and_without_display_nodes(self, live_like):
        _rows_, with_display, plain = live_like
        assert plain["display_nodes"] == {}
        assert _control_keys(with_display) == _control_keys(plain)

    def test_control_accessors_return_the_same_values(self, live_like):
        rows, with_display, plain = live_like
        base = {"thermal_swing_f": 1.0, "k_passive": -0.02, "confidence": "high"}
        eng = prediction_engines.ENGINES["v4"]
        eng._rates_cache = None
        a = eng.control_model(with_display, base)
        eng._rates_cache = None
        b = eng.control_model(plain, base)
        assert a == b and a["k_active_heat"] > 0
        ma, mb = p4.from_dict(with_display), p4.from_dict(plain)
        assert p4.control_rates(ma) == p4.control_rates(mb)
        # The control path: the pre-decision cycle (baseline, no ``display``) is the same with or without the nodes,
        # and the shadow cycle's ``display=True`` changes none of the lines the decisions or ``_ca`` read.
        ctl_a, _ = v4_shadow.run_cycle(baseline=True, **_cycle_kwargs(with_display, rows))
        ctl_b, _ = v4_shadow.run_cycle(baseline=True, **_cycle_kwargs(plain, rows))
        assert ctl_a == ctl_b and set(ctl_a) == {"_ca"}
        disp, _ = v4_shadow.run_cycle(baseline=True, display=True, **_cycle_kwargs(with_display, rows))
        assert {k: v for k, v in disp.items() if k == "_ca"} == ctl_a

    def test_a_display_bedroom_never_becomes_the_ca_line(self, live_like):
        """Tonight's bedroom plan and the ceiling guard read ``_ca``; a display-only bedroom node must not feed it."""
        rows, model_dict, _plain = live_like
        model = p4.from_dict(model_dict)
        kw = _cycle_kwargs(model_dict, rows)
        common = {
            "now": END,
            "readings": kw["readings"],
            "mass_f": None,
            "inputs": kw["inputs"],
            "tz": TZ,
            "ca_sleep_node": BED,  # even if a caller passed it
            "ca_sleep_time": "22:00",
            "ca_wake_time": "06:00",
        }
        with_nodes = p4.predict(model, display_nodes=True, **common)
        without = p4.predict(model, **common)
        assert with_nodes["_ca"] == without["_ca"]
        assert BED in with_nodes and BED not in without

    def test_control_extras_are_fitted_exactly_as_before(self):
        """A model with a CONTROL extra node: adding a display node leaves house, nodes, thermostat and rates alone."""
        from tests.test_predictor_v4 import BED as SBED
        from tests.test_predictor_v4 import HALL, T0, TRUE_BED, simulate

        samples, _t, _ = simulate(T0, 12, bed=TRUE_BED, noise=0.03)
        late = "sensor.late"
        samples = [
            dataclasses.replace(s, readings={**s.readings, late: s.readings[SBED] if k >= 7 * 96 else None})
            for k, s in enumerate(samples)
        ]
        kw = {"primary": HALL, "sensors": [HALL, SBED], "tz": TZ, "now": samples[-1].ts}
        plain = p4.fit(samples, **kw)
        disp = p4.fit(samples, display_sensors=[late], **kw)
        assert late in disp.display_nodes and late not in disp.sensors
        da, db = p4.to_dict(plain), p4.to_dict(disp)
        assert _control_keys(da) == _control_keys(db)
        assert p4.control_rates(plain) == p4.control_rates(disp)


class TestSerialisation:
    def test_round_trip_and_older_files(self, live_like):
        _rows_, model_dict, _plain = live_like
        m = p4.from_dict(model_dict)
        assert p4.to_dict(m) == model_dict
        old = {k: v for k, v in model_dict.items() if k != "display_nodes"}
        assert p4.from_dict(old).display_nodes == {}  # a model written before #1422 reads with none

    def test_a_bad_display_node_is_dropped_not_the_model(self, live_like):
        _rows_, model_dict, _plain = live_like
        bad = dict(model_dict)
        bad["display_nodes"] = {**model_dict["display_nodes"], "sensor.x": {"b_h": math.inf}, "_ca": {}}
        m = p4.from_dict(bad)
        assert m is not None and list(m.display_nodes) == [THERMO, BED]
        assert p4.from_dict({**model_dict, "display_nodes": "oops"}).display_nodes == {}


class TestOffCycleRefit:
    def test_a_newly_qualified_sensor_is_pending_and_folded_in_without_touching_control(self, live_like):
        rows, _model_dict, plain = live_like
        # the model the last nightly fit stored, before the sensors had 3 days: no display nodes tried yet
        pending = v4_shadow.display_pending(
            plain, rows, now=END, tz=TZ, primary=THERMO, sensors=[THERMO, BED], train_days=30
        )
        assert pending == [THERMO, BED]
        new, seconds, _line = v4_shadow.run_display_fit(
            chart_entries=rows,
            tz=TZ,
            now=END,
            primary=THERMO,
            sensors=[THERMO, BED],
            model_dict=plain,
            in_sleep=_asleep,
            train_days=30,
        )
        assert list(new["display_nodes"]) == [THERMO, BED]
        assert _control_keys(new) == _control_keys(plain)  # house, nodes, thermostat, confidence, fitted_at
        assert new["fitted_at"] == plain["fitted_at"]
        assert (
            v4_shadow.display_pending(new, rows, now=END, tz=TZ, primary=THERMO, sensors=[THERMO, BED], train_days=30)
            == []
        )  # nothing left to fold in: the 6-hourly check does not refit again
        assert seconds < 30.0

    def test_a_rejected_sensor_waits_for_the_nightly_fit(self, live_like):
        rows, _model_dict, plain = live_like
        tried = dict(plain)
        tried["fit"] = {**plain["fit"], "display": {"tried": [THERMO, BED], "kept": [], "rejected": {}}}
        assert v4_shadow.display_pending(tried, rows, now=END, tz=TZ, primary=THERMO, sensors=[THERMO, BED]) == []

    def test_no_usable_model_means_no_display_fit(self, live_like):
        rows, _model_dict, plain = live_like
        none = {**plain, "confidence": "none"}
        assert v4_shadow.display_pending(none, rows, now=END, tz=TZ, primary=THERMO, sensors=[THERMO, BED]) == []


# ------------------------------------------------------------------------------- coordinator wiring
class TestCoordinatorTrigger:
    """The 6-hourly check folds a newly qualified sensor in without a full refit (the control model stays put)."""

    def test_the_cycle_runs_a_display_fit_at_most_every_6_h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_v4_shadow_wiring import _build, _cycle, _fake_model_dict, _seed_cycle_inputs
        from tests.v4_fake_predictor import install

        fake = install(monkeypatch)
        coord, _hass, scheduler = _build()
        stored = _fake_model_dict(coord)
        coord._v4_store.v4_model = stored
        coord._v4_store.night_validation = {"n_nights_replayed": 0}  # no night replay in the way
        _seed_cycle_inputs(coord, scheduler)
        checks: list[Any] = []
        fits: list[dict] = []

        def pending(model_dict, entries, **kw):
            checks.append(kw)
            return [] if model_dict.get("display_nodes") else [BED]

        def display_fit(**kw):
            fits.append(kw)
            out = dict(kw["model_dict"])
            out["display_nodes"] = {BED: dict(p4.NODE_START)}
            return out, 0.1, "v4 display nodes: kept=[bed]"

        monkeypatch.setattr(v4_shadow, "display_pending", pending)
        monkeypatch.setattr(v4_shadow, "run_display_fit", display_fit)
        _cycle(coord, scheduler, drain=True)
        assert len(checks) == 1 and len(fits) == 1 and fits[0]["train_days"] == 30
        new = coord._v4_store.v4_model
        assert new["display_nodes"] == {BED: dict(p4.NODE_START)}
        assert {k: v for k, v in new.items() if k != "display_nodes"} == stored  # control keys untouched
        assert fake.fit_calls == []  # no full refit
        _cycle(coord, scheduler, drain=True)  # immediately again: the check waits 6 h
        assert len(checks) == 1
        coord._v4_last_display_check -= 7 * 3600
        _cycle(coord, scheduler, drain=True)
        assert len(checks) == 2 and len(fits) == 1  # nothing new pending: no fit
        assert fake.fit_calls == []

    def test_a_failed_display_fit_keeps_the_model_and_is_not_a_fit_failure(self, monkeypatch) -> None:
        from tests.test_v4_shadow_wiring import _build, _cycle, _fake_model_dict, _seed_cycle_inputs
        from tests.v4_fake_predictor import install

        install(monkeypatch)
        coord, _hass, scheduler = _build()
        stored = _fake_model_dict(coord)
        coord._v4_store.v4_model = stored
        coord._v4_store.night_validation = {"n_nights_replayed": 0}
        _seed_cycle_inputs(coord, scheduler)
        monkeypatch.setattr(v4_shadow, "display_pending", lambda *a, **k: [BED])

        def boom(**_kw):
            raise RuntimeError("bad")

        monkeypatch.setattr(v4_shadow, "run_display_fit", boom)
        _cycle(coord, scheduler, drain=True)
        assert coord._v4_store.v4_model is stored
        assert coord._v4_store.last_fit_error is None
        assert coord._v4_fit_running is False


# ------------------------------------------------------------------------------- the backtest mirror
class TestBacktestMirror:
    """``tools/predict_backtest.py`` uses the same display rule, so its published past lines match the live ones."""

    def test_a_late_sensor_is_a_display_node_and_scored(self):
        from types import SimpleNamespace

        from tests import test_predict_backtest as tpb

        class DisplayFake(tpb.FakePredictor):
            resolve_display_nodes = staticmethod(p4.resolve_display_nodes)

            def fit(self, samples, *, display_sensors=(), **kw):
                m = super().fit(samples, **kw)
                return SimpleNamespace(**vars(m), display_nodes={s: dict(p4.NODE_START) for s in display_sensors})

            def predict(self, model, *, display_nodes=False, **kw):
                res = super().predict(model, **kw)
                if res is not None and display_nodes:
                    first = next(iter(res.values()))
                    for s in model.display_nodes:
                        res[s] = [(t, v - 2.0) for t, v in first]
                return res

        old = tpb.pb._PREDICTOR
        tpb.pb._PREDICTOR = DisplayFake()
        try:
            rows = tpb.make_rows(56, sensors=42 * 48)  # per-sensor readings from day 42: 7 days before the last week
            rep = tpb.run(rows, "--no-diagnostic", "--train-days", "20")
        finally:
            tpb.pb._PREDICTOR = old
        last = rep["weeks"][-1]
        assert last["primary"] == "climate.t" and "sensor.bed" not in last["nodes"]  # control rule: as before
        assert last["display_nodes"] == ["sensor.bed"]
        assert "sensor.bed" in rep["stats"]
        assert "sensor.bed" in {s["id"] for s in rep["sensors"]}
