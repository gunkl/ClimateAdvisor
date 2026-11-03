"""Real-HA tests for Issue #1194 (e): what only the real coordinator, real timers and a late-answering thermostat show.

Occupant story: it is a cold night and the bedroom (the sleep sensor) is under the sleep floor while the hallway
thermostat is satisfied. Climate Advisor nudges the hallway setpoint. The pieces the fast harness (Tier A) cannot
prove with its virtual clock and instant thermostat are checked here against real Home Assistant and the dev
thermostat simulator:

* the 1-minute cadence -- the loop steps on its timer alone, with no new bedroom report, and a silent sensor is
  dropped by the same timer after 120 minutes;
* the delayed echo -- a cloud thermostat reports the loop's own steps 30-120 s late and without Climate Advisor's
  Context; the sleeper's thermostat must not be read as overridden (#1232), and a person who really turns the dial
  in that time must still be caught;
* restart -- Home Assistant restarts mid-night with the offset on the thermostat: nothing is written inside the
  startup window, the thermostat is never driven past the cap, and the house is back on plan afterwards.

Both fan configurations (``whf``, ``hvac_fan``). Cool mode ships simulation-tested only (Tier A).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
SIM_DOMAIN = "ca_dev_thermostat_sim"
BEDROOM = "sensor.house_bedroom"
_PLAIN_F = 66.0
_HOUSE = {
    "contacts": [],
    "startup": "settled",
    "profile": "scripted",
    "start": datetime(2026, 1, 15, 2, 0, tzinfo=UTC),
    "initial_indoor_f": 68.0,  # the hallway reads above its setpoint-1: the thermostat is satisfied
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
    "ca_config": {
        "sleep_indoor_temp_entity": BEDROOM,
        "sleep_time": "20:30",
        "wake_time": "06:30",
        "manual_grace_seconds": 3600,
        "override_confirm_seconds": 60,
        "fan_min_runtime_per_hour": 0,
    },
}


def _bedroom(house, f: float) -> None:
    house.hass.states.async_set(BEDROOM, str(f), {"unit_of_measurement": "°F"})


async def _seed_bedroom(house) -> None:
    _bedroom(house, 58.0)


async def _cold_night(make_house, fan: str, **extra):
    house = await make_house(fan=fan, before_ca_setup=_seed_bedroom, **{**_HOUSE, **extra})
    engine = house.coordinator.automation_engine
    assert engine._remote_target_f == _PLAIN_F, "precondition: CA wrote the sleep floor"
    return house, engine


def _sim(house):
    return house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]


def _ca_setpoints(house, since: int) -> list[float]:
    return [
        c.data["temperature"]
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service == "set_temperature"
    ]


def _raises(house, since: int) -> list[float]:
    """The offset values CA moved through, in order (above the plain sleep floor, repeats collapsed).

    Other writers (the band refresh, the window refresh) re-apply the HELD offset through the single setpoint path,
    so the same value can be written again; that is not a step.
    """
    steps: list[float] = []
    for w in _ca_setpoints(house, since):
        if w > _PLAIN_F and (not steps or steps[-1] != w):
            steps.append(w)
    return steps


def _overrides(house) -> int:
    return house.coordinator._today_record.manual_overrides


# ---------------------------------------------------------------------------------------------------------------
# The 1-minute cadence, on real timers
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan", _FANS)
async def test_the_one_minute_timer_steps_the_loop_with_no_new_bedroom_report(make_house, fan) -> None:
    """The bedroom reported once, a long time ago in sensor terms. The loop's own 1-minute timer still raises one
    step within a minute, then the next after the startup wait: the sleeper does not wait for the next report."""
    house, engine = await _cold_night(make_house, fan)
    first = len(house.calls)
    await house.advance(seconds=75)  # one timer tick, no bedroom report
    assert _raises(house, first) == [_PLAIN_F + 2.0], "the first step did not come from the 1-minute timer"
    await house.advance(seconds=120)
    assert _raises(house, first) == [_PLAIN_F + 2.0], "a second step inside the startup wait"
    await house.advance(minutes=4)  # past the 300 s wait
    assert _raises(house, first) == [_PLAIN_F + 2.0, _PLAIN_F + 4.0]
    await house.advance(minutes=30)
    assert _raises(house, first) == [_PLAIN_F + 2.0, _PLAIN_F + 4.0], "stepped past the +4 F cap"
    assert engine._remote_offset_applied_f == pytest.approx(4.0)


@pytest.mark.parametrize("fan", _FANS)
async def test_a_silent_bedroom_sensor_is_dropped_by_the_timer_alone_after_120_minutes(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    await house.advance(minutes=12)
    assert house.thermostat.setpoint_f == _PLAIN_F + 4.0, "precondition: the loop is at the cap"
    mark = len(house.calls)
    await house.advance(minutes=100)  # still inside the 120 minutes since the last report
    assert house.thermostat.setpoint_f == _PLAIN_F + 4.0
    assert set(_ca_setpoints(house, mark)) <= {_PLAIN_F + 4.0}, "a new step inside the 120 minutes"
    await house.advance(minutes=30)  # past 120 minutes with no report at all
    assert house.thermostat.setpoint_f == _PLAIN_F, "a stale cold reading still held the +4 F offset"
    after = len(house.calls)
    await house.advance(minutes=60)
    assert not _raises(house, after), "the loop stepped again on a stale reading"
    assert house.thermostat.setpoint_f == _PLAIN_F


# ---------------------------------------------------------------------------------------------------------------
# The delayed echo of the loop's own steps
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("echo_s", [45, 110])
async def test_the_loops_own_steps_echoing_back_late_are_not_an_override(make_house, fan, echo_s) -> None:
    house, engine = await _cold_night(make_house, fan)
    _sim(house).set_echo_delay(echo_s)
    await house.advance(minutes=12)  # both steps are written; each is reported echo_s later, without CA's Context
    await house.advance(seconds=echo_s + 30)
    _sim(house).set_echo_delay(0)
    assert house.thermostat.setpoint_f == _PLAIN_F + 4.0, "precondition: both late echoes arrived"
    assert _overrides(house) == 0, "CA's own late echo was counted as the occupant"
    assert not engine._manual_override_active and not engine._override_confirm_pending
    await house.advance(minutes=10)
    assert not engine._manual_override_active and not engine._grace_active, "a false override paused automation"


@pytest.mark.parametrize("fan", _FANS)
async def test_the_occupant_turning_the_dial_while_a_step_is_in_flight_is_still_caught(make_house, fan) -> None:
    house, engine = await _cold_night(make_house, fan)
    _sim(house).set_echo_delay(110)
    await house.advance(seconds=75)  # the first step is written; its echo is 110 s away
    await house.occupant_sets_thermostat(setpoint_f=72.0)  # a different value, from the person
    _sim(house).set_echo_delay(0)
    await house.advance(minutes=4)
    assert _overrides(house) >= 1, "the occupant's real change was swallowed as the loop's echo"
    assert engine._manual_override_active or engine._override_confirm_pending
    mark = len(house.calls)
    await house.advance(minutes=20)
    assert not _ca_setpoints(house, mark), "the loop stepped over the occupant's setpoint"


# ---------------------------------------------------------------------------------------------------------------
# Restart with an offset on the thermostat
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fan", _FANS)
async def test_restart_mid_night_writes_nothing_in_the_startup_window_and_never_exceeds_the_cap(
    make_house, fan
) -> None:
    house, engine = await _cold_night(make_house, fan)
    await house.advance(minutes=12)
    assert house.thermostat.setpoint_f == _PLAIN_F + 4.0, "precondition: the cap is on the thermostat"
    await house.restart(settle=False)
    engine = house.coordinator.automation_engine
    assert engine._remote_offset_f == 0.0 and engine._remote_target_f is None or engine._remote_target_f == _PLAIN_F, (
        "the loop must start from a clean slate after a restart (in-memory state only)"
    )
    mark = len(house.calls)
    await house.advance(seconds=200)  # inside the 300 s startup-coalescing window
    assert not _ca_setpoints(house, mark), "the loop wrote inside the startup-coalescing window"
    _bedroom(house, 58.0)  # a fresh cold report during the window: still silent
    await house.advance(seconds=60)
    assert not _ca_setpoints(house, mark)
    await house.advance(minutes=30)
    writes = _ca_setpoints(house, mark)
    assert all(_PLAIN_F <= w <= _PLAIN_F + 4.0 for w in writes), f"outside plain..cap after a restart: {writes}"
    assert house.thermostat.setpoint_f <= _PLAIN_F + 4.0
    assert not engine._manual_override_active, "the restart was read as the occupant"
