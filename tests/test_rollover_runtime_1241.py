"""Issue #1241: an HVAC run that crosses the 23:59 rollover keeps its post-rollover runtime.

What the occupant saw: the furnace ran 23:00-01:00 and the dashboard credited 59 min to the first day
and 0 min to the next, instead of about 61. Every overnight cycle crossing the rollover was
undercounted (the fan-only counter the same way), and the live "HVAC runtime today" read 0 for the
rest of the run.

Cause (since 239cc20; 55c81ff/#912 extended it to the fan-only timer and session mode):
``_async_end_of_day`` flushed the session (crediting today's part and resetting the timer to now) and
then cleared ``_hvac_on_since`` / ``_thermostat_fan_only_on_since`` / ``_hvac_session_mode``, so the
real off-edge the next day had nothing to flush.

Mirror of #1240 (which stopped crediting idle time). The clear was also the only daily bound on a
session whose off-edge was missed, so the rollover now keeps the session only while the thermostat
still reports it running by the session-edge rule; those guard cases are tested here too.

These tests drive the REAL ``_async_thermostat_changed``, ``_async_end_of_day`` and live getters.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.learning import DailyRecord
from tests.test_hvac_session_detection import (
    _make_state,
    _make_thermostat_coord,
    _make_thermostat_event,
)

_DT = "custom_components.climate_advisor.coordinator.dt_util"

# session mode -> (thermostat state while running, DailyRecord field, live getter)
_RUNNING = {
    "heat": (("heat", "heating"), "hvac_runtime_minutes", "get_hvac_runtime_today"),
    "cool": (("cool", "cooling"), "hvac_runtime_minutes", "get_hvac_runtime_today"),
    "fan_only": (("fan_only", "fan"), "thermostat_fan_only_runtime_minutes", "get_thermostat_fan_only_runtime_today"),
}


def _t(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, day, hour, minute, 0)


def _fire(coord, when: datetime, old: tuple[str, str], new: tuple[str, str]) -> None:
    event = _make_thermostat_event(_make_state(*old), _make_state(*new))
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = when
        asyncio.run(coord._async_thermostat_changed(event))


def _coord():
    coord = _make_thermostat_coord()
    coord._today_record = DailyRecord(date="2026-04-08", day_type="cold", trend_direction="stable")
    coord._async_end_of_day = types.MethodType(ClimateAdvisorCoordinator._async_end_of_day, coord)
    coord._outdoor_temp_history = []
    coord._indoor_temp_history = []
    coord._hourly_forecast_temps = []
    coord._refresh_hourly_forecast = AsyncMock()
    coord._async_save_state = AsyncMock()
    coord._cancel_pre_cool_trigger = MagicMock()
    coord._emit_event = MagicMock()
    coord.learning = MagicMock()
    coord._executor_job = AsyncMock()
    return coord


def _rollover(coord, thermostat_now: tuple[str, str] | None) -> float:
    """Run the 23:59 end-of-day with the thermostat in ``thermostat_now``; return the day-1 record's value."""
    coord.hass.states.get = MagicMock(return_value=_make_state(*thermostat_now) if thermostat_now else None)
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = _t(8, 23, 59)
        asyncio.run(coord._async_end_of_day(_t(8, 23, 59)))
    return coord.learning.record_day.call_args.args[0]


def _new_day_record(coord) -> None:
    """What the first post-midnight classification cycle does (``_ensure_today_record``, new date)."""
    coord._today_record = DailyRecord(date="2026-04-09", day_type="cold", trend_direction="stable")


def _live(coord, getter: str, when: datetime) -> float:
    with patch(_DT) as mock_dt:
        mock_dt.now.return_value = when
        return getattr(coord, getter)()


class TestSessionCrossingRolloverKeepsItsRuntime:
    @pytest.mark.parametrize("mode", ["heat", "cool", "fan_only"])
    def test_run_23_00_to_01_00_credits_59_then_61(self, mode):
        running, field, getter = _RUNNING[mode]
        coord = _coord()
        _fire(coord, _t(8, 23), (running[0], "idle"), running)  # equipment starts 23:00

        day1 = _rollover(coord, running)
        assert getattr(day1, field) == pytest.approx(59.0)
        assert coord._hvac_session_mode == mode, "a session still running at 23:59 must carry over"

        _new_day_record(coord)
        assert _live(coord, getter, _t(9, 0, 30)) == pytest.approx(31.0), "live getter after the rollover"

        _fire(coord, _t(9, 1), running, (running[0], "idle"))  # equipment stops 01:00
        assert getattr(coord._today_record, field) == pytest.approx(61.0)
        assert coord._hvac_session_mode is None

    def test_live_getter_counts_from_23_59_before_the_new_record_exists(self):
        """Between 23:59 and the first post-midnight cycle there is no record; the run still counts."""
        running, _, getter = _RUNNING["heat"]
        coord = _coord()
        _fire(coord, _t(8, 23), ("heat", "idle"), running)
        _rollover(coord, running)
        assert coord._today_record is None
        assert _live(coord, getter, _t(9, 0, 9)) == pytest.approx(10.0)

    def test_thermal_observation_still_ends_at_the_real_off_edge(self):
        """The observation never relied on the clear: the off-edge ends its active phase after midnight."""
        running, _, _ = _RUNNING["heat"]
        coord = _coord()
        _fire(coord, _t(8, 23), ("heat", "idle"), running)
        coord._start_hvac_observation.assert_called_once_with("heat")
        _rollover(coord, running)
        _new_day_record(coord)
        coord._end_hvac_active_phase.reset_mock()
        _fire(coord, _t(9, 1), running, ("heat", "idle"))
        assert coord._end_hvac_active_phase.called
        coord._start_hvac_observation.assert_called_once()  # no second observation was opened


class TestStaleSessionStillResetDaily:
    """The clear used to bound a session whose off-edge was missed; the rollover still does that."""

    @pytest.mark.parametrize("mode", ["heat", "cool", "fan_only"])
    @pytest.mark.parametrize("thermostat_now", [("heat", "idle"), ("off", "off"), ("off", ""), None])
    def test_session_not_running_at_23_59_is_ended(self, mode, thermostat_now):
        running, field, getter = _RUNNING[mode]
        coord = _coord()
        _fire(coord, _t(8, 23), (running[0], "idle"), running)
        # The off-edge is missed (e.g. dropped during startup coalescing): the timer is still armed.
        _rollover(coord, thermostat_now)

        assert coord._hvac_session_mode is None
        assert coord._hvac_on_since is None
        assert coord._thermostat_fan_only_on_since is None
        _new_day_record(coord)
        assert _live(coord, getter, _t(9, 6)) == 0.0
        assert getattr(coord._today_record, field) == 0.0

    def test_ca_ventilation_under_a_cool_mode_is_not_a_running_session(self):
        """Issue #986 rule: CA's own fan/nat-vent under hvac_mode cool (action "fan") is not a session."""
        coord = _coord()
        _fire(coord, _t(8, 23), ("off", ""), ("cool", ""))  # no-signal thermostat: mode fallback session
        assert coord._hvac_session_mode == "cool"
        coord.automation_engine._natural_vent_active = True
        _rollover(coord, ("cool", "fan"))
        assert coord._hvac_session_mode is None
        assert coord._hvac_on_since is None

    def test_no_signal_thermostat_session_carries_over(self):
        """#93 fallback: a thermostat with no hvac_action and mode heat is a running session; keep it."""
        coord = _coord()
        _fire(coord, _t(8, 23), ("off", ""), ("heat", ""))
        _rollover(coord, ("heat", ""))
        assert coord._hvac_session_mode == "heat"
        _new_day_record(coord)
        _fire(coord, _t(9, 0, 30), ("heat", ""), ("off", ""))
        assert coord._today_record.hvac_runtime_minutes == pytest.approx(31.0)

    def test_no_session_rollover_stays_clean(self):
        coord = _coord()
        _rollover(coord, ("heat", "heating"))  # thermostat running but CA never saw an on-edge
        assert coord._hvac_session_mode is None
        assert coord._hvac_on_since is None
