"""Issue #1336: AI settings saved in one zone apply to every zone, and a too-small provider limit only warns.

Occupant view: the owner has two zones, opens AI settings in the first one, picks Home Assistant AI Task with no
Claude key, and saves. Real Home Assistant's options flow then shows the second zone holding the same AI settings.
When the provider's output limit is known to be too small, a warning step names the setting after the save has
already happened; submitting it returns to the options menu.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.house.builder import CA_DOMAIN, SIM_DOMAIN

_AI_LIMITS = "custom_components.climate_advisor.ai_limits"

_SUBMIT = {
    "ai_enabled": True,
    "ai_provider": "ha_ai_task",
    "ai_task_entity": "ai_task.anthropic",
    "ai_api_key": "",
    "ai_temperature": 0.3,
    "ai_max_tokens": 8192,
    "ai_monthly_budget": 10,
    "ai_auto_requests_per_day": 5,
    "ai_manual_requests_per_day": 20,
    "ai_investigator_enabled": True,
    "ai_investigator_requests_per_day": 3,
}


async def _add_zone(hass, house, name: str) -> list[MockConfigEntry]:
    """A second simulated thermostat plus a Climate Advisor zone on it."""
    before = {s.entity_id for s in hass.states.async_all("climate")}
    sim = MockConfigEntry(
        domain=SIM_DOMAIN, title=f"{name} Thermostat", data={**house.sim_entry.data, "name": f"{name} Thermostat"}
    )
    sim.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sim.entry_id)
    await hass.async_block_till_done()
    climate = next(s.entity_id for s in hass.states.async_all("climate") if s.entity_id not in before)
    zone = MockConfigEntry(
        domain=CA_DOMAIN,
        title=name,
        version=house.ca_entry.version,
        minor_version=house.ca_entry.minor_version,
        data={**house.ca_entry.data, "climate_entity": climate},
        options=dict(house.ca_entry.options),
    )
    zone.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zone.entry_id)
    await hass.async_block_till_done()
    return [sim, zone]


async def _open_ai_settings(hass, entry_id: str):
    result = await hass.config_entries.options.async_init(entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "ai_settings"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "ai_settings"
    return result


async def test_saving_ai_settings_in_one_zone_applies_them_to_every_zone(make_house, monkeypatch) -> None:
    import custom_components.climate_advisor as pkg

    reader = AsyncMock(return_value={"readable": True, "checks": [], "ok": True})
    stub = types.ModuleType(_AI_LIMITS)
    stub.async_read_limits = reader
    monkeypatch.setitem(sys.modules, _AI_LIMITS, stub)
    monkeypatch.setattr(pkg, "ai_limits", stub, raising=False)

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    added = await _add_zone(hass, house, "Upstairs")
    upstairs = added[1]

    result = await _open_ai_settings(hass, house.ca_entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _SUBMIT)
    assert result["type"] is FlowResultType.MENU  # no key needed for AI Task; limits fine -> back to the menu

    zone1 = hass.config_entries.async_get_entry(house.ca_entry.entry_id)
    zone2 = hass.config_entries.async_get_entry(upstairs.entry_id)
    assert zone1.data["ai_provider"] == "ha_ai_task"
    assert zone1.data["ai_task_entity"] == "ai_task.anthropic"
    for key in _SUBMIT:
        if key == "ai_api_key":
            continue
        assert zone2.data.get(key) == zone1.data.get(key), key
    assert zone2.data["climate_entity"] != zone1.data["climate_entity"]  # each zone keeps its own thermostat
    reader.assert_awaited_once()

    # Small provider limits: the save still happened, then the warning names the setting; submit -> menu.
    reader.return_value = {
        "provider": "ha_ai_task",
        "entity_id": "ai_task.anthropic",
        "integration": "anthropic",
        "readable": True,
        "checks": [{"kind": "output", "setting": "max_tokens", "value": 3000, "required": 8192, "ok": False}],
        "ok": False,
    }
    result = await _open_ai_settings(hass, upstairs.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_SUBMIT, "ai_task_entity": "ai_task.other"}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "ai_limits_warning"
    checks = result["description_placeholders"]["checks"]
    assert "anthropic" in checks and "max_tokens" in checks and "3000" in checks and "8192" in checks
    # Saved before the warning, in both zones.
    assert hass.config_entries.async_get_entry(upstairs.entry_id).data["ai_task_entity"] == "ai_task.other"
    assert hass.config_entries.async_get_entry(house.ca_entry.entry_id).data["ai_task_entity"] == "ai_task.other"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU


async def test_builtin_claude_without_a_key_is_refused_by_the_real_flow(make_house, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, _AI_LIMITS, None)  # no reader: the check is skipped, never an error
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    result = await _open_ai_settings(hass, house.ca_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_SUBMIT, "ai_provider": "builtin_claude"}
    )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"ai_api_key": "ai_key_required"}
