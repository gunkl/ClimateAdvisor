"""Edge branches of the bedroom night strategy (Silver test-coverage).

Occupant view: "Tonight in the bedroom" offers only options v4 can back with the house's own history. Thin history,
an unlearned rate, a bedroom sensor that is not part of the model, a missing reading or a home without AC each lower
or hide an option (with the reason), and the plan's words (headline, trade-offs) match the chosen option.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor import operation_timeline as ot
from tests.test_night_strategy import BED, END, GOOD_EVIDENCE, HALL, START, _opt, cold_night, make_model, warm_night


def _sim(n: int = 8, *, uh=0.0, uc=0.0, win=0.0, fan=0.0) -> ns._Sim:
    ts = [START + timedelta(minutes=15 * k) for k in range(n)]
    return ns._Sim(ts, [68.0] * n, [67.0] * n, [uh] * n, [uc] * n, [win] * n, [fan] * n, 0)


def _grade(inp, spec_key, sim, target=BED, fallback=False):
    return ns.grade_rates(inp, ns._SPECS[spec_key], sim, target, target_fallback=fallback)


# ------------------------------------------------------------------------------------------------ stored validation
def test_from_dict_skips_unknown_classes_missing_counts_and_non_finite_errors() -> None:
    d = {
        "schema_version": ns.NIGHT_STRATEGY_SCHEMA_VERSION,
        "classes": {
            "teleport": {"n_nights": 9, "mae_f": 0.5},
            ns.CLS_CLOSED: {"mae_f": 0.5},
            ns.CLS_WINDOWS: {"n_nights": 5, "mae_f": float("nan")},
            ns.CLS_FAN: {"n_nights": 5, "mae_f": 0.8, "flat_mae_f": "x", "bias_f": float("inf")},
        },
    }
    val = ns.NightValidation.from_dict(d)
    assert val is not None and set(val.classes) == {ns.CLS_FAN}
    assert val.classes[ns.CLS_FAN] == ns.ClassValidation(5, 0.8, None, None)


# ------------------------------------------------------------------------------------------------ grade_rates
def test_an_unusable_model_grades_every_option_low_with_its_reason() -> None:
    inp = cold_night(model=make_model(confidence="none"))
    grade, reasons = _grade(inp, ns.OPT_CLOSED, _sim())
    assert grade == "low" and reasons[0].startswith("v4 model unusable (thin history)")


def test_heating_history_thin_is_low_and_short_of_high_is_medium() -> None:
    thin = cold_night(evidence=dataclasses.replace(GOOD_EVIDENCE, heat_hours=2.0))
    grade, reasons = _grade(thin, ns.OPT_CLOSED, _sim(uh=1.0))
    assert grade == "low" and "heating: 2 h of history (need 4)" in reasons
    medium = cold_night(evidence=dataclasses.replace(GOOD_EVIDENCE, heat_hours=6.0))
    assert _grade(medium, ns.OPT_CLOSED, _sim(uh=1.0))[0] == "medium"


def test_an_unlearned_heating_rate_is_low() -> None:
    inp = cold_night(model=make_model(frozen=["h/q_h"]))
    assert "heating rate q_h not learned" in _grade(inp, ns.OPT_CLOSED, _sim(uh=1.0))[1][0]


def test_a_bedroom_sensor_outside_the_model_is_low() -> None:
    inp = cold_night()
    grade, reasons = _grade(inp, ns.OPT_CLOSED, _sim(), target="sensor.attic")
    assert grade == "low" and "bedroom sensor is not a node of the v4 model" in reasons


def test_bedroom_sensor_hours_short_of_high_is_medium() -> None:
    ev = dataclasses.replace(GOOD_EVIDENCE, sensor_hours={HALL: 5000, BED: 100})
    assert _grade(cold_night(evidence=ev), ns.OPT_CLOSED, _sim())[0] == "medium"


def test_unlearned_bedroom_shares_are_low() -> None:
    inp = cold_night(model=make_model(frozen=["n/0/q_b", "n/0/b_w"]))
    _g, reasons = _grade(inp, ns.OPT_WINDOWS_2H, _sim(uh=1.0, win=1.0))
    assert "bedroom share of the HVAC gain not learned" in reasons
    assert "bedroom window rate b_w not learned" in reasons


# ------------------------------------------------------------------------------------------------ plan_night
def test_options_that_need_the_ac_are_hidden_in_a_home_without_cooling() -> None:
    plan = ns.plan_night(warm_night(can_cool=False))
    hidden = {h["key"]: h["reasons"] for h in plan.hidden}
    assert hidden[ns.OPT_FAN_THEN_AC] == ["no cooling available to assist"]
    assert plan.option(ns.OPT_FAN_THEN_AC) is None


def test_without_a_forecast_or_a_reading_no_option_can_be_simulated() -> None:
    for inp in (cold_night(outdoor=[]), cold_night(readings={BED: 67.0}, target_sensor=None)):
        plan = ns.plan_night(inp)
        assert plan.options == () and plan.best is None
        assert all(h["reasons"] == ["could not simulate: missing reading or forecast"] for h in plan.hidden)
        assert ns.tradeoffs(plan) == [] and ns.headline(plan) is None


def test_rank_of_nothing_is_nothing() -> None:
    assert ns.rank([]) == []


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="latent bug: _label_msg passes key=key to i18n.Msg(key, **params), whose first argument is already 'key' "
    "(TypeError). Every option in _SPECS has its own label today, so no night plan reaches it yet.",
)
def test_an_unknown_option_key_has_a_generic_label() -> None:
    assert ns._label_msg("teleport", cold_night()).key == "night.option_unknown"
    assert "teleport" in ns._label("teleport", cold_night(), None)


# ------------------------------------------------------------------------------------------------ presentation
def _strategy(*options, recommended):
    return ns.NightStrategy(
        start=START,
        end=END,
        floor_f=66.0,
        ceiling_f=72.0,
        target_sensor=BED,
        fan_mode="whole_house_fan",
        options=tuple(options),
        recommended=recommended,
        show_numbers=True,
    )


def _o(key, *, score=1.0, actions=0, disc=0.0, heat=0, cool=0, runs=True, acts=(), label_msg=None, note_msg=None):
    return dataclasses.replace(
        _opt(key, score, actions),
        discomfort_fh=disc,
        heat_min=heat,
        cool_min=cool,
        runs=runs,
        actions=tuple(acts),
        label_msg=label_msg,
        note_msg=note_msg,
    )


def test_tradeoffs_fewer_discomfort_fewer_actions_and_the_same() -> None:
    best = _o(ns.OPT_WINDOWS_2H, actions=2, disc=1.0)
    better_sleep = _o(ns.OPT_CLOSED, actions=0, disc=0.0)  # less discomfort, fewer actions
    same = _o(ns.OPT_FAN_2H, actions=2, disc=1.0)  # nothing differs: "about the same"
    rows = {r["key"]: r for r in ns.tradeoffs(_strategy(best, better_sleep, same, recommended=best.key))}
    assert rows[ns.OPT_CLOSED]["text_msg"]["key"] == "night.tradeoff"
    parts = [p["key"] for p in rows[ns.OPT_CLOSED]["text_msg"]["params"]["parts"]["items"]]
    assert parts == ["night.tradeoff_disc_fewer", "night.tradeoff_actions_fewer"]
    assert rows[ns.OPT_FAN_2H]["text_msg"]["key"] == "night.tradeoff_same"
    assert "label_msg" not in rows[ns.OPT_FAN_2H]


def test_headlines_for_windows_all_night_and_an_open_with_no_close() -> None:
    allnight = _o(ns.OPT_WINDOWS_ALL, acts=[ns.OptionAction(START, "windows_open")])
    msg = ns.headline_msg(_strategy(allnight, recommended=allnight.key))
    assert msg.params["text"].key == "night.headline_windows_all"
    open_only = _o(ns.OPT_FAN_2H, acts=[ns.OptionAction(START, "fan_on")], heat=10, cool=5)
    msg = ns.headline_msg(_strategy(open_only, recommended=open_only.key))
    assert msg.params["text"].key == "night.headline_open_only" and msg.params["text"].params["what"] == "fan"
    assert msg.params["hold"].key == "night.hold_both"


def test_option_to_dict_carries_message_siblings_and_can_drop_the_path() -> None:
    o = _o(ns.OPT_CLOSED, label_msg=i18n.Msg("night.option_closed"), note_msg=i18n.Msg("night.option_closed"))
    d = o.to_dict(include_path=False)
    assert d["label_msg"]["key"] == "night.option_closed" and d["note_msg"]["key"] == "night.option_closed"
    assert "path" not in d


def test_api_payload_of_an_option_without_messages_keeps_its_stored_label() -> None:
    o = _o(ns.OPT_CLOSED)
    out = ns.api_payload(_strategy(o, recommended=o.key), unit="fahrenheit", convert=lambda v, u: v)
    assert out["options"][0]["label"] == ns.OPT_CLOSED


def test_best_and_option_without_a_match_are_none() -> None:
    s = _strategy(_o(ns.OPT_CLOSED), recommended="teleport")
    assert s.best is None and s.option("teleport") is None


# ------------------------------------------------------------------------------------------------ timeline step
def _empty_timeline():
    return ot.Timeline(
        schema_version=1,
        zone="z",
        generated_at=START - timedelta(hours=3),
        day=START.date(),
        horizon_end=START + timedelta(hours=24),
        steps=(),
        path=(),
        latched=True,
        regen_reason=None,
    )


def test_apply_without_a_pick_returns_the_timeline_unchanged() -> None:
    tl = _empty_timeline()
    assert ns.apply_night_strategy(tl, _strategy(_o(ns.OPT_CLOSED), recommended=None)) is tl


def test_apply_a_bare_option_needs_its_band_and_span() -> None:
    with pytest.raises(ValueError, match="needs floor_f"):
        ns.apply_night_strategy(_empty_timeline(), _o(ns.OPT_CLOSED))


def test_a_natvent_session_that_starts_after_the_fan_does_not_cover_it() -> None:
    late = START + timedelta(hours=2)
    steps = [ot.TimelineStep(ts=late, actor=ot.Actor.CA, kind=ot.StepKind.NAT_VENT_START, text="", reason="x")]
    assert ns._covered_by_natvent(steps, ot.StepKind, ot.Actor, START, START + timedelta(hours=3)) is False
