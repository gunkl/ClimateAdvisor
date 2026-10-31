"""Real-HA tests for bugs that escaped the fast tests (Step 2 Increment 3, design section 6).

Each test states the occupant story, names the commit it was proven against, and sits next to a positive
control: the same flow with the bug's trigger absent, so the instrument demonstrably sees what the bug hides.

Prove a test is not vacuous with ``scripts/revert_proof.py`` (fails on the pre-fix tree, passes on HEAD):

    python scripts/revert_proof.py b686a05^ tests_ha/test_escaped_bugs.py::test_observe_only_does_not_release_blower
    python scripts/revert_proof.py 2507a19^ tests_ha/test_escaped_bugs.py::test_manual_fan_survives_restart
    python scripts/revert_proof.py 1df0246  tests_ha/test_escaped_bugs.py::test_reloaded_zone_leaves_no_ghost

No mirror logic: every flow goes through the real config entry, the real switch entity, the real services and
the real state file written by a first instance and read by a second.
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.house


def _ca(calls, instance: str | None = None):
    """Commands Climate Advisor sent (optionally one instance), without weather-service reads."""
    return [c for c in calls if c.origin.startswith("ca:") and (instance is None or c.origin == instance)]


# --------------------------------------------------------------------------------------------------
# #1108: "automation off" means hands off
# --------------------------------------------------------------------------------------------------


async def _house_that_owns_the_blower(make_house):
    """CA runs its own min-runtime blower cycle at start-up, so it owns the thermostat fan (fan_active)."""
    house = await make_house(
        fan="hvac_fan", startup="cold", initial_indoor_f=72.0, outdoor_f=72.0, high_f=75.0, low_f=60.0
    )
    await house.advance(seconds=20)
    assert house.thermostat.fan_mode == "on", "precondition: CA started the thermostat blower on its own"
    assert house.coordinator.automation_engine._fan_active is True
    return house


async def test_observe_only_does_not_release_blower(make_house) -> None:
    """#1108. The person switched Climate Advisor's automation OFF to run the fan themselves.

    Before the fix, saving settings (fan control disabled, zone reloaded) made CA release the blower it believed
    it owned: one ``climate.set_fan_mode auto``, switching the person's fan off while 'automation off' promised
    hands off. Proven against ``b686a05^``.
    """
    house = await _house_that_owns_the_blower(make_house)
    await house.set_automation(False)
    assert house.coordinator.automation_engine.dry_run is True

    calls_before = len(house.calls)
    await house.reload_zone(fan_mode="disabled")  # a settings save with fan control turned off
    await house.advance(minutes=6)  # past the 300 s start-up reconcile

    fan_releases = [c for c in house.calls[calls_before:] if c.domain == "climate" and c.service == "set_fan_mode"]
    assert fan_releases == [], f"observe-only CA still commanded the fan: {fan_releases}"
    assert house.thermostat.fan_mode == "on", "the person's blower was switched off"


async def test_positive_control_automation_on_releases_blower(make_house) -> None:
    """#1108 control: with automation ON the same flow hands the blower back once, by design."""
    house = await _house_that_owns_the_blower(make_house)
    assert house.coordinator.automation_engine.dry_run is False

    calls_before = len(house.calls)
    await house.reload_zone(fan_mode="disabled")
    await house.advance(minutes=6)

    releases = [c for c in _ca(house.calls[calls_before:]) if c.service == "set_fan_mode"]
    assert len(releases) == 1 and releases[0].data["fan_mode"] == "auto", releases
    assert house.thermostat.fan_mode == "auto"


async def test_observe_only_sends_no_thermostat_commands(make_house) -> None:
    """#1108 mirror: with automation off the thermostat itself is left alone for half an hour, hot house or not."""
    house = await make_house(initial_indoor_f=72.0, outdoor_f=95.0, high_f=98.0, low_f=75.0, startup="settled")
    await house.set_automation(False)
    calls_before = len(house.calls)

    house.set_indoor(84.0)  # warm enough that a live CA would act
    await house.advance(minutes=30)

    commands = [c for c in house.calls[calls_before:] if c.domain == "climate"]
    assert commands == [], f"observe-only CA commanded the thermostat: {commands}"


async def test_positive_control_automation_on_commands_thermostat(make_house) -> None:
    """#1108 mirror control: the same hot house with automation ON does get a thermostat command."""
    house = await make_house(initial_indoor_f=72.0, outdoor_f=95.0, high_f=98.0, low_f=75.0, startup="settled")
    calls_before = len(house.calls)

    house.set_indoor(84.0)
    await house.advance(minutes=30)

    commands = [c for c in _ca(house.calls[calls_before:]) if c.domain == "climate"]
    assert commands, "positive control: a live CA never commanded the thermostat for an 84F house"


# --------------------------------------------------------------------------------------------------
# #1045: a manually started fan survives a restart
# --------------------------------------------------------------------------------------------------

_QUIET_WHF_HOUSE = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "initial_indoor_f": 72.0,
    "outdoor_f": 72.0,
    "high_f": 74.0,
    "low_f": 62.0,
    # No scheduled min-runtime cycle: the only reason the fan runs is the person pressing it.
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
}


async def _person_turns_the_fan_on(make_house):
    house = await make_house(**_QUIET_WHF_HOUSE)
    await house.open("window")
    await house.advance(minutes=2)
    assert not house.fan.is_on, "precondition: nothing but the person should be running the fan"
    # A refresh just ran, so the coordinator's 10 s refresh cooldown is open. Without this, the press is
    # raced by an immediate reconcile that switches the fan off before the override is recorded; that is a
    # separate production finding (test_press_is_not_raced_by_immediate_reconcile below).
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(True)
    await house.advance(seconds=90)  # CA's own listener records the manual override and persists its grace
    ae = house.coordinator.automation_engine
    assert ae._fan_override_active is True, "precondition: CA noticed the person turned the fan on"
    return house


async def test_manual_fan_survives_restart(make_house) -> None:
    """#1045. The person turns the whole-house fan on at the wall; HA restarts a few minutes later.

    Before the fix CA forgot the override at restart and its start-up reconcile switched the fan back off, minutes
    after the person turned it on. Proven against ``2507a19^``. The first instance writes the state file and the
    second reads it, so the save/restore key round trip is part of what is tested.
    """
    house = await _person_turns_the_fan_on(make_house)

    calls_before = len(house.calls)
    await house.restart()  # unload, set up again on the same state file, settle past the 300 s reconcile

    fan_off = [c for c in house.calls[calls_before:] if c.domain == "fan" and c.service == "turn_off"]
    assert fan_off == [], f"CA switched the person's fan off after the restart: {fan_off}"
    assert house.fan.is_on
    assert house.coordinator.automation_engine._fan_override_active is True, "the manual override was not restored"


async def test_positive_control_expired_manual_fan_is_turned_off_after_restart(make_house) -> None:
    """#1045 control: if the 3 h manual grace ran out while HA was down, the reconcile does turn the fan off."""
    house = await _person_turns_the_fan_on(make_house)

    calls_before = len(house.calls)
    await house.restart(down_for_seconds=3 * 3600 + 300)  # CA is down past the end of the saved grace

    assert house.fan.is_on or [c for c in house.calls[calls_before:] if c.domain == "fan"]
    fan_off = [c for c in house.calls[calls_before:] if c.domain == "fan" and c.service == "turn_off"]
    assert fan_off, "control: with the saved override expired, CA should have reconciled the running fan"
    assert all(c.origin.startswith("ca:") for c in fan_off)
    assert not house.fan.is_on


# --------------------------------------------------------------------------------------------------
# #1102 / #1117: the ghost copy, and the forecast path that hid it
# --------------------------------------------------------------------------------------------------

_HOT_HOUSE_COOL_OUTDOORS = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "cold",
    "initial_indoor_f": 82.0,
    "outdoor_f": 60.0,
    "high_f": 70.0,
    "low_f": 52.0,
    "ca_config": {"sensor_debounce_seconds": 60},
}


async def test_reloaded_zone_leaves_no_ghost(make_house) -> None:
    """#1102. After saving settings the old zone's 300 s timer woke a dead copy of Climate Advisor.

    The ghost then kept commanding the whole-house fan after the person turned it off. Reload at +172 s (inside the
    start-up window, as in the field), have the person switch the fan off, run to +601 s: the replaced coordinator
    must run no update cycle and issue no command. Proven against ``1df0246``.
    """
    house = await make_house(**_HOT_HOUSE_COOL_OUTDOORS)
    await house.open("window")
    await house.advance(seconds=172)

    first = house.coordinator
    first_cycles: list = []
    real_update = first._async_update_data

    async def _spy():
        first_cycles.append(1)
        return await real_update()

    first._async_update_data = _spy

    await house.reload_zone()
    assert house.coordinator is not first
    await house.occupant_presses_fan(False)
    first_cycles.clear()
    calls_before = len(house.calls)

    await house.advance(seconds=601 - 172)

    ghost_calls = _ca(house.calls[calls_before:], "ca:1")
    assert not first_cycles, f"the replaced coordinator ran {len(first_cycles)} update cycle(s) after the reload"
    assert not ghost_calls, f"the replaced instance kept commanding devices: {ghost_calls}"


async def test_positive_control_zone_without_reload_runs_its_regular_cycle(make_house, caplog) -> None:
    """#1117 control: with no reload the regular update cycle runs and reaches the fan decision.

    The old fast harness crashed in the forecast step, so this path could not be watched. Here the real weather
    entity answers ``weather.get_forecasts`` and CA commands the fan; no forecast fallback is logged.
    """
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(**_HOT_HOUSE_COOL_OUTDOORS)
    await house.open("window")
    cycles: list = []
    real_update = house.coordinator._async_update_data

    async def _spy():
        cycles.append(1)
        return await real_update()

    house.coordinator._async_update_data = _spy

    await house.advance(minutes=10)

    assert cycles, "no regular update cycle ran in 10 minutes"
    assert [c for c in _ca(house.calls) if c.domain == "fan"], "CA never reached a fan decision"
    assert not [r for r in caplog.records if "get_forecasts service call failed" in r.getMessage()]


async def test_person_switching_fan_off_is_respected_by_the_new_instance(make_house) -> None:
    """#1102 control: after a reload the person's 'fan off' is honored by the new instance for the manual grace.

    The seed's manual grace is 15 minutes, after which nat-vent cycling may legitimately run the fan again, so the
    window checked here is 10 minutes (plus the 90 s CA needed to notice).
    """
    house = await make_house(**_HOT_HOUSE_COOL_OUTDOORS)
    await house.open("window")
    await house.advance(minutes=8)
    await house.reload_zone()
    await house.advance(seconds=301)  # past the new instance's start-up window
    ae = house.coordinator.automation_engine
    for _ in range(90):  # wait until the fan runs outside any of CA's own settling windows
        if house.fan.is_on and not ae._grace_active:
            break
        await house.advance(minutes=1)
    assert house.fan.is_on and not ae._grace_active, "precondition: no moment when the person could switch it off"
    await house.occupant_presses_fan(False)
    await house.advance(seconds=90)
    assert ae._grace_active, "precondition: CA noticed the person switching the fan off (starts a grace period)"
    calls_before = len(house.calls)

    await house.advance(minutes=10)

    fan_on = [c for c in _ca(house.calls[calls_before:]) if c.domain == "fan" and c.service == "turn_on"]
    assert fan_on == [], f"CA turned the fan back on after the person switched it off: {fan_on}"


# --------------------------------------------------------------------------------------------------
# Production finding surfaced while building the #1045 flow (not a fix; a strict xfail that flips when fixed)
# --------------------------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,  # a hang (pytest-timeout) or a settle error must stay a real failure, not satisfy the xfail
    reason=(
        "Issue #1130: the person turning the whole-house fan on "
        "is raced by the Issue #510 immediate refresh. _async_fan_entity_changed() schedules async_request_refresh() "
        "BEFORE it evaluates the override; HA starts tasks eagerly and the coordinator's refresh debouncer is "
        "immediate unless a refresh ran in the last 10 s, so _async_update_data() runs the untracked-fan backstop "
        "reconcile first and switches the fan off ('backstop_30min reconcile - fan running without CA warrant') in "
        "the same event-loop tick. Delete this marker when fixed."
    ),
)
async def test_press_is_not_raced_by_immediate_reconcile(make_house) -> None:
    """The person turns the fan on at the wall (no refresh happened in the last 10 s): CA must not turn it off."""
    house = await make_house(**_QUIET_WHF_HOUSE)
    await house.open("window")
    await house.advance(minutes=2)
    calls_before = len(house.calls)

    await house.occupant_presses_fan(True)
    await house.advance(seconds=90)

    fan_off = [c for c in _ca(house.calls[calls_before:]) if c.domain == "fan" and c.service == "turn_off"]
    assert fan_off == [], f"CA switched off the fan the person had just turned on: {fan_off}"
    assert house.fan.is_on
