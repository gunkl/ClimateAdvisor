"""Issue #1474: the bedtime setback records into the coordinator's live daily record.

Occupant view: the Analysis tab's "Night setback" row always read "none", because every bedtime write (applied or
skipped) went to the engine's own ``_today_record``, which production never set (since #151). The tests of those
writes assigned the record by hand, so nothing noticed. The engine now reads the coordinator's record live; these pin
the production wiring itself, not a hand-assigned record.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import OCCUPANCY_AWAY

from .test_bedtime_setback import _make_engine, _make_today_record
from .test_learning_toggle import _BASE_CONFIG


def _coordinator(tmp_path: Path):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(return_value=None)
    return ClimateAdvisorCoordinator(hass, dict(_BASE_CONFIG))


def test_production_engine_reads_the_coordinators_record(tmp_path: Path):
    coord = _coordinator(tmp_path)
    assert coord.automation_engine._today_record is None  # no record yet: the engine sees none
    first = _make_today_record()
    coord._today_record = first
    assert coord.automation_engine._today_record is first
    second = _make_today_record()  # a new day replaces the coordinator's record
    coord._today_record = second
    assert coord.automation_engine._today_record is second


def test_bedtime_skip_lands_in_the_coordinators_record(tmp_path: Path):
    """End to end through the production wiring: the skip reason is on the record the coordinator saves."""
    coord = _coordinator(tmp_path)
    coord._today_record = _make_today_record()
    engine = coord.automation_engine
    engine._emit_event_callback = lambda *_: None
    engine.set_occupancy_mode(OCCUPANCY_AWAY)
    asyncio.run(engine.handle_bedtime())
    assert coord._today_record.setback_skipped_reason == "occupancy"


def test_bare_engine_keeps_its_own_record():
    """Tests and headless tools that assign the record by hand still work (no provider)."""
    engine = _make_engine()
    assert engine._today_record is None
    rec = _make_today_record()
    engine._today_record = rec
    assert engine._today_record is rec
    bare = object.__new__(AutomationEngine)
    assert bare._today_record is None
