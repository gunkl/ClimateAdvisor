"""Issue #1289 (real HA, two sensors): free cooling restarts for a window open past its delay, whatever door B does.

Occupant story: window A has been open long past its delay on a cool evening. The whole-house fan cooled the house
to the comfort floor and stopped (window A still open, so the HVAC stays paused). The house warms again, and
Climate Advisor should restart free cooling for window A. Someone opens door B briefly, for less than its delay.
Before the fix the idle-open restart gate asked "is ANY sensor still inside its delay?" house-wide, so door B hid
window A and free cooling waited up to one delay (10 min here) while the house stayed warmer than needed.

Owner rule: "is the house open?" needs only ONE sensor open past its own delay. A dark (unavailable) sensor never
counts: it never STARTS the fan (Issue #1181), so the dark-window control must never start it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

DEB = 600  # door/window delay; door B's brief open stays well inside it
BASE = {
    "fan": "whf",
    "profile": "scripted",
    "contacts": ["window", "door"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 75.0,
    "outdoor_f": 64.0,
    "high_f": 70.0,
    "low_f": 60.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": DEB},
}
RESTART_BUDGET_S = 6 * 60  # measured ~4.5 min with window A alone; door B stays open longer than this


async def _cooled_to_floor_with_window_a_open(make_house):
    """Window A open past its delay; the fan session ended at the comfort floor with window A still open."""
    house = await make_house(**BASE)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=DEB + 60)
    assert engine._natural_vent_active and house.fan.is_on, "precondition: window A started free cooling"
    house.set_indoor(66.0)  # below the comfort floor: the session ends
    for _ in range(60):
        await house.advance(seconds=30)
        if not engine._natural_vent_active:
            break
    assert not engine._natural_vent_active and not house.fan.is_on, "precondition: the floor exit stopped the fan"
    assert engine._paused_by_door and engine._paused_with_hvac_already_off, (
        "precondition: window A keeps the idle pause, so the restart goes through the idle-open gate"
    )
    return house, engine


@pytest.mark.parametrize("door_b", [False, True], ids=["window_only", "door_b_brief_open"])
async def test_free_cooling_restarts_for_window_a_while_door_b_is_inside_its_delay(make_house, door_b) -> None:
    house, engine = await _cooled_to_floor_with_window_a_open(make_house)

    if door_b:
        await house.open("door")  # door B: a brief open, it stays inside its own delay below
    house.set_indoor(77.0)  # the house warms again; outdoor is still far cooler
    for _ in range(RESTART_BUDGET_S // 30):
        await house.advance(seconds=30)
        if engine._natural_vent_active:
            break
    assert engine._natural_vent_active and house.fan.is_on, (
        "free cooling did not restart for window A (open past its delay)"
        + (" while door B was inside its delay" if door_b else "")
    )
    assert house.thermostat.mode == "off", "rule 3: HVAC armed while window A is open"
    if door_b:
        await house.close_contact("door")  # closes before its delay: door B never counted as open
        await house.advance(minutes=2)
        assert engine._natural_vent_active and house.fan.is_on, "door B closing must not stop window A's session"


async def test_dark_window_a_never_starts_the_fan_while_door_b_is_inside_its_delay(make_house) -> None:
    """Control (#1181): window A goes dark (its last reading was open). Door B is inside its delay.

    No sensor reads open past its own delay, so nothing may start the fan, however warm the house gets.
    """
    house, engine = await _cooled_to_floor_with_window_a_open(make_house)

    house.hass.states.async_set("binary_sensor.house_window", "unavailable")
    await house.advance(seconds=30)
    await house.open("door")
    house.set_indoor(77.0)
    for _ in range((DEB - 120) // 30):  # stop before door B's own delay ends (then door B legitimately counts)
        await house.advance(seconds=30)
        assert not house.fan.is_on and not engine._natural_vent_active, (
            "a dark window plus a door inside its delay started the fan"
        )
    assert house.thermostat.mode == "off", "rule 3: HVAC armed while window A (dark, last open) holds the pause"


GRACE = 1200
CONFIRM = 60


def _fan_running(house) -> bool:
    """The ventilation fan: the whole-house fan entity, or the thermostat's own blower."""
    return house.fan.is_on if house.fan is not None else house.thermostat.fan_mode == "on"


@pytest.mark.parametrize("door_b", [False, True], ids=["grace_expiry", "then_door_b_brief_open"])
@pytest.mark.parametrize("fan", ["whf", "hvac_fan"])
async def test_dark_window_never_starts_the_fan_when_the_occupants_grace_expires(
    make_house, fan, door_b, caplog
) -> None:
    """Control (#1181) on the grace-expiry start path.

    Window A is open past its delay and holds an idle pause. The occupant presses Cool (manual grace). Window A then
    goes dark (its last reading was open). The grace expires on a cool evening with a warm house: rule 3 keeps HVAC
    off for window A (holding read), but a dark sensor must never START the fan (whole-house fan or blower).
    """
    caplog.set_level("INFO", logger="custom_components.climate_advisor")
    base = {
        **BASE,
        "fan": fan,
        "ca_config": {**BASE["ca_config"], "manual_grace_seconds": GRACE, "override_confirm_seconds": CONFIRM},
    }
    house = await make_house(**base)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=DEB + 60)
    assert engine._natural_vent_active, "precondition: window A started free cooling"
    house.set_indoor(66.0)
    for _ in range(60):
        await house.advance(seconds=30)
        if not engine._natural_vent_active:
            break
    assert not engine._natural_vent_active and engine._paused_by_door, "precondition: window A holds the pause"

    await house.occupant_sets_thermostat(mode="cool")
    await house.advance(seconds=CONFIRM + 60)
    assert engine._manual_override_active and engine._grace_active, "precondition: the occupant's grace runs"

    house.hass.states.async_set("binary_sensor.house_window", "unavailable")
    await house.advance(seconds=30)
    assert not _fan_running(house)
    for _ in range((GRACE + 600) // 30):  # through the grace's end and the re-check that follows
        house.set_indoor(74.0)  # warm (below the blower's ceiling), outdoor far cooler: only the sensor gate fails
        await house.advance(seconds=30)
        assert not _fan_running(house) and not engine._natural_vent_active, (
            f"a dark window started the fan (grace={engine._grace_active}, paused={engine._paused_by_door})"
        )
    assert not engine._grace_active, "precondition: the grace expired inside the loop"
    assert engine._paused_by_door, "rule 3: window A (dark, last open) still holds the pause"
    quiet = [r.getMessage() for r in caplog.records if "pause will be quiet" in r.getMessage()]
    assert not quiet, f"G5: the pause was silenced for a fan session that can never start: {quiet}"
    assert house.thermostat.mode == "off", "rule 3: window A (dark, last open) still holds HVAC off"
    if door_b:
        # Door B opens briefly: its refresh runs the paused-by-door reactivation re-check. Door B is inside its
        # delay and window A is dark, so nothing reads open past its delay: still no fan start.
        await house.open("door")
        for _ in range(8):
            house.set_indoor(74.0)
            await house.advance(seconds=30)
            assert not _fan_running(house) and not engine._natural_vent_active, (
                f"door B (inside its delay) plus dark window A started the fan (paused={engine._paused_by_door})"
            )
        await house.close_contact("door")
        await house.advance(minutes=2)
        assert not _fan_running(house) and house.thermostat.mode == "off"
