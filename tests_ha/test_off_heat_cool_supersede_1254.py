"""Issue #1254 (real HA): after a confirmed Off, heat, then back to cool, no stale "Off" override is left behind.

Occupant story: on a hot (or warm) day the person turns the thermostat Off and Climate Advisor confirms it as a
manual override. Later they switch to heat, then back to cool, which is what Climate Advisor runs anyway. The house is
cooling again; the dashboard must not keep saying "manual override: off" with its grace running, and when that grace
ended Climate Advisor must not act on an Off the person had already abandoned.

What keeps it right is the coordinator's Fix-D branch (``_async_thermostat_changed``, #282): a mode change during an
active override, to a different mode than the override's, supersedes it (``_replace_thermostat_override`` with
``OVERRIDE_SUPERSEDED``) before a new confirmation starts. The heat confirmation is still pending when cool returns,
so it self-resolves and the leftover grace is ended. Disable the Fix-D condition and these tests fail: the Off
override stays active over a cooling house.

Both day types where Climate Advisor cools (hot: the day label is cool; warm: the label is "off" but the house is above
the comfort ceiling, so the comfort band cools) and both fan modes. The house has a door/window sensor, kept closed
(a whole-house-fan home without one is unsupported, #1200).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_CONFIRM_S = 300
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


async def _cooling_house(make_house, fan: str, day: str):
    house = await make_house(
        fan=fan,
        contacts=["window"],
        startup="settled",
        profile="scripted",
        initial_hvac_mode="cool",
        ca_config={"fan_min_runtime_per_hour": 0, "manual_grace_seconds": 3600, "override_confirm_seconds": _CONFIRM_S},
        **_DAYS[day],
    )
    await house.advance(minutes=8)  # past the start-up window and >120 s after CA's own commands
    engine = house.coordinator.automation_engine
    assert house.thermostat.mode == "cool", f"precondition: CA is cooling the {day} day"
    assert engine._last_commanded_hvac_mode == "cool"
    assert not engine._manual_override_active
    return house, engine


@pytest.mark.parametrize("day", list(_DAYS))
@pytest.mark.parametrize("fan", _FANS)
async def test_off_then_heat_then_cool_leaves_no_stale_off_override(make_house, fan, day) -> None:
    house, engine = await _cooling_house(make_house, fan, day)

    await house.occupant_sets_thermostat(mode="off")
    await house.advance(seconds=_CONFIRM_S + 60)
    assert engine._manual_override_active, "precondition: the occupant's Off was confirmed as an override"
    assert engine._manual_override_mode == "off"

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(seconds=120)
    assert not engine._manual_override_active, "Fix-D: the Off override was not superseded by the switch to heat"
    assert engine._manual_override_mode != "off"
    assert engine._override_confirm_pending, "the switch to heat starts its own confirmation"

    await house.occupant_sets_thermostat(mode="cool")
    await house.advance(seconds=_CONFIRM_S + 60)
    assert house.thermostat.mode == "cool"
    assert not engine._manual_override_active, (
        f"stale override {engine._manual_override_mode!r} left over a house that is cooling"
    )
    assert not engine._override_confirm_pending
    assert not engine._grace_active, "the abandoned override's grace is still running"
