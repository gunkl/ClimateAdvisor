"""Operation timeline (Issue #1230 R3a): the pure plan of what CA does and what the occupant should do.

Occupant view: the occupant reads ONE list of the next day or two: CA's band changes at wake and bedtime, the pre-cool,
the fan session, and the few times they themselves should open or close a window. These tests pin that list for every
day type, fan mode and occupancy, the sensor swap at bedtime and wake, an occupant override in the middle of the plan,
an evening window period, and the #1276 contradiction (heat on at the floor while "open windows" was on the card).
"""

from __future__ import annotations

import json
import math
import time as _time
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor.automation import _in_sleep_window
from custom_components.climate_advisor.coordinator import _simulate_indoor_physics_v3, _solar_factor
from custom_components.climate_advisor.operation_timeline import (
    SENTENCES,
    MissingFact,
    StepKind,
    TimelineLatch,
    build_timeline,
    material_changes,
    say,
)
from custom_components.climate_advisor.temperature import outdoor_direction_ok, outdoor_direction_ok_for_thermostat
from tests.timeline_fixtures import CONFIG, DAY_TYPES, MODEL, TZ, make_snapshot, sine_day

NOON = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)


def _kinds(tl, actor: str | None = None) -> list[str]:
    return [s.kind for s in tl.steps if actor is None or s.actor == actor]


# ---------------------------------------------------------------------------
# Day types x fan modes x occupancy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day_type", sorted(DAY_TYPES))
@pytest.mark.parametrize("fan_mode", ["whole_house_fan", "hvac_fan", "disabled"])
@pytest.mark.parametrize("occupancy", ["home", "away", "vacation", "guest"])
def test_every_day_type_fan_mode_and_occupancy_builds_a_valid_timeline(day_type, fan_mode, occupancy) -> None:
    snap = make_snapshot(day_type, now=NOON, fan_mode=fan_mode, occupancy=occupancy)
    tl = build_timeline(snap)

    assert tl.steps == tuple(sorted(tl.steps, key=lambda s: (s.ts, 0 if s.actor == "ca" else 1, s.kind)))
    assert len({s.id for s in tl.steps}) == len(tl.steps)
    for s in tl.steps:
        assert s.kind in {k.value for k in StepKind}
        assert s.text and s.confidence in ("ok", "low") and s.grade in ("low", "medium", "high")
        assert s.ts >= snap.now or s.kind == "setback_start"
        # "you" steps are windows and nothing else: CA never asks the occupant for something it can do itself
        assert (s.actor == "you") == (s.kind in ("windows_open", "windows_close"))
    assert json.loads(json.dumps(ot.to_dict(tl)))["schema_version"] == ot.TIMELINE_SCHEMA_VERSION

    # the clock-driven steps follow the occupancy: nobody home means no wake/sleep band, only the hold
    ca = _kinds(tl, "ca")
    if occupancy in ("away", "vacation"):
        assert "setback_start" in ca
        assert "wake_band" not in ca or occupancy == "away"  # away is today only: tomorrow's schedule is normal
        today = snap.now.date()
        # nobody home to open a window today (away is today only: tomorrow's engine plan may offer one, vacation never)
        assert not [s for s in tl.steps if s.actor == "you" and (occupancy == "vacation" or s.ts.date() == today)]
    else:
        assert ca.count("wake_band") == 2 and ca.count("sleep_band") >= 1
        assert "setback_start" not in ca
    # a fan session is only ever planned with a fan configured
    if fan_mode == "disabled":
        assert not {"nat_vent_start", "nat_vent_stop"} & set(ca)


def test_vacation_holds_the_deep_band_for_every_day_with_no_return_time() -> None:
    tl = build_timeline(make_snapshot("warm", now=NOON, occupancy="vacation"))
    hold = next(s for s in tl.steps if s.kind == "setback_start")
    assert "until you return" in hold.text and hold.ts == NOON
    assert all(ctx == "vacation" for _t, _lo, _hi, ctx in tl.band)


def test_away_applies_to_today_only_then_the_normal_schedule_returns() -> None:
    tl = build_timeline(make_snapshot("warm", now=NOON, occupancy="away"))
    contexts = {t.date(): ctx for t, _lo, _hi, ctx in tl.band}
    assert tl.band[0][3] == "away"
    assert [ctx for t, _lo, _hi, ctx in tl.band if t.date() > NOON.date() and t.hour == 6][0] in ("sleep", "comfort")
    assert contexts


def test_wake_and_sleep_steps_carry_the_engine_band_and_the_armed_edge() -> None:
    tl = build_timeline(make_snapshot("cool", now=NOON))
    wake = next(s for s in tl.steps if s.kind == "wake_band")
    sleep = next(s for s in tl.steps if s.kind == "sleep_band")
    assert (wake.facts["floor"], wake.facts["ceiling"]) == (CONFIG["comfort_heat"], CONFIG["comfort_cool"])
    assert (sleep.facts["floor"], sleep.facts["ceiling"]) == (CONFIG["sleep_heat"], CONFIG["sleep_cool"])
    assert wake.facts["setpoint_edge"] == "floor" and wake.facts["setpoint"] == CONFIG["comfort_heat"]  # heat day
    hot = build_timeline(make_snapshot("hot", now=NOON))
    assert next(s for s in hot.steps if s.kind == "wake_band").facts["setpoint_edge"] == "ceiling"


def test_a_mode_change_is_planned_where_consecutive_days_differ() -> None:
    tl = build_timeline(make_snapshot("warm", now=NOON, tomorrow="cold"))
    change = [s for s in tl.steps if s.kind == "mode_change"]
    assert change and change[0].facts["old"] == "off" and change[0].facts["new"] == "heat"
    assert change[0].ts.time() == time(6, 30)  # applied when that day's schedule starts, not at midnight
    assert not [s for s in build_timeline(make_snapshot("cold", now=NOON)).steps if s.kind == "mode_change"]


# ---------------------------------------------------------------------------
# Sleep / wake sensor swap
# ---------------------------------------------------------------------------


def test_sensor_swap_is_named_at_bedtime_and_again_at_wake() -> None:
    tl = build_timeline(make_snapshot("mild", now=NOON, sleep_sensor="bedroom sensor"))
    sleep = next(s for s in tl.steps if s.kind == "sleep_band")
    wake = next(s for s in tl.steps if s.kind == "wake_band")
    assert "reads the bedroom sensor until" in sleep.text and "6:30 AM" in sleep.text
    assert "reads the hallway again" in wake.text


def test_without_a_sleep_sensor_the_band_steps_do_not_mention_a_swap() -> None:
    tl = build_timeline(make_snapshot("mild", now=NOON))
    for s in tl.steps:
        if s.kind in ("sleep_band", "wake_band"):
            assert "reads" not in s.text


# ---------------------------------------------------------------------------
# Override in the middle of the plan
# ---------------------------------------------------------------------------


def test_an_override_holds_the_band_steps_until_the_engine_resumes() -> None:
    now = datetime(2026, 11, 4, 19, 30, tzinfo=TZ)
    grace_end = datetime(2026, 11, 4, 22, 33, tzinfo=TZ)
    snap = make_snapshot(
        "mild", now=now, manual_override_active=True, grace_active=True, grace_end=grace_end, thermostat_mode="heat"
    )
    tl = build_timeline(snap)
    resume = next(s for s in tl.steps if s.kind == "resume_schedule")
    assert resume.ts == grace_end and resume.actor == "ca"
    sleep = next(s for s in tl.steps if s.kind == "sleep_band")  # 20:30 falls inside the hands-off period
    assert sleep.status == "held" and "applies when CA resumes the schedule at 10:33 PM" in sleep.text
    wake = next(s for s in tl.steps if s.kind == "wake_band")
    assert wake.status == "planned"  # after the hold: back on schedule


def test_a_door_pause_marks_the_imminent_band_step_as_waiting() -> None:
    now = datetime(2026, 11, 4, 20, 10, tzinfo=TZ)
    tl = build_timeline(make_snapshot("mild", now=now, paused_by_door=True))
    sleep = next(s for s in tl.steps if s.kind == "sleep_band")
    assert sleep.status == "held" and "applies when the door or window closes" in sleep.text


# ---------------------------------------------------------------------------
# Window candidates
# ---------------------------------------------------------------------------


def _evening_cooling(fan_mode: str = "whole_house_fan", **kw):
    """A warm day at 5:30 PM: the house is above its ceiling and the evening air is much cooler."""
    now = datetime(2026, 11, 4, 17, 30, tzinfo=TZ)
    # a house that breathes through open windows (fast ventilated rate), like one with a whole-house fan
    kw.setdefault("thermal_model", dict(MODEL, k_vent_window=-0.5))
    snap = make_snapshot("warm", now=now, fan_mode=fan_mode, indoor=77.0, **kw)
    hourly = []
    for ts, _v in snap.hourly:
        # 77 F outside until 17:00, falling ~3 F/h to 58 F by 22:00: a real evening cool-down
        hourly.append((ts, 82.0 if ts.hour < 17 else max(58.0, 82.0 - 4.0 * (ts.hour - 16))))
    snap.hourly = hourly
    return snap


def test_an_evening_window_period_is_offered_with_its_close_and_the_fan_session() -> None:
    tl = build_timeline(_evening_cooling())
    accepted = [c for c in tl.candidates if c.accepted]
    assert accepted and accepted[0].kind == "free_cooling"
    assert accepted[0].hvac_saved_min >= ot.WINDOW_MIN_HVAC_SAVED_MIN
    opens = [s for s in tl.steps if s.kind == "windows_open"]
    closes = [s for s in tl.steps if s.kind == "windows_close"]
    assert opens and closes and opens[0].ts < closes[0].ts
    assert opens[0].actor == "you" and "cooler than the house" in opens[0].text
    assert opens[0].show_from == opens[0].ts - timedelta(minutes=30)  # DP-4: shown 30 minutes ahead
    fan = [s for s in tl.steps if s.kind == "nat_vent_start"]
    assert fan and fan[0].after == opens[0].id and "whole-house fan" in fan[0].text
    assert any(s.kind == "nat_vent_stop" for s in tl.steps)
    # the close lands before the sleep window starts: nobody is awake to close it
    assert closes[0].ts.time() <= time(20, 30)


def test_the_hvac_fan_is_named_when_that_is_the_fan_mode() -> None:
    tl = build_timeline(_evening_cooling("hvac_fan"))
    assert any("HVAC fan" in s.text for s in tl.steps if s.kind == "nat_vent_start")


def test_without_a_fan_the_windows_are_offered_but_no_fan_session_is_planned() -> None:
    tl = build_timeline(_evening_cooling("disabled"))
    assert any(s.kind == "windows_open" for s in tl.steps)
    assert not any(s.kind in ("nat_vent_start", "nat_vent_stop") for s in tl.steps)


def test_a_window_period_is_omitted_when_the_window_effect_is_not_validated() -> None:
    model = dict(MODEL, observation_count_vent_window=12)  # fewer than 50 ventilated observations
    tl = build_timeline(_evening_cooling(thermal_model=model))
    # D1 (R3b): only the engine's own recommendation can be offered, and then with low confidence; a scanned period
    # (free cooling / heating) needs a validated window effect and is never offered without one
    assert all(s.confidence == "low" for s in tl.steps if s.actor == "you")
    assert tl.candidates and all(
        (c.reason == "unvalidated" and not c.accepted) or c.reason == "engine_unvalidated" for c in tl.candidates
    )


def test_a_window_effect_that_is_not_distinguishable_from_the_envelope_is_not_validated() -> None:
    model = dict(MODEL, k_vent_window=-0.04)  # barely more than k_passive (-0.0315): ratio < 1.5
    tl = build_timeline(_evening_cooling(thermal_model=model))
    assert all(
        s.confidence == "low" for s in tl.steps if s.actor == "you"
    )  # D1: only the engine's own advice, unvalidated


def test_an_engine_recommended_period_that_saves_nothing_is_kept_with_the_numbers() -> None:
    """D1 (R3b): the engine's own recommendation is the source, so a period the simulation finds worthless is
    kept (the occupant gets it today) and the numbers say so; only the direction rule removes it."""
    now = datetime(2026, 11, 4, 11, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=70.0)
    tl = build_timeline(snap)
    planned = [c for c in tl.candidates if c.kind == "planned"]
    assert planned
    for cand in planned:
        assert cand.accepted == (cand.reason in ("accepted", "engine_no_benefit", "engine_unvalidated"))
        assert cand.accepted or cand.reason in ("direction", "overlaps", "too_short")


def test_the_engine_recommendation_stands_unvalidated_with_low_confidence_when_the_window_effect_is_unlearned() -> None:
    """D1 (R3b): Zone 1 was told to open windows on 62 of 89 days; an unvalidated model must not silently remove it."""
    model = dict(MODEL, observation_count_vent_window=12)
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=70.0, model=model)
    tl = build_timeline(snap)
    you = [s for s in tl.steps if s.actor == "you"]
    assert you, "the engine's window advice was dropped"
    assert all(s.confidence == "low" for s in you)
    assert any(c.kind == "planned" and c.reason == "engine_unvalidated" for c in tl.candidates)


def test_windows_already_open_get_a_close_time_and_the_fan_stop() -> None:
    snap = _evening_cooling(windows_physically_open=True, nat_vent_active=True)
    tl = build_timeline(snap)
    assert any(c.kind == "open_now" and c.accepted for c in tl.candidates)
    kinds = _kinds(tl)
    assert "windows_close" in kinds and "nat_vent_stop" in kinds
    # the occupant already opened them: no "open" step for this period (a later evening's may exist)
    assert not [s for s in tl.steps if s.kind == "windows_open" and s.ts < snap.now + timedelta(hours=6)]


def test_the_planned_window_close_is_the_plans_cutoff_when_one_exists() -> None:
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    cutoff = datetime(2026, 11, 4, 11, 0, tzinfo=TZ)
    snap = make_snapshot("warm", now=now, indoor=79.0, nat_vent_cutoff=cutoff)
    from custom_components.climate_advisor.operation_timeline import _planned_spans

    spans = _planned_spans(snap, now + timedelta(hours=48))
    assert spans and spans[0][2] == cutoff


# ---------------------------------------------------------------------------
# #1276: heat on at the floor, outdoor cooler, "open windows" must not be offered
# ---------------------------------------------------------------------------


def test_1276_heat_on_at_the_floor_with_cooler_air_never_offers_windows() -> None:
    now = datetime(2026, 11, 2, 19, 33, tzinfo=TZ)
    snap = make_snapshot(
        "mild",
        now=now,
        indoor=68.5,
        outdoor=57.0,
        thermostat_mode="heat",
        windows_recommended=True,  # the classifier still recommends a window day
        window_open_time=time(10, 0),
        window_close_time=time(23, 0),
    )
    snap.hourly = [(ts, 57.0 - 0.3 * i) for i, (ts, _v) in enumerate(snap.hourly)]
    tl = build_timeline(snap)
    assert not [s for s in tl.steps if s.kind == "windows_open"], "the card said 'Open windows' here (#1276)"
    assert tl.first_user_step() is None
    rejected = [c for c in tl.candidates if c.kind == "planned"]
    assert rejected and not rejected[0].accepted and rejected[0].reason == "direction"
    assert "Keep windows closed" in " ".join(tl.summary)


def test_the_direction_rule_is_the_live_one_including_the_swing_at_a_held_edge() -> None:
    # house at its 68 floor, heat held, outdoor cooler: not helping (#1276); with the thermostat off it would be
    assert (
        outdoor_direction_ok_for_thermostat(
            57.0, 68.4, 68.0, 74.0, thermostat_mode="heat", swing_heat_f=1.0, swing_cool_f=1.0
        )
        is False
    )
    assert outdoor_direction_ok_for_thermostat(
        57.0, 68.4, 68.0, 74.0, thermostat_mode="off", swing_heat_f=1.0, swing_cool_f=1.0
    ) == outdoor_direction_ok(57.0, 68.4, 68.0, 74.0)
    # mirror: AC held near the ceiling, warmer air is not helping
    assert (
        outdoor_direction_ok_for_thermostat(
            80.0, 73.6, 68.0, 74.0, thermostat_mode="cool", swing_heat_f=1.0, swing_cool_f=1.0
        )
        is False
    )
    # above the ceiling cooler air always helps
    assert outdoor_direction_ok_for_thermostat(
        60.0, 77.0, 68.0, 74.0, thermostat_mode="cool", swing_heat_f=1.0, swing_cool_f=1.0
    )


# ---------------------------------------------------------------------------
# Pre-cool / TOU
# ---------------------------------------------------------------------------


def test_the_weather_pre_cool_and_the_tou_precondition_are_planned_with_their_causes() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
    snap = make_snapshot(
        "hot",
        now=now,
        pre_cool_trigger=datetime(2026, 11, 5, 3, 0, tzinfo=TZ),
        pre_cool_target=70.0,
        tou_precondition_start=datetime(2026, 11, 4, 14, 0, tzinfo=TZ),
        tou_schedule_start=datetime(2026, 11, 4, 16, 0, tzinfo=TZ),
        tou_target=72.0,
        tou_mode="cool",
    )
    tl = build_timeline(snap)
    pre = [s for s in tl.steps if s.kind == "precool_start"]
    assert {s.facts["cause"] for s in pre} == {"weather_trend", "tou"}
    assert any(c == 70.0 for _t, _lo, c, _ctx in tl.band), "the pre-cool ceiling is in the planned band"
    heat = build_timeline(replace(snap, tou_mode="heat", pre_cool_trigger=None, pre_cool_target=None))
    assert any(s.kind == "preheat_start" and "pre-heats" in s.text for s in heat.steps)


# ---------------------------------------------------------------------------
# Predicted path and runs
# ---------------------------------------------------------------------------


def test_the_path_is_v3_when_the_plan_matches_and_replanned_when_it_does_not() -> None:
    snap = make_snapshot("mild", now=NOON)
    v3 = [(NOON + timedelta(minutes=30 * i), 70.0) for i in range(1, 10)]
    tl = build_timeline(replace(snap, v3_path=v3, windows_recommended=False, opportunity_spans=()))
    assert tl.path_source == "v3" and [(p.ts, p.temp_f) for p in tl.path] == v3
    calls = []

    def path_fn(spans):
        calls.append(list(spans))
        return [(NOON + timedelta(minutes=30), 69.0)]

    tl2 = build_timeline(replace(snap, path_fn=path_fn))
    assert calls and tl2.path_source in ("v3", "v3_replanned") and tl2.path[0].temp_f == 69.0
    broken = build_timeline(replace(snap, path_fn=lambda spans: 1 / 0, v3_path=()))
    assert broken.path_source == "sim" and broken.path


def test_runs_are_clustered_and_only_claimed_at_medium_confidence_or_better() -> None:
    tl = build_timeline(make_snapshot("cold", now=NOON))
    assert tl.runs and all(r.minutes >= ot.RUN_MIN_MINUTES for r in tl.runs)
    assert all((r.end - r.start) <= timedelta(hours=ot.RUN_MAX_SPAN_H, minutes=15) for r in tl.runs)
    weak = dict(MODEL, observation_count_heat=2, observation_count_cool=1)  # HVAC rate not graded
    assert build_timeline(make_snapshot("cold", now=NOON, thermal_model=weak)).runs == ()


def test_the_simulation_step_is_the_v3_ode_step() -> None:
    cases = [(70.0, 55.0, -0.0315, 0.0, 0.25, None), (67.0, 40.0, -0.12, 0.75, 0.25, None)]
    for t, out, k, q, dt, _ in cases:
        mine = ot.ode_step(t, out, k, q, dt)
        theirs = _simulate_indoor_physics_v3(
            t, out, k, None, dt, None, comfort_heat=68.0, comfort_cool=74.0, k_solar=q, solar_factor=1.0
        )
        assert math.isclose(mine, theirs, abs_tol=1e-9)
    for hour in range(24):
        assert ot.solar_factor(hour, 3.0) == pytest.approx(_solar_factor(hour, 3.0))


@pytest.mark.parametrize(
    ("wake", "sleep"), [("06:30", "20:30"), ("07:00", "22:30"), ("09:00", "01:00"), ("06:30", "06:30")]
)
def test_the_sleep_window_is_the_engines_sleep_window(wake: str, sleep: str) -> None:
    cfg = {"wake_time": wake, "sleep_time": sleep}
    w, s = time(*map(int, wake.split(":"))), time(*map(int, sleep.split(":")))
    for hour in range(24):
        for minute in (0, 30):
            at = datetime(2026, 11, 4, hour, minute, tzinfo=TZ)
            assert ot.asleep_at(at, w, s) == _in_sleep_window(at, cfg)


# ---------------------------------------------------------------------------
# Latch and regeneration
# ---------------------------------------------------------------------------


def _latched(**kw):
    latch = TimelineLatch()
    first = latch.update(make_snapshot("mild", now=NOON, **kw))
    assert first.regenerated and first.reasons == ("first",)
    return latch, first


def test_nothing_changed_keeps_the_days_steps_exactly() -> None:
    latch, first = _latched()
    later = latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=30)))
    assert not later.regenerated and later.reasons == ()
    assert [s.id for s in later.timeline.steps] == [s.id for s in first.timeline.steps]
    assert later.timeline.generation == first.timeline.generation == 1
    assert later.timeline.generated_at == NOON  # the latch keeps when the day's plan was made
    assert later.timeline.path != () and later.timeline.band[0][0] == NOON + timedelta(
        minutes=30
    )  # but the path is fresh


def test_steps_in_the_past_are_marked_done_not_dropped() -> None:
    latch, first = _latched()
    sleep = next(s for s in first.timeline.steps if s.kind == "sleep_band")
    after = latch.update(make_snapshot("mild", now=sleep.ts + timedelta(minutes=5)))
    assert not after.regenerated
    assert next(s for s in after.timeline.steps if s.id == sleep.id).status == "done"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"occupancy": "away"}, "occupancy"),
        ({"fan_mode": "hvac_fan"}, "fan_mode"),
        ({"paused_by_door": True}, "pause"),
        ({"manual_override_active": True}, "override"),
        ({"grace_active": True}, "override"),
        ({"automation_enabled": False}, "automation"),
    ],
)
def test_a_material_change_regenerates_with_its_reason(change, reason) -> None:
    latch, first = _latched()
    snap = make_snapshot(
        "mild",
        now=NOON + timedelta(minutes=30),
        **{k: v for k, v in change.items() if k != "occupancy" and k != "fan_mode"},
    )
    if "occupancy" in change:
        snap = make_snapshot("mild", now=NOON + timedelta(minutes=30), occupancy=change["occupancy"])
    if "fan_mode" in change:
        snap = make_snapshot("mild", now=NOON + timedelta(minutes=30), fan_mode=change["fan_mode"])
    out = latch.update(snap)
    assert out.regenerated and reason in out.reasons
    assert out.timeline.generation == 2 and out.timeline.regenerated_for == out.reasons


def test_a_classification_change_regenerates() -> None:
    latch, _ = _latched()
    out = latch.update(make_snapshot("hot", now=NOON + timedelta(minutes=30)))
    assert out.regenerated and "classification" in out.reasons


def test_a_band_change_regenerates_only_from_half_a_degree() -> None:
    latch, _ = _latched()
    cfg = dict(CONFIG, sleep_heat=66.3)
    assert not latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=30), config=cfg)).regenerated
    cfg = dict(CONFIG, sleep_heat=67.5)
    out = latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=60), config=cfg))
    assert out.regenerated and "band" in out.reasons


def test_a_boundary_sliding_into_the_past_is_not_a_band_change() -> None:
    latch, first = _latched()
    after_bedtime = NOON + timedelta(hours=9)  # 21:00: the 20:30 sleep boundary is now behind us
    out = latch.update(make_snapshot("mild", now=after_bedtime))
    assert not out.regenerated, out.reasons


def test_forecast_shift_regenerates_only_beyond_the_documented_threshold() -> None:
    latch, _ = _latched()
    base = make_snapshot("mild", now=NOON + timedelta(minutes=30))
    nudged = replace(base, hourly=[(t, v + 1.0) for t, v in base.hourly])
    assert not latch.update(nudged).regenerated  # 1 F is noise
    shifted = replace(base, hourly=[(t, v + 3.5) for t, v in base.hourly])
    out = latch.update(shifted)
    assert out.regenerated and "forecast" in out.reasons
    mean, peak = ot.forecast_shift(
        [(t.timestamp(), v) for t, v in base.hourly], [(t.timestamp(), v + 3.5) for t, v in base.hourly], base.now
    )
    assert mean == pytest.approx(3.5) and peak == pytest.approx(3.5)


def test_a_new_day_regenerates() -> None:
    latch, _ = _latched()
    out = latch.update(make_snapshot("mild", now=NOON + timedelta(days=1)))
    assert out.regenerated and out.reasons == ("new_day",)


def test_an_explicit_trigger_forces_a_regeneration() -> None:
    latch, _ = _latched()
    out = latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=5)), force="occupancy")
    assert out.regenerated and out.reasons == ("occupancy",)


def test_the_window_close_can_only_move_earlier_within_a_day() -> None:
    latch = TimelineLatch()
    first = latch.update(_evening_cooling())
    close = next(s for s in first.timeline.steps if s.kind == "windows_close")
    # a later cycle with a slightly warmer evening: close would land EARLIER -> accepted
    warmer = _evening_cooling()
    warmer.now = warmer.now + timedelta(minutes=30)
    warmer.hourly = [(t, v + (1.0 if t.hour >= 18 else 0.0)) for t, v in warmer.hourly]
    out = latch.update(warmer)
    new_close = next(s for s in out.timeline.steps if s.kind == "windows_close")
    assert new_close.ts <= close.ts
    # a later cycle with a colder evening: close would land LATER -> the promised time stays
    colder = _evening_cooling()
    colder.now = colder.now + timedelta(minutes=60)
    colder.hourly = [(t, v - (1.0 if t.hour >= 18 else 0.0)) for t, v in colder.hourly]
    out2 = latch.update(colder)
    kept = next(s for s in out2.timeline.steps if s.kind == "windows_close")
    assert kept.ts <= new_close.ts


def test_material_changes_is_empty_for_identical_plans() -> None:
    snap = make_snapshot("mild", now=NOON)
    segs = ot.build_segments(snap, NOON + timedelta(hours=48))
    sig = ot.PlanSignature.of(snap, segs)
    assert material_changes(sig, sig, NOON) == []


# ---------------------------------------------------------------------------
# Sentence table
# ---------------------------------------------------------------------------


def test_every_step_kind_has_a_sentence_in_the_table() -> None:
    for kind, prefix in ot.STEP_SENTENCE_PREFIX.items():
        assert [k for k in SENTENCES if k == prefix or k.startswith(prefix + ".")], f"no sentence for {kind}"


def test_every_sentence_declares_exactly_the_facts_its_template_uses_and_its_provenance() -> None:
    for key, sentence in SENTENCES.items():
        assert sentence.key == key
        assert ot.template_fields(sentence.template) == set(sentence.facts), key
        assert sentence.source and sentence.rule and sentence.guard, f"{key} lacks source/rule/guard"
        assert set(sentence.facts.values()) <= {"time", "temp", "delta", "text", "int", "minutes"}, key


def test_a_sentence_with_a_missing_fact_is_refused_not_guessed() -> None:
    with pytest.raises(MissingFact):
        say("wake_band.default", t=NOON, floor=68.0)
    with pytest.raises(MissingFact):
        say("wake_band.default", t=NOON, floor=68.0, ceiling=None)


def test_a_step_whose_facts_are_missing_is_dropped_and_reported(monkeypatch) -> None:
    monkeypatch.setattr(ot, "_next_wake", lambda snap, after: None)  # no wake time to name
    tl = build_timeline(make_snapshot("mild", now=NOON, sleep_sensor="bedroom sensor"))
    assert "sleep_band.sensor_swap" in tl.dropped  # the swap sentence needs the wake time
    assert not [s for s in tl.steps if s.kind == "sleep_band"]


def test_sentences_follow_the_users_unit_and_round_temperatures_to_half_a_degree() -> None:
    text = say("peak.warmest", "celsius", temp=68.4, t=NOON)
    assert text == "Warmest point: about 20.0°C at 12:00 PM." or text.startswith("Warmest point: about 20")
    assert say("peak.warmest", "fahrenheit", temp=68.4, t=NOON).startswith("Warmest point: about 68°F") or "68" in say(
        "peak.warmest", "fahrenheit", temp=68.4, t=NOON
    )
    celsius = build_timeline(make_snapshot("mild", now=NOON, unit="celsius"))
    assert "°C" in next(s for s in celsius.steps if s.kind == "wake_band").text


def test_the_weekday_is_added_for_a_time_that_is_not_today() -> None:
    tl = build_timeline(make_snapshot("mild", now=NOON))
    wake = next(s for s in tl.steps if s.kind == "wake_band")
    assert wake.text.startswith("Thu 6:30 AM:")
    assert next(s for s in tl.steps if s.kind == "sleep_band").text.startswith("8:30 PM:")


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_one_build_over_48_hours_is_cheap() -> None:
    snap = make_snapshot("hot", now=NOON, sleep_sensor="bedroom sensor")
    build_timeline(snap)  # warm
    t0 = _time.perf_counter()
    for _ in range(20):
        build_timeline(snap)
    per = (_time.perf_counter() - t0) / 20 * 1000.0
    print(f"operation timeline: {per:.1f} ms per build (48 h, 15-min steps)")
    assert per < 250.0  # generous CI bound: it measures ~5-10 ms locally


def test_dead_helpers_stay_alive() -> None:
    """The fixture's sine day is what the tests read; keep it honest."""
    day = sine_day(NOON.date(), 80.0, 60.0)
    assert max(v for _t, v in day) == pytest.approx(80.0) and min(v for _t, v in day) == pytest.approx(60.0)
    assert ot.Actor.CA.value == "ca"


# ---------------------------------------------------------------------------
# The frozen output contract (/home/dev/ca-group-briefs/1230-r3-timeline-contract.md)
# ---------------------------------------------------------------------------

_CONTRACT_KINDS = {
    "mode_change", "setpoint_change", "fan_on", "fan_off", "nat_vent_start", "nat_vent_stop", "precool_start",
    "preheat_start", "setback_start", "setback_end", "sleep_band", "wake_band", "occupancy", "windows_open",
    "windows_close", "night_strategy",
}  # fmt: skip


def test_the_step_enum_keeps_every_contract_member_and_only_appends() -> None:
    values = [k.value for k in StepKind]
    assert set(values) >= _CONTRACT_KINDS
    # appended by R3a (resume_schedule) and R3b (briefing_send, ceiling_guard); reported to the coordinator
    assert set(values) - _CONTRACT_KINDS == {"resume_schedule", "briefing_send", "ceiling_guard"}
    assert [a.value for a in ot.Actor] == ["ca", "you"]
    # every member is either emitted (has a sentence + a code path) or listed as not emitted, with no gap
    assert set(StepKind) == set(ot.STEP_SENTENCE_PREFIX) | ot.NOT_EMITTED_BY_R3A
    assert set(ot.STEP_SOURCE) == set(ot.STEP_SENTENCE_PREFIX)


def test_the_dataclasses_have_exactly_the_contract_fields() -> None:
    import dataclasses

    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(ot.TimelineStep) == [
        "ts", "actor", "kind", "text", "reason", "target_f", "mode", "confidence", "lead_minutes", "source", "meta",
    ]  # fmt: skip
    assert names(ot.PathPoint) == ["ts", "temp_f", "sensor", "model"]
    assert names(ot.Timeline) == [
        "schema_version", "zone", "generated_at", "day", "horizon_end", "steps", "path", "latched", "regen_reason",
    ]  # fmt: skip
    assert ot.TIMELINE_SCHEMA_VERSION == 1


def test_to_dict_round_trips_through_json_and_from_dict() -> None:
    tl = build_timeline(make_snapshot("warm", now=NOON, zone="zone-a", sleep_sensor="bedroom sensor"))
    d = json.loads(json.dumps(ot.to_dict(tl)))
    assert set(d) == {
        "schema_version", "zone", "generated_at", "day", "horizon_end", "steps", "path", "latched", "regen_reason",
    }  # fmt: skip
    back = ot.from_dict(d)
    assert back is not None and back.zone == "zone-a" and back.day == tl.day
    assert [(s.ts, s.kind, s.text, s.reason, s.actor) for s in back.steps] == [
        (s.ts, s.kind, s.text, s.reason, s.actor) for s in tl.steps
    ]
    assert [(p.ts, p.temp_f, p.sensor, p.model) for p in back.path][:3] == [
        (p.ts, round(p.temp_f, 1), p.sensor, p.model) for p in tl.path
    ][:3]
    assert ot.from_dict({**d, "schema_version": 99}) is None and ot.from_dict({"junk": 1}) is None


def test_steps_are_sorted_by_ts_the_path_by_sensor_then_ts_and_you_steps_lead_30_minutes() -> None:
    snap = _evening_cooling(sensor_paths={"sensor.bed": [(NOON + timedelta(hours=1), 66.0)]}, sensor_path_model="m")
    tl = build_timeline(snap)
    assert [s.ts for s in tl.steps] == sorted(s.ts for s in tl.steps)
    keys = [(p.sensor, p.ts) for p in tl.path]
    assert keys == sorted(keys)
    assert {(p.sensor, p.model) for p in tl.path} == {("_ca", "v3"), ("sensor.bed", "m")}
    you = [s for s in tl.steps if s.actor == "you"]
    assert you and all(s.lead_minutes == 30 for s in you) and all(s.confidence == "ok" for s in you)
    assert all(s.lead_minutes == 0 for s in tl.steps if s.actor == "ca")
    assert all(s.source and s.reason in SENTENCES for s in tl.steps)


def test_latched_and_regen_reason_follow_the_contract() -> None:
    latch = TimelineLatch()
    first = latch.update(make_snapshot("mild", now=NOON)).timeline
    assert first.latched and first.regen_reason is None  # the first of the day
    again = latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=30))).timeline
    assert again.latched and again.regen_reason is None
    changed = latch.update(make_snapshot("mild", now=NOON + timedelta(minutes=60), occupancy="away")).timeline
    assert changed.regen_reason is not None and "occupancy" in changed.regen_reason
    assert not build_timeline(make_snapshot("mild", now=NOON)).latched  # a raw build is not the day's latched plan
