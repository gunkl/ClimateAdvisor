"""Issue #1637: Home Assistant quality scale, first batch of small wins.

Occupant story: none of this changes what the house does. It changes how Climate Advisor shows up in Home
Assistant: every entity keeps its unique id (so names, history, dashboards and automations survive the upgrade),
the zone is shown as a service rather than a piece of hardware, the entities that describe Climate Advisor itself
are filed under Diagnostic, and the one duplicated sensor (Trend) starts disabled on new installs only.

These tests pin each rule so a later change cannot silently undo it:
- common-modules / devices: one shared base entity and device (``entity.py``), ``entry_type=SERVICE``;
- entity-unique-id: the unique ids are exactly the ones every existing install already has;
- entity-category / entity-disabled-by-default: which entities, and only those;
- parallel-updates: both platforms set ``PARALLEL_UPDATES = 0``;
- exception-translations: the new ``update_failed`` message exists in every language and keeps its placeholder.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from homeassistant.helpers.device_registry import DeviceEntryType  # noqa: E402
from homeassistant.helpers.entity import EntityCategory  # noqa: E402

from custom_components.climate_advisor import entity as entity_mod  # noqa: E402
from custom_components.climate_advisor import sensor as sensor_platform  # noqa: E402
from custom_components.climate_advisor import switch as switch_platform  # noqa: E402
from custom_components.climate_advisor.const import DOMAIN  # noqa: E402
from custom_components.climate_advisor.entity import ClimateAdvisorEntity  # noqa: E402

_PKG = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"

# The unique-id suffixes every existing install has (``<entry_id>_<key>``). Changing one orphans the old entity:
# the user loses its history, name and every automation or dashboard that refers to it.
_SENSOR_KEYS = {
    "day_type",
    "trend_direction",
    "next_human_action",
    "daily_briefing",
    "compliance_score",
    "automation_status",
    "next_automation_action",
    "next_automation_time",
    "occupancy_mode",
    "last_action_time",
    "last_action_reason",
    "fan_status",
    "contact_status",
    "ai_status",
    "indoor_temp",
    "outdoor_temp",
    "forecast_high",
    "forecast_low",
    "effective_target_temp",
    "heat_threshold",
    "cool_threshold",
}
_DIAGNOSTIC = {"heat_threshold", "cool_threshold", "last_action_time", "last_action_reason", "ai_status"}
_DISABLED_BY_DEFAULT = {"trend_direction"}


def _entry():
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.title = "Living Room"
    return entry


def _entities(platform):
    coord = MagicMock()
    coord.config = {}
    coord.data = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"zone1": coord}}
    added: list = []
    asyncio.run(platform.async_setup_entry(hass, _entry(), added.extend))
    return added


def test_unique_ids_are_unchanged():
    sensors = _entities(sensor_platform)
    assert {s._attr_unique_id for s in sensors} == {f"zone1_{k}" for k in _SENSOR_KEYS}
    (switch,) = _entities(switch_platform)
    assert switch._attr_unique_id == "zone1_automation_enabled"


def test_every_entity_uses_the_shared_base_and_translated_name():
    for ent in [*_entities(sensor_platform), *_entities(switch_platform)]:
        assert isinstance(ent, ClimateAdvisorEntity)
        assert ent._attr_has_entity_name is True
        assert not hasattr(ent, "_attr_name")
        assert ent._attr_unique_id == f"zone1_{ent._attr_translation_key}"


def test_one_service_device_per_zone(monkeypatch):
    # DeviceInfo is a TypedDict in Home Assistant; the unit tier's stub is a mock, so read the fields as a dict.
    monkeypatch.setattr(entity_mod, "DeviceInfo", dict)
    for ent in [*_entities(sensor_platform), *_entities(switch_platform)]:
        info = ent._attr_device_info
        assert info["identifiers"] == {(DOMAIN, "zone1")}
        assert info["name"] == "Living Room"
        assert info["entry_type"] == DeviceEntryType.SERVICE


def test_diagnostic_and_disabled_entities():
    sensors = {s._attr_translation_key: s for s in _entities(sensor_platform)}
    diagnostic = {
        k for k, s in sensors.items() if getattr(s, "_attr_entity_category", None) == EntityCategory.DIAGNOSTIC
    }
    assert diagnostic == _DIAGNOSTIC
    disabled = {k for k, s in sensors.items() if getattr(s, "_attr_entity_registry_enabled_default", True) is False}
    assert disabled == _DISABLED_BY_DEFAULT
    # The main control stays a primary entity, enabled, with no category.
    (switch,) = _entities(switch_platform)
    assert getattr(switch, "_attr_entity_category", None) is None
    assert getattr(switch, "_attr_entity_registry_enabled_default", True) is True


def test_parallel_updates_set_on_both_platforms():
    assert sensor_platform.PARALLEL_UPDATES == 0
    assert switch_platform.PARALLEL_UPDATES == 0


def test_update_failed_message_in_every_language():
    paths = [_PKG / "strings.json", *sorted((_PKG / "translations").glob("*.json"))]
    assert len(paths) == 17
    for path in paths:
        message = json.loads(path.read_text(encoding="utf-8"))["exceptions"]["update_failed"]["message"]
        assert "{error}" in message, path.name
        assert "Climate Advisor" in message, path.name
