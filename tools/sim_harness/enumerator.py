"""enumerator — boundary-focused, t-wise synthetic scenario generator (Step 1).

Per the architecture-reset plan, this is NOT a naive full-Cartesian grid (which
would explode across ~20-30 raw config/state dims) and NOT a claim of "complete
coverage over all inputs". It is exhaustive t=3 coverage (t=4 available) over the
set of MEASURED decision-boundary dimensions found by direct code reading this
session (direction gates, comfort floor/ceiling, sleep window, occupancy, fan
archetype, aggressive_savings, sensor/window-open) — the dimensions the real bug
population (#327/#392/#400/#402/#415/#417/#427/#428) actually varies on, plus
`sensor_open` (added after Step 2 found the original 9-dimension set couldn't
reach any sensor-gated decision path, including the nat-vent reactivation gate
itself — a real coverage gap, not a hypothetical one).

This dimension list is a living inventory, not a one-time snapshot: whenever a
new decision-boundary dependency is found (by direct reading, by the territory
map's per-function feature instrumentation, or — as with `sensor_open` — by a
downstream tool discovering zero calls reached its target), add it here at the
source rather than working around the gap in the consuming tool.

Each generated scenario is a short, minimal (classification + temp_update) event
sequence built from a t-wise assignment of boundary-focused values. Scenarios are
run through the SAME ``old_vs_old`` engine already validated (positive control +
51/51 goldens + ~5700 real chart_log entries) — any divergence is either a hidden
input the harness still doesn't control, or (once mutation testing is layered on
top in a later step) a real bug at that boundary.

"Boundary-focused": each numeric dimension samples AT and immediately either side
of its real threshold (not a uniform grid), because the entire bug population this
was validated against (see plan) lived exactly on such boundaries.

Honesty note: this covers dimensions confirmed by direct code reading. The
territory map's per-function feature-read instrumentation (a later, deeper
deliverable) is what will confirm or extend this dimension list for the other
decision functions not yet read line-by-line.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Boundary dimensions — grounded in real thresholds read from const.py / automation.py
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """One boundary-focused axis of variation."""

    name: str
    values: tuple[Any, ...]  # small, boundary-focused value set (not a uniform grid)
    baseline: Any  # value held when this dimension is not part of the active t-combo


# Config baseline mirrors build_engine.py's _DEFAULT_CONFIG: comfort_heat=70,
# comfort_cool=76, sleep_heat/sleep_cool derived defaults, natural_vent_delta=3.0.
_COMFORT_HEAT = 70.0
_COMFORT_COOL = 76.0
_NAT_VENT_DELTA = 3.0
_NAT_VENT_THRESHOLD = _COMFORT_COOL + _NAT_VENT_DELTA  # 79.0

DIMENSIONS: tuple[Dimension, ...] = (
    # Direction gate: outdoor - indoor, boundary at 0 (Issue #327/#428 bug class).
    Dimension("outdoor_minus_indoor", (-1.0, -0.1, 0.0, 0.1, 1.0), baseline=-3.0),
    # Comfort floor boundary (#402/#417/#427 bug class: sleep-aware floor drift).
    Dimension(
        "indoor_vs_comfort_heat",
        tuple(_COMFORT_HEAT + d for d in (-1.0, -0.1, 0.0, 0.1, 1.0)),
        baseline=_COMFORT_HEAT + 5.0,
    ),
    # Comfort ceiling boundary (#392 archetype-aware ceiling guard).
    Dimension(
        "indoor_vs_comfort_cool",
        tuple(_COMFORT_COOL + d for d in (-1.0, -0.1, 0.0, 0.1, 1.0)),
        baseline=_COMFORT_COOL - 5.0,
    ),
    # nat_vent_threshold boundary (comfort_cool + nat_vent_delta; #400/#415 dashboard-drift class).
    Dimension(
        "outdoor_vs_nat_vent_threshold",
        tuple(_NAT_VENT_THRESHOLD + d for d in (-1.0, -0.1, 0.0, 0.1, 1.0)),
        baseline=_NAT_VENT_THRESHOLD - 10.0,
    ),
    Dimension("day_type", ("hot", "warm", "mild", "cool", "cold"), baseline="mild"),
    Dimension("sleep_window", (True, False), baseline=False),
    Dimension("occupancy_mode", ("home", "away", "vacation"), baseline="home"),
    Dimension("fan_mode", ("disabled", "whole_house_fan", "hvac_fan"), baseline="disabled"),
    Dimension("aggressive_savings", (True, False), baseline=False),
    # Physical prerequisite for nat-vent reactivation (automation.py:2225-2226's
    # `_idle_open` requires a monitored sensor open). Added after Step 2 found this
    # dimension missing meant zero of the original 4735 t=3 scenarios ever reached
    # `_nat_vent_may_reactivate()`'s check_natural_vent_conditions() call site — a
    # real coverage gap in the original 9-dimension set, not a workaround.
    Dimension("sensor_open", (True, False), baseline=False),
)

_DIM_BY_NAME = {d.name: d for d in DIMENSIONS}


# ---------------------------------------------------------------------------
# t-wise combination generation
# ---------------------------------------------------------------------------


def generate_t_wise_assignments(t: int = 3, dims: tuple[Dimension, ...] = DIMENSIONS) -> list[dict[str, Any]]:
    """Exhaustive t-way coverage: every combination of ``t`` dimensions, crossed with
    every value combination for exactly those dimensions (other dims at baseline).

    This is a full combinatorial sweep per t-subset (not a minimized covering
    array) — simpler to implement correctly, and it strictly guarantees the
    property the plan asked for: any bug depending on <= t of these dimensions
    co-occurring is represented in at least one generated assignment.
    """
    assignments: list[dict[str, Any]] = []
    baseline = {d.name: d.baseline for d in dims}
    for combo in itertools.combinations(dims, t):
        for values in itertools.product(*(d.values for d in combo)):
            a = dict(baseline)
            a.update(dict(zip((d.name for d in combo), values, strict=True)))
            assignments.append(a)
    return assignments


# ---------------------------------------------------------------------------
# Scenario construction from one assignment
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)  # 12:00 UTC — outside default sleep window
_SLEEP_TIME_UTC = datetime(2026, 6, 15, 23, 30, 0, tzinfo=UTC)  # inside a 22:30-06:30 sleep window


def assignment_to_scenario(assignment: dict[str, Any], *, name: str) -> dict[str, Any]:
    """Build a minimal (classification + temp_update) scenario from one t-wise assignment."""
    outdoor_minus_indoor = assignment["outdoor_minus_indoor"]
    indoor_vs_heat = assignment["indoor_vs_comfort_heat"]
    indoor_vs_cool = assignment["indoor_vs_comfort_cool"]
    outdoor_vs_threshold = assignment["outdoor_vs_nat_vent_threshold"]

    # Resolve a single indoor/outdoor pair that best respects the active boundary
    # dims. When both floor and ceiling dims are simultaneously "active" (non-baseline)
    # they could conflict; the floor dimension wins ties (arbitrary, documented) since
    # the historical bug population weighted more heavily there (4 of 8 t=3/4 bugs).
    if indoor_vs_heat != _DIM_BY_NAME["indoor_vs_comfort_heat"].baseline:
        indoor = indoor_vs_heat
    elif indoor_vs_cool != _DIM_BY_NAME["indoor_vs_comfort_cool"].baseline:
        indoor = indoor_vs_cool
    else:
        indoor = 73.0  # neutral mid-comfort-band default

    if outdoor_vs_threshold != _DIM_BY_NAME["outdoor_vs_nat_vent_threshold"].baseline:
        outdoor = outdoor_vs_threshold
    else:
        outdoor = indoor - outdoor_minus_indoor  # outdoor_minus_indoor = outdoor - indoor

    sleep_window = bool(assignment["sleep_window"])
    start_time = _SLEEP_TIME_UTC if sleep_window else _BASE_TIME
    classification_time = start_time
    temp_time = start_time + timedelta(minutes=1)

    day_type = assignment["day_type"]
    hvac_mode_by_day = {"hot": "cool", "warm": "cool", "mild": "off", "cool": "heat", "cold": "heat"}

    config: dict[str, Any] = {
        "comfort_heat": _COMFORT_HEAT,
        "comfort_cool": _COMFORT_COOL,
        "natural_vent_delta": _NAT_VENT_DELTA,
        "fan_mode": assignment["fan_mode"],
        "aggressive_savings": bool(assignment["aggressive_savings"]),
        # Sleep window bounding the classification/temp_update timestamps above.
        "sleep_time": "22:30",
        "wake_time": "06:30",
    }

    events: list[dict[str, Any]] = [
        {
            "type": "classification",
            "time": classification_time.isoformat(),
            "day_type": day_type,
            "hvac_mode": hvac_mode_by_day[day_type],
            "windows_recommended": False,
        }
    ]

    occupancy_mode = assignment["occupancy_mode"]
    if occupancy_mode != "home":
        events.append(
            {
                "type": f"occupancy_{occupancy_mode}",
                "time": (classification_time + timedelta(seconds=30)).isoformat(),
            }
        )

    if bool(assignment.get("sensor_open", False)):
        events.append(
            {
                "type": "sensor_open",
                "time": temp_time.isoformat(),
                "entity": "binary_sensor.synthetic_probe",
            }
        )

    events.append(
        {
            "type": "temp_update",
            "time": temp_time.isoformat(),
            "indoor_f": round(indoor, 2),
            "outdoor_f": round(outdoor, 2),
        }
    )

    return {
        "name": name,
        "description": f"Synthetic t-wise boundary scenario: {assignment}",
        "config": config,
        "events": events,
    }


@dataclass
class EnumeratedScenario:
    name: str
    assignment: dict[str, Any]
    scenario: dict[str, Any]


def build_enumerated_scenarios(t: int = 3, limit: int | None = None, offset: int = 0) -> list[EnumeratedScenario]:
    """Build the full (or offset/capped) set of t-wise boundary scenarios.

    ``offset``/``limit`` slice the assignment list — useful for manual partial
    runs. Batching is no longer required to avoid a stall: the root cause
    (thousands of fresh ``asyncio.run()``-created event loops exhausting a
    Windows kernel resource) is fixed at the source in
    ``tools/sim_harness/_loop.py`` — see the Step-2 status report.
    """
    assignments = generate_t_wise_assignments(t=t)
    end = offset + limit if limit is not None else None
    assignments = assignments[offset:end]
    out = []
    for i, a in enumerate(assignments, start=offset):
        name = f"t{t}_boundary_{i:05d}"
        out.append(EnumeratedScenario(name=name, assignment=a, scenario=assignment_to_scenario(a, name=name)))
    return out


@dataclass
class CoverageStats:
    t: int
    total_assignments: int
    dims_considered: int = field(default=len(DIMENSIONS))

    @staticmethod
    def compute(t: int) -> CoverageStats:
        return CoverageStats(t=t, total_assignments=len(generate_t_wise_assignments(t=t)))
