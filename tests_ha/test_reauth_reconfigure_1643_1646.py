"""Issues #1643 and #1646: a rejected credential asks for a new one, and a zone's setup changes without deleting it.

Occupant view (#1643): the owner's Claude API key is revoked. The next AI request is refused by Anthropic, and Home
Assistant shows "re-authentication required" for Climate Advisor instead of the AI features quietly stopping. The
owner types a new key; a key that fails its test is refused with a message, a good one is saved to every zone and
the zone reloads. The same happens for a zone's GitHub token when GitHub refuses a report submission. Saving a new
key in the zone's AI settings instead closes the pending card.

Occupant view (#1646): the owner replaces the thermostat. In the zone's "Reconfigure" they pick the new thermostat
(or another weather service); the zone reloads on it with its learning, history and settings kept (owner: "keep
the data"). A thermostat another zone already runs is refused, in the reconfigure step and in the zone settings.

Real Home Assistant (flow manager, config entries, reload), the real coordinator and the real AI client; only
Anthropic and GitHub are stood in for.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.house.builder import CA_DOMAIN, CLIMATE_ENTITY, SIM_DOMAIN, WEATHER_ENTITY

pytestmark = pytest.mark.house

_AI = {"ai_enabled": True, "ai_provider": "builtin_claude", "ai_api_key": "old-key", "ai_max_tokens": 8192}


class _APIError(Exception):
    """Stands in for ``anthropic.APIError`` (the anthropic package is not installed here)."""


class _Rejected(_APIError):
    """Stands in for ``anthropic.AuthenticationError``, an ``APIError`` (HTTP 401)."""


class _OtherError(Exception):
    """Stands in for the SDK's rate-limit and timeout errors, which a rejected key is not."""


def _reauth_flows(hass) -> list[dict[str, Any]]:
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == SOURCE_REAUTH]


async def _second_zone(hass, house, name: str = "Upstairs") -> MockConfigEntry:
    climate = await _second_thermostat(hass, house, f"{name} Thermostat")
    zone = MockConfigEntry(
        domain=CA_DOMAIN,
        title=name,
        unique_id=climate,
        version=house.ca_entry.version,
        minor_version=house.ca_entry.minor_version,
        data={**house.ca_entry.data, "climate_entity": climate},
        options=dict(house.ca_entry.options),
    )
    zone.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zone.entry_id)
    await hass.async_block_till_done()
    return zone


async def _second_thermostat(hass, house, name: str) -> str:
    before = {s.entity_id for s in hass.states.async_all("climate")}
    sim = MockConfigEntry(domain=SIM_DOMAIN, title=name, data={**house.sim_entry.data, "name": name})
    sim.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sim.entry_id)
    await hass.async_block_till_done()
    return next(s.entity_id for s in hass.states.async_all("climate") if s.entity_id not in before)


@pytest.fixture
def no_ai_limit_reader(monkeypatch):
    monkeypatch.setitem(sys.modules, "custom_components.climate_advisor.ai_limits", None)


@pytest.fixture
def anthropic_rejects(monkeypatch):
    """Anthropic refuses every request with an authentication error; only ``good-key`` passes the key test."""
    from custom_components.climate_advisor import claude_api

    monkeypatch.setattr(claude_api, "AuthenticationError", _Rejected)
    monkeypatch.setattr(claude_api, "APIError", _APIError)
    monkeypatch.setattr(claude_api, "RateLimitError", _OtherError)
    monkeypatch.setattr(claude_api, "APITimeoutError", _OtherError)
    monkeypatch.setattr(claude_api, "AI_RETRY_BASE_DELAY_SECONDS", 0)

    async def _test_connection(self, hass=None):
        return (self._config.get("ai_api_key") == "good-key", "checked")

    monkeypatch.setattr(claude_api.ClaudeAPIClient, "async_test_connection", _test_connection)
    monkeypatch.setattr(claude_api, "fetch_available_models", AsyncMock(return_value=["claude-sonnet-4-6"]))


async def _refused_ai_request(house) -> None:
    client = house.coordinator.claude_client
    assert client is not None and client.on_auth_failed is not None
    calls = []

    async def _single_call(**_kwargs):
        calls.append(1)
        raise _Rejected("401 invalid x-api-key")

    client._client = object()  # a key is configured
    client._single_api_call = _single_call
    response = await client.async_request("system", "hello")
    assert response.success is False and "Authentication failed" in (response.error or "")
    assert len(calls) == 1  # never retried: the same key fails the same way
    await house.hass.async_block_till_done()


async def test_rejected_claude_key_asks_for_a_new_one_and_saves_it_to_every_zone(
    make_house, anthropic_rejects, no_ai_limit_reader
) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0, ca_config=_AI)
    hass = house.hass
    upstairs = await _second_zone(hass, house)

    await _refused_ai_request(house)
    flows = _reauth_flows(hass)
    assert len(flows) == 1, flows
    assert flows[0]["step_id"] == "reauth_ai_key"
    assert flows[0]["context"]["entry_id"] == house.ca_entry.entry_id  # install-wide key: one card, first zone
    await _refused_ai_request(house)  # a second refusal while the card is shown starts nothing new
    assert len(_reauth_flows(hass)) == 1

    flow_id = flows[0]["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {"ai_api_key": "still-bad"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "ai_connection_failed"}
    assert house.ca_entry.data["ai_api_key"] == "old-key"
    result = await hass.config_entries.flow.async_configure(flow_id, {"ai_api_key": "good-key"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert house.ca_entry.data["ai_api_key"] == "good-key"
    assert hass.config_entries.async_get_entry(upstairs.entry_id).data["ai_api_key"] == "good-key"
    assert house.ca_entry.state is ConfigEntryState.LOADED  # reloaded on the new key
    assert house.coordinator.claude_client._config["ai_api_key"] == "good-key"
    assert not _reauth_flows(hass)


async def test_new_key_saved_in_ai_settings_closes_the_pending_card(
    make_house, anthropic_rejects, no_ai_limit_reader
) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0, ca_config=_AI)
    hass = house.hass
    await _refused_ai_request(house)
    assert len(_reauth_flows(hass)) == 1

    options = hass.config_entries.options
    result = await options.async_init(house.ca_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "ai_settings"})
    values = {k: v for k, v in _AI.items()} | {"ai_api_key": "good-key", "ai_temperature": 0.3}
    result = await options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.MENU, result
    await hass.async_block_till_done()
    assert house.ca_entry.data["ai_api_key"] == "good-key"
    assert not _reauth_flows(hass)


async def test_switching_to_an_ai_task_provider_closes_the_pending_card(
    make_house, anthropic_rejects, no_ai_limit_reader
) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0, ca_config=_AI)
    hass = house.hass
    await _refused_ai_request(house)
    assert len(_reauth_flows(hass)) == 1

    options = hass.config_entries.options
    result = await options.async_init(house.ca_entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "ai_settings"})
    values = {**_AI, "ai_provider": "ha_ai_task", "ai_api_key": "", "ai_temperature": 0.3}
    result = await options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.MENU, result
    await hass.async_block_till_done()
    assert not _reauth_flows(hass)


async def test_rejected_github_token_asks_for_a_new_one(make_house, aioclient_mock) -> None:
    house = await make_house(
        units="us",
        profile="scripted",
        initial_indoor_f=72.0,
        ca_config={"github_token": "old-token", "github_repo": "me/house"},
    )
    hass, entry_id = house.hass, house.ca_entry.entry_id
    from custom_components.climate_advisor.api import ClimateAdvisorSubmitGithubIssueView

    aioclient_mock.post("https://api.github.com/repos/me/house/issues", status=401)
    request = types.SimpleNamespace(
        app={"hass": hass},
        method="POST",
        path="/api/climate_advisor/submit_github_issue",
        remote="127.0.0.1",
        query={"entry_id": entry_id},
        json=AsyncMock(return_value={"title": "Report", "body": "text"}),
    )
    response = await ClimateAdvisorSubmitGithubIssueView().post(request)
    assert response.status == 200
    await hass.async_block_till_done()
    flows = _reauth_flows(hass)
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_github_token", flows
    assert flows[0]["context"]["entry_id"] == entry_id

    flow_id = flows[0]["flow_id"]
    aioclient_mock.get("https://api.github.com/repos/me/house", status=401)
    result = await hass.config_entries.flow.async_configure(flow_id, {"github_token": "also-revoked"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "github_token_invalid"}
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://api.github.com/repos/me/house", status=200, json={"full_name": "me/house"})
    result = await hass.config_entries.flow.async_configure(flow_id, {"github_token": "new-token"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert house.ca_entry.data["github_token"] == "new-token"
    assert house.ca_entry.state is ConfigEntryState.LOADED


async def test_github_submission_refused_for_another_reason_asks_for_nothing(make_house, aioclient_mock) -> None:
    house = await make_house(
        units="us",
        profile="scripted",
        initial_indoor_f=72.0,
        ca_config={"github_token": "tok", "github_repo": "me/house"},
    )
    from custom_components.climate_advisor.api import ClimateAdvisorSubmitGithubIssueView

    aioclient_mock.post("https://api.github.com/repos/me/house/issues", status=422)
    request = types.SimpleNamespace(
        app={"hass": house.hass},
        method="POST",
        path="/api/climate_advisor/submit_github_issue",
        remote="127.0.0.1",
        query={"entry_id": house.ca_entry.entry_id},
        json=AsyncMock(return_value={"title": "Report"}),
    )
    await ClimateAdvisorSubmitGithubIssueView().post(request)
    await house.hass.async_block_till_done()
    assert not _reauth_flows(house.hass)


# ------------------------------------------------------------------ reconfigure (#1646)


async def _reconfigure(hass, entry_id: str):
    result = await hass.config_entries.flow.async_init(
        CA_DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry_id}
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure", result
    return result


async def test_reconfigure_moves_the_zone_to_a_new_thermostat_and_keeps_its_data(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    hass.config_entries.async_update_entry(entry, unique_id=CLIMATE_ENTITY)  # as the setup wizard stores it
    entry_id = entry.entry_id
    new_climate = await _second_thermostat(hass, house, "New Thermostat")
    await house.advance(minutes=5)
    old_coordinator = house.coordinator

    result = await _reconfigure(hass, entry_id)
    shown = {str(k): (k.description or {}).get("suggested_value") for k in result["data_schema"].schema}
    assert shown["climate_entity"] == CLIMATE_ENTITY and shown["weather_entity"] == WEATHER_ENTITY
    core = {"weather_entity": WEATHER_ENTITY, "climate_entity": new_climate, "notify_service": "not valid"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], core)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"notify_service": "invalid_notify_service"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**core, "notify_service": "notify.notify"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful", result
    await house._drain()

    assert hass.config_entries.async_get_entry(entry_id) is entry  # same zone, not a new one
    assert entry.unique_id == new_climate
    assert entry.data["climate_entity"] == new_climate
    assert entry.data["comfort_heat"] == house.ca_entry.data["comfort_heat"]  # settings kept
    assert entry.state is ConfigEntryState.LOADED
    assert house.coordinator is not old_coordinator  # reloaded ...
    assert house.coordinator.config["climate_entity"] == new_climate  # ... on the new thermostat
    assert house.coordinator._entry_id == entry_id  # per-zone files (learning, history) are named by entry id
    assert any(entry_id in p.name for p in house.config_dir.iterdir())


async def test_reconfigure_changes_only_the_weather_service(make_house) -> None:
    from homeassistant.components.weather.const import DATA_COMPONENT

    from tests_ha.house.weather import HouseWeather

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    backup = type("BackupWeather", (HouseWeather,), {"_attr_unique_id": "ca_backup", "_attr_name": "Backup"})
    other = backup("weather.backup")
    await hass.data[DATA_COMPONENT].async_add_entities([other])
    result = await _reconfigure(hass, entry.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"weather_entity": "weather.backup", "climate_entity": CLIMATE_ENTITY, "notify_service": "notify.notify"},
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful", result
    await house._drain()
    assert entry.data["weather_entity"] == "weather.backup"
    assert entry.data["climate_entity"] == CLIMATE_ENTITY and entry.unique_id == CLIMATE_ENTITY
    assert entry.state is ConfigEntryState.LOADED


async def test_thermostat_of_another_zone_is_refused_in_reconfigure_and_in_settings(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass, entry = house.hass, house.ca_entry
    upstairs = await _second_zone(hass, house)
    taken = upstairs.data["climate_entity"]

    result = await _reconfigure(hass, entry.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"weather_entity": WEATHER_ENTITY, "climate_entity": taken, "notify_service": "notify.notify"},
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["climate_entity"] == CLIMATE_ENTITY

    # The zone settings' Core section can change the thermostat too: same rule, then a free one is accepted.
    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": "core"})
    core = {
        "zone_name": entry.title,
        "weather_entity": WEATHER_ENTITY,
        "climate_entity": taken,
        "temp_unit": "fahrenheit",
        "notify_service": "notify.notify",
    }
    result = await options.async_configure(result["flow_id"], core)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"climate_entity": "thermostat_shared_zone"}
    free = await _second_thermostat(hass, house, "Spare Thermostat")
    result = await options.async_configure(result["flow_id"], {**core, "climate_entity": free})
    assert result["type"] is FlowResultType.MENU
    assert entry.data["climate_entity"] == free
    assert entry.unique_id == free  # setup's duplicate check now sees the zone's real thermostat
