"""Issue #1141: ending natural ventilation with a window still open records the pause only once the
fan-off command actually ran.

Occupant framing: nat-vent ends (outdoor warmed up / cooled to the floor) while a window is open. The
whole-house-fan off command can be held back by the 5-minute fan-toggle floor. The session is then
correctly KEPT (#931: the fan is still physically on), but the exit also recorded the window pause, so
Status, the Activity Log and the routines saw "paused for the window" and "ventilating" at once -- a
combination the door/window and nat-vent FSMs document as unreachable.

Owner clarification (2026-10-31): recording a pause when nat-vent ends with a window open is CORRECT,
planned window or not (HVAC stays off while a window is open). This test file therefore does NOT test
the planned window; it tests only the deferred-fan-off ordering, in both orders.

REAL ``AutomationEngine`` + the state-following fake thermostat from ``tests.test_restore_resolved_band``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.const import CONF_FAN_ENTITY
from tests.test_door_pause_recorded_1132 import _AUTOMATION, _DAY, _engine, _run

_REASON = "outdoor warmed past the threshold"


def _ventilating_engine(*, sensor_open: bool = True, pre_fan_mode: str = "cool"):
    """A running nat-vent session (WHF on, HVAC suppressed to off) with a window open."""
    engine = _engine(mode="off", sensor_open=sensor_open)
    engine.config[CONF_FAN_ENTITY] = "fan.whf"
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._fan_on_since = _DAY - timedelta(minutes=20)
    engine._pre_fan_hvac_mode = pre_fan_mode
    # a fan toggle 10 s ago: a deactivate now falls inside the 5-minute floor
    engine._fan_toggle_command_time = _DAY - timedelta(seconds=10)
    return engine


def _exit(engine, *, at=_DAY):
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=at):
        return _run(engine._exit_nat_vent(reason=_REASON, set_outdoor_exit_time=True))


def _floor_over(engine):
    """The 5-minute floor has passed (the deferred fan-off may now run)."""
    engine._fan_toggle_command_time = _DAY - timedelta(seconds=400)


class TestDeferredFanOffRecordsNoPauseYet:
    @pytest.mark.parametrize("pre_fan_mode", ["cool", "heat"])
    def test_rate_limited_off_keeps_session_and_records_no_pause(self, pre_fan_mode):
        engine = _ventilating_engine(pre_fan_mode=pre_fan_mode)
        _exit(engine)
        assert engine._natural_vent_active is True, "fan-off deferred: the session stays (#931)"
        assert engine._paused_by_door is False, "session and window pause must never both be recorded"
        assert engine._paused_entity is None
        assert engine._pre_pause_mode is None

    def test_executed_off_records_pause(self):
        """Positive control / mirror: the fan-off ran -> the session ends and the pause is recorded."""
        engine = _ventilating_engine()
        _floor_over(engine)
        _exit(engine)
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._paused_entity == "nat-vent-exit"

    def test_sensors_closed_deferred_off_starts_no_grace_until_the_fan_stops(self):
        """Mirror (sensors-closed branch): a deferred fan-off keeps the session, so the
        "nat_vent_exit_resume" automation grace must not start while the fan is still on."""
        engine = _ventilating_engine(sensor_open=False)
        _exit(engine)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        assert engine._grace_active is False, "session and grace must not both be active"
        assert engine._pre_fan_hvac_mode == "cool", "WHF still owns the thermostat until the fan stops"

    def test_sensors_closed_retry_after_floor_ends_session_and_starts_grace(self):
        """Positive control / completion: once the fan-off can run, session ends and grace starts."""
        engine = _ventilating_engine(sensor_open=False)
        _exit(engine)
        _floor_over(engine)
        _exit(engine, at=_DAY + timedelta(minutes=6))
        assert engine._natural_vent_active is False
        assert engine._grace_active is True


class TestBothOrders:
    def test_deferred_off_runs_first_then_pause_is_recorded_and_close_restores(self):
        """Order 1: the retry after the floor ends the session AND records the pause; closing restores."""
        engine = _ventilating_engine()
        _exit(engine)  # deferred
        assert engine._paused_by_door is False
        _floor_over(engine)
        _exit(engine, at=_DAY + timedelta(minutes=6))  # the next tick's retry
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._test_state.state == "off", "no HVAC into the open window"
        engine._sensor_check_callback = lambda: False
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY + timedelta(minutes=30)):
            _run(engine.handle_all_doors_windows_closed())
        assert engine._paused_by_door is False

    def test_window_closes_first_then_normal_sensors_closed_exit(self):
        """Order 2: the window closes before the deferred off runs -> no pause is ever recorded and the
        closed-window exit completes the session (restore + grace), nothing is stranded."""
        engine = _ventilating_engine()
        _exit(engine)  # deferred
        engine._sensor_check_callback = lambda: False
        _floor_over(engine)
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY + timedelta(minutes=6)):
            _run(engine.handle_all_doors_windows_closed())
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is False
        assert engine._pre_fan_hvac_mode is None, "WHF ownership of the thermostat is released"


class TestRealTickRetryCompletesTheExit:
    """The retry is the REAL slow-loop tick (``check_natural_vent_conditions()``), not a direct call: an
    outdoor-rise exit that is deferred by the fan-toggle floor is completed by a later tick."""

    def _tick(self, engine, at):
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=at):
            _run(engine.check_natural_vent_conditions())

    def test_deferred_outdoor_rise_exit_is_completed_by_a_later_tick(self):
        engine = _ventilating_engine()
        engine._last_outdoor_temp = 90.0  # outdoor now far above indoor 72 -> OUTDOOR_RISE / ceiling exit
        self._tick(engine, _DAY)  # arms the sustain-confirmation clock
        assert engine._natural_vent_active is True
        self._tick(engine, _DAY + timedelta(seconds=100))  # confirmed -> exit, fan-off deferred (floor)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False
        self._tick(engine, _DAY + timedelta(seconds=400))  # floor over -> the retry ends the session
        assert engine._natural_vent_active is False
        assert engine._paused_by_door is True
        assert engine._paused_entity == "nat-vent-exit"
