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


# ---------------------------------------------------------------------------
# The coordinator's real 5-minute tick drives the exit reconcile (the trigger, not just the engine half)
# ---------------------------------------------------------------------------


def _tick_coordinator(engine):
    """A bare coordinator whose REAL ``_async_thermal_sample_tick`` / exit-check methods run against ``engine``.

    ``@callback`` is the identity in a private copy of the class (the lightweight HA stub swallows it, which
    would make the tick unreachable); everything the tick calls besides the exit check is stubbed.
    """
    import types
    from unittest.mock import MagicMock

    from tests.test_promise_wiring import TestRealEntryPointsFeedThePromises

    cls = TestRealEntryPointsFeedThePromises._callback_transparent_class()
    coord = object.__new__(cls)
    coord.automation_engine = engine
    coord._refresh_outdoor_temp = MagicMock()
    coord._tick_household_promises = MagicMock()
    coord._refresh_indoor_sensor_health = MagicMock()
    coord._get_indoor_temp = MagicMock(return_value=None)
    coord._sample_all_observations = MagicMock()
    created: list = []

    def _spawn(coro, **_kw):
        task = asyncio.ensure_future(coro)
        created.append(task)
        return task

    coord._spawn = _spawn
    for name in (
        "_async_thermal_sample_tick",
        "_schedule_planned_window_exit_check",
        "_async_planned_window_exit_sync",
    ):
        setattr(coord, name, types.MethodType(getattr(cls, name), coord))
    return coord, created


def test_the_real_tick_pauses_a_window_left_open_when_the_period_ends_and_notifies_once():
    """Occupant: a window is open (past the debounce) when the 10:00 close time passes, with the AC armed.
    The house must be paused by the 5-minute tick that crosses the close time, not by the next 30-minute cycle,
    and the occupant is told once, however many ticks follow."""
    engine = _engine_in_period()
    engine.hass.states.get.return_value = _make_state("cool")
    engine._sensor_check_callback = lambda: True

    async def _run():
        coord, created = _tick_coordinator(engine)
        with patch("custom_components.climate_advisor.automation.dt_util") as dt:
            dt.now.return_value = _make_dt(9, 55)
            coord._async_thermal_sample_tick(dt.now.return_value)  # in period: remembers it was active
            await asyncio.gather(*created)
            assert engine._paused_by_door is False, "inside the period the open window is expected"

            dt.now.return_value = _make_dt(10, 5)
            coord._async_thermal_sample_tick(dt.now.return_value)  # crosses the close time
            await asyncio.gather(*created)
            assert engine._paused_by_door is True, "the tick crossing the close time must pause the open window"
            assert len(created) == 1, "exactly one reconcile for the one edge"
            calls_after_first = engine.hass.services.async_call.await_count

            for minute in (10, 15, 20):  # later ticks: no edge, nothing repeats
                dt.now.return_value = _make_dt(10, minute)
                coord._async_thermal_sample_tick(dt.now.return_value)
            await asyncio.gather(*created)
            assert len(created) == 1
            assert engine.hass.services.async_call.await_count == calls_after_first, "no duplicate action/notification"

    asyncio.run(_run())
