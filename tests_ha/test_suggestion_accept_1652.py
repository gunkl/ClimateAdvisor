"""Real-HA test for Issue #1652: accepting a learning suggestion changes the house, and the change survives a restart.

Occupant story: the dashboard says HVAC paused often because of open doors and the person presses Accept. Before
#1652 the card went away and nothing changed: the accepted key (``door_pause_seconds``) was no setting at all, so a
door left open for 12 minutes still paused the heat, and after a restart nothing was remembered either. Now Accept
lengthens the real door/window debounce from 10 to 15 minutes, in the running zone and in its config entry; a
12-minute opening no longer pauses the heat, even after Home Assistant restarts. "Comfort misses" moves the away
temperatures 2 °F toward home through the dashboard's own REST call, and advice (here "short departures") changes
nothing.

Real Home Assistant, the real coordinator, the real service and REST view. Explicit asserts only.
"""

from __future__ import annotations

import pytest

from tests_ha.house.builder import CA_DOMAIN

pytestmark = pytest.mark.house

_COLD_NIGHT = {
    "initial_indoor_f": 64.0,
    "outdoor_f": 35.0,
    "high_f": 42.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
}
_ZONE = {
    "fan_min_runtime_per_hour": 0,
    "sensor_debounce_seconds": 600,
    "comfort_heat": 68.0,
    "comfort_cool": 75.0,
    "sleep_heat": 64.0,
    "sleep_cool": 78.0,
    "setback_heat": 62.0,
    "setback_cool": 80.0,
}


async def _accept(house, key: str) -> None:
    await house.hass.services.async_call(
        CA_DOMAIN,
        "respond_to_suggestion",
        {"entry_id": house.ca_entry.entry_id, "action": "accept", "suggestion_key": key},
        blocking=True,
    )
    await house.advance(seconds=1)


async def test_door_pause_accept_lengthens_the_debounce_and_survives_a_restart(make_house) -> None:
    house = await make_house(fan=None, contacts=["door"], startup="settled", ca_config=_ZONE, **_COLD_NIGHT)
    try:
        await house.advance(minutes=6)
        await _accept(house, "frequent_door_pauses")
        assert house.coordinator.config["sensor_debounce_seconds"] == 900
        assert house.ca_entry.data["sensor_debounce_seconds"] == 900

        await house.restart()
        ae = house.coordinator.automation_engine
        assert house.coordinator.config["sensor_debounce_seconds"] == 900, "the accepted change was lost at restart"

        await house.open("door")
        await house.advance(minutes=12)  # longer than the old 10-minute debounce
        assert not ae._paused_by_door, "a 12-minute opening still paused the heat: the accept changed nothing"
        await house.advance(minutes=4)  # past the new 15 minutes: owner rule 3 still applies
        assert ae._paused_by_door and house.thermostat.mode == "off"
        await house.close_contact("door")
        await house.advance(minutes=20)

        # Accepting again steps to the 20-minute cap, and never beyond it.
        await _accept(house, "frequent_door_pauses")
        assert house.ca_entry.data["sensor_debounce_seconds"] == 1200
        await _accept(house, "frequent_door_pauses")
        assert house.ca_entry.data["sensor_debounce_seconds"] == 1200
        assert house.coordinator.config["sensor_debounce_seconds"] == 1200
    finally:
        await house.close()


async def test_comfort_accept_over_rest_moves_away_toward_home_and_advice_changes_nothing(
    make_house, hass_client, hass_admin_user
) -> None:
    house = await make_house(fan=None, startup="settled", ca_config=_ZONE, **_COLD_NIGHT)
    hass = house.hass
    try:
        await house.advance(minutes=6)
        # The house freezes the clock, so the fixture's token (issued at real time) is not yet valid: issue one now.
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        client = await hass_client(hass.auth.async_create_access_token(refresh))
        params = {"entry_id": house.ca_entry.entry_id}

        resp = await client.post(
            "/api/climate_advisor/respond_suggestion",
            params=params,
            json={"action": "accept", "suggestion_key": "comfort_violations"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["changes"] == {"setback_heat": 64.0, "setback_cool": 78.0}
        assert body["changes_text"] == [
            "Away Heating Temperature: 62°F → 64°F",
            "Away Cooling Temperature: 80°F → 78°F",
        ]
        assert (house.ca_entry.data["setback_heat"], house.ca_entry.data["setback_cool"]) == (64.0, 78.0)

        before = dict(house.ca_entry.data)
        resp = await client.post(
            "/api/climate_advisor/respond_suggestion",
            params=params,
            json={"action": "accept", "suggestion_key": "short_departures"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["changes"] == {} and body["changes_text"] == []
        assert dict(house.ca_entry.data) == before

        await house.restart()
        config = house.coordinator.config
        assert (config["setback_heat"], config["setback_cool"]) == (64.0, 78.0)
        assert house.coordinator.automation_engine.config is config  # the engine decides from these values
    finally:
        await house.close()
