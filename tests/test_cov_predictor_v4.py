"""Edge branches of the v4 predictor (Silver test-coverage).

Occupant view: v4 forecasts each room from the house's own history. Stored records that are damaged or from an older
release, sensors with too little history, chart rows with odd values and forecast snapshots with bad entries must be
skipped one by one (the rest still counts), never crash the fit or put a made-up line on the chart.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta

import pytest

from custom_components.climate_advisor import predictor_v4 as p4
from tests.test_night_strategy import BED, HALL, make_model
from tests.test_night_strategy_replay import _history as replay_history
from tests.timeline_fixtures import TZ

OFFICE = "sensor.office"
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)


# ------------------------------------------------------------------------------------------------ small pure helpers
def test_sun_is_zero_outside_its_ten_hour_arc() -> None:
    assert p4._sun(5.0, 0.0) == 0.0 and p4._sun(19.0, 0.0) == 0.0
    assert p4._sun(13.0, 0.0) == pytest.approx(1.0)


def test_iso_epoch_of_garbage_is_none() -> None:
    assert p4._iso_epoch("not a time") is None and p4._iso_epoch(None) is None


def test_dot_of_non_overlapping_rows_is_zero() -> None:
    assert p4._dot((0, [1.0, 2.0]), (5, [3.0])) == 0.0


def test_blend_without_its_own_value_keeps_the_carried_one() -> None:
    assert p4._blend(3.0, None, 0.5) == 3.0 and p4._blend(None, 2.0, 0.5) == 2.0


def test_mode_grade_falls_back_to_the_model_confidence() -> None:
    assert p4._mode_grade_of(make_model(confidence="ok"), "heat") == "ok"
    assert p4._mode_grade_of(make_model(confidence="low"), "heat") == "low"


def test_jsonable_choice_turns_non_finite_numbers_into_null_in_lists() -> None:
    out = p4._jsonable_choice({"scores": [1.0, float("nan")], "t": (float("inf"),), "n": 3})
    assert out == {"scores": [1.0, None], "t": [None], "n": 3}


def test_hvac_at_reads_the_latest_row_at_or_before_the_time() -> None:
    tl = [(0.0, "idle"), (100.0, "heating"), (200.0, "idle")]
    assert p4._hvac_at(tl, 150.0) == "heating" and p4._hvac_at(tl, -1.0) is None
    assert p4._duty([], 900.0) == (None, None)


def test_archive_index_skips_bad_snapshots_and_bad_hour_pairs() -> None:
    snaps = [
        "not a mapping",
        {"issued": "junk", "hours": []},
        {
            "issued": T0.isoformat(),
            "hours": [
                [T0.isoformat(), 60.0],
                ["only-one"],
                [T0.isoformat(), None],
                5,
                [(T0 + timedelta(hours=1)).isoformat(), 61.0],
            ],
        },
    ]
    issued, tables = p4._archive_index(snaps, TZ)
    hour = int(T0.timestamp() // 3600)
    assert issued == [T0.timestamp()] and tables == [{hour: 60.0, hour + 1: 61.0}]


# ------------------------------------------------------------------------------------------------ stored records
def _carry(**kw):
    base = {"q": 9.0, "on_f": 1.0, "off_f": 1.0, "keep": 1.0, "grade": "ok"}
    base.update(kw)
    return base


def test_carry_record_validates_and_cleans_each_field() -> None:
    assert p4._carry_record(_carry(q="x")) is None  # unreadable
    assert p4._carry_record(_carry(q=float("nan"))) is None
    assert p4._carry_record(_carry(q=10_000.0)) is None  # outside the gain bounds
    rec = p4._carry_record(
        _carry(min_run_min=-3.0, burst_min=float("inf"), counted_through=float("nan"), grade="excellent", keep=7.0)
    )
    assert rec is not None
    assert rec["min_run_min"] is None and rec["burst_min"] is None and rec["counted_through"] is None
    assert rec["grade"] == "low" and rec["keep"] == 1.0


def test_from_dict_rejects_an_out_of_bounds_node_and_bad_thermostat_numbers() -> None:
    good = p4.to_dict(make_model())
    assert p4.from_dict(good) is not None
    bad_node = {**good, "nodes": {BED: {**good["nodes"][BED], "b_h": 1e6}}}
    assert p4.from_dict(bad_node) is None
    bad_step = {**good, "thermostat": {**good["thermostat"], "reading_step_f": float("nan")}}
    assert p4.from_dict(bad_step) is None


def test_display_nodes_from_drops_a_bad_entry_and_keeps_the_good() -> None:
    nd = dict(make_model().nodes[BED])
    raw = {OFFICE: nd, "sensor.bad": {**nd, "b_h": 1e6}, HALL: nd, 7: nd}
    assert p4._display_nodes_from(raw, [HALL, BED]) == {OFFICE: nd}


def test_jsonable_turns_an_unknown_object_into_text() -> None:
    assert p4._jsonable({"a": object}) == {"a": str(object)}


def test_carried_params_carry_a_node_the_previous_model_learned() -> None:
    prev = make_model(frozen=["n/0/b_w"])
    out = p4._carried_params(prev, HALL, [BED, OFFICE])
    assert ("n", 0, "b_w") not in out
    assert any(k[0] == "n" and k[1] == 0 for k in out)
    assert not any(k[0] == "n" and k[1] == 1 for k in out)  # OFFICE was never a node of prev


def test_mode_carry_grades_skip_a_mode_graded_none() -> None:
    modes = {"heat": {"grade": "none", "reason": "x"}}
    p4._carry_mode_grades(modes, {"heat": {"grade": "ok", "keep": 1.0}})
    assert modes == {"heat": {"grade": "none", "reason": "x"}}


# ------------------------------------------------------------------------------------------------ state and lines
def test_step_mass_takes_the_primary_from_the_sample_when_the_readings_lack_it() -> None:
    model = make_model()
    smp = replay_history(1)[0]
    got = p4.step_mass(model, mass_f=None, readings={}, sample=smp)
    assert math.isfinite(got)
    assert got == pytest.approx(p4.step_mass(model, mass_f=None, readings=dict(smp.readings)))


def test_ca_line_with_the_stitched_primary_keeps_its_own_night_value() -> None:
    t = datetime(2026, 6, 1, 23, 0, tzinfo=TZ)  # inside the sleep window
    preds = {p4.CA_NODE: [(t, 70.0)], BED: []}
    out = p4.ca_line(preds, primary=p4.CA_NODE, sleep_node=BED, sleep_time="22:00", wake_time="06:00", tz=TZ)
    assert out == [(t, 70.0)]


def test_samples_skip_rows_that_are_not_objects() -> None:
    rows = ["x", {"ts": T0.isoformat(), "indoor": 70.0, "outdoor": 50.0, "hvac": "idle"}]
    assert len(p4.samples_from_chart_log(rows, tz=TZ)) == len(p4.samples_from_chart_log(rows[1:], tz=TZ))


def test_row_primary_value_reads_an_alias() -> None:
    row = {"indoor_sensors": {"climate.old_name": 69.5}}
    assert p4._row_primary_value(row, "climate.hall", ("climate.old_name",), None, TZ) == 69.5


def test_resolve_display_nodes_and_sensor_history_skip_bad_rows() -> None:
    rows = ["x", {"ts": "junk"}, {"ts": T0.isoformat(), "indoor_sensors": None}]
    assert p4.resolve_display_nodes(rows, nodes=[HALL], primary=HALL, sensors=[OFFICE], tz=TZ) == []
    assert p4.resolve_display_nodes(["x"], nodes=[HALL], primary=HALL, sensors=[OFFICE], tz=TZ) == []
    assert p4.sensor_history(["x", {"ts": "junk"}], OFFICE, tz=TZ) is None


def test_score_ca_lines_without_models_or_shared_anchors() -> None:
    assert p4.score_ca_lines({}, [], tz=TZ)["n_anchors"] == 0
    smp = replay_history(3)
    # a composing model whose sleep node never has a reading: no anchor both models can start from
    out = p4.score_ca_lines({"a": make_model()}, smp, tz=TZ, sleep_node=BED, max_anchors=5)
    assert out["n_anchors"] == 0


# ------------------------------------------------------------------------------------------------ display nodes
def _with(samples, sid, fn):
    return [dataclasses.replace(s, readings={**s.readings, sid: fn(k, s)}) for k, s in enumerate(samples)]


def test_fit_display_nodes_rejects_each_sensor_with_its_reason() -> None:
    model = dataclasses.replace(make_model(), fit={"frozen": [], "reason": "", "horizon_h": "twelve"})
    smp = replay_history(6)
    smp = _with(smp, "sensor.late", lambda k, s: s.readings[HALL] if k > len(smp) - 8 else None)
    smp = _with(smp, "sensor.noise", lambda k, s: 65.0)  # never changes: nothing beats "no change"
    out, info = p4.fit_display_nodes(
        model,
        smp,
        display_sensors=[HALL, BED, 42, "sensor.none", "sensor.late", "sensor.noise"],
        tz=TZ,
        warm={"sensor.noise": {"b_h": 0.3, "b_o": float("nan")}},
        max_anchors=40,
        max_iterations=5,
    )
    assert info["tried"] == ["sensor.none", "sensor.late", "sensor.noise"]
    assert info["rejected"]["sensor.none"] == "no readings"
    assert info["rejected"]["sensor.late"] == "too few anchors"
    assert info["rejected"]["sensor.noise"].startswith("does not beat no-change")
    assert out == {}


def test_fit_display_nodes_skips_anchors_without_the_node_reading() -> None:
    model = make_model()
    smp = replay_history(6)
    # the office reads like the hall, offset, with every 7th reading missing
    smp = _with(smp, OFFICE, lambda k, s: None if k % 7 == 3 else s.readings[HALL] - 1.0)
    out, info = p4.fit_display_nodes(model, smp, display_sensors=[OFFICE], tz=TZ, max_anchors=60, max_iterations=8)
    assert info["tried"] == [OFFICE]
    assert OFFICE in out and info["kept"] == [OFFICE]  # a sensor that tracks the hall beats "no change"


def test_fit_display_nodes_of_an_unusable_model_tries_nothing() -> None:
    out, info = p4.fit_display_nodes(make_model(confidence="none"), replay_history(1), display_sensors=[OFFICE], tz=TZ)
    assert out == {} and info["tried"] == []
