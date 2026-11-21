"""Issue #1643: who gets a "re-authentication required" card, and which card a saved credential closes.

Occupant view: a rejected Claude key or GitHub token shows one card asking for the new value. A rejection with no
zone to ask on, or for a credential that has no card, shows nothing (never a broken card). When the owner saves the
new value in the zone settings instead, only that credential's card closes: saving the GitHub token must not close
a pending Claude-key card, or the owner would never be asked for the key again until the next rejection.

Unit tier: ``reauth.py`` against mocked config entries. The real flow (card, form, save, reload) is in
``tests_ha/test_reauth_reconfigure_1643_1646.py``.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor import reauth  # noqa: E402
from custom_components.climate_advisor.const import CONF_AI_API_KEY, CONF_GITHUB_TOKEN  # noqa: E402


def _entry(flows: list[dict] | None = None) -> MagicMock:
    entry = MagicMock()
    entry.title = "Living Room"
    entry.async_get_active_flows.return_value = list(flows or [])
    return entry


def test_no_zone_to_ask_on_starts_nothing() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    assert reauth.async_request_ai_key_reauth(hass) is False


def test_a_credential_without_a_reauth_step_starts_nothing() -> None:
    entry = _entry()
    assert reauth.async_request_reauth(MagicMock(), entry, "weather_entity") is False
    entry.async_start_reauth.assert_not_called()


def test_a_rejection_starts_one_flow_and_a_repeat_while_pending_starts_none() -> None:
    hass, entry = MagicMock(), _entry()
    assert reauth.async_request_reauth(hass, entry, CONF_GITHUB_TOKEN) is True
    entry.async_start_reauth.assert_called_once_with(hass, data={reauth.REAUTH_CREDENTIAL: CONF_GITHUB_TOKEN})
    entry.async_get_active_flows.return_value = [{"flow_id": "f1", "step_id": "reauth_github_token"}]
    assert reauth.async_request_reauth(hass, entry, CONF_GITHUB_TOKEN) is False
    assert entry.async_start_reauth.call_count == 1


def test_saving_one_credential_closes_only_its_own_card(caplog) -> None:
    hass = MagicMock()
    entries = [
        _entry([{"flow_id": "key", "step_id": "reauth_ai_key"}]),
        _entry([{"flow_id": "token", "step_id": "reauth_github_token"}]),
    ]
    with caplog.at_level(logging.INFO, logger=reauth.__name__):
        assert reauth.async_dismiss_reauth(hass, entries, CONF_GITHUB_TOKEN) == 1
    hass.config_entries.flow.async_abort.assert_called_once_with("token")
    assert "Reauthentication closed" in caplog.text


def test_saving_a_credential_with_no_pending_card_closes_nothing(caplog) -> None:
    hass = MagicMock()
    entries = [_entry([{"flow_id": "key", "step_id": "reauth_ai_key"}]), _entry()]
    with caplog.at_level(logging.INFO, logger=reauth.__name__):
        assert reauth.async_dismiss_reauth(hass, entries, CONF_GITHUB_TOKEN) == 0
    hass.config_entries.flow.async_abort.assert_not_called()
    assert "Reauthentication closed" not in caplog.text
