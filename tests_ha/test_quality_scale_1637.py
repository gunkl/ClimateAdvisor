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
