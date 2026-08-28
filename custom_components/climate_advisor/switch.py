"""Switch platform for Climate Advisor — automation enable/disable toggle.

Provides a switch entity that puts Climate Advisor into observe-only mode.
When turned off, all computation continues but thermostat and notification
service calls are skipped and logged with a [DRY RUN] prefix.

See: GitHub Issue #19

Issue #757 Phase 6 Step 8: the shadow-engine-primary switch (Issue #727/#729)
was removed along with the rest of the dual-engine shell it controlled — the
migration it was built to A/B test is complete, and there is only one
``AutomationEngine`` now.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClimateAdvisorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Climate Advisor switch entities from a config entry."""
    coordinator: ClimateAdvisorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ClimateAdvisorAutomationSwitch(coordinator, entry),
        ]
    )


class ClimateAdvisorAutomationSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable Climate Advisor automation actions.

    When OFF, Climate Advisor enters observe-only mode: classification,
    decision-making, and logging continue, but all thermostat changes
    and notifications are skipped.
    """

    _attr_icon = "mdi:robot"

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the automation switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_automation_enabled"
        self._attr_name = "Climate Advisor Automation"

    @property
    def is_on(self) -> bool:
        """Return True if automation actions are enabled."""
        return self.coordinator.automation_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable automation actions."""
        self.coordinator.set_automation_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable automation actions (enter observe-only mode)."""
        self.coordinator.set_automation_enabled(False)
        self.async_write_ha_state()
