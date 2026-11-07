"""v4's prediction holds tomorrow's planned window period (Issue #1391).

Occupant view: after the evening's forecast refresh the 24 h line (and the decisions that read it: the nat-vent plan,
the ceiling-guard horizon) shows tomorrow morning's free cooling when tomorrow's plan holds a window period, instead
of a house with the windows closed. When tomorrow's plan is not known the hours stay closed; the backtest never
scores a tomorrow window from a plan only written at the end of that day.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

from custom_components.climate_advisor import v4_shadow
from tests.test_control_feed_r6b import _ready, _refresh
from tests.test_v4_shadow_wiring import _build
from tests.timeline_fixtures import TZ
from tools.sim_harness.scenario_model import v4_model_from_spec

TODAY = (True, time(10, 0), time(12, 0))
TOMORROW = (True, time(8, 0), time(11, 0))


def _inputs(now: datetime, windows, tomorrow):
    hourly = [(now + timedelta(hours=h), 60.0) for h in range(50)]
    return v4_shadow.build_inputs(
        now=now,
        hourly=hourly,
        band=[(now, 66.0, 74.0)],
        windows=windows,
        tomorrow_windows=tomorrow,
        horizon_hours=44,
    )


def _open(inputs) -> list[datetime]:
    return [i.ts for i in inputs if i.windows_open]


def test_today_only_plan_keeps_tomorrow_closed() -> None:
    now = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    open_ts = _open(_inputs(now, TODAY, None))
    assert open_ts and all(t.date() == now.date() for t in open_ts)


def test_today_and_tomorrow_plans_both_open_their_own_hours() -> None:
    now = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    open_ts = _open(_inputs(now, TODAY, TOMORROW))
    today = [t for t in open_ts if t.date() == now.date()]
    tomorrow = [t for t in open_ts if t.date() == now.date() + timedelta(days=1)]
    assert today[0] == datetime(2026, 11, 6, 10, 0, tzinfo=TZ) and today[-1] == datetime(2026, 11, 6, 11, 45, tzinfo=TZ)
    assert tomorrow[0] == datetime(2026, 11, 7, 8, 0, tzinfo=TZ) and tomorrow[-1] == datetime(
        2026, 11, 7, 10, 45, tzinfo=TZ
    )


def test_tomorrow_only_plan_after_midnight_and_in_the_evening() -> None:
    # today's period is over (or never planned): only tomorrow's morning is open, wherever the anchor is
    for now in (datetime(2026, 11, 6, 22, 0, tzinfo=TZ), datetime(2026, 11, 7, 0, 30, tzinfo=TZ)):
        open_ts = _open(_inputs(now, (False, None, None), TOMORROW))
        day = now.date() + timedelta(days=1)
        assert open_ts and all(t.date() == day for t in open_ts)
        assert open_ts[0] == datetime.combine(day, time(8, 0), tzinfo=TZ)


def test_unknown_tomorrow_is_closed_and_tomorrow_has_no_anchor_fallback() -> None:
    now = datetime(2026, 11, 6, 22, 0, tzinfo=TZ)
    assert _open(_inputs(now, (False, None, None), None)) == []
    # recommended with no planned times: nothing says when, so tomorrow stays closed (no 4 h fallback)
    assert _open(_inputs(now, (False, None, None), (True, None, None))) == []
    assert v4_shadow.planned_window_span(now, True, None, None, day_offset=1) is None
    assert v4_shadow.planned_window_spans(now, (False, None, None), None) == ()


def test_tomorrow_overnight_period_runs_past_its_midnight() -> None:
    now = datetime(2026, 11, 6, 20, 0, tzinfo=TZ)
    span = v4_shadow.planned_window_span(now, True, time(22, 0), time(1, 0), day_offset=1)
    assert span == (datetime(2026, 11, 7, 22, 0, tzinfo=TZ), datetime(2026, 11, 8, 1, 0, tzinfo=TZ))


# ----------------------------------------------------------------------- the live coordinator: plan AND control path
def test_plan_and_control_cycles_feed_the_same_tomorrow_window(monkeypatch) -> None:
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)
    coord._tomorrow_classification_plan = lambda: {
        "windows_recommended": True,
        "window_open_time": "00:00:00",
        "window_close_time": "23:59:00",
    }
    seen: list[tuple[bool, list]] = []
    real = v4_shadow.run_cycle

    def spy(**kw):
        seen.append((kw.get("baseline", False), list(kw["inputs"])))
        return real(**kw)

    monkeypatch.setattr(v4_shadow, "run_cycle", spy)
    _refresh(coord, scheduler)  # the control (baseline) cycle
    assert seen and seen[-1][0] is True
    inputs = seen[-1][1]
    tomorrow = scheduler.now().astimezone().date() + timedelta(days=1)
    tomorrow_inputs = [i for i in inputs if i.ts.astimezone(scheduler.now().tzinfo).date() == tomorrow]
    assert tomorrow_inputs and all(i.windows_open for i in tomorrow_inputs)
    # plan cycle: the shared kwargs builder is the one place the inputs come from
    kwargs = coord._v4_cycle_kwargs(scheduler.now(), coord._v4_live_row(scheduler.now()), model, coord._v4_store)
    assert [i.windows_open for i in kwargs["inputs"]] == [i.windows_open for i in inputs]
    # tomorrow unknown -> closed
    coord._tomorrow_classification_plan = lambda: None
    kwargs = coord._v4_cycle_kwargs(scheduler.now(), coord._v4_live_row(scheduler.now()), model, coord._v4_store)
    assert not any(i.windows_open for i in kwargs["inputs"])


# ----------------------------------------------------------------------------------------------- the backtest guard
def _backtest_module():
    tools = str(Path(__file__).resolve().parent.parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import importlib

    return importlib.import_module("predict_backtest")


def test_backtest_uses_tomorrows_record_only_when_known_at_the_anchor() -> None:
    pb = _backtest_module()
    recs = {"2026-11-07": (time(8, 0), time(11, 0))}
    args = SimpleNamespace(window_plan_source="record", tomorrow_plan_hour=18.0, window_plan_hours=4.0)
    evening = datetime(2026, 11, 6, 21, 0, tzinfo=TZ)
    morning = datetime(2026, 11, 6, 9, 0, tzinfo=TZ)
    assert pb.tomorrow_window_plan(evening, args, recs, TZ) == (True, time(8, 0), time(11, 0))
    # no hindsight: an anchor before the refresh must not see the plan the day record only got at day end
    assert pb.tomorrow_window_plan(morning, args, recs, TZ) is None
    # no record for tomorrow, or the fallback source: closed
    assert pb.tomorrow_window_plan(evening, args, {}, TZ) is None
    args.window_plan_source = "fallback"
    assert pb.tomorrow_window_plan(evening, args, recs, TZ) is None
    args.window_plan_source = "record"
    args.tomorrow_plan_hour = 24.0  # disabled
    assert pb.tomorrow_window_plan(evening, args, recs, TZ) is None
    # today's own record is never read as tomorrow's
    assert (
        pb.tomorrow_window_plan(
            evening, SimpleNamespace(window_plan_source="record"), {"2026-11-06": (time(8, 0), time(9, 0))}, TZ
        )
        is None
    )
