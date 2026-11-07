"""Tests for the bedroom night-strategy engine (Issue #1230 R4, ADVISE only).

The planner runs the REAL v4 rollout (``predictor_v4.rollout``) on a synthetic house with known parameters, so the
assertions are physical relations (an open window on a cold night costs bedroom degrees and saves heat minutes), not
hand-copied numbers. ``apply_night_strategy`` is exercised against a contract-shaped stand-in for the R3 timeline
module (``operation_timeline`` lands in lane R3a); one test runs it against the real module when it exists.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import predictor_v4 as p4

TZ = ZoneInfo("America/Los_Angeles")
HALL = "_ca"
BED = "sensor.bedroom"
START = datetime(2026, 11, 6, 22, 0, tzinfo=TZ)
END = datetime(2026, 11, 7, 6, 30, tzinfo=TZ)
REPO = Path(__file__).resolve().parent.parent
PENDING = REPO / "tools" / "simulations" / "pending"

HOUSE = {
    "a_o": 0.04,
    "a_w": 0.15,
    "a_f": 0.6,
    "c_hm": 0.4,
    "c_mh": 0.2,
    "s": 0.5,
    "phase": 3.0,
    "q_h": 9.0,
    "q_c": 10.0,
}
NODE = {"b_h": 0.2, "b_o": 0.06, "b_w": 0.1, "s_b": 0.0, "q_b": 6.0}


def make_model(*, confidence: str = "ok", frozen: list[str] | None = None, **house: float) -> p4.V4Model:
    return p4.V4Model(
        schema_version=p4.V4_SCHEMA_VERSION,
        fitted_at="2026-11-06T03:15:00-08:00",
        primary=HALL,
        sensors=(HALL, BED),
        house={**HOUSE, **house},
        nodes={BED: dict(NODE)},
        thermostat=p4.V4Thermostat(on_below_f=1.0, on_above_f=1.0, burst_minutes=10.0, reading_step_f=0.0),
        forecast=p4.V4ForecastCorrection(hourly_bias_f=tuple([0.0] * 24), nowcast_tau_h=3.0),
        fit={"frozen": frozen or [], "reason": "" if confidence == "ok" else "thin history"},
        confidence=confidence,
    )


def outdoor(start_f: float, end_f: float, start: datetime = START, end: datetime = END) -> list[tuple[datetime, float]]:
    n = int((end - start).total_seconds() // 900) + 2
    return [(start + timedelta(minutes=15 * k), start_f + (end_f - start_f) * k / (n - 1)) for k in range(n)]


WHF_KEYS = {
    ns.OPT_CLOSED,
    ns.OPT_WINDOWS_2H,
    ns.OPT_WINDOWS_ALL,
    ns.OPT_FAN_2H,
    ns.OPT_FAN_UNTIL_TARGET,
    ns.OPT_FAN_THEN_AC,
}
GOOD_EVIDENCE = ns.Evidence(
    window_open_hours=300, fan_hours=200, heat_hours=120, cool_hours=80, sensor_hours={HALL: 5000, BED: 900}
)
GOOD_VALIDATION = ns.NightValidation(
    classes={
        ns.CLS_CLOSED: ns.ClassValidation(20, 0.9, 2.0, 0.1),
        ns.CLS_WINDOWS: ns.ClassValidation(15, 1.1, 2.0, 0.1),
        ns.CLS_FAN: ns.ClassValidation(40, 1.2, 2.5, 0.2),
        ns.CLS_BLOWER: ns.ClassValidation(5, 1.2, 2.0, 0.0),
    },
    provisional=False,
)


def cold_night(**kw: Any) -> ns.NightInputs:
    base: dict[str, Any] = {
        "model": make_model(),
        "start": START,
        "end": END,
        "tz": TZ,
        "fan_mode": "whole_house_fan",
        "outdoor": outdoor(56, 49),
        "readings": {HALL: 68.5, BED: 67.0},
        "floor_f": 66.0,
        "ceiling_f": 72.0,
        "target_sensor": BED,
        "evidence": GOOD_EVIDENCE,
        "validation": GOOD_VALIDATION,
    }
    base.update(kw)
    return ns.NightInputs(**base)


def warm_night(**kw: Any) -> ns.NightInputs:
    base: dict[str, Any] = {
        "outdoor": outdoor(66, 58),
        "readings": {HALL: 79.0, BED: 78.0},
    }
    base.update(kw)
    return cold_night(**base)


# ------------------------------------------------------------------------------------------- scoring + ranking
def test_score_is_dp2_one_degree_hour_equals_one_hvac_hour() -> None:
    assert ns.HVAC_HOURS_PER_DISCOMFORT_FH == 1.0
    assert ns._score(2.0, 60) == pytest.approx(3.0)
    assert ns._score(0.0, 30) == pytest.approx(0.5)  # the plan's "keep closed" example: 30 min heat, none outside


def _opt(key: str, score: float, actions: int) -> ns.StrategyOption:
    return ns.StrategyOption(
        key=key, label=key, cls=ns.CLS_CLOSED, actions=(), n_actions=actions, bed_min_f=66, bed_max_f=68,
        bed_wake_f=67, discomfort_fh=0, heat_min=0, cool_min=0, score=score, grade="medium", reasons=(), runs=True,
    )  # fmt: skip


def test_tie_goes_to_fewer_occupant_actions() -> None:
    ranked = ns.rank([_opt("windows_2h", 0.50, 2), _opt("closed", 0.50, 0), _opt("fan_2h", 2.07, 2)])
    assert [o.key for o in ranked] == ["closed", "windows_2h", "fan_2h"]
    # within TIE_EPS counts as a tie too
    ranked = ns.rank([_opt("windows_2h", 0.50, 2), _opt("closed", 0.53, 0)])
    assert ranked[0].key == "closed"
    # a real difference is not a tie
    ranked = ns.rank([_opt("windows_2h", 0.50, 2), _opt("closed", 0.80, 0)])
    assert ranked[0].key == "windows_2h"


# ------------------------------------------------------------------------------------------- physics of the options
def test_cold_night_closed_wins_and_open_windows_cost_bedroom_degrees() -> None:
    st = ns.plan_night(cold_night())
    assert st.recommended == ns.OPT_CLOSED
    closed = st.option(ns.OPT_CLOSED)
    allnight = st.option(ns.OPT_WINDOWS_ALL)
    assert closed is not None and allnight is not None
    assert allnight.bed_min_f < closed.bed_min_f - 1.0  # cold air in the bedroom
    assert allnight.discomfort_fh > closed.discomfort_fh
    assert allnight.score > closed.score
    # HVAC is paused while windows are open: the cold bedroom is not heated
    assert allnight.heat_min == 0
    assert closed.heat_min > 0 or closed.bed_min_f >= 66.0 - 1.5


def test_sleep_band_outer_loop_raises_the_heat_when_the_bedroom_is_cold() -> None:
    cold_bed = cold_night(readings={HALL: 68.0, BED: 63.0})
    with_loop = ns.plan_night(cold_bed).option(ns.OPT_CLOSED)
    without = ns.plan_night(dataclasses.replace(cold_bed, offset_loop=False)).option(ns.OPT_CLOSED)
    assert with_loop is not None and without is not None
    # the hallway is in band, so without #1194 the thermostat never starts and the bedroom stays cold
    assert with_loop.heat_min > without.heat_min
    assert with_loop.bed_wake_f > without.bed_wake_f


def test_warm_night_fan_saves_ac_minutes_over_closed() -> None:
    st = ns.plan_night(warm_night())
    closed = st.option(ns.OPT_CLOSED)
    fan = st.option(ns.OPT_FAN_UNTIL_TARGET)
    assert closed is not None and fan is not None
    assert closed.cool_min > 0
    assert fan.cool_min < closed.cool_min
    assert fan.runs and fan.n_actions == 2
    assert fan.bed_min_f < closed.bed_min_f
    kinds = [a.kind for a in fan.actions]
    assert kinds[0] in ("windows_open", "fan_on") and set(kinds) == {
        "windows_open",
        "windows_close",
        "fan_on",
        "fan_off",
    }
    off = next(a for a in fan.actions if a.kind == "fan_off")
    assert off.predicted is True and START < off.ts < END  # a condition-based stop is flagged as a prediction


def test_fixed_window_option_opens_at_bedtime_and_closes_after_n_hours() -> None:
    st = ns.plan_night(warm_night())
    fan2 = st.option(ns.OPT_FAN_2H)
    assert fan2 is not None
    opens = [a for a in fan2.actions if a.kind == "windows_open"]
    closes = [a for a in fan2.actions if a.kind == "windows_close"]
    assert opens[0].ts == START
    assert closes[0].ts == START + timedelta(hours=2)
    assert closes[0].predicted is False


def test_option_that_would_not_run_is_never_the_pick_and_counts_no_actions() -> None:
    # bedroom already under the "fan until" target: the fan never needs to run
    st = ns.plan_night(cold_night(readings={HALL: 66.5, BED: 66.4}))
    fu = st.option(ns.OPT_FAN_UNTIL_TARGET)
    assert fu is not None and not fu.runs and fu.n_actions == 0 and "would not run" in fu.note
    assert st.recommended != ns.OPT_FAN_UNTIL_TARGET


def test_plan_is_deterministic_and_fast() -> None:
    a = ns.plan_night(cold_night()).to_dict()
    b = ns.plan_night(cold_night()).to_dict()
    assert a == b
    json.dumps(a)  # JSON-safe


# ------------------------------------------------------------------------------------ options per fan mode (DP-3)
def test_options_per_fan_mode() -> None:
    whf = ns.plan_night(cold_night(fan_mode="whole_house_fan"))
    keys = {o.key for o in whf.options} | {h["key"] for h in whf.hidden}
    assert keys == WHF_KEYS
    hv = ns.plan_night(cold_night(fan_mode="hvac_fan"))
    keys = {o.key for o in hv.options} | {h["key"] for h in hv.hidden}
    assert keys == {ns.OPT_CLOSED, ns.OPT_WINDOWS_2H, ns.OPT_WINDOWS_ALL, ns.OPT_BLOWER_2H}


def test_blower_keeps_hvac_running_and_windows_closed() -> None:
    st = ns.plan_night(cold_night(fan_mode="hvac_fan", readings={HALL: 66.0, BED: 65.0}))
    blower = st.option(ns.OPT_BLOWER_2H)
    assert blower is not None
    assert blower.used["window_steps"] == 0 and blower.used["fan_steps"] > 0
    assert blower.used["hvac_paused_steps"] == 0  # the blower is the HVAC fan: HVAC is not paused
    assert all(a.kind in ("fan_on", "fan_off") for a in blower.actions)


@pytest.mark.parametrize("mode", ["both", "something_else"])
def test_unsupported_fan_modes_only_get_the_baseline(mode: str) -> None:
    """Scope fact (#1175/#1197): `both` is never planned for. No fan option, offered or hidden."""
    st = ns.plan_night(cold_night(fan_mode=mode))
    assert [o.key for o in st.options] == [ns.OPT_CLOSED]
    assert st.hidden == ()
    assert ns.options_for_fan_mode(mode) == (ns.OPT_CLOSED,)


def test_no_fan_configured_gets_window_options_only() -> None:
    st = ns.plan_night(cold_night(fan_mode="disabled"))
    keys = {o.key for o in st.options} | {h["key"] for h in st.hidden}
    assert keys == {ns.OPT_CLOSED, ns.OPT_WINDOWS_2H, ns.OPT_WINDOWS_ALL}


# ------------------------------------------------------------------------------------------- the confidence gate
def test_unvalidated_options_are_hidden_and_never_in_the_occupant_payload() -> None:
    st = ns.plan_night(cold_night(validation=None))
    offered = {o.key for o in st.options}
    assert offered == {ns.OPT_CLOSED}
    hidden = {h["key"]: h for h in st.hidden}
    assert set(hidden) == {
        ns.OPT_WINDOWS_2H,
        ns.OPT_WINDOWS_ALL,
        ns.OPT_FAN_2H,
        ns.OPT_FAN_UNTIL_TARGET,
        ns.OPT_FAN_THEN_AC,
    }
    for key in (ns.OPT_WINDOWS_2H, ns.OPT_FAN_2H):
        assert any("recorded" in r and "replayed" in r for r in hidden[key]["reasons"]), hidden[key]
    payload = json.dumps(st.to_dict())
    for key in hidden:
        assert key not in payload, "a hidden option leaked into the occupant payload"
    assert "hidden" not in st.to_dict() and "debug" not in st.to_dict()
    dbg = st.to_dict(include_debug=True)
    assert {h["key"] for h in dbg["debug"]["hidden"]} == set(hidden)  # debug field carries the reasons
    assert all(h["reasons"] for h in dbg["debug"]["hidden"])


def test_fully_validated_night_offers_everything() -> None:
    st = ns.plan_night(warm_night())
    assert {o.key for o in st.options} == WHF_KEYS
    assert st.hidden == ()
    assert st.show_numbers
    assert all(o.grade in ("medium", "high") for o in st.options)


def test_replay_that_misses_the_limit_hides_the_class() -> None:
    bad = dataclasses.replace(
        GOOD_VALIDATION,
        classes={**GOOD_VALIDATION.classes, ns.CLS_FAN: ns.ClassValidation(40, 1.8, 2.5, 1.2)},
    )
    st = ns.plan_night(warm_night(validation=bad))
    assert not any(o.cls == ns.CLS_FAN and o.key != ns.OPT_CLOSED and o.runs for o in st.options)
    why = next(h for h in st.hidden if h["key"] == ns.OPT_FAN_UNTIL_TARGET)["reasons"]
    assert any("1.8" in r and "limit" in r for r in why)


def test_class_no_better_than_nothing_changes_is_not_validated() -> None:
    val = ns.NightValidation({ns.CLS_FAN: ns.ClassValidation(12, 1.4, 1.3, 0.0)})
    grade, why = ns.validation_status(val, ns.CLS_FAN)
    assert grade == "low" and "nothing changes" in why


def test_validation_thresholds() -> None:
    v = ns.NightValidation({ns.CLS_FAN: ns.ClassValidation(2, 0.5, 2.0)})
    assert ns.validation_status(v, ns.CLS_FAN)[0] == "low"  # too few nights
    v = ns.NightValidation({ns.CLS_FAN: ns.ClassValidation(3, 1.5, 2.0)})
    assert ns.validation_status(v, ns.CLS_FAN)[0] == "medium"
    v = ns.NightValidation({ns.CLS_FAN: ns.ClassValidation(10, 1.0, 2.0)})
    assert ns.validation_status(v, ns.CLS_FAN)[0] == "high"
    v = ns.NightValidation({ns.CLS_FAN: ns.ClassValidation(10, 1.0, 2.0)}, provisional=True)
    assert ns.validation_status(v, ns.CLS_FAN)[0] == "medium"  # provisional replays never reach "high"
    assert ns.validation_status(None, ns.CLS_FAN)[0] == "unknown"


def test_provisional_validation_caps_every_offered_grade_at_medium() -> None:
    prov = dataclasses.replace(GOOD_VALIDATION, provisional=True)
    st = ns.plan_night(warm_night(validation=prov))
    assert st.options and all(o.grade == "medium" for o in st.options)


def test_frozen_fan_rate_hides_fan_options_that_use_it() -> None:
    st = ns.plan_night(warm_night(model=make_model(frozen=["h/a_f"])))
    hidden = {h["key"]: h for h in st.hidden}
    assert ns.OPT_FAN_UNTIL_TARGET in hidden and ns.OPT_FAN_2H in hidden
    assert any("a_f" in r for r in hidden[ns.OPT_FAN_UNTIL_TARGET]["reasons"])
    assert ns.OPT_WINDOWS_ALL in {o.key for o in st.options}  # windows do not depend on the fan rate


def test_unresolved_fan_effect_hides_fan_options() -> None:
    st = ns.plan_night(warm_night(model=make_model(a_f=0.014)))
    assert {h["key"] for h in st.hidden} >= {ns.OPT_FAN_2H, ns.OPT_FAN_UNTIL_TARGET}


def test_windows_only_needs_a_resolved_window_effect() -> None:
    """Zone 1 fitted a_w 0.008/h next to a_o 0.037/h: windows-only is indistinguishable from closed there."""
    st = ns.plan_night(cold_night(model=make_model(a_w=0.008, a_o=0.037)))
    hidden = {h["key"]: h for h in st.hidden}
    assert {ns.OPT_WINDOWS_2H, ns.OPT_WINDOWS_ALL} <= set(hidden)
    assert any("not resolved" in r for r in hidden[ns.OPT_WINDOWS_ALL]["reasons"])
    st = ns.plan_night(cold_night(model=make_model(a_w=0.2, a_o=0.04)))
    assert ns.OPT_WINDOWS_ALL in {o.key for o in st.options}


def test_frozen_window_rate_hides_window_options() -> None:
    st = ns.plan_night(cold_night(model=make_model(frozen=["h/a_w"])))
    assert ns.OPT_WINDOWS_ALL in {h["key"] for h in st.hidden}


def test_assumed_ac_rate_hides_an_option_that_needs_the_ac() -> None:
    """Cooling never seen in the fit: q_c is tied to q_h by a ratio, so a plan that runs the AC is a guess."""
    st = ns.plan_night(warm_night(model=make_model(frozen=["h/q_c"])))
    closed = st.option(ns.OPT_CLOSED)
    assert closed is not None and closed.cool_min > 0
    assert closed.grade == "low"  # the baseline stays, flagged
    assert any("q_c" in r for r in closed.reasons)
    assert not st.show_numbers


def test_thin_bedroom_history_keeps_only_the_baseline_without_numbers() -> None:
    ev = dataclasses.replace(GOOD_EVIDENCE, sensor_hours={HALL: 5000, BED: 2})
    st = ns.plan_night(cold_night(evidence=ev))
    assert [o.key for o in st.options] == [ns.OPT_CLOSED]
    assert st.show_numbers is False
    assert st.options[0].grade == "low" and any("bedroom sensor" in r for r in st.options[0].reasons)
    assert st.recommended == ns.OPT_CLOSED
    assert ns.headline(st) is not None


def test_model_without_confidence_offers_only_the_baseline() -> None:
    st = ns.plan_night(cold_night(model=make_model(confidence="low")))
    assert [o.key for o in st.options] == [ns.OPT_CLOSED]
    assert not st.show_numbers


def test_missing_bedroom_reading_falls_back_to_the_thermostat_room_and_says_so() -> None:
    st = ns.plan_night(cold_night(readings={HALL: 68.0}))
    assert st.target_sensor == HALL and st.debug["target_fallback"] is True
    assert st.option(ns.OPT_CLOSED) is not None and st.option(ns.OPT_CLOSED).grade == "low"  # type: ignore[union-attr]


def test_no_history_counts_means_low() -> None:
    st = ns.plan_night(cold_night(evidence=None))
    assert [o.key for o in st.options] == [ns.OPT_CLOSED] and st.options[0].grade == "low"


def test_evidence_from_samples_counts_hours() -> None:
    def s(i: int, win: bool, fan: bool, heat: float, cool: float, bed: float | None) -> p4.V4Sample:
        return p4.V4Sample(
            ts=START + timedelta(minutes=15 * i), outdoor_f=50.0, forecast_outdoor_f=50.0, hvac_action="idle",
            fan_running=fan, windows_open=win, heat_sp_f=None, cool_sp_f=None,
            readings={HALL: 68.0, BED: bed}, heat_duty=heat, cool_duty=cool,
        )  # fmt: skip

    rows = [
        s(0, True, False, 0, 0, 66),
        s(1, True, True, 0.5, 0, None),
        s(2, False, True, 0, 1.0, 66),
        s(3, False, False, 1.0, 0, 66),
    ]
    ev = ns.evidence_from_samples(rows)
    assert ev.window_open_hours == 0.5 and ev.fan_hours == 0.5
    assert ev.heat_hours == pytest.approx(0.375, abs=0.01) and ev.cool_hours == pytest.approx(0.25, abs=0.01)
    assert ev.sensor_hours[BED] == 0.75 and ev.sensor_hours[HALL] == 1.0


def test_validation_round_trip_and_rejects_foreign_schema() -> None:
    d = GOOD_VALIDATION.to_dict()
    back = ns.NightValidation.from_dict(json.loads(json.dumps(d)))
    assert back is not None and back.classes[ns.CLS_FAN].n_nights == 40 and back.provisional is False
    assert ns.NightValidation.from_dict({"schema_version": 99}) is None
    assert ns.NightValidation.from_dict("nope") is None


# ------------------------------------------------------------------------------------------- recommendation text
def test_headline_names_the_close_time_and_what_ca_keeps() -> None:
    st = ns.plan_night(warm_night())
    text = ns.headline(st)
    assert text and text.endswith(".")
    # cold night, closed: nothing to do, CA keeps the heat at the floor
    cold = ns.headline(ns.plan_night(cold_night()))
    assert cold is not None and cold.startswith("Keep the windows closed") and "66" in cold


def test_tradeoffs_compare_every_alternative_to_the_pick() -> None:
    st = ns.plan_night(cold_night())
    rows = ns.tradeoffs(st)
    assert {r["key"] for r in rows} == {o.key for o in st.options} - {st.recommended}
    allnight = next(r for r in rows if r["key"] == ns.OPT_WINDOWS_ALL)
    assert allnight["score_delta"] > 0 and "outside your sleep band" in allnight["text"]


# ------------------------------------------------------------------------------------------- the timeline step
class _Actor(StrEnum):
    CA = "ca"
    YOU = "you"


class _Kind(StrEnum):
    NAT_VENT_START = "nat_vent_start"
    NAT_VENT_STOP = "nat_vent_stop"
    SLEEP_BAND = "sleep_band"
    WAKE_BAND = "wake_band"
    WINDOWS_OPEN = "windows_open"
    WINDOWS_CLOSE = "windows_close"
    FAN_ON = "fan_on"
    FAN_OFF = "fan_off"
    NIGHT_STRATEGY = "night_strategy"


@dataclass(frozen=True)
class _Step:
    ts: datetime
    actor: _Actor
    kind: _Kind
    text: str
    reason: str
    target_f: float | None = None
    mode: str | None = None
    confidence: str = "ok"
    lead_minutes: int = 0
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Point:
    ts: datetime
    temp_f: float
    sensor: str = "_ca"
    model: str = "v3"


@dataclass(frozen=True)
class _Timeline:
    schema_version: int
    zone: str
    generated_at: datetime
    day: date
    horizon_end: datetime
    steps: tuple[_Step, ...]
    path: tuple[_Point, ...]
    latched: bool
    regen_reason: str | None


STUB = SimpleNamespace(Actor=_Actor, StepKind=_Kind, TimelineStep=_Step, PathPoint=_Point, Timeline=_Timeline)


@pytest.fixture
def stub_timeline(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(ns, "_timeline_module", lambda: STUB)
    return STUB


def _timeline(*steps: _Step, path: tuple[_Point, ...] = ()) -> _Timeline:
    return _Timeline(
        schema_version=1, zone="z1", generated_at=START - timedelta(hours=3), day=START.date(),
        horizon_end=START + timedelta(hours=40), steps=tuple(steps), path=path, latched=True, regen_reason=None,
    )  # fmt: skip


def _step(ts: datetime, actor: _Actor, kind: _Kind, **kw: Any) -> _Step:
    return _Step(ts=ts, actor=actor, kind=kind, text=kw.pop("text", f"{kind}"), reason=kw.pop("reason", "x"), **kw)


def test_apply_adds_ca_summary_and_you_window_steps_with_lead(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(warm_night())
    chosen = st.option(ns.OPT_WINDOWS_2H)
    assert chosen is not None
    tl = ns.apply_night_strategy(_timeline(), chosen, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    kinds = [(s.kind, s.actor) for s in tl.steps]
    assert kinds[0] == (_Kind.NIGHT_STRATEGY, _Actor.CA)
    assert (_Kind.WINDOWS_OPEN, _Actor.YOU) in kinds and (_Kind.WINDOWS_CLOSE, _Actor.YOU) in kinds
    for s in tl.steps:
        if s.actor == _Actor.YOU:
            assert s.lead_minutes == ns.YOU_STEP_LEAD_MIN and s.confidence == "ok" and s.source == ns.STEP_SOURCE
    assert [s.ts for s in tl.steps] == sorted(s.ts for s in tl.steps)
    # the CA step never instructs the occupant (Next Automation must be automation-executed, #849)
    ca = next(s for s in tl.steps if s.kind == _Kind.NIGHT_STRATEGY)
    assert "window" not in ca.text.lower() and "open" not in ca.text.lower()


def test_apply_is_pure_and_idempotent(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(warm_night())
    base = _timeline(_step(START - timedelta(hours=1), _Actor.CA, _Kind.SLEEP_BAND))
    once = ns.apply_night_strategy(base, st)
    twice = ns.apply_night_strategy(once, st)
    assert once == twice
    assert len(base.steps) == 1  # input untouched
    other = st.option(ns.OPT_WINDOWS_ALL)
    swapped = ns.apply_night_strategy(once, other, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    assert sum(1 for s in swapped.steps if s.kind == _Kind.NIGHT_STRATEGY) == 1


def test_fan_steps_are_you_advice_unless_ca_natvent_already_runs_the_fan(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(warm_night())
    fan = st.option(ns.OPT_FAN_2H)
    assert fan is not None
    tl = ns.apply_night_strategy(_timeline(), fan, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    fan_steps = [s for s in tl.steps if s.kind in (_Kind.FAN_ON, _Kind.FAN_OFF)]
    assert len(fan_steps) == 2 and all(
        s.actor == _Actor.YOU for s in fan_steps
    )  # R4: CA never runs a fan for a strategy
    on = START
    off = START + timedelta(hours=2)
    natvent = _timeline(
        _step(on - timedelta(minutes=5), _Actor.CA, _Kind.NAT_VENT_START),
        _step(off + timedelta(minutes=5), _Actor.CA, _Kind.NAT_VENT_STOP),
    )
    tl2 = ns.apply_night_strategy(natvent, fan, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    assert not [s for s in tl2.steps if s.kind in (_Kind.FAN_ON, _Kind.FAN_OFF)]  # CA's own rule runs it
    assert next(s for s in tl2.steps if s.kind == _Kind.NIGHT_STRATEGY).meta["fan_by_ca_natvent"] is True
    assert len([s for s in tl2.steps if s.kind in (_Kind.NAT_VENT_START, _Kind.NAT_VENT_STOP)]) == 2
    # a session that stops half way covers only part of the span: the occupant steps stay
    partial = _timeline(
        _step(on, _Actor.CA, _Kind.NAT_VENT_START), _step(on + timedelta(minutes=30), _Actor.CA, _Kind.NAT_VENT_STOP)
    )
    tl3 = ns.apply_night_strategy(partial, fan, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    assert [s for s in tl3.steps if s.kind in (_Kind.FAN_ON, _Kind.FAN_OFF)]


def test_apply_replaces_the_plans_own_window_steps_inside_the_night(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(warm_night())
    fan = st.option(ns.OPT_FAN_2H)
    assert fan is not None
    generic_in = _step(START + timedelta(hours=1), _Actor.YOU, _Kind.WINDOWS_CLOSE, meta={"id": "generic-close"})
    generic_before = _step(START - timedelta(hours=2), _Actor.YOU, _Kind.WINDOWS_OPEN, meta={"id": "generic-evening"})
    tl = ns.apply_night_strategy(
        _timeline(generic_before, generic_in), fan, floor_f=66, ceiling_f=72, start=START, end=END
    )
    assert generic_before in tl.steps and generic_in not in tl.steps
    assert next(s for s in tl.steps if s.kind == _Kind.NIGHT_STRATEGY).meta["replaced"] == ["generic-close"]


def test_closed_pick_keeps_the_generic_close_but_drops_a_generic_open(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(cold_night())
    closed = st.best
    assert closed is not None and closed.key == ns.OPT_CLOSED
    keep = _step(START, _Actor.YOU, _Kind.WINDOWS_CLOSE, meta={"id": "close"})
    drop = _step(START + timedelta(hours=1), _Actor.YOU, _Kind.WINDOWS_OPEN, meta={"id": "open"})
    tl = ns.apply_night_strategy(_timeline(keep, drop), st)
    assert keep in tl.steps and drop not in tl.steps
    plan = next(s for s in tl.steps if s.kind == _Kind.NIGHT_STRATEGY)
    assert "nothing for you to do" in plan.text
    assert not [s for s in tl.steps if s.actor == _Actor.YOU and s is not keep]


def test_apply_replaces_the_target_path_in_the_night_span(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(cold_night())
    old = tuple(_Point(START + timedelta(minutes=15 * k), 70.0, BED, "v3") for k in range(1, 5))
    outside = _Point(START - timedelta(hours=1), 70.0, BED, "v3")
    hall = _Point(START + timedelta(minutes=15), 70.0, HALL, "v4")
    tl = ns.apply_night_strategy(_timeline(path=(outside, hall, *old)), st)
    bed_pts = [p for p in tl.path if p.sensor == BED]
    assert outside in tl.path and hall in tl.path
    assert all(p.model == "v4" for p in bed_pts if p.ts > START) and len(bed_pts) > 4
    assert [(p.sensor, p.ts) for p in tl.path] == sorted((p.sensor, p.ts) for p in tl.path)


def test_low_confidence_pick_adds_no_you_steps(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(cold_night(evidence=dataclasses.replace(GOOD_EVIDENCE, sensor_hours={HALL: 5000, BED: 2})))
    tl = ns.apply_night_strategy(_timeline(), st)
    plan = next(s for s in tl.steps if s.kind == _Kind.NIGHT_STRATEGY)
    assert plan.confidence == "low"
    assert not [s for s in tl.steps if s.actor == _Actor.YOU]


def test_step_texts_use_the_unit_and_stay_one_sentence(stub_timeline: SimpleNamespace) -> None:
    st = ns.plan_night(warm_night())
    fan = st.option(ns.OPT_FAN_UNTIL_TARGET)
    tl = ns.apply_night_strategy(_timeline(), fan, floor_f=66.0, ceiling_f=72.0, start=START, end=END, unit="celsius")
    texts = " ".join(s.text for s in tl.steps)
    assert "°C" in texts and "°F" not in texts
    for s in tl.steps:
        assert s.text.count(": ") >= 1 and s.text.endswith(".")
        assert s.reason.startswith("night_strategy.")


def test_every_sentence_declares_its_facts() -> None:
    import string

    for key, sentence in ns.NIGHT_SENTENCES.items():
        fields = {f for _, f, _, _ in string.Formatter().parse(sentence.template) if f}
        assert fields == set(sentence.facts), key


def test_apply_runs_on_the_real_timeline_module_when_it_exists() -> None:
    if importlib.util.find_spec("custom_components.climate_advisor.operation_timeline") is None:
        return  # R3a is not merged into this branch yet; the check activates the moment the module lands
    ot = importlib.import_module("custom_components.climate_advisor.operation_timeline")
    st = ns.plan_night(warm_night())
    fan = st.option(ns.OPT_WINDOWS_2H)
    assert fan is not None
    tl = ot.Timeline(
        schema_version=ot.TIMELINE_SCHEMA_VERSION, zone="z1", generated_at=START, day=START.date(),
        horizon_end=START + timedelta(hours=40), steps=(), path=(), latched=False, regen_reason=None,
    )  # fmt: skip
    out = ns.apply_night_strategy(tl, fan, floor_f=66.0, ceiling_f=72.0, start=START, end=END)
    assert any(s.kind == ot.StepKind.NIGHT_STRATEGY for s in out.steps)
    assert ot.from_dict(ot.to_dict(out)) is not None


# ------------------------------------------------------------------------------------------- draft scenarios (DP-11)
def _scenario_inputs(doc: dict[str, Any]) -> ns.NightInputs:
    m = doc["model"]
    model = p4.V4Model(
        schema_version=p4.V4_SCHEMA_VERSION, fitted_at="2026-11-06T03:15:00-08:00", primary=HALL, sensors=(HALL, BED),
        house=m["house"], nodes={BED: m["node"]},
        thermostat=p4.V4Thermostat(**m["thermostat"]),
        forecast=p4.V4ForecastCorrection(tuple([0.0] * 24), 3.0),
        fit={"frozen": m.get("frozen", []), "reason": ""}, confidence=m["confidence"],
    )  # fmt: skip
    n = doc["night"]
    start, end = datetime.fromisoformat(n["start"]), datetime.fromisoformat(n["end"])
    return ns.NightInputs(
        model=model, start=start, end=end, tz=TZ, fan_mode=doc["config"]["fan_mode"],
        outdoor=outdoor(n["outdoor_start_f"], n["outdoor_end_f"], start, end), readings=n["readings"],
        floor_f=doc["config"]["sleep_floor_f"], ceiling_f=doc["config"]["sleep_ceiling_f"], target_sensor=BED,
        evidence=ns.Evidence(**{**doc["evidence"], "sensor_hours": doc["evidence"]["sensor_hours"]}),
        validation=ns.NightValidation.from_dict({"schema_version": 1, **doc["validation"]}),
    )  # fmt: skip


DRAFTS = sorted(PENDING.glob("night_strategy_*.json.draft"))


def test_draft_scenarios_exist_for_both_fan_modes() -> None:
    assert {p.name for p in DRAFTS} >= {
        "night_strategy_whole_house_fan.json.draft",
        "night_strategy_hvac_fan.json.draft",
    }


@pytest.mark.parametrize("path", DRAFTS, ids=lambda p: p.name.removesuffix(".json.draft"))
def test_draft_scenario_matches_the_engine(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["status"].startswith("DRAFT"), "never signed or promoted from R4"
    for needed in ("owner_card", "three_exercises", "verdict", "assertions"):
        assert doc.get(needed), f"{path.name}: missing {needed}"
    st = ns.plan_night(_scenario_inputs(doc))
    for a in doc["assertions"]:
        expect = a["expect"]
        if expect == "recommended":
            assert st.recommended == a["value"], (path.name, a, st.recommended)
        elif expect == "offered":
            assert {o.key for o in st.options} == set(a["value"]), (path.name, a, [o.key for o in st.options])
        elif expect == "hidden":
            assert {h["key"] for h in st.hidden} == set(a["value"]), (path.name, a)
        elif expect == "score_order":
            order = [o.key for o in st.options if o.key in a["value"]]
            assert order == a["value"], (path.name, a, order)
        elif expect == "show_numbers":
            assert st.show_numbers is a["value"]
        else:
            pytest.fail(f"unknown assertion kind {expect!r}")
