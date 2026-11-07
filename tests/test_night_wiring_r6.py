"""Night strategy wiring, endpoint, nightly replay and the shared window rule (Issue #1230 R6).

Occupant view: each evening the plan says what to do about the bedroom tonight (or that CA holds it and nothing is
needed), the bedroom card reads the same plan, predicted figures appear whenever v4 runs and the bedroom model is at
least medium confidence (no accuracy gate), and the night's numbers agree with the v4 prediction of the same hours.
The window assumption v4 predicts
under is one rule shared by the live shadow and the backtest.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import night_replay as nr
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.api import API_VIEWS, ClimateAdvisorNightStrategyView
from custom_components.climate_advisor.const import API_NIGHT_STRATEGY, DOMAIN
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.operation_timeline import (
    SENTENCES,
    Actor,
    StepKind,
    TimelineLatch,
)
from custom_components.climate_advisor.temperature import display_temp
from tests.test_night_strategy import BED, GOOD_EVIDENCE, GOOD_VALIDATION, HALL, make_model, outdoor
from tests.test_night_strategy_replay import _history as replay_history
from tests.timeline_fixtures import TZ, make_snapshot

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=TZ)  # evening, bedtime 20:30 (fixture config), wake 06:30
START = datetime(2026, 11, 6, 20, 30, tzinfo=TZ)
END = datetime(2026, 11, 7, 6, 30, tzinfo=TZ)


# ------------------------------------------------------------------------------------ the shared window rule (task 3)
def test_window_span_uses_the_engines_planned_times_on_the_anchor_day() -> None:
    anchor = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    span = v4_shadow.planned_window_span(anchor, True, time(10, 0), time(17, 0))
    assert span == (datetime(2026, 11, 6, 10, 0, tzinfo=TZ), datetime(2026, 11, 6, 17, 0, tzinfo=TZ))
    assert not v4_shadow.window_open_at(span, datetime(2026, 11, 6, 9, 45, tzinfo=TZ))
    assert v4_shadow.window_open_at(span, datetime(2026, 11, 6, 10, 0, tzinfo=TZ))
    assert v4_shadow.window_open_at(span, datetime(2026, 11, 6, 16, 45, tzinfo=TZ))
    # the recommendation is today's: the same clock time TOMORROW is not repeated (the old live rule repeated it)
    assert not v4_shadow.window_open_at(span, datetime(2026, 11, 7, 11, 0, tzinfo=TZ))


def test_window_span_is_none_when_not_recommended_or_already_over() -> None:
    anchor = datetime(2026, 11, 6, 18, 0, tzinfo=TZ)
    assert v4_shadow.planned_window_span(anchor, False, time(10, 0), time(17, 0)) is None
    assert v4_shadow.planned_window_span(anchor, True, time(10, 0), time(17, 0)) is None  # closed at 17:00 already


def test_window_span_falls_back_to_the_documented_four_hours_without_planned_times() -> None:
    anchor = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    assert v4_shadow.WINDOW_PLAN_FALLBACK_HOURS == 4.0
    assert v4_shadow.planned_window_span(anchor, True, None, None) == (anchor, anchor + timedelta(hours=4))
    assert v4_shadow.planned_window_span(anchor, True, time(10, 0), None) == (anchor, anchor + timedelta(hours=4))
    assert v4_shadow.planned_window_span(anchor, True, None, None, fallback_hours=0) is None


def test_window_span_close_before_open_runs_past_midnight() -> None:
    anchor = datetime(2026, 11, 6, 20, 0, tzinfo=TZ)
    span = v4_shadow.planned_window_span(anchor, True, time(22, 0), time(1, 0))
    assert span == (datetime(2026, 11, 6, 22, 0, tzinfo=TZ), datetime(2026, 11, 7, 1, 0, tzinfo=TZ))


def test_live_inputs_and_the_backtest_share_the_one_rule() -> None:
    now = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    hourly = [(now + timedelta(hours=h), 60.0) for h in range(30)]
    band = [(now, 66.0, 74.0)]
    inputs = v4_shadow.build_inputs(
        now=now, hourly=hourly, band=band, windows=(True, time(10, 0), time(12, 0)), horizon_hours=26
    )
    open_ts = [i.ts for i in inputs if i.windows_open]
    assert (
        open_ts
        and open_ts[0] == datetime(2026, 11, 6, 10, 0, tzinfo=TZ)
        and open_ts[-1] == datetime(2026, 11, 6, 11, 45, tzinfo=TZ)
    )
    assert all(i.ts.date() == now.date() for i in inputs if i.windows_open)  # none tomorrow
    # no planned times: the same fallback the backtest uses
    inputs = v4_shadow.build_inputs(now=now, hourly=hourly, band=band, windows=(True, None, None))
    assert sum(1 for i in inputs if i.windows_open) == 15  # 4 h on the 15-minute grid, after `now`


def _backtest_module():
    tools = str(Path(__file__).resolve().parent.parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module("predict_backtest")


def test_backtest_takes_the_day_record_times_and_falls_back_to_four_hours() -> None:
    pb = _backtest_module()
    db = {
        "records": [
            {"date": "2026-11-05", "window_open_time": "10:00:00", "window_close_time": "17:00:00"},
            {"date": "2026-11-04", "window_open_time": None, "window_close_time": None},
            {"date": "garbage"},
        ]
    }
    recs = pb.window_records(db)
    assert recs == {"2026-11-05": (time(10, 0), time(17, 0))}
    idx = SimpleNamespace(
        tz=TZ, row_at=lambda ts, tol=None: {"windows_recommended": True}
    )  # the live flag at the anchor says "recommended"
    args = SimpleNamespace(window_plan_hours=4.0, window_plan_source="record")
    a = datetime(2026, 11, 5, 8, 0, tzinfo=TZ)
    assert pb.window_plan_span(idx, a, args, recs) == v4_shadow.planned_window_span(a, True, time(10, 0), time(17, 0))
    b = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)  # no record for the day -> the documented fallback
    assert pb.window_plan_span(idx, b, args, recs) == (b, b + timedelta(hours=4))
    args.window_plan_source = "fallback"  # the pre-R6 rule on request
    assert pb.window_plan_span(idx, a, args, recs) == (a, a + timedelta(hours=4))
    idx.row_at = lambda ts, tol=None: {"windows_recommended": False}
    assert pb.window_plan_span(idx, a, args, recs) is None


# ------------------------------------------------------------------------------------ night window + inputs
def test_night_window_cases() -> None:
    sleep, wake = time(22, 30), time(6, 30)
    day = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
    assert v4_shadow.night_window(day, sleep, wake) is None  # daytime: no night plan (and not last night's)
    ev = datetime(2026, 11, 6, 17, 0, tzinfo=TZ)
    assert v4_shadow.night_window(ev, sleep, wake) == (
        datetime(2026, 11, 6, 22, 30, tzinfo=TZ),
        datetime(2026, 11, 7, 6, 30, tzinfo=TZ),
    )
    late = datetime(2026, 11, 6, 23, 15, tzinfo=TZ)  # bedtime passed: the night in progress starts now
    assert v4_shadow.night_window(late, sleep, wake) == (late, datetime(2026, 11, 7, 6, 30, tzinfo=TZ))
    small = datetime(2026, 11, 7, 2, 0, tzinfo=TZ)  # after midnight, before wake
    assert v4_shadow.night_window(small, sleep, wake) == (small, datetime(2026, 11, 7, 6, 30, tzinfo=TZ))
    nearly = datetime(2026, 11, 7, 5, 0, tzinfo=TZ)  # under two hours left: not worth a plan
    assert v4_shadow.night_window(nearly, sleep, wake) is None
    after = datetime(2026, 11, 7, 6, 31, tzinfo=TZ)
    assert v4_shadow.night_window(after, sleep, wake) is None
    # a bedtime after midnight
    assert v4_shadow.night_window(datetime(2026, 11, 6, 17, 0, tzinfo=TZ), time(0, 30), time(6, 30)) is None
    assert v4_shadow.night_window(datetime(2026, 11, 6, 23, 0, tzinfo=TZ), time(0, 30), time(6, 30)) == (
        datetime(2026, 11, 7, 0, 30, tzinfo=TZ),
        datetime(2026, 11, 7, 6, 30, tzinfo=TZ),
    )


def _inputs(**kw):
    model = make_model()
    pred = {
        HALL: [(START + timedelta(minutes=15 * k), 68.0 - 0.01 * k) for k in range(-8, 50)],
        BED: [(START + timedelta(minutes=15 * k), 67.0) for k in range(-8, 50)],
    }
    base = {
        "model_dict": p4.to_dict(model),
        "start": START,
        "end": END,
        "now": NOW,
        "fan_mode": "whole_house_fan",
        "corrected_outdoor": outdoor(56, 49, NOW, END),
        "predictions": pred,
        "current_readings": {HALL: 70.0, BED: 69.0, "_ca": 70.0},
        "floor_f": 66.0,
        "ceiling_f": 72.0,
        "target_sensor": BED,
    }
    base.update(kw)
    return v4_shadow.night_inputs(**base)


def test_night_inputs_start_from_the_predicted_state_at_bedtime() -> None:
    inp = _inputs()
    assert inp.readings[HALL] == pytest.approx(68.0)  # the plan's predicted hallway at 20:30, not the 70.0 of now
    assert inp.readings[BED] == pytest.approx(67.0)
    assert inp.mass_f is None  # no live mass state given: the node is seeded at the bedtime reading
    live = _inputs(start=NOW, now=NOW, mass_f=67.5)  # night already under way: the live reading and mass
    assert live.readings[HALL] == 70.0 and live.mass_f == 67.5


def test_the_hidden_mass_is_relaxed_along_the_predicted_path_to_bedtime() -> None:
    """The live mass filter never restarts: the plan starts from the live state advanced to bedtime along v4's predicted
    primary path (the state the nightly replay validates), not from a cold seed."""
    model = p4.from_dict(p4.to_dict(make_model()))
    inp = _inputs(mass_f=72.0)
    m = 72.0
    for k in range(1, 7):  # 19:15 .. 20:30: six 15-minute steps along the predicted hallway path (68.0 - 0.01 k')
        ts = NOW + timedelta(minutes=15 * k)
        k_from_start = int((ts - START).total_seconds() // 900)
        m = p4.step_mass(model, mass_f=m, readings={HALL: 68.0 - 0.01 * k_from_start})
    assert inp.mass_f == pytest.approx(m)
    assert 68.0 < inp.mass_f < 72.0  # moved toward the predicted air, not reset to it and not left at the live value
    assert _inputs(mass_f=72.0, predictions=None).mass_f is None  # no predicted path: seed at the bedtime reading
    assert _inputs(mass_f=None).mass_f is None
    assert _inputs(start=NOW, mass_f=72.0).mass_f == 72.0  # night already under way: the live state as is


def test_night_inputs_fall_back_to_the_timeline_path_then_the_live_reading() -> None:
    path = [(START + timedelta(minutes=15 * k), 69.0) for k in range(-8, 8)]
    inp = _inputs(predictions={BED: [(START, 67.5)]}, path=path)
    assert inp.readings[HALL] == 69.0 and inp.readings[BED] == 67.5
    inp = _inputs(predictions=None, path=None)
    assert inp.readings[HALL] == 70.0 and inp.readings[BED] == 69.0


def test_night_inputs_none_without_a_forecast_or_a_usable_model() -> None:
    assert _inputs(corrected_outdoor=[]) is None
    assert _inputs(model_dict={**p4.to_dict(make_model()), "confidence": "none"}) is None
    assert _inputs(current_readings={}, predictions=None) is None


# ------------------------------------------------------------------------------------ the R1b emulator in the planner
def test_night_figures_and_the_v4_predictor_run_the_heat_on_the_same_minutes() -> None:
    """The planner's closed option and ``predictor_v4.predict`` over the same hours under the same setpoints use the
    one emulator (hysteretic stop, minimum run), so a night figure cannot disagree with the v4 line for the same
    hours."""
    th = p4.V4Thermostat(
        on_below_f=0.5, on_above_f=0.5, burst_minutes=10.0, reading_step_f=0.5, off_above_f=1.0, min_run_heat_min=30.0
    )
    model = make_model()
    model = p4.V4Model(**{**model.__dict__, "thermostat": th, "sensors": (HALL,), "nodes": {}})
    inp = ns.NightInputs(
        model=model, start=START, end=END, tz=TZ, fan_mode="disabled", outdoor=outdoor(45, 40), readings={HALL: 66.5},
        floor_f=66.0, ceiling_f=72.0, evidence=GOOD_EVIDENCE, validation=GOOD_VALIDATION, offset_loop=False,
        can_cool=False,
    )  # fmt: skip
    plan = ns.plan_night(inp)
    closed = plan.option(ns.OPT_CLOSED)
    assert closed is not None and closed.heat_min > 0
    steps = p4.grid_steps(START, int((END - START).total_seconds() // 900))
    out = [ns._interp(sorted((t.timestamp(), v) for t, v in inp.outdoor), t.timestamp()) for t in steps]
    roll = p4.rollout(
        [model.house[k] for k in p4.HOUSE_KEYS], [], h0=66.5, m0=66.5, b0=[], out=out, win=[0.0] * len(steps),
        fan=[0.0] * len(steps), hours=[p4._local_hour(t, TZ) for t in steps], thermostat=th,
        heat_sp=[66.0] * len(steps), cool_sp=[None] * len(steps),
    )  # fmt: skip
    assert closed.heat_min == round(sum(roll.heat_duty) * 15)
    assert [round(v, 6) for _t, v in closed.hall_path] == [round(v, 6) for v in roll.primary]


def test_a_paused_hvac_stops_a_run_in_progress() -> None:
    model = make_model(confidence="ok")
    inp = ns.NightInputs(
        model=model, start=START, end=END, tz=TZ, fan_mode="whole_house_fan", outdoor=outdoor(50, 45),
        readings={HALL: 64.0, BED: 64.0}, floor_f=66.0, ceiling_f=72.0, evidence=GOOD_EVIDENCE,
        validation=GOOD_VALIDATION, window_hours=2.0,
    )  # fmt: skip
    plan = ns.plan_night(inp)
    win = plan.option(ns.OPT_WINDOWS_2H)
    closed = plan.option(ns.OPT_CLOSED)
    assert win is not None and closed is not None
    # windows open for the first two hours: no heat in them (owner rule 2), heat resumes after
    assert win.used["hvac_paused_steps"] == 8
    assert win.heat_min < closed.heat_min or win.bed_min_f < closed.bed_min_f


# ------------------------------------------------------------------------------------ gate (C11) + payload
def _plan(**kw):
    return ns.plan_night(
        ns.NightInputs(
            model=make_model(), start=START, end=END, tz=TZ, fan_mode="whole_house_fan", outdoor=outdoor(56, 49),
            readings={HALL: 68.5, BED: 67.0}, floor_f=66.0, ceiling_f=72.0, target_sensor=BED,
            evidence=GOOD_EVIDENCE, validation=GOOD_VALIDATION, **kw,
        )
    )  # fmt: skip


def test_figures_follow_the_bedroom_models_own_confidence_there_is_no_accuracy_gate() -> None:
    plan = _plan()
    assert plan.show_numbers is True
    assert not hasattr(ns, "gate_numbers")  # DP-8 is information, never a gate (Issue #1230)
    low = ns.NightStrategy(**{**plan.__dict__, "show_numbers": False})
    payload = ns.api_payload(low, unit="fahrenheit", convert=display_temp)
    assert payload["show_numbers"] is False  # below medium confidence: the plan in words only


def test_payload_nulls_every_figure_when_hidden_and_converts_units_when_shown() -> None:
    plan = _plan()
    low = ns.NightStrategy(**{**plan.__dict__, "show_numbers": False})
    hidden = ns.api_payload(low, unit="fahrenheit", convert=display_temp)
    assert hidden["show_numbers"] is False and hidden["tradeoffs"] == []
    for o in hidden["options"]:
        assert all(o[k] is None for k in ns.FIGURE_KEYS) and o["path"] == []
        assert o["key"] and o["label"] and "actions" in o  # the plan itself is still there
    assert hidden["headline"]
    shown_c = ns.api_payload(plan, unit="celsius", convert=display_temp)
    assert shown_c["unit"] == "celsius" and shown_c["show_numbers"] is True
    assert shown_c["source_model"] == "v4" and "v4" in shown_c["source_note"]  # labelled "from v4"
    assert shown_c["floor_f"] == pytest.approx(display_temp(66.0, "celsius"))
    best = shown_c["options"][0]
    assert best["bedroom_low_f"] == pytest.approx(display_temp(plan.options[0].bed_min_f, "celsius"))
    assert best["path"] and best["path"][0][1] == pytest.approx(display_temp(plan.options[0].path[0][1], "celsius"))
    assert shown_c["options"][0]["discomfort_fh"] == pytest.approx(plan.options[0].discomfort_fh * 5 / 9, abs=0.01)
    json.dumps(shown_c)  # JSON-safe
    json.dumps(hidden)
    assert "hidden" not in json.dumps(shown_c.get("debug", {}))  # no debug block without include_debug


# ------------------------------------------------------------------------------------ sentence table
def test_night_sentences_are_registered_in_the_timeline_sentence_table() -> None:
    for key, sentence in ns.NIGHT_SENTENCES.items():
        reg = SENTENCES[key]
        assert reg.template == sentence.template and dict(reg.facts) == dict(sentence.facts)
        assert reg.rule and reg.guard


# ------------------------------------------------------------------------------------ coordinator wiring
class _Chart:
    def last_entry(self):
        return {"indoor_sensors": {HALL: 70.0, BED: 69.0}, "indoor": 70.0}


def _coordinator(*, gate: bool = True, validation=True, snap_now: datetime = NOW, fan_mode: str = "whole_house_fan"):
    c = object.__new__(ClimateAdvisorCoordinator)
    snap = make_snapshot("mild", now=snap_now, fan_mode=fan_mode, thermal_model=None)
    latch = TimelineLatch()
    latch.update(snap)
    c._timeline_latch = latch
    c._timeline_last = {"at": snap_now.isoformat()}
    c._timeline_night = None
    c._timeline_night_failed_logged = False
    model = make_model()
    c._v4_store = SimpleNamespace(
        v4_model=p4.to_dict(model),
        night_validation=nr.validation_record(
            {
                "classes": {k: v.to_dict() for k, v in GOOD_VALIDATION.classes.items()},
                "generated": "2026-11-06T03:20:00+00:00",
                "params": {"train_days": 30},
                "classes_ok_fits_only": {k: {"n_nights": 20} for k in GOOD_VALIDATION.classes},
            },
            GOOD_EVIDENCE,
        )
        if validation
        else None,
        v4_mass_f=67.0,
    )
    steps = p4.grid_steps(snap_now, 96)
    c._v4_last_future = {
        "predictions": {
            HALL: [(t, 67.0) for t in steps],
            BED: [(t, 66.8) for t in steps],
        },
        "corrected": [(t, 50.0 - 0.02 * i) for i, t in enumerate(steps)],
    }
    c._chart_log = _Chart()
    caps = SimpleNamespace(supports_heat=True, supports_cool=True)
    c.automation_engine = SimpleNamespace(_get_thermostat_capabilities=lambda: caps)
    c.config = {"sleep_indoor_temp_entity": BED, "fan_mode": fan_mode, "temp_unit": "fahrenheit"}
    if not gate:  # "gate" now means "v4 is running": off when it is neither the primary nor the comparison engine
        c.config.update({"primary_engine": "v3", "shadow_engine": "none"})
    return c, snap


def test_the_evening_adds_the_night_strategy_and_its_steps_to_the_timeline() -> None:
    c, snap = _coordinator(gate=True)
    before = c._timeline_latch.timeline
    c._timeline_night_apply(snap, True)
    after = c._timeline_latch.timeline
    night = [s for s in after.steps if s.kind == StepKind.NIGHT_STRATEGY]
    assert len(night) == 1 and night[0].actor == Actor.CA
    assert night[0].meta["show_numbers"] is True
    assert night[0].text and "nothing for you to do" in night[0].text or "bedroom plan" in night[0].text
    assert c._timeline_night["strategy"] is not None and c._timeline_night["reason"] == "ok"
    assert len(after.steps) > len(before.steps)
    # re-applying (the next cycle) is idempotent and never duplicates a step
    c._timeline_night_apply(snap, False)
    again = c._timeline_latch.timeline
    assert [(s.ts, s.kind, s.reason) for s in again.steps] == [(s.ts, s.kind, s.reason) for s in after.steps]


def test_with_v4_off_there_is_no_night_plan_and_the_reason_says_so() -> None:
    c, snap = _coordinator(gate=False)
    c._timeline_night_apply(snap, True)
    assert c._timeline_night["strategy"] is None and c._timeline_night["reason"] == "v4_off"
    assert not [s for s in c._timeline_latch.timeline.steps if s.kind == StepKind.NIGHT_STRATEGY]
    c._surface_timeline = lambda: None
    assert c.night_strategy_payload() == (None, "v4_off")


def test_v4_as_the_comparison_engine_still_plans_the_night_without_any_gate() -> None:
    c, snap = _coordinator(gate=False)
    c.config.update({"primary_engine": "v3", "shadow_engine": "v4"})
    c._timeline_night_apply(snap, True)
    step = next(s for s in c._timeline_latch.timeline.steps if s.kind == StepKind.NIGHT_STRATEGY)
    assert step.meta["show_numbers"] is True


def test_the_night_steps_survive_the_latchs_next_update_without_duplicates() -> None:
    c, snap = _coordinator(gate=True)
    c._timeline_night_apply(snap, True)
    first = [s for s in c._timeline_latch.timeline.steps if s.kind == StepKind.NIGHT_STRATEGY]
    out = c._timeline_latch.update(make_snapshot("mild", now=NOW + timedelta(minutes=30)))
    c._timeline_night_apply(make_snapshot("mild", now=NOW + timedelta(minutes=30)), out.regenerated)
    night = [s for s in c._timeline_latch.timeline.steps if s.kind == StepKind.NIGHT_STRATEGY]
    assert len(first) == 1 and len(night) == 1
    keys = [(s.ts, s.kind, s.reason) for s in c._timeline_latch.timeline.steps]
    assert len(keys) == len(set(keys))


def test_no_night_plan_in_the_daytime_or_without_a_model() -> None:
    c, snap = _coordinator(gate=True, snap_now=datetime(2026, 11, 6, 12, 0, tzinfo=TZ))
    c._timeline_night_apply(snap, True)
    assert c._timeline_night["strategy"] is None and c._timeline_night["reason"] == "no_night_in_view"
    assert not [s for s in c._timeline_latch.timeline.steps if s.kind == StepKind.NIGHT_STRATEGY]
    c, snap = _coordinator(gate=True)
    c._v4_store.v4_model = None
    c._timeline_night_apply(snap, True)
    assert c._timeline_night["strategy"] is None and c._timeline_night["reason"] == "no_model"


def test_a_missing_validation_offers_no_fan_option_and_no_figures_confidence() -> None:
    c, snap = _coordinator(gate=True, validation=False)
    c._timeline_night_apply(snap, True)
    strat = c._timeline_night["strategy"]
    assert strat is not None
    assert {o.key for o in strat.options} == {ns.OPT_CLOSED}  # nothing validated yet: only the baseline
    assert strat.show_numbers is False  # no evidence hours either: the baseline is low confidence


def test_the_planning_failure_never_costs_the_timeline() -> None:
    c, snap = _coordinator(gate=True)
    c._v4_last_future = {"corrected": "not a list", "predictions": 3}
    n_before = len(c._timeline_latch.timeline.steps)
    c._timeline_night_apply(snap, True)  # swallowed, logged
    assert len(c._timeline_latch.timeline.steps) == n_before


def test_the_payload_reads_the_cache_and_answers_not_ready_before_a_night_is_in_view() -> None:
    c, snap = _coordinator(gate=True)
    c._surface_timeline = lambda: None
    body, reason = c.night_strategy_payload()
    assert body is None and reason == "not_ready"
    c._timeline_night_apply(snap, True)
    body, reason = c.night_strategy_payload()
    assert reason == "ok" and body["unit"] == "fahrenheit" and body["recommended"] and body["source_model"] == "v4"
    assert body["show_numbers"] is True  # no accuracy gate: the bedroom model's own confidence decides


# ------------------------------------------------------------------------------------ endpoint
def _request(coordinators: dict, query: dict | None = None):
    hass = MagicMock()
    hass.data = {DOMAIN: coordinators}
    hass.config_entries.async_entries = MagicMock(return_value=[])
    req = MagicMock()
    req.app = {"hass": hass}
    req.method, req.path, req.remote = "GET", API_NIGHT_STRATEGY, "127.0.0.1"
    req.rel_url.query = query or {}
    return req


def _coord(result):
    c = MagicMock()
    c.zone_label = "Zone"
    c.night_strategy_payload.return_value = result
    return c


def test_the_endpoint_is_registered_authenticated_and_read_only() -> None:
    assert ClimateAdvisorNightStrategyView in API_VIEWS
    assert ClimateAdvisorNightStrategyView.requires_auth is True
    assert ClimateAdvisorNightStrategyView.url == "/api/climate_advisor/night_strategy"
    assert not hasattr(ClimateAdvisorNightStrategyView, "post")


def test_the_endpoint_answers_200_503_and_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.climate_advisor.zone_registry.ir.async_create_issue", MagicMock())
    monkeypatch.setattr("custom_components.climate_advisor.api.zone_registry.list_zones", lambda _h: [])
    payload = {"unit": "fahrenheit", "recommended": "closed"}
    req = _request({"zoneA": _coord((payload, "ok"))}, {"zone": "zoneA"})
    resp = asyncio.run(ClimateAdvisorNightStrategyView().get(req))
    assert resp.status == 200 and resp.json_data == payload
    resp = asyncio.run(
        ClimateAdvisorNightStrategyView().get(
            _request({"zoneA": _coord((None, "no_night_in_view"))}, {"zone": "zoneA"})
        )
    )
    assert resp.status == 503 and resp.json_data == {"error": "night_strategy_not_ready", "reason": "no_night_in_view"}
    resp = asyncio.run(ClimateAdvisorNightStrategyView().get(_request({})))
    assert resp.status == 503
    a, b = _coord((payload, "ok")), _coord((payload, "ok"))
    resp = asyncio.run(ClimateAdvisorNightStrategyView().get(_request({"zoneA": a, "zoneB": b})))
    assert resp.status == 400 and not a.night_strategy_payload.called  # several zones, no zone: never the wrong house
    resp = asyncio.run(ClimateAdvisorNightStrategyView().get(_request({"zoneA": a, "zoneB": b}, {"zone": "zoneB"})))
    assert resp.status == 200 and b.night_strategy_payload.called and not a.night_strategy_payload.called


# ------------------------------------------------------------------------------------ nightly replay (task 2)
def test_the_nightly_job_returns_a_compact_record_the_gate_reads() -> None:
    samples = replay_history(30)
    rep = nr.run(samples, TZ, bed=(22, 30), wake=(6, 30), train_days=30, max_nights=14)
    assert rep["n_nights_replayed"] > 0 and "nights" in rep
    rec = nr.validation_record(rep, ns.evidence_from_samples(samples))
    assert "nights" not in rec  # compact: no per-night rows in the model file
    json.dumps(rec)
    val = ns.NightValidation.from_dict(rec)
    assert val is not None and ns.CLS_CLOSED in val.classes
    assert val.provisional is True  # a synthetic month has no 'ok'-fit nights per class: never final
    ev = nr.evidence_from_record(rec)
    assert ev is not None and ev.sensor_hours["_ca"] > 500
    assert nr.evidence_from_record({}) is None and nr.evidence_from_record({"evidence": "x"}) is None


def test_a_replay_is_final_only_on_the_live_rule_with_enough_trusted_fits() -> None:
    base = {
        "params": {"train_days": 30},
        "classes": {"closed": {}, "fan": {}},
        "classes_ok_fits_only": {"closed": {"n_nights": 12}, "fan": {"n_nights": 10}},
    }
    assert nr.is_provisional(base) is False
    assert nr.is_provisional({**base, "params": {"train_days": 0}}) is True  # all-history fits are not the live rule
    assert nr.is_provisional({**base, "classes_ok_fits_only": {"closed": {"n_nights": 12}}}) is True  # fan unproven
    assert nr.is_provisional({"params": {"train_days": 30}, "classes": {}}) is True


def test_the_nightly_job_runs_the_fit_samples_the_live_fit_uses() -> None:
    """One sample builder for the fit, the backtest and the replay (no second reading of the chart log)."""
    import inspect

    assert "build_samples" in inspect.getsource(v4_shadow.run_fit)
    assert "build_samples" in inspect.getsource(nr.run_nightly)


# ------------------------------------------------------------------------------------ model file + advisory guards
def test_the_model_file_round_trips_the_night_validation_and_old_files_read_as_none(tmp_path: Path) -> None:
    from custom_components.climate_advisor.v4_archive import V4ModelStore

    store = V4ModelStore(tmp_path, entry_id="e1")
    store.v4_model = {"primary": "_ca"}
    store.night_validation = {"schema_version": 1, "classes": {}, "provisional": True}
    assert store.save()
    again = V4ModelStore(tmp_path, entry_id="e1")
    again.load()
    assert again.night_validation == {"schema_version": 1, "classes": {}, "provisional": True}
    path = next(tmp_path.glob("climate_advisor_v4_model*.json"))
    raw = json.loads(path.read_text())
    raw.pop("night_validation")  # a file written before R6
    path.write_text(json.dumps(raw))
    older = V4ModelStore(tmp_path, entry_id="e1")
    older.load()
    assert older.night_validation is None and older.v4_model == {"primary": "_ca"}


def test_night_wiring_is_advisory_only() -> None:
    """The coordinator's night methods read v4 state and write only the night cache; they command nothing."""
    import ast

    src = (
        Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "coordinator.py"
    ).read_text()
    cls = next(
        n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef) and n.name == "ClimateAdvisorCoordinator"
    )
    names = {"_timeline_night_apply", "_timeline_night_plan", "night_strategy_payload"}
    forbidden = {"async_call", "services", "set_hvac_mode", "set_temperature", "_emit_event", "_emit_incident"}
    found = set()
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name in names:
            found.add(fn.name)
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    assert isinstance(node.value, ast.Name) and node.value.id == "self"
                    assert node.attr.startswith("_timeline_night"), f"{fn.name} writes self.{node.attr}"
                if isinstance(node, ast.Attribute):
                    assert node.attr not in forbidden, f"{fn.name} reaches {node.attr}"
    assert found == names
    # the one engine member it may read is the thermostat capability getter (read-only)
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name == "_timeline_night_plan":
            engine_attrs = {
                n.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Attribute)
                and n.value.attr == "automation_engine"
            }
            assert engine_attrs == {"_get_thermostat_capabilities"}


def test_the_night_methods_are_in_the_timeline_shadow_guard_list() -> None:
    """Pin: the timeline structural test also guards the new methods (they write only ``_timeline*`` state)."""
    text = (Path(__file__).resolve().parent / "test_operation_timeline_structural.py").read_text()
    assert "_timeline_night_apply" in text and "_timeline_night_plan" in text


def test_the_replay_settles_the_hidden_mass_before_each_night() -> None:
    """The live filter never restarts, so the replayed night starts from a settled mass, not a 6 h cold start (which
    read autumn nights 1-2 F warm that the running house never had)."""
    assert nr.MASS_WARMUP_STEPS >= 96
    model = make_model()
    samples = replay_history(6)
    idx = {int(s.ts.timestamp()): i for i, s in enumerate(samples)}
    start = samples[300].ts
    got = nr.mass_at(model, samples, idx, start)
    ref = None
    for s in samples[300 - nr.MASS_WARMUP_STEPS : 301]:
        ref = p4.step_mass(model, mass_f=ref, readings=s.readings)
    cold = None
    for s in samples[300 - 24 : 301]:
        cold = p4.step_mass(model, mass_f=cold, readings=s.readings)
    assert got == pytest.approx(ref) and got != pytest.approx(cold, abs=1e-6)
