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
            ClimateAdvisorOverrideGraceFsmAuthoritativeSwitch(coordinator, entry),
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


class ClimateAdvisorOverrideGraceFsmAuthoritativeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to make the override/grace lifecycle FSM (Issue #639) authoritative for
    ``_override_confirm_pending``/``_grace_active``/``_grace_protects_override``,
    instead of each real call site's own inline flag write (Issue #664).

    **Full authority for all 8 real call sites, shipped in one increment** — unlike
    door/window's staged partial-authority rollout, this switch does not need one.
    Investigation for Issue #664 proved every flag value these primitives compute is a
    pure function of their own call arguments (never of prior engine state), so the FSM
    and the legacy inline computation are provably equivalent at every site — there was
    no latent derivation ambiguity to discover and fix incrementally the way door/window's
    11-step rollout did. The real timer-owning primitives (``_start_grace_period_action()``/
    ``_cancel_grace_timers_action()``/etc.) are never gated by this switch — they always
    run, unconditionally, exactly like ``_start_grace_period()``/``_cancel_grace_timers()``
    are never gated by the door/window switch either; only which computation decides the
    3 flag values is what this switch genuinely governs.

    Default OFF, NOT persisted across a Home Assistant restart — same reasoning as the
    nat-vent/door-window switches' own docstrings: a restart always comes back up on the
    proven legacy path, and the owner must explicitly re-enable this each time.
    """

    _attr_icon = "mdi:state-machine"
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: ClimateAdvisorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the override/grace FSM-authoritative switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_override_grace_fsm_authoritative"
        self._attr_name = "Climate Advisor Override/Grace FSM Authoritative"

    @property
    def is_on(self) -> bool:
        """Return True if the override/grace FSM is authoritative for production decisions."""
        return self.coordinator.override_grace_fsm_authoritative

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the override/grace FSM authoritative."""
        self.coordinator.set_override_grace_fsm_authoritative(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Revert to the legacy inline override/grace flag writes."""
        self.coordinator.set_override_grace_fsm_authoritative(False)
        self.async_write_ha_state()
