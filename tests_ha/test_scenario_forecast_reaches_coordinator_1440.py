"""Real HA: a scenario's forecast and thermal model reach the coordinator, the single owner (Issues #1440, #1443).

Occupant view: on real Home Assistant the plan (and every decision that reads the forecast, such as the forecast-peak
veto of a window-open fan session) must be built from the same forecast Tier A uses. Before #1440 the real-HA adapter
seeded only the engine's copy; the coordinator's first weather refresh pushed its own empty list over it, so the plan
never saw the scenario forecast and a plan-timing golden could not be checked on real HA.
"""

from __future__ import annotations

import json

from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.engine_inputs import forecast_peak_next_2h
from tests_ha.realha_session import RealHASession
from tools.sim_harness import divergence
from tools.sim_harness.session import make_session

# Every golden with an hourly_forecast today also carries a v4_model, the one case the old adapter already seeded into
# the coordinator, so this test takes one of them without its v4_model: the case #1440 found (a v3-only scenario).
SCENARIO = "r6b_natvent_window_close_follows_primary__v3"


async def test_scenario_forecast_and_model_survive_the_real_refresh(hass, freezer, tmp_path) -> None:
    scenario = json.loads((divergence.GOLDEN_DIR / f"{SCENARIO}.json").read_text(encoding="utf-8"))
    scenario.pop("v4_model", None)
    assert isinstance(scenario.get("hourly_forecast"), list) and isinstance(scenario.get("thermal_model"), dict)
    session = make_session(scenario, engine="realha", hass=hass, freezer=freezer, config_dir=tmp_path)
    assert isinstance(session, RealHASession)
    try:
        await session.start()
        coordinator, engine = session.coordinator, session.engine
        await coordinator._refresh_hourly_forecast()  # the real refresh: no weather entity -> keeps the future hours
        await coordinator._async_update_data_impl()  # and a whole real cycle (thermal-model refresh included)

        now = dt_util.now()
        assert coordinator._hourly_forecast_temps, "the scenario forecast must survive the coordinator's refresh"
        future = [e for e in scenario["hourly_forecast"] if dt_util.parse_datetime(e["datetime"]) > now]
        assert future, "the scenario must have forecast hours after its start"
        # the engine holds no copy: it reads the owner's list through its inputs provider
        assert not hasattr(engine, "_hourly_forecast_temps")
        assert engine.inputs.forecast_peak_next_2h(now) == forecast_peak_next_2h(scenario["hourly_forecast"], now)
        assert engine.inputs.forecast_peak_next_2h(now) is not None
        assert engine.inputs.thermal_model() == scenario["thermal_model"]
    finally:
        await session.close()
