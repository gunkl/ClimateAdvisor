"""Real-HA test for Issue #1652 (owner decision): accepting a learning suggestion changes no setting.

Occupant story: the person presses "Got it" (the ``accept`` action) on a suggestion, through the dashboard's REST
call or the ``respond_to_suggestion`` service. Learning must never lengthen the door/window pause or change the
away temperatures: the zone's settings and its config entry stay exactly as the person set them, also after Home
Assistant restarts. The removed suggestions (``frequent_door_pauses``, ``comfort_violations``) are rejected as
unknown on both paths, and a door left open past the person's own 10-minute delay still pauses the heat.

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
    "setback_heat": 62.0,
    "setback_cool": 80.0,
}


async def test_accept_changes_no_setting_and_removed_keys_are_rejected(
    make_house, hass_client, hass_admin_user
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    house = await make_house(fan=None, contacts=["door"], startup="settled", ca_config=_ZONE, **_COLD_NIGHT)
    hass = house.hass
    try:
        await house.advance(minutes=6)
        entry_before = dict(house.ca_entry.data)
        config_before = {k: house.coordinator.config.get(k) for k in _ZONE}

        # The service: every remaining suggestion is an acknowledgement.
        # Issue #1695: "Turn on Humid Overcool" is an acknowledgement too: the option stays off (checked below).
        for key in (
            "short_departures",
            "frequent_overrides",
            "low_window_compliance",
            "high_runtime_mild_days",
            "humid_overcool",
        ):
            await hass.services.async_call(
                CA_DOMAIN,
                "respond_to_suggestion",
                {"entry_id": house.ca_entry.entry_id, "action": "accept", "suggestion_key": key},
                blocking=True,
            )
        # The removed suggestions are unknown keys now.
        for key in ("frequent_door_pauses", "comfort_violations"):
            with pytest.raises(ServiceValidationError) as caught:
                await hass.services.async_call(
                    CA_DOMAIN,
                    "respond_to_suggestion",
                    {"entry_id": house.ca_entry.entry_id, "action": "accept", "suggestion_key": key},
                    blocking=True,
                )
            assert caught.value.translation_key == "unknown_suggestion"

        # The dashboard's REST call.
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        client = await hass_client(hass.auth.async_create_access_token(refresh))
        params = {"entry_id": house.ca_entry.entry_id}
        resp = await client.post(
            "/api/climate_advisor/respond_suggestion",
            params=params,
            json={"action": "accept", "suggestion_key": "thermal_model_ready"},
        )
        assert resp.status == 200
        assert await resp.json() == {"status": "ok", "acknowledged": "thermal_model_ready", "changes": {}}
        resp = await client.post(
            "/api/climate_advisor/respond_suggestion",
            params=params,
            json={"action": "accept", "suggestion_key": "frequent_door_pauses"},
        )
        assert resp.status == 400

        assert dict(house.ca_entry.data) == entry_before
        assert {k: house.coordinator.config.get(k) for k in _ZONE} == config_before
        assert not house.coordinator.config.get("humid_overcool_enabled", False)  # #1695: never turned on

        await house.restart()
        assert dict(house.ca_entry.data) == entry_before
        assert {k: house.coordinator.config.get(k) for k in _ZONE} == config_before
        assert not house.coordinator.config.get("humid_overcool_enabled", False)

        # The person's own 10-minute delay still decides: a 12-minute opening pauses the heat (owner rule 3).
        ae = house.coordinator.automation_engine
        await house.open("door")
        await house.advance(minutes=12)
        assert ae._paused_by_door and house.thermostat.mode == "off"
    finally:
        await house.close()
