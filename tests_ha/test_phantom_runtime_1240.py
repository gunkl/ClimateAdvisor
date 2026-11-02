"""Real-HA tests for Issue #1240: an idle mode change must not be credited as HVAC runtime.

Occupant story: someone switches the thermostat Off -> Heat (or Cool) but the house is already warm (or cool) enough,
so the equipment never runs (``hvac_action`` stays "idle"). Later they switch it back to Off. The dashboard must not
show hours of heating/cooling that never happened. Before the fix the coordinator's hvac_mode fallback treated any
mode change with no heating/cooling action as running, armed ``_hvac_on_since`` and flushed the whole idle window
into ``_today_record.hvac_runtime_minutes``.

The control test proves real runtime (action "heating") is still credited.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "outdoor_f": 50.0,
    "high_f": 55.0,
    "low_f": 45.0,
    "initial_hvac_mode": "off",
}


_HOT_DAY = {"outdoor_f": 92.0, "high_f": 96.0, "low_f": 76.0}


async def _run(
    make_house, *, mode: str, indoor_f: float, expect_action: str, quiet_ca: bool = False, **weather
) -> object:
    house = await make_house(initial_indoor_f=indoor_f, **{**_HOUSE, **weather})
    await house.advance(seconds=360)  # past start-up and >120 s after CA's own commands
    house.set_indoor(indoor_f)
    if quiet_ca:
        # On a hot day CA itself switches Off -> Cool at start-up, so the occupant's Cool would be a no-op. Park the
        # thermostat Off and mute CA's automation so the occupant's own Off -> Cool is the only mode change.
        await house.set_automation(False)
        if house.thermostat.mode != "off":
            await house.occupant_sets_thermostat(mode="off")
            await house.advance(minutes=6)
        assert house.thermostat.mode == "off"
        assert house.coordinator._hvac_on_since is None
    await house.occupant_sets_thermostat(mode=mode)
    await house.advance(minutes=1)
    assert house.thermostat.mode == mode, f"precondition: occupant's {mode} stuck, is {house.thermostat.mode}"
    await house.advance(minutes=60)
    assert house.thermostat.mode == mode, f"precondition: still {mode} after an hour, is {house.thermostat.mode}"
    assert house.thermostat.action == expect_action, f"precondition: action {house.thermostat.action}"
    await house.occupant_sets_thermostat(mode="off")
    await house.advance(minutes=2)
    assert house.thermostat.mode == "off"
    return house


async def test_idle_heat_not_counted_as_runtime(make_house) -> None:
    house = await _run(make_house, mode="heat", indoor_f=75.0, expect_action="idle")
    coord = house.coordinator
    assert coord._today_record.hvac_runtime_minutes < 1.0, coord._today_record.hvac_runtime_minutes
    assert coord._hvac_on_since is None


async def test_idle_cool_not_counted_as_runtime(make_house) -> None:
    house = await _run(make_house, mode="cool", indoor_f=72.0, expect_action="idle", quiet_ca=True, **_HOT_DAY)
    coord = house.coordinator
    assert coord._today_record.hvac_runtime_minutes < 1.0, coord._today_record.hvac_runtime_minutes
    assert coord._hvac_on_since is None


async def test_control_real_heating_is_counted(make_house) -> None:
    house = await _run(make_house, mode="heat", indoor_f=55.0, expect_action="heating")
    coord = house.coordinator
    assert coord._today_record.hvac_runtime_minutes > 0, coord._today_record.hvac_runtime_minutes
    assert coord._hvac_on_since is None
