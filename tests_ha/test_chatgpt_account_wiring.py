"""Account-manager wiring in setup / unload / removal against real Home Assistant."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.climate_advisor import chatgpt_account
from custom_components.climate_advisor.const import CHATGPT_ACCOUNT_HASS_KEY, DOMAIN
from tests_ha.test_unload_cleanup import _setup_zone


@pytest.mark.asyncio
async def test_manager_lifecycle(hass: HomeAssistant, tmp_path) -> None:
    entry = await _setup_zone(hass, tmp_path)
    manager = hass.data[CHATGPT_ACCOUNT_HASS_KEY]
    assert CHATGPT_ACCOUNT_HASS_KEY != DOMAIN
    assert all(v is not manager for v in hass.data[DOMAIN].values())  # DOMAIN holds only coordinators
    assert manager.state == chatgpt_account.STATE_DISCONNECTED

    # Unload (also what reload does) leaves the manager and its credentials alone.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.data[CHATGPT_ACCOUNT_HASS_KEY] is manager

    # Removing the final entry deletes the account; a later setup builds a fresh manager.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert CHATGPT_ACCOUNT_HASS_KEY not in hass.data
