"""Issue #1432: v4's decisions forecast each room with its own model once both rooms have a few days of readings.

Occupant view: pre-heat and pre-cool timing, the ODE ceiling guard, the nat-vent close time and the setback depth read
v4's forecast of the reading Climate Advisor uses - the thermostat's room by day, the bedroom inside the sleep window.
v4 forecast that reading two ways: A, one model of the stitched reading, and B, a node per room composed by time of
day. The whole-window coverage rule kept B away for about three weeks after per-sensor logging began, and in between it
would have forecast sleep hours from the hallway node. Now:

* B is used as soon as BOTH the thermostat and the bedroom sensor have 3+ days of their own readings (the #1422 rule),
  and only when B's forecast of the CA reading is no worse than A's on this home's own history (the backtest gate);
* sleep hours are never forecast from the thermostat's room while a separate sleep sensor exists (``ca_line`` guard);
* every fit logs which model the decisions use and why, and a change is an Activity row in the user's unit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS
from tests.test_predictor_v4 import Plant, band, outdoor_fn
from tests.test_v4_display_nodes_1422 import BED, BED_HOUSE, BED_NODE, END, THERMO, TZ, _asleep, _cycle_kwargs, _rows


def _house_log(*, days: int = 30, thermo_days: float = 6.0, bed_days: float = 6.0, stitch: bool = True) -> list[dict]:
    """15-minute rows of a simulated heated house with a bedroom. ``indoor`` (the CA reading) is the bedroom inside
    the sleep window when ``stitch`` (a sleep sensor is configured), else the hallway; the thermostat's and the
    bedroom's own ``indoor_sensors`` readings start ``thermo_days`` / ``bed_days`` before the end."""
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
            "indoor": bed if (stitch and _asleep(ts)) else hall,
            "outdoor": round(o, 2),
            "pred_outdoor": round(o, 2),
            "setpoint": hsp,
            "lower": hsp,
            "upper": 76.0,
        }
        sens = {}
        age = (END - ts).total_seconds()
        if age <= thermo_days * 86400.0:
            sens[THERMO] = hall
        if age <= bed_days * 86400.0:
            sens[BED] = bed
        if sens:
            row["indoor_sensors"] = sens
        rows.append(row)
    return rows


def _fit(rows: list[dict], *, sleep: str | None = BED, previous: dict | None = None) -> dict:
    model_dict, _s, _line = v4_shadow.run_fit(
        chart_entries=rows,
        snapshots_fn=None,
        tz=TZ,
        now=END,
        primary=THERMO,
        sensors=[THERMO, BED],
        in_sleep=_asleep if sleep else None,
        train_days=30,
        sleep_sensor=sleep,
        previous_model=previous,
    )
    return model_dict


@pytest.fixture(scope="module")
def live_like() -> tuple[list[dict], dict]:
    """The occupant's home: 30 days of the stitched CA reading, the thermostat and bedroom for the last 6 days."""
    rows = _house_log()
    return rows, _fit(rows)


# ------------------------------------------------------------------------------------------------ the rule
class TestTheRule:
    def test_both_rooms_with_three_days_are_a_per_room_candidate(self):
        rows = _rows({THERMO: (4.0, 1), BED: (4.0, 1)})
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[THERMO, BED], sleep_sensor=BED, tz=TZ)
        assert plan.per_room == (THERMO, [THERMO, BED]) and plan.stitched == ("_ca", ["_ca"])
        assert plan.record["primary_ok"] and plan.record["sleep_ok"]
        assert plan.record["primary_days"] >= 3.0 and plan.record["sleep_days"] >= 3.0
        assert (p4.NODE_MIN_HISTORY_DAYS, p4.NODE_MIN_COVERAGE) == (3.0, 0.5)  # the #1422 constants, reused

    def test_primary_ready_but_bedroom_not_stays_stitched(self):
        rows = _rows({THERMO: (6.0, 1), BED: (2.0, 1)})
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[THERMO, BED], sleep_sensor=BED, tz=TZ)
        assert plan.per_room is None and plan.record["reason"] == "sleep_not_ready"
        assert plan.record["primary_ok"] is True and plan.record["sleep_ok"] is False

    def test_the_old_whole_window_primary_alone_no_longer_forecasts_sleep_hours(self):
        """A primary that passes the old 80 % rule (every row reads ``indoor`` outside a sleep window) with a sleep
        sensor configured but not modelled: before #1432 that model's ``_ca`` took the hallway at night."""
        rows = _rows({BED: (1.0, 1)})
        assert p4.resolve_nodes(rows, primary=THERMO, sensors=[BED], tz=TZ)[0] == THERMO  # the old rule
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[BED], sleep_sensor=BED, tz=TZ)
        assert plan.per_room is None and plan.record["reason"] == "sleep_not_ready"

    def test_no_sleep_sensor_only_the_primary_must_qualify(self):
        rows = _rows({THERMO: (4.0, 1)})
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[THERMO], sleep_sensor=None, tz=TZ)
        assert plan.per_room == (THERMO, [THERMO]) and plan.sleep_node is None
        # the sleep sensor equal to the primary is no separate sleep sensor either
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[THERMO], sleep_sensor=THERMO, tz=TZ)
        assert plan.per_room == (THERMO, [THERMO])

    def test_a_young_primary_stays_stitched(self):
        rows = _rows({THERMO: (2.0, 1), BED: (6.0, 1)})
        for r in rows:  # ``indoor`` inside the sleep window is the bedroom: it does not count for the primary
            if _asleep(datetime.fromisoformat(r["ts"])):
                r["indoor"] = None
        rows = [{k: v for k, v in r.items() if k != "indoor"} for r in rows]  # no legacy rows to stitch from
        plan = p4.resolve_control_nodes(
            rows, primary=THERMO, sensors=[THERMO, BED], sleep_sensor=BED, in_sleep=_asleep, tz=TZ
        )
        assert plan.per_room is None and plan.record["reason"] == "primary_not_ready"

    def test_legacy_primary_sources_count_for_the_primary(self):
        """``indoor_primary`` rows (before per-sensor logging) are the primary's own readings, as in the fit."""
        rows = _rows({BED: (6.0, 1)})
        for r in rows:
            r.pop("indoor", None)
            r["indoor_primary"] = 69.0
        plan = p4.resolve_control_nodes(rows, primary=THERMO, sensors=[BED], sleep_sensor=BED, tz=TZ)
        assert plan.per_room == (THERMO, [THERMO, BED])
        assert p4.sensor_history(rows, THERMO, tz=TZ, primary_aliases=())["days"] >= 29.0


class TestTheGate:
    def test_both_rooms_ready_and_no_worse_uses_per_room(self, live_like):
        _rows_, md = live_like
        nc = md["fit"]["node_choice"]
        assert md["primary"] == THERMO and md["sensors"] == [THERMO, BED]  # the per-room model is what control reads
        assert nc["model"] == p4.MODEL_PER_ROOM and nc["reason"] == "per_room_no_worse"
        mae = nc["scores"]["mae"]
        for lead in p4.CA_GATE_LEADS:
            assert mae["per_room"][lead] <= mae["stitched"][lead] + p4.CA_GATE_TOLERANCE_F
        assert nc["scores"]["n_anchors"] >= p4.GATE_MIN_ANCHORS
        assert md["display_nodes"] == {}  # both rooms are control nodes now, not display-only

    def test_bedroom_not_ready_stays_stitched(self):
        md = _fit(_house_log(bed_days=2.0))
        nc = md["fit"]["node_choice"]
        assert md["primary"] == "_ca" and nc["model"] == p4.MODEL_STITCHED and nc["reason"] == "sleep_not_ready"
        assert "scores" not in nc  # B was never a candidate: no second fit

    def test_no_sleep_sensor_and_primary_ready_uses_per_room(self):
        md = _fit(_house_log(stitch=False), sleep=None)
        nc = md["fit"]["node_choice"]
        assert md["primary"] == THERMO and nc["model"] == p4.MODEL_PER_ROOM, nc
        assert BED not in md["sensors"]  # not a control node: no sleep sensor, so BED is just an extra (display)

    def test_gate_fails_stays_stitched_and_says_why(self):
        """No sleep sensor configured but ``indoor`` IS the bedroom at night: the thermostat's own model forecasts
        the CA reading worse, so the stitched model stands (the gate catches what the coverage rule cannot)."""
        md = _fit(_house_log(stitch=True), sleep=None)
        nc = md["fit"]["node_choice"]
        assert md["primary"] == "_ca" and nc["model"] == p4.MODEL_STITCHED and nc["reason"] == "per_room_scored_worse"
        mae = nc["scores"]["mae"]
        assert mae["per_room"]["4h"] > mae["stitched"]["4h"] + p4.CA_GATE_TOLERANCE_F
        head, why = v4_shadow.describe_node_choice(nc)
        assert "stitched" in head and "scored" in why and "vs" in why and "at most 0.05°F worse" in why

    def test_gate_verdicts(self):
        def sc(a4, b4, *, n=100, a12=1.0, b12=1.0):
            return {"n_anchors": n, "mae": {"stitched": {"1h": 0.3, "4h": a4, "12h": a12},
                                            "per_room": {"1h": 0.3, "4h": b4, "12h": b12}}}  # fmt: skip

        assert p4.ca_gate(sc(0.50, 0.55)) == (True, "per_room_no_worse")  # within the tolerance
        assert p4.ca_gate(sc(0.50, 0.56)) == (False, "per_room_scored_worse")
        assert p4.ca_gate(sc(0.50, 0.40, a12=1.0, b12=1.2)) == (False, "per_room_scored_worse")  # 12 h checked too
        assert p4.ca_gate(sc(0.50, 0.40, n=5)) == (False, "too_few_shared_anchors")


# ----------------------------------------------------------------------------------------- never the wrong room
class TestNeverTheWrongRoom:
    def _preds(self) -> dict[str, list[tuple[datetime, float]]]:
        times = p4.grid_steps(END, 96)  # 12:00 -> 12:00 the next day: crosses the 22:00-06:00 sleep window
        return {THERMO: [(t, 70.0) for t in times]}

    def test_ca_line_drops_sleep_hours_the_bedroom_cannot_answer(self):
        preds = self._preds()
        line = p4.ca_line(preds, primary=THERMO, sleep_node=BED, sleep_time="22:00", wake_time="06:00", tz=TZ)
        assert line and all(not _asleep(t) for t, _v in line)  # no sleep hour from the hallway
        assert len(line) < len(preds[THERMO])
        # a partial bedroom line: the hours it answers are the bedroom's, the rest of the night stays empty
        night = [t for t, _ in preds[THERMO] if _asleep(t)]
        preds[BED] = [(t, 64.0) for t in night[:4]]
        line = p4.ca_line(preds, primary=THERMO, sleep_node=BED, sleep_time="22:00", wake_time="06:00", tz=TZ)
        assert [v for t, v in line if _asleep(t)] == [64.0] * 4

    def test_the_stitched_reading_still_stands_in_at_night(self):
        times = p4.grid_steps(END, 96)
        preds = {"_ca": [(t, 69.0) for t in times]}
        line = p4.ca_line(preds, primary="_ca", sleep_node=BED, sleep_time="22:00", wake_time="06:00", tz=TZ)
        assert line == preds["_ca"]
        no_sleep = p4.ca_line(self._preds(), primary=THERMO, sleep_node=None, sleep_time=None, wake_time=None, tz=TZ)
        assert no_sleep == self._preds()[THERMO]  # no sleep sensor: the thermostat IS the CA reading

    def test_predict_never_composes_sleep_hours_from_the_primary(self, live_like):
        rows, md = live_like
        model = p4.from_dict(md)
        kw = _cycle_kwargs(md, rows)
        common = {"now": END, "mass_f": None, "inputs": kw["inputs"], "tz": TZ}
        win = {"ca_sleep_node": BED, "ca_sleep_time": "22:00", "ca_wake_time": "06:00"}
        full = p4.predict(model, readings=kw["readings"], **common, **win)
        assert any(_asleep(t) for t, _ in full["_ca"])  # the bedroom answers the night
        assert all(v == dict(full[BED])[t] for t, v in full["_ca"] if _asleep(t))
        missing = {k: v for k, v in kw["readings"].items() if k != BED}  # bedroom unavailable this cycle
        res = p4.predict(model, readings=missing, **common, **win)
        assert BED not in res and "_ca" in res
        assert res["_ca"] and all(not _asleep(t) for t, _ in res["_ca"])
        # a room-node model with no bedroom node at all (an older stored model): same guard
        older = p4.from_dict({**md, "sensors": [THERMO], "nodes": {}})
        res = p4.predict(older, readings=kw["readings"], **common, **win)
        assert all(not _asleep(t) for t, _ in res["_ca"])

    def test_the_control_path_never_copies_the_primary_into_sleep_hours(self, live_like):
        """``V4Engine._paths`` copies the primary into ``_ca`` only when ``_ca`` is absent; it is now always there."""
        from types import SimpleNamespace

        from custom_components.climate_advisor import prediction_engines

        rows, md = live_like
        kw = _cycle_kwargs(md, rows)
        kw["readings"] = {k: v for k, v in kw["readings"].items() if k != BED}
        preds, _m = v4_shadow.run_cycle(baseline=True, **kw)
        src = SimpleNamespace(
            _v4_store=SimpleNamespace(v4_model=md), _v4_control_future={"at": END.isoformat(), "predictions": preds}
        )
        path = prediction_engines.ENGINES["v4"].control_path(src, END)
        assert path["_ca"] and all(not _asleep(t) for t, _ in path["_ca"])

    def test_the_no_plan_chart_line_composes_ca_too(self, live_like):
        rows, md = live_like
        kw = _cycle_kwargs(md, rows)
        _corrected, baseline = v4_shadow.chart_extras(
            model_dict=md, now=END, tz=TZ, readings=kw["readings"], mass_f=None, inputs=kw["inputs"],
            ca_sleep_node=BED, ca_sleep_time="22:00", ca_wake_time="06:00",
        )  # fmt: skip
        assert all(v == dict(baseline[BED])[t] for t, v in baseline["_ca"] if _asleep(t))

    def test_the_archive_never_scores_a_sleep_slot_from_the_hallway(self):
        from pathlib import Path

        from custom_components.climate_advisor.v4_archive import V4Archive

        arch = V4Archive(Path("/nonexistent"), entry_id="x")
        now = END
        times = p4.grid_steps(now, 96)
        arch.set_ca_source(THERMO, sleep=BED, sleep_time="22:00", wake_time="06:00", tz="America/Los_Angeles")
        arch.record(now, {THERMO: [(t, 70.0) for t in times]}, THERMO)  # no bedroom line this cycle
        ca = arch.data.get("_ca", {})
        night = [e for e in ca if _asleep(datetime.fromtimestamp(e, TZ))]
        assert ca and not night


# ---------------------------------------------------------------------------------------------- logging
def _note(md_prev: dict | None, md_new: dict, *, unit: str = "fahrenheit") -> tuple[list[tuple[str, dict]], Any]:
    from types import SimpleNamespace

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    events: list[tuple[str, dict]] = []
    fake = SimpleNamespace(
        config={"temp_unit": unit},
        zone_label="Zone 1",
        v4_indoor_sensors_meta=lambda ids: [{"id": i, "name": {THERMO: "Hallway", BED: "Bedroom"}[i]} for i in ids],
        _emit_event=lambda t, d: events.append((t, d)),
    )
    ClimateAdvisorCoordinator._v4_note_node_choice(fake, "nightly", md_prev, md_new)  # type: ignore[arg-type]
    return events, fake


class TestLogging:
    @pytest.fixture(scope="class")
    def stitched(self) -> dict:
        return _fit(_house_log(bed_days=2.0))

    def test_the_change_to_per_room_is_one_activity_row(self, live_like, stitched, caplog):
        _rows_, per_room = live_like
        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"):
            events, _f = _note(stitched, per_room)
        assert [t for t, _ in events] == ["forecast_model_changed"]
        ev = events[0][1]
        assert (ev["from_model"], ev["to_model"]) == ("stitched", "per_room")
        label, detail = EVENT_RENDERERS["forecast_model_changed"](ev, "fahrenheit")
        assert label == "v4 now forecasts each room separately (thermostat by day, bedroom at night)"
        assert "both rooms have 3+ days of readings (Hallway 6.0 days, Bedroom 6.0 days)" in detail
        assert "the per-room forecast scored" in detail and "°F vs " in detail
        line = next(r.getMessage() for r in caplog.records if "decisions use the" in r.getMessage())
        assert "per_room" in line and "Hallway" in line and "Bedroom" in line and "°F" in line

    def test_a_refit_without_a_change_writes_no_row_but_logs(self, live_like, caplog):
        _rows_, per_room = live_like
        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"):
            events, _f = _note(per_room, dict(per_room))
        assert events == []
        assert any("decisions use the per_room model" in r.getMessage() for r in caplog.records)
        assert _note(None, per_room)[0] == []  # the first fit ever: nothing to change from

    def test_an_older_stored_model_counts_by_its_primary(self, live_like):
        _rows_, per_room = live_like
        old_stitched = {"primary": "_ca", "sensors": ["_ca"], "fit": {}}  # written before #1432: no record
        assert [t for t, _ in _note(old_stitched, per_room)[0]] == ["forecast_model_changed"]

    def test_the_stay_on_stitched_reason_names_the_missing_bedroom(self, live_like, stitched, caplog):
        _rows_, per_room = live_like
        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"):
            events, _f = _note(per_room, stitched)
        label, detail = EVENT_RENDERERS["forecast_model_changed"](events[0][1], "fahrenheit")
        assert label.startswith("v4 now forecasts from the stitched Climate Advisor reading")
        assert "Bedroom has only 2.0 days of its own readings (needs 3)" in detail
        assert "never forecast from Hallway's room" in detail

    def test_messages_are_in_the_users_unit(self, live_like, stitched):
        _rows_, per_room = live_like
        events, _f = _note(stitched, per_room, unit="celsius")
        _label, detail_c = EVENT_RENDERERS["forecast_model_changed"](events[0][1], "celsius")
        assert "°C" in detail_c and "°F" not in detail_c
        b4 = per_room["fit"]["node_choice"]["scores"]["mae"]["per_room"]["4h"]
        assert f"{b4 * 5 / 9:.2f}°C" in detail_c  # a difference: scale only, no offset

    def test_the_activity_row_type_is_catalogued_and_unmapped(self):
        from custom_components.climate_advisor.ai_skills_context import _NO_DEDUP
        from tools.sim_harness.outcomes import UNMAPPED_PRODUCTION_EVENTS

        assert "forecast_model_changed" in _NO_DEDUP and "forecast_model_changed" in UNMAPPED_PRODUCTION_EVENTS
        assert EVENT_RENDERERS["forecast_model_changed"]({}, "fahrenheit")[0]  # never blank


class TestCoordinatorFit:
    """The real coordinator: the fit stores the model, logs the choice and emits ONE row only on a change."""

    def test_a_to_b_emits_once_and_a_refit_emits_none(self, monkeypatch, live_like):
        from tests.test_v4_shadow_wiring import _build, _fake_model_dict, _seed_cycle_inputs
        from tests.v4_fake_predictor import install

        install(monkeypatch)
        coord, _hass, scheduler = _build()
        _seed_cycle_inputs(coord, scheduler)
        _rows_, per_room = live_like
        stitched = _fake_model_dict(coord, primary="_ca", sensors=["_ca"])
        coord._v4_store.v4_model = stitched
        coord._v4_store.night_validation = {"n_nights_replayed": 0}
        seen: list[dict] = []

        def fit(**kw):
            seen.append(kw)
            return dict(per_room), 1.0, "v4 fit: test"

        monkeypatch.setattr(v4_shadow, "run_fit", fit)
        coord.config["sleep_indoor_temp_entity"] = BED

        def rows() -> list[dict]:
            return [e for e in coord._event_log if e.get("type") == "forecast_model_changed"]

        with scheduler.installed():
            assert coord._v4_start_fit("nightly")
            scheduler.advance_by(0)
        assert seen[-1]["sleep_sensor"] == BED  # the configured sleep sensor reaches the rule
        assert coord._v4_store.v4_model["primary"] == THERMO
        assert len(rows()) == 1 and rows()[0]["to_model"] == "per_room"
        with scheduler.installed():
            assert coord._v4_start_fit("nightly")
            scheduler.advance_by(0)
        assert len(rows()) == 1  # same choice again: no second row
        summary = coord.v4_model_summary()
        assert summary["forecast_model"]["model"] == "per_room"
        assert summary["forecast_model"]["text"].startswith("v4 forecasts each room separately")
        assert "node_choice" not in summary  # raw degrees F stay out of the debug payload

    def test_the_sleep_node_is_passed_for_a_room_node_model(self, monkeypatch, live_like):
        from tests.test_v4_shadow_wiring import _build

        coord, _hass, _scheduler = _build()
        coord.config.update(sleep_indoor_temp_entity=BED, sleep_time="22:00", wake_time="06:00")
        _rows_, per_room = live_like
        assert coord._v4_sleep_node(per_room) == (BED, "22:00", "06:00")
        older = {**per_room, "sensors": [THERMO]}  # a room primary without the bedroom node: still guarded
        assert coord._v4_sleep_node(older)[0] == BED
        assert coord._v4_sleep_node({"primary": "_ca", "sensors": ["_ca"]})[0] is None  # stitched: as before


# ----------------------------------------------------------------------------------------- the backtest mirror
class TestBacktestMirror:
    def test_the_backtest_week_fit_makes_the_live_choice(self, live_like):
        from tools import predict_backtest as pb

        rows, live = live_like
        info: dict[str, Any] = {}
        fitted = pb.fit_week_shared(
            p4,
            [r for r in rows if datetime.fromisoformat(r["ts"]) < END],
            [],
            W=END,
            cand_primary=THERMO,
            cand_nodes=[THERMO, BED],
            sleep_node=BED,
            aliases=(),
            in_sleep=_asleep,
            tz=TZ,
            min_train_days=3.0,
            info=info,
        )
        assert fitted is not None
        model, primary, nodes = fitted
        assert (primary, nodes) == (live["primary"], live["sensors"]) == (THERMO, [THERMO, BED])
        assert info["node_choice"]["model"] == live["fit"]["node_choice"]["model"] == "per_room"

    def test_backtest_ca_scoring_never_takes_the_hallway_at_night(self):
        from tools import predict_backtest as pb

        night = datetime(2026, 11, 7, 23, 0, tzinfo=TZ)
        t = int(night.timestamp())
        v4 = {THERMO: {t: 70.0}}
        args = (night, BED, _asleep)
        assert pb._v4_value(v4, THERMO, "_ca", t, *args, [THERMO, BED], TZ) is None  # bedroom has no value
        assert pb._v4_value(v4, THERMO, "_ca", t, *args, [THERMO], TZ) is None  # bedroom not modelled
        assert pb._v4_value({"_ca": {t: 69.0}}, "_ca", "_ca", t, *args, ["_ca"], TZ) == 69.0  # stitched stands in
