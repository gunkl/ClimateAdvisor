"""Fan devices for the house (design 4.1).

A whole-house fan (WHF) is a *separate physical device* from the thermostat blower: Climate Advisor
commands it with ``<domain>.turn_on/turn_off`` on a command entity and reads its real state back
(feedback mode) from either the same entity or a separate state entity. The two-entity shape is what
a one-way RF remote plus a power monitor looks like.

``WholeHouseFan`` registers real services for its domain (so a CA command changes the entity state,
like any device) and exposes ``press(on)``: the occupant using the wall switch or remote, which
changes the *physical* state without any service call and therefore without a Climate Advisor
Context.

Realistic lag (Issue #1215, off by default so every existing suite is unchanged): ``enable_realistic_lag()`` makes the
dual-entity shape behave like the production QuietCool install. The command entity (the one-way RF transmitter)
flips at once and carries the caller's Context, while the separate power-detect reading follows ``on_lag_s`` /
``off_lag_s`` later (5.5 s and 15.5 s measured); a command to an entity already in that state transmits nothing (an
ESPHome template fan acts on a state CHANGE only); and ``remote_updates_command_entity`` selects the firmware: True
(firmware option 1) means a remote press also flips the command entity, False (old firmware) leaves it reading
"off" for a fan that was started from the remote.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util


class WholeHouseFan:
    """A command entity (``fan.whf``) and optionally a separate physical-state entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        command_entity: str = "fan.whf",
        state_entity: str | None = None,
        feedback: bool = True,
    ) -> None:
        self.hass = hass
        self.command_entity = command_entity
        self.state_entity = state_entity
        self.feedback = feedback
        self._domain = command_entity.split(".")[0]
        self._lag: tuple[float, float] | None = None
        self._remote_updates_command = False
        self._transmit_only_on_change = False
        self._cancel_lag = None
        self._unsub_physical = None
        self.physical_log: list[tuple[datetime, bool]] = []  # every physical on/off transition (realistic lag only)
        self.ignored_commands: list[str] = []  # commands that transmitted nothing (entity already in that state)
        hass.states.async_set(command_entity, "off", {"friendly_name": "Whole House Fan"})
        if state_entity:
            hass.states.async_set(state_entity, "off", {"friendly_name": "Whole House Fan power"})
        hass.services.async_register(self._domain, "turn_on", self._turn_on)
        hass.services.async_register(self._domain, "turn_off", self._turn_off)

    @property
    def physical_entity(self) -> str:
        return self.state_entity or self.command_entity

    @property
    def is_on(self) -> bool:
        state = self.hass.states.get(self.physical_entity)
        return state is not None and state.state == "on"

    def enable_realistic_lag(
        self,
        *,
        on_lag_s: float = 5.5,
        off_lag_s: float = 15.5,
        remote_updates_command_entity: bool = True,
        transmit_only_on_change: bool = True,
    ) -> None:
        """Opt in to the production device behaviour described in the module docstring (dual-entity shape only)."""
        assert self.state_entity, "a physical-reading lag needs a separate state entity (fan='whf_dual')"
        self._lag = (on_lag_s, off_lag_s)
        self._remote_updates_command = remote_updates_command_entity
        self._transmit_only_on_change = transmit_only_on_change
        if self._unsub_physical is None:
            self._unsub_physical = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state)

    def close(self) -> None:
        """Cancel a pending lagged reading and the transition listener (teardown of a realistic-lag fan)."""
        self._cancel_pending()
        if self._unsub_physical is not None:
            self._unsub_physical()
            self._unsub_physical = None

    @callback
    def _on_state(self, event: Event) -> None:
        if event.data["entity_id"] != self.state_entity:
            return
        new, old = event.data["new_state"], event.data["old_state"]
        if new is not None and old is not None and new.state != old.state:
            self.physical_log.append((dt_util.utcnow(), new.state == "on"))

    def _cancel_pending(self) -> None:
        if self._cancel_lag is not None:
            self._cancel_lag()
            self._cancel_lag = None

    @callback
    def _apply_physical(self, on: bool, _now) -> None:
        self._cancel_lag = None
        self.hass.states.async_set(self.state_entity, "on" if on else "off", {"friendly_name": "Whole House Fan power"})

    def _set(self, on: bool, context=None) -> None:
        value = "on" if on else "off"
        if self._lag is not None:
            self.hass.states.async_set(
                self.command_entity, value, {"friendly_name": "Whole House Fan"}, context=context
            )
            self._cancel_pending()
            self._cancel_lag = async_call_later(
                self.hass, self._lag[0] if on else self._lag[1], partial(self._apply_physical, on)
            )
            return
        self.hass.states.async_set(self.command_entity, value, {"friendly_name": "Whole House Fan"}, context=context)
        if self.state_entity:
            self.hass.states.async_set(
                self.state_entity, value, {"friendly_name": "Whole House Fan power"}, context=context
            )

    def _command_is_noop(self, on: bool) -> bool:
        current = self.hass.states.get(self.command_entity)
        return self._transmit_only_on_change and current is not None and (current.state == "on") == on

    async def _turn_on(self, call: ServiceCall) -> None:
        if self._command_is_noop(True):
            self.ignored_commands.append("turn_on")
            return
        self._set(True, call.context)

    async def _turn_off(self, call: ServiceCall) -> None:
        if self._command_is_noop(False):
            self.ignored_commands.append("turn_off")
            return
        self._set(False, call.context)

    def press(self, on: bool) -> None:
        """The occupant at the wall switch: physical state changes, no service call, no CA Context.

        With realistic lag the occupant supersedes any pending lagged reading, and (firmware option 1) the command
        entity follows the remote's real power state.
        """
        value = "on" if on else "off"
        if self._lag is not None:
            self._cancel_pending()
            if self._remote_updates_command:
                self.hass.states.async_set(self.command_entity, value, {"friendly_name": "Whole House Fan"})
        self.hass.states.async_set(self.physical_entity, value, {"friendly_name": "Whole House Fan"})
