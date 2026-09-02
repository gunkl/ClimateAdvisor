"""Repairs flows for Climate Advisor."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from .const import DOMAIN


def _resolve_target_entry(hass: HomeAssistant, entry_id: str | None) -> tuple[object | None, str | None]:
    """Resolve ``(entry, entry_id)`` for a repair flow (Issue #812).

    Prefers the ``entry_id`` captured on the issue at creation time (via
    ``ir.async_create_issue(..., data={"entry_id": ...})``) over the previous
    "first config entry" fallback — this is the actual fix for the bug where
    a 2+ zone install's Repairs "Fix" button always patched/reloaded
    ``hass.config_entries.async_entries(DOMAIN)[0]`` regardless of which
    zone's issue was actually being fixed.

    Falls back to the old single-zone behavior
    (``hass.config_entries.async_entries(DOMAIN)[0]``) only when no
    ``entry_id`` was supplied — e.g. a stale issue instance raised by a
    pre-#812 version of this integration that's still open across an
    upgrade, or a caller/test that constructs the flow directly without one.
    For a single-zone install this is a no-op change: there is only ever one
    possible entry to fall back to.

    Returns ``(None, entry_id)`` when ``entry_id`` is known but no longer
    resolves to a live config entry (the zone was removed after the issue
    was raised) — callers use the still-known ``entry_id`` to clear the now
    orphaned Repairs card even though there's nothing left to fix, rather
    than crashing or leaving the card stuck open forever.
    """
    if entry_id is not None:
        return hass.config_entries.async_get_entry(entry_id), entry_id
    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0] if entries else None
    return entry, (entry.entry_id if entry else None)


class WeatherEntityRepairFlow(RepairsFlow):
    """Repair flow to select a new weather entity."""

    def __init__(self, entry_id: str | None = None) -> None:
        """Store the entry_id captured when the issue was raised (Issue #812)."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Handle the repair step."""
        if user_input is not None and "weather_entity" in user_input:
            weather_entity = user_input["weather_entity"]

            # Validate the selected entity exists
            if not self.hass.states.get(weather_entity):
                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        {
                            vol.Required("weather_entity"): selector.EntitySelector(
                                selector.EntitySelectorConfig(domain="weather")
                            ),
                        }
                    ),
                    errors={"weather_entity": "entity_not_found"},
                )

            # Update the config entry with the new weather entity — resolved
            # via the entry_id captured on the issue (Issue #812), not
            # "whichever zone happens to be first".
            entry, entry_id = _resolve_target_entry(self.hass, self._entry_id)
            if entry is not None:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "weather_entity": weather_entity}
                )
                # Defer reload to avoid tearing down the integration mid-flow
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
            if entry_id is not None:
                ir.async_delete_issue(self.hass, DOMAIN, f"weather_entity_not_found_{entry_id}")

            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("weather_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather")
                    ),
                }
            ),
        )


class ReloadNeededRepairFlow(RepairsFlow):
    """Repair flow offering to reload Climate Advisor to apply saved-but-pending settings.

    Raised by ClimateAdvisorOptionsFlow._commit_section() (Issue #573) every time
    an options-flow section is saved — the write is immediate, but the running
    coordinator/AI client only pick up the change on an actual reload. Rather
    than build a dedicated in-flow reload control (HA's menu-step UI can't
    render a real button, and a form step allows only one submit action — see
    #573's design history), this points at HA's own generic "Reload" action,
    which the confirm step here performs directly.
    """

    def __init__(self, entry_id: str | None = None) -> None:
        """Store the entry_id captured when the issue was raised (Issue #812)."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Confirm and reload."""
        if user_input is not None:
            entry, entry_id = _resolve_target_entry(self.hass, self._entry_id)
            if entry is not None:
                await self.hass.config_entries.async_reload(entry.entry_id)
            if entry_id is not None:
                ir.async_delete_issue(self.hass, DOMAIN, f"reload_needed_{entry_id}")
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create a fix flow for the given issue.

    ``data`` is the dict passed to ``ir.async_create_issue(..., data=...)`` at
    issue-creation time (HA's RepairsFlowManager reads it off the persisted
    issue and forwards it here — see ``homeassistant/components/repairs/
    issue_handler.py``: ``flow = await platform.async_create_fix_flow(hass,
    issue_id, issue.data)``). Issue #812 uses it to carry ``entry_id`` so the
    fix flow targets the exact zone the issue was raised for.

    Issue ids for these two issue types are entry-scoped as of Issue #812
    (``f"weather_entity_not_found_{entry_id}"`` /
    ``f"reload_needed_{entry_id}"``) — matched here by prefix. The bare,
    unscoped literals are also still matched for backward compatibility with
    any issue instance raised by a pre-#812 version of this integration that
    is still open across an upgrade (see the one-time migration cleanup in
    ``__init__.py::async_setup_entry``, which clears those old ids outright —
    this fallback only covers the narrow race of a user clicking "Fix" before
    that cleanup has run).
    """
    if issue_id == "weather_entity_not_found" or issue_id.startswith("weather_entity_not_found_"):
        entry_id = data.get("entry_id") if data else None
        return WeatherEntityRepairFlow(entry_id)
    if issue_id == "reload_needed" or issue_id.startswith("reload_needed_"):
        entry_id = data.get("entry_id") if data else None
        return ReloadNeededRepairFlow(entry_id)
    return ConfirmRepairFlow()
