"""Real-HA test for Issue #1258: no comfort incident while the room is only inside the thermostat's own swing.

Occupant story: the thermostat is in heat and, by its own configuration, lets the room sit up to its learned heat swing
(1.4 F here) below the setpoint before it starts the equipment. The Activity Log must stay quiet about that. A room
further under than the swing is a real shortfall and still raises the incident, as does a cold room with the
thermostat Off.

The REAL coordinator incident scan runs against the dev thermostat sim, reading the real thermostat state.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "closed_loop",
    "start": datetime(2026, 1, 15, 14, 0, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 50.0,
    "high_f": 55.0,
    "low_f": 45.0,
    "initial_hvac_mode": "heat",
    "sim_config": {"deadband_heat_f": 1.4, "off_offset_heat_f": -0.9, "min_run_seconds": 120, "min_off_seconds": 120},
}


def _incidents(coord) -> list[dict]:
    return [
        e
        for e in coord._event_log
        if e.get("type") == "incident_detected"
        and e.get("incident_class") in ("comfort_undertemp", "comfort_violation")
    ]


async def _scan_at(house, indoor_f: float) -> list[dict]:
    coord = house.coordinator
    coord._event_log[:] = [e for e in coord._event_log if e.get("type") != "incident_detected"]
    house.set_indoor(indoor_f)
    coord._get_indoor_temp = lambda: indoor_f  # the scan's fresh indoor reading, pinned to the value under test
    coord._detect_and_emit_incidents()
    return _incidents(coord)


def _learn_swing(house, heat_f: float) -> None:
    state = house.coordinator.learning._state
    state.thermal_model_cache = {**(state.thermal_model_cache or {}), "swing_heat_f": heat_f}


async def test_room_inside_the_heat_swing_raises_no_incident(make_house) -> None:
    house = await make_house(**_HOUSE)
    floor = float(house.coordinator._resolve_active_comfort_band()[0])
    _learn_swing(house, 1.4)
    assert house.hass.states.get(house.coordinator.automation_engine.climate_entity).state == "heat"
    assert await _scan_at(house, floor - 1.0) == []


async def test_room_beyond_the_heat_swing_raises_the_incident_with_its_tolerance(make_house) -> None:
    house = await make_house(**_HOUSE)
    floor = float(house.coordinator._resolve_active_comfort_band()[0])
    _learn_swing(house, 1.4)
    inc = await _scan_at(house, floor - 1.7)
    assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]
    assert inc[0]["swing_tolerance_f"] == pytest.approx(1.4)
    assert inc[0]["swing_tolerance_source"] == "learned"
    assert inc[0]["swing_tolerance_applied"] is True
    assert inc[0]["thermostat_mode"] == "heat"


async def test_cold_room_with_the_thermostat_off_still_raises_the_incident(make_house) -> None:
    house = await make_house(**_HOUSE)
    floor = float(house.coordinator._resolve_active_comfort_band()[0])
    _learn_swing(house, 1.4)
    entity = house.coordinator.automation_engine.climate_entity
    await house.hass.services.async_call(  # the occupant switches the thermostat Off
        "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": "off"}, blocking=True
    )
    assert house.hass.states.get(entity).state == "off"
    inc = await _scan_at(house, floor - 1.0)
    assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]
    assert inc[0]["swing_tolerance_applied"] is False
