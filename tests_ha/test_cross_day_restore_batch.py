"""Real-HA tests for the cross-day restore batch (Issues #1354, #1357, #1358).

Occupant story: Home Assistant goes down late in the evening and comes back after midnight. The person in the home
expects the house to behave as if nothing happened. Before this batch, `async_restore_state()` threw away every saved
setting when the state file's date was not today, so after an overnight outage:

* #1354: the family on vacation (or away) came home to a thermostat holding the comfort setpoint, because Climate
  Advisor's engine started in "home" and the live Vacation/Away toggle was never pushed into it;
* #1358: someone who had switched the whole-house fan off to stop the noise heard it start again (Climate Advisor
  restarted nat-vent), because the fan-off hold and fan override were not restored across the date boundary;
* #1357: the activity report showed nothing from before the outage, only a fresh log.

Every cross-day scene has a same-day (10 minute outage) or no-press control, so no test passes only because Climate
Advisor would not have acted anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_MIDNIGHT = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
_SHORT_S = 10 * 60
_ACROSS_S = 75 * 60

# --------------------------------------------------------------------------------------------------------------
# #1354 vacation / away
# --------------------------------------------------------------------------------------------------------------

_COLD = {
    "start": datetime(2026, 1, 15, 22, 0, tzinfo=UTC),
    "initial_indoor_f": 64.0,
    "outdoor_f": 35.0,
    "high_f": 45.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
}
_COMFORT_HEAT_F = 70.0
_VACATION_TOGGLE = {
    "extra_states": {"input_boolean.vacation": "on"},
    "ca_config": {"vacation_toggle_entity": "input_boolean.vacation"},
}
_AWAY_TOGGLE = {
    "extra_states": {"input_boolean.home": "off"},
    "ca_config": {"home_toggle_entity": "input_boolean.home"},
}


async def _occupancy_after_restart(make_house, toggle: dict, down_for_s: float):
    from homeassistant.util import dt as dt_util

    house = await make_house(**_COLD, **toggle)
    # The outage starts at 23:40 when it must cross midnight, 23:30 for the same-day control.
    await house.advance_to(datetime(2026, 1, 15, 23, 30 if down_for_s <= _SHORT_S else 40, tzinfo=UTC))
    day_before = dt_util.now().date()
    await house.restart(down_for_seconds=down_for_s, settle=False)
    house.crossed_midnight = dt_util.now().date() > day_before
    await house.advance(seconds=300 + 90)  # past the 300 s startup window and the plan application that follows
    await house.advance(minutes=5)
    return house


@pytest.mark.parametrize(
    ("down_for_s", "crosses_midnight"),
    [
        pytest.param(_SHORT_S, False, id="same_day_10min_control"),
        pytest.param(_ACROSS_S, True, id="across_midnight_75min"),
    ],
)
async def test_vacation_setback_survives_restart(make_house, down_for_s, crosses_midnight):
    """The family is on vacation, the Vacation switch is on and the house restarts overnight: the thermostat goes to
    the vacation setback, not back up to the 70 F comfort setpoint, whether or not midnight passed during the outage.

    Old behaviour: across midnight the engine kept its default "home" occupancy, so the plan commanded comfort heat.
    """
    house = await _occupancy_after_restart(make_house, _VACATION_TOGGLE, down_for_s)
    assert house.crossed_midnight is crosses_midnight
    co = house.coordinator
    assert co._occupancy_mode == "vacation"
    assert co.automation_engine._occupancy_mode == "vacation"
    assert co._occupancy_away_since is not None, "away minutes are not being counted"
    assert house.thermostat.mode == "heat"
    assert house.thermostat.setpoint_f is not None
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 5, f"setpoint {house.thermostat.setpoint_f} is comfort"


async def test_vacation_toggle_on_before_first_setup_applies_setback(make_house):
    """The family leaves on vacation, turns the Vacation switch on and only then Climate Advisor starts (first install
    or a reload): after the startup window the thermostat holds the vacation setback, not comfort heat.

    Old behaviour: the engine started in "home" and only a later toggle change would have switched it.
    """
    house = await make_house(**_COLD, **_VACATION_TOGGLE)
    await house.advance(minutes=15)
    co = house.coordinator
    assert co._occupancy_mode == "vacation"
    assert co.automation_engine._occupancy_mode == "vacation"
    assert co._occupancy_away_since is not None
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 5


async def test_away_toggle_survives_restart_across_midnight(make_house):
    """Everyone is out (the Home switch is off) when the house restarts after midnight: the thermostat holds the away
    setback, not comfort heat.

    Old behaviour: the engine came back in "home", the same as the vacation case.
    """
    house = await _occupancy_after_restart(make_house, _AWAY_TOGGLE, _ACROSS_S)
    assert house.crossed_midnight
    co = house.coordinator
    assert co._occupancy_mode == "away"
    assert co.automation_engine._occupancy_mode == "away"
    assert house.thermostat.setpoint_f < _COMFORT_HEAT_F - 3


async def test_home_control_keeps_comfort_after_restart_across_midnight(make_house):
    """Control: with nobody away the same cold night and restart leaves the thermostat at the comfort setpoint, so the
    setback assertions above are caused by the toggle and not by the scene.
    """
    house = await _occupancy_after_restart(make_house, {}, _ACROSS_S)
    assert house.coordinator.automation_engine._occupancy_mode == "home"
    assert house.thermostat.setpoint_f == pytest.approx(_COMFORT_HEAT_F, abs=1.0)


# --------------------------------------------------------------------------------------------------------------
# #1358 whole-house fan: the occupant's fan-off / fan-on survive an overnight restart
# --------------------------------------------------------------------------------------------------------------

_FAN_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",  # indoor stays 72 F: the only fan exits are the ones the test causes
    "start": datetime(2026, 7, 15, 22, 30, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 65.0,
    "high_f": 70.0,
    "low_f": 55.0,
    "initial_hvac_mode": "cool",
}
_LONG_GRACE_S = 4 * 3600  # long enough that the occupant's hold outlasts the 75 minute outage


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


async def _fan_house(make_house, fan: str, **overrides):
    house = await make_house(
        fan=fan,
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "manual_grace_seconds": _LONG_GRACE_S},
        **{**_FAN_HOUSE, **overrides},
    )
    await _steps(house, 360)  # past the start-up window
    return house


def _fan_on(house) -> bool:
    return house.fan.is_on


def _ca_fan_starts(house) -> list:
    return [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_on"]


async def _start_natvent(house) -> None:
    await house.open("window")
    await _steps(house, 90)
    assert _ca_fan_starts(house), "precondition: with the window open Climate Advisor runs the fan (nat-vent)"
    assert _fan_on(house)


async def _fan_after_restart(house, down_for_s: float) -> list[bool]:
    from homeassistant.util import dt as dt_util

    day_before = dt_util.now().date()
    starts_before = len(_ca_fan_starts(house))
    await house.restart(down_for_seconds=down_for_s, settle=False)
    house.crossed_midnight = dt_util.now().date() > day_before
    seen: list[bool] = []
    for _ in range(14):  # through the 300 s startup window and 9 minutes beyond it
        await house.advance(minutes=1)
        seen.append(_fan_on(house))
    seen.append(len(_ca_fan_starts(house)) > starts_before)
    return seen


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
@pytest.mark.parametrize(
    ("down_for_s", "label"),
    [
        pytest.param(_SHORT_S, "same_day_control", id="same_day"),
        pytest.param(_ACROSS_S, "across_midnight", id="midnight"),
    ],
)
async def test_fan_off_by_occupant_stays_off_after_restart(make_house, fan, down_for_s, label):
    """The occupant switches the whole-house fan off to stop the noise shortly before midnight; Home Assistant goes
    down and returns: the fan stays off and Climate Advisor does not start it again, even with the window open and
    cool air outside.

    Old behaviour: a restart landing on a later date dropped the fan-off hold, so nat-vent turned the fan back on once
    the startup window closed.
    """
    house = await _fan_house(make_house, fan)
    await _start_natvent(house)
    await _steps(house, 180)  # the off must not look like an echo of Climate Advisor's own command
    await house.occupant_presses_fan(False)
    await _steps(house, 30)
    assert not _fan_on(house)
    assert house.coordinator.automation_engine._fan_off_hold_active(), "precondition: the fan-off hold is running"
    await house.advance_to(datetime(2026, 7, 15, 23, 30 if down_for_s <= _SHORT_S else 40, tzinfo=UTC))
    graces_before = _fan_off_grace_rows(house)

    seen = await _fan_after_restart(house, down_for_s)

    assert house.crossed_midnight is (label == "across_midnight")
    assert not any(seen[:-1]), f"the fan came back on after the restart: {seen}"
    assert seen[-1] is False, "Climate Advisor commanded the fan on after the restart"
    assert house.coordinator.automation_engine._fan_off_hold_active()
    # Issue #1362: the Activity log shows the hold was re-armed by the restart (the event-log restore used to
    # run after the re-arm and drop its grace_started row).
    assert _fan_off_grace_rows(house) == graces_before + 1, "re-armed fan-off grace missing from the Activity log"


def _fan_off_grace_rows(house) -> int:
    return sum(
        1 for e in house.coordinator._event_log if e.get("type") == "grace_started" and e.get("trigger") == "fan_off"
    )


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_control_without_press_natvent_runs_the_fan_after_restart(make_house, fan):
    """Control: the same scene where the occupant never touches the fan. Climate Advisor still runs the fan after the
    restart across midnight, so the hold test above is not passing because nat-vent cannot run.
    """
    house = await _fan_house(make_house, fan)
    await _start_natvent(house)
    await _steps(house, 180)
    await house.advance_to(datetime(2026, 7, 15, 23, 40, tzinfo=UTC))

    seen = await _fan_after_restart(house, _ACROSS_S)

    assert house.crossed_midnight
    assert _fan_on(house), f"nat-vent should be running the fan: {seen}"


@pytest.mark.parametrize("fan", ["whf"])
async def test_fan_on_by_occupant_stays_on_after_restart_across_midnight(make_house, fan):
    """The occupant switches the whole-house fan ON by hand (window open, hotter outside, so nat-vent would not
    run it): the fan keeps running through the overnight restart and Climate Advisor does not switch it off.

    Old behaviour: the fan override was dropped by the date gate, so Climate Advisor reconciled the fan off at the end
    of the startup window.
    """
    house = await _fan_house(make_house, fan, outdoor_f=82.0, high_f=88.0, low_f=70.0)
    await house.open("window")
    await _steps(house, 90)
    assert not _fan_on(house), "precondition: nat-vent does not run the fan when it is hotter outside"
    await house.occupant_presses_fan(True)
    await _steps(house, 60)
    assert _fan_on(house)
    await house.advance_to(datetime(2026, 7, 15, 23, 40, tzinfo=UTC))

    seen = await _fan_after_restart(house, _ACROSS_S)

    assert house.crossed_midnight
    assert all(seen[:-1]), f"the fan was switched off after the restart: {seen}"
    assert not [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_off"]


# --------------------------------------------------------------------------------------------------------------
# #1357 activity continuity
# --------------------------------------------------------------------------------------------------------------


async def test_activity_log_survives_restart_across_midnight(make_house):
    """The occupant opens the Activity report in the morning and still sees what Climate Advisor did the evening
    before the outage, plus a line saying the system restarted.

    Old behaviour: the event log was discarded by the date gate, so the report started empty after midnight.
    """
    from homeassistant.util import dt as dt_util

    house = await make_house(**_COLD)
    await house.advance_to(datetime(2026, 1, 15, 23, 40, tzinfo=UTC))
    co = house.coordinator
    before = [dict(e) for e in co._event_log]
    assert before, "precondition: Climate Advisor logged something in the first half hour"
    day_before = dt_util.now().date()

    await house.restart(down_for_seconds=_ACROSS_S, settle=False)
    await house.advance(seconds=30)

    assert dt_util.now().date() > day_before
    co = house.coordinator
    after_times = {(e["time"], e["type"]) for e in co._event_log}
    missing = [e for e in before if e["type"] != "system_restarted" and (e["time"], e["type"]) not in after_times]
    assert not missing, f"events from before the outage vanished: {missing}"
    restarts = [e for e in co._event_log if e["type"] == "system_restarted"]
    assert restarts, "no system_restarted entry after the restart"
    assert restarts[-1]["recovered_events"] >= len(before) - sum(e["type"] == "system_restarted" for e in before)
