"""Real-HA tests for Issue #1349: observe-only survives a Home Assistant restart across midnight.

Occupant story: on a cold January evening the owner has the thermostat Off and Climate Advisor's Automation
switch off, so it only watches. Home Assistant goes down at 23:20 and comes back after midnight. Before the fix
the switch came back on with no notice and, when the 5-minute startup window ended, Climate Advisor switched
the thermostat to heat 70 F against the owner's choice. A restart on the same day kept the choice.

Controls: a 10-minute same-day outage (always worked) and an automation-on house restarted across midnight (CA
heats after the window, so the observe-only test is not passing because nothing would have been sent anyway).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_START = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)


def _switch_state(house) -> str:
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(house.hass)
    ent = next(
        e
        for e in er.async_entries_for_config_entry(reg, house.ca_entry.entry_id)
        if e.unique_id == f"{house.ca_entry.entry_id}_automation_enabled"
    )
    return house.state(ent.entity_id).state


async def _cold_house_off(make_house, *, automation: bool):
    house = await make_house(
        start=_START,
        initial_indoor_f=64.0,
        outdoor_f=35.0,
        high_f=45.0,
        low_f=28.0,
        initial_hvac_mode="off",
        startup="cold",
    )
    if not automation:
        await house.set_automation(False)
    await house.advance(minutes=20)
    return house


@pytest.mark.parametrize(
    "down_for_s",
    [
        pytest.param(10 * 60, id="same_day_10min"),
        pytest.param(75 * 60, id="across_midnight_75min"),
        pytest.param(26 * 3600, id="two_dates_26h"),
    ],
)
async def test_observe_only_survives_restart(make_house, down_for_s):
    house = await _cold_house_off(make_house, automation=False)
    assert house.thermostat.mode == "off"
    assert house.coordinator.automation_engine.dry_run is True
    calls_before = len(house.calls)

    await house.restart(down_for_seconds=down_for_s, settle=False)

    co = house.coordinator
    # Through the 300 s startup window and past its end, minute by minute.
    for minute in range(1, 9):
        await house.advance(minutes=1)
        assert co.automation_enabled is False, f"automation came back on at +{minute} min"
        assert co.automation_engine.dry_run is True, f"dry_run cleared at +{minute} min"
        assert _switch_state(house) == "off", f"Automation switch reads on at +{minute} min"
        assert house.thermostat.mode == "off", f"thermostat touched at +{minute} min: {house.thermostat.mode}"
    await house.advance(minutes=30)
    assert house.thermostat.mode == "off"
    ca_climate = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "climate"]
    assert ca_climate == [], f"Climate Advisor commanded the thermostat in observe-only: {ca_climate}"


async def test_automation_on_control_across_midnight(make_house):
    """Control: with automation on the same restart leads to heat, so the scene really calls for heating."""
    house = await _cold_house_off(make_house, automation=True)
    calls_before = len(house.calls)

    await house.restart(down_for_seconds=75 * 60, settle=False)
    await house.advance(minutes=38)

    co = house.coordinator
    assert co.automation_enabled is True
    assert co.automation_engine.dry_run is False
    assert _switch_state(house) == "on"
    assert house.thermostat.mode == "heat"
    assert any(c.origin.startswith("ca:") and c.domain == "climate" for c in house.calls[calls_before:])
