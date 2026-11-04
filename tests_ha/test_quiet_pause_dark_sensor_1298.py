"""Issue #1298 (real HA): a door/window pause held only by a dark sensor is announced, not kept quiet.

Occupant story: on a cool evening with the AC running, automation is switched off (observe-only) and a window is
opened. Nothing is sent (#1293), so the AC keeps running. The window sensor then goes dark (unavailable, last
reading open). When automation is switched back on, the re-enable reconcile pauses the AC for the window (rule 3:
a dark sensor whose last reading was open holds a pause, #1181). Before the fix the quiet-pause prediction
(``_fan_session_about_to_take_over``) said "a fan session is about to take over" and suppressed the "HVAC paused"
message -- but a dark sensor never STARTS a fan (#1181/#1289), so the AC stayed off, no fan ran, and the occupant
was never told why (G5).

Control: the same evening with the window really open (live). The fan session does take over, so the quiet pause is
still right (no "HVAC paused" message seconds before free cooling clears it).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

DEB = 60
BASE = {
    "fan": "whf",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 75.0,
    "outdoor_f": 64.0,
    "high_f": 70.0,
    "low_f": 60.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": DEB},
}


def _spy_notices(engine) -> list[tuple[str, str]]:
    """Every notification Climate Advisor asks to send, as (type, message)."""
    sent: list[tuple[str, str]] = []
    real_notify = engine._notify

    async def _spy(message, title, notification_type, **kwargs):
        sent.append((notification_type, message))
        return await real_notify(message, title, notification_type, **kwargs)

    engine._notify = _spy
    return sent


def _pause_notices(sent) -> list[str]:
    return [m for t, m in sent if t == "door_window_pause"]


def _quiet_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "pause will be quiet" in r.getMessage()]


async def _window_opened_in_observe_only(make_house):
    house = await make_house(**BASE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the start-up window
    assert house.thermostat.mode == "cool", "precondition: Climate Advisor's cooling runs"
    await house.set_automation(False)
    await house.advance(minutes=1)
    # The house is at the comfort floor when the window opens, so no fan session (not even an observe-only one)
    # starts. It warms again later (``_house_warms``) and free cooling becomes possible.
    house.set_indoor(69.0)
    await house.open("window")
    await house.advance(seconds=DEB + 60)
    assert house.thermostat.mode == "cool", "precondition: observe-only sent nothing"
    assert not engine._paused_by_door and not engine._natural_vent_active and not house.fan.is_on
    return house, engine


async def _house_warms(house) -> None:
    house.set_indoor(75.0)
    await house.advance(seconds=30)


async def test_dark_window_pause_on_reenable_is_announced(make_house, caplog) -> None:
    caplog.set_level("INFO", logger="custom_components.climate_advisor")
    house, engine = await _window_opened_in_observe_only(make_house)

    house.hass.states.async_set("binary_sensor.house_window", "unavailable")  # dark, last reading open
    await house.advance(seconds=30)
    await _house_warms(house)
    caplog.clear()
    sent = _spy_notices(engine)

    await house.set_automation(True)
    for _ in range(4):  # ~2 min: the reconcile runs at once; a fan session would start within this
        await house.advance(seconds=30)

    assert house.thermostat.mode == "off", "rule 3: the dark window (last open) must pause the cooling"
    assert engine._paused_by_door, "rule 3: the pause is recorded"
    assert not house.fan.is_on and not engine._natural_vent_active, "#1181: a dark sensor never starts the fan"
    assert not _quiet_logs(caplog), f"G5: pause silenced for a fan session that can never start: {_quiet_logs(caplog)}"
    assert _pause_notices(sent), "G5: the occupant was never told the cooling is paused"


async def test_live_window_pause_on_reenable_stays_quiet_and_fan_takes_over(make_house, caplog) -> None:
    caplog.set_level("INFO", logger="custom_components.climate_advisor")
    house, engine = await _window_opened_in_observe_only(make_house)
    await _house_warms(house)
    caplog.clear()
    sent = _spy_notices(engine)

    await house.set_automation(True)
    for _ in range(4):
        await house.advance(seconds=30)

    assert house.thermostat.mode == "off", "rule 3: the open window pauses the cooling"
    assert engine._natural_vent_active and house.fan.is_on, "free cooling takes over for the live open window"
    assert _quiet_logs(caplog), "control: the reconcile's pause should have been predicted quiet"
    assert not _pause_notices(sent), "a fan session took over: no 'HVAC paused' message expected"
