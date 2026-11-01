"""Real-HA tests for Issues #1195 and #1196: the thermostat blower and the 5-minute toggle floor.

Occupant stories (hvac_fan install, the thermostat's own blower is the ventilation fan):

* #1195. A mild evening, the windows are opened and Climate Advisor runs the blower for free cooling. The
  windows are closed again within five minutes of the blower starting. The blower-off is held back by the toggle
  floor; before the fix nothing ever finished the exit, so the blower ran on with every window shut and Status
  still said "nat-vent" (overnight, on a mild night). Now it stops as soon as the floor ends.
  Positive control: windows closed after the floor -> the blower stops at once.
* #1196. The blower is restricted (hvac_fan_restrict_mode). A window that opens, closes and reopens while the
  blower is refused used to leave a stale "manual fan-on not yet closed" marker. Later Climate Advisor
  legitimately started the blower, the person turned it off, and Climate Advisor turned it back on five
  minutes later. Now the person's off holds. Control: no window churn -> the person's off holds (30 min grace).

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60
_MILD_EVENING = {
    "fan": "hvac_fan",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 74.5,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "off",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
}


def _ca_fan_commands(calls, *, since: int = 0):
    return [c for c in calls[since:] if c.origin.startswith("ca:") and c.service == "set_fan_mode"]


async def _blower_running_for_ventilation(make_house):
    house = await make_house(**_MILD_EVENING)
    await house.advance(minutes=6)  # past the start-up window
    engine = house.coordinator.automation_engine
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: the open window started natural ventilation"
    assert house.thermostat.fan_mode == "on", "precondition: Climate Advisor is running the blower"
    return house, engine


async def test_windows_close_inside_the_floor_the_blower_still_stops(make_house) -> None:
    house, engine = await _blower_running_for_ventilation(make_house)

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)  # debounced close, still inside the 5-minute floor
    assert house.thermostat.fan_mode == "on", "precondition: the floor holds the blower-off back"
    assert engine._natural_vent_active, "precondition: the session is kept while the blower is still on (#931)"

    await house.advance(minutes=5)  # the floor ends; the deferred exit must now finish by itself
    assert house.thermostat.fan_mode != "on", "the blower ran on with every window shut"
    assert engine._natural_vent_active is False, "Status must not keep saying nat-vent behind shut windows"
    assert engine._fan_active is False


async def test_positive_control_windows_closed_after_the_floor_stop_the_blower_at_once(make_house) -> None:
    house, engine = await _blower_running_for_ventilation(make_house)

    await house.advance(minutes=6)  # well past the floor, window still open
    assert house.thermostat.fan_mode == "on" and engine._natural_vent_active

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert house.thermostat.fan_mode != "on"
    assert engine._natural_vent_active is False


# ---------------------------------------------------------------------------
# #1196
# ---------------------------------------------------------------------------

_RESTRICTED = {
    **_MILD_EVENING,
    "initial_hvac_mode": "cool",
    "initial_indoor_f": 74.5,
    "ca_config": {
        **_MILD_EVENING["ca_config"],
        "hvac_fan_restrict_mode": "heat",  # no blower while cooling, or within 2 h of cooling
        "automation_grace_seconds": 300,
        "manual_grace_seconds": 1800,
    },
}


async def _restricted_house_then_blower_started_by_ca(make_house, *, churn: bool):
    house = await make_house(**_RESTRICTED)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    # the house cooled a moment ago (the restriction also holds for 2 h after cooling stops)
    engine._last_hvac_cooling_active = dt_util.utcnow().isoformat()
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)
    assert house.thermostat.fan_mode != "on", "precondition: the restriction refuses the blower"
    if churn:
        await house.close_contact("window")
        await house.advance(seconds=_DEBOUNCE_S + 30)
        await house.open("window")
        await house.advance(seconds=_DEBOUNCE_S + 30)
        await house.advance(minutes=6)  # past the automation grace: the grace-expiry re-check ran
    # the restriction lapses: 2 hours after the last cooling
    await house.advance(hours=2.2)
    return house, engine


async def test_stale_marker_does_not_make_climate_advisor_undo_the_occupants_blower_off(make_house) -> None:
    house, engine = await _restricted_house_then_blower_started_by_ca(make_house, churn=True)
    assert engine._fan_manual_origin_unclosed is False, "no blower ran, so no 'manual fan-on' can be unclosed"
    await _occupant_turns_blower_off_and_it_stays_off(house, engine)


async def test_control_without_window_churn_the_occupants_blower_off_holds(make_house) -> None:
    house, engine = await _restricted_house_then_blower_started_by_ca(make_house, churn=False)
    await _occupant_turns_blower_off_and_it_stays_off(house, engine)


async def _occupant_turns_blower_off_and_it_stays_off(house, engine) -> None:
    if house.thermostat.fan_mode != "on":
        await house.advance(minutes=10)  # let CA start the blower now that the restriction has lapsed
    assert house.thermostat.fan_mode == "on", "precondition: Climate Advisor started the blower"
    await house.advance(minutes=8)
    await house.occupant_sets_fan_mode("auto")
    calls_before = len(house.calls)
    await house.advance(minutes=1)
    assert engine._grace_active, "the occupant's blower-off starts the fan-off grace"
    await house.advance(minutes=12)
    assert house.thermostat.fan_mode != "on", "Climate Advisor turned the blower back on over the occupant's choice"
    assert not [c for c in _ca_fan_commands(house.calls, since=calls_before) if c.data.get("fan_mode") == "on"]
