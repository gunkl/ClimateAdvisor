"""Per-zone occupancy scheduler for CA Dev Thermostat Sim (Issue #898).

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Reuses the real Climate Advisor Schedule dataclass and is_schedule_active_at()
function (custom_components/climate_advisor/scheduler.py) rather than
reimplementing day-of-week/time-window matching — same DRY precedent climate.py
already sets by importing _simulate_indoor_physics from coordinator.py. This
module is read-only reuse of a pure, stateless formula: Schedule is a frozen
dataclass and is_schedule_active_at() takes a schedule + timestamp and returns a
bool with no side effects, no hass access, and no read of any TOU runtime state
(production's own _tou_phase_resolution / _tou_active_cost_resolution, or the
config["schedules"] field TOU actually uses). This module's schedule list is
entirely separate data, stored in this integration's own config entry — nothing
here can read or affect a real TOU precondition decision, and nothing about
production's TOU scheduling can affect this module.

One repurposing worth flagging explicitly: Schedule.cost_tag is a plain `str`
field production uses for "high"/"low" cost tags. is_schedule_active_at() never
reads cost_tag at all (only resolve_active_schedules() does, for TOU's own
purpose, which this module never calls) — so it's safe to store an occupancy
target ("home"/"away"/"vacation"/"guest") in that same field for this module's
own schedules without needing any change to scheduler.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .const import OCCUPANCY_STATES

try:
    # Same defensive-import shape climate.py uses for _simulate_indoor_physics
    # (Issue #898 DRY reuse) — scheduler.py transitively imports automation.py,
    # which requires the homeassistant package. On a real HA instance (where
    # this integration is meant to run) that's always present; in a bare local
    # Python environment (no `homeassistant` installed — confirmed via
    # test_sim_math.py's own documented constraint) it is not, so this import
    # is guarded rather than crashing module load entirely.
    from custom_components.climate_advisor.scheduler import Schedule, is_schedule_active_at
except ImportError as err:
    Schedule = None  # type: ignore[assignment,misc]
    is_schedule_active_at = None  # type: ignore[assignment]
    _IMPORT_ERROR = err
else:
    _IMPORT_ERROR = None


class OccupancySwitchProtocol(Protocol):
    """Minimal shape ZoneOccupancyState needs from a switch entity — avoids importing
    switch.py here (which imports homeassistant.components.switch), keeping this module
    a plain, HA-independent scheduling helper."""

    def set_occupancy_active(self, active: bool) -> None: ...


_LOGGER = logging.getLogger(__name__)


def schedule_from_dict(raw: dict) -> Schedule:
    """Build a Schedule from one stored occupancy-schedule dict entry.

    ``target`` (this module's own field name in stored config) is written into
    Schedule.cost_tag — see module docstring for why that's safe.
    """
    return Schedule(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        days=tuple(raw["days"]),
        start=raw["start"],
        end=raw["end"],
        cost_tag=raw["target"],
    )


def resolve_occupancy_target(schedules: list[Schedule], now: datetime) -> str | None:
    """Which occupancy state (if any) is scheduled active at ``now``.

    First-listed match wins on overlap — same convention production's own
    resolve_active_schedules() uses for conflicting schedules, not silently
    hidden here either. Returns None when no schedule covers ``now`` (the zone's
    occupancy switches are left as whatever they were last manually/previously
    set to — this module never forces a default state).
    """
    for schedule in schedules:
        if is_schedule_active_at(schedule, now):
            if schedule.cost_tag not in OCCUPANCY_STATES:
                _LOGGER.warning(
                    "Occupancy schedule %s has unrecognized target %r — skipping",
                    schedule.id,
                    schedule.cost_tag,
                )
                continue
            return schedule.cost_tag
    return None


@dataclass
class ZoneOccupancyState:
    """Shared per-config-entry state connecting the scheduler evaluation (driven by
    the climate entity's own physics tick — see climate.py's _async_tick(), Issue
    #898 decision: reuse the existing tick, no second timer) to the four occupancy
    switch entities it updates.

    Not a DataUpdateCoordinator — there's no polling/fetching involved, just a
    small shared object two platforms both reference via hass.data, the same
    shape production's own coordinator.py uses to let entities read shared state
    without a direct object reference to each other.
    """

    schedules: list[Schedule]
    switches: dict[str, OccupancySwitchProtocol]

    def evaluate(self, now: datetime) -> None:
        """Called once per physics tick. Determines the scheduled occupancy target
        (if any) and updates every registered switch entity to match — exactly one
        switch on at a time when a schedule is active, matching the "one target per
        schedule entry" decision (Issue #898)."""
        target = resolve_occupancy_target(self.schedules, now)
        if target is None:
            return
        for state, switch in self.switches.items():
            switch.set_occupancy_active(state == target)
