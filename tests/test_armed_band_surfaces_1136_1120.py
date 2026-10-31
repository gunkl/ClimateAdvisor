"""Issues #1136 + #1120: surfaces report the comfort band armed RIGHT NOW.

Occupant framing: at night the Next User Action card must judge the room against the sleep range
the thermostat is holding (not the daytime range), and after a skipped wake-up or bedtime the
Activity Record must quote the range actually in force (not last period's).

Real ``_compute_next_action`` and real ``AutomationEngine._append_mode_change``; expected values are
the configured literals, never recomputed by the test.
"""

from __future__ import annotations

import datetime
import importlib
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import ComfortBand
from custom_components.climate_advisor.classifier import DayClassification
from tests.test_mode_change_clock import _make_engine

_CFG = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,
    "sleep_heat": 64.0,
    "sleep_cool": 72.0,
    "sleep_time": "22:30",
    "wake_time": "06:30",
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "temp_unit": "fahrenheit",
}


def _cls() -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        day_type="mild",
        trend_direction="stable",
        trend_magnitude=0,
        today_high=72,
        today_low=60,
        tomorrow_high=72,
        tomorrow_low=60,
        hvac_mode="off",
        pre_condition=False,
        pre_condition_target=None,
        windows_recommended=False,
        window_open_time=None,
        window_close_time=None,
        setback_modifier=0.0,
        window_opportunity_morning=False,
        window_opportunity_evening=False,
        window_opportunity_morning_start=datetime.time(6, 0),
        window_opportunity_morning_end=datetime.time(9, 0),
        window_opportunity_evening_start=datetime.time(17, 0),
        window_opportunity_evening_end=datetime.time(0, 0),
    )
    return c


def _next_action(hour: int, minute: int, indoor: float, outdoor: float = 60.0) -> str:
    coord_cls = importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator
    coord = object.__new__(coord_cls)
    coord.config = dict(_CFG)
    coord._occupancy_mode = "home"
    now = datetime.datetime(2026, 10, 28, hour, minute)
    with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
        return coord_cls._compute_next_action(
            coord, _cls(), indoor_temp=indoor, outdoor_temp=outdoor, windows_physically_open=False, ae=None
        )


class TestNextActionUsesArmedBand1136:
    def test_night_inside_sleep_band_is_comfortable(self):
        # 66F is below the 68F daytime floor but inside the 64-72 sleep band.
        assert _next_action(23, 30, indoor=66.0) == "Comfortable — no action needed."

    def test_night_above_sleep_ceiling_asks_for_cooling_help(self):
        # 73F is inside the 68-74 daytime band but above the 72F sleep ceiling.
        assert _next_action(23, 30, indoor=73.0) != "Comfortable — no action needed."

    def test_night_below_sleep_floor_asks_for_heating_help(self):
        assert _next_action(23, 30, indoor=63.0) != "Comfortable — no action needed."

    def test_daytime_still_uses_daytime_band(self):
        # Mirror: awake at 12:00 the same 66F IS below the 68F floor, 73F is fine.
        assert _next_action(12, 0, indoor=66.0) != "Comfortable — no action needed."
        assert _next_action(12, 0, indoor=73.0) == "Comfortable — no action needed."


class TestModeChangeRecordsBandInForce1120:
    @pytest.mark.parametrize(
        ("hour", "minute", "stale", "expected"),
        [
            # Wake-up skipped: last ARMED band is last night's sleep band, clock says awake.
            (7, 0, (64.0, 72.0), (68.0, 74.0)),
            # Mirror, bedtime skipped: last armed band is the day band, clock says asleep.
            (23, 0, (68.0, 74.0), (64.0, 72.0)),
        ],
    )
    def test_user_mode_change_quotes_band_in_force(self, hour, minute, stale, expected):
        engine = _make_engine(indoor=70.0, mode="cool")
        engine.config.update(_CFG)
        engine._current_classification = _cls()
        engine._last_comfort_band = ComfortBand(floor=stale[0], ceiling=stale[1], active="ceiling", reason="stale")
        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda etype, data: events.append((etype, data))
        now = datetime.datetime(2026, 10, 28, hour, minute, tzinfo=datetime.UTC)
        with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=now):
            engine._append_mode_change("cool", "heat", "user")
        entry = engine.comfort_mode_changes[-1]
        assert (entry["floor_f"], entry["ceiling_f"]) == expected
        payload = next(d for t, d in events if t == "thermostat_mode_set")
        assert (payload["floor_f"], payload["ceiling_f"]) == expected
