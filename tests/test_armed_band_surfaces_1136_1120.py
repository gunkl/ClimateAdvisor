"""Issues #1136 + #1120: surfaces report the comfort band armed RIGHT NOW.

Occupant framing: at night the Next User Action card must judge the room against the sleep range
the thermostat is holding (not the daytime range), and after a skipped wake-up or bedtime the
Activity Record must quote the range actually in force (not last period's).

Real ``_compute_next_action`` and real ``AutomationEngine._append_mode_change``; expected values are
the configured literals, never recomputed by the test.
"""

from __future__ import annotations

import asyncio
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


class TestPreCoolRowQuotesArmedBand1120:
    """Pre-cool arms a lowered ceiling; its mode-change row must show THAT band, not the clock band."""

    @staticmethod
    def _hot_tomorrow_engine():
        engine = _make_engine(indoor=75.0, mode="off")
        engine.config.update(_CFG)
        c = _cls()
        c.tomorrow_high = 100
        c.setback_modifier = -3.0
        engine._current_classification = c
        return engine

    def test_pre_cool_row_shows_the_ceiling_it_armed(self):
        engine = self._hot_tomorrow_engine()
        # 03:00 is inside the sleep window (64-72); pre-cool lowers the 72 ceiling by 3 to 69.
        now = datetime.datetime(2026, 10, 28, 3, 0, tzinfo=datetime.UTC)
        with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=now):
            asyncio.run(engine.handle_pre_cool(75.0, False))
        entry = engine.comfort_mode_changes[-1]
        assert entry["to_mode"] == "cool"
        assert (entry["floor_f"], entry["ceiling_f"]) == (64.0, 69.0)

    @staticmethod
    def _at(hour, minute=0):
        return patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime.datetime(2026, 10, 28, hour, minute, tzinfo=datetime.UTC),
        )

    def _pre_cooled(self):
        engine = self._hot_tomorrow_engine()
        with self._at(3, 0):
            asyncio.run(engine.handle_pre_cool(75.0, False))
        return engine

    @staticmethod
    def _quoted(engine):
        entry = engine.comfort_mode_changes[-1]
        return (entry["floor_f"], entry["ceiling_f"])

    def test_user_change_while_pre_cool_is_held_quotes_the_pre_cool_band(self):
        engine = self._pre_cooled()
        with self._at(4, 0):
            engine._append_mode_change("cool", "heat", "user")
        assert self._quoted(engine) == (64.0, 69.0)

    def test_door_pause_while_pre_cool_is_held_quotes_the_pre_cool_band(self):
        engine = self._pre_cooled()
        with self._at(4, 0):

            async def _pause():
                with engine._mode_change_context("pause"):
                    await engine._set_hvac_mode("off", reason="door open")

            asyncio.run(_pause())
        entry = engine.comfort_mode_changes[-1]
        assert (entry["to_mode"], entry["reason"]) == ("off", "pause")
        assert self._quoted(engine) == (64.0, 69.0)

    def test_after_the_wake_up_re_arm_rows_quote_the_day_band(self):
        engine = self._pre_cooled()
        with self._at(6, 30):
            asyncio.run(engine.handle_morning_wakeup(70.0))
            engine._append_mode_change("cool", "heat", "user")
        assert self._quoted(engine) == (68.0, 74.0)

    def test_any_later_setpoint_write_supersedes_the_pre_cool_band(self):
        engine = self._pre_cooled()
        with self._at(4, 0):
            asyncio.run(engine._set_temperature(72.0, reason="selector", mode="cool"))
            engine._append_mode_change("cool", "heat", "user")
        assert self._quoted(engine) == (64.0, 72.0)

    def test_skipped_wake_up_after_pre_cool_does_not_leave_the_night_band_all_day(self):
        # Override active at wake time: the wake-up is skipped, so no setpoint write clears the pre-cool band.
        # Pre-cool is a night state: from the wake time on rows quote the day band (the original #1120 bug).
        engine = self._pre_cooled()
        engine._manual_override_active = True
        with self._at(6, 30):
            asyncio.run(engine.handle_morning_wakeup(70.0))
        with self._at(4, 0):
            engine._append_mode_change("cool", "heat", "user")
        assert self._quoted(engine) == (64.0, 69.0)  # before the wake time it is still what is held
        with self._at(7, 0):
            engine._append_mode_change("heat", "cool", "user")
        assert self._quoted(engine) == (68.0, 74.0)

    def test_restart_during_pre_cool_has_no_stale_band(self):
        self._pre_cooled()
        fresh = self._hot_tomorrow_engine()  # a restart builds a new engine: nothing remembered
        with self._at(4, 0):
            fresh._append_mode_change("cool", "heat", "user")
        assert self._quoted(fresh) == (64.0, 72.0)

    def test_pre_cool_that_was_not_sent_is_not_remembered(self):
        engine = self._hot_tomorrow_engine()
        engine._whf_owns_hvac = lambda: True  # the fan owns the thermostat: the write is blocked
        with self._at(3, 0):
            asyncio.run(engine.handle_pre_cool(75.0, False))
            engine._append_mode_change("cool", "heat", "user")
        assert self._quoted(engine) == (64.0, 72.0)

    def test_door_pause_row_shows_clock_band_even_with_stale_armed_cache(self):
        engine = _make_engine(indoor=75.0, mode="cool")
        engine.config.update(_CFG)
        engine._current_classification = _cls()
        engine._last_comfort_band = ComfortBand(floor=64.0, ceiling=69.0, active="ceiling", reason="stale pre-cool")
        now = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)
        with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=now):

            async def _pause():
                with engine._mode_change_context("pause"):
                    await engine._set_hvac_mode("off", reason="door open")

            asyncio.run(_pause())
        entry = engine.comfort_mode_changes[-1]
        assert (entry["to_mode"], entry["reason"]) == ("off", "pause")
        assert (entry["floor_f"], entry["ceiling_f"]) == (68.0, 74.0)


class TestNextActionReadsTheEnginesResolvedBand:
    """The card reads the band through the engine's ``_resolved_comfort_band()`` (the call restore, override
    matching and the status API use), so any future input that resolver gains reaches the card too.
    Away/vacation return a flavor line before the band comparison, so the TOU away expansion
    (``tou_expansion_f``) cannot change this card today; this pins the single source, not that case."""

    @staticmethod
    def _card(indoor: float, engine_band: ComfortBand | None) -> str:
        coord_cls = importlib.import_module("custom_components.climate_advisor.coordinator").ClimateAdvisorCoordinator
        coord = object.__new__(coord_cls)
        coord.config = dict(_CFG)
        coord._occupancy_mode = "home"
        engine = None
        if engine_band is not None:
            engine = _make_engine(indoor=indoor, mode="off")
            engine._resolved_comfort_band = lambda _c: engine_band
        now = datetime.datetime(2026, 10, 28, 14, 0)
        with patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now):
            return coord_cls._compute_next_action(
                coord, _cls(), indoor_temp=indoor, outdoor_temp=60.0, windows_physically_open=False, ae=engine
            )

    def test_card_uses_the_engines_band(self):
        wide = ComfortBand(floor=60.0, ceiling=80.0, active="ceiling", reason="engine")
        assert self._card(77.0, wide) == "Comfortable — no action needed."

    def test_without_an_engine_the_bare_resolver_is_used(self):
        assert self._card(77.0, None) != "Comfortable — no action needed."
