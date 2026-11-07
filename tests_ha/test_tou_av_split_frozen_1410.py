"""Real-HA run of the Issue #1410 pending golden: the Away bank target and widened edge are whole steps, frozen.

Occupant story: an empty house pre-cooling for a 4:15 PM high-cost period was told cool 77.3875 F, then 77.15 F as
the house cooled, and 82.6125 F creeping to 82.1375 F during the period as it drifted back up. Now it is told cool 77
for the whole bank and cool 83 for the whole period (decision C23). The scenario is still pending (unsigned), so the
all-goldens real-HA file does not pick it up; this file drives the same scenario through real Home Assistant and
requires every assertion to pass, with no logged exception and no timer left armed.
"""

from __future__ import annotations

import json
import logging

from tests_ha.realha_session import RealHASession, async_drive_scenario
from tools.sim_harness import divergence

NAME = "tou_away_cool_bank_frozen_for_window_1410"
PATH = divergence.REPO / "tools" / "simulations" / "pending" / f"{NAME}.json"


async def test_frozen_away_bank_on_real_ha(hass, freezer, tmp_path, caplog) -> None:
    scenario = json.loads(PATH.read_text(encoding="utf-8"))
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    session = RealHASession(scenario, hass=hass, freezer=freezer, config_dir=tmp_path)
    result = await async_drive_scenario(session, scenario)
    dump = divergence.dump_run(
        NAME, "real", scenario, result, use_coordinator=True, dispatch=session.dispatch_modes, notes=session.notes
    )
    dump["leaked_timers"] = session.leaked_timers
    failing, outcomes = divergence.failing_outcomes(dump)
    assert not failing, f"real-HA outcomes: {outcomes}"
    writes = [
        round(float(e["data"]["temperature"]), 4)
        for e in result.action_log
        if e.get("domain") == "climate" and e.get("service") == "set_temperature" and "temperature" in e.get("data", {})
    ]
    assert writes and set(writes) <= {77.0, 83.0}, f"only the frozen whole-degree values are told: {writes}"
    assert any("TOU split frozen for window" in r.getMessage() for r in caplog.records), "the freeze is logged"
