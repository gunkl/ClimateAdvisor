"""Real-HA tests for Issue #1194 (b): the remote-sensor outer loop's gates, in both fan configurations.

Occupant story: it is the middle of a cold night. The bedroom (the sleep sensor) is below the sleep floor while the
hallway thermostat is satisfied, so Climate Advisor raises the hallway setpoint in 2 F steps (increment a). The person
in the bedroom must never have those extra degrees pushed into an open window or over a thermostat change they just
made, and when the window closes or their change ends they get the right temperature back, not a stranded +4 F.

The REAL coordinator listeners run against the dev thermostat sim and a real contact sensor; the bedroom is a real
state in the state machine. Both fan configurations (``whf``, ``hvac_fan``).

Prove the tests are not vacuous with ``scripts/revert_proof.py <pre-fix commit> <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
BEDROOM = "sensor.house_bedroom"
_DEBOUNCE_S = 120
_PLAIN_F = 66.0  # the sleep floor Climate Advisor writes at the start of the night
_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 2, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,  # the hallway reads above its setpoint-1: the thermostat is satisfied
    "outdoor_f": 30.0,  # far too cold for natural ventilation: an open window means "pause"
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
    "ca_config": {
        "sleep_indoor_temp_entity": BEDROOM,
        "sleep_time": "20:30",
        "wake_time": "06:30",
        "sensor_debounce_seconds": _DEBOUNCE_S,
        "manual_grace_seconds": 3600,
        "override_confirm_seconds": 60,
        "fan_min_runtime_per_hour": 0,
    },
}


def _bedroom(house, f: float) -> None:
    house.hass.states.async_set(BEDROOM, str(f), {"unit_of_measurement": "°F"})


async def _seed_bedroom(house) -> None:
    _bedroom(house, 58.0)


async def _cold_night(make_house, fan: str):
    house = await make_house(fan=fan, before_ca_setup=_seed_bedroom, **_HOUSE)
    engine = house.coordinator.automation_engine
    assert engine._remote_target_f == _PLAIN_F, "precondition: CA wrote the sleep floor"
    return house, engine


async def _at_cap(house, engine) -> None:
    await house.advance(minutes=12)  # two raises, a startup wait apart
    assert house.thermostat.setpoint_f == _PLAIN_F + 4.0, "precondition: the loop raised to the +4 F cap"
    assert engine._remote_offset_applied_f == pytest.approx(4.0)
    await house.advance(minutes=3)  # clear of CA's own command-recency guards


def _ca_heat_cool_commands(house, since: int) -> list:
    """CA calls that set a setpoint or arm heat/cool (an Off or a fan command is not what the loop could add)."""
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and (c.service == "set_temperature" or (c.service == "set_hvac_mode" and c.data.get("hvac_mode") != "off"))
    ]


def _ca_setpoints(house, since: int) -> list[float]:
    return [
        c.data["temperature"]
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service == "set_temperature"
    ]


@pytest.mark.parametrize("fan", _FANS)
async def test_window_inside_its_debounce_gets_no_extra_degrees(make_house, fan) -> None:
    """The window opens at the very start of the night: the frozen offset (none yet) is held, never raised."""
    house, engine = await _cold_night(make_house, fan)
    mark = len(house.calls)
    await house.open("window")
    await house.advance(seconds=60)
    _bedroom(house, 57.0)  # a fresh cold report: the listener checks the loop at once
    await house.advance(seconds=30)
    assert engine._sensor_debounce_pending and not engine._paused_by_door, "precondition: inside the debounce"
    extra = [t for t in _ca_setpoints(house, mark) if t > _PLAIN_F]
    assert not extra, f"extra degrees were added into an open window: {extra}"


@pytest.mark.parametrize("fan", _FANS)
async def test_window_inside_its_debounce_freezes_the_held_offset(make_house, fan) -> None:
    """Owner decision D1 (FREEZE): the +4 F already on the thermostat stays as it is inside the debounce -- the window
    event's own refresh re-applies it, the loop neither steps nor withdraws it -- and a blip that closes in time
    changes nothing."""
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    cap = _PLAIN_F + 4.0
    mark = len(house.calls)
    await house.open("window")
    await house.advance(seconds=30)
    _bedroom(house, 58.0)  # a fresh cold report: an immediate loop check, frozen
    await house.advance(seconds=30)
    assert engine._sensor_debounce_pending and not engine._paused_by_door, "precondition: inside the debounce"
    assert house.thermostat.setpoint_f == cap, "the held offset was withdrawn or stepped inside the debounce"
    await house.close_contact("window")
    await house.advance(minutes=3)
    writes = _ca_setpoints(house, mark)
    assert set(writes) <= {cap}, f"the blip changed the setpoint: {writes}"
    assert house.thermostat.setpoint_f == cap and engine._remote_offset_f == pytest.approx(4.0)


@pytest.mark.parametrize("fan", _FANS)
async def test_a_busy_door_never_flaps_the_setpoint(make_house, fan) -> None:
    """A door opened for 20 s every 3 minutes for an hour: 0 setpoint changes (40 per hour before the freeze)."""
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    mark = len(house.calls)
    seen = []
    for _ in range(20):
        await house.open("window")
        await house.advance(seconds=20)
        seen.append(house.thermostat.setpoint_f)
        await house.close_contact("window")
        await house.advance(seconds=160)
        seen.append(house.thermostat.setpoint_f)
    writes = _ca_setpoints(house, mark)
    assert set(seen) == {_PLAIN_F + 4.0}, f"the setpoint flapped: {sorted(set(seen))}"
    assert set(writes) <= {_PLAIN_F + 4.0}, f"CA wrote a different setpoint during door use: {sorted(set(writes))}"


@pytest.mark.parametrize("fan", _FANS)
async def test_window_pause_then_close_returns_the_plain_target_not_the_cap(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the pause switched HVAC off"
    mark = len(house.calls)
    for f in (74.0, 58.0):  # the bedroom swings both ways while the window is open
        _bedroom(house, f)
        await house.advance(minutes=10)
    ca_climate = _ca_heat_cool_commands(house, mark)
    assert not ca_climate, f"CA commanded the thermostat while the window was open: {ca_climate}"

    _bedroom(house, _PLAIN_F)  # the bedroom is fine again: no raise is wanted
    await house.advance(seconds=30)
    mark = len(house.calls)
    await house.close_contact("window")
    await house.advance(minutes=4)
    assert not engine._paused_by_door and house.thermostat.mode == "heat", "precondition: HVAC is back"
    writes = _ca_setpoints(house, mark)
    assert (_PLAIN_F + 4.0) not in writes, f"the old +4 F cap was written back: {writes}"
    assert all(t <= _PLAIN_F + 2.0 for t in writes), f"doubled or stranded offset: {writes}"
    assert house.thermostat.setpoint_f == _PLAIN_F, "the plain target must be back"


@pytest.mark.parametrize("fan", _FANS)
async def test_occupant_change_with_an_offset_applied_is_never_stepped(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    await house.occupant_sets_thermostat(setpoint_f=69.0)
    await house.advance(minutes=3)  # past the 60 s confirmation
    assert engine._manual_override_active, "precondition: the change was confirmed as an override"
    mark = len(house.calls)
    for f in (75.0, 58.0, 75.0):  # both sides of the hysteresis band: neither a step down nor a raise is allowed
        _bedroom(house, f)
        await house.advance(minutes=10)
    assert house.thermostat.setpoint_f == 69.0, "the occupant's value did not survive"
    ca_climate = _ca_heat_cool_commands(house, mark)
    assert not ca_climate, f"CA commanded the thermostat over the occupant's change: {ca_climate}"


@pytest.mark.parametrize("fan", _FANS)
async def test_cancelling_the_override_returns_the_plain_target_not_the_cap(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    await house.occupant_sets_thermostat(setpoint_f=69.0)
    await house.advance(minutes=3)
    assert engine._manual_override_active
    await house.advance(minutes=3)  # the loop has seen the gate closed
    _bedroom(house, _PLAIN_F)
    mark = len(house.calls)
    engine.cancel_override(reason="user_cancel_override")
    await house.advance(minutes=4)
    assert not engine._manual_override_active
    writes = _ca_setpoints(house, mark)
    assert (_PLAIN_F + 4.0) not in writes, f"a stranded +4 F offset was written: {writes}"
    assert house.thermostat.setpoint_f == _PLAIN_F, f"the plain target must be back, got {writes}"
