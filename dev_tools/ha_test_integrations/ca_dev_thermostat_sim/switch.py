"""Occupancy toggle switches for CA Dev Thermostat Sim (Issue #898).

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Three plain switch entities per zone (Home/Vacation/Guest) — one per production
occupancy toggle field (CONF_HOME_TOGGLE/CONF_VACATION_TOGGLE/CONF_GUEST_TOGGLE,
climate_advisor/const.py). There is deliberately no separate "Away" switch:
production has no CONF_AWAY_TOGGLE — it derives Away from the Home switch being
OFF (occupancy_priority.py's guest > vacation > home/away chain). An earlier
version of this file created a fourth "Away" entity that production could never
be pointed at and that could disagree with the Home switch's state; fixed to
match production's real three-toggle model exactly (bug found live 2026-09-16).

climate_advisor's own occupancy config reads whatever entity_id it's pointed at
via _is_toggle_on() (coordinator.py), which only checks `state.state == "on"`. A
standard HA switch entity satisfies that with zero climate_advisor changes —
confirmed by reading _is_toggle_on() directly, not assumed.

These switches can always be toggled manually (e.g. from a dashboard) in addition to
being driven by the occupancy scheduler (see occupancy_schedule.py) — the scheduler
only writes a new state when a schedule actually covers "now"; it never forces all
three off when no schedule applies, so a manual toggle in between scheduled windows
sticks until the next schedule transition.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SWITCH_OCCUPANCY_STATES
from .occupancy_schedule import ZoneOccupancyState

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the three occupancy switch entities for this zone (Home/Vacation/Guest —
    see module docstring for why there is no fourth "Away" entity)."""
    switches = [OccupancySwitch(entry, state) for state in SWITCH_OCCUPANCY_STATES]
    async_add_entities(switches)

    zone_data = hass.data[DOMAIN][entry.entry_id]
    occupancy_state: ZoneOccupancyState = zone_data["occupancy_state"]
    for switch in switches:
        occupancy_state.switches[switch.occupancy_state] = switch


class OccupancySwitch(RestoreEntity, SwitchEntity):
    """One occupancy state's on/off switch (e.g. "Vacation") for a simulated zone."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, occupancy_state: str) -> None:
        self._entry = entry
        self.occupancy_state = occupancy_state
        self._attr_unique_id = f"{entry.entry_id}_occupancy_{occupancy_state}"
        self._attr_name = f"Occupancy {occupancy_state.capitalize()}"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"

    @property
    def is_on(self) -> bool:
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Manual turn-on — the scheduler (see occupancy_schedule.py) does not
        exclusively own this switch; a manual toggle sticks until the next
        schedule transition, matching how a real presence-detection setup can
        be manually overridden between automatic updates."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    def set_occupancy_active(self, active: bool) -> None:
        """Called by ZoneOccupancyState.evaluate() on each physics tick — see
        occupancy_schedule.py. Only writes state (and logs) on an actual change,
        matching this project's logging convention of not spamming unchanged state."""
        if self._attr_is_on == active:
            return
        self._attr_is_on = active
        _LOGGER.info(
            "CA Dev Thermostat Sim %s: occupancy schedule set %s",
            self.entity_id,
            "on" if active else "off",
        )
        self.async_write_ha_state()
