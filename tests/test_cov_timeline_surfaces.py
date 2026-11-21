"""Edge branches of the timeline surfaces (Silver test-coverage).

Occupant view: the Next Action card, the Next Automation card, the chart strips and the plan list all read the one
timeline. These cover the less common steps (a TOU bank, a past window period, an open to warm the house, unknown
readings) so each surface still says what the plan holds.
"""

from __future__ import annotations

from dataclasses import replace

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.operation_timeline import Actor, PredictedRun, StepKind
from custom_components.climate_advisor.timeline_surfaces import (
    CardLive,
    CardText,
    _duration_bar,
    card_text,
    card_user_step,
    is_direction_vetoed,
    next_automation_label,
    next_automation_label_msg,
    plan_list_text,
    strip_covers,
    strip_spans,
    timeline_api_payload,
    window_card_msg,
)
from tests.test_chart_strips_1436 import NOW, _at, _step, _tl


def _live(**kw) -> CardLive:
    base = {
        "unit": "fahrenheit",
        "indoor": 70.0,
        "outdoor": 60.0,
        "windows_open": False,
        "direction_ok": True,
        "heat_side_veto": False,
        "armed_heat": 68.0,
        "armed_cool": 74.0,
    }
    base.update(kw)
    return CardLive(**base)


# ------------------------------------------------------------------------------------------------ cards
def test_card_text_of_a_plain_string_carries_no_message() -> None:
    ct = card_text("Keep windows closed")
    assert isinstance(ct, CardText) and ct == "Keep windows closed" and ct.msg is None


def test_the_card_skips_a_window_period_already_over_and_names_the_next() -> None:
    past_open = _step(_at(9, 0), StepKind.WINDOWS_OPEN, Actor.YOU)
    past_close = _step(_at(11, 0), StepKind.WINDOWS_CLOSE, Actor.YOU)
    next_open = _step(_at(18, 0), StepKind.WINDOWS_OPEN, Actor.YOU)
    next_close = _step(_at(20, 0), StepKind.WINDOWS_CLOSE, Actor.YOU)
    tl = _tl(past_open, past_close, next_open, next_close)
    assert card_user_step(tl, NOW, windows_open=False) is next_open


def _open_now(reason: str = "windows_open"):
    return _tl(
        _step(_at(16, 0), StepKind.WINDOWS_OPEN, Actor.YOU, reason=reason),
        _step(_at(19, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
    )


def test_open_to_warm_the_house_while_outdoor_is_warmer_says_briefly() -> None:
    msg, step = window_card_msg(_open_now("windows_open.free_heating"), NOW, _live(indoor=66.0, outdoor=72.0))
    assert step.kind == StepKind.WINDOWS_OPEN and msg.key == "timeline.card.open_briefly"


def test_open_with_unknown_readings_is_never_vetoed() -> None:
    msg, _step_ = window_card_msg(_open_now(), NOW, _live(indoor=None, outdoor=None))
    assert msg.key == "timeline.card.open_unknown"


def test_outdoor_not_cooler_vetoes_the_open_even_when_the_band_direction_is_ok() -> None:
    msg, step = window_card_msg(_open_now(), NOW, _live(indoor=70.0, outdoor=75.0))
    assert is_direction_vetoed(step) and step.meta["hold"] == "held.direction_veto.warm"
    assert msg is step.msg and "75" in i18n.render_en(msg)


# ------------------------------------------------------------------------------------------------ next automation
def test_next_automation_labels_for_the_tou_and_band_steps() -> None:
    def label(kind, reason, **fields):
        step = replace(_step(_at(18, 0), kind, reason=reason, facts=fields.pop("facts", {})), **fields)
        out = next_automation_label(step, "fahrenheit")
        return None if out is None else str(out)

    assert "72" in label(StepKind.PRECOOL_START, "pre_cool_start.tou", target_f=72.0)
    assert "69" in label(StepKind.PREHEAT_START, "pre_heat_start.tou", target_f=69.0)
    for reason in ("setpoint_change.tou_expansion_start", "setpoint_change.tou_expansion_end"):
        assert "76" in label(StepKind.SETPOINT_CHANGE, reason, target_f=76.0)
    assert label(StepKind.SETPOINT_CHANGE, "setpoint_change.other", target_f=76.0) is None
    # a sleep band on the cooling side names its ceiling; without one there is no label (never a made-up number)
    assert "75" in label(StepKind.SLEEP_BAND, "sleep_band.default", mode="cool", facts={"ceiling": 75.0})
    assert label(StepKind.SLEEP_BAND, "sleep_band.default", mode="cool") is None


# ------------------------------------------------------------------------------------------------ chart strips
def test_run_bursts_with_no_minutes_or_outside_the_window_draw_nothing_and_odd_modes_are_skipped() -> None:
    run = PredictedRun(
        "heat",
        _at(17, 0),
        _at(18, 0),
        10.0,
        "hold_floor",
        ((_at(17, 0), 0.0), (_at(18, 30), 10.0), (_at(17, 15), 10.0)),
    )
    fan_run = PredictedRun("fan", _at(17, 0), _at(18, 0), 10.0, "x")
    spans = strip_spans(_tl(runs=[run, fan_run]), NOW)
    assert [(d["row"], d["start"], d["end"]) for d in spans] == [
        ("heating", _at(17, 15).isoformat(), _at(17, 25).isoformat())
    ]


def test_a_duration_bearing_step_draws_its_minutes_not_the_whole_span() -> None:
    on = _step(_at(17, 0), StepKind.FAN_ON, facts={"duration_min": 30})
    a, b, extra = _duration_bar(_at(17, 0), _at(19, 0), on)
    assert (a, b) == (_at(17, 0), _at(17, 30))
    assert extra == {
        "window_start": _at(17, 0).isoformat(),
        "window_end": _at(19, 0).isoformat(),
        "minutes": 30.0,
        "placed": False,
    }
    off = _step(_at(19, 0), StepKind.FAN_OFF)
    spans = strip_spans(_tl(on, off), NOW)
    assert [(d["row"], d["end"], d["minutes"]) for d in spans] == [("fan", _at(17, 30).isoformat(), 30.0)]


def test_a_fan_on_without_an_off_and_a_span_already_over_draw_nothing() -> None:
    assert strip_spans(_tl(_step(_at(17, 0), StepKind.FAN_ON)), NOW) == []
    past = _tl(_step(_at(9, 0), StepKind.FAN_ON), _step(_at(10, 0), StepKind.FAN_OFF))
    assert strip_spans(past, NOW) == []


def test_strip_covers_skips_malformed_spans() -> None:
    spans = [{"row": "fan"}, {"row": "fan", "start": "bad", "end": "bad"}, {"row": "fan", "start": None, "end": 1}]
    assert strip_covers(spans, "fan", NOW) is False


# ------------------------------------------------------------------------------------------------ plan list / API
def test_plan_list_text_keeps_a_sentence_that_is_only_its_time() -> None:
    assert plan_list_text("8:30 PM: ", Actor.CA.value) == "8:30 PM: "


def test_api_payload_for_an_old_step_without_a_message_and_without_detail() -> None:
    contract = {"steps": [{"text": "8:30 PM: CA switches to the sleep band.", "actor": "ca", "target_f": 68.0}]}
    out = timeline_api_payload(contract, None, "celsius", lambda v, u: round((v - 32) * 5 / 9, 1))
    (step,) = out["steps"]
    assert step["text_short"] == "Switches to the sleep band." and step["target_f"] == 20.0
    assert "detail" not in out and out["path"] == []


def test_api_payload_detail_without_a_summary_converts_its_bands() -> None:
    detail = {"band": [{"floor": 68.0, "ceiling": 77.0}], "days": [{"expected_high": 86.0, "expected_low": None}]}
    out = timeline_api_payload({"steps": []}, detail, "celsius", lambda v, u: round((v - 32) * 5 / 9, 1))
    assert out["detail"]["band"] == [{"floor": 20.0, "ceiling": 25.0}]
    assert out["detail"]["days"] == [{"expected_high": 30.0, "expected_low": None}]
    assert "summary" not in out["detail"]


def test_next_automation_window_start_is_not_a_candidate() -> None:
    planned = _step(_at(18, 0), StepKind.NAT_VENT_START, reason="natvent_start.planned")
    assert next_automation_label_msg(planned, "fahrenheit") is None
