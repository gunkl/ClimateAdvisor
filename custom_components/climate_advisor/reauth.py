"""Ask the owner for a new credential when one is rejected (Issue #1643, quality scale Silver reauthentication-flow).

Two credentials can be revoked or expire while Climate Advisor runs: the built-in Claude API key (install-wide,
the same value in every zone's entry, ``ai_settings_sync``) and a zone's GitHub token (report submission). Neither
is used while a zone sets up, so a rejection is never a setup failure (``ConfigEntryAuthFailed`` would be raised
from setup or a coordinator refresh, neither of which touches them). Instead the place that sees the rejection calls
:func:`async_request_reauth`, which starts Home Assistant's reauthentication flow on the zone's entry: the owner
gets a "re-authentication required" card, enters the new value, it is tested, saved and the zone reloads
(``config_flow.ClimateAdvisorConfigFlow.async_step_reauth``). Nothing is stored for this: the credential being
asked for travels in the flow's start data only.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_AI_API_KEY, CONF_GITHUB_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Key of the flow's start data naming the rejected credential (never stored in the entry).
REAUTH_CREDENTIAL = "reauth_credential"

# The reauth form step for each credential.
REAUTH_STEPS: dict[str, str] = {
    CONF_AI_API_KEY: "reauth_ai_key",
    CONF_GITHUB_TOKEN: "reauth_github_token",
}


@callback
def async_request_reauth(hass: HomeAssistant, entry: ConfigEntry | None, credential: str) -> bool:
    """Start the reauth flow for ``credential`` on ``entry``. Returns True when a new flow was started.

    Repeated rejections while the card is already shown start nothing (Home Assistant runs one reauth flow per
    entry at a time).
    """
    if entry is None or credential not in REAUTH_STEPS:
        return False
    if any(entry.async_get_active_flows(hass, {SOURCE_REAUTH})):
        _LOGGER.debug("Credential rejected; reauthentication already pending: credential=%s", credential)
        return False
    _LOGGER.warning(
        "Credential rejected — asking the owner for a new one: credential=%s zone=%s", credential, entry.title
    )
    entry.async_start_reauth(hass, data={REAUTH_CREDENTIAL: credential})
    return True


@callback
def async_request_ai_key_reauth(hass: HomeAssistant) -> bool:
    """The built-in Claude key was rejected. It is install-wide, so one card on the first zone asks for it."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return async_request_reauth(hass, entries[0] if entries else None, CONF_AI_API_KEY)


@callback
def async_dismiss_reauth(hass: HomeAssistant, entries: list[ConfigEntry], credential: str) -> int:
    """The owner saved a new ``credential`` in the zone settings instead: close its pending reauth card(s)."""
    step = REAUTH_STEPS.get(credential)
    closed = 0
    for entry in entries:
        for flow in entry.async_get_active_flows(hass, {SOURCE_REAUTH}):
            if flow.get("step_id") == step:
                hass.config_entries.flow.async_abort(flow["flow_id"])
                closed += 1
    if closed:
        _LOGGER.info("Reauthentication closed — credential saved in settings: credential=%s", credential)
    return closed
