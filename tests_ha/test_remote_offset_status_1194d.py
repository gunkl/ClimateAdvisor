"""Real-HA test for Issue #1194 (d): what the occupant sees on an offset night, end to end.

Occupant story: the bedroom is cold, the hallway thermostat is satisfied, and Climate Advisor has raised the hallway
setpoint to the +4 F cap. The Status card must say "Bedroom 66F target - thermostat set 70F (+4F, clamped at the +4F
cap)" instead of "70F (CA: 66F)", and the chart must keep the Target line at the bedroom's 66 F with the thermostat's
70 F as its own series -- never a sleep band that appears to have moved.

The REAL status view and the REAL chart builder run against the real coordinator / engine (dev thermostat sim, real
bedroom state). Explicit asserts only: the syrupy snapshot file is locked, no snapshot entry is added.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor.api import ClimateAdvisorStatusView
from tests_ha.test_remote_offset_gates_1194b import (
    _DEBOUNCE_S,
    _FANS,
    _PLAIN_F,
    _at_cap,
    _bedroom,
    _cold_night,
)

pytestmark = pytest.mark.house


async def _status(house) -> dict:
    request = SimpleNamespace(
        app={"hass": house.hass}, query={}, method="GET", path="/api/climate_advisor/status", remote="t"
    )
    response = await ClimateAdvisorStatusView().get(request)
    return json.loads(response.text)


@pytest.mark.parametrize("fan", _FANS)
async def test_clamped_offset_night_status_and_chart(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)
    _bedroom(house, 58.0)  # a fresh cold report while at the cap: the loop wants more and a limit stops it
    await house.advance(minutes=2)

    body = await _status(house)
    ro = body["remote_offset"]
    assert ro["state"] == "clamped" and ro["clamp_limit"] == "cap" and ro["last_decision_reason"] == "at_cap"
    assert ro["comfort_target"] == _PLAIN_F and ro["commanded"] == _PLAIN_F + 4.0
    assert ro["offset_applied"] == 4.0 and ro["offset_applied_f"] == 4.0
    assert ro["remote_reading"] == 58.0 and ro["remote_reading_age_s"] is not None
    assert ro["text"] == "Bedroom 66°F target - thermostat set 70°F (+4°F, clamped at the +4°F cap)"
    # The #402 comparison input: the thermostat reads the commanded 70, the expected value is 70 (no "(CA: 66)").
    assert body["current_setpoint"] == pytest.approx(_PLAIN_F + 4.0)
    assert ro["expected_setpoint"] == _PLAIN_F + 4.0 and body["ca_target_heat"] == _PLAIN_F

    # The chart: a row logged while the offset is in force carries the comfort target beside the commanded setpoint.
    coordinator = house.coordinator
    assert coordinator._read_chart_comfort_target() == _PLAIN_F
    coordinator._chart_log.append(
        hvac="heating",
        fan=False,
        indoor=58.0,
        outdoor=30.0,
        setpoint=coordinator._read_chart_setpoint(),
        comfort_target=coordinator._read_chart_comfort_target(),
    )
    chart = coordinator.get_chart_data("24h")
    targets = [p["target"] for p in chart["effective_target_history"] if p["target"] is not None]
    commanded = [p["target"] for p in chart["commanded_setpoint_history"] if p["target"] is not None]
    assert targets[-1] == pytest.approx(_PLAIN_F, abs=0.06), "the Target line must stay at the bedroom's target"
    assert commanded and commanded[-1] == pytest.approx(_PLAIN_F + 4.0, abs=0.06)


@pytest.mark.parametrize("fan", _FANS)
async def test_pause_and_stale_each_get_a_short_reason(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await _at_cap(house, engine)

    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door, "precondition: the window outlasted its debounce"
    gated = (await _status(house))["remote_offset"]
    assert gated["state"] == "gated" and gated["gate_reason"] == "door_window_pause"
    assert gated["text"] == "Bedroom offset off: paused for an open window or door"

    await house.close_contact("window")
    await house.advance(minutes=5)
    assert not engine._paused_by_door
    await house.advance(hours=3)  # the bedroom sensor goes quiet: no report in 3 h
    stale = (await _status(house))["remote_offset"]
    assert stale["state"] == "stale"
    assert stale["text"] == "Bedroom sensor not reporting - offset not in use"
