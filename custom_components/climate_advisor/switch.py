"""Switch platform for Climate Advisor — automation enable/disable toggle.

Provides a switch entity that puts Climate Advisor into observe-only mode.
When turned off, all computation continues but thermostat and notification
service calls are skipped and logged with a [DRY RUN] prefix.

See: GitHub Issue #19

Issue #594 Phase R, Step 4: also provides the per-lifecycle nat-vent and
door/window FSM-authoritative toggles.
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
            ClimateAdvisorNatVentFsmAuthoritativeSwitch(coordinator, entry),
            ClimateAdvisorDoorWindowFsmAuthoritativeSwitch(coordinator, entry),
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


class ClimateAdvisorNatVentFsmAuthoritativeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to make the nat-vent lifecycle FSM (Issue #633) authoritative for
    the active-session soft-start-escalation + exit-chain decision, instead of
    the legacy inline computation (Issue #594 Phase R, Step 4).

    Default OFF. Unlike the automation-enable switch above, this is NOT
    persisted across a Home Assistant restart — see
    ``ClimateAdvisorCoordinator.set_natvent_fsm_authoritative()``'s docstring:
    a restart always comes back up on the proven legacy path, and the owner
    must explicitly re-enable this each time. This is the first switch in the
    Block 5 migration capable of letting a bug in new code reach real
    HVAC/fan hardware (every prior shadow-diagnostic phase was `dry_run=True`
    by construction) — instantly reversible, but not something that should
    silently persist through an unattended restart.
    """

    _attr_icon = "mdi:state-machine"
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the nat-vent FSM-authoritative switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_natvent_fsm_authoritative"
        self._attr_name = "Climate Advisor Nat-Vent FSM Authoritative"

    @property
    def is_on(self) -> bool:
        """Return True if the nat-vent FSM is authoritative for production decisions."""
        return self.coordinator.natvent_fsm_authoritative

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the nat-vent FSM authoritative."""
        self.coordinator.set_natvent_fsm_authoritative(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Revert to the legacy inline nat-vent computation."""
        self.coordinator.set_natvent_fsm_authoritative(False)
        self.async_write_ha_state()


class ClimateAdvisorDoorWindowFsmAuthoritativeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to make the door/window lifecycle FSM (Issue #637) authoritative,
    instead of the legacy inline flag writes (Issue #594 Phase R, Step 4).

    **Partial authority** — unlike the nat-vent switch above, this one increment
    only governs 2 of the door/window lifecycle's 7 methods
    (``handle_manual_override_during_pause``, ``resume_from_pause``). The other 5
    (``handle_door_window_open``, ``handle_all_doors_windows_closed``,
    ``_re_pause_for_open_sensor``, ``_on_grace_expired``, ``_exit_nat_vent``'s
    sensor-still-open branch) stay on the legacy path regardless of this switch's
    position — each has its own documented blocker (see
    ``AutomationEngine._doorwindow_fsm_authoritative``'s docstring in
    automation.py) that must resolve before it can join a future increment.
    Flipping this switch on does NOT mean "the FSM fully controls door/window."

    Default OFF, NOT persisted across a Home Assistant restart — same reasoning
    as the nat-vent switch's own docstring: a restart always comes back up on
    the proven legacy path, and the owner must explicitly re-enable this each
    time.
    """

    _attr_icon = "mdi:state-machine"
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the door/window FSM-authoritative switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_doorwindow_fsm_authoritative"
        self._attr_name = "Climate Advisor Door/Window FSM Authoritative"

    @property
    def is_on(self) -> bool:
        """Return True if the door/window FSM is authoritative for the 2
        methods this increment covers."""
        return self.coordinator.doorwindow_fsm_authoritative

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the door/window FSM authoritative (partial scope — see class docstring)."""
        self.coordinator.set_doorwindow_fsm_authoritative(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Revert to the legacy inline door/window flag writes."""
        self.coordinator.set_doorwindow_fsm_authoritative(False)
        self.async_write_ha_state()
