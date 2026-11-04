"""Real-HA tests for Issue #1314: a dashboard Resume holds the open window un-paused for its whole grace.

Occupant story: a hot evening, a window is open and the AC is paused for it. The occupant presses Resume on the
dashboard because they want the AC with the window open. Climate Advisor restores it and starts the manual
"dashboard_resume" grace, then about 5 minutes later paused the AC again (the next cycle's live-sensor reconcile and
the comfort-band safety check ignored that grace), overriding the occupant's explicit choice.

Rule 3 lets a recognised manual action run HVAC into an open window, and grace-periods-spec's state table says a
manual / dashboard-resume grace is not re-paused (a NEW window open during it is suppressed too). Grace expiry then
re-pauses a window still open. Control: without a Resume the open window pauses as before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.api import ClimateAdvisorResumeFromPauseView

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60


def _hot(contacts: list[str]) -> dict:
    return {
        "fan": None,
        "profile": "scripted",
        "contacts": contacts,
        "startup": "settled",
        "start": datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        "initial_indoor_f": 76.0,
        "outdoor_f": 92.0,
        "high_f": 95.0,
        "low_f": 70.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
    }


async def _press_dashboard_resume(house) -> dict:
    request = SimpleNamespace(
        app={"hass": house.hass}, query={}, method="POST", path="/api/climate_advisor/resume_from_pause", remote="t"
    )
    response = await ClimateAdvisorResumeFromPauseView().post(request)
    await house.advance(seconds=1)
    return json.loads(response.text)


async def _paused_then_resumed(make_house, contacts: list[str]):
    house = await make_house(**_hot(contacts))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"
    await _press_dashboard_resume(house)
    await house.advance(seconds=5)
    assert house.thermostat.mode == "cool" and not engine._paused_by_door, "precondition: the Resume restored the AC"
    assert engine._grace_active and engine._last_grace_trigger == "dashboard_resume"
    return house, engine


async def _held_through_grace(house, engine) -> None:
    """Minute by minute until the grace ends: never re-paused, the AC stays as the occupant chose."""
    for minute in range(120):
        if not engine._grace_active:
            return
        assert not engine._paused_by_door, f"re-paused {minute} min into the occupant's Resume grace"
        assert house.thermostat.mode == "cool", f"AC switched {house.thermostat.mode} {minute} min into the grace"
        await house.advance(minutes=1)
    raise AssertionError("the Resume grace never ended")


async def test_resume_with_window_open_is_held_for_the_whole_grace_then_repaused(make_house) -> None:
    house, engine = await _paused_then_resumed(make_house, ["window"])
    await _held_through_grace(house, engine)
    for _ in range(180):
        if engine._paused_by_door:
            break
        await house.advance(seconds=1)
    assert engine._paused_by_door, "grace expiry with the window still open must re-pause"
    assert house.thermostat.mode == "off"


async def test_new_window_during_resume_grace_does_not_repause(make_house) -> None:
    """grace-periods-spec: GRACE (manual / dashboard resume) + new door/window open (too warm for nat-vent) ->
    GRACE unchanged, no re-pause."""
    house, engine = await _paused_then_resumed(make_house, ["window", "door"])
    await house.advance(minutes=2)
    await house.open("door")
    await house.advance(seconds=_DEBOUNCE_S + 120)
    assert not engine._paused_by_door, "a new window open during the Resume grace re-paused the AC"
    assert house.thermostat.mode == "cool"
    await _held_through_grace(house, engine)
    for _ in range(180):
        if engine._paused_by_door:
            break
        await house.advance(seconds=1)
    assert engine._paused_by_door, "grace expiry with windows still open must re-pause"


async def test_control_without_resume_the_open_window_stays_paused(make_house) -> None:
    house = await make_house(**_hot(["window"]))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off"
    for _ in range(40):
        await house.advance(minutes=1)
        assert engine._paused_by_door and house.thermostat.mode == "off", "the normal pause changed"
