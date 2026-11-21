"""Entity names, state labels and flow texts come from Home Assistant's own translation system (Issue #1341, D6).

Two things are pinned on a real Home Assistant core:

* an English install shows exactly the names and entity ids it showed before translation keys were added
  (``_attr_name`` is gone; ``translation_key`` + ``has_entity_name`` give the same friendly name), and
* an install set to German reads the German catalog: entity names, state labels, the schedule words used by the
  options flow, and the exception text. The German strings are a small fixture overlaid on the integration's own
  catalog through HA's translation loader (the production catalog ships one file per language; the loader and cache
  paths exercised here are HA's own).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation

from custom_components.climate_advisor import config_flow
from custom_components.climate_advisor.const import DOMAIN

pytestmark = pytest.mark.house

_STRINGS_PATH = Path(__file__).parent.parent / "custom_components" / "climate_advisor" / "strings.json"
_DE_FIXTURE = json.loads((Path(__file__).parent / "data" / "i18n_de_fixture_1341.json").read_text(encoding="utf-8"))

# The unique-id suffix -> (entity id, friendly name) of a fresh English install of a zone titled "House", recorded
# from the code before translation keys replaced ``_attr_name`` (so this is the before, not a re-derivation).
ENGLISH_BEFORE = {
    "ai_status": ("sensor.house_ai_status", "House AI Status"),
    "automation_enabled": ("switch.house_automation", "House Automation"),
    "automation_status": ("sensor.house_status", "House Status"),
    "compliance_score": ("sensor.house_comfort_score", "House Comfort Score"),
    "contact_status": ("sensor.house_contact_sensors", "House Contact Sensors"),
    "cool_threshold": ("sensor.house_cool_threshold", "House Cool Threshold"),
    "daily_briefing": ("sensor.house_daily_briefing", "House Daily Briefing"),
    "day_type": ("sensor.house_day_type", "House Day Type"),
    "effective_target_temp": ("sensor.house_target_temperature", "House Target Temperature"),
    "fan_status": ("sensor.house_fan_status", "House Fan Status"),
    "forecast_high": ("sensor.house_forecast_high", "House Forecast High"),
    "forecast_low": ("sensor.house_forecast_low", "House Forecast Low"),
    "heat_threshold": ("sensor.house_heat_threshold", "House Heat Threshold"),
    "indoor_temp": ("sensor.house_indoor_temperature", "House Indoor Temperature"),
    "last_action_reason": ("sensor.house_last_action_reason", "House Last Action Reason"),
    "last_action_time": ("sensor.house_last_action_time", "House Last Action Time"),
    "next_automation_action": ("sensor.house_next_automation_action", "House Next Automation Action"),
    "next_automation_time": ("sensor.house_next_automation_time", "House Next Automation Time"),
    "next_human_action": ("sensor.house_your_next_action", "House Your Next Action"),
    "occupancy_mode": ("sensor.house_occupancy_mode", "House Occupancy Mode"),
    "outdoor_temp": ("sensor.house_outdoor_temperature", "House Outdoor Temperature"),
    "trend_direction": ("sensor.house_trend", "House Trend"),
}


def _zone_entities(house: Any) -> dict[str, tuple[str, str | None, str | None]]:
    """``{unique-id suffix: (entity_id, friendly_name, registry original_name)}`` of the zone's entities."""
    reg = er.async_get(house.hass)
    out = {}
    for entry in er.async_entries_for_config_entry(reg, house.ca_entry.entry_id):
        state = house.hass.states.get(entry.entity_id)
        suffix = entry.unique_id.removeprefix(f"{house.ca_entry.entry_id}_")
        out[suffix] = (entry.entity_id, state.attributes.get("friendly_name") if state else None, entry.original_name)
    return out


def _overlay_german(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make HA's translation loader return the German fixture on top of the integration's own English catalog."""
    real = translation._load_translations_files_by_language

    def loader(files: dict[str, dict[str, Path]]) -> dict[str, dict[str, Any]]:
        loaded = real(files)
        if "de" in loaded and DOMAIN in loaded["de"]:
            loaded["de"][DOMAIN] = {**loaded["de"][DOMAIN], **_DE_FIXTURE}
        elif "de" in loaded:
            loaded["de"][DOMAIN] = dict(_DE_FIXTURE)
        return loaded

    monkeypatch.setattr(translation, "_load_translations_files_by_language", loader)


async def test_english_install_keeps_its_entity_ids_and_friendly_names(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=78.0)
    await house.advance(minutes=5)

    found = _zone_entities(house)
    # Issue #1637: Trend is disabled by default on a new install, so it has no state (no friendly name) until the
    # user enables it; its entity id is still the one recorded before.
    assert {k: v[:2] for k, v in found.items()} == {**ENGLISH_BEFORE, "trend_direction": ("sensor.house_trend", None)}
    # 21 sensors + the switch, every one named by its translation key (no hard-coded name).
    assert len(found) == 22
    reg = er.async_get(house.hass)
    for entry in er.async_entries_for_config_entry(reg, house.ca_entry.entry_id):
        assert entry.translation_key == entry.unique_id.removeprefix(f"{house.ca_entry.entry_id}_")


async def test_german_install_reads_german_names_states_flow_words_and_errors(make_house, monkeypatch) -> None:
    _overlay_german(monkeypatch)

    async def _set_german(house: Any) -> None:
        house.hass.config.language = "de"  # before Climate Advisor sets up, like an install configured in German

    house = await make_house(units="us", profile="scripted", initial_indoor_f=78.0, before_ca_setup=_set_german)
    await house.advance(minutes=5)
    hass = house.hass
    assert hass.config.language == "de"

    found = _zone_entities(house)
    # Names: German where the catalog has them, per-key English fallback where it does not.
    assert found["day_type"][1] == "House Tagestyp"
    assert found["automation_status"][1] == "House Zustand"
    assert found["automation_enabled"][1] == "House Automatik"
    # (Comfort Score, like Trend before #1637 disabled it by default, is not in the German fixture.)
    assert found["compliance_score"][1] == ENGLISH_BEFORE["compliance_score"][1]
    # German is a native-entity-id language in HA, so a fresh German install's ids come from the German names.
    assert found["day_type"][0] == "sensor.house_tagestyp"

    # State labels use HA's own state translation.
    assert translation.async_translate_state(hass, "hot", "sensor", DOMAIN, "day_type", None) == "Heiß"
    assert translation.async_translate_state(hass, "warm", "sensor", DOMAIN, "day_type", None) == "Warm"

    # The options flow's own words (schedule summary, weekday names, key status).
    texts = await config_flow._async_texts(hass)
    schedule = {"name": "Evening", "days": ["mon", "tue"], "start": "16:00:00", "end": "21:00:00", "cost_tag": "high"}
    assert (
        config_flow.ClimateAdvisorOptionsFlow._format_schedule_summary(schedule, texts)
        == "Evening — Mo/Di 16:00:00-21:00:00 (Hoch)"
    )
    legacy = {**schedule, "days": ["all"], "cost_tag": "low"}
    assert (
        config_flow.ClimateAdvisorOptionsFlow._format_schedule_summary(legacy, texts)
        == "Evening — Alle Tage 16:00:00-21:00:00 (Niedrig)"
    )
    assert config_flow._text(texts, "key_status_not_configured") == "Nicht eingerichtet"
    assert config_flow._text(texts, "token_status_configured") == "configured"  # no German entry: English fallback

    # Exceptions: translated text is cached for the language; HA's str() of the error is always English.
    cached = translation.async_get_cached_translations(hass, "de", "exceptions", DOMAIN)
    assert cached[f"component.{DOMAIN}.exceptions.unknown_zone.message"].startswith("Unbekannte")


@pytest.mark.parametrize(
    ("step", "keys"),
    [
        ("core", {"temp_unit"}),
        ("temperature_sources", {"outdoor_temp_source", "indoor_temp_source", "thermostat_companion"}),
        ("sensors", {"fan_mode", "hvac_fan_restrict"}),
        ("scheduler_edit", {"weekday"}),
        ("ai_settings", {"ai_reasoning_effort", "ai_provider"}),
        ("advanced", {"prediction_engine"}),
    ],
)
async def test_options_flow_selectors_carry_translation_keys_and_serialise(make_house, step, keys) -> None:
    """HA's own flow manager accepts the new selector configs, and the frontend sees ``translation_key`` + plain values.

    ``Selector.serialize`` is what the options-flow websocket sends to the frontend, so a config HA cannot
    serialise would break the form for every household.
    """
    from homeassistant.data_entry_flow import FlowResultType

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    if step == "scheduler_edit":
        # the list step first (its "add" choice is the plain value ``add_new``), then the edit form
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "scheduler"})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"manage": "add_new"})
    else:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == step

    # Selector.serialize() is what HA sends to the frontend for each field.
    serialised = [v.serialize() for v in result["data_schema"].schema.values() if hasattr(v, "serialize")]
    used = {f["selector"]["select"].get("translation_key") for f in serialised if "select" in (f.get("selector") or {})}
    assert keys <= used, (used, keys)
    strings = json.loads(_STRINGS_PATH.read_text(encoding="utf-8"))["selector"]
    for field in serialised:
        select = (field.get("selector") or {}).get("select")
        if not select or select.get("translation_key") not in keys:
            continue
        for option in select["options"]:
            value = option["value"] if isinstance(option, dict) else option
            assert value in strings[select["translation_key"]]["options"], (select["translation_key"], value)


async def test_scheduler_list_with_saved_schedules_and_add_row_is_accepted_by_ha(make_house) -> None:
    """HA rejects a select list that mixes plain values and dicts; the list's rows are all dicts."""
    from homeassistant.data_entry_flow import FlowResultType

    schedule = {
        "id": "a" * 32,
        "name": "Evening",
        "days": ["mon", "tue"],
        "start": "16:00:00",
        "end": "21:00:00",
        "cost_tag": "high",
    }
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0, ca_config={"schedules": [schedule]})
    hass, entry = house.hass, house.ca_entry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "scheduler"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "scheduler"
    options = next(iter(result["data_schema"].schema.values())).serialize()["selector"]["select"]["options"]
    assert [o["value"] for o in options] == ["a" * 32, "add_new"]
    assert options[0]["label"] == "Evening — Mon/Tue 16:00:00-21:00:00 (High)"
    assert options[1]["label"] == "+ Add a new schedule"
