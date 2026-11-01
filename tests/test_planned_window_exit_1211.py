"""Issue #1211 (owner decision O4): the planned window period ending with a window still open.

Mirror of the open-side change (A3). The period ends silently (clock past the close time, or the
recommendation cleared); nothing fired, so a window left open past the end was only paused by whichever
coordinator cycle ran the lazy reconcile next (up to 30 minutes with the HVAC running against it).
``consume_planned_window_exit_edge()`` reports the active -> inactive edge once, and the existing pause-only
reconcile then runs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from tests.test_windows_recommended_integration import (
    _make_automation_engine,
    _make_dt,
    _make_state,
    _warm_day_classification_with_windows,
)


def _engine_in_period():
    engine = _make_automation_engine()
    engine._current_classification = _warm_day_classification_with_windows()  # 06:00-10:00, flag on
    return engine


def test_edge_fires_once_when_the_clock_passes_the_close_time():
    engine = _engine_in_period()
    with patch("custom_components.climate_advisor.automation.dt_util") as dt:
        dt.now.return_value = _make_dt(9, 0)
        assert engine.consume_planned_window_exit_edge() is False  # period active: first look, no edge
        dt.now.return_value = _make_dt(10, 5)
        assert engine.consume_planned_window_exit_edge() is True  # just ended
        assert engine.consume_planned_window_exit_edge() is False  # consumed
        dt.now.return_value = _make_dt(11, 0)
        assert engine.consume_planned_window_exit_edge() is False


def test_edge_fires_when_the_recommendation_is_cleared_inside_the_period():
    engine = _engine_in_period()
    with patch("custom_components.climate_advisor.automation.dt_util") as dt:
        dt.now.return_value = _make_dt(8, 0)
        assert engine.consume_planned_window_exit_edge() is False
        engine._current_classification.windows_recommended = False  # the live gate / a reclassification
        assert engine.consume_planned_window_exit_edge() is True


def test_no_edge_when_the_period_was_never_active():
    engine = _engine_in_period()
    with patch("custom_components.climate_advisor.automation.dt_util") as dt:
        dt.now.return_value = _make_dt(12, 0)
        assert engine.consume_planned_window_exit_edge() is False
        assert engine.consume_planned_window_exit_edge() is False


def test_window_left_open_past_the_end_of_the_period_is_paused_without_waiting_for_a_cycle():
    engine = _engine_in_period()
    engine.hass.states.get.return_value = _make_state("cool")
    engine._sensor_check_callback = lambda: True  # a monitored window is open, debounce long settled
    with patch("custom_components.climate_advisor.automation.dt_util") as dt:
        dt.now.return_value = _make_dt(9, 55)
        # inside the period the lazy reconcile is exempt: the window is expected
        asyncio.run(engine._sync_paused_by_door_with_live_sensors())
        assert engine._paused_by_door is False
        assert engine.consume_planned_window_exit_edge() is False

        dt.now.return_value = _make_dt(10, 5)
        assert engine.consume_planned_window_exit_edge() is True
        asyncio.run(engine.async_sync_after_planned_window_exit())
    assert engine._paused_by_door is True, "the open window must be paused as soon as the period ends"
