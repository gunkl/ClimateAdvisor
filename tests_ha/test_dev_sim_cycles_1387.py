"""Real-HA test for Issue #1387: the dev thermostat sim heats (cools) only while it says it does, in short cycles.

Developer story (no occupant impact: the sim is dev tooling): the simulated zones kept ``hvac_action: heating`` for
hours with the room pinned at the setpoint, because the sim applied heating power only BELOW the setpoint while its
compressor stopped only once the room passed target+1.5. Anything that learns from ``hvac_action`` on those zones
learned a heater ~3x weaker than configured, and the cycles looked nothing like a real thermostat's (the owner's
runs ~8 min; the sim's ran 4-6 h).

The real sim entity runs its own tick on the real HA event loop; the assertions read its compressor state each tick.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house


def _house(mode: str, sim_config: dict | None = None) -> dict:
    heat = mode == "heat"
    return {
        "contacts": [],
        "startup": "settled",
        "profile": "closed_loop",
        "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC) if heat else datetime(2026, 7, 15, 18, 0, tzinfo=UTC),
        "initial_indoor_f": 70.0 if heat else 76.0,
        "outdoor_f": 50.0 if heat else 92.0,
        # flat forecast: Climate Advisor's outdoor reading is the sim's own outdoor air (Issue #1512 reopened)
        "high_f": 50.0 if heat else 92.0,
        "low_f": 50.0 if heat else 92.0,
        "initial_hvac_mode": mode,
        "sim_config": {"min_run_seconds": 120, "min_off_seconds": 120, **(sim_config or {})},
    }


async def _cycles(house, hours: int) -> tuple[list[dict], list[str]]:
    """Run ``hours`` of 30 s ticks; return the completed runs and any tick that reported running without power."""
    sim = house.hass.data["ca_dev_thermostat_sim"][house.sim_entry.entry_id]["thermostat_entity"]
    runs: list[dict] = []
    unpowered: list[str] = []
    run: dict | None = None
    for i in range(hours * 120):
        before = sim._current_temp
        # Issue #1512 reopened: the sim reports an edge at the tick it is decided, BEFORE any of its heat, so the
        # power of a reported run shows in the tick AFTER the report (the old tick applied a tick of heat before
        # reporting, an artifact): judge each tick's movement by the action reported at its start.
        powered = sim.hvac_action
        await house.advance(seconds=30)
        after, target = sim._current_temp, sim._target_temp
        action = sim.hvac_action
        if powered in ("heating", "cooling"):
            moved = after - before if powered == "heating" else before - after
            if moved <= 0:
                unpowered.append(f"tick {i}: {powered} at {after:.2f} (target {target}) moved {moved:+.3f}")
        if action in ("heating", "cooling"):
            if run is None:
                run = {"ticks": 0, "action": action, "target": target}
            run["ticks"] += 1
            run["steady"] = run.get("steady", True) and target == run["target"]
        elif run is not None:
            run["stop_offset"] = after - target if run["action"] == "heating" else target - after
            runs.append(run)
            run = None
    return runs, unpowered


@pytest.mark.parametrize("mode", ["heat", "cool"])
async def test_default_cycles_are_short_and_stop_short_of_the_target(make_house, mode) -> None:
    """Default config: every reported run applies power, runs last minutes, and stop ~0.9 F short of the target."""
    house = await make_house(**_house(mode))
    runs, unpowered = await _cycles(house, 6)
    assert not unpowered, unpowered[:5]
    assert len(runs) >= 5, runs
    assert max(r["ticks"] for r in runs) <= 60, runs  # 30 min; the measured real thermostat ran ~8 min
    # stop point: 0.9 F short of the target (the room passes it by at most one tick of capacity)
    # (runs during which Climate Advisor moved the setpoint are left out: their stop is judged against a new target)
    stops = [round(r["stop_offset"], 2) for r in runs if r["steady"]]
    assert len(stops) >= 5 and all(-1.0 <= s <= -0.75 for s in stops), stops


async def test_symmetric_band_still_reaches_its_far_stop_point(make_house) -> None:
    """The old symmetric band (stop at target+1.5): heating must reach it and stop, not pin at the target for hours."""
    house = await make_house(**_house("heat", {"off_offset_heat_f": 1.5}))
    runs, unpowered = await _cycles(house, 6)
    assert not unpowered, unpowered[:5]
    assert len(runs) >= 3, runs
    # a 3 F climb at ~3.3 F/h net takes about an hour; #1387 pinned at the target for 4-6 h
    assert max(r["ticks"] for r in runs) <= 150, [r["ticks"] for r in runs]
    stops = [round(r["stop_offset"], 2) for r in runs if r["steady"]]
    assert stops and all(1.4 <= s <= 1.6 for s in stops), stops
