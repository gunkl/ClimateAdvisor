"""Fan devices for the house (design 4.1).

A whole-house fan (WHF) is a *separate physical device* from the thermostat blower: Climate Advisor
commands it with ``<domain>.turn_on/turn_off`` on a command entity and reads its real state back
(feedback mode) from either the same entity or a separate state entity. The two-entity shape is what
a one-way RF remote plus a power monitor looks like.

``WholeHouseFan`` registers real services for its domain (so a CA command changes the entity state,
like any device) and exposes ``press(on)``: the occupant using the wall switch or remote, which
changes the *physical* state without any service call and therefore without a Climate Advisor
Context.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall


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

    def _set(self, on: bool, context=None) -> None:
        value = "on" if on else "off"
        self.hass.states.async_set(self.command_entity, value, {"friendly_name": "Whole House Fan"}, context=context)
        if self.state_entity:
            self.hass.states.async_set(
                self.state_entity, value, {"friendly_name": "Whole House Fan power"}, context=context
            )

    async def _turn_on(self, call: ServiceCall) -> None:
        self._set(True, call.context)

    async def _turn_off(self, call: ServiceCall) -> None:
        self._set(False, call.context)

    def press(self, on: bool) -> None:
        """The occupant at the wall switch: physical state changes, no service call, no CA Context."""
        value = "on" if on else "off"
        self.hass.states.async_set(self.physical_entity, value, {"friendly_name": "Whole House Fan"})
