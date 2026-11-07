"""ChatGPT sign-in through the real options-flow HTTP views against real Home Assistant.

Proves where the owner id comes from: the flow step reads ``request["hass_user"]`` from
``homeassistant.helpers.http.current_request`` while an admin drives ``/api/config/config_entries/options/flow``
(flow context has no user id). The account manager's begin/complete calls are replaced (no OpenAI traffic; the
real manager's login logic is covered in its own tests); everything else is real: manager, Store, entry,
coordinator, flow manager, REST views and the status/disconnect views. All values are fake.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.climate_advisor import api, config_flow
from custom_components.climate_advisor.const import CHATGPT_ACCOUNT_HASS_KEY, DOMAIN
from tests_ha.test_unload_cleanup import _add_entities, _seed_entry

LOGIN_URL = "https://auth.openai.com/oauth/authorize?state=fake"
PASTED = "http://127.0.0.1:1455/auth/callback?code=fake&state=fake"
FLOW = "/api/config/config_entries/options/flow"


def _request(hass, user):
    req = MagicMock()
    req.app = {"hass": hass}
    req.query = {}
    req.get = lambda key, default=None: {"hass_user": user}.get(key, default)
    return req


async def _setup_chatgpt_zone(hass: HomeAssistant, tmp_path) -> MockConfigEntry:
    hass.config.config_dir = str(tmp_path)
    seed = _seed_entry()
    data = {**seed["data"], "ai_enabled": True, "ai_provider": "chatgpt"}
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


async def _post(client, path, body=None):
    resp = await client.post(path, json=body if body is not None else {})
    assert resp.status == 200, await resp.text()
    return await resp.json()


@pytest.mark.asyncio
async def test_admin_signs_in_through_options_flow_http_and_owner_is_the_request_user(
    hass: HomeAssistant, hass_client, hass_admin_user, tmp_path
) -> None:
    entry = await _setup_chatgpt_zone(hass, tmp_path)
    assert await async_setup_component(hass, "config", {})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    client_obj = coordinator.claude_client
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    seen: dict[str, object] = {}

    async def begin():
        return LOGIN_URL

    async def complete(pasted_url, owner_user_id):
        seen["pasted"], seen["owner"] = pasted_url, owner_user_id
        manager._state, manager._owner_user_id = "connected", owner_user_id
        return manager.status()

    http = await hass_client()
    with (
        patch.object(manager, "async_begin_login", side_effect=begin, create=True),
        patch.object(manager, "async_complete_login", side_effect=complete, create=True),
        patch.object(config_flow, "_check_account_readiness", AsyncMock(return_value="ready")),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock,
    ):
        step = await _post(http, FLOW, {"handler": entry.entry_id})
        step = await _post(http, f"{FLOW}/{step['flow_id']}", {"next_step_id": "ai_provider"})
        assert step["step_id"] == "ai_provider"
        step = await _post(http, f"{FLOW}/{step['flow_id']}", {"ai_provider": "chatgpt"})
        assert step["step_id"] == "ai_settings"
        step = await _post(http, f"{FLOW}/{step['flow_id']}", {"ai_enabled": True})
        assert step["type"] == "menu" and step["step_id"] == "chatgpt_account"
        assert step["menu_options"] == ["chatgpt_account_signin", "init"]

        step = await _post(http, f"{FLOW}/{step['flow_id']}", {"next_step_id": "chatgpt_account_signin"})
        assert step["step_id"] == "chatgpt_account_signin"
        assert step["description_placeholders"]["login_url"] == LOGIN_URL

        step = await _post(http, f"{FLOW}/{step['flow_id']}", {"chatgpt_account_redirect_url": PASTED})
        assert step["step_id"] == "chatgpt_account_done"
        # This zone has no model and the fake account lists none, so it must not claim to be ready.
        assert "pick a model" in step["description_placeholders"]["readiness"]

    # The owner is the admin whose HTTP request drove the step: the evidence for current_request["hass_user"].
    assert seen == {"pasted": PASTED, "owner": hass_admin_user.id}
    assert manager.owner_user_id == hass_admin_user.id

    # Connecting needs no reload: same entry state, coordinator and client object; nothing was reloaded.
    reload_mock.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is coordinator and coordinator.claude_client is client_obj
    status = client_obj.get_status()
    assert status["status"] == "active" and status["account_state"] == "connected"
    assert client_obj.check_user_allowed(hass_admin_user.id) is True
    assert client_obj.check_user_allowed("someone-else") is False

    # Status view: identity for admins and the owner only.
    admin = SimpleNamespace(id=hass_admin_user.id, is_admin=True)
    other = SimpleNamespace(id="other", is_admin=False)
    for user, sees in ((admin, True), (other, False)):
        body = json.loads((await api.ClimateAdvisorChatGPTAccountStatusView().get(_request(hass, user))).text)
        assert ("email" in body) is sees

    # A second admin may disconnect (no revoke traffic).
    with patch.object(manager, "_async_revoke", AsyncMock(return_value="confirmed")):
        gone = await api.ClimateAdvisorChatGPTAccountDisconnectView().post(
            _request(hass, SimpleNamespace(id="admin-2", is_admin=True))
        )
    assert gone.status == 200
    assert client_obj.get_status()["status"] == "not_connected"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_abandoned_options_flow_cancels_the_pending_login(hass: HomeAssistant, hass_client, tmp_path) -> None:
    entry = await _setup_chatgpt_zone(hass, tmp_path)
    assert await async_setup_component(hass, "config", {})
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    http = await hass_client()
    cancel = MagicMock()
    with (
        patch.object(manager, "async_begin_login", AsyncMock(return_value=LOGIN_URL), create=True),
        patch.object(manager, "async_cancel_login", cancel),
        patch.object(type(manager), "pending_login_id", new_callable=PropertyMock, return_value="L1"),
    ):
        step = await _post(http, FLOW, {"handler": entry.entry_id})
        flow_id = step["flow_id"]
        step = await _post(http, f"{FLOW}/{flow_id}", {"next_step_id": "ai_provider"})
        step = await _post(http, f"{FLOW}/{flow_id}", {"ai_provider": "chatgpt"})
        step = await _post(http, f"{FLOW}/{flow_id}", {"ai_enabled": True})
        step = await _post(http, f"{FLOW}/{flow_id}", {"next_step_id": "chatgpt_account_signin"})
        assert step["step_id"] == "chatgpt_account_signin"
        resp = await http.delete(f"{FLOW}/{flow_id}")
        assert resp.status == 200
        await hass.async_block_till_done()
    cancel.assert_called_once_with("L1")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
