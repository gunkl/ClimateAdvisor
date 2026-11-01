"""Real-HA test: a Home Assistant restart must not make Climate Advisor alert or command (Issue #1183).

Occupant story: every restart (an update, a deploy, a manual restart) sent a false "2 entities aren't
responding" email about the whole-house fan, and a thermostat command that failed with "Connection lost".
Nothing was wrong: HA had already fired EVENT_HOMEASSISTANT_STOP and was unloading the fan's integration, and
its entity going unavailable triggered a full update cycle. A restart never unloads Climate Advisor's config
entry, so the #1102 shut-down latch never engaged.

Prove the test is not vacuous with ``scripts/revert_proof.py origin/main <test>``; the positive control shows
the same steps without the stop do alert.
"""

from __future__ import annotations

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState

pytestmark = pytest.mark.house


async def _quiet_house(make_house):
    """A settled house whose startup reconcile has finished. Its fan.turn_off would otherwise answer the
    unavailable fan below by setting it back to "off" (the fake device obeys commands; a real unloaded one
    would not)."""
    house = await make_house(fan="whf", startup="settled")
    await house.advance(minutes=2)
    assert not house.fan.is_on
    return house


async def _fan_goes_unavailable(house) -> None:
    """HA unloads the fan's integration: its entity goes unavailable, then the next update cycle runs."""
    house.hass.states.async_set(house.fan.command_entity, "unavailable", {"friendly_name": "Whole House Fan"})
    await house.advance(seconds=3)
    await house.coordinator.async_request_refresh()
    await house.advance(seconds=30)


async def _cold_house_while_fan_unavailable(house) -> None:
    """The fan goes unavailable and the house is cold: running, CA alerts and commands heat (the control)."""
    await _fan_goes_unavailable(house)
    house.set_indoor(60.0)
    await house.coordinator.async_request_refresh()
    await house.advance(seconds=30)


async def test_positive_control_unavailable_fan_alerts_while_running(make_house) -> None:
    house = await _quiet_house(make_house)
    notified_before = len(house.notifications)
    calls_before = len(house.calls)

    await _cold_house_while_fan_unavailable(house)

    assert len(house.notifications) > notified_before, "control: an unavailable fan should alert while HA runs"
    ca_climate = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "climate"]
    assert ca_climate, "control: a cold house should get a thermostat command while HA runs"


async def test_no_alert_or_command_after_home_assistant_starts_stopping(make_house) -> None:
    house = await _quiet_house(make_house)
    hass = house.hass
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await house.advance(seconds=1)
    notified_before = len(house.notifications)
    calls_before = len(house.calls)

    try:
        await _cold_house_while_fan_unavailable(house)

        new_notifications = house.notifications[notified_before:]
        ca_calls = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:")]
        assert not new_notifications, f"CA alerted after HA began stopping: {new_notifications}"
        assert not ca_calls, f"CA commanded devices after HA began stopping: {ca_calls}"
    finally:
        hass.set_state(CoreState.running)
