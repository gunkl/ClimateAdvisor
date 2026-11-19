"""Issues #1612 and #1613 (real HA): occupant thermostat changes that land while a Climate Advisor command is in flight.

Occupant story: the person changes the thermostat (mode, setpoint or the blower) a moment after Climate Advisor sent
one of its own commands. Override detection skips every change inside its command guard (pending flags, the 3 s
mode window, the 30 s setpoint/fan windows) and nothing ever looked at it again, so Climate Advisor recorded no
override and no grace, and a later write of its own plan quietly undid the person's choice:

* #1612 (no override active): a switch to Heat inside the guard of CA's blower command (``hvac_fan``) or of the
  periodic cycle's ``set_temperature`` (both fan modes) -- the house was back on Cool within 35 minutes.
* #1613 pause branch: during a door/window pause, Heat or Cool turned on within 3 s of the pause's own HVAC-off --
  turned Off again by the next cycle.
* #1613 setpoint: 72 F set within 30 s of the cycle's setpoint write -- written back to 75 F by the next cycle.
* #1613 thermostat fan mode (``hvac_fan``): the blower turned on within 30 s of the cycle's write -- no fan override.

Now a change dropped only by those guards is re-checked once they clear, through the same handler and branches
(a synthetic event with only the dropped attributes changed). Not re-checked: a change inside a CA FAN-command window
to a setpoint or fan mode (a thermostat can revert its setpoint after a fan command; CA's own blower echo), and a mode
change to ``fan_only`` (a thermostat may switch to fan-only as a side effect of CA's own fan command; CA never writes
that mode, so it cannot tell). CA's own mode echo inside the window is still not the occupant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.components.climate import HVACMode

pytestmark = pytest.mark.house

_CONFIRM_S = 300
_CLIMATE = "climate.house_thermostat"
_FANS = ["whf", "hvac_fan"]
_DAYS = {
    "hot": {
        "start": datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
        "initial_indoor_f": 80.0,
        "outdoor_f": 95.0,
        "high_f": 98.0,
        "low_f": 75.0,
    },
    "warm": {
        "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        "initial_indoor_f": 75.0,  # above comfort_cool (74): CA cools although the day label is "off"
        "outdoor_f": 68.0,
        "high_f": 80.0,
        "low_f": 62.0,
    },
}


def _sim(house):
    return house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]


async def _house(make_house, fan: str, day: str, *, fmr: int = 10):
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",
        initial_hvac_mode="cool",
        ca_config={
            "fan_min_runtime_per_hour": fmr,
            "manual_grace_seconds": 3600,
            "override_confirm_seconds": _CONFIRM_S,
        },
        **_DAYS[day],
    )
    await house.advance(minutes=8)  # past the start-up window and >120 s after CA's own commands
    engine = house.coordinator.automation_engine
    assert house.thermostat.mode == "cool", f"precondition: CA is cooling the {day} day"
    assert not engine._manual_override_active
    return house, engine


def _state(house, engine) -> str:
    t = house.thermostat
    return (
        f"thermostat={t.mode} setpoint={t.setpoint_f} fan_mode={t.fan_mode} override={engine._manual_override_active}"
        f"/{engine._manual_override_mode} pending={engine._override_confirm_pending}/{engine._override_confirm_mode}"
        f" grace={engine._grace_active} paused={engine.is_paused_by_door} fan_override={engine._fan_override_active}"
    )


async def _cycles(house, minutes: int = 35) -> None:
    """The real coordinator update every 5 minutes: each one re-applies CA's plan when nothing protects a change."""
    for _ in range(minutes // 5):
        await house.advance(minutes=5)
        await house.coordinator.async_refresh()
        await house.advance(seconds=0)


async def _cycle_write(house) -> None:
    """One real coordinator cycle: CA re-sends its setpoint (``climate.set_temperature``), opening its guards."""
    n0 = len(house.calls)
    await house.coordinator.async_refresh()
    await house.advance(seconds=0)
    assert any(c.service == "set_temperature" and c.origin.startswith("ca") for c in house.calls[n0:]), (
        "precondition: the cycle sent CA's setpoint"
    )


async def _during_blower_command(house, engine, occupant) -> None:
    """``occupant()`` (a real service call) lands while CA's real blower command (``set_fan_mode``) is in flight."""
    sim = _sim(house)
    real = sim.async_set_fan_mode
    seen: dict[str, bool] = {}

    async def _cloud_round_trip(fan_mode):
        if not seen:
            seen["ca_command_pending"] = engine._fan_command_pending
            await occupant()
        await real(fan_mode)

    sim.async_set_fan_mode = _cloud_round_trip
    try:
        for _ in range(3600):  # the minimum-runtime cycle starts the blower within the hour
            await house.advance(seconds=1)
            if seen:
                break
    finally:
        sim.async_set_fan_mode = real
    assert seen.get("ca_command_pending") is True, "precondition: CA's real blower command was in flight"


def _occupant_mode(house, mode: str):
    async def _call():
        await house.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": _CLIMATE, "hvac_mode": mode},
            blocking=True,
            context=house._occupant_context(),
        )

    return _call


# ---------------------------------------------------------------- #1612: no override active


async def _heat_after_cycle_write(house, engine) -> None:
    await _cycle_write(house)
    await house.advance(seconds=1)
    assert house.coordinator._is_recent_hvac_command(), "precondition: inside the 3 s window"
    await house.occupant_sets_thermostat(mode="heat")


async def _heat_during_blower(house, engine) -> None:
    await _during_blower_command(house, engine, _occupant_mode(house, "heat"))


_MODE_CASES = [
    ("whf", "cycle_write", _heat_after_cycle_write),
    ("hvac_fan", "cycle_write", _heat_after_cycle_write),
    ("hvac_fan", "blower_command", _heat_during_blower),
]


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize(("fan", "command", "change"), _MODE_CASES, ids=[f"{c[0]}-{c[1]}" for c in _MODE_CASES])
async def test_1612_mode_change_inside_a_ca_command_window_is_detected(make_house, fan, command, change, day):
    house, engine = await _house(make_house, fan, day)
    await change(house, engine)
    assert house.thermostat.mode == "heat"
    await house.advance(seconds=10)  # the guard has cleared
    assert engine._override_confirm_pending and engine._override_confirm_mode == "heat", (
        f"the occupant's Heat was not detected: {_state(house, engine)}"
    )
    await _cycles(house)
    assert house.thermostat.mode == "heat", f"CA undid the occupant's Heat: {_state(house, engine)}"
    assert engine._manual_override_active and engine._manual_override_mode == "heat", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", _FANS)
async def test_1612_cas_own_mode_echo_inside_the_window_is_not_the_occupant(make_house, fan, day) -> None:
    house, engine = await _house(make_house, fan, day)
    _sim(house).set_echo_delay(2)
    await engine._set_hvac_mode("heat", reason="#1612 test: CA's own write")
    await house.advance(seconds=2)
    assert house.thermostat.mode == "heat", "precondition: CA's echo arrived"
    assert house.coordinator._is_recent_hvac_command(), "precondition: the echo landed inside the guard window"
    _sim(house).set_echo_delay(0)
    await house.advance(seconds=20)
    assert not engine._override_confirm_pending and not engine._manual_override_active, _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", _FANS)
async def test_1612_control_change_outside_the_window_is_detected_at_once(make_house, fan, day) -> None:
    house, engine = await _house(make_house, fan, day)
    await house.occupant_sets_thermostat(mode="heat")
    assert engine._override_confirm_pending and engine._override_confirm_mode == "heat", _state(house, engine)


# ---------------------------------------------------------------- #1613: pause branch


async def _paused(house, engine) -> None:
    await house.open()
    for _ in range(1800):
        await house.advance(seconds=1)
        if engine.is_paused_by_door and house.thermostat.mode == "off":
            break
    assert engine.is_paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the HVAC"


@pytest.mark.parametrize("target", ["heat", "cool"])
@pytest.mark.parametrize("fan", _FANS)
async def test_1613_pause_change_right_after_the_pause_write_is_detected(make_house, fan, target) -> None:
    """Hot day (the window pauses; on the warm day it starts ventilation instead)."""
    house, engine = await _house(make_house, fan, "hot")
    await _paused(house, engine)
    assert house.coordinator._is_recent_hvac_command(), "precondition: inside the pause write's 3 s window"
    await house.occupant_sets_thermostat(mode=target)
    await house.advance(seconds=10)
    assert engine._override_confirm_pending or engine._manual_override_active, (
        f"the occupant's {target} during the pause was not detected: {_state(house, engine)}"
    )
    await _cycles(house)
    assert house.thermostat.mode == target, f"CA undid the occupant's {target}: {_state(house, engine)}"


@pytest.mark.parametrize("fan", _FANS)
async def test_1613_control_pause_change_outside_the_window(make_house, fan) -> None:
    house, engine = await _house(make_house, fan, "hot")
    await _paused(house, engine)
    await house.advance(seconds=10)
    await house.occupant_sets_thermostat(mode="heat")
    assert engine._override_confirm_pending or engine._manual_override_active, _state(house, engine)


# ---------------------------------------------------------------- #1613: setpoint


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", _FANS)
async def test_1613_setpoint_inside_the_cycle_write_window_is_detected(make_house, fan, day) -> None:
    house, engine = await _house(make_house, fan, day, fmr=0)
    await _cycle_write(house)
    await house.advance(seconds=5)
    assert house.coordinator._is_recent_temp_command(), "precondition: inside the 30 s setpoint window"
    await house.occupant_sets_thermostat(setpoint_f=72.0)
    await house.advance(seconds=40)  # the 30 s guard has cleared
    assert engine._override_confirm_pending or engine._manual_override_active, (
        f"the occupant's 72 F was not detected: {_state(house, engine)}"
    )
    await _cycles(house)
    assert house.thermostat.setpoint_f == 72.0, f"CA wrote its setpoint over the occupant's: {_state(house, engine)}"


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", _FANS)
async def test_1613_cas_own_setpoint_echo_inside_the_window_is_not_the_occupant(make_house, fan, day) -> None:
    house, engine = await _house(make_house, fan, day, fmr=0)
    _sim(house).set_echo_delay(5)
    await engine._set_temperature(73.0, reason="#1613 test: CA's own write", mode="cool")
    await house.advance(seconds=5)
    assert house.thermostat.setpoint_f == 73.0, "precondition: CA's echo arrived"
    _sim(house).set_echo_delay(0)
    await house.advance(seconds=60)
    assert not engine._override_confirm_pending and not engine._manual_override_active, _state(house, engine)


# ---------------------------------------------------------------- #1613: thermostat fan mode (hvac_fan only, #1197)


@pytest.mark.parametrize("day", list(_DAYS))
async def test_1613_blower_press_inside_the_cycle_write_window_is_detected(make_house, day) -> None:
    house, engine = await _house(make_house, "hvac_fan", day, fmr=0)
    await _cycle_write(house)
    await house.advance(seconds=5)
    assert house.coordinator._is_recent_hvac_command(threshold_seconds=30.0), "precondition: inside the 30 s window"
    await house.occupant_sets_fan_mode("on")
    # The fan-mode detector also waits out the 120 s "thermostat in CA's last commanded mode" window.
    await house.advance(seconds=130)
    assert engine._fan_override_active, f"the occupant's blower press was not detected: {_state(house, engine)}"


# ---------------------------------------------------------------- side effects of CA's own fan command


async def _fan_only_side_effect(house, engine) -> None:
    """The thermostat answers CA's blower command by switching itself to fan_only (CA never writes that mode)."""
    sim = _sim(house)
    real = sim.async_set_fan_mode
    seen: dict[str, bool] = {}

    async def _side_effect(fan_mode):
        await real(fan_mode)
        if not seen and fan_mode != "auto":
            seen["pending"] = engine._fan_command_pending
            sim._hvac_mode = HVACMode.FAN_ONLY
            sim.async_write_ha_state()

    sim.async_set_fan_mode = _side_effect
    try:
        for _ in range(3600):
            await house.advance(seconds=1)
            if seen:
                break
    finally:
        sim.async_set_fan_mode = real
    assert seen.get("pending") is True, "precondition: the side effect landed while CA's fan command was in flight"
    assert house.thermostat.mode == "fan_only"


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("with_override", [False, True])
async def test_fan_only_side_effect_of_cas_fan_command_is_not_the_occupant(make_house, day, with_override) -> None:
    house, engine = await _house(make_house, "hvac_fan", day)
    if with_override:
        await house.occupant_sets_thermostat(mode="off")
        await house.advance(seconds=_CONFIRM_S + 60)
        assert engine._manual_override_active and engine._manual_override_mode == "off", "precondition: Off confirmed"
    await _fan_only_side_effect(house, engine)
    await house.advance(seconds=60)
    assert engine._override_confirm_mode != "fan_only" and engine._manual_override_mode != "fan_only", (
        f"CA's own fan command side effect was read as the occupant: {_state(house, engine)}"
    )
    if with_override:
        assert engine._manual_override_active and engine._manual_override_mode == "off", _state(house, engine)


@pytest.mark.parametrize("day", list(_DAYS))
async def test_setpoint_revert_after_cas_fan_command_is_not_the_occupant(make_house, day) -> None:
    """A thermostat that restores its program setpoint as a side effect of CA's blower command (ecobee)."""
    house, engine = await _house(make_house, "hvac_fan", day)
    sim = _sim(house)
    real = sim.async_set_fan_mode
    seen: dict[str, bool] = {}

    async def _side_effect(fan_mode):
        await real(fan_mode)
        if not seen:
            seen["pending"] = engine._fan_command_pending
            sim._target_temp = 78.0
            sim.async_write_ha_state()

    sim.async_set_fan_mode = _side_effect
    try:
        for _ in range(3600):
            await house.advance(seconds=1)
            if seen:
                break
    finally:
        sim.async_set_fan_mode = real
    assert seen.get("pending") is True and house.thermostat.setpoint_f == 78.0
    await house.advance(seconds=60)
    assert not engine._override_confirm_pending and not engine._manual_override_active, _state(house, engine)


@pytest.mark.parametrize("fan", _FANS)
async def test_a_later_change_judged_live_is_not_judged_again_by_the_recheck(make_house, fan, caplog) -> None:
    """Heat lands inside the window (dropped), Off follows outside it (detected live): the re-check must not judge
    the thermostat's move from the pre-drop Cool to the live Off a second time."""
    import logging

    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house, engine = await _house(make_house, fan, "hot", fmr=0)
    await _cycle_write(house)
    await house.advance(seconds=1)
    assert house.coordinator._is_recent_hvac_command()
    await house.occupant_sets_thermostat(mode="heat")  # dropped: inside the 3 s window
    await house.advance(seconds=2)
    assert not house.coordinator._is_recent_hvac_command()
    await house.occupant_sets_thermostat(mode="off")  # judged live
    await house.advance(seconds=20)  # the re-check has run
    detected = [r.getMessage() for r in caplog.records if "Manual HVAC override detected" in r.getMessage()]
    assert len(detected) == 1 and "-> off" in detected[0], detected
    assert engine._override_confirm_pending and engine._override_confirm_mode == "off", _state(house, engine)
