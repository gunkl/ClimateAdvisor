"""Switch platform for Climate Advisor — automation enable/disable toggle.

Provides a switch entity that puts Climate Advisor into observe-only mode.
When turned off, all computation continues but thermostat and notification
service calls are skipped and logged with a [DRY RUN] prefix.

See: GitHub Issue #19

Issue #727/#729: also provides the shadow-engine-primary switch — the single
control axis for choosing between the fixed-legacy and fixed-FSM
``AutomationEngine`` identity, persisted across a Home Assistant restart. The
3 independent per-subsystem FSM-authoritative switches from Issue #594 Phase
R / #664 were retired in #729 — each engine's FSM-vs-legacy behavior is now
fixed at construction, not independently runtime-toggleable.
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
            ClimateAdvisorShadowEnginePrimarySwitch(coordinator, entry),
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


class ClimateAdvisorShadowEnginePrimarySwitch(CoordinatorEntity, SwitchEntity):
    """Switch to promote the fixed-FSM ``AutomationEngine`` identity to be the one
    issuing real HVAC/fan commands, demoting the fixed-legacy engine to
    diagnostic-only (Issue #727, redesigned #729).

    This is the single control axis for legacy-vs-FSM behavior. Each of the two
    engine objects the coordinator builds has its FSM-authoritative flags fixed
    at construction — one is always fully legacy, one is always fully FSM (see
    ``ClimateAdvisorCoordinator.__init__``) — so choosing which one is primary
    is now the only decision this integration exposes, replacing the 3
    independent per-subsystem switches (nat-vent/door-window/override-grace,
    Issue #594 Phase R / #664) that used to let each engine's flags vary
    independently. Flipping this switch does NOT flip a flag in place —
    ``async_set_shadow_engine_primary()`` persists the choice and reloads the
    whole config entry (Issue #729), because the previous live in-process swap
    couldn't migrate in-flight timers (grace, override-confirm, setpoint-retry,
    etc.) to the newly-primary engine. A reload cleanly tears down and rebuilds
    both engines instead, using the coordinator's existing, already-tested
    startup/shutdown path.

    Default OFF (legacy primary). Persisted across a Home Assistant restart —
    holds whichever engine was primary; a restart does not silently revert to
    the legacy engine.

    KNOWN RISK: the FSM engine's decision coverage is a strict subset of
    production's — some entry points have no ``_mirror_to_shadow()`` counterpart
    at all (see the documented list in ``coordinator.py``). Promoting while that
    gap exists means the newly-primary engine may not make every decision the
    demoted engine would have. This is accepted, not blocked, for this first
    pass — logged loudly (WARNING) every time this switch changes state.
    """

    _attr_icon = "mdi:swap-horizontal-bold"
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the shadow-engine-primary switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_shadow_engine_primary"
        self._attr_name = "Climate Advisor Shadow Engine Primary"

    @property
    def is_on(self) -> bool:
        """Return True if the shadow engine is currently primary (issuing real commands)."""
        return self.coordinator.shadow_engine_primary

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Promote the shadow engine to primary."""
        await self.coordinator.async_set_shadow_engine_primary(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Demote the shadow engine back to diagnostic-only."""
        await self.coordinator.async_set_shadow_engine_primary(False)
        self.async_write_ha_state()
