"""Issue #1341 (D1/D6): everything Home Assistant renders is a key in strings.json, with the English kept exact.

Occupant story: an English household must see the same entity names, option labels and service text after the
translation keys were added; a household in another language gets HA's own translation of the same keys. These are
the unit-tier guards (stubbed HA); the real-HA tier is ``tests_ha/test_entity_translations_1341.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import pytest  # noqa: E402

from custom_components.climate_advisor import config_flow, i18n, prediction_engines  # noqa: E402
from custom_components.climate_advisor.const import DOMAIN  # noqa: E402

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_STRINGS = json.loads((_COMPONENT / "strings.json").read_text(encoding="utf-8"))

# The English names the 21 sensors and the switch had while ``_attr_name`` was hard-coded (the before).
ENGLISH_NAMES = {
    ("sensor", "day_type"): "Day Type",
    ("sensor", "trend_direction"): "Trend",
    ("sensor", "next_human_action"): "Your Next Action",
    ("sensor", "daily_briefing"): "Daily Briefing",
    ("sensor", "compliance_score"): "Comfort Score",
    ("sensor", "automation_status"): "Status",
    ("sensor", "next_automation_action"): "Next Automation Action",
    ("sensor", "next_automation_time"): "Next Automation Time",
    ("sensor", "occupancy_mode"): "Occupancy Mode",
    ("sensor", "last_action_time"): "Last Action Time",
    ("sensor", "last_action_reason"): "Last Action Reason",
    ("sensor", "fan_status"): "Fan Status",
    ("sensor", "contact_status"): "Contact Sensors",
    ("sensor", "ai_status"): "AI Status",
    ("sensor", "indoor_temp"): "Indoor Temperature",
    ("sensor", "outdoor_temp"): "Outdoor Temperature",
    ("sensor", "forecast_high"): "Forecast High",
    ("sensor", "forecast_low"): "Forecast Low",
    ("sensor", "effective_target_temp"): "Target Temperature",
    ("sensor", "heat_threshold"): "Heat Threshold",
    ("sensor", "cool_threshold"): "Cool Threshold",
    ("switch", "automation_enabled"): "Automation",
}

# The option labels config_flow.py built in Python before they moved to ``selector`` (the before).
ENGLISH_OPTION_LABELS = {
    "fan_mode": {
        "disabled": "Disabled (no fan control)",
        "whole_house_fan": "Whole house fan (dedicated entity)",
        "hvac_fan": "HVAC fan mode",
    },
    "hvac_fan_restrict": {
        "heat": "Heat only (avoid humidity after cooling)",
        "cool": "Cool only",
        "both": "Both (no restriction)",
    },
    "outdoor_temp_source": {
        "weather_service": "Weather service (recommended)",
        "sensor": "Dedicated sensor",
        "input_number": "Input helper (input_number)",
    },
    "indoor_temp_source": {
        "climate_fallback": "Climate entity (recommended)",
        "sensor": "Dedicated sensor",
        "input_number": "Input helper (input_number)",
    },
    "thermostat_companion": {
        "auto": "Auto (use the thermostat's finer sensor when proven)",
        "off": "Off (always use the thermostat's own reading)",
    },
    "temp_unit": {"fahrenheit": "Fahrenheit (°F)", "celsius": "Celsius (°C)"},
    "weekday": {d: d.capitalize() for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
    "ai_reasoning_effort": {"low": "Low", "medium": "Medium", "high": "High"},
}


def _options(key: str) -> dict[str, str]:
    return _STRINGS["selector"][key]["options"]


def _sensor_entities() -> list:
    from custom_components.climate_advisor import sensor, switch

    coordinator = MagicMock()
    coordinator.data = {}
    entry = MagicMock()
    entry.entry_id = "zone1"
    entry.title = "House"
    hass = MagicMock()
    hass.data = {DOMAIN: {"zone1": coordinator}}
    added: list = []
    import asyncio

    asyncio.run(sensor.async_setup_entry(hass, entry, added.extend))
    asyncio.run(switch.async_setup_entry(hass, entry, added.extend))
    return added


class TestEntityNames:
    def test_every_entity_is_named_by_translation_key_not_attr_name(self):
        entities = _sensor_entities()
        assert len(entities) == 22  # 21 sensors + the automation switch
        for ent in entities:
            assert not hasattr(ent, "_attr_name"), type(ent).__name__
            assert ent._attr_has_entity_name is True
            assert ent._attr_translation_key == ent._attr_unique_id.removeprefix("zone1_")

    def test_english_names_are_exactly_the_old_hard_coded_names(self):
        platform_of = {}
        for ent in _sensor_entities():
            platform_of[ent._attr_translation_key] = "switch" if type(ent).__name__.endswith("Switch") else "sensor"
        assert {(platform_of[k], k): v for (_p, k), v in ENGLISH_NAMES.items()} == {
            (platform_of[k], k): _STRINGS["entity"][platform_of[k]][k]["name"] for (_p, k) in ENGLISH_NAMES
        }
        assert set(platform_of) == {k for (_p, k) in ENGLISH_NAMES}

    def test_state_translation_keys_are_hassfest_slugs(self):
        slug = re.compile(r"^[a-z0-9]+([-_][a-z0-9]+)*$")
        for key, entry in _STRINGS["entity"]["sensor"].items():
            for state in entry.get("state", {}):
                assert slug.match(state), (key, state)

    def test_code_valued_states_cover_the_values_the_coordinator_emits(self):
        from custom_components.climate_advisor import const

        day_types = {
            const.DAY_TYPE_HOT,
            const.DAY_TYPE_WARM,
            const.DAY_TYPE_MILD,
            const.DAY_TYPE_COOL,
            const.DAY_TYPE_COLD,
        }
        assert day_types <= set(_STRINGS["entity"]["sensor"]["day_type"]["state"])
        occupancy = {const.OCCUPANCY_HOME, const.OCCUPANCY_AWAY, const.OCCUPANCY_VACATION, const.OCCUPANCY_GUEST}
        assert occupancy == set(_STRINGS["entity"]["sensor"]["occupancy_mode"]["state"])
        assert {"warming", "cooling", "stable"} <= set(_STRINGS["entity"]["sensor"]["trend_direction"]["state"])
        ai = {"disabled", "circuit_open", "budget_exceeded", "inactive", "error", "active"}
        assert ai == set(_STRINGS["entity"]["sensor"]["ai_status"]["state"])


class TestSelectors:
    def test_english_option_labels_are_exactly_the_old_python_labels(self):
        for key, labels in ENGLISH_OPTION_LABELS.items():
            assert _options(key) == labels, key

    def test_option_values_match_the_code(self):
        assert set(_options("fan_mode")) == set(config_flow.FAN_MODE_OPTIONS)
        assert set(_options("hvac_fan_restrict")) == set(config_flow.HVAC_FAN_RESTRICT_OPTIONS)
        assert set(_options("outdoor_temp_source")) == set(config_flow.OUTDOOR_SOURCE_OPTIONS)
        assert set(_options("indoor_temp_source")) == set(config_flow.INDOOR_SOURCE_OPTIONS)
        assert set(_options("thermostat_companion")) == set(config_flow.THERMOSTAT_COMPANION_OPTIONS)
        assert set(_options("temp_unit")) == set(config_flow.TEMP_UNIT_OPTIONS)
        from custom_components.climate_advisor.const import AI_REASONING_OPTIONS

        assert set(_options("ai_reasoning_effort")) == set(AI_REASONING_OPTIONS)

    def test_every_registered_prediction_engine_has_an_option_entry(self):
        engines = {i for i, _ in prediction_engines.options(False)}
        assert engines | {"none"} == set(_options("prediction_engine"))

    def test_engine_dropdown_puts_none_first_as_a_plain_translated_value(self):
        from homeassistant.helpers import selector

        selector.SelectSelectorConfig.reset_mock()
        config_flow._engine_select(True)
        kwargs = selector.SelectSelectorConfig.call_args.kwargs
        assert kwargs["translation_key"] == "prediction_engine"
        assert kwargs["options"][0] == "none"
        selector.SelectSelectorConfig.reset_mock()
        config_flow._engine_select(False)
        assert "none" not in selector.SelectSelectorConfig.call_args.kwargs["options"]

    def test_select_options_are_translated_through_their_key(self):
        from homeassistant.helpers import selector

        selector.SelectSelectorConfig.reset_mock()
        flow = object.__new__(config_flow.ClimateAdvisorOptionsFlow)
        flow.hass = MagicMock()
        flow.config_entry = MagicMock(data={})
        flow._updates, flow._removed, flow._editing_schedule_id = {}, set(), None
        import asyncio

        asyncio.run(flow.async_step_scheduler_edit(None))
        keys = [c.kwargs.get("translation_key") for c in selector.SelectSelectorConfig.call_args_list]
        assert "weekday" in keys


class TestFlowWords:
    def test_texts_are_read_through_homes_translation_cache_per_language(self):
        hass = MagicMock()
        hass.config.language = "de"
        de = {f"component.{DOMAIN}.common.schedule_all_days": "Alle Tage"}
        sel = {f"component.{DOMAIN}.selector.weekday.options.mon": "Mo"}

        async def fake(_hass, lang, category, integrations=None, config_flow=None):
            assert lang == "de" and integrations == {DOMAIN}
            return {"common": de, "selector": sel}[category]

        import asyncio

        with patch.object(config_flow.translation, "async_get_translations", new=AsyncMock(side_effect=fake)):
            texts = asyncio.run(config_flow._async_texts(hass))
        assert texts == {"schedule_all_days": "Alle Tage", "weekday.mon": "Mo"}

    def test_unreadable_catalog_never_breaks_a_form(self):
        hass = MagicMock()
        import asyncio

        with patch.object(config_flow.translation, "async_get_translations", new=AsyncMock(side_effect=RuntimeError)):
            assert asyncio.run(config_flow._async_texts(hass)) == {}
        assert config_flow._text({}, "schedule_all_days") == "schedule_all_days"

    def test_english_summary_is_exactly_the_old_format(self):
        texts = {k: v for k, v in _STRINGS["common"].items()}
        texts.update({f"weekday.{k}": v for k, v in _STRINGS["selector"]["weekday"]["options"].items()})
        fmt = config_flow.ClimateAdvisorOptionsFlow._format_schedule_summary
        base = {"name": "Evening", "start": "16:00:00", "end": "21:00:00"}
        assert (
            fmt({**base, "days": ["mon", "fri"], "cost_tag": "high"}, texts)
            == "Evening — Mon/Fri 16:00:00-21:00:00 (High)"
        )
        assert fmt({**base, "days": ["all"], "cost_tag": "low"}, texts) == "Evening — All days 16:00:00-21:00:00 (Low)"
        assert fmt({"days": ["sun"], "start": "1", "end": "2"}, texts) == "(unnamed) — Sun 1-2 (Low)"

    def test_add_row_label_english(self):
        assert _STRINGS["common"]["schedule_add_new"] == "+ Add a new schedule"

    def test_status_words_english(self):
        assert _STRINGS["common"]["key_status_not_configured"] == "Not configured"
        assert _STRINGS["common"]["token_status_configured"] == "configured"
        assert _STRINGS["common"]["token_status_not_configured"] == "not configured"


class TestExceptionAndServices:
    def test_unknown_zone_is_a_translated_exception(self):
        from custom_components.climate_advisor import _resolve_zone_coordinator

        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        call = MagicMock()
        call.data = {"entry_id": "nope"}
        with pytest.raises(Exception) as err:  # noqa: PT011 - the stub ServiceValidationError
            _resolve_zone_coordinator(hass, call)
        assert err.value.translation_domain == DOMAIN
        assert err.value.translation_key == "unknown_zone"
        assert err.value.translation_placeholders == {"entry_id": "nope"}
        message = _STRINGS["exceptions"]["unknown_zone"]["message"]
        assert message.format(entry_id="nope").startswith("Unknown or unloaded Climate Advisor zone entry_id 'nope'.")

    def test_services_yaml_holds_no_text(self):
        text = (_COMPONENT / "services.yaml").read_text(encoding="utf-8")
        assert not re.search(r"^\s+(name|description):", text, re.M)

    def test_english_service_text_is_what_the_yaml_had(self):
        svc = _STRINGS["services"]
        assert svc["force_reclassify"]["name"] == "Force reclassify"
        assert svc["reset_learning_data"]["fields"]["scope"]["name"] == "Scope"
        assert svc["respond_to_suggestion"]["fields"]["entry_id"]["name"] == "Zone"
        assert svc["reset_learning_data"]["fields"]["entry_id"]["description"].endswith("this service is destructive.")


class TestValidateTool:
    def test_hassfest_style_validation_passes(self):
        sys.path.insert(0, str(_COMPONENT.parent.parent / "tools"))
        try:
            import validate  # noqa: PLC0415

            result = validate.check_ha_translations(str(_COMPONENT))
        finally:
            sys.path.pop(0)
        assert result.errors == []

    def test_it_catches_a_hard_coded_name_and_an_icu_value(self, tmp_path):
        sys.path.insert(0, str(_COMPONENT.parent.parent / "tools"))
        try:
            import validate  # noqa: PLC0415

            comp = tmp_path / "c"
            comp.mkdir()
            bad = json.loads((_COMPONENT / "strings.json").read_text(encoding="utf-8"))
            bad["common"]["x"] = "{n, plural, one {a} other {b}}"
            (comp / "strings.json").write_text(json.dumps(bad), encoding="utf-8")
            (comp / "sensor.py").write_text("class A:\n    def __init__(self):\n        self._attr_name = 'x'\n")
            result = validate.check_ha_translations(str(comp))
        finally:
            sys.path.pop(0)
        joined = "\n".join(result.errors)
        assert "placeholder" in joined and "_attr_name" in joined


# ---------------------------------------------------------------------------
# Prose sensors: render a Msg in HA's language, stable code/params for automations
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogs():
    i18n.reset_catalogs()
    i18n.load_catalogs()
    yield
    i18n.reset_catalogs()


def _sensor(cls, data, language=None):
    coord = MagicMock()
    coord.data = data
    entry = MagicMock()
    entry.entry_id = "zone1"
    sensor = cls(coord, entry)
    if language:
        sensor.hass = MagicMock()
        sensor.hass.config.language = language
    return sensor


class TestProseSensors:
    def test_plain_prose_passes_through_with_empty_code(self, catalogs):
        from custom_components.climate_advisor.const import ATTR_AUTOMATION_STATUS
        from custom_components.climate_advisor.sensor import ClimateAdvisorStatusSensor

        s = _sensor(ClimateAdvisorStatusSensor, {ATTR_AUTOMATION_STATUS: "grace period (manual)"}, "de")
        assert s.native_value == "grace period (manual)"
        assert s.extra_state_attributes == {"code": None, "params": {}}

    def test_msg_renders_in_english_and_german_with_stable_code(self, catalogs):
        from custom_components.climate_advisor.const import ATTR_AUTOMATION_STATUS
        from custom_components.climate_advisor.sensor import ClimateAdvisorStatusSensor

        msg = i18n.Msg("common.mode.cool")
        en = _sensor(ClimateAdvisorStatusSensor, {ATTR_AUTOMATION_STATUS: msg})
        de = _sensor(ClimateAdvisorStatusSensor, {ATTR_AUTOMATION_STATUS: msg}, "de")
        assert en.native_value == "Cool"
        assert de.native_value == "Kühlen"
        for s in (en, de):  # the automation-facing form never depends on the language
            assert s.extra_state_attributes == {"code": "common.mode.cool", "params": {}}

    def test_last_action_reason_renders_state_and_full_reason_and_caps(self, catalogs):
        from custom_components.climate_advisor.const import ATTR_LAST_ACTION_REASON
        from custom_components.climate_advisor.sensor import ClimateAdvisorLastActionReasonSensor

        s = _sensor(ClimateAdvisorLastActionReasonSensor, {ATTR_LAST_ACTION_REASON: i18n.Msg("common.mode.heat")}, "de")
        assert s.native_value == "Heizen"
        assert s.extra_state_attributes == {"full_reason": "Heizen", "code": "common.mode.heat", "params": {}}
        long = _sensor(ClimateAdvisorLastActionReasonSensor, {ATTR_LAST_ACTION_REASON: "x" * 300})
        assert len(long.native_value) <= 255
        assert long.extra_state_attributes["full_reason"] == "x" * 300

    def test_briefing_state_and_attributes_render(self, catalogs):
        from custom_components.climate_advisor.const import ATTR_BRIEFING, ATTR_BRIEFING_SHORT
        from custom_components.climate_advisor.sensor import ClimateAdvisorBriefingSensor

        data = {ATTR_BRIEFING_SHORT: i18n.Msg("common.mode.off"), ATTR_BRIEFING: "Full text"}
        s = _sensor(ClimateAdvisorBriefingSensor, data, "de")
        assert s.native_value == "Aus"
        attrs = s.extra_state_attributes
        assert attrs["tldr"] == "Aus" and attrs["full_briefing"] == "Full text" and attrs["code"] == "common.mode.off"

    def test_contact_icon_follows_the_sensors_not_the_text(self):
        from custom_components.climate_advisor.const import ATTR_CONTACT_STATUS
        from custom_components.climate_advisor.sensor import ClimateAdvisorContactStatusSensor

        s = _sensor(ClimateAdvisorContactStatusSensor, {ATTR_CONTACT_STATUS: "whatever text"})
        s.coordinator._compute_contact_details.return_value = []
        assert s.icon == "mdi:door-closed"
        s.coordinator._compute_contact_details.return_value = [{"open": False, "unreadable": False}]
        assert s.icon == "mdi:door-closed"
        s.coordinator._compute_contact_details.return_value = [{"open": True, "unreadable": False}]
        assert s.icon == "mdi:door-open"
        s.coordinator._compute_contact_details.return_value = [{"open": False, "unreadable": True}]
        assert s.icon == "mdi:door-open"


# ---------------------------------------------------------------------------
# Tools look entities up by unique id, never by an assumed entity id
# ---------------------------------------------------------------------------


def _tool(name: str):
    import importlib.util

    tools = _COMPONENT.parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        spec = importlib.util.spec_from_file_location(name, tools / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


class TestToolsLookUpByUniqueId:
    REGISTRY = {
        "data": {
            "entities": [
                {
                    "entity_id": "sensor.haus_zustand",
                    "platform": "climate_advisor",
                    "unique_id": "01AAA_automation_status",
                    "config_entry_id": "01AAA",
                },
                {
                    "entity_id": "sensor.upstairs_status",
                    "platform": "climate_advisor",
                    "unique_id": "01BBB_automation_status",
                    "config_entry_id": "01BBB",
                },
                {"entity_id": "sensor.other_status", "platform": "other", "unique_id": "x_automation_status"},
                {
                    "entity_id": "sensor.haus_tagestyp",
                    "platform": "climate_advisor",
                    "unique_id": "01AAA_day_type",
                    "config_entry_id": "01AAA",
                },
            ]
        }
    }

    def test_ha_logs_finds_the_entity_whatever_its_id_is_called(self):
        ha_logs = _tool("ha_logs")
        assert ha_logs.find_ca_entity_ids(self.REGISTRY, "day_type") == ["sensor.haus_tagestyp"]
        assert ha_logs.find_ca_entity_ids(self.REGISTRY, "automation_status") == [
            "sensor.haus_zustand",
            "sensor.upstairs_status",
        ]
        assert ha_logs.find_ca_entity_ids(self.REGISTRY, "automation_status", "01BBB") == ["sensor.upstairs_status"]
        assert ha_logs.find_ca_entity_ids(self.REGISTRY, "nope") == []

    def test_ha_logs_passes_literal_ids_through_without_ssh(self):
        ha_logs = _tool("ha_logs")
        assert ha_logs.resolve_entity_ids({}, ["sensor.house_status"]) == ["sensor.house_status"]

    def test_thermal_health_template_does_not_assume_an_entity_id(self):
        thermal_health = _tool("thermal_health")
        any_zone = thermal_health._zone_sensor_template(None)
        assert "config_entry_id" not in any_zone and "thermal_learning_health" in any_zone
        assert "climate_advisor_comfort_score" not in Path(thermal_health.__file__).read_text(encoding="utf-8")
