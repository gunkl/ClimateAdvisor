"""Real-HA test for Issue #1637: Home Assistant quality scale, first batch of small wins.

Occupant story: in Home Assistant's own device and entity registries, a zone is a service (Climate Advisor is
software on top of the thermostat, not hardware), the entities that describe Climate Advisor itself are under
Diagnostic, the duplicated Trend sensor starts disabled on a new install, and every other entity, including the
Automation switch, is an ordinary enabled entity on the zone's device.

Real Home Assistant, the real coordinator and the real platforms. Explicit asserts only.
"""

from __future__ import annotations

import pytest

from tests_ha.house.builder import CA_DOMAIN

pytestmark = pytest.mark.house

_DIAGNOSTIC = {"heat_threshold", "cool_threshold", "last_action_time", "last_action_reason", "ai_status"}
_DISABLED_BY_DEFAULT = {"trend_direction"}


async def test_zone_is_a_service_device_with_categorized_entities(make_house) -> None:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity import EntityCategory

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    hass, entry_id = house.hass, house.ca_entry.entry_id

    devices = [
        d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        if (CA_DOMAIN, entry_id) in d.identifiers
    ]
    assert len(devices) == 1, devices
    device = devices[0]
    assert device.entry_type is dr.DeviceEntryType.SERVICE

    entries = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    by_key = {e.unique_id.removeprefix(f"{entry_id}_"): e for e in entries}
    assert len(by_key) == 22, sorted(by_key)
    for key, ent in by_key.items():
        assert ent.device_id == device.id, key
        expected_category = EntityCategory.DIAGNOSTIC if key in _DIAGNOSTIC else None
        assert ent.entity_category == expected_category, key
        if key in _DISABLED_BY_DEFAULT:
            assert ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
            assert hass.states.get(ent.entity_id) is None, key
        else:
            assert ent.disabled_by is None, key
            assert hass.states.get(ent.entity_id) is not None, key


async def test_enum_sensors_expose_their_options(make_house) -> None:
    """entity-device-class (#1637): Day Type and Occupancy Mode are ENUM sensors whose state is in options."""
    from homeassistant.helpers import entity_registry as er

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    by_key = {
        e.unique_id.removeprefix(f"{entry_id}_"): e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    }
    for key in ("day_type", "occupancy_mode"):
        state = hass.states.get(by_key[key].entity_id)
        assert state.attributes["device_class"] == "enum", key
        assert state.state in state.attributes["options"], (key, state.state)
    assert by_key["trend_direction"].original_device_class == "enum"


async def test_icon_translations_load(make_house) -> None:
    """icon-translations (#1637): Home Assistant loads icons.json for the entities and the services."""
    from homeassistant.helpers.icon import async_get_icons

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    entity_icons = await async_get_icons(house.hass, "entity", integrations=[CA_DOMAIN])
    assert entity_icons[CA_DOMAIN]["sensor"]["trend_direction"]["state"]["warming"] == "mdi:trending-up"
    assert entity_icons[CA_DOMAIN]["switch"]["automation_enabled"]["default"] == "mdi:robot"
    service_icons = await async_get_icons(house.hass, "services", integrations=[CA_DOMAIN])
    assert set(service_icons[CA_DOMAIN]) == {
        "respond_to_suggestion",
        "force_reclassify",
        "resend_briefing",
        "dump_diagnostics",
        "reset_learning_data",
    }


async def test_services_outlive_the_zone_and_reject_it_once_unloaded(make_house) -> None:
    """action-setup (#1637): the services exist while HA runs; a call for an unloaded zone fails clearly."""
    from homeassistant.exceptions import ServiceValidationError

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    await house.advance(minutes=5)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    assert hass.services.has_service(CA_DOMAIN, "force_reclassify")

    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(CA_DOMAIN, "force_reclassify")
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(CA_DOMAIN, "force_reclassify", {"entry_id": entry_id}, blocking=True)
    assert caught.value.translation_key == "unknown_zone"
