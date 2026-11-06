"""Real-HA tests for Issue #1363: Away/Vacation does not re-send the same setpoint every 5 minutes.

Occupant story: everyone leaves on a cold January day and the thermostat holds the away (or vacation) setback.
Nothing in the house changes, yet Climate Advisor used to send the same setpoint about every 5 minutes, 12 identical
writes an hour (confirmed in production 2026-11-06: 64 identical writes in 5.3 h per Away/Vacation zone, against
12-17 for a Home zone). Each write is traffic to a cloud thermostat (vendor rate limits) and a repeated Activity
entry; nothing about comfort improves.

Cause: ``apply_classification()``'s Away/Vacation branch re-applies the setback and returns before the Issue #96
revisit cancel at the end of the normal path, so every write's 5-minute follow-up re-ran the classification, which
wrote again and scheduled the next follow-up.

Controls: a Home house on the same day (the normal ~30-minute cadence), and the setback still being applied when
the occupant leaves and still held after an hour.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_COLD = {
    "start": datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 66.0,
    "outdoor_f": 35.0,
    "high_f": 45.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
}
_COMFORT_HEAT_F = 70.0
_HOME_TOGGLE = {"ca_config": {"home_toggle_entity": "input_boolean.home"}}
_VACATION_TOGGLE = {"ca_config": {"vacation_toggle_entity": "input_boolean.vacation"}}
# A 30-minute cycle can land twice in an hour, plus one write for the cycle that straddles the window edge.
_MAX_WRITES_PER_HOUR = 3


def _setpoint_writes(house, since: int) -> list:
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service == "set_temperature"
    ]


async def _hour_of_writes(house) -> list:
    since = len(house.calls)
    await house.advance(minutes=60)
    return _setpoint_writes(house, since)


@pytest.mark.parametrize(
    ("toggle", "extra"),
    [
        pytest.param(_HOME_TOGGLE, {"input_boolean.home": "off"}, id="away"),
        pytest.param(_VACATION_TOGGLE, {"input_boolean.vacation": "on"}, id="vacation"),
    ],
)
async def test_away_setback_is_not_resent_every_five_minutes(make_house, toggle, extra):
    house = await make_house(**_COLD, **toggle, extra_states=extra)
    await house.advance(minutes=40)  # past start-up, the first plan and its follow-up
    assert house.coordinator.automation_engine._occupancy_mode in ("away", "vacation")
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 3, "precondition: the setback is applied"

    writes = await _hour_of_writes(house)

    assert len(writes) <= _MAX_WRITES_PER_HOUR, (
        f"{len(writes)} setpoint writes in an hour with nothing changing: "
        f"{[(c.data.get('temperature'), c.data.get('hvac_mode')) for c in writes]}"
    )
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 3, "the setback must still be held"


async def test_home_control_same_cadence(make_house):
    """Control: a Home house in the same scene writes on the ~30-minute cycle only."""
    house = await make_house(**_COLD, **_HOME_TOGGLE, extra_states={"input_boolean.home": "on"})
    await house.advance(minutes=40)
    assert house.coordinator.automation_engine._occupancy_mode == "home"

    writes = await _hour_of_writes(house)

    assert len(writes) <= _MAX_WRITES_PER_HOUR
    assert house.thermostat.setpoint_f == pytest.approx(_COMFORT_HEAT_F, abs=1.0)


async def test_leaving_applies_setback_then_settles(make_house):
    """The occupant leaves (Home switch turned off): the away setback is applied, then held without a write storm."""
    house = await make_house(**_COLD, **_HOME_TOGGLE, extra_states={"input_boolean.home": "on"})
    await house.advance(minutes=40)
    assert house.thermostat.setpoint_f == pytest.approx(_COMFORT_HEAT_F, abs=1.0)

    house.hass.states.async_set("input_boolean.home", "off")
    await house.advance(minutes=20)  # the 15-minute away grace, then the setback
    assert house.coordinator.automation_engine._occupancy_mode == "away"
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 3, "leaving must still apply the away setback"

    writes = await _hour_of_writes(house)

    assert len(writes) <= _MAX_WRITES_PER_HOUR, f"{len(writes)} setpoint writes in the hour after leaving"
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 3
