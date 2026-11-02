"""Issue #1227 follow-up: "open and past its delay" is decided per sensor, never house-wide.

A window open for hours stays open while a second door is still inside its own delay. The first #1227 form
("any sensor holding open AND no sensor debouncing") hid window A whenever door B was debouncing, so a
nat-vent exit read "sensors closed", re-armed cooling and the AC ran into window A (rule 3).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

DEBOUNCE_S = 600
EXIT_AT_S = 540  # outdoor-rise exit lands about 9 min after outdoor jumps (measured with no door)
BASE = {
    "fan": "hvac_fan",
    "profile": "scripted",
    "contacts": ["window", "door"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": DEBOUNCE_S},
}


@pytest.mark.parametrize("door_b_opened", [False, True], ids=["window_only", "door_b_debouncing"])
async def test_window_open_past_its_delay_stays_open_while_another_door_debounces(make_house, door_b_opened):
    """Window A ventilating; outdoor rises and ends the session while door B is 4 min into its delay.

    Rule 3: window A is open throughout, so the thermostat must never be armed to heat or cool.
    """
    house = await make_house(**BASE)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    await house.open("window")
    await house.advance(seconds=DEBOUNCE_S + 60)
    assert engine._natural_vent_active

    house.set_outdoor(80.0, high_f=82.0, low_f=62.0)
    t0 = dt_util.utcnow()
    opened = False
    for _ in range(120):
        if door_b_opened and not opened and (dt_util.utcnow() - t0).total_seconds() >= EXIT_AT_S - 240:
            await house.open("door")
            opened = True
        await house.advance(seconds=30)
        if not engine._natural_vent_active:
            break
    assert not engine._natural_vent_active, "the outdoor-rise exit never fired"

    if opened:
        await house.close_contact("door")  # door B closes before its delay: it never counted as open
    house.set_indoor(79.0)
    for minutes in (0.5, 1, 4, 5, 10):
        await house.advance(seconds=int(minutes * 60))
        assert house.thermostat.mode == "off", f"HVAC armed ({house.thermostat.mode}) into window A, which is open"
