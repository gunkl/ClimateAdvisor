"""Coverage for __init__.py migration and zone-removal edge paths (Silver test-coverage, #1644).

Occupant view: an owner upgrading from an old release whose weather entity was renamed keeps a working zone
when Home Assistant has exactly one weather entity (it is picked up), and is told to pick one in the settings
when it is ambiguous; stored preheat settings of the wrong type come back as the documented defaults instead
of breaking the zone. Deleting a zone never fails because its files could not be removed.

Real Home Assistant: real config entries and state machine; ``async_migrate_entry`` / ``async_remove_entry``
are called the way Home Assistant calls them.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor import async_migrate_entry, async_remove_entry
from tests_ha.house.builder import CA_DOMAIN

_PKG = "custom_components.climate_advisor"


def _entry(hass: HomeAssistant, version: int, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=CA_DOMAIN,
        title="Zone",
        version=version,
        data={"climate_entity": "climate.zone", "weather_entity": "weather.old_name", **data},
    )
    entry.add_to_hass(hass)
    return entry


async def test_renamed_weather_entity_is_resolved_when_only_one_exists(hass: HomeAssistant, caplog) -> None:
    hass.states.async_set("weather.backyard", "sunny")
    entry = _entry(hass, 5)
    with caplog.at_level(logging.WARNING):
        assert await async_migrate_entry(hass, entry) is True
    assert entry.data["weather_entity"] == "weather.backyard"
    assert "auto-resolved to 'weather.backyard'" in caplog.text
    assert entry.version > 5


async def test_renamed_weather_entity_is_kept_and_warned_when_ambiguous(hass: HomeAssistant, caplog) -> None:
    hass.states.async_set("weather.one", "sunny")
    hass.states.async_set("weather.two", "rainy")
    entry = _entry(hass, 5)
    with caplog.at_level(logging.WARNING):
        assert await async_migrate_entry(hass, entry) is True
    assert entry.data["weather_entity"] == "weather.old_name"
    assert "cannot be auto-resolved" in caplog.text


async def test_v10_learning_toggles_keep_valid_values_and_repair_bad_ones(hass: HomeAssistant) -> None:
    hass.states.async_set("weather.old_name", "sunny")
    entry = _entry(hass, 10, adaptive_preheat_enabled=False, adaptive_setback_enabled="yes")
    assert await async_migrate_entry(hass, entry) is True
    assert entry.data["adaptive_preheat_enabled"] is False  # a real bool the owner chose is kept
    assert entry.data["adaptive_setback_enabled"] is True  # a wrong type falls back to the default
    assert entry.data["weather_bias_enabled"] is True  # a missing one gets the default


async def test_v11_preheat_values_of_the_wrong_type_get_defaults(hass: HomeAssistant) -> None:
    hass.states.async_set("weather.old_name", "sunny")
    entry = _entry(
        hass,
        11,
        min_preheat_minutes="soon",
        max_preheat_minutes=200,
        preheat_safety_margin="lots",
        max_setback_depth_f=6.0,
    )
    assert await async_migrate_entry(hass, entry) is True
    assert entry.data["min_preheat_minutes"] == 30
    assert entry.data["max_preheat_minutes"] == 200
    assert entry.data["preheat_safety_margin"] == 1.3
    assert entry.data["max_setback_depth_f"] == 6.0


async def test_removing_a_zone_survives_a_failed_file_cleanup(hass: HomeAssistant, caplog) -> None:
    entry = _entry(hass, 21)
    with (
        patch(f"{_PKG}.remove_entry_scoped_files", side_effect=OSError("read-only")),
        caplog.at_level(logging.ERROR),
    ):
        await async_remove_entry(hass, entry)  # must not raise
    assert f"could not clean up files of removed zone {entry.entry_id}" in caplog.text
