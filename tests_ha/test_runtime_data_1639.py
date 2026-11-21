"""Issue #1639 (quality scale Bronze runtime-data): each zone's coordinator lives on its config entry.

Occupant view: the owner has several zones, saves one zone's settings (a reload) or deletes a zone. The dashboard,
the REST views and the services must then answer for the zones that are really running: never for an old copy of
the reloaded zone and never for a zone that is gone. Before #1639 the coordinators sat in ``hass.data[DOMAIN]``,
which Climate Advisor filled and emptied by hand; now each is the zone's ``entry.runtime_data`` and a zone is listed
only while real Home Assistant reports its entry LOADED (HA drops ``runtime_data`` itself on unload).

Real Home Assistant, three loaded zones:
- every loaded zone is enumerated, in the stable config-entry order, and nothing is kept in ``hass.data[DOMAIN]``;
- unloading one zone removes it (no ``runtime_data`` left on its entry) and keeps the other two reachable;
- reloading a zone puts a new coordinator on its entry, and zone_registry resolves the new one, not the old one;
- a service call naming the unloaded zone is refused with the translated unknown_zone error.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor import zone_registry
from tests_ha.house.builder import CA_DOMAIN
from tests_ha.test_zone_param_1418 import _add_zone


async def test_zones_live_on_their_entries_through_unload_and_reload(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    added: list[MockConfigEntry] = []
    try:
        for name, heat, cool in (("Upstairs", 66.0, 78.0), ("Studio", 64.0, 80.0)):
            added.extend(await _add_zone(hass, house, name, heat, cool))
        await house.advance(minutes=6)
        zone1, upstairs, studio = house.ca_entry, added[1], added[3]
        zones = (zone1, upstairs, studio)

        # Enumeration: every loaded zone, each one's own coordinator, in the stable async_entries order.
        assert CA_DOMAIN not in hass.data
        loaded = zone_registry.loaded_zones(hass)
        assert list(loaded) == [e.entry_id for e in hass.config_entries.async_entries(CA_DOMAIN)]
        assert list(loaded) == [e.entry_id for e in zones]
        for entry in zones:
            assert loaded[entry.entry_id] is entry.runtime_data
        assert [z["title"] for z in zone_registry.list_zones(hass)] == [e.title for e in zones]

        # Unload one zone: gone from every lookup, nothing stale left on its entry, the others untouched.
        studio_coordinator = studio.runtime_data
        assert await hass.config_entries.async_unload(studio.entry_id)
        await hass.async_block_till_done()
        assert studio.state is ConfigEntryState.NOT_LOADED
        assert not hasattr(studio, "runtime_data")
        assert zone_registry.get_coordinator(hass, studio.entry_id) is None
        assert studio_coordinator not in list(zone_registry.iter_coordinators(hass))
        assert list(zone_registry.loaded_zones(hass)) == [zone1.entry_id, upstairs.entry_id]
        with pytest.raises(ServiceValidationError) as caught:
            await hass.services.async_call(
                CA_DOMAIN, "force_reclassify", {"entry_id": studio.entry_id}, blocking=True
            )
        assert caught.value.translation_key == "unknown_zone"

        # Reload another zone (a settings save): its entry carries a NEW coordinator, which is the one resolved.
        old = upstairs.runtime_data
        assert await hass.config_entries.async_reload(upstairs.entry_id)
        await hass.async_block_till_done()
        assert upstairs.state is ConfigEntryState.LOADED
        assert upstairs.runtime_data is not old
        assert zone_registry.get_coordinator(hass, upstairs.entry_id) is upstairs.runtime_data
        assert old not in list(zone_registry.iter_coordinators(hass))
        assert len(list(zone_registry.iter_coordinators(hass))) == 2
    finally:
        for entry in reversed(added):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
