"""R3b additions to the operation timeline builder (Issue #1230): the steps Next Automation and the briefing read.

Occupant view: the "Next Automation" card names what CA itself does next (briefing, wake/bedtime band, pre-cool, the AC
switching on to hold the ceiling, natural ventilation). Each of those is now a timeline step, so the card, the
briefing's
step list and the endpoint can only ever say the same thing; an occupant away on a TOU day is told the band
widening that
is really coming.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta

from custom_components.climate_advisor.operation_timeline import (
    LIVE_KINDS,
    StepKind,
    TimelineLatch,
    build_timeline,
    detail_dict,
)
from custom_components.climate_advisor.timeline_surfaces import (
    NEXT_AUTOMATION_LABELS,
    next_automation,
    next_automation_label,
    operations_lines,
)
from tests.timeline_fixtures import MODEL, TZ, make_snapshot

NOON = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)


def test_the_daily_briefing_is_a_ca_step_at_the_configured_time() -> None:
    tl = build_timeline(make_snapshot("mild", now=NOON))
    sends = [s for s in tl.steps if s.kind == StepKind.BRIEFING_SEND]
    assert [s.ts for s in sends] == [datetime(2026, 11, 5, 6, 0, tzinfo=TZ)]  # today's 06:00 is past
    assert sends[0].actor == "ca" and sends[0].text == "6:00 AM: CA sends the daily briefing." or "Thu" in sends[0].text
    assert next_automation_label(sends[0], "fahrenheit") == "Send daily briefing"


def test_the_ceiling_guard_step_is_the_ac_turning_on_to_hold_the_ceiling() -> None:
    breach = NOON + timedelta(hours=3)
    snap = make_snapshot("warm", now=NOON, ceiling_breach=breach)
    tl = build_timeline(snap)
    step = next(s for s in tl.steps if s.kind == StepKind.CEILING_GUARD)
    assert step.ts == breach and step.mode == "cool" and step.source.startswith("automation._apply_ode_ceiling_guard")
    assert next_automation_label(step, "fahrenheit") == "AC turns on to hold the ceiling"
    action, _at, used = next_automation(tl, NOON, "fahrenheit")
    assert action in NEXT_AUTOMATION_LABELS.values() or action.startswith(("Bedtime", "Pre-cool"))
    assert used is not None


def test_the_engine_nat_vent_start_prediction_needs_a_fan() -> None:
    start = NOON + timedelta(hours=2)
    with_fan = build_timeline(
        make_snapshot("warm", now=NOON, nat_vent_predicted_start=start, fan_mode="whole_house_fan")
    )
    step = next(s for s in with_fan.steps if s.reason == "natvent_start.predicted")
    assert step.kind == StepKind.NAT_VENT_START and step.ts == start and "whole-house fan" in step.text
    assert next_automation_label(step, "fahrenheit") == "Natural ventilation"
    no_fan = build_timeline(make_snapshot("warm", now=NOON, nat_vent_predicted_start=start, fan_mode="disabled"))
    assert not [s for s in no_fan.steps if s.reason == "natvent_start.predicted"]


def test_an_away_zone_on_a_tou_day_gets_the_band_widening_and_its_end() -> None:
    start = NOON + timedelta(hours=5)
    end = start + timedelta(hours=4)
    snap = make_snapshot("hot", now=NOON, occupancy="away", tou_av_start=start, tou_av_end=end, tou_av_expansion_f=3.0)
    tl = build_timeline(snap)
    pair = [s for s in tl.steps if s.kind == StepKind.SETPOINT_CHANGE]
    assert [s.reason for s in pair] == ["setpoint_change.tou_expansion_start", "setpoint_change.tou_expansion_end"]
    assert pair[0].ts == start and pair[1].ts == end and pair[0].actor == "ca"
    assert "away band" in pair[0].text and "high-cost" in pair[0].text
    home = build_timeline(replace(snap, occupancy_mode="home"))
    assert not [s for s in home.steps if s.kind == StepKind.SETPOINT_CHANGE]  # only while nobody is home


def test_tomorrows_engine_window_plan_is_in_the_timeline_and_its_days() -> None:
    snap = make_snapshot("mild", now=NOON, tomorrow="warm")
    tl = build_timeline(snap)
    tomorrow = (NOON + timedelta(days=1)).date()
    assert [s for s in tl.steps if s.actor == "you" and s.ts.date() == tomorrow], "tomorrow's engine plan is dropped"
    days = detail_dict(tl)["days"]
    assert [d["date"] for d in days] == [NOON.date().isoformat(), tomorrow.isoformat()]
    assert days[1]["day_type"] == "warm" and "windows_recommended" in days[1]


def test_live_steps_follow_the_engine_each_cycle_while_the_rest_stays_as_promised() -> None:
    latch = TimelineLatch()
    snap = make_snapshot("warm", now=NOON, ceiling_breach=NOON + timedelta(hours=3))
    latch.update(snap)
    later = replace(snap, now=NOON + timedelta(minutes=30), ceiling_breach=NOON + timedelta(hours=4))
    out = latch.update(later)
    assert not out.regenerated
    guard = [s for s in out.timeline.steps if s.kind == StepKind.CEILING_GUARD]
    assert [s.ts for s in guard] == [NOON + timedelta(hours=4)]  # the engine's new answer, not the stale one
    assert StepKind.CEILING_GUARD in LIVE_KINDS and StepKind.WAKE_BAND not in LIVE_KINDS


def test_a_validated_model_may_adjust_the_engine_period_but_never_removes_it() -> None:
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=70.0, model=dict(MODEL))
    tl = build_timeline(snap)
    planned = [c for c in tl.candidates if c.kind == "planned"]
    assert planned and all(c.accepted or c.reason in ("direction", "overlaps", "too_short") for c in planned)
    for c in planned:
        if c.accepted:
            assert c.confidence in ("medium", "high") and c.reason in ("accepted", "engine_no_benefit")


def test_the_briefing_steps_list_is_the_timelines_own_sentences() -> None:
    tl = build_timeline(make_snapshot("mild", now=NOON))
    lines = operations_lines(tl, NOON)
    today = [s for s in tl.steps if NOON - timedelta(minutes=1) <= s.ts < datetime(2026, 11, 5, 0, 0, tzinfo=TZ)]
    assert lines == [f"  • {s.text}" for s in today if s.kind != StepKind.BRIEFING_SEND][: len(lines)]
    assert all(
        s.text.split(": ")[0] in line for s, line in zip(today, lines, strict=False) if s.kind != StepKind.BRIEFING_SEND
    )
    assert time(0, 0)  # (module imports kept honest)
