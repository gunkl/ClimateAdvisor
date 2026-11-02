"""Real-HA tests for Issue #1222: leaving home ends a cleared override's grace at once.

Occupant story: you nudged the thermostat, Climate Advisor confirmed it and is honouring it for the grace period.
Everyone then leaves. Climate Advisor drops the override for the away/vacation setback; the grace that protected it
must end with it. It used to keep running (dashboard: "Grace period active" in an empty, setback house) until the
orphaned-grace watchdog reaped it with an ERROR ("Stuck grace detected"), up to one coordinator cycle later.

Away and vacation, whole-house-fan and hvac_fan homes, plus the hvac_fan blower pressed beside the setpoint change
(the fan override's grace is then the live one). Control: a fan-off hold survives leaving home.

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_CONFIRM_S = 60
_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 76.0,
    "outdoor_f": 80.0,
    "high_f": 90.0,
    "low_f": 65.0,
    "initial_hvac_mode": "cool",
}
_LEAVE = [("handle_occupancy_away", "occupancy_away"), ("handle_occupancy_vacation", "occupancy_vacation")]


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


async def _house_with_confirmed_setpoint(make_house, caplog, fan: str):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan=fan,
        ca_config={
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
            "manual_grace_seconds": 900,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_HOUSE,
    )
    await _steps(house, 360)  # past the start-up window that suppresses override detection
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await house.occupant_sets_thermostat(setpoint_f=house.thermostat.setpoint_f - 3.0)
    await _steps(house, _CONFIRM_S + 30)
    assert engine._manual_override_active and engine._grace_active, "precondition: the setpoint change is confirmed"
    assert engine._grace_protects_override
    return house, engine, events


async def _leave_and_check(house, engine, events, caplog, handler: str, reason: str) -> None:
    before = len(events)
    await getattr(engine, handler)()
    assert engine._manual_override_active is False, "the away/vacation setback drops the override"
    assert engine._grace_active is False, "the grace protected only the override that was just cleared"
    cancelled = [d for t, d in events[before:] if t == "grace_cancelled"]
    assert [d.get("reason") for d in cancelled] == [reason], cancelled
    # the watchdog must find nothing to reap, now or a full cycle later
    await _steps(house, 90, step=30)
    assert not [t for t, _ in events if t == "stuck_grace_recovered"]
    assert "Stuck grace detected" not in caplog.text


@pytest.mark.parametrize("fan", ["whf", "hvac_fan"])
@pytest.mark.parametrize(("handler", "reason"), _LEAVE)
async def test_leaving_home_ends_the_cleared_setpoint_overrides_grace(make_house, caplog, fan, handler, reason):
    house, engine, events = await _house_with_confirmed_setpoint(make_house, caplog, fan)
    await _leave_and_check(house, engine, events, caplog, handler, reason)
    await house.close()


@pytest.mark.parametrize(("handler", "reason"), _LEAVE)
async def test_leaving_home_ends_the_blower_overrides_grace_too(make_house, caplog, handler, reason):
    house, engine, events = await _house_with_confirmed_setpoint(make_house, caplog, "hvac_fan")
    await house.occupant_sets_fan_mode("on")
    await _steps(house, 60)
    assert engine._fan_override_active and engine._last_grace_trigger == "fan_manual_override"
    await _leave_and_check(house, engine, events, caplog, handler, reason)
    assert engine._fan_override_active is False
    await house.close()


@pytest.mark.parametrize("fan", ["whf", "hvac_fan"])
@pytest.mark.parametrize(("handler", "reason"), _LEAVE)
async def test_control_no_override_leaves_graces_alone(make_house, caplog, fan, handler, reason):
    """Nothing cleared, nothing reaped: leaving home with no override running ends no grace."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan=fan, ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30}, **_HOUSE
    )
    await _steps(house, 360)
    engine = house.coordinator.automation_engine
    events = _record_events(house)
    await getattr(engine, handler)()
    assert not [t for t, _ in events if t == "grace_cancelled"]
    assert not [t for t, _ in events if t == "stuck_grace_recovered"]
    await house.close()
