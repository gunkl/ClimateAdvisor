"""Repairs flows for Climate Advisor."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from .const import DOMAIN


class WeatherEntityRepairFlow(RepairsFlow):
    """Repair flow to select a new weather entity."""

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

            # Update the config entry with the new weather entity
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if entries:
                entry = entries[0]
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "weather_entity": weather_entity}
                )
                ir.async_delete_issue(self.hass, DOMAIN, "weather_entity_not_found")
                # Defer reload to avoid tearing down the integration mid-flow
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))

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

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        """Confirm and reload."""
        if user_input is not None:
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if entries:
                await self.hass.config_entries.async_reload(entries[0].entry_id)
            ir.async_delete_issue(self.hass, DOMAIN, "reload_needed")
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create a fix flow for the given issue."""
    if issue_id == "weather_entity_not_found":
        return WeatherEntityRepairFlow()
    if issue_id == "reload_needed":
        return ReloadNeededRepairFlow()
    return ConfirmRepairFlow()
