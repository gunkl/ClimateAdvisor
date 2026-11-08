"""Issue #1435: a scenario's occupancy_change_with_override event goes through the coordinator's occupancy entry.

Production runs every occupancy CHANGE through ``ClimateAdvisorCoordinator._run_occupancy_handler`` (Issue #1409),
which sets the engine's ``_occupancy_bank_write`` and enters ``occupancy_change()`` so a change inside an open
time-of-use bank window writes the new bank once.  The plain occupancy events took that entry since #1409; this
event still called the engine handler directly, a path production never takes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from tools.sim_harness.run_production import run_production_scenario

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tools" / "simulations" / "golden"
GOLDEN = GOLDEN_DIR / "away_occupancy_override_cleared.json"


def _record_away_handler_calls():
    """Patch the engine's away handler (class-level) to note, per call, what production's entry would have set."""
    from custom_components.climate_advisor import automation
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    calls: list[dict] = []
    entries: list[str] = []
    original_handler = automation.AutomationEngine.handle_occupancy_away
    original_entry = ClimateAdvisorCoordinator._run_occupancy_handler

    async def handle_occupancy_away(self, *args, **kwargs):
        calls.append(
            {
                "in_occupancy_change": automation._OCCUPANCY_CHANGE.get() is self,
                "bank_write_set": self._occupancy_bank_write is not None,
            }
        )
        return await original_handler(self, *args, **kwargs)

    async def _run_occupancy_handler(self, mode):
        entries.append(mode)
        return await original_entry(self, mode)

    patches = (
        patch.object(automation.AutomationEngine, "handle_occupancy_away", handle_occupancy_away),
        patch.object(ClimateAdvisorCoordinator, "_run_occupancy_handler", _run_occupancy_handler),
    )
    return patches, calls, entries


def test_coordinator_override_event_runs_through_run_occupancy_handler() -> None:
    scenario = json.loads(GOLDEN.read_text(encoding="utf-8"))
    patches, calls, entries = _record_away_handler_calls()
    with patches[0], patches[1]:
        run_production_scenario(scenario, use_coordinator=True)
    assert entries == ["away"]
    assert calls == [{"in_occupancy_change": True, "bank_write_set": True}]


def test_engine_only_override_event_still_calls_the_handler_directly() -> None:
    """No coordinator: the engine handler is the only call, outside any occupancy-change context."""
    scenario = json.loads(GOLDEN.read_text(encoding="utf-8"))
    patches, calls, entries = _record_away_handler_calls()
    with patches[0], patches[1]:
        run_production_scenario(scenario, use_coordinator=False)
    assert entries == []
    assert calls == [{"in_occupancy_change": False, "bank_write_set": False}]
