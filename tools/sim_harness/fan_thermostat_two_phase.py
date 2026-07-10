"""fan_thermostat_two_phase — two-phase synthetic scenarios for fan_thermostat_check (Step 2).

The Step-1 enumerator's single-tick scenarios never drive fan_thermostat_check()'s
comparison at all: it only fires when ``self._fan_running`` (``_fan_active`` or
``_natural_vent_active``) is already True, and that's downstream SESSION STATE —
reachable only by letting a real prior decision activate the fan, not a raw input
event like ``sensor_open``. Forcing ``self._fan_active = True`` directly on the
engine would produce the same "clean" numbers without proving the state is
reachable the way production actually reaches it — the workaround this project's
process rejects.

Instead, each of the existing t=3 assignments (unmodified — no new combinatorial
dimension) is wrapped with a real, two-phase event sequence:

  Phase 1 (preamble) — a real production entry point drives the engine into one
  of two genuinely different real states:
    "nat_vent"   — sensor_open + strongly favorable, fixed indoor/outdoor (NOT the
                   assignment's own values) so check_natural_vent_conditions()
                   activates a real nat-vent session (_natural_vent_active=True)
                   regardless of fan_mode (a nat-vent session needs no fan device).
    "fan_only"   — activate_fan_min_runtime event, calling the real, public
                   start_min_fan_runtime_cycles() entry point directly
                   (_fan_active=True, _natural_vent_active=False). Only reachable
                   when the assignment's own fan_mode is NOT disabled and a real
                   min-runtime is configured — skipped otherwise (correctly: there
                   is no device to activate, matching production).

  Phase 2 (boundary tick) — the assignment's own indoor/outdoor pair (the actual
  t-wise combination under test), applied as a temp_update tick while the
  preamble's real state is in effect — this is what finally lets
  fan_thermostat_check() evaluate Check 1/Check 2 at the intended boundary.

This is real production and reuses the existing 5809-scenario t=3 assignment set
(no new dimension, no double-counting) — it only changes how each assignment
REACHES the comparator's fan_running precondition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from tools.sim_harness.enumerator import (
    _BASE_TIME,
    _SLEEP_TIME_UTC,
    assignment_to_scenario,
    generate_t_wise_assignments,
)

_NAT_VENT_PREAMBLE_INDOOR = 76.0
_NAT_VENT_PREAMBLE_OUTDOOR = 60.0
_FAN_MIN_RUNTIME_MINUTES = 60  # >= 60 => always-on once activated (automation.py _fan_cycle_on)


def _fan_only_reachable(assignment: dict[str, Any]) -> bool:
    """A real fan device must be configured — matches production: no device, no activation."""
    return assignment.get("fan_mode") != "disabled"


def _base_start_time(assignment: dict[str, Any]):
    return _SLEEP_TIME_UTC if bool(assignment["sleep_window"]) else _BASE_TIME


def build_two_phase_scenario(assignment: dict[str, Any], *, preamble: str, name: str) -> dict[str, Any] | None:
    """Wrap one t=3 assignment's boundary tick with a real activation preamble.

    Returns None when the requested preamble isn't reachable for this assignment
    (e.g. "fan_only" with fan_mode=disabled) — the caller skips it, matching
    reality: there is nothing to activate, not a scenario-building failure.
    """
    if preamble == "fan_only" and not _fan_only_reachable(assignment):
        return None

    boundary_scenario = assignment_to_scenario(assignment, name=name)
    start_time = _base_start_time(assignment)

    preamble_events: list[dict[str, Any]] = [
        {
            "time": (start_time - timedelta(minutes=10)).isoformat(),
            "type": "classification",
            "day_type": "mild",
            "hvac_mode": "off",
            "windows_recommended": False,
        }
    ]

    if preamble == "nat_vent":
        preamble_events.append(
            {
                "time": (start_time - timedelta(minutes=9)).isoformat(),
                "type": "temp_update",
                "indoor_f": _NAT_VENT_PREAMBLE_INDOOR,
                "outdoor_f": _NAT_VENT_PREAMBLE_OUTDOOR,
            }
        )
        preamble_events.append(
            {
                "time": (start_time - timedelta(minutes=8)).isoformat(),
                "type": "sensor_open",
                "entity": "binary_sensor.two_phase_preamble",
            }
        )
    elif preamble == "fan_only":
        boundary_scenario["config"]["fan_min_runtime_per_hour"] = _FAN_MIN_RUNTIME_MINUTES
        preamble_events.append(
            {
                "time": (start_time - timedelta(minutes=8)).isoformat(),
                "type": "activate_fan_min_runtime",
            }
        )
    else:
        raise ValueError(f"unknown preamble: {preamble!r}")

    boundary_scenario["events"] = preamble_events + boundary_scenario["events"]
    boundary_scenario["name"] = name
    boundary_scenario["description"] = f"Two-phase ({preamble} preamble): {boundary_scenario['description']}"
    return boundary_scenario


@dataclass
class TwoPhaseScenario:
    name: str
    preamble: str
    assignment: dict[str, Any]
    scenario: dict[str, Any]


def build_two_phase_scenarios(t: int = 3, limit: int | None = None) -> list[TwoPhaseScenario]:
    """Build both preamble variants for each t-wise assignment (skipping unreachable ones)."""
    assignments = generate_t_wise_assignments(t=t)
    if limit is not None:
        assignments = assignments[:limit]

    out: list[TwoPhaseScenario] = []
    for i, assignment in enumerate(assignments):
        for preamble in ("nat_vent", "fan_only"):
            name = f"two_phase_{preamble}_{i:05d}"
            scen = build_two_phase_scenario(assignment, preamble=preamble, name=name)
            if scen is not None:
                out.append(TwoPhaseScenario(name=name, preamble=preamble, assignment=assignment, scenario=scen))
    return out
