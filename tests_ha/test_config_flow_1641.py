"""Issue #1641: the setup wizard and the zone settings, driven through Home Assistant's real flow manager.

Occupant view: a new owner adding Climate Advisor meets the setup wizard first. When they type a notify service
in the wrong shape, pick a fan another zone already controls, give the zone a name that is too long, or pick a
heating and cooling pair that would make the system take turns, the wizard says so and they fix it without
starting over; the zone is then created. Adding a second zone on a thermostat that already has one is refused.
In an existing zone's settings, every section opens and saves, and each section's error (a failed AI key test,
a bad GitHub repository, thresholds out of order, preheat times out of order, a schedule with no days) is
shown, fixed and saved.

Real Home Assistant: ``hass.config_entries.flow`` / ``hass.config_entries.options`` (FlowManager, schema
validation, selectors) run the flow; only the zone's own setup is skipped in the setup-wizard tests (it is not
what they test). The settings tests run against a real, loaded zone (the house fixture).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.house.builder import CA_DOMAIN

_PKG = "custom_components.climate_advisor"
_AI_LIMITS = f"{_PKG}.ai_limits"

_SETPOINTS_OK = {
    "comfort_heat": 68,
    "comfort_cool": 74,
    "setback_heat": 63,
    "setback_cool": 79,
    "sleep_heat": 64,
    "sleep_cool": 72,
}


@pytest.fixture
async def skip_zone_setup(hass):
    """The setup-wizard tests test the wizard; the created zone's own setup (and unload) is not run."""
    with (
        patch(f"{_PKG}.async_setup_entry", return_value=True) as setup,
        patch(f"{_PKG}.async_unload_entry", return_value=True),
    ):
        yield setup
        for entry in hass.config_entries.async_entries(CA_DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)


def _other_zone(hass, *, climate: str, fan: str | None = None, title: str = "Upstairs") -> MockConfigEntry:
    data: dict[str, Any] = {
        "climate_entity": climate,
        "weather_entity": "weather.home",
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        "fan_mode": "disabled",
    }
    if fan:
        data.update({"fan_mode": "whole_house_fan", "fan_entity": fan})
    entry = MockConfigEntry(domain=CA_DOMAIN, title=title, unique_id=climate, data=data, version=21)
    entry.add_to_hass(hass)
    return entry


def _form(result, step_id: str) -> None:
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == step_id, result


async def test_setup_wizard_recovers_from_every_error_and_creates_the_zone(hass, skip_zone_setup) -> None:
    _other_zone(hass, climate="climate.upstairs", fan="fan.attic")
    flow = hass.config_entries.flow

    result = await flow.async_init(CA_DOMAIN, context={"source": SOURCE_USER})
    _form(result, "user")
    core = {"weather_entity": "weather.home", "climate_entity": "climate.hall", "notify_service": "Phone Please"}
    result = await flow.async_configure(result["flow_id"], core)
    _form(result, "user")
    assert result["errors"] == {"notify_service": "invalid_notify_service"}
    result = await flow.async_configure(result["flow_id"], {**core, "notify_service": "notify.mobile_app_phone"})
    _form(result, "unit")

    result = await flow.async_configure(result["flow_id"], {"temp_unit": "fahrenheit"})
    _form(result, "setpoints")
    narrow = {**_SETPOINTS_OK, "comfort_heat": 73, "comfort_cool": 75}
    result = await flow.async_configure(result["flow_id"], narrow)
    _form(result, "setpoints")  # warned once: the heat and AC would take turns
    assert result["errors"] and all(v.endswith("_warning") for v in result["errors"].values()), result["errors"]
    result = await flow.async_configure(result["flow_id"], _SETPOINTS_OK)  # fixed instead of resubmitted
    _form(result, "temperature_sources")

    result = await flow.async_configure(
        result["flow_id"], {"outdoor_temp_source": "sensor", "indoor_temp_source": "sensor"}
    )
    _form(result, "outdoor_temp_entity")
    result = await flow.async_configure(result["flow_id"], {"outdoor_temp_entity": "sensor.porch_temperature"})
    _form(result, "indoor_temp_entity")
    result = await flow.async_configure(result["flow_id"], {"indoor_temp_entity": "sensor.hall_temperature"})
    _form(result, "sensors")

    fan = {"fan_mode": "whole_house_fan", "fan_entity": "fan.attic"}
    result = await flow.async_configure(result["flow_id"], fan)
    _form(result, "sensors")
    assert result["errors"] == {"fan_entity": "whf_entity_shared_zone"}  # Upstairs already runs that fan
    fan = {
        "fan_mode": "whole_house_fan",
        "fan_entity": "fan.hall_whf",
        "door_window_sensors": ["binary_sensor.hall_window"],
    }
    result = await flow.async_configure(result["flow_id"], fan)
    _form(result, "sensors")  # warned once: Climate Advisor cannot see this fan's real state
    assert result["errors"] == {"fan_entity": "whf_command_only_warning"}
    result = await flow.async_configure(result["flow_id"], fan)  # the identical resubmit keeps it
    _form(result, "occupancy")

    result = await flow.async_configure(result["flow_id"], {})
    _form(result, "schedule")
    schedule = {
        "wake_time": "06:30:00",
        "sleep_time": "22:30:00",
        "briefing_time": "06:00:00",
        "briefing_notifications_enabled": False,
    }
    result = await flow.async_configure(result["flow_id"], {**schedule, "zone_name": "D" * 51})
    _form(result, "schedule")
    assert result["errors"] == {"zone_name": "zone_name_too_long"}
    result = await flow.async_configure(result["flow_id"], {**schedule, "zone_name": "Downstairs"})

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Downstairs"
    entry = result["result"]
    assert entry.unique_id == "climate.hall"
    data = entry.data
    assert data["climate_entity"] == "climate.hall" and data["weather_entity"] == "weather.home"
    assert data["notify_service"] == "notify.mobile_app_phone"
    assert data["comfort_heat"] == 68 and data["comfort_cool"] == 74  # the fixed pair, not the warned one
    assert data["outdoor_temp_entity"] == "sensor.porch_temperature"
    assert data["indoor_temp_entity"] == "sensor.hall_temperature"
    assert data["fan_entity"] == "fan.hall_whf"
    assert "zone_name" not in data  # the name is the entry title only
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert any(call.args[1] is entry for call in skip_zone_setup.await_args_list)


async def test_setup_wizard_celsius_setpoints_are_stored_in_fahrenheit(hass, skip_zone_setup) -> None:
    flow = hass.config_entries.flow
    result = await flow.async_init(CA_DOMAIN, context={"source": SOURCE_USER})
    result = await flow.async_configure(
        result["flow_id"],
        {"weather_entity": "weather.home", "climate_entity": "climate.hall", "notify_service": "notify.notify"},
    )
    result = await flow.async_configure(result["flow_id"], {"temp_unit": "celsius"})
    _form(result, "setpoints")
    celsius = {
        "comfort_heat": 20,
        "comfort_cool": 23.5,
        "setback_heat": 17,
        "setback_cool": 26,
        "sleep_heat": 18,
        "sleep_cool": 22,
    }
    result = await flow.async_configure(result["flow_id"], celsius)
    _form(result, "temperature_sources")
    result = await flow.async_configure(result["flow_id"], {})  # defaults: weather service + thermostat reading
    _form(result, "sensors")
    result = await flow.async_configure(result["flow_id"], {})
    _form(result, "occupancy")
    result = await flow.async_configure(result["flow_id"], {})
    result = await flow.async_configure(
        result["flow_id"],
        {"zone_name": "", "wake_time": "06:30:00", "sleep_time": "22:30:00", "briefing_time": "06:00:00"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Climate Advisor"  # a cleared name falls back to the default title
    data = result["result"].data
    assert data["temp_unit"] == "celsius"
    assert data["comfort_heat"] == pytest.approx(68.0)
    assert data["comfort_cool"] == pytest.approx(74.3)
    assert data["outdoor_temp_source"] == "weather_service"
    assert data["briefing_notifications_enabled"] is True  # the first zone sends the briefing


async def test_second_zone_on_the_same_thermostat_is_refused(hass, skip_zone_setup) -> None:
    _other_zone(hass, climate="climate.hall", title="Hall")
    result = await hass.config_entries.flow.async_init(CA_DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"weather_entity": "weather.home", "climate_entity": "climate.hall", "notify_service": "notify.notify"},
    )
    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(CA_DOMAIN)) == 1
    skip_zone_setup.assert_not_called()


# ------------------------------------------------------------------ the zone's settings (options flow)


def _shown_values(result) -> dict[str, Any]:
    """What submitting the form untouched sends: every default, plus every suggested value."""
    values: dict[str, Any] = {}
    for key in result["data_schema"].schema:
        if key.default is not vol.UNDEFINED:
            values[str(key)] = key.default()
        elif (key.description or {}).get("suggested_value") is not None:
            values[str(key)] = key.description["suggested_value"]
    return values


async def _open(hass, entry_id: str, step: str):
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.MENU, result
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


@pytest.fixture
def no_ai_limit_reader(monkeypatch):
    """No provider limit reader: the post-save limit check is skipped (never an error)."""
    monkeypatch.setitem(sys.modules, _AI_LIMITS, None)


@pytest.mark.house
async def test_every_settings_section_opens_and_saves_its_shown_values(make_house, no_ai_limit_reader) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    from custom_components.climate_advisor.config_flow import OPTIONS_MENU_OPTIONS

    before = dict(house.ca_entry.data)
    for step in OPTIONS_MENU_OPTIONS:
        result = await _open(hass, entry_id, step)
        _form(result, step)
        if step == "scheduler":
            result = await hass.config_entries.options.async_configure(result["flow_id"], {"manage": "add_new"})
            _form(result, "scheduler_edit")
            continue  # adding a schedule is its own test below
        values = _shown_values(result)
        result = await hass.config_entries.options.async_configure(result["flow_id"], values)
        if result["type"] is FlowResultType.FORM and result["step_id"] == step:
            # A warn-once step (setpoints, sensors) shows its warnings, then keeps the identical resubmit.
            assert result["errors"] and all(v.endswith("_warning") for v in result["errors"].values()), (step, result)
            result = await hass.config_entries.options.async_configure(result["flow_id"], values)
        assert result["type"] is FlowResultType.MENU, (step, result)

    after = hass.config_entries.async_get_entry(entry_id).data
    for key in ("climate_entity", "weather_entity", "notify_service", "temp_unit", "comfort_heat", "comfort_cool"):
        assert after[key] == before[key], key
    assert house.ca_entry.state is ConfigEntryState.LOADED


@pytest.mark.house
async def test_settings_errors_are_shown_then_fixed(make_house, no_ai_limit_reader) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    options = hass.config_entries.options

    # Core: a notify service in the wrong shape and a too-long zone name, then both fixed.
    result = await _open(hass, entry_id, "core")
    core = {**_shown_values(result), "notify_service": "bad service", "zone_name": "Z" * 51}
    result = await options.async_configure(result["flow_id"], core)
    _form(result, "core")
    assert result["errors"] == {"notify_service": "invalid_notify_service", "zone_name": "zone_name_too_long"}
    result = await options.async_configure(
        result["flow_id"], {**core, "notify_service": "notify.mobile_app_phone", "zone_name": "Main floor"}
    )
    assert result["type"] is FlowResultType.MENU
    assert house.ca_entry.title == "Main floor"
    assert house.ca_entry.data["notify_service"] == "notify.mobile_app_phone"

    # Classification thresholds out of order, then fixed.
    result = await _open(hass, entry_id, "classification_thresholds")
    thresholds = _shown_values(result)
    bad = {**thresholds, "threshold_cool": 60, "threshold_mild": 50}
    result = await options.async_configure(result["flow_id"], bad)
    _form(result, "classification_thresholds")
    assert result["errors"] == {"threshold_cool": "thresholds_must_be_ascending"}
    fixed = {**thresholds, "threshold_cool": 40, "threshold_mild": 50}
    result = await options.async_configure(result["flow_id"], fixed)
    assert result["type"] is FlowResultType.MENU
    assert house.ca_entry.data["threshold_cool"] == 40

    # Advanced: preheat minimum and default above the maximum, then fixed.
    result = await _open(hass, entry_id, "advanced")
    advanced = _shown_values(result)
    bad = {**advanced, "min_preheat_minutes": 50, "default_preheat_minutes": 100, "max_preheat_minutes": 40}
    result = await options.async_configure(result["flow_id"], bad)
    _form(result, "advanced")
    assert result["errors"] == {
        "min_preheat_minutes": "preheat_min_exceeds_max",
        "default_preheat_minutes": "preheat_default_exceeds_max",
    }
    result = await options.async_configure(
        result["flow_id"], {**bad, "min_preheat_minutes": 30, "default_preheat_minutes": 35}
    )
    assert result["type"] is FlowResultType.MENU
    assert house.ca_entry.data["max_preheat_minutes"] == 40

    # A cost-period schedule with no days, then one whose start equals its end, then a good one.
    result = await _open(hass, entry_id, "scheduler")
    result = await options.async_configure(result["flow_id"], {"manage": "add_new"})
    sched = {"name": "Peak", "days": [], "start": "16:00:00", "end": "21:00:00"}
    result = await options.async_configure(result["flow_id"], sched)
    _form(result, "scheduler_edit")
    assert result["errors"] == {"days": "schedule_days_required"}
    result = await options.async_configure(result["flow_id"], {**sched, "days": ["mon"], "end": "16:00:00"})
    _form(result, "scheduler_edit")
    assert result["errors"] == {"end": "schedule_start_end_equal"}
    result = await options.async_configure(result["flow_id"], {**sched, "days": ["mon", "tue"]})
    _form(result, "scheduler")  # back at the list, which now holds the schedule
    assert [s["name"] for s in house.ca_entry.data["schedules"]] == ["Peak"]

    # GitHub: a repository not in owner/repo form, then fixed; a blank token keeps the stored one.
    hass.config_entries.async_update_entry(house.ca_entry, data={**house.ca_entry.data, "github_token": "tok-1"})
    result = await _open(hass, entry_id, "github_settings")
    result = await options.async_configure(result["flow_id"], {"github_token": "", "github_repo": "not-a-repo"})
    _form(result, "github_settings")
    assert result["errors"] == {"github_repo": "github_repo_invalid"}
    result = await options.async_configure(result["flow_id"], {"github_token": "", "github_repo": "me/house"})
    assert result["type"] is FlowResultType.MENU
    assert house.ca_entry.data["github_token"] == "tok-1"
    assert house.ca_entry.data["github_repo"] == "me/house"

    # Temperature sources: a sensor source with no sensor picked, then the sensor.
    result = await _open(hass, entry_id, "temperature_sources")
    result = await options.async_configure(
        result["flow_id"], {"outdoor_temp_source": "sensor", "indoor_temp_source": "climate_fallback"}
    )
    _form(result, "temperature_sources")
    assert result["errors"] == {"outdoor_temp_entity": "entity_required_for_source"}
    result = await options.async_configure(
        result["flow_id"],
        {
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.porch_temperature",
            "indoor_temp_source": "climate_fallback",
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert house.ca_entry.data["outdoor_temp_entity"] == "sensor.porch_temperature"


class _FakeClaudeClient:
    """Stands in for the built-in Claude client: only the key ``good-key`` connects."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._key = config.get("ai_api_key", "")

    async def async_test_connection(self, hass=None) -> tuple[bool, str]:
        if self._key == "explode":
            raise RuntimeError("network down")
        return (self._key == "good-key", "checked")


@pytest.mark.house
async def test_ai_key_that_fails_its_connection_test_is_refused_then_a_good_one_saves(
    make_house, no_ai_limit_reader, monkeypatch
) -> None:
    from custom_components.climate_advisor import claude_api

    monkeypatch.setattr(claude_api, "ClaudeAPIClient", _FakeClaudeClient)
    monkeypatch.setattr(claude_api, "fetch_available_models", AsyncMock(return_value=["claude-sonnet-4-6"]))
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry_id = house.hass, house.ca_entry.entry_id
    options = hass.config_entries.options

    result = await _open(hass, entry_id, "ai_settings")
    # 8192 tokens: enough for the built-in output-length check, so the save goes straight back to the menu.
    ai = {**_shown_values(result), "ai_enabled": True, "ai_provider": "builtin_claude", "ai_max_tokens": 8192}
    result = await options.async_configure(result["flow_id"], {**ai, "ai_api_key": ""})
    _form(result, "ai_settings")
    assert result["errors"] == {"ai_api_key": "ai_key_required"}
    for failing in ("bad-key", "explode"):  # a refused key, and a test that could not run at all
        result = await options.async_configure(result["flow_id"], {**ai, "ai_api_key": failing})
        _form(result, "ai_settings")
        assert result["errors"] == {"base": "ai_connection_failed"}, failing
        assert house.ca_entry.data.get("ai_api_key") != failing  # nothing saved
    result = await options.async_configure(result["flow_id"], {**ai, "ai_api_key": "good-key"})
    assert result["type"] is FlowResultType.MENU, result
    assert house.ca_entry.data["ai_api_key"] == "good-key"
    assert house.ca_entry.data["ai_provider"] == "builtin_claude"


@pytest.mark.house
async def test_ai_limit_warning_step_returns_to_the_menu(make_house, monkeypatch) -> None:
    reader = AsyncMock(
        return_value={
            "integration": "anthropic",
            "checks": [{"kind": "output", "setting": "max_tokens", "value": 1000, "required": 8192, "ok": False}],
            "ok": False,
        }
    )
    stub = types.ModuleType(_AI_LIMITS)
    stub.async_read_limits = reader
    monkeypatch.setitem(sys.modules, _AI_LIMITS, stub)
    import custom_components.climate_advisor as pkg

    monkeypatch.setattr(pkg, "ai_limits", stub, raising=False)
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    result = await _open(hass, house.ca_entry.entry_id, "ai_settings")
    values = {**_shown_values(result), "ai_enabled": True, "ai_provider": "ha_ai_task", "ai_api_key": ""}
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    _form(result, "ai_limits_warning")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
