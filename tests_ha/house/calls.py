"""Ground truth for what was commanded (design 4.2): a listener on the service-call bus event.

Climate Advisor's own listeners read the same ``EVENT_CALL_SERVICE`` event, so this sees every command
regardless of what CA believes it sent. Each call is attributed:

* ``"occupant"``: the call carried a Context the house created for the person (wall switch, thermostat app);
* ``"ca:<n>"``: the n-th Climate Advisor *instance* (1-based, in set-up order) recorded the call's Context in
  its recent-command set, so a ghost copy of a reloaded zone shows up as ``ca:1`` after ``ca:2`` exists;
* ``"other"``: anything else (HA internals, the sim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback

_IGNORED_DOMAINS = frozenset({"homeassistant", "persistent_notification", "system_log", "logbook"})


@dataclass
class Call:
    """One service call seen on the bus."""

    at: datetime
    domain: str
    service: str
    data: dict[str, Any]
    origin: str
    context_id: str

    @property
    def entity_id(self) -> str | None:
        eid = self.data.get("entity_id")
        if isinstance(eid, (list, tuple)):
            return eid[0] if eid else None
        return eid

    def __repr__(self) -> str:  # compact, for assertion messages
        return f"{self.at:%H:%M:%S} {self.domain}.{self.service} {self.entity_id} {self.origin}"


@dataclass
class CallRecorder:
    """Collects every service call; ``instances`` are the coordinators seen so far (set-up order)."""

    hass: HomeAssistant
    calls: list[Call] = field(default_factory=list)
    occupant_context_ids: set[str] = field(default_factory=set)
    instances: list[Any] = field(default_factory=list)
    _unsub: Any = None

    def start(self) -> None:
        self._unsub = self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call)

    def reattribute(self) -> None:
        """Re-resolve calls first seen as ``other`` (e.g. commands CA sent during its own set-up, before
        the new instance was registered here)."""
        for call in self.calls:
            if call.origin == "other":
                call.origin = self._origin(call.context_id, None)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _origin(self, ctx_id: str, parent_id: str | None) -> str:
        if ctx_id in self.occupant_context_ids:
            return "occupant"
        for n, coordinator in enumerate(self.instances, start=1):
            engine = getattr(coordinator, "automation_engine", None)
            if engine is None:
                continue
            matcher = getattr(engine, "command_context_matches", None)
            if matcher is not None:
                if any(matcher(kind, ctx_id, parent_id) for kind in ("fan", "climate")):
                    return f"ca:{n}"
            elif engine.fan_command_context_matches(ctx_id, parent_id):  # older trees (revert-proof): fan kind only
                return f"ca:{n}"
        return "other"

    @callback
    def _on_call(self, event: Event) -> None:
        domain = event.data["domain"]
        if domain in _IGNORED_DOMAINS:
            return
        ctx = event.context
        self.calls.append(
            Call(
                at=_now(),
                domain=domain,
                service=event.data["service"],
                data=dict(event.data.get("service_data") or {}),
                origin=self._origin(ctx.id, ctx.parent_id),
                context_id=ctx.id,
            )
        )

    def select(self, domain: str, service: str | None = None, *, entity_id: str | None = None) -> list[Call]:
        return [
            c
            for c in self.calls
            if c.domain == domain
            and (service is None or c.service == service)
            and (entity_id is None or c.entity_id == entity_id)
        ]


def _now() -> datetime:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()
