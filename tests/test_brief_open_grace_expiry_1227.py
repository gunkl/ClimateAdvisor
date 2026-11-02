"""Issue #1227: a door/window opened for less than its delay must never pause or start ventilation.

Occupant story: someone opens a door for a minute while one of Climate Advisor's own grace periods happens to end.
The 600 s delay exists so a pass-through never changes the HVAC; the grace expiry used to read the raw sensor and
paused the HVAC (and started the blower) for a door that had been open for seconds. REAL ``AutomationEngine``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.test_door_pause_recorded_1132 import _engine
from tests.test_open_window_decision_owner_1142 import _expire_grace


def _blip(engine, *, pending: bool) -> None:
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: pending


class TestGraceExpiryHonoursTheDelay:
    def test_grace_ending_inside_the_delay_does_not_pause(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _blip(engine, pending=True)
        engine._re_pause_for_open_sensor = MagicMock(return_value="repause-task")
        _expire_grace(engine)
        assert not engine._re_pause_for_open_sensor.called, "no re-pause / ventilation start for a blip"
        assert engine._paused_by_door is False, "a sensor inside its delay is not open yet"
        assert engine._grace_active is False, "the grace still ends normally"
        assert engine._test_state.state == "cool", "the thermostat is untouched"

    def test_control_open_past_the_delay_still_re_pauses(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _blip(engine, pending=False)
        engine.config.pop("fan_mode", None)  # no ventilation: the settled open window is a pause
        _expire_grace(engine)
        assert engine._paused_by_door is True

    def test_settled_open_helper_is_the_debounce_safe_read(self):
        engine = _engine(mode="cool", indoor=72.0, outdoor=65.0)
        _blip(engine, pending=True)
        assert engine._any_monitored_sensor_holding_open() is True
        assert engine._any_monitored_sensor_settled_open() is False
        _blip(engine, pending=False)
        assert engine._any_monitored_sensor_settled_open() is True
