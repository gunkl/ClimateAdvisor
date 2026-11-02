"""Issue #1227 follow-up: the coordinator decides "holds open and past its own delay" per sensor."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

WINDOW = "binary_sensor.window_a"
DOOR = "binary_sensor.door_b"


def _coordinator(states: dict[str, tuple[str, float]], timers: tuple[str, ...] = ()) -> ClimateAdvisorCoordinator:
    """A bare coordinator over the REAL per-sensor methods; states map entity -> (state, seconds since change)."""
    coord = ClimateAdvisorCoordinator.__new__(ClimateAdvisorCoordinator)
    now = dt_util.now()
    coord.hass = MagicMock()
    coord.hass.states.get = lambda eid: (
        SimpleNamespace(state=states[eid][0], last_changed=now - timedelta(seconds=states[eid][1]), attributes={})
        if eid in states
        else None
    )
    coord.config = {"sensor_debounce_seconds": 600}
    coord._resolved_sensors = list(states)
    coord._door_open_timers = {eid: object() for eid in timers}
    coord._sensor_reconnect_blip_last_changed = {}
    coord._sensor_polarity_inverted = {}
    coord._state_is_open = lambda st: st is not None and st.state == "on"
    coord._sensor_real_reading = lambda st: None if st is None or st.state in ("unavailable", "unknown") else st.state
    coord._sensor_last_readings = lambda: {}
    return coord


def test_a_settled_window_counts_while_another_door_is_inside_its_delay():
    coord = _coordinator({WINDOW: ("on", 7200), DOOR: ("on", 60)}, timers=(DOOR,))
    assert coord._sensor_debounce_pending() is True  # the house-wide form would hide window A
    assert coord._any_sensor_settled_open() is True


def test_a_door_inside_its_delay_alone_is_not_settled_open():
    coord = _coordinator({WINDOW: ("off", 7200), DOOR: ("on", 60)}, timers=(DOOR,))
    assert coord._any_sensor_settled_open() is False


def test_a_just_opened_door_before_its_timer_registers_is_not_settled_open():
    """#623 race: last_changed alone marks it as still inside the delay."""
    coord = _coordinator({DOOR: ("on", 5)})
    assert coord._any_sensor_settled_open() is False


def test_a_window_open_past_its_delay_is_settled_open():
    coord = _coordinator({WINDOW: ("on", 900)})
    assert coord._any_sensor_settled_open() is True


def test_everything_closed_is_not_settled_open():
    coord = _coordinator({WINDOW: ("off", 900), DOOR: ("off", 30)})
    assert coord._any_sensor_settled_open() is False
