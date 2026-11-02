"""Real-HA tests for Issue #1208: coming home on a warm or mild day restores comfort on arrival.

Occupant story: you come home on a warm (or mild) afternoon. The push says "Restoring comfort temperature" but the
thermostat keeps the 80 F away setback until the next half-hourly refresh, because the classifier labels warm and
mild days "off" and the arrival restore keyed on that label instead of the comfort band Climate Advisor arms on
those days. Hot and cold days (labels "cool"/"heat") were never affected and are the controls.

Also pinned here (same label-keyed no-op, same fix): the door closing after a window pause, and the nat-vent exit.
The Rule 3 / Rule 2 controls prove the fix writes nothing while the thermostat is legitimately Off.

Runs in whole_house_fan and hvac_fan houses.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]

# day -> (forecast high, low, indoor_f at arrival, expected family, expected comfort edge)
_DAYS = {
    "warm": (80.0, 62.0, 73.0, "cool"),
    "mild": (68.0, 50.0, 69.0, "heat"),
    "hot": (95.0, 70.0, 76.0, "cool"),
    "cold": (40.0, 25.0, 66.0, "heat"),
}
_OFF_DAYS = ["warm", "mild"]


def _house_args(day: str, fan: str, **extra):
    high, low, indoor, _ = _DAYS[day]
    return {
        "fan": fan,
        "contacts": ["window"],
        "profile": "scripted",
        "startup": "settled",
        "start": datetime(2026, 4, 5, 9, 0, tzinfo=UTC),
        "initial_indoor_f": indoor,
        "outdoor_f": (high + low) / 2,
        "high_f": high,
        "low_f": low,
        "initial_hvac_mode": "off",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 60, **extra},
    }


def _ca_writes(house, since: int = 0):
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service in ("set_hvac_mode", "set_temperature")
    ]


async def _away_then_ready_to_return(make_house, day: str, fan: str, **extra):
    house = await make_house(**_house_args(day, fan, **extra))
    await house.advance(minutes=40)
    engine = house.coordinator.automation_engine
    await engine.handle_occupancy_away()
    await house.advance(minutes=60)
    return house, engine


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", [*_OFF_DAYS, "hot", "cold"])
async def test_arrival_home_restores_the_comfort_edge_at_once(make_house, day, fan) -> None:
    expected_mode = _DAYS[day][3]
    house, engine = await _away_then_ready_to_return(make_house, day, fan)
    comfort = float(engine.config["comfort_cool" if expected_mode == "cool" else "comfort_heat"])
    setback = float(engine.config["setback_cool" if expected_mode == "cool" else "setback_heat"])
    assert house.thermostat.setpoint_f == pytest.approx(setback, abs=0.6), "precondition: the away setback is held"
    mark = len(house.calls)
    await engine.handle_occupancy_home()
    await house.advance(seconds=30)  # well inside the 30-minute classification cycle
    assert house.thermostat.mode == expected_mode, f"{day}/{fan}: {house.thermostat.mode}"
    assert house.thermostat.setpoint_f == pytest.approx(comfort, abs=0.6), (
        f"{day}/{fan}: the occupant is home and the thermostat still holds {house.thermostat.setpoint_f}"
    )
    assert _ca_writes(house, mark), "arrival wrote nothing"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
async def test_arrival_with_a_window_open_past_its_debounce_stays_off(make_house, day, fan) -> None:
    """Rule 3: the window pause keeps the thermostat Off; the arrival restore waits for the window to close."""
    house, engine = await _away_then_ready_to_return(make_house, day, fan)
    await house.open("window")
    await house.advance(seconds=60 + 120)
    if house.thermostat.mode != "off":
        pytest.skip("this house armed ventilation instead of a pause; covered by the nat-vent controls")
    mark = len(house.calls)
    await engine.handle_occupancy_home()
    await house.advance(seconds=60)
    armed = [c for c in _ca_writes(house, mark) if c.data.get("hvac_mode") in ("heat", "cool")]
    assert not armed, f"arrival armed heat/cool through the open window: {[c.data for c in armed]}"
    assert house.thermostat.mode == "off"


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("day", _OFF_DAYS)
async def test_window_closing_after_an_arrival_during_a_pause_writes_the_comfort_edge(make_house, day, fan) -> None:
    """Mirror: you are away, a window pauses HVAC, you arrive home (the restore waits for the window), and the window
    closes. The door-close restore also keyed on the label and wrote nothing on warm/mild days, so the away setback
    stayed until the next half-hourly refresh."""
    expected_mode = _DAYS[day][3]
    house, engine = await _away_then_ready_to_return(make_house, day, fan)
    comfort = float(engine.config["comfort_cool" if expected_mode == "cool" else "comfort_heat"])
    await house.open("window")
    await house.advance(seconds=60 + 120)
    assert engine._paused_by_door and not engine._natural_vent_active, "precondition: HVAC paused for the window"
    assert house.thermostat.mode == "off"
    await engine.handle_occupancy_home()
    await house.advance(seconds=60)
    assert engine._occupancy_restore_deferred_pending, "precondition: the arrival restore waits for the window"
    mark = len(house.calls)
    await house.close_contact("window")
    await house.advance(minutes=2)
    assert house.thermostat.mode == expected_mode, f"{day}/{fan}: {house.thermostat.mode}"
    assert house.thermostat.setpoint_f == pytest.approx(comfort, abs=0.6), (
        f"{day}/{fan}: window closed, occupant home, thermostat still holds {house.thermostat.setpoint_f}"
    )
    assert [c for c in _ca_writes(house, mark) if c.service == "set_temperature"], "the setpoint was not written"
