"""Real HA: a scenario's ``humidity`` / ``outdoor_humidity`` reach the coordinator's dew-point facts (Issue #1690).

Occupant view: the humid-night protections of #1507 will be checked on real Home Assistant with the same scenarios
Tier A runs, so the real-HA adapter must feed humidity the way FakeHass does: the thermostat's and the outdoor
sensor's own humidity sensors report, and the coordinator pairs each with its device's temperature.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.feels_like import dew_point_f
from tests_ha.realha_session import RealHASession
from tools.sim_harness import divergence
from tools.sim_harness.session import make_session

SCENARIO = "cancel_override_then_resume"


async def test_scenario_humidity_reaches_the_dew_point_facts(hass, freezer, tmp_path) -> None:
    scenario = json.loads((divergence.GOLDEN_DIR / f"{SCENARIO}.json").read_text(encoding="utf-8"))
    scenario["events"] = []
    session = make_session(scenario, engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    try:
        await session.start()
        coordinator = session.coordinator
        now = session.now()
        await session.feed({"time": now.isoformat(), "type": "temp_update", "indoor_f": 76.0, "outdoor_f": 70.0})
        assert coordinator.engine_facts(dt_util.now()).indoor_dew_point_f is None  # no humidity yet: unknown
        await session.feed(
            {
                "time": now.isoformat(),
                "type": "temp_update",
                "indoor_f": 76.0,
                "outdoor_f": 70.0,
                "humidity": 60.0,
                "outdoor_humidity": 90.0,
            }
        )
        facts = coordinator.engine_facts(dt_util.now())
        indoor = coordinator.hass.states.get(coordinator.config["climate_entity"]).attributes["current_temperature"]
        assert float(indoor) == pytest.approx(76.0, abs=0.05)  # the reading the event set
        assert facts.indoor_dew_point_f == pytest.approx(dew_point_f(76.0, 60.0), abs=0.1)
        assert facts.outdoor_dew_point_f == pytest.approx(dew_point_f(70.0, 90.0))
    finally:
        await session.close()
