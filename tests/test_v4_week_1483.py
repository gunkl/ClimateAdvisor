"""Issue #1483: v4's display line runs up to a week ahead, as far as the forecast goes; the decisions stay on 24 h.

Occupant view: the Status chart's main prediction (v4) stopped 24 h ahead while the forecast and the comparison line
(v3) ran for days, and day two of the plan list had no v4 path behind it. Now the line runs to the end of the
forecast (the real hourly forecast, then the hourly curve built from the daily highs and lows), never past a week,
and each later day's planned window period is in it. What the house does is unchanged: the prediction the decisions
read is still 24 h on the real hourly forecast alone, and the first 24 h of both lines are the same inputs.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from custom_components.climate_advisor import v4_shadow
from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day
from custom_components.climate_advisor.const import (
    V4_CONTROL_HORIZON_HOURS,
    V4_PLAN_DAYS_AHEAD,
    V4_PREDICT_HORIZON_HOURS,
)
from tests.test_control_feed_r6b import _ready, _refresh
from tests.test_v4_shadow_wiring import _build
from tools.sim_harness.scenario_model import v4_model_from_spec


def _daily(now: datetime, days: int, *, high: float = 66.0, low: float = 48.0, skip: tuple[int, ...] = ()) -> list:
    """A daily forecast from today on, in the provider's shape (UTC-midnight day labels, high and low)."""
    out = []
    for k in range(days):
        if k in skip:
            continue
        d = now.date() + timedelta(days=k)
        out.append({"datetime": f"{d.isoformat()}T00:00:00+00:00", "temperature": high + k, "templow": low + k})
    return out


def _setup(days: int = 7, **daily_kw):
    coord, _hass, scheduler = _build()
    model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
    _ready(coord, scheduler, "v4", model)
    coord._daily_forecast_full = _daily(scheduler.now(), days, **daily_kw)
    return coord, scheduler, model


def _kwargs(coord, scheduler, model, *, week: bool):
    now = scheduler.now()
    return coord._v4_cycle_kwargs(now, coord._v4_live_row(now), model, coord._v4_store, week=week)


def _last_real_hour(coord, now: datetime) -> datetime:
    return datetime.fromisoformat(coord._v4_hourly_until(now))


def _key(i) -> tuple:
    return (i.ts, round(i.outdoor_f, 6), i.heat_sp_f, i.cool_sp_f, i.windows_open, i.fan_running)


# ----------------------------------------------------------------------------------- 1. the decisions are unchanged
class TestDecisionsUnchanged:
    def test_the_decisions_prediction_is_still_24_h_on_the_real_hourly_forecast(self, monkeypatch):
        """A week of daily forecast is there, yet the curve the decisions scan stops at 24 h, as before."""
        coord, scheduler, _model = _setup()
        seen: list[list] = []
        real = v4_shadow.run_cycle

        def spy(**kw):
            if kw.get("baseline"):
                seen.append(list(kw["inputs"]))
            return real(**kw)

        monkeypatch.setattr(v4_shadow, "run_cycle", spy)
        _refresh(coord, scheduler)
        assert seen, "the control cycle did not run"
        inputs = seen[-1]
        assert len(inputs) == V4_CONTROL_HORIZON_HOURS * 4 == 96
        assert inputs[-1].ts == scheduler.now() + timedelta(hours=V4_CONTROL_HORIZON_HOURS)

    def test_the_decisions_inputs_ignore_the_daily_forecast_and_the_later_days(self):
        coord, scheduler, model = _setup()
        with_daily = [_key(i) for i in _kwargs(coord, scheduler, model, week=False)["inputs"]]
        coord._daily_forecast_full = []
        coord._v4_later_window_plans = lambda now: pytest.fail("the decisions' cycle must not plan later days")
        without = [_key(i) for i in _kwargs(coord, scheduler, model, week=False)["inputs"]]
        assert with_daily == without

    def test_the_first_24_h_of_the_display_line_are_the_decisions_inputs(self):
        """Same inputs for the first day, so the two predictions agree there (the rollout is causal)."""
        coord, scheduler, model = _setup()
        control = [_key(i) for i in _kwargs(coord, scheduler, model, week=False)["inputs"]]
        display = [_key(i) for i in _kwargs(coord, scheduler, model, week=True)["inputs"]]
        assert display[: len(control)] == control


# --------------------------------------------------------------------------------- 2. the display line's reach
class TestDisplayReach:
    def test_the_line_runs_past_the_hourly_forecast_on_the_daily_highs_and_lows(self):
        coord, scheduler, model = _setup(days=5)
        now = scheduler.now()
        inputs = _kwargs(coord, scheduler, model, week=True)["inputs"]
        last_real = _last_real_hour(coord, now)
        assert inputs[-1].ts > last_real + timedelta(hours=24), "nothing was added past the real hourly forecast"
        # the last forecast day ends the line: today + 4 days is the last daily entry
        assert inputs[-1].ts.astimezone(now.tzinfo).date() <= now.date() + timedelta(days=4)
        assert inputs[-1].ts <= now + timedelta(hours=V4_PREDICT_HORIZON_HOURS)

    def test_a_longer_forecast_stops_at_a_week(self):
        coord, scheduler, model = _setup(days=12)
        now = scheduler.now()
        inputs = _kwargs(coord, scheduler, model, week=True)["inputs"]
        assert inputs[-1].ts == now + timedelta(hours=V4_PREDICT_HORIZON_HOURS)
        assert len(inputs) == V4_PREDICT_HORIZON_HOURS * 4

    def test_no_daily_forecast_stops_at_the_hourly_forecast(self):
        coord, scheduler, model = _setup()
        coord._daily_forecast_full = []
        now = scheduler.now()
        inputs = _kwargs(coord, scheduler, model, week=True)["inputs"]
        assert inputs[-1].ts <= _last_real_hour(coord, now) + timedelta(minutes=30)

    def test_the_real_hours_are_never_replaced_by_the_daily_curve(self):
        coord, scheduler, model = _setup()
        now = scheduler.now()
        real = {i.ts: i.outdoor_f for i in _kwargs(coord, scheduler, model, week=False)["inputs"]}
        week = {i.ts: i.outdoor_f for i in _kwargs(coord, scheduler, model, week=True)["inputs"]}
        last_real = _last_real_hour(coord, now)
        for ts, v in week.items():
            if ts <= last_real and ts in real:
                assert v == pytest.approx(real[ts])

    def test_the_cycle_keeps_where_the_real_forecast_ends_for_the_chart(self):
        from tests.test_v4_shadow_wiring import _cycle, _seed_cycle_inputs  # noqa: PLC0415

        coord, scheduler, _model = _setup()
        coord._v4_store.v4_model = v4_model_from_spec({"confidence": "ok", "primary": coord.config["climate_entity"]})
        _seed_cycle_inputs(coord, scheduler)
        _cycle(coord, scheduler)
        future = coord._v4_last_future
        assert future is not None and future["hourly_until"] == coord._v4_hourly_until(scheduler.now())
        last = max(t for pts in future["predictions"].values() for t, _v in pts)
        assert last > datetime.fromisoformat(future["hourly_until"]) + timedelta(hours=24)


# ----------------------------------------------------------------------------------- 3. every day's window plan
class TestLaterDayWindows:
    def test_each_later_day_is_classified_the_way_tomorrow_is(self):
        """Day N's plan = classify_day on its own high/low in the 'today' position, trend stable, no hysteresis."""
        coord, scheduler, _model = _setup()
        now = scheduler.now()
        plans = coord._v4_later_window_plans(now)
        assert len(plans) == V4_PLAN_DAYS_AHEAD - 1  # the day after tomorrow up to today + V4_PLAN_DAYS_AHEAD
        for k, plan in enumerate(plans, start=2):
            hi, lo, prev_lo = 66.0 + k, 48.0 + k, 48.0 + k - 1
            want = classify_day(
                ForecastSnapshot(
                    today_high=hi, today_low=lo, tomorrow_high=hi, tomorrow_low=lo, current_outdoor_temp=prev_lo
                ),
                **coord._classify_kwargs(seed_hysteresis=False),
            )
            assert plan == (bool(want.windows_recommended), want.window_open_time, want.window_close_time), k

    def test_a_day_the_forecast_does_not_cover_is_closed(self):
        coord, scheduler, _model = _setup(skip=(3,))
        plans = coord._v4_later_window_plans(scheduler.now())
        assert plans[1] is None  # today + 3
        assert plans[0] is not None and plans[2] is not None

    def test_a_day_without_a_low_is_closed(self):
        coord, scheduler, _model = _setup()
        coord._daily_forecast_full[4]["templow"] = None  # today + 4
        assert coord._v4_later_window_plans(scheduler.now())[2] is None

    def test_celsius_daily_forecast_is_read_in_fahrenheit(self, monkeypatch):
        """The daily forecast is in the provider's unit; the classifier is fed degrees F (internal unit)."""
        from custom_components.climate_advisor import coordinator as coord_mod  # noqa: PLC0415

        coord, scheduler, _model = _setup()
        now = scheduler.now()
        coord.config["temp_unit"] = "celsius"
        coord._daily_forecast_full = [
            {
                **e,
                "temperature": round((e["temperature"] - 32) / 1.8, 4),
                "templow": round((e["templow"] - 32) / 1.8, 4),
            }
            for e in _daily(now, 7)
        ]
        fed: list[ForecastSnapshot] = []
        real = coord_mod.classify_day
        monkeypatch.setattr(coord_mod, "classify_day", lambda fc, **kw: fed.append(fc) or real(fc, **kw))
        coord._v4_later_window_plans(now)
        assert [(round(f.today_high, 2), round(f.today_low, 2)) for f in fed] == [
            (66.0 + k, 48.0 + k) for k in range(2, V4_PLAN_DAYS_AHEAD + 1)
        ]

    def test_a_later_days_planned_period_opens_the_windows_in_the_display_line_only(self):
        coord, scheduler, model = _setup()
        now = scheduler.now()
        day3 = now.astimezone(now.tzinfo).date() + timedelta(days=3)
        coord._v4_later_window_plans = lambda _now: [None, (True, time(10, 0), time(14, 0)), None, None, None]
        week = _kwargs(coord, scheduler, model, week=True)["inputs"]
        opened = [
            i.ts.astimezone(now.tzinfo) for i in week if i.windows_open and i.ts.astimezone(now.tzinfo).date() == day3
        ]
        assert opened and opened[0].time() == time(10, 0) and opened[-1].time() == time(13, 45)
        assert not any(i.windows_open for i in week if i.ts.astimezone(now.tzinfo).date() not in (day3,)
                       and i.ts > now + timedelta(days=2))  # fmt: skip

    def test_the_shared_rule_puts_each_later_day_on_its_own_date(self):
        now = datetime(2026, 11, 8, 9, 0).astimezone()
        spans = v4_shadow.planned_window_spans(
            now,
            (False, None, None),
            None,
            later=[(True, time(8, 0), time(9, 0)), None, (True, time(22, 0), time(1, 0))],
        )
        d = now.date()
        assert spans == (
            (datetime.combine(d + timedelta(days=2), time(8, 0), tzinfo=now.tzinfo),
             datetime.combine(d + timedelta(days=2), time(9, 0), tzinfo=now.tzinfo)),
            (datetime.combine(d + timedelta(days=4), time(22, 0), tzinfo=now.tzinfo),
             datetime.combine(d + timedelta(days=5), time(1, 0), tzinfo=now.tzinfo)),
        )  # fmt: skip


# ------------------------------------------------------------------------------- 4. the plan list's own horizon
def test_the_plan_list_reads_the_week_long_line_only_up_to_its_own_horizon():
    """The 48 h plan list takes v4's path up to 48 h (no day-6 temperatures in its summary); it used to stop at 24 h."""
    from custom_components.climate_advisor.operation_timeline import TIMELINE_HORIZON_HOURS  # noqa: PLC0415
    from tests.test_debug_pane_words import _mild_day  # noqa: PLC0415

    coord, scheduler, _model = _setup()
    now = scheduler.now()
    week = [(now + timedelta(minutes=15 * k), 70.0) for k in range(1, V4_PREDICT_HORIZON_HOURS * 4 + 1)]
    coord._v4_last_future = {"at": now.isoformat(), "predictions": {"_ca": week}, "corrected": [], "baseline": {}}
    with scheduler.installed():
        snap = coord._timeline_snapshot(_mild_day())
    last = max(t for t, _v in snap.selected_path)
    assert snap.selected_path_model == "v4"
    assert last == now + timedelta(hours=TIMELINE_HORIZON_HOURS)
