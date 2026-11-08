"""Real-HA test: a real Home Assistant restart keeps what Climate Advisor had not saved yet (Issues #1434, #1438).

Occupant story: Home Assistant restarts (an update, a deploy, a manual restart) while the furnace or the AC is
running. Before the fix, the minutes that run had lasted before the restart never reached today's HVAC runtime,
so the day's runtime, the heat/cool split and the daily cost estimate came out low. The same restart also dropped
the chart log's event rows since its last 30-minute save and up to an hour of v4's prediction record. A restart
never unloads the config entry, so ``async_shutdown`` (the only place these were saved) did not run. Only
``EVENT_HOMEASSISTANT_STOP`` fires before the process exits.

Shape: a running house, 20 more minutes of the run, then an event row and a v4 archive value that only memory holds.
STOP is fired with NO unload. To model the process exiting right after STOP, the old instance's unload is then
stripped of every write before the house restarts. The restored zone must have the pre-restart minutes in today's
record, the event row in its chart log and the v4 value in its archive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.v4_archive import slot_key

pytestmark = pytest.mark.house

_COLD_DAY = {
    "start": datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
    "outdoor_f": 30.0,
    "high_f": 36.0,
    "low_f": 22.0,
    "initial_indoor_f": 62.0,
}
_TOL_MIN = 1.0


def _no_writes_after_stop(coord) -> None:
    """The process exits after STOP: the old instance's unload must not save anything (it would mask the bug)."""
    coord._async_save_state = AsyncMock()
    coord._v4_flush_on_shutdown = AsyncMock()
    coord._chart_log.save = lambda: True


async def test_stop_without_unload_keeps_runtime_event_rows_and_v4_record(make_house) -> None:
    house = await make_house(profile="scripted", startup="settled", **_COLD_DAY)
    await house.advance(minutes=10)
    assert house.thermostat.action == "heating", f"precondition: action {house.thermostat.action}"
    await house.advance(minutes=20)
    coord = house.coordinator
    assert coord._hvac_on_since is not None and coord._hvac_session_mode == "heat", "precondition: a run in progress"
    base_before = coord._today_record.hvac_runtime_minutes
    live = coord.get_hvac_runtime_today()  # today's runtime including the run in progress
    assert live - base_before >= 5.0, f"precondition: unflushed run minutes {live - base_before:.1f}"

    marker_ts = dt_util.now().isoformat()
    # A distinctive event row (the clock is frozen, so the restored zone's own rows share this timestamp).
    coord._chart_log.append(hvac="heating", fan=False, indoor=63.37, outdoor=30.0, event="override", ts=marker_ts)
    v4_slot = slot_key(dt_util.now() + timedelta(hours=5))
    coord._v4_archive.put("_ca", v4_slot, "1h", 66.66)

    hass = house.hass
    hass.set_state(CoreState.stopping)
    try:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    finally:
        hass.set_state(CoreState.running)
    _no_writes_after_stop(coord)

    await house.restart(settle=False)
    new = house.coordinator
    assert new is not coord, "precondition: a fresh instance restored from disk"
    restored = new._today_record.hvac_runtime_minutes
    events = [
        r
        for r in new._chart_log.raw_entries()
        if r.get("ts") == marker_ts and r.get("event") == "override" and r.get("indoor") == 63.37
    ]
    v4_value = new._v4_archive.series("_ca", "1h", v4_slot, v4_slot + 1).get(v4_slot)
    failures = []
    if restored < live - _TOL_MIN:
        failures.append(f"today's runtime {restored:.1f} min lost {live - restored:.1f} min of the run (#1434)")
    if restored > live + _TOL_MIN:
        failures.append(f"today's runtime {restored:.1f} min counts {restored - live:.1f} min twice")
    if not events:
        failures.append("the chart log's event row since its last save was lost (#1438)")
    if v4_value != 66.66:
        failures.append(f"the v4 archive lost its unsaved value ({v4_value}) (#1423)")
    assert not failures, f"base={base_before:.1f} live={live:.1f} restored={restored:.1f}: {failures}"
