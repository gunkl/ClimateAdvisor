"""The AI Settings options step against real Home Assistant (schema serialisation,
provider switch, and that Claude settings survive a round trip through ChatGPT)."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor import api, claude_api, config_flow
from custom_components.climate_advisor.ai_backend import AIUserNotAllowedError
from custom_components.climate_advisor.chatgpt_backend import ChatGPTBackend
from custom_components.climate_advisor.claude_api import ClaudeResponse
from custom_components.climate_advisor.const import CHATGPT_ACCOUNT_HASS_KEY, DOMAIN
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.diagnostics import async_get_config_entry_diagnostics
from tests_ha.test_unload_cleanup import _add_entities, _seed_entry

_SEED = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "integration_harness"
    / "config-seed"
    / ".storage"
    / "core.config_entries"
)


def _fields(result) -> set[str]:
    return {str(k) for k in result["data_schema"].schema}


async def _open_provider_step(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "ai_provider"})


async def _open_ai_step(hass: HomeAssistant, entry: MockConfigEntry, provider: str):
    """Menu, AI Settings, choose ``provider``: returns the settings form."""
    pick = await _open_provider_step(hass, entry)
    assert pick["type"] is FlowResultType.FORM and pick["step_id"] == "ai_provider"
    return await hass.config_entries.options.async_configure(pick["flow_id"], {"ai_provider": provider})


def _entry(hass: HomeAssistant, **extra) -> MockConfigEntry:
    seed = next(e for e in json.loads(_SEED.read_text("utf-8"))["data"]["entries"] if e["domain"] == DOMAIN)
    entry = MockConfigEntry(domain=DOMAIN, version=seed["version"], data={**seed["data"], **extra})
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_provider_step_is_a_single_serialisable_selector(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    pick = await _open_provider_step(hass, entry)
    assert pick["type"] is FlowResultType.FORM and _fields(pick) == {"ai_provider"}
    (marker,) = pick["data_schema"].schema
    spec = pick["data_schema"].schema[marker].serialize()["selector"]["select"]
    assert {o["value"] for o in spec["options"]} >= {"anthropic", "chatgpt"}
    assert marker.default() == "anthropic"
    hass.config_entries.options.async_abort(pick["flow_id"])


@pytest.mark.asyncio
async def test_abandon_after_choosing_provider_changes_nothing(hass: HomeAssistant) -> None:
    entry = _entry(hass, ai_api_key="sk-ant-keepme")
    before = dict(entry.data)
    form = await _open_ai_step(hass, entry, "chatgpt")
    assert form["step_id"] == "ai_settings" and "ai_api_key" not in _fields(form)
    hass.config_entries.options.async_abort(form["flow_id"])
    assert dict(entry.data) == before


@pytest.mark.asyncio
async def test_claude_end_to_end_saves_as_before(hass: HomeAssistant) -> None:
    entry = _entry(hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5", ai_reasoning_effort="low")
    form = await _open_ai_step(hass, entry, "anthropic")
    assert "ai_provider" not in _fields(form)
    assert {"ai_api_key", "ai_temperature"} <= _fields(form)
    done = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ai_enabled": True, "ai_temperature": 0.4, "ai_max_tokens": 1024}
    )
    assert done["type"] is FlowResultType.MENU and done["step_id"] == "init"
    assert entry.data["ai_provider"] == "anthropic" and entry.data["ai_api_key"] == "sk-ant-keepme"
    assert entry.data["ai_temperature"] == 0.4


@pytest.mark.asyncio
async def test_provider_switch_round_trip(hass: HomeAssistant) -> None:
    entry = _entry(hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5", ai_temperature=0.4)

    form = await _open_ai_step(hass, entry, "chatgpt")
    assert form["type"] is FlowResultType.FORM
    assert "ai_api_key" not in _fields(form) and "ai_temperature" not in _fields(form)
    assert entry.data.get("ai_provider", "anthropic") == "anthropic"  # not committed yet
    menu = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})
    assert menu["step_id"] == "chatgpt_account"
    assert entry.data["ai_provider"] == "chatgpt"
    assert entry.data["ai_api_key"] == "sk-ant-keepme"
    assert entry.data["ai_temperature"] == 0.4
    hass.config_entries.options.async_abort(menu["flow_id"])

    # Switch back: Claude fields return with the preserved values.
    form = await _open_ai_step(hass, entry, "anthropic")
    assert "ai_api_key" in _fields(form)
    assert entry.data["ai_model"] == "claude-sonnet-5"
    hass.config_entries.options.async_abort(form["flow_id"])


@pytest.mark.asyncio
async def test_chatgpt_save_leads_to_account_menu_and_signin_form(hass: HomeAssistant) -> None:
    seed = next(e for e in json.loads(_SEED.read_text("utf-8"))["data"]["entries"] if e["domain"] == DOMAIN)
    entry = MockConfigEntry(domain=DOMAIN, version=seed["version"], data={**seed["data"], "ai_provider": "chatgpt"})
    entry.add_to_hass(hass)

    form = await _open_ai_step(hass, entry, "chatgpt")
    menu = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})
    assert menu["type"] is FlowResultType.MENU and menu["step_id"] == "chatgpt_account"
    assert "chatgpt_account_signin" in menu["menu_options"]

    with patch.object(config_flow, "_current_user_id", return_value=None):
        signin = await hass.config_entries.options.async_configure(
            menu["flow_id"], {"next_step_id": "chatgpt_account_signin"}
        )
    assert signin["type"] is FlowResultType.FORM and _fields(signin) == {"chatgpt_account_redirect_url"}
    assert signin["errors"] == {"base": "owner_required"}
    hass.config_entries.options.async_abort(signin["flow_id"])


@pytest.mark.asyncio
async def test_fake_extra_provider_appears_in_selector(hass: HomeAssistant) -> None:
    from custom_components.climate_advisor.ai_backend import PROVIDERS, get_provider
    from custom_components.climate_advisor.ai_providers import ensure_providers_registered

    ensure_providers_registered()
    PROVIDERS["fakeprov"] = replace(get_provider("anthropic"), id="fakeprov", label="Fake Provider")
    try:
        entry = _entry(hass)
        pick = await _open_provider_step(hass, entry)
        (marker,) = pick["data_schema"].schema
        options = pick["data_schema"].schema[marker].config["options"]
        assert {"value": "fakeprov", "label": "Fake Provider"} in options
        hass.config_entries.options.async_abort(pick["flow_id"])
    finally:
        del PROVIDERS["fakeprov"]


# ---------------------------------------------------------------------------
# A provider whose API-key field has its own name (OpenRouter's shape)
# ---------------------------------------------------------------------------


@pytest.fixture
def own_key_provider():
    """Registers ``ownkey``: API-key provider, key in ``ai_ownkey_key``, model in ``ai_ownkey_model``."""
    from custom_components.climate_advisor.ai_backend import PROVIDERS, get_provider
    from custom_components.climate_advisor.ai_providers import ensure_providers_registered

    ensure_providers_registered()
    validate = AsyncMock(side_effect=lambda config, hass=None: (config.get("ai_ownkey_key") == "good-key-1234", "x"))
    PROVIDERS["ownkey"] = replace(
        get_provider("anthropic"),
        id="ownkey",
        label="Own Key Router",
        display_name="Own Key Router",
        api_name="Own Key Router",
        build_backend=lambda config, client=None: MagicMock(client=object(), inactive_reason=None),
        supports_tuning=False,
        supports_tier_fallback=False,
        uses_claude_wording=False,
        resolve_request_options=lambda config, overrides: {"model": config.get("ai_ownkey_model", "")},
        list_models=AsyncMock(return_value=["own/m1", "own/m2"]),
        validate_credentials=validate,
        credential_setting="ai_ownkey_key",
        model_setting="ai_ownkey_model",
        default_model="",
        fallback_models=(),
    )
    yield validate
    PROVIDERS.pop("ownkey", None)


@pytest.mark.asyncio
async def test_own_key_provider_form_validation_and_saving(hass: HomeAssistant, own_key_provider) -> None:
    entry = _entry(hass, ai_api_key="sk-ant-claude")
    form = await _open_ai_step(hass, entry, "ownkey")
    assert "ai_ownkey_key" in _fields(form) and "ai_api_key" not in _fields(form)
    # Claude's saved key is not this provider's key: not masked as configured, and not accepted.
    assert "••••" not in form["description_placeholders"]["status_lines"]
    base = {"ai_enabled": True, "ai_ownkey_model": "own/m1"}
    missing = await hass.config_entries.options.async_configure(form["flow_id"], base)
    assert missing["errors"] == {"ai_ownkey_key": "ai_key_required"}

    bad = await hass.config_entries.options.async_configure(form["flow_id"], {**base, "ai_ownkey_key": "bad"})
    assert bad["errors"] == {"base": "ai_connection_failed"}
    assert entry.data.get("ai_provider", "anthropic") == "anthropic"

    saved = await hass.config_entries.options.async_configure(
        form["flow_id"], {**base, "ai_ownkey_key": "good-key-1234"}
    )
    assert saved["type"] is FlowResultType.MENU and saved["step_id"] == "init"
    assert entry.data["ai_provider"] == "ownkey"
    assert entry.data["ai_ownkey_key"] == "good-key-1234"
    assert entry.data["ai_ownkey_model"] == "own/m1"
    assert entry.data["ai_api_key"] == "sk-ant-claude"

    # Blank on the next visit keeps the saved key, without re-validating it.
    own_key_provider.reset_mock()
    again = await _open_ai_step(hass, entry, "ownkey")
    assert again["description_placeholders"]["status_lines"].endswith("1234")
    await hass.config_entries.options.async_configure(again["flow_id"], base)
    assert entry.data["ai_ownkey_key"] == "good-key-1234"
    own_key_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_key_provider_coordinator_gate_and_diagnostics(
    hass: HomeAssistant, own_key_provider, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    keyed = await _setup_zone(hass, ai_provider="ownkey", ai_ownkey_key="own-secret-key", ai_ownkey_model="own/m1")
    coordinator = hass.data[DOMAIN][keyed.entry_id]
    assert coordinator.claude_client is not None
    assert coordinator.claude_client.configured_model() == "own/m1"

    payload = await async_get_config_entry_diagnostics(hass, keyed)
    assert "own-secret-key" not in json.dumps(payload, default=str)

    assert await hass.config_entries.async_unload(keyed.entry_id)
    # Claude's key does not open the gate for a provider with its own key field.
    unkeyed = await _setup_zone(hass, ai_provider="ownkey", ai_api_key="sk-ant-claude")
    assert hass.data[DOMAIN][unkeyed.entry_id].claude_client is None


# ---------------------------------------------------------------------------
# After sign-in the running zone must see the result without a reload
# ---------------------------------------------------------------------------


async def _setup_zone(hass: HomeAssistant, **extra) -> MockConfigEntry:
    seed = _seed_entry()
    data = {**seed["data"], "ai_enabled": True, **extra}
    _add_entities(hass, data)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=seed["version"],
        minor_version=seed.get("minor_version", 1),
        data=data,
        options=seed.get("options", {}),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
def chatgpt_models(hass: HomeAssistant):
    """The account's model list, patched where the real provider path fetches it."""

    async def _list():
        # As the real account: nothing to list until it is connected.
        return ["gpt-x", "gpt-y"] if hass.data[CHATGPT_ACCOUNT_HASS_KEY].status()["state"] == "connected" else []

    models = AsyncMock(side_effect=_list)
    with patch.object(ChatGPTBackend, "async_list_models", models):
        yield models


@contextmanager
def _signed_in_flow(hass: HomeAssistant, readiness: str = "ready") -> Iterator[AsyncMock]:
    """Sign-in stubs at the manager seam; the real flow, coordinator and client run."""
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]

    async def begin():
        return "https://auth.openai.com/oauth/authorize?state=fake"

    async def complete(pasted_url, owner_user_id):
        manager._state, manager._owner_user_id = "connected", owner_user_id
        return manager.status()

    with (
        patch.object(manager, "async_begin_login", side_effect=begin),
        patch.object(manager, "async_complete_login", side_effect=complete),
        patch.object(config_flow, "_current_user_id", return_value="owner-1"),
        patch.object(config_flow, "_check_account_readiness", AsyncMock(return_value=readiness)),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock,
    ):
        yield reload_mock


async def _sign_in_from_menu(hass: HomeAssistant, entry: MockConfigEntry):
    """Menu, AI Settings, ChatGPT, sign in: returns the step after the pasted address."""
    form = await _open_ai_step(hass, entry, "chatgpt")
    step = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})
    assert step["step_id"] == "chatgpt_account"
    step = await hass.config_entries.options.async_configure(
        step["flow_id"], {"next_step_id": "chatgpt_account_signin"}
    )
    assert step["step_id"] == "chatgpt_account_signin"
    return await hass.config_entries.options.async_configure(
        step["flow_id"], {"chatgpt_account_redirect_url": "http://127.0.0.1:1455/auth/callback?code=c&state=fake"}
    )


async def _investigate_status(hass: HomeAssistant, coordinator) -> int:
    """POST the investigate view as the owner; the skill itself is stubbed, its gates are not."""
    coordinator.ai_skills = MagicMock()
    coordinator.ai_skills.async_execute = AsyncMock(
        return_value={"success": False, "source": "fallback", "data": {}, "error": "stub"}
    )
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {}
    request.headers = {}
    request.json = AsyncMock(return_value={"focus": "", "hours": 24})
    request.get = lambda key, default=None: {"hass_user": SimpleNamespace(id="owner-1")}.get(key, default)
    resp = await api.ClimateAdvisorInvestigateView().post(request)
    coordinator.ai_skills.async_execute.assert_awaited_once()
    return resp.status


@pytest.mark.asyncio
async def test_first_sign_in_model_choice_reaches_running_zone_without_reload(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_provider="chatgpt", ai_investigator_enabled=True)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    assert client.configured_model() == ""

    with _signed_in_flow(hass) as reload_mock:
        step = await _sign_in_from_menu(hass, entry)
        assert step["step_id"] == "chatgpt_account_model"
        done = await hass.config_entries.options.async_configure(step["flow_id"], {"ai_chatgpt_model": "gpt-y"})
    assert done["step_id"] == "chatgpt_account_done"
    assert "are ready" in done["description_placeholders"]["readiness"]
    assert entry.data["ai_chatgpt_model"] == "gpt-y"

    reload_mock.assert_not_called()
    assert hass.data[DOMAIN][entry.entry_id] is coordinator and coordinator.claude_client is client
    assert client.configured_model() == "gpt-y"
    assert await _investigate_status(hass, coordinator) == 200
    hass.config_entries.options.async_abort(done["flow_id"])


@pytest.mark.asyncio
async def test_switch_from_unconfigured_claude_to_chatgpt_reaches_running_zone(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    """The real first setup: the zone started on Claude with AI off and no key, so it has no client at all."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_enabled=False, ai_api_key="", ai_investigator_enabled=True)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.claude_client is None

    with _signed_in_flow(hass) as reload_mock:
        step = await _sign_in_from_menu(hass, entry)
        assert step["step_id"] == "chatgpt_account_model"
        done = await hass.config_entries.options.async_configure(step["flow_id"], {"ai_chatgpt_model": "gpt-x"})
    assert "are ready" in done["description_placeholders"]["readiness"]
    reload_mock.assert_not_called()

    client = coordinator.claude_client
    assert client is not None and client.configured_model() == "gpt-x"
    assert client.get_status()["provider"] == "chatgpt"
    assert client.get_status()["status"] == "active"
    assert coordinator.ai_skills is not None
    assert await _investigate_status(hass, coordinator) == 200
    hass.config_entries.options.async_abort(done["flow_id"])


@pytest.mark.asyncio
async def test_switch_from_working_claude_to_chatgpt_stops_using_claude(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.claude_client.get_status()["provider"] == "anthropic"

    with _signed_in_flow(hass):
        step = await _sign_in_from_menu(hass, entry)
        done = await hass.config_entries.options.async_configure(step["flow_id"], {"ai_chatgpt_model": "gpt-x"})
    status = coordinator.claude_client.get_status()
    assert status["provider"] == "chatgpt" and status["model"] == "gpt-x" and status["status"] == "active"
    hass.config_entries.options.async_abort(done["flow_id"])


@pytest.mark.asyncio
async def test_done_does_not_claim_ready_when_the_running_zone_did_not_take_the_model(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_provider="chatgpt")
    coordinator = hass.data[DOMAIN][entry.entry_id]

    with _signed_in_flow(hass):
        step = await _sign_in_from_menu(hass, entry)
        with patch.object(coordinator, "async_apply_ai_settings", AsyncMock(side_effect=RuntimeError("boom"))):
            done = await hass.config_entries.options.async_configure(step["flow_id"], {"ai_chatgpt_model": "gpt-x"})
    assert done["step_id"] == "chatgpt_account_done"
    text = done["description_placeholders"]["readiness"]
    assert "are ready" not in text and "Reload" in text
    hass.config_entries.options.async_abort(done["flow_id"])


@pytest.mark.asyncio
async def test_done_says_ai_is_off_when_the_zone_has_ai_turned_off(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-x")
    form = await _open_ai_step(hass, entry, "chatgpt")
    with _signed_in_flow(hass):
        step = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ai_enabled": False, "ai_chatgpt_model": "gpt-x"}
        )
        step = await hass.config_entries.options.async_configure(
            step["flow_id"], {"next_step_id": "chatgpt_account_signin"}
        )
        done = await hass.config_entries.options.async_configure(
            step["flow_id"], {"chatgpt_account_redirect_url": "http://127.0.0.1:1455/auth/callback?code=c&state=fake"}
        )
    text = done["description_placeholders"]["readiness"]
    assert "are ready" not in text and "turned off" in text
    hass.config_entries.options.async_abort(done["flow_id"])


@pytest.mark.asyncio
async def test_model_picked_later_in_ai_settings_reaches_running_zone(
    hass: HomeAssistant, chatgpt_models, tmp_path
) -> None:
    """Sign-in could not list models and said to pick one in AI Settings: that pick must work too."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_provider="chatgpt")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hass.data[CHATGPT_ACCOUNT_HASS_KEY]._state = "connected"
    form = await _open_ai_step(hass, entry, "chatgpt")
    menu = await hass.config_entries.options.async_configure(
        form["flow_id"], {"ai_enabled": True, "ai_chatgpt_model": "gpt-y"}
    )
    assert menu["step_id"] == "chatgpt_account"
    assert coordinator.claude_client.configured_model() == "gpt-y"
    hass.config_entries.options.async_abort(menu["flow_id"])


@pytest.mark.asyncio
async def test_claude_save_still_waits_for_reload(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(
        hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5", ai_temperature=0.4, ai_reasoning_effort="low"
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    with patch.object(ClimateAdvisorCoordinator, "async_apply_ai_settings", AsyncMock(), create=True) as apply_mock:
        form = await _open_ai_step(hass, entry, "anthropic")
        done = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ai_enabled": True, "ai_temperature": 0.9, "ai_max_tokens": 1024}
        )
    assert done["step_id"] == "init"
    apply_mock.assert_not_called()
    assert entry.data["ai_temperature"] == 0.9
    assert coordinator.config["ai_temperature"] == 0.4 and coordinator.claude_client is client


@pytest.mark.asyncio
async def test_switch_from_chatgpt_to_claude_stops_using_chatgpt_at_once(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(
        hass,
        ai_provider="chatgpt",
        ai_chatgpt_model="gpt-x",
        ai_api_key="sk-ant-keepme",
        ai_model="claude-sonnet-5",
        ai_reasoning_effort="low",
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    assert isinstance(client._backend, ChatGPTBackend)

    form = await _open_ai_step(hass, entry, "anthropic")
    # The SDK is not installed in this tier.
    with (
        patch.object(claude_api, "ANTHROPIC_AVAILABLE", True),
        patch.object(claude_api, "AsyncAnthropic", MagicMock(), create=True) as sdk,
    ):
        done = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})

    assert done["step_id"] == "init"
    assert coordinator.claude_client is client
    status = client.get_status()
    assert status["provider"] == "anthropic" and status["model"] == "claude-sonnet-5"
    assert isinstance(client._backend, claude_api.AnthropicBackend)
    sdk.assert_called_once_with(api_key="sk-ant-keepme")
    assert coordinator.config["ai_provider"] == "anthropic"
    hass.config_entries.options.async_abort(done["flow_id"])


@contextmanager
def _provider_build_fails(provider: str) -> Iterator[None]:
    from custom_components.climate_advisor.ai_backend import PROVIDERS
    from custom_components.climate_advisor.ai_providers import ensure_providers_registered

    ensure_providers_registered()
    original = PROVIDERS[provider]
    PROVIDERS[provider] = replace(original, build_backend=MagicMock(side_effect=RuntimeError("boom")))
    try:
        yield
    finally:
        PROVIDERS[provider] = original


@pytest.mark.asyncio
async def test_failed_switch_away_from_chatgpt_stops_the_zone_and_says_so(hass: HomeAssistant, tmp_path) -> None:
    """The save stands and the build failed: nothing goes to OpenAI, and nothing is half applied."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(
        hass,
        ai_provider="chatgpt",
        ai_chatgpt_model="gpt-x",
        ai_api_key="sk-ant-keepme",
        ai_model="claude-sonnet-5",
        ai_reasoning_effort="low",
        ai_temperature=0.4,
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    form = await _open_ai_step(hass, entry, "anthropic")

    with (
        _provider_build_fails("anthropic"),
        patch.object(ChatGPTBackend, "async_request", AsyncMock()) as sent,
        patch.object(ChatGPTBackend, "async_stream", MagicMock()) as streamed,
    ):
        failed = await hass.config_entries.options.async_configure(
            form["flow_id"], {"ai_enabled": True, "ai_temperature": 0.9, "ai_max_tokens": 1024}
        )
        response = await client.async_request("sys", "hi")
        with pytest.raises(RuntimeError, match="reload"):
            async for _event in client.async_request_streaming("sys", "hi"):
                pass

    assert failed["type"] is FlowResultType.FORM and failed["step_id"] == "ai_apply_failed"
    assert entry.data["ai_provider"] == "anthropic"
    assert coordinator.config["ai_provider"] == "chatgpt" and coordinator.config["ai_temperature"] == 0.4
    assert response.success is False and "reload" in response.error
    status = client.get_status()
    assert status["status"] == "inactive" and "reload" in status["stop_reason"]
    sent.assert_not_called()
    streamed.assert_not_called()

    menu = await hass.config_entries.options.async_configure(failed["flow_id"], {})
    assert menu["step_id"] == "init"
    hass.config_entries.options.async_abort(menu["flow_id"])


@pytest.mark.asyncio
async def test_failed_switch_onto_chatgpt_stops_the_zone_and_says_so(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_api_key="sk-ant-keepme")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    form = await _open_ai_step(hass, entry, "chatgpt")

    with _provider_build_fails("chatgpt"):
        failed = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})

    assert failed["step_id"] == "ai_apply_failed"
    assert entry.data["ai_provider"] == "chatgpt" and coordinator.config.get("ai_provider", "anthropic") == "anthropic"
    assert "reload" in (await coordinator.claude_client.async_request("sys", "hi")).error
    hass.config_entries.options.async_abort(failed["flow_id"])


@pytest.mark.asyncio
async def test_apply_that_works_after_a_failed_one_restarts_the_zone(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-x", ai_api_key="sk-ant-keepme")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    saved = {**entry.data, "ai_provider": "anthropic"}
    with _provider_build_fails("anthropic"), pytest.raises(RuntimeError, match="boom"):
        await coordinator.async_apply_ai_settings(saved)
    assert client.ready_model() == ""

    with (
        patch.object(claude_api, "ANTHROPIC_AVAILABLE", True),
        patch.object(claude_api, "AsyncAnthropic", MagicMock(), create=True),
    ):
        await coordinator.async_apply_ai_settings(saved)

    assert client.ready_model() == client.configured_model() != ""
    assert client.provider_id == "anthropic" and coordinator.config["ai_provider"] == "anthropic"
    # Built against a copy, the new backend then shares the dict that later config changes reach.
    assert client._config is coordinator.config and client._backend._config is coordinator.config


@pytest.mark.asyncio
async def test_new_request_while_the_switch_builds_does_not_reach_the_old_provider(
    hass: HomeAssistant, tmp_path
) -> None:
    """Saved away from ChatGPT: nothing new goes to OpenAI from the first await, and the commit lifts the stop."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(
        hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-x", ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5"
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.claude_client
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    manager._state, manager._owner_user_id = "connected", "owner-1"
    building, finish_build = asyncio.Event(), asyncio.Event()
    run_job = coordinator._executor_job

    async def held_job(fn, *args):
        building.set()
        await finish_build.wait()
        return await run_job(fn, *args)

    with (
        patch.object(claude_api, "ANTHROPIC_AVAILABLE", True),
        patch.object(claude_api, "AsyncAnthropic", MagicMock(), create=True),
        patch.object(coordinator, "_executor_job", held_job),
        patch.object(ChatGPTBackend, "async_request", AsyncMock(side_effect=_plan_response)) as sent,
    ):
        applying = hass.async_create_task(
            coordinator.async_apply_ai_settings({**entry.data, "ai_provider": "anthropic"})
        )
        await building.wait()
        response = await client.async_request("sys", "hi")
        stop_while_building = client.get_status()["stop_reason"]
        finish_build.set()
        await applying

    sent.assert_not_awaited()
    assert response.success is False and "reload" in response.error and stop_while_building
    assert client.provider_id == "anthropic" and client.get_status()["stop_reason"] is None


@pytest.mark.asyncio
async def test_switch_from_chatgpt_to_claude_with_ai_off_stops_at_once(hass: HomeAssistant, tmp_path) -> None:
    """AI off: the client is not rebuilt, and the investigate gate reads the setting applied here."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(
        hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-x", ai_model="claude-sonnet-5", ai_reasoning_effort="low"
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    form = await _open_ai_step(hass, entry, "anthropic")
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": False})

    assert done["step_id"] == "init"
    assert coordinator.config["ai_enabled"] is False and coordinator.config["ai_provider"] == "anthropic"
    assert coordinator.claude_client.get_status()["status"] == "disabled"
    hass.config_entries.options.async_abort(done["flow_id"])


def _model_field(form, key: str) -> tuple[list[str], str]:
    """The dropdown's values and its preselected value."""
    marker = next(m for m in form["data_schema"].schema if str(m) == key)
    return [o["value"] for o in form["data_schema"].schema[marker].config["options"]], marker.default()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["claude-retired", ""], ids=["not_in_live_list", "blank"])
async def test_claude_model_dropdown_is_the_live_list_as_before(hass: HomeAssistant, saved: str) -> None:
    """As at base: the saved model is the default, and it is not added to the provider's list."""
    from custom_components.climate_advisor.ai_backend import PROVIDERS

    entry = _entry(hass, ai_api_key="sk-ant-keepme", ai_model=saved)
    with patch.dict(
        PROVIDERS, {"anthropic": replace(PROVIDERS["anthropic"], list_models=AsyncMock(return_value=["claude-live"]))}
    ):
        form = await _open_ai_step(hass, entry, "anthropic")
    assert _model_field(form, "ai_model") == (["claude-live"], saved)
    hass.config_entries.options.async_abort(form["flow_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "offered"),
    [(["gpt-live"], ["gpt-saved", "gpt-live"]), ([], ["gpt-saved"])],
    ids=["not_in_live_list", "list_unavailable"],
)
async def test_chatgpt_saved_model_stays_selectable(hass: HomeAssistant, live: list[str], offered: list[str]) -> None:
    """The saved model must still validate, so saving another setting does not force a new model."""
    entry = _entry(hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-saved")
    with patch.object(ChatGPTBackend, "async_list_models", AsyncMock(return_value=live)):
        form = await _open_ai_step(hass, entry, "chatgpt")
    assert _model_field(form, "ai_chatgpt_model") == (offered, "gpt-saved")
    hass.config_entries.options.async_abort(form["flow_id"])


@pytest.mark.asyncio
async def test_auto_request_limit_hidden_for_a_manual_only_provider_and_kept(hass: HomeAssistant) -> None:
    entry = _entry(hass, ai_api_key="sk-ant-keepme", ai_auto_requests_per_day=7)

    form = await _open_ai_step(hass, entry, "chatgpt")
    assert "ai_auto_requests_per_day" not in _fields(form)
    menu = await hass.config_entries.options.async_configure(form["flow_id"], {"ai_enabled": True})
    assert menu["step_id"] == "chatgpt_account"
    assert entry.data["ai_auto_requests_per_day"] == 7
    hass.config_entries.options.async_abort(menu["flow_id"])

    form = await _open_ai_step(hass, entry, "anthropic")
    marker = next(k for k in form["data_schema"].schema if str(k) == "ai_auto_requests_per_day")
    assert marker.default() == 7
    hass.config_entries.options.async_abort(form["flow_id"])


# ---------------------------------------------------------------------------
# An admin switches the zone to the connected account while an investigation gathers context
# ---------------------------------------------------------------------------


async def _zone_switching_during_context(hass: HomeAssistant, tmp_path):
    """A Claude zone with a connected account, whose context builder performs the live switch."""
    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5")
    coordinator = hass.data[DOMAIN][entry.entry_id]
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    manager._state, manager._owner_user_id = "connected", "owner-1"

    async def context(_hass, _coordinator, **_kwargs):
        await coordinator.async_apply_ai_settings({**entry.data, "ai_provider": "chatgpt", "ai_chatgpt_model": "gpt-x"})
        return "private context"

    skill = coordinator.ai_skills.get("investigator")
    coordinator.ai_skills._skills["investigator"] = replace(skill, context_builder=context, fallback=MagicMock())
    return coordinator


def _plan_response(**_kwargs):
    return ClaudeResponse(
        success=True, content="ok", input_tokens=0, output_tokens=0, estimated_cost=0.0, latency_ms=1.0
    )


async def _plan_stream(self, **_kwargs):
    _plan_stream.calls += 1
    yield {"type": "text", "text": "ok"}
    yield {"type": "stop", "stop_reason": "end_turn", "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0}


async def _run_stream(coordinator, hass: HomeAssistant, requester_id: str) -> list[dict]:
    return [
        event
        async for event in coordinator.ai_skills.async_execute_streaming(
            "investigator", hass, coordinator, coordinator.claude_client, requester_id=requester_id
        )
    ]


@pytest.mark.asyncio
async def test_switch_during_context_refuses_non_owner_at_dispatch(hass: HomeAssistant, tmp_path) -> None:
    coordinator = await _zone_switching_during_context(hass, tmp_path)
    send = AsyncMock(side_effect=_plan_response)
    with patch.object(ChatGPTBackend, "async_request", send), pytest.raises(AIUserNotAllowedError):
        await coordinator.ai_skills.async_execute(
            "investigator", hass, coordinator, coordinator.claude_client, requester_id="someone-else"
        )
    send.assert_not_awaited()
    coordinator.ai_skills.get("investigator").fallback.assert_not_called()


@pytest.mark.asyncio
async def test_switch_during_context_refuses_non_owner_stream_at_dispatch(hass: HomeAssistant, tmp_path) -> None:
    coordinator = await _zone_switching_during_context(hass, tmp_path)
    _plan_stream.calls = 0
    with patch.object(ChatGPTBackend, "async_stream", _plan_stream):
        events = await _run_stream(coordinator, hass, "someone-else")
    assert _plan_stream.calls == 0
    assert events[-1]["type"] == "error" and events[-1]["status"] == 403
    assert "Only the user who connected" in events[-1]["message"]


@pytest.mark.asyncio
async def test_switch_during_context_still_works_for_the_owner(hass: HomeAssistant, tmp_path) -> None:
    coordinator = await _zone_switching_during_context(hass, tmp_path)
    send = AsyncMock(side_effect=_plan_response)
    with patch.object(ChatGPTBackend, "async_request", send):
        result = await coordinator.ai_skills.async_execute(
            "investigator", hass, coordinator, coordinator.claude_client, requester_id="owner-1"
        )
    assert result["success"] is True
    send.assert_awaited_once()
    assert send.await_args.kwargs["model"] == "gpt-x"


@pytest.mark.asyncio
async def test_switch_during_context_still_streams_for_the_owner(hass: HomeAssistant, tmp_path) -> None:
    coordinator = await _zone_switching_during_context(hass, tmp_path)
    _plan_stream.calls = 0
    with patch.object(ChatGPTBackend, "async_stream", _plan_stream):
        events = await _run_stream(coordinator, hass, "owner-1")
    assert _plan_stream.calls == 1 and events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_live_switch_never_exposes_a_half_switched_client(hass: HomeAssistant, tmp_path) -> None:
    """The new backend is built in the executor, so the loop can dispatch meanwhile: the old pair must still stand."""
    from custom_components.climate_advisor.ai_backend import PROVIDERS

    hass.config.config_dir = str(tmp_path)
    entry = await _setup_zone(hass, ai_api_key="sk-ant-keepme", ai_model="claude-sonnet-5")
    client = hass.data[DOMAIN][entry.entry_id].claude_client
    original = PROVIDERS["chatgpt"]
    seen: list[str] = []

    def build(config, manager=None):
        seen.append(client._spec.id)
        return original.build_backend(config, manager)

    PROVIDERS["chatgpt"] = replace(original, build_backend=build)
    try:
        await hass.data[DOMAIN][entry.entry_id].async_apply_ai_settings({**entry.data, "ai_provider": "chatgpt"})
    finally:
        PROVIDERS["chatgpt"] = original
    assert seen == ["anthropic"]
    assert client._spec.id == "chatgpt"


@pytest.mark.asyncio
async def test_investigator_note_placeholder_only_for_claude(hass: HomeAssistant) -> None:
    note = "Each investigation uses extended thinking. "
    claude = await _open_ai_step(hass, _entry(hass), "anthropic")
    assert claude["description_placeholders"]["investigator_note"] == note
    hass.config_entries.options.async_abort(claude["flow_id"])
    chatgpt = await _open_ai_step(hass, _entry(hass), "chatgpt")
    assert chatgpt["description_placeholders"]["investigator_note"] == ""
    hass.config_entries.options.async_abort(chatgpt["flow_id"])
    text = json.loads((Path(config_flow.__file__).parent / "strings.json").read_text("utf-8"))
    desc = text["options"]["step"]["ai_settings"]["data_description"]["ai_investigator_requests_per_day"]
    assert desc.format(investigator_note=note) == (
        "Maximum investigative analysis runs per day. Each investigation uses extended thinking. Resets at midnight."
    )


# ---------------------------------------------------------------------------
# A broken ChatGPT module never fails setup; Claude-only installs get no account store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_enabled", [False, True], ids=["ai_off", "ai_on"])
async def test_provider_registration_failure_never_fails_setup(
    hass: HomeAssistant, tmp_path, caplog, ai_enabled
) -> None:
    """The real import of the ChatGPT module fails: that provider is lost, the entry still loads."""
    from custom_components.climate_advisor.ai_backend import PROVIDERS

    hass.config.config_dir = str(tmp_path)
    without_chatgpt = {key: value for key, value in PROVIDERS.items() if key != "chatgpt"}
    with (
        patch.dict(sys.modules, {ChatGPTBackend.__module__: None}),
        patch.dict(PROVIDERS, without_chatgpt, clear=True),
    ):
        entry = await _setup_zone(hass, ai_enabled=ai_enabled, ai_api_key="sk-ant-keepme")
        assert "chatgpt" not in PROVIDERS and "anthropic" in PROVIDERS
    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]
    assert "AI provider module chatgpt_backend could not be loaded" in caplog.text


@pytest.mark.asyncio
async def test_claude_only_install_creates_no_account_store(hass: HomeAssistant, hass_storage, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    await _setup_zone(hass, ai_api_key="sk-ant-keepme")
    assert "climate_advisor.chatgpt_account" not in hass_storage
    assert hass.data[CHATGPT_ACCOUNT_HASS_KEY].host_id is None


@pytest.mark.asyncio
async def test_existing_sign_in_loads_unchanged(hass: HomeAssistant, hass_storage, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    stored = {
        "host_id": "urn:uuid:existing",
        "state": "connected",
        "owner_user_id": "owner-1",
        "credentials": {
            "client_id": "c",
            "subject": "s",
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 9e12,
        },
    }
    hass_storage["climate_advisor.chatgpt_account"] = {"version": 1, "minor_version": 1, "key": "x", "data": stored}
    await _setup_zone(hass, ai_provider="chatgpt", ai_chatgpt_model="gpt-x")
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    assert manager.state == "connected" and manager.owner_user_id == "owner-1"
    assert manager.host_id == "urn:uuid:existing"
    assert hass_storage["climate_advisor.chatgpt_account"]["data"] == stored
    manager.async_cancel_runtime_tasks()
