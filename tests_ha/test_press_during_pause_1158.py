"""Real-HA tests for Issue #1158 (rule 3): the occupant's heat/cool press during a door/window pause is kept.

Occupant story: a window (or door) is open and Climate Advisor has paused heating/cooling. The person turns the
heat (cold day) or the cooling (hot day) back on at the thermostat. Owner rule 3: HVAC stays off while a window is
open UNLESS a manual override turns it on. Before the fix the press equalled the day's classification label
("heat" on a cold day), so at the end of the confirmation window it was discarded as "self-resolved" and the
window pause was re-applied ten minutes later. A press during a pause is the occupant's own change, whatever the
day's label says.

Prove a test is not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_COLD = {
    "start": datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
    "initial_indoor_f": 62.0,
    "outdoor_f": 38.0,
    "high_f": 44.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
}
_HOT = {
    "start": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 82.0,
    "outdoor_f": 96.0,
    "high_f": 98.0,
    "low_f": 78.0,
    "initial_hvac_mode": "cool",
}


def _house_cfg(day: dict, contact: str, fan: str):
    return {
        "fan": fan,
        "contacts": [contact],
        "startup": "settled",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "override_confirm_seconds": 600},
        **day,
    }


@pytest.mark.parametrize(
    ("day", "press", "contact"),
    [
        (_COLD, "heat", "window"),
        (_COLD, "heat", "door"),
        (_HOT, "cool", "window"),
        (_HOT, "cool", "door"),
    ],
    ids=["cold-heat-window", "cold-heat-door", "hot-cool-window", "hot-cool-door"],
)
async def test_press_during_a_pause_is_confirmed_not_discarded_as_self_resolved(
    make_house, day, press, contact
) -> None:
    house = await make_house(**_house_cfg(day, contact, fan=None))
    ae = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window that suppresses override detection
    await house.open(contact)
    await house.advance(minutes=2)
    assert ae._paused_by_door, "precondition: the open contact paused HVAC"
    assert house.thermostat.mode == "off"

    await house.occupant_sets_thermostat(mode=press)
    await house.advance(minutes=14)  # past the 10 minute confirmation and the 5 minute revisits

    assert ae._manual_override_active, "the press was discarded as self-resolved instead of confirmed"
    assert house.thermostat.mode == press, "rule 3: the occupant's press holds while the contact is open"


@pytest.mark.parametrize(
    ("day", "press"),
    [(_COLD, "heat"), (_HOT, "cool")],
    ids=["cold-heat", "hot-cool"],
)
async def test_press_during_a_pause_with_a_fan_grace_is_confirmed(make_house, day, press) -> None:
    """Same story with a whole-house-fan override grace running (the #1158 report): the press stops the fan
    (newest occupant action wins) and is confirmed, not discarded."""
    house = await make_house(**_house_cfg(day, "window", fan="whf"))
    ae = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(minutes=2)
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(True)
    for _ in range(30):
        await house.advance(seconds=1)
    assert ae._grace_active, "precondition: the fan override's grace is running"

    await house.occupant_sets_thermostat(mode=press)
    await house.advance(minutes=14)

    assert ae._manual_override_active, "the press was discarded as self-resolved instead of confirmed"
    assert house.thermostat.mode == press
    assert not house.fan.is_on, "rule 2: the newest occupant action (the press) wins, the fan must have stopped"
