"""session - the ONE runner seam (Issue #1127 Increment 5 / #1128 I2).

A ``Session`` drives one scenario against one world: ``feed`` dispatches a scenario event and lets
the world settle, ``advance_to`` moves its clock (firing whatever the world has scheduled),
``thermostat`` is a read-only view of the thermostat, and ``finish`` returns the standard
``RunResult`` that ``outcomes.py`` judges.  The FakeHass adapter (``run_production.FakeHassSession``)
is the only implementation today; the real-Home-Assistant adapter implements the same protocol.

This module has no Home Assistant imports and installs no stubs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tools.sim_harness.scenario_model import (
    ProductionRunResult,
    RunResult,
    parse_event_time,
    sorted_events,
)

__all__ = [
    "ProductionRunResult",
    "RunResult",
    "Session",
    "ThermostatView",
    "drive_scenario",
    "make_session",
]


@runtime_checkable
class ThermostatView(Protocol):
    """Read-only view of the (simulated) thermostat."""

    @property
    def mode(self) -> str | None: ...
    @property
    def setpoint_f(self) -> float | None: ...
    @property
    def action(self) -> str | None: ...
    @property
    def indoor_f(self) -> float | None: ...


@runtime_checkable
class Session(Protocol):
    def feed(self, event: dict) -> None:
        """Dispatch one scenario event, then settle the world."""

    def advance_to(self, ts: datetime) -> None:
        """Move the clock to ``ts``, firing every timer that comes due on the way."""

    def now(self) -> datetime: ...

    def thermostat(self) -> ThermostatView: ...

    def finish(self) -> RunResult:
        """Snapshot final state and return the run result (closes the session)."""


def drive_scenario(session: Session, scenario: dict) -> RunResult:
    """The scenario loop shared by every adapter: advance to each event's time, then feed it.

    Event ordering, the unparseable-timestamp fallback (``session.now()``) and the
    advance-then-feed order are exactly the original ``run_production_scenario`` loop.
    """
    for event in sorted_events(scenario):
        event_dt = parse_event_time(event.get("time", ""), session.now())
        session.advance_to(event_dt)
        session.feed(event)
    return session.finish()


def make_session(
    scenario: dict,
    *,
    engine: str = "fakehass",
    probe: Any = None,
    **opts: Any,
) -> Session:
    """Build a session for ``scenario``.  ``engine`` selects the adapter.

    ``fakehass`` is the only adapter in this module's environment; the real-HA adapter lives in
    the Home Assistant test tier.  The FakeHass adapter is imported lazily because it is the one
    that installs the Home Assistant stubs.
    """
    if engine == "fakehass":
        from tools.sim_harness.run_production import FakeHassSession  # noqa: PLC0415

        return FakeHassSession(scenario, probe=probe, **opts)
    if engine == "realha":
        from tests_ha.realha_session import RealHASession  # noqa: PLC0415  (real-HA env only)

        return RealHASession(scenario, probe=probe, **opts)
    raise ValueError(f"unknown session engine: {engine!r}")
