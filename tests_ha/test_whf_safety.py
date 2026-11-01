"""Real-HA tests for the whole-house-fan safety enforcer (Issue #1144, phase 1).

Occupant story: a person turned the whole-house fan on (HomeKit, wall switch or RF remote) while every monitored
door and window was closed. The fan then ran for ~55 minutes pulling outdoor air through a sealed house because
Climate Advisor only looked for that situation on a few events and never when the fan was turned on while the house
was already sealed. Rule 1 (owner): the fan never runs on a sealed house and is off within 30 s, override or not.

Prove a test is not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.house


def _ca(calls):
    return [c for c in calls if c.origin.startswith("ca:")]


def _ca_fan_off(calls):
    return [c for c in _ca(calls) if c.domain == "fan" and c.service == "turn_off"]


def _sealed_house(**extra):
    cfg = {
        "contacts": ["window"],
        "startup": "settled",
        "initial_indoor_f": 72.0,
        "outdoor_f": 72.0,
        "high_f": 74.0,
        "low_f": 62.0,
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    }
    cfg.update(extra)
    return cfg


async def _advance_finely(house, seconds: int) -> None:
    """Advance in 1 s slices. The harness steps its clock 30 s at a time, which would fire every Climate Advisor
    timer (10 s settle, 12 s verify, 15 s tick) at the end of the step and make a "within 30 s" claim untestable."""
    for _ in range(int(seconds)):
        await house.advance(seconds=1)


async def _press_after_refresh(house, on: bool = True):
    """Press the fan outside the coordinator's refresh cooldown (the separate #1130 race)."""
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(on)


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_fan_pressed_on_a_sealed_house_is_stopped_within_30s(make_house, fan) -> None:
    house = await make_house(fan=fan, **_sealed_house())
    assert not house.fan.is_on
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)

    assert _ca_fan_off(house.calls[calls_before:]), "CA never sent fan.turn_off for a fan running on a sealed house"
    assert not house.fan.is_on, "the whole-house fan is still running on a sealed house 30 s after it was turned on"
    ae = house.coordinator.automation_engine
    assert ae._fan_override_active is False, "a safety stop must end the fan override"
    assert ae._grace_active is False, "a safety stop must end the fan grace period"


async def test_positive_control_fan_pressed_with_window_open_keeps_running(make_house) -> None:
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    await _advance_finely(house, 60)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_sealed_stop_inside_the_300s_window_after_a_restart(make_house) -> None:
    """Start-up coalescing suppresses the fan listeners for 300 s; a sealed fan must still be stopped in 30 s."""
    house = await make_house(fan="whf", **_sealed_house())
    await house.restart(settle=False)
    await house.advance(seconds=20)
    calls_before = len(house.calls)

    await house.occupant_presses_fan(True)
    await _advance_finely(house, 45)

    assert _ca_fan_off(house.calls[calls_before:]), "no stop inside the post-restart coalescing window"
    assert not house.fan.is_on


async def test_occupant_repress_after_a_safety_stop_is_stopped_again(make_house) -> None:
    """HomeKit-style on/off/on right after a stop: the 300 s toggle rate limit must not shield the fan."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert not house.fan.is_on
    calls_before = len(house.calls)

    await house.occupant_presses_fan(True)
    await _advance_finely(house, 30)

    assert _ca_fan_off(house.calls[calls_before:]), "the second press was not stopped"
    assert not house.fan.is_on


async def test_window_closing_under_an_override_stops_the_fan(make_house) -> None:
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 60)
    assert house.fan.is_on
    calls_before = len(house.calls)

    await house.close_contact("window")
    await _advance_finely(house, 60)

    assert _ca_fan_off(house.calls[calls_before:])
    assert not house.fan.is_on


async def test_user_turning_heat_on_stops_an_override_only_fan(make_house) -> None:
    """Rule 2, newest action wins: the person sets heat while an override-only whole-house fan runs.

    Before #1144 the stand-down reported ALREADY_IN_STATE (``_fan_active`` is False for an override-only fan)
    and the fan kept running next to the heat.
    """
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert house.fan.is_on
    calls_before = len(house.calls)

    await house.occupant_sets_thermostat(mode="heat")
    await _advance_finely(house, 45)

    assert _ca_fan_off(house.calls[calls_before:]), "the fan kept running next to the occupant's heat"
    assert not house.fan.is_on


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Separate finding surfaced by #1144 (pre-existing on origin/main, see the #1144 phase-1 report): with a "
        "fan-override grace active, the occupant's heat set during a door/window pause is dropped from the "
        "pending-confirmation state and the next update cycle turns it back off. Owner rule 3 says a manual "
        "override may turn HVAC on while a window is open. Delete this marker when fixed."
    ),
)
async def test_occupants_heat_survives_the_next_cycle_while_a_window_is_open(make_house) -> None:
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)

    await house.occupant_sets_thermostat(mode="heat")
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await _advance_finely(house, 5)

    assert house.thermostat.mode == "heat"


async def test_zero_configured_sensors_the_fan_is_never_stopped(make_house) -> None:
    """Owner decision 1: with no door/window sensor configured sealed-ness cannot be judged."""
    house = await make_house(fan="whf", **_sealed_house(contacts=[]))
    await _press_after_refresh(house, True)
    calls_before = len(house.calls)

    await _advance_finely(house, 90)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_window_opening_inside_the_10s_settle_is_not_fought(make_house) -> None:
    """Owner decision 3: someone who opens a window and presses the fan in one motion is not stopped."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    calls_before = len(house.calls)
    await _advance_finely(house, 4)

    await house.open("window")
    await _advance_finely(house, 60)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_unavailable_sensor_is_trusted_for_60s_then_counted_closed(make_house) -> None:
    """Owner decision 2: an unavailable sensor gets 60 s of benefit of the doubt, then counts as closed."""
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 20)
    assert house.fan.is_on
    calls_before = len(house.calls)

    house.hass.states.async_set("binary_sensor.house_window", "unavailable")
    await _advance_finely(house, 45)
    assert not _ca_fan_off(house.calls[calls_before:]), "stopped before the sensor had been unavailable for 60 s"

    await _advance_finely(house, 45)
    assert _ca_fan_off(house.calls[calls_before:]), "an unavailable sensor never counted as closed"
    assert not house.fan.is_on


@pytest.mark.parametrize("seconds_after_press", [5, 12, 16, 20])
async def test_no_lingering_timers_after_unload(make_house, seconds_after_press) -> None:
    """Unload at every stage of an episode (settling, just stopped with the verify timer armed, later)."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    await _advance_finely(house, seconds_after_press)
    await house.close()  # the harness' verify_cleanup fails the test on any lingering timer
