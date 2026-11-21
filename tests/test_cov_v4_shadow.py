"""Edge branches of the v4 shadow glue (Silver test-coverage).

Occupant view: the Debug tab and the Activity log explain in plain words why v4 has its grade and which room it
forecasts from; the Status chart draws v4's extra lines. A stored reason v4 has no words for, an unreadable model, an
odd clock or a broken forecast archive must give an honest answer (or no line), never a crash or a wrong room.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import i18n, v4_shadow
from custom_components.climate_advisor import predictor_v4 as p4
from custom_components.climate_advisor.const import REMOTE_CONTEXT_SLEEP
from tests.test_night_strategy import make_model
from tests.timeline_fixtures import TZ

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=TZ)
BAD_MODEL = {"schema": "not-a-v4-model"}


def _en(msg) -> str:
    return "" if msg is None else (msg if isinstance(msg, str) else i18n.render_en(msg))


# ------------------------------------------------------------------------------------------------ describe_grade
def _summary(**kw):
    base = {"has_model": True, "confidence": "low", "modes": {}, "sample_days": 12.0}
    base.update(kw)
    return base


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        ("under 3 days of samples", "12"),
        ("fit diverged", "did not settle"),
        ("does not beat no-change at 4 h by 10 %", "4"),
    ],
)
def test_grade_reason_codes_have_their_own_words(code: str, needle: str) -> None:
    why, by_mode = v4_shadow.describe_grade(_summary(confidence_reason=code))
    assert why and needle in why
    assert by_mode == ""  # no per-mode block without modes


def test_an_unknown_stored_reason_is_shown_as_it_is() -> None:
    why, _ = v4_shadow.describe_grade(_summary(confidence_reason="a reason from a newer release"))
    assert why == "a reason from a newer release"


def test_a_seen_mode_without_a_count_and_a_low_grade_with_its_own_reason() -> None:
    modes = {
        "heat": {"seen": True, "starts": 3, "grade": "ok"},  # no accepted count
        "cool": {"seen": True, "accepted": 6, "starts": 8, "grade": "low", "reason": "fit diverged"},
    }
    _why, by_mode = v4_shadow.describe_grade(_summary(modes=modes, confidence_reason=""))
    heat, cool = by_mode.split(" · ")
    assert heat.startswith("heating") and "usable" not in heat
    assert cool.startswith("cooling low") and "6 usable of 8" in cool and "diverge" in cool


# ------------------------------------------------------------------------------------------------ describe_node_choice
def _choice(reason: str, **kw):
    base = {"model": p4.MODEL_STITCHED, "primary": "climate.hall", "sleep": None, "reason": reason, "lead": "4h"}
    base.update(kw)
    return base


SCORES_4H_ONLY = {"mae": {p4.MODEL_STITCHED: {"4h": 0.5}, p4.MODEL_PER_ROOM: {"4h": 0.6}}}
SCORES_4H_1H = {"mae": {p4.MODEL_STITCHED: {"4h": 0.5, "1h": 0.2}, p4.MODEL_PER_ROOM: {"4h": 0.6, "1h": 0.3}}}


def test_per_room_scored_worse_without_a_tolerance_and_with_one_other_lead() -> None:
    _h, why = v4_shadow.describe_node_choice(_choice("per_room_scored_worse", scores=SCORES_4H_1H))
    assert "0.60°F" in why and "0.50°F" in why and "0.30°F" in why
    _h, why_one = v4_shadow.describe_node_choice(_choice("per_room_scored_worse", scores=SCORES_4H_ONLY))
    assert "0.60°F" in why_one and "0.30°F" not in why_one


def test_a_missing_score_leaves_the_score_out() -> None:
    _h, why = v4_shadow.describe_node_choice(_choice("per_room_no_worse", scores={"mae": {}}))
    assert why and "°F" not in why


@pytest.mark.parametrize(
    ("reason", "extra"),
    [
        ("stitched_not_usable", {}),
        ("too_few_shared_anchors", {"scores": {"n_anchors": 4}}),
        ("too_few_shared_anchors", {"scores": {}}),
        ("per_room_not_usable", {"per_room_fit_reason": "fit diverged"}),
        ("primary_not_ready", {"primary_days": 1.5}),
        ("no_primary_entity", {}),
    ],
)
def test_every_node_choice_reason_has_words(reason: str, extra: dict) -> None:
    head, why = v4_shadow.describe_node_choice(_choice(reason, **extra))
    assert head and why


def test_too_few_anchors_names_the_count_and_unknown_reason_has_no_words() -> None:
    assert "4" in v4_shadow.describe_node_choice(_choice("too_few_shared_anchors", scores={"n_anchors": 4}))[1]
    assert v4_shadow.describe_node_choice_msgs(_choice("a_new_reason"))[1] is None


# ------------------------------------------------------------------------------------------------ fits and cycles
def test_run_fit_with_an_unreadable_forecast_archive_fits_from_the_chart_log(caplog) -> None:
    def bad(start, end):
        raise OSError("archive unreadable")

    with caplog.at_level(logging.WARNING):
        model, seconds, _line = v4_shadow.run_fit(
            chart_entries=[], snapshots_fn=bad, tz=TZ, now=NOW, primary="_ca", sensors=["_ca"]
        )
    assert "forecast archive unreadable" in caplog.text
    assert model["confidence"] == "none" and seconds >= 0.0


def test_model_climate_entity_of_a_non_model_is_unknown() -> None:
    assert v4_shadow.model_climate_entity(None) is None
    assert v4_shadow.model_climate_entity(["not", "a", "model"]) is None  # type: ignore[arg-type]


def test_unreadable_stored_models_raise_for_the_caller_to_keep_the_old_one() -> None:
    with pytest.raises(ValueError, match="not readable"):
        v4_shadow.run_display_fit(
            chart_entries=[], tz=TZ, now=NOW, primary="_ca", sensors=["_ca"], model_dict=BAD_MODEL
        )
    with pytest.raises(ValueError, match="not readable"):
        v4_shadow.run_cycle(
            model_dict=BAD_MODEL, now=NOW, tz=TZ, readings={}, mass_f=None, mass_steps=0, sample_fields={}, inputs=[]
        )
    with pytest.raises(ValueError, match="not readable"):
        v4_shadow.corrected_forecast(model_dict=BAD_MODEL, times=[NOW], raw_f=[50.0], tz=TZ)
    assert v4_shadow.corrected_forecast(model_dict=BAD_MODEL, times=[], raw_f=[], tz=TZ) == []


def test_run_display_fit_without_a_training_bound_keeps_the_model(monkeypatch) -> None:
    md = p4.to_dict(make_model())
    out, _s, _line = v4_shadow.run_display_fit(
        chart_entries=[], tz=TZ, now=NOW, primary="_ca", sensors=["_ca"], model_dict=md, train_days=None
    )
    assert out["house"] == md["house"] and out["primary"] == md["primary"]


# ------------------------------------------------------------------------------------------------ chart extras
def _inputs(n: int = 4):
    return [
        SimpleNamespace(ts=NOW + timedelta(minutes=15 * k), outdoor_f=50.0, heat_sp_f=None, cool_sp_f=None)
        for k in range(1, n + 1)
    ]


def test_chart_extras_without_inputs_or_with_an_unreadable_model_draw_nothing() -> None:
    kw = {"now": NOW, "tz": TZ, "readings": {}, "mass_f": None}
    assert v4_shadow.chart_extras(model_dict=BAD_MODEL, inputs=[], **kw) == ([], {})
    # corrected line fails (unreadable model raises) and the baseline has no model: both empty, no exception
    assert v4_shadow.chart_extras(model_dict=BAD_MODEL, inputs=_inputs(), **kw) == ([], {})


def test_chart_extras_baseline_failure_keeps_the_corrected_line(monkeypatch) -> None:
    md = p4.to_dict(make_model())
    monkeypatch.setattr(v4_shadow, "corrected_forecast", lambda **k: [(NOW, 51.0)])
    monkeypatch.setattr(p4, "predict", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    corrected, baseline = v4_shadow.chart_extras(
        model_dict=md, now=NOW, tz=TZ, readings={}, mass_f=None, inputs=_inputs()
    )
    assert corrected == [(NOW, 51.0)] and baseline == {}


# ------------------------------------------------------------------------------------------------ small helpers
def test_rows_in_window_reads_a_naive_timestamp_in_the_zone_of_now() -> None:
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert v4_shadow.rows_in_window([{"ts": naive}, {"ts": "junk"}, "x"], NOW, 1.0) == [{"ts": naive}]


def test_run_state_stops_at_a_long_gap_and_an_unreadable_newest_row() -> None:
    rows = [
        {"ts": (NOW - timedelta(hours=5)).isoformat(), "hvac": "heating"},
        {"ts": (NOW - timedelta(minutes=30)).isoformat(), "hvac": "heating"},
    ]
    action, minutes = v4_shadow.run_state_from_rows(rows, NOW)
    assert action == "heating" and minutes == pytest.approx(30.0)  # the 4.5 h gap ends the run
    assert v4_shadow.run_state_from_rows([{"ts": "junk", "hvac": "cooling"}], NOW) == ("cooling", 0.0)


def test_live_loop_seed_ignores_a_zero_offset_and_a_naive_raise_time() -> None:
    zero = SimpleNamespace(context=REMOTE_CONTEXT_SLEEP, offset_f=0.0)
    assert v4_shadow.live_loop_seed(zero, None, NOW) == (0.0, None)
    held = SimpleNamespace(context=REMOTE_CONTEXT_SLEEP, offset_f=1.5)
    assert v4_shadow.live_loop_seed(held, datetime(2026, 11, 6, 18, 0), NOW) == (1.5, None)


def test_override_hold_with_a_bad_end_or_an_unknown_mode_is_none() -> None:
    eng = SimpleNamespace(_manual_override_active=True, _grace_end_time="not a time")
    assert v4_shadow.override_hold(eng, mode="heat", setpoint_f=70.0) is None
    eng._grace_end_time = (NOW + timedelta(hours=1)).isoformat()
    assert v4_shadow.override_hold(eng, mode="heat_cool", setpoint_f=70.0) is None
    assert v4_shadow.override_hold(eng, mode="off", setpoint_f=None) is not None


def test_night_window_for_a_bedtime_after_midnight() -> None:
    sleep, wake = dtime(1, 0), dtime(9, 0)
    late = datetime(2026, 11, 7, 2, 0, tzinfo=TZ)  # in the night already: start now, end at wake today
    assert v4_shadow.night_window(late, sleep, wake) == (late, datetime(2026, 11, 7, 9, 0, tzinfo=TZ))


def test_at_time_outside_the_series_is_none() -> None:
    series = [(NOW, 70.0), (NOW + timedelta(hours=1), 71.0)]
    assert v4_shadow._at_time(series, NOW + timedelta(hours=3)) is None
    assert v4_shadow._at_time(series, NOW + timedelta(minutes=30)) == pytest.approx(70.5)


def test_mass_at_bedtime_with_a_path_that_ends_before_bedtime_uses_every_point() -> None:
    model = make_model()
    path = [(NOW + timedelta(minutes=15 * k), 70.0) for k in range(1, 3)]
    start = NOW + timedelta(hours=2)
    got = v4_shadow._mass_at_bedtime(p4, model, 68.0, path, NOW, start)
    ref = 68.0
    for _ts, v in path:
        ref = p4.step_mass(model, mass_f=ref, readings={model.primary: v})
    assert got == pytest.approx(ref)


def test_utc_now_is_accepted_by_night_window() -> None:
    assert v4_shadow.night_window(datetime(2026, 11, 6, 12, 0, tzinfo=UTC), dtime(22, 0), dtime(6, 0)) is None
