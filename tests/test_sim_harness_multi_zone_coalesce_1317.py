"""Issue #1317: multi-zone simulated houses end start-up coalescing after 300 s, as in production.

Occupant view: when Climate Advisor starts, each zone ignores the occupant's thermostat changes for the
first 300 s only (Issue #321), then re-applies the plan (owner decision #1235: a change made inside the
window is overwritten) and from then on treats every change as the occupant's. The multi-zone harness
(``build_headless_multi_zone`` / ``setup_entry_like_ha``) runs the real ``async_setup_entry``; the real
300 s timer fires, but with no weather forecast its refresh never reaches production's coalescing gate
(inside ``if forecast:``), so each zone kept ignoring the occupant for the whole run. Multi-zone mirror of
Issues #1291 / #1297 / #1309.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_multi_zone, setup_entry_like_ha
from tools.sim_harness.run_production import _inject_indoor_temp, _inject_thermostat_mode
from tools.sim_harness.scenario_model import build_classification_from_event

_HOT = {"type": "classification", "day_type": "hot", "today_high": 90, "hvac_mode": "cool"}


def _kinds(coord, kind: str) -> list:
    return [e for e in coord._event_log if (e.get("type") if isinstance(e, dict) else e[0]) == kind]


def test_each_zone_ends_its_window_at_300s_then_sees_the_occupant():
    logging.disable(logging.CRITICAL)
    try:
        zones, fake_hass, scheduler = build_headless_multi_zone(2)
        start = scheduler.now()
        with scheduler.installed():
            for z in zones.values():
                _inject_indoor_temp(fake_hass, z["climate_entity"], 76.0)
                z["coordinator"]._current_classification = build_classification_from_event(_HOT)
            zone_a = zones["zone_0"]
            coord_a, entity_a = zone_a["coordinator"], zone_a["climate_entity"]

            # +60 s: inside the window -> start-up noise
            scheduler.advance_to(start + timedelta(seconds=60))
            _inject_thermostat_mode(fake_hass, entity_a, "heat", dispatch=True)
            scheduler.advance_to(scheduler.now())
            assert coord_a._startup_coalesce_active, "precondition: +60 s is inside the window"

            # +360 s: every zone's window is over; zone A's +60 s change was overwritten by the plan (#1235)
            scheduler.advance_to(start + timedelta(seconds=360))
            for label, z in zones.items():
                assert not z["coordinator"]._startup_coalesce_active, (
                    f"{label}: start-up window still open at +360 s (production ends it at +300 s)"
                )
            assert fake_hass.states.get(entity_a).state == "cool", "the +60 s change was not overwritten"
            assert not coord_a.automation_engine._manual_override_active

            # +400 s: the occupant's own change -> an override
            scheduler.advance_to(start + timedelta(seconds=400))
            _inject_thermostat_mode(fake_hass, entity_a, "heat", dispatch=True)
            scheduler.advance_to(start + timedelta(seconds=1200))
            assert coord_a.automation_engine._manual_override_active, "the +400 s change was not seen as an override"
    finally:
        logging.disable(logging.NOTSET)


def test_reload_rearms_and_unload_cancels_the_window_end():
    """A reload (settings save) opens a fresh window that ends at +300 s; the old instance's end never fires."""
    logging.disable(logging.CRITICAL)
    try:
        zones, fake_hass, scheduler = build_headless_multi_zone(1)
        zone = zones["zone_0"]
        old, entry = zone["coordinator"], zone["entry"]
        with scheduler.installed():
            scheduler.advance_by(400)
            from tools.sim_harness.build_coordinator import async_unload_entry_like_ha

            run_coro(async_unload_entry_like_ha(fake_hass, entry))
            assert old._sim_restart_coalesce_cancel is None, "unload left the old instance's window end armed"
            new = setup_entry_like_ha(fake_hass, scheduler, entry)
            new._current_classification = build_classification_from_event(_HOT)
            assert new._startup_coalesce_active
            scheduler.advance_by(360)
            assert not new._startup_coalesce_active, "the reloaded zone's window is still open at +360 s"
    finally:
        logging.disable(logging.NOTSET)
