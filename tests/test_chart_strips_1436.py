"""Issue #1436: the chart's activity strips right of NOW are the operation timeline's spans.

Occupant story: the plan says "7:14 PM: close the windows before the house drifts below 68F" and "8:30 PM: CA holds the
bedroom between 66F and 72F tonight". Under the chart the faded strips (what the plan expects) showed Fan, Nat-vent and
Win plan from 5 PM to 7 AM, drawn from ``chart_data.predicted_activity`` (an older predictor that ignores the floor, the
close step and the night strategy). The person could not tell what the chart was showing.

Covered here: ``timeline_surfaces.strip_spans`` (every strip rule), ``planned_windows_open`` (the chart log's new
``windows_planned``), ``coordinator._compute_predicted_activity`` derived from the plan, the latch moving a nat-vent
stop with its window close (Issue #1439, the mirror found while building this), the chart log field, and the P8
``strip_check`` that fails on the old predictor's shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.climate_advisor.operation_timeline import (
    Actor,
    DetailedTimeline,
    PredictedRun,
    StepKind,
    TimelineStep,
    build_timeline,
)
from custom_components.climate_advisor.timeline_surfaces import (
    STRIP_ROWS,
    planned_windows_open,
    strip_covers,
    strip_spans,
)
from tests.timeline_fixtures import TZ, make_snapshot

NOW = datetime(2026, 11, 7, 16, 17, tzinfo=TZ)


def _at(h: int, m: int = 0, day: int = 0) -> datetime:
    return datetime(2026, 11, 7 + day, h, m, tzinfo=TZ)


def _step(ts, kind, actor=Actor.CA, text=None, *, status="planned", after=None, reason=None, conf="ok", **meta):
    m = {"id": f"{ts:%Y%m%dT%H%M}:{kind.value}", "status": status, **meta}
    if after:
        m["after"] = after
    return TimelineStep(
        ts=ts,
        actor=actor,
        kind=kind,
        text=text or f"{ts:%H:%M}: {kind.value}",
        reason=reason or kind.value,
        confidence=conf,
        meta=m,
    )


def _tl(*steps, runs=()) -> DetailedTimeline:
    return DetailedTimeline(
        schema_version=1,
        zone="z",
        generated_at=NOW - timedelta(minutes=5),
        day=NOW.date(),
        horizon_end=NOW + timedelta(hours=48),
        steps=tuple(sorted(steps, key=lambda s: s.ts)),
        path=(),
        latched=True,
        regen_reason=None,
        runs=tuple(runs),
    )


def _rows(spans):
    out: dict[str, list[tuple[str, str]]] = {}
    for d in spans:
        a, b = datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"])
        out.setdefault(d["row"], []).append((f"{a:%d %H:%M}", f"{b:%d %H:%M}"))
    return out


# The live Zone 1 plan of 2026-11-07 16:17 (GET /timeline): windows open now (no open step), close at 19:14, the night
# strategy holds the bedroom (no window or fan step), bedtime band 20:30, wake 06:15; heat runs overnight.
LIVE = _tl(
    _step(
        _at(19, 14), StepKind.WINDOWS_CLOSE, Actor.YOU, "7:14 PM: close the windows before the house drifts below 68°F."
    ),
    _step(_at(20, 30), StepKind.NIGHT_STRATEGY, text="8:30 PM: CA holds the bedroom between 66°F and 72°F tonight."),
    _step(_at(20, 30), StepKind.SLEEP_BAND),
    _step(_at(6, 15, 1), StepKind.WAKE_BAND),
    runs=[PredictedRun("heat", _at(1, 17, 1), _at(4, 17, 1), 20.3, "hold_floor")],
)


def test_the_owners_case_win_plan_ends_at_the_close_step_and_nothing_runs_overnight():
    spans = strip_spans(LIVE, NOW)
    rows = _rows(spans)
    assert rows["windowsplan"] == [("07 16:17", "07 19:14")]
    assert "natvent" not in rows and "fan" not in rows  # no session in the plan: nothing drawn, overnight or not
    assert rows["heating"] == [("08 01:17", "08 04:17")]
    win = next(d for d in spans if d["row"] == "windowsplan")
    assert win["text"] == "7:14 PM: close the windows before the house drifts below 68°F."  # the hover's plan step
    assert win["kind"] == "windows_close"
    for t in (_at(19, 30), _at(23, 0), _at(3, 0, 1), _at(6, 30, 1)):
        assert not strip_covers(spans, "windowsplan", t) and not strip_covers(spans, "natvent", t)


def test_a_period_ahead_runs_from_its_open_step_to_its_close_and_its_hover_names_the_open():
    tl = _tl(
        _step(_at(17, 0), StepKind.WINDOWS_OPEN, Actor.YOU, "5:00 PM: open the windows."),
        _step(_at(17, 0), StepKind.NAT_VENT_START, after="20261107T1700:windows_open"),
        _step(_at(19, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
        _step(_at(19, 0), StepKind.NAT_VENT_STOP),
    )
    spans = strip_spans(tl, NOW)
    rows = _rows(spans)
    assert rows["windowsplan"] == rows["natvent"] == rows["fan"] == [("07 17:00", "07 19:00")]
    assert next(d for d in spans if d["row"] == "windowsplan")["text"] == "5:00 PM: open the windows."


def test_a_running_session_with_only_a_stop_runs_from_now_and_is_clipped_to_the_window_period():
    # the latch moved the close earlier than the stop (mirror bug fixed in _merge_latched): the strip never shows the
    # fan after the windows the plan closes
    tl = _tl(
        _step(_at(18, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
        _step(_at(18, 30), StepKind.NAT_VENT_STOP),
    )
    rows = _rows(strip_spans(tl, NOW))
    assert rows["natvent"] == rows["fan"] == [("07 16:17", "07 18:00")]


def test_a_vetoed_period_draws_no_windows_plan_and_no_session():
    tl = _tl(
        _step(
            _at(15, 0),
            StepKind.WINDOWS_OPEN,
            Actor.YOU,
            "Keep windows closed to hold heat.",
            status="held",
            hold="held.direction_veto.heat",
            hold_kind="direction_veto",
        ),
        _step(_at(15, 0), StepKind.NAT_VENT_START, after="20261107T1500:windows_open"),
        _step(_at(18, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
        _step(_at(18, 0), StepKind.NAT_VENT_STOP),
    )
    assert strip_spans(tl, NOW) == []


def test_a_predicted_nat_vent_start_outside_any_window_period_draws_nothing():
    tl = _tl(_step(_at(21, 0), StepKind.NAT_VENT_START, reason="natvent_start.predicted"))
    assert strip_spans(tl, NOW) == []


def test_a_predicted_start_inside_a_period_ends_at_the_next_close():
    tl = _tl(
        _step(_at(19, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
        _step(_at(17, 0), StepKind.NAT_VENT_START, reason="natvent_start.predicted"),
    )
    assert _rows(strip_spans(tl, NOW))["natvent"] == [("07 17:00", "07 19:00")]


def test_the_night_strategy_fan_steps_draw_the_fan_and_its_windows():
    tl = _tl(
        _step(_at(21, 0), StepKind.WINDOWS_OPEN, Actor.YOU),
        _step(_at(21, 0), StepKind.FAN_ON, Actor.YOU, "9:00 PM: turn on the whole-house fan."),
        _step(_at(23, 0), StepKind.FAN_OFF, Actor.YOU),
        _step(_at(23, 0), StepKind.WINDOWS_CLOSE, Actor.YOU),
    )
    rows = _rows(strip_spans(tl, NOW))
    assert rows["fan"] == rows["windowsplan"] == [("07 21:00", "07 23:00")]
    assert "natvent" not in rows  # advice, not a CA session


@pytest.mark.parametrize(
    ("reason", "end"),
    [("occupancy_setback.away", ("08 00:00",)), ("occupancy_setback.vacation", ("09 16:17",))],
)
def test_away_holds_today_and_vacation_holds_the_whole_plan(reason, end):
    tl = _tl(_step(NOW, StepKind.SETBACK_START, reason=reason))
    assert _rows(strip_spans(tl, NOW))["away"] == [("07 16:17", end[0])]


def test_an_override_runs_from_now_to_the_resume_step():
    tl = _tl(_step(_at(18, 45), StepKind.RESUME_SCHEDULE, text="6:45 PM: your change ends and CA resumes."))
    spans = strip_spans(tl, NOW)
    assert _rows(spans) == {"override": [("07 16:17", "07 18:45")]}
    assert spans[0]["text"].startswith("6:45 PM")


def test_runs_name_their_minutes_and_edge_and_cooling_runs_draw_the_cooling_row():
    tl = _tl(runs=[PredictedRun("cool", _at(17, 0), _at(18, 30), 42.0, "hold_ceiling")])
    (span,) = strip_spans(tl, NOW)
    assert span["row"] == "cooling" and span["kind"] == "run"
    assert span["text"] == "Cooling expected for about 40 min between 5:00 PM and 6:30 PM, holding the band's ceiling."


def test_the_windows_open_row_has_no_plan_part_and_every_row_is_known():
    spans = strip_spans(LIVE, NOW)
    assert {d["row"] for d in spans} <= set(STRIP_ROWS)
    assert not [d for d in spans if d["row"] == "windows"]


def test_planned_windows_open_is_the_chart_log_meaning():
    assert planned_windows_open(LIVE, _at(17, 0)) is True
    assert planned_windows_open(LIVE, _at(19, 14)) is False  # the close instant: closed
    assert planned_windows_open(LIVE, _at(22, 0)) is False


@pytest.mark.parametrize("day_type", ["hot", "warm", "mild", "cool", "cold"])
@pytest.mark.parametrize("hour", [7, 11, 15, 19])
@pytest.mark.parametrize("fan_mode", ["whole_house_fan", "hvac_fan", "disabled"])
def test_built_plans_strips_agree_with_the_plan_reference(day_type, hour, fan_mode):
    """Real builder (the band and classification are production's): the strips always pass the P8 strip check."""
    from tools.sim_harness.strip_check import strip_problems

    now = datetime(2026, 11, 4, hour, 5, tzinfo=TZ)
    tl = build_timeline(make_snapshot(day_type, now=now, fan_mode=fan_mode))
    spans = strip_spans(tl, now)
    assert strip_problems(tl, now, spans, None, fan_mode=fan_mode) == []
    for d in spans:
        assert datetime.fromisoformat(d["start"]) >= now and d["end"] > d["start"]


# --- the P8 strip check ---------------------------------------------------------------------------------------------


def test_strip_check_fails_on_the_old_predictors_all_night_shape():
    from tools.sim_harness.strip_check import strip_problems

    old = [
        {
            "ts": (NOW.replace(minute=0) + timedelta(hours=h)).isoformat(),
            "fan_active": True,
            "windows_recommended": True,
        }
        for h in range(1, 16)
    ]
    problems = strip_problems(LIVE, NOW, None, old)
    assert any("windows_recommended=True" in p and "next close step 19:14" in p for p in problems), problems
    assert any("fan_active" in p for p in problems), problems
    # the derived flags (what get_chart_data now hands out) pass
    spans = strip_spans(LIVE, NOW)
    derived = [
        {
            **e,
            "fan_active": False,
            "windows_recommended": strip_covers(spans, "windowsplan", datetime.fromisoformat(e["ts"])),
        }
        for e in old
    ]
    assert strip_problems(LIVE, NOW, spans, derived) == []


def test_strip_check_catches_a_strip_beyond_the_plan():
    from tools.sim_harness.strip_check import strip_problems

    bad = [*strip_spans(LIVE, NOW), {"row": "natvent", "start": _at(20, 0).isoformat(), "end": _at(23, 0).isoformat()}]
    assert any(p.startswith("Nat-vent strip shows") for p in strip_problems(LIVE, NOW, bad, None))
    gap = [d for d in strip_spans(LIVE, NOW) if d["row"] != "windowsplan"]
    assert any(p.startswith("Win plan strip is empty") for p in strip_problems(LIVE, NOW, gap, None))


# --- predicted_activity derived from the plan -------------------------------------------------------------------------


def _coord_mod():
    import importlib

    return importlib.import_module("custom_components.climate_advisor.coordinator")


def _band(hours):
    return [{"ts": (NOW.replace(minute=0) + timedelta(hours=h)).isoformat(), "lower": 68, "upper": 74} for h in hours]


def test_predicted_activity_window_and_fan_flags_are_the_plans():
    fn = _coord_mod()._compute_predicted_activity
    regime = {e["ts"]: {"nat_vent_active": True, "hvac_mode": "off"} for e in _band(range(1, 16))}
    tl = _tl(
        _step(_at(19, 14), StepKind.WINDOWS_CLOSE, Actor.YOU),
        _step(_at(19, 14), StepKind.NAT_VENT_STOP),
    )
    out = fn(_band(range(1, 16)), regime, {"fan_mode": "whole_house_fan"}, plan_spans=strip_spans(tl, NOW))
    on = [datetime.fromisoformat(e["ts"]).hour for e in out if e["windows_recommended"]]
    fan = [datetime.fromisoformat(e["ts"]).hour for e in out if e["fan_active"]]
    assert on == fan == [17, 18, 19]  # 17:00, 18:00 and 19:00 (before the 19:14 close); none overnight
    assert {e["hvac_mode"] for e in out} == {"off"}  # the thermostat mode still comes from the walk


def test_predicted_activity_degraded_path_keeps_the_walk_and_fan_mode_on_keeps_the_fan():
    fn = _coord_mod()._compute_predicted_activity
    regime = {e["ts"]: {"nat_vent_active": True, "hvac_mode": "off"} for e in _band(range(1, 4))}
    assert all(e["fan_active"] and e["windows_recommended"] for e in fn(_band(range(1, 4)), regime, {}))
    out = fn(_band(range(1, 4)), regime, {"fan_mode": "on"}, plan_spans=[])
    assert all(e["fan_active"] and not e["windows_recommended"] for e in out)


# --- the latch moves a nat-vent stop with its close (Issue #1439, the mirror) -------------------------------------


def test_the_latch_moves_the_nat_vent_stop_when_it_moves_the_close_earlier():
    from custom_components.climate_advisor.operation_timeline import _merge_latched

    latched = _tl(
        _step(_at(8, 45, 1), StepKind.WINDOWS_OPEN, Actor.YOU),
        _step(_at(8, 45, 1), StepKind.NAT_VENT_START),
        _step(_at(10, 0, 1), StepKind.WINDOWS_CLOSE, Actor.YOU, "10:00 AM: close the windows."),
        _step(_at(10, 0, 1), StepKind.NAT_VENT_STOP, text="10:00 AM: CA stops the fan."),
    )
    fresh = _tl(
        _step(_at(8, 45, 1), StepKind.WINDOWS_OPEN, Actor.YOU),
        _step(_at(8, 45, 1), StepKind.NAT_VENT_START),
        _step(_at(9, 50, 1), StepKind.WINDOWS_CLOSE, Actor.YOU, "9:50 AM: close the windows."),
        _step(_at(9, 50, 1), StepKind.NAT_VENT_STOP, text="9:50 AM: CA stops the fan."),
    )
    merged = _merge_latched(latched, fresh, NOW)
    kinds = [(f"{s.ts:%H:%M}", s.kind.value) for s in merged.steps]
    assert ("09:50", "nat_vent_stop") in kinds and ("10:00", "nat_vent_stop") not in kinds
    assert ("09:50", "windows_close") in kinds and ("10:00", "windows_close") not in kinds
    assert len({s.id for s in merged.steps}) == len(merged.steps)


def test_the_latch_leaves_a_stop_alone_when_no_close_moved():
    from custom_components.climate_advisor.operation_timeline import _merge_latched

    latched = _tl(_step(_at(19, 0), StepKind.WINDOWS_CLOSE, Actor.YOU), _step(_at(19, 0), StepKind.NAT_VENT_STOP))
    merged = _merge_latched(latched, latched, NOW)
    assert [(f"{s.ts:%H:%M}", s.kind.value) for s in merged.steps] == [
        ("19:00", "nat_vent_stop"),
        ("19:00", "windows_close"),
    ]


# --- the chart log records what the plan said -------------------------------------------------------------------


def test_chart_log_writes_and_buckets_windows_planned(tmp_path):
    from custom_components.climate_advisor.chart_log import ChartStateLog, _bucket_context

    log = ChartStateLog(tmp_path, max_days=365)
    log.append(hvac="idle", fan=False, indoor=70.0, outdoor=65.0, windows_recommended=True, windows_planned=False)
    log.append(hvac="idle", fan=False, indoor=70.0, outdoor=65.0, windows_recommended=True)
    rows = log._entries
    assert rows[0]["windows_planned"] is False and rows[0]["windows_recommended"] is True
    assert "windows_planned" not in rows[1]  # no plan: absent, the chart falls back to the classifier flag
    assert _bucket_context([{"windows_planned": False}, {"windows_planned": True}])["windows_planned"] is True
    assert "windows_planned" not in _bucket_context([{"windows_recommended": True}])


def test_the_built_plan_grid_is_not_vacuous():
    """Guard on the grid above: some of those plans hold window periods, nat-vent sessions and runs to compare."""
    rows: set[str] = set()
    for day_type in ("hot", "warm", "mild", "cool", "cold"):
        for hour in (7, 11, 15, 19):
            now = datetime(2026, 11, 4, hour, 5, tzinfo=TZ)
            rows |= {d["row"] for d in strip_spans(build_timeline(make_snapshot(day_type, now=now)), now)}
    assert {"windowsplan", "natvent", "fan"} <= rows, rows
    assert rows & {"heating", "cooling"}, rows
