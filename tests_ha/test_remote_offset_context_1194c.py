"""Real-HA tests for Issue #1194 (c): a whole night of the bedroom-sensor outer loop, entry to exit.

Occupant story: the household goes to bed. The hallway thermostat is satisfied but the bedroom (the sleep sensor) is
cold, so Climate Advisor raises the hallway setpoint in 2 F steps. At the start of the night the extra degrees must
never be stacked on the evening number, and in the morning the thermostat must come back to the plain MORNING target:
never the morning target plus the bedroom's extra degrees, and never last night's sleep target.

The night is compressed to a 30-minute sleep window that wraps midnight (23:50 -> 00:20) so a real-HA run stays short.
The REAL coordinator timers (bedtime, wake, the 1-minute loop floor) and listeners run against the dev thermostat sim.
Both fan configurations (``whf``, ``hvac_fan``).

Prove the tests are not vacuous with ``scripts/revert_proof.py <pre-fix commit> <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
BEDROOM = "sensor.house_bedroom"
_SLEEP_F = 66.0  # the sleep floor Climate Advisor writes when the night starts
_MORNING_F = 71.0  # the daytime floor: neither the sleep floor nor the sleep floor + the 4 F cap (70)
_HOUSE = {
    "start": datetime(2026, 1, 15, 23, 40, tzinfo=UTC),
    "startup": "settled",
    "profile": "scripted",
    "initial_indoor_f": 68.0,  # the hallway reads above its setpoint-1: the thermostat is satisfied
    "outdoor_f": 30.0,
    "high_f": 38.0,
    "low_f": 22.0,
    "initial_hvac_mode": "heat",
    "ca_config": {
        "sleep_indoor_temp_entity": BEDROOM,
        "sleep_time": "23:50",
        "wake_time": "00:20",
        "comfort_heat": _MORNING_F,
        "manual_grace_seconds": 3600,
        "override_confirm_seconds": 60,
        "fan_min_runtime_per_hour": 0,
    },
}


def _bedroom(house, state: str) -> None:
    house.hass.states.async_set(BEDROOM, state, {"unit_of_measurement": "°F"})


async def _seed_bedroom(house) -> None:
    _bedroom(house, "58.0")


def _ca_setpoints(house, since: int) -> list[float]:
    return [
        c.data["temperature"]
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service == "set_temperature"
    ]


def _collapse(values: list[float]) -> list[float]:
    out: list[float] = []
    for v in values:
        if not out or out[-1] != v:
            out.append(v)
    return out


async def _evening(make_house, fan: str):
    house = await make_house(fan=fan, before_ca_setup=_seed_bedroom, **_HOUSE)
    engine = house.coordinator.automation_engine
    assert not engine._remote_context_key, "precondition: not in the sleep window yet (23:45)"
    assert house.thermostat.setpoint_f == _MORNING_F, f"precondition: the evening number, {house.thermostat.setpoint_f}"
    return house, engine


@pytest.mark.parametrize("fan", _FANS)
async def test_a_night_enters_on_the_sleep_band_and_leaves_on_the_morning_band(make_house, fan) -> None:
    house, engine = await _evening(make_house, fan)
    mark = len(house.calls)

    # ENTRY: 23:50. The sleep band lands plain; the loop's first decision follows it, never the evening number + 2.
    await house.advance(minutes=6)
    writes = _collapse(_ca_setpoints(house, mark))
    while writes and writes[0] == _MORNING_F:  # CA's own evening re-asserts before the window opened
        writes.pop(0)
    assert engine._remote_context_key == f"sleep:{BEDROOM}", "precondition: the sleep context began"
    assert writes and writes[0] == _SLEEP_F, f"the night must start on the plain sleep band: {writes}"
    assert max(writes) <= _SLEEP_F + 4.0, f"extra degrees on top of the evening number: {writes}"
    assert _MORNING_F + 2.0 not in writes

    await house.advance(minutes=12)  # two raises, a startup wait apart
    assert house.thermostat.setpoint_f == _SLEEP_F + 4.0, "precondition: the loop reached the +4 F cap overnight"
    assert engine._remote_offset_applied_f == pytest.approx(4.0)

    # EXIT: 00:20. The wake band is the plain morning target; the loop adds nothing on top and writes nothing after it.
    await house.advance(minutes=15)  # 00:18
    _bedroom(house, "58.0")  # a fresh cold report just before the wake minute: the loop would hold the cap
    await house.advance(minutes=1)
    mark = len(house.calls)
    await house.advance(minutes=7)  # across 00:20
    assert engine._remote_context_key is None, "precondition: the sleep context ended"
    after = _collapse(_ca_setpoints(house, mark))
    assert after, "precondition: the wake routine wrote the morning band"
    assert after == [_MORNING_F], f"the morning must be the plain morning target, got {after}"
    assert house.thermostat.setpoint_f == _MORNING_F
    assert engine._remote_offset_f == 0.0 and engine._remote_offset_applied_f == 0.0

    # And it stays there: the morning bedroom reports do not wake the loop up again.
    mark = len(house.calls)
    _bedroom(house, "55.0")
    await house.advance(minutes=20)
    assert _ca_setpoints(house, mark) == [] or set(_ca_setpoints(house, mark)) == {_MORNING_F}
    assert house.thermostat.setpoint_f == _MORNING_F


@pytest.mark.parametrize("fan", _FANS)
async def test_the_bedroom_sensor_dropping_out_overnight_clears_the_offset_then_resumes(make_house, fan) -> None:
    """Exit with NO wake write: the sensor goes unavailable mid-night. The loop itself returns the thermostat to the
    plain sleep target once (after the grace), and starts from zero when the sensor is back."""
    house, engine = await _evening(make_house, fan)
    await house.advance(minutes=18)  # 23:50 entry, then up to the cap
    assert house.thermostat.setpoint_f == _SLEEP_F + 4.0, "precondition: at the cap"
    mark = len(house.calls)
    _bedroom(house, "unavailable")
    await house.advance(minutes=1)
    assert house.thermostat.setpoint_f == _SLEEP_F + 4.0, "the loop must leave the next band's writer a moment first"
    await house.advance(minutes=3)
    assert engine._remote_context_key is None, "precondition: the context ended with the sensor"
    assert _collapse(_ca_setpoints(house, mark)) == [_SLEEP_F], "one plain write after the grace"
    assert house.thermostat.setpoint_f == _SLEEP_F

    _bedroom(house, "58.0")  # back, and cold: a new entry starts from zero, then one fresh decision
    mark = len(house.calls)
    await house.advance(minutes=3)
    assert engine._remote_context_key == f"sleep:{BEDROOM}"
    assert engine._remote_offset_f <= 2.0 + 1e-6, "re-entry must not inherit the old +4 F"
    assert max(_ca_setpoints(house, mark) or [_SLEEP_F]) <= _SLEEP_F + 2.0
