"""Issue #1389 (real HA): a restart must not stop the whole-house fan because a sensor is still loading.

Occupant story (2026-11-06, 17:15): the windows are open and the whole-house fan is cooling the house. Home
Assistant restarts. The door/window sensor (a group helper in production) reads "unavailable" from the shutdown
until it loads, ~1.5 minutes into the start. Climate Advisor counted the dark sensor as closed after 60 s -- while
Home Assistant was still starting -- called the house sealed, sent the fan off and e-mailed "door/window sensor
unavailable". After the fix the 60 s grace starts when Home Assistant has finished starting.

Real HA owns everything the fix depends on: ``hass.is_running`` during setup, ``async_at_started`` and
``EVENT_HOMEASSISTANT_STARTED``, and the 15 s safety tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

WINDOW = "binary_sensor.house_window"
BASE = {
    "fan": "whf",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 73.0,
    "outdoor_f": 64.0,
    "high_f": 72.0,
    "low_f": 60.0,
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
}


def _ca_fan_offs(house) -> list:
    return [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_off"]


async def _ventilating(make_house):
    house = await make_house(**BASE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=120)
    assert engine._natural_vent_active and house.fan.is_on, "precondition: the open window started free cooling"
    return house


async def _restart_during_boot(house) -> None:
    """Restart Climate Advisor as Home Assistant's own start does: set up before HA is running."""
    house.hass.states.async_set(WINDOW, "unavailable")  # shutdown: the sensor is dark from here
    house.hass.set_state(CoreState.not_running)
    await house.restart(settle=False)


async def _home_assistant_started(house) -> None:
    house.hass.set_state(CoreState.running)
    house.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await house.advance(seconds=1)


async def test_a_sensor_still_loading_at_restart_does_not_stop_the_fan(make_house) -> None:
    house = await _ventilating(make_house)
    offs_before = len(_ca_fan_offs(house))
    await _restart_during_boot(house)

    # Production: HA was still starting 60 s after Climate Advisor, and the group loaded at ~96 s.
    await house.advance(seconds=100)
    assert len(_ca_fan_offs(house)) == offs_before, (
        "#1389: the fan was stopped as a 'sealed house' while Home Assistant was still starting"
    )
    house.hass.states.async_set(WINDOW, "on")  # the sensor loads: the window is open
    await _home_assistant_started(house)
    await house.advance(seconds=120)

    assert house.fan.is_on, "the fan ventilating the open house was stopped by the restart"
    assert len(_ca_fan_offs(house)) == offs_before, "Climate Advisor sent the fan off across the restart"


async def test_a_sensor_still_dark_after_home_assistant_started_is_still_acted_on(make_house) -> None:
    """Control: a sensor that never comes back is still counted closed 60 s after HA has started (rule 1)."""
    house = await _ventilating(make_house)
    offs_before = len(_ca_fan_offs(house))
    await _restart_during_boot(house)
    await house.advance(seconds=100)
    assert len(_ca_fan_offs(house)) == offs_before, "precondition: nothing stops the fan while HA is starting"

    await _home_assistant_started(house)
    await house.advance(seconds=30)
    assert len(_ca_fan_offs(house)) == offs_before, "inside the 60 s grace after HA started"
    await house.advance(seconds=60)
    assert len(_ca_fan_offs(house)) > offs_before, "a sensor dead after HA started must still stop the fan (rule 1)"


async def test_a_reconcile_turn_off_deferred_by_the_toggle_floor_still_lands(make_house) -> None:
    """Issue #1390: the reconcile's fan-off, deferred by the 5-minute floor, is re-run and lands (real timers).

    Production: the #1389 stop at 17:16 started the floor; at 17:20 the startup reconcile wanted the fan off (the
    house had cooled below the comfort floor), was deferred to 17:21:25, and nothing ever sent it. The real
    coordinator's retry re-reads the fan and the window from HA and decides again.
    """
    house = await _ventilating(make_house)
    engine = house.coordinator.automation_engine
    offs_before = len(_ca_fan_offs(house))
    # Restart state: the nat-vent session is gone, the fan still runs; a CA toggle 220 s ago started the floor.
    engine._natural_vent_active = False
    engine._fan_active = False
    engine._fan_toggle_command_time = dt_util.now() - timedelta(seconds=220)
    house.set_indoor(69.4)  # below the 70 comfort floor: free cooling is no longer warranted
    await engine.reconcile_fan_on_startup(
        indoor=69.4, outdoor=64.0, thermostat_fan_running=True, any_sensor_open=True, trigger="ha_restart"
    )
    await house.advance(seconds=30)
    assert len(_ca_fan_offs(house)) == offs_before, "precondition: the 5-minute floor defers the turn-off"
    assert house.fan.is_on

    await house.advance(seconds=90)  # the floor has passed
    assert len(_ca_fan_offs(house)) > offs_before, "#1390: the deferred fan-off never went out"
    assert not house.fan.is_on, "the fan was left running, unmanaged, after its deferred turn-off"
