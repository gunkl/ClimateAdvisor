"""Real-HA test for Issue #1231 (absorbs #1218): the planned window period ends on time.

Occupant story: a mild day. The briefing says "close windows by 17:00". The person leaves a window open. After
17:00 the Status card kept saying "windows open (as planned)" for hours and the background open-window check
(the one that pauses HVAC for a window left open past its debounce) was skipped for those hours. The forward-only
nat-vent scan re-resolves the cutoff to "now + 1 h" once the outdoor/indoor crossing is behind us
(``nat_vent_cutoff_already_reached=True``), and the engine only compared that cutoff's time of day.

No heat or cool was observed running into the window (every Climate Advisor write passes the open-sensor guard);
this is about the period ending on time, with the open-window pause then applying.

The house has no calibrated thermal curve, so the plan cache is replaced by a stand-in that returns exactly what
``compute_nat_vent_plan`` returns for this day: cutoff 17:00 before the crossing, then "next hour grid point +
already_reached" once 17:00 has passed. Everything downstream (the coordinator's call sites, the engine, the
Status text) is the real code.

Fan mode ``both`` is unsupported and never used here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

pytestmark = pytest.mark.house

_DEBOUNCE_S = 30


def _forward_only_plan(self) -> None:
    now = dt_util.now()
    close = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if now < close:
        cutoff, reached = close, False
    else:
        cutoff, reached = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1), True
    self._nat_vent_plan = {
        "nat_vent_cutoff": cutoff,
        "nat_vent_cutoff_reason": "outdoor_rise",
        "comfort_floor_crossing_time": None,
        "ceiling_breach_time": None,
        "precool_start_time": None,
        "any_nat_vent_window": True,
        "evening_open_time": None,
        "nat_vent_cutoff_already_reached": reached,
    }


@pytest.mark.parametrize("fan", ["whf", "hvac_fan"])
async def test_window_left_open_past_close_ends_the_planned_period(make_house, monkeypatch, fan):
    monkeypatch.setattr(ClimateAdvisorCoordinator, "_compute_and_cache_nat_vent_plan", _forward_only_plan)
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        start=datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
        initial_indoor_f=72.0,
        outdoor_f=68.0,
        high_f=72.0,
        low_f=58.0,
        initial_hvac_mode="off",
        ca_config={"sensor_debounce_seconds": _DEBOUNCE_S, "fan_min_runtime_per_hour": 0},
    )
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    c = engine._current_classification
    assert c is not None and c.windows_recommended and c.window_close_time.hour == 17, "precondition: mild day"
    await house.open("window")
    await house.advance(minutes=30)
    assert engine._is_within_planned_window_period(), "precondition: inside the planned period before 17:00"

    await house.advance(hours=3)  # 18:36 -- a cycle has recomputed the plan after the 17:00 crossing

    assert dt_util.now().hour >= 18
    assert engine._nat_vent_cutoff_already_reached is True, "the already-reached plan reached the engine"
    assert not engine._is_within_planned_window_period(), "the period ended at the close, not at 'now + 1 h'"
    assert house.sensor("automation_status").state != "windows open (as planned)"
    assert engine._paused_by_door or engine._natural_vent_active, "the open-window pause (or nat-vent) applies"
    assert house.thermostat.action not in ("cooling", "heating"), "no heat/cool into the open window"
