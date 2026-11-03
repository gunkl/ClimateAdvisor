"""Issue #1289: the nat-vent START read is "one sensor reads open (not dark) past its own delay", per sensor.

Occupant story: window A has been open for hours; door B is opened briefly. Free cooling must restart for window A
on schedule -- door B inside its own delay is irrelevant to "is the house open?". A dark (unavailable) sensor never
starts the fan (Issue #1181), even when its last real reading was open.
"""

from __future__ import annotations

from tests.test_door_pause_recorded_1132 import _engine
from tests.test_settled_open_per_sensor_1227 import DOOR, WINDOW, _coordinator


def test_a_settled_window_counts_while_another_door_is_inside_its_delay():
    coord = _coordinator({WINDOW: ("on", 7200), DOOR: ("on", 60)}, timers=(DOOR,))
    assert coord._sensor_debounce_pending() is True  # the old house-wide gate hid window A
    assert coord._any_sensor_open_and_settled() is True


def test_a_door_inside_its_delay_alone_does_not_count():
    coord = _coordinator({WINDOW: ("off", 7200), DOOR: ("on", 60)}, timers=(DOOR,))
    assert coord._any_sensor_open_and_settled() is False


def test_a_just_opened_door_before_its_timer_registers_does_not_count():
    """#623 race: last_changed alone marks it as still inside its delay."""
    coord = _coordinator({DOOR: ("on", 5)})
    assert coord._any_sensor_open_and_settled() is False


def test_a_dark_window_whose_last_reading_was_open_never_counts():
    """#1181: dark holds a pause (holding read) but never starts anything (this read)."""
    coord = _coordinator({WINDOW: ("unavailable", 7200), DOOR: ("on", 60)}, timers=(DOOR,))
    coord._sensor_last_readings = lambda: {WINDOW: "open"}
    assert coord._any_sensor_settled_open() is True  # holding-based: must NOT be used for the start gate
    assert coord._any_sensor_open_and_settled() is False


def test_everything_closed_does_not_count():
    coord = _coordinator({WINDOW: ("off", 900), DOOR: ("off", 30)})
    assert coord._any_sensor_open_and_settled() is False


def test_engine_reads_the_coordinator_callback_and_falls_back_house_wide_without_one():
    engine = _engine(mode="off", indoor=72.0, outdoor=65.0)
    engine._sensor_check_callback = lambda: True
    engine._sensor_debounce_pending_callback = lambda: True
    engine._sensor_open_settled_callback = None
    assert engine._any_monitored_sensor_open_and_settled() is False  # no coordinator: house-wide fallback
    engine._sensor_open_settled_callback = lambda: True
    assert engine._any_monitored_sensor_open_and_settled() is True  # per-sensor read wins when wired


def test_pause_held_start_refusal_needs_a_live_open_sensor_and_never_refuses_without_a_coordinator():
    engine = _engine(mode="off", indoor=72.0, outdoor=65.0)
    engine._sensor_open_settled_callback = None
    assert engine._no_live_open_sensor_for_start() is False  # bare engine / simulator: previous behavior
    engine._sensor_open_settled_callback = lambda: False  # only a dark sensor, or a door inside its delay
    assert engine._no_live_open_sensor_for_start() is True
    engine._sensor_open_settled_callback = lambda: True
    assert engine._no_live_open_sensor_for_start() is False
