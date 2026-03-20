"""Repairs flows for Climate Advisor."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir, selector

from .const import DOMAIN


class WeatherEntityRepairFlow(RepairsFlow):
    """Repair flow to select a new weather entity."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the repair step."""
        if user_input is not None:
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
                await self.hass.config_entries.async_reload(entry.entry_id)

            return self.async_create_entry(data={})

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


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Create a fix flow for the given issue."""
    if issue_id == "weather_entity_not_found":
        return WeatherEntityRepairFlow()
    return RepairsFlow()
