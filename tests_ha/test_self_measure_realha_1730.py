"""The loop-lag probe on real Home Assistant (Issue #1730): one timer for every zone, from setup to the last unload.

Occupant view: nothing visible. The probe is a self-rescheduling timer on Home Assistant's own event loop; it must not
outlive the zones that started it (HA's autouse ``verify_cleanup`` fails a test that leaves a timer behind), must not be
started once per zone, and must survive a reload.

The tier's clock is the frozen simulated one, so this checks lifecycle and that ticks arrive, not lateness (the lateness
arithmetic is covered deterministically in ``tests/test_self_measure_1730.py``).
"""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.climate_advisor import self_measure
from tests_ha.test_zone_param_1418 import _add_zone

pytestmark = pytest.mark.house


def _live_probe_timers(hass, perf) -> list:
    """The probe's scheduled timer handles still on the loop (a cancelled TimerHandle stays queued until it is due)."""
    return [
        h
        for h in hass.loop._scheduled
        if isinstance(h, asyncio.TimerHandle) and not h.cancelled() and h is perf._handle
    ]


async def test_one_probe_for_all_zones_cancelled_with_the_last_unload(make_house) -> None:
    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    first = house.ca_entry
    perf = self_measure.for_hass(hass)
    assert first.runtime_data._perf is perf and perf.running
    handle = perf._handle
    assert isinstance(handle, asyncio.TimerHandle) and not handle.cancelled()

    samples = len(perf.lag)
    await house.advance(seconds=20)
    assert len(perf.lag) > samples  # the timer really ticks on the real loop

    _sim, second = await _add_zone(hass, house, "Upstairs", 66.0, 78.0)
    assert second.runtime_data._perf is perf  # a second zone shares it: still one timer
    assert perf._refs == 2 and len(_live_probe_timers(hass, perf)) == 1

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert perf.running and perf._refs == 1  # the first zone still holds it

    assert await hass.config_entries.async_reload(first.entry_id)  # a settings save on the last zone
    await hass.async_block_till_done()
    assert first.state is ConfigEntryState.LOADED and perf.running and perf._refs == 1  # released, then taken again

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert not perf.running and perf._refs == 0
    assert perf._handle is None and _live_probe_timers(hass, perf) == []


async def test_home_assistant_stop_cancels_the_probe(make_house) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    house = await make_house(units="us", profile="scripted", initial_indoor_f=72.0)
    hass = house.hass
    perf = self_measure.for_hass(hass)
    assert perf.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not perf.running and perf._refs == 0  # HA stop never unloads entries; the stop event is the mirror path
