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
    langs = json.loads((_PKG / "frontend" / "i18n" / "index.json").read_text(encoding="utf-8"))["languages"]
    assert len(paths) == 1 + len(langs)  # strings.json plus one translations file per language
    for path in paths:
        message = json.loads(path.read_text(encoding="utf-8"))["exceptions"]["update_failed"]["message"]
        assert "{error}" in message, path.name
        assert "Climate Advisor" in message, path.name


# ── Batch 2 (#1637): icons.json, ENUM device classes, services in async_setup, service errors ──

_ENUM_SENSORS = {"day_type", "trend_direction", "occupancy_mode", "ai_status"}  # ai_status: Issue #1648


def _strings() -> dict:
    return json.loads((_PKG / "strings.json").read_text(encoding="utf-8"))


def test_icons_json_covers_every_entity_and_service():
    """icon-translations: every entity and service has its icon in icons.json; none is hard-coded."""
    icons = json.loads((_PKG / "icons.json").read_text(encoding="utf-8"))
    sensor_keys = {s._attr_translation_key for s in _entities(sensor_platform)}
    assert set(icons["entity"]["sensor"]) == sensor_keys
    assert set(icons["entity"]["switch"]) == {"automation_enabled"}
    for platform in ("sensor", "switch"):
        for key, spec in icons["entity"][platform].items():
            assert spec["default"].startswith("mdi:"), key
    from custom_components.climate_advisor.services import ZONE_SCOPED_SERVICES

    assert set(icons["services"]) == set(ZONE_SCOPED_SERVICES)
    # Trend's per-state icons replaced its code ``icon`` property.
    assert icons["entity"]["sensor"]["trend_direction"]["state"] == {
        "warming": "mdi:trending-up",
        "cooling": "mdi:trending-down",
    }
    for ent in [*_entities(sensor_platform), *_entities(switch_platform)]:
        assert getattr(ent, "_attr_icon", None) is None, ent._attr_translation_key
    # The one code icon left depends on per-sensor detail, not on the state (documented exception).
    with_property = {
        cls.__name__
        for cls in {type(e) for e in _entities(sensor_platform)}
        for klass in cls.__mro__
        if "icon" in vars(klass) and klass.__module__.endswith("climate_advisor.sensor")
    }
    assert with_property == {"ClimateAdvisorContactStatusSensor"}


def test_enum_sensors_list_exactly_their_translated_states():
    """entity-device-class: ENUM sensors' options equal the state names strings.json translates."""
    from homeassistant.components.sensor import SensorDeviceClass

    states = _strings()["entity"]["sensor"]
    for s in _entities(sensor_platform):
        key = s._attr_translation_key
        if key in _ENUM_SENSORS:
            assert s._attr_device_class == SensorDeviceClass.ENUM, key
            assert set(s._attr_options) == set(states[key]["state"]), key
        else:
            assert getattr(s, "_attr_device_class", None) != SensorDeviceClass.ENUM, key


def test_enum_sensor_reports_unknown_for_a_value_off_its_list():
    """A value outside ``options`` would make Home Assistant reject the state; it reads as None instead."""
    sensors = {s._attr_translation_key: s for s in _entities(sensor_platform)}
    day = sensors["day_type"]
    day.coordinator.data = {"day_type": "warm"}
    assert day.native_value == "warm"
    day.coordinator.data = {"day_type": "scorching"}
    assert day.native_value is None


def test_service_errors_in_every_language():
    paths = [_PKG / "strings.json", *sorted((_PKG / "translations").glob("*.json"))]
    for path in paths:
        exc = json.loads(path.read_text(encoding="utf-8"))["exceptions"]
        assert "{suggestion_key}" in exc["unknown_suggestion"]["message"], path.name
        assert "{valid}" in exc["unknown_suggestion"]["message"], path.name
        assert "{service}" in exc["service_failed"]["message"], path.name
        assert "{error}" in exc["service_failed"]["message"], path.name


def test_services_registered_once_in_async_setup_before_any_zone():
    """action-setup: async_setup alone registers all five services, with no zone loaded."""
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.climate_advisor import CONFIG_SCHEMA, async_setup
    from custom_components.climate_advisor.services import ZONE_SCOPED_SERVICES
    from tools.sim_harness.fake_hass import FakeHass

    assert CONFIG_SCHEMA is not None
    hass = FakeHass()
    assert asyncio.run(async_setup(hass, {})) is True
    for name in ZONE_SCOPED_SERVICES:
        assert hass.services.has_service(DOMAIN, name), name
    # No zone loaded: a call fails with the translated unknown_zone error, not "service not found".
    try:
        asyncio.run(hass.services.async_call(DOMAIN, "force_reclassify", {"entry_id": "gone"}))
    except ServiceValidationError as err:
        assert err.translation_key == "unknown_zone"
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("force_reclassify for an unloaded zone did not raise")


class TestServiceErrors:
    """action-exceptions: bad input -> ServiceValidationError; a failure -> HomeAssistantError; never silent."""

    def _zone(self):
        from tools.sim_harness._loop import run_coro
        from tools.sim_harness.build_coordinator import build_headless_multi_zone

        zones, fake_hass, _scheduler = build_headless_multi_zone(zone_count=1)
        return zones["zone_0"], fake_hass, run_coro

    def test_unknown_suggestion_key_is_rejected_and_nothing_is_recorded(self):
        import pytest
        from homeassistant.exceptions import ServiceValidationError

        zone, hass, run = self._zone()
        zone["coordinator"].learning._state.dismissed_suggestions = []
        with pytest.raises(ServiceValidationError) as caught:
            run(
                hass.services.async_call(
                    DOMAIN,
                    "respond_to_suggestion",
                    {"entry_id": zone["entry"].entry_id, "action": "dismiss", "suggestion_key": "made_up"},
                )
            )
        assert caught.value.translation_key == "unknown_suggestion"
        assert caught.value.translation_placeholders["suggestion_key"] == "made_up"
        assert zone["coordinator"].learning._state.dismissed_suggestions == []

    def test_every_real_suggestion_key_is_accepted(self):
        from custom_components.climate_advisor.learning import SUGGESTION_KEYS

        zone, hass, run = self._zone()
        for key in sorted(SUGGESTION_KEYS):
            run(
                hass.services.async_call(
                    DOMAIN,
                    "respond_to_suggestion",
                    {"entry_id": zone["entry"].entry_id, "action": "dismiss", "suggestion_key": key},
                )
            )
        assert set(zone["coordinator"].learning._state.dismissed_suggestions) == set(SUGGESTION_KEYS)

    def test_a_failing_reset_raises_a_translated_error_with_the_cause(self):
        import pytest
        from homeassistant.exceptions import HomeAssistantError

        zone, hass, run = self._zone()

        def broken(_scope):
            raise OSError("disk full")

        zone["coordinator"].learning.reset = broken
        with pytest.raises(HomeAssistantError) as caught:
            run(
                hass.services.async_call(
                    DOMAIN, "reset_learning_data", {"entry_id": zone["entry"].entry_id, "scope": "all"}
                )
            )
        assert caught.value.translation_key == "service_failed"
        assert caught.value.translation_placeholders == {
            "service": "reset_learning_data",
            "error": "OSError: disk full",
        }
        assert isinstance(caught.value.__cause__, OSError)


def test_service_accept_persists_settings_like_the_dashboard():
    """Issue #1652: the service saves an accepted change into the zone's entry through the same helper as the
    dashboard (before, only the dashboard path had this step). Today no suggestion maps to a stored setting
    (#1652 tracks that), so the test drives the helper with a real one.
    """
    from tools.sim_harness._loop import run_coro
    from tools.sim_harness.build_coordinator import build_headless_multi_zone

    zones, hass, _scheduler = build_headless_multi_zone(zone_count=1)
    zone = zones["zone_0"]
    entry = zone["entry"]
    zone["coordinator"].learning.accept_suggestion = lambda key: {
        "comfort_heat": 69.0,
        "request_setpoint_analysis": True,
    }
    run_coro(
        hass.services.async_call(
            DOMAIN,
            "respond_to_suggestion",
            {"entry_id": entry.entry_id, "action": "accept", "suggestion_key": "frequent_door_pauses"},
        )
    )
    stored = hass.config_entries.async_get_entry(entry.entry_id).data
    assert stored["comfort_heat"] == 69.0
    assert "request_setpoint_analysis" not in stored  # not a stored setting: runtime only, as on the dashboard
