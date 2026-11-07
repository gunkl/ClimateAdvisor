"""Real-HA test for Issue #1409: an occupancy change inside a time-of-use bank window writes the new bank once.

Occupant story: on a hot afternoon the house is pre-cooling to 68 F for a 3:00 PM high-cost period when everyone
leaves. Before the fix Climate Advisor first told the thermostat the Away setback (cool 80 F, the AC stops) and then,
at the next 5-minute check, the Away pre-cool target (cool 77 F, it may restart), a short cycle. Coming home during an
Away pre-heat it wrote the comfort floor (heat 68 F) and then the home pre-heat (heat 76 F). Now the change writes the
new occupancy's bank target once.

Driven through the REAL occupancy toggle entity (the coordinator's state listener and, for leaving, its real 15-minute
away timer), so this proves the production path reaches ``ClimateAdvisorCoordinator._run_occupancy_handler``.
Explicit asserts only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_PEAK = {"id": "peak1", "name": "Afternoon Peak", "days": ["all"], "start": "15:00", "end": "19:00", "cost_tag": "high"}
_CONFIG = {
    "comfort_heat": 68.0,
    "comfort_cool": 76.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "schedules": [_PEAK],
    "default_tou_lead_minutes": 45,  # no learned rate yet: the bank window opens 14:15
    "home_toggle_entity": "input_boolean.home",
}


def _ca_setpoints(house, since: int) -> list[float]:
    out = []
    for c in house.calls[since:]:
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service == "set_temperature":
            t = c.data.get("temperature")
            if t is not None:
                out.append(round(float(t), 1))
    return out


async def test_leaving_mid_bank_writes_the_away_bank_without_the_setback_first(make_house):
    house = await make_house(
        start=datetime(2026, 7, 15, 13, 50, tzinfo=UTC),
        initial_indoor_f=76.0,
        outdoor_f=92.0,
        high_f=96.0,
        low_f=72.0,
        initial_hvac_mode="cool",
        ca_config=dict(_CONFIG),
        extra_states={"input_boolean.home": "on"},
    )
    await house.advance_to(datetime(2026, 7, 15, 14, 25, tzinfo=UTC))
    assert house.thermostat.mode == "cool"
    assert house.thermostat.setpoint_f == pytest.approx(68.0, abs=0.5), "precondition: the home bank is on"

    since = len(house.calls)
    house.hass.states.async_set("input_boolean.home", "off")  # everyone leaves (the real toggle entity)
    await house.advance(minutes=25)  # past the 15-minute away timer
    assert house.coordinator._occupancy_mode == "away"

    writes = _ca_setpoints(house, since)
    assert writes, "the Away bank was never written"
    assert 80.0 not in writes, f"the Away setback was written before the bank (short-cycle risk): {writes}"
    assert all(w < 80.0 for w in writes) and house.thermostat.setpoint_f < 80.0, writes
    assert house.coordinator.current_hold().bank is not None  # still banking, now for the empty house


async def test_coming_home_mid_bank_writes_the_home_bank_without_the_comfort_floor_first(make_house):
    house = await make_house(
        start=datetime(2026, 1, 15, 13, 50, tzinfo=UTC),
        initial_indoor_f=64.0,
        outdoor_f=30.0,
        high_f=40.0,
        low_f=25.0,
        initial_hvac_mode="heat",
        ca_config=dict(_CONFIG),
        extra_states={"input_boolean.home": "off"},
    )
    await house.advance_to(datetime(2026, 1, 15, 14, 25, tzinfo=UTC))
    assert house.coordinator._occupancy_mode == "away"
    assert house.thermostat.mode == "heat"

    since = len(house.calls)
    house.hass.states.async_set("input_boolean.home", "on")  # someone comes home (handled at once)
    await house.advance(minutes=10)
    assert house.coordinator._occupancy_mode == "home"

    writes = _ca_setpoints(house, since)
    assert writes and writes[0] == pytest.approx(76.0, abs=0.5), f"expected the home bank (heat 76 F) first: {writes}"
    assert 68.0 not in writes, f"the comfort floor was written before the home bank: {writes}"
    assert house.thermostat.setpoint_f == pytest.approx(76.0, abs=0.5)
