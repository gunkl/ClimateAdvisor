"""Lifecycle contract of the house (design 4.4-4.5, symmetry item 0.3).

Occupant view: after saving settings or restarting Home Assistant, exactly one copy of each zone is alive and it
is the new one. The thermostat, fan and notifications are driven by that copy only.

The existing ``test_unload_cleanup.py`` checks the *stop* side of a zone's life (nothing survives an unload).
These tests add the *start* side: what a second set-up on the same Home Assistant does, and that the house
itself, with a thermostat, weather, contact and fan around it, tears down clean.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.house


async def test_house_teardown_leaves_nothing_running(make_house) -> None:
    """Build the full house, run it for 5 minutes, close it. HA's ``verify_cleanup`` fails on any survivor."""
    house = await make_house(fan="whf", contacts=["window"], startup="cold")
    await house.advance(minutes=5)
    await house.close()
    # verify_cleanup (autouse) now fails the test on any lingering timer, task or thread.


_HOT_COOL_OUTDOOR = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "cold",
    "initial_indoor_f": 82.0,
    "outdoor_f": 60.0,
    "high_f": 70.0,
    "low_f": 52.0,
    "ca_config": {"sensor_debounce_seconds": 60},
}


async def test_positive_control_house_without_reload_commands_the_fan(make_house) -> None:
    """The instrument sees Climate Advisor act: a hot house with an open window and cool outdoors gets its fan run."""
    house = await make_house(**_HOT_COOL_OUTDOOR)
    await house.open("window")
    await house.advance(minutes=10)
    fan_on = [c for c in house.calls if c.domain == "fan" and c.service == "turn_on"]
    assert fan_on, f"CA never commanded the fan in 10 minutes: {[c for c in house.calls if c.domain != 'weather']}"
    assert all(c.origin == "ca:1" for c in fan_on)


async def test_two_setups_in_a_row_only_the_new_one_commands(make_house) -> None:
    """The settings-save shape inside the start-up window (the #1102 field incident, as a general contract).

    Occupant view: the person saves settings 172 s after a restart, then switches the fan off. A leftover copy of
    the old zone must not wake at its 300 s mark and turn it back on. Set up, open the window, reload at +172 s,
    have the occupant press the fan off, advance 10 minutes. Every command after the reload must come from the
    second instance and the first must run no further update cycle.
    """
    house = await make_house(**_HOT_COOL_OUTDOOR)
    await house.open("window")
    await house.advance(seconds=172)

    first = house.coordinator
    updates_of_first: list = []
    real_update = first._async_update_data

    async def _spy():
        updates_of_first.append(1)
        return await real_update()

    first._async_update_data = _spy

    await house.reload_zone()
    second = house.coordinator
    assert second is not first
    assert house.instances == [first, second]
    await house.occupant_presses_fan(False)
    calls_before = len(house.calls)
    updates_of_first.clear()

    await house.advance(minutes=10)

    after = house.calls[calls_before:]
    ghost = [c for c in after if c.origin == "ca:1"]
    assert not ghost, f"the replaced instance kept commanding: {ghost}"
    assert not updates_of_first, f"the replaced coordinator ran {len(updates_of_first)} update cycle(s) after reload"


async def test_reload_zone_keeps_persisted_state_and_drops_timers(make_house) -> None:
    """A reload restores what the occupant set (automation off) from disk and the old copy stays quiet."""
    house = await make_house(fan="whf", contacts=["window"], startup="settled")
    await house.set_automation(False)
    first = house.coordinator
    assert first.automation_enabled is False

    await house.reload_zone()

    second = house.coordinator
    assert second is not first
    assert second.automation_enabled is False, "automation switch was not restored from the saved state"
    quiet_from = len(house.calls)
    await house.advance(minutes=30)
    assert [c for c in house.calls[quiet_from:] if c.origin == "ca:1"] == []
