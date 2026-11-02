"""Real-HA tests for Issue #1199: grace and hold end instants are true instants across a daylight-saving change.

Occupant story (whole_house_fan and hvac_fan): the home is in America/Los_Angeles. Someone switches off the fan
Climate Advisor started (window open, cool air outside) and the configured fan-off hold is one hour.

* Fall-back (2026-11-01): the hold started shortly after 01:00 PDT used to run an extra hour, because the end was
  stored as wall-clock "now + 1 h" and the repeated 01:00-02:00 hour is counted twice.
* Spring-forward (2026-03-08): the hold started at about 01:15 PST used to end 30 minutes early (02:15 never
  exists), and a restart in the middle restored the wrong remaining time.

The tests run the real coordinator and engine in a real Home Assistant core whose configured time zone is Los
Angeles (``build_house(time_zone=...)``), the frozen clock stepping through the transition. The unit tier
(``tests/test_dst_grace_end_1199.py``) covers the same paths with the stub clock.

Prove the tests are not vacuous with ``scripts/revert_proof.py origin/main tests_ha/test_dst_grace_end_1199.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_HOUR_S = 3600
_LA = "America/Los_Angeles"
# (id, house start in UTC)
_STARTS = [
    pytest.param(datetime(2026, 11, 1, 7, 50, 0, tzinfo=UTC), id="fall_back"),  # 00:50 PDT; the clocks go back at 02:00
    pytest.param(datetime(2026, 3, 8, 9, 5, 0, tzinfo=UTC), id="spring_forward"),  # 01:05 PST; the jump is at 02:00
]
_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",
    "initial_indoor_f": 72.0,
    "outdoor_f": 65.0,
    "high_f": 70.0,
    "low_f": 55.0,
    "initial_hvac_mode": "cool",
    "time_zone": _LA,
}


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _fan_on(house, fan: str) -> bool:
    return house.fan.is_on if fan == "whf" else house.thermostat.fan_mode == "on"


def _ca_starts(house, fan: str) -> list:
    if fan == "whf":
        return [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_on"]
    return [
        c
        for c in house.calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_fan_mode"
        and c.data.get("fan_mode") == "on"
    ]


async def _house_with_fan_off_hold(make_house, fan: str, start: datetime, caplog):
    """A running CA nat-vent fan, then the occupant's off: returns (house, engine, instant of the off)."""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan=fan,
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "manual_grace_seconds": _HOUR_S},
        start=start,
        **_HOUSE,
    )
    await _steps(house, 360)  # past the 300 s start-up window
    engine = house.coordinator.automation_engine
    starts_before = len(_ca_starts(house, fan))
    await house.open("window")
    await _steps(house, 90)
    assert len(_ca_starts(house, fan)) == starts_before + 1 and _fan_on(house, fan), "precondition: CA runs the fan"
    started = _ca_starts(house, fan)[-1].at
    while (dt_util.utcnow() - started).total_seconds() < 140:  # the off must not read as an echo of CA's command
        await house.advance(seconds=10)
    if fan == "whf":
        await house.occupant_presses_fan(False)
    else:
        await house.occupant_sets_fan_mode("auto")
    t_off = dt_util.utcnow()
    await _steps(house, 10)
    assert engine._last_grace_trigger == "fan_off" and engine._fan_off_hold_until is not None
    local = dt_util.as_local(t_off)
    assert local.tzinfo is not None and str(local.tzinfo) == _LA, "precondition: the house runs in Los Angeles"
    return house, engine, t_off


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("start", _STARTS)
async def test_fan_off_hold_lasts_one_real_hour(make_house, fan, start, caplog) -> None:
    house, engine, t_off = await _house_with_fan_off_hold(make_house, fan, start, caplog)

    end = engine._fan_off_hold_until
    assert abs((end - (t_off + timedelta(seconds=_HOUR_S))).total_seconds()) < 30, f"hold ends at {end}"
    stored = datetime.fromisoformat(engine._grace_end_time)
    assert stored.utcoffset() == timedelta(0), f"grace_end_time must be a UTC instant, got {engine._grace_end_time}"
    assert f"ends={dt_util.as_local(end).strftime('%H:%M')}" in "\n".join(r.getMessage() for r in caplog.records), (
        "the grace-start log names the true local end time"
    )

    await house.advance(seconds=_HOUR_S - 90)  # ~ 80 s before the end
    assert engine._fan_off_hold_active(), "the occupant's hour is not over: the fan must still be held off"
    assert not _ca_starts(house, fan)[1:], "CA must not have started the fan again inside the hour"
    await house.advance(seconds=180)  # ~ 100 s after the end
    assert not engine._fan_off_hold_active(), "the hour is over: the hold must have lapsed"
    await house.close()


@pytest.mark.parametrize("fan", _FANS)
@pytest.mark.parametrize("start", _STARTS)
async def test_restart_mid_hold_keeps_the_same_real_end(make_house, fan, start, caplog) -> None:
    house, engine, t_off = await _house_with_fan_off_hold(make_house, fan, start, caplog)
    true_end = t_off + timedelta(seconds=_HOUR_S)

    await house.advance(seconds=600)
    await house.restart(settle=False)
    restarted = house.coordinator.automation_engine

    assert restarted._grace_active and restarted._last_grace_trigger == "fan_off"
    assert restarted._fan_off_hold_until is not None
    assert abs((restarted._fan_off_hold_until - true_end).total_seconds()) < 30
    remaining = (true_end - dt_util.utcnow()).total_seconds()
    assert restarted._grace_duration_seconds == pytest.approx(remaining, abs=30), "~50 real minutes remain"
    await house.close()


@pytest.mark.parametrize("start", _STARTS)
async def test_startup_window_ends_five_real_minutes_after_the_start(make_house, start) -> None:
    """#1235 window: ``_startup_coalesce_expiry`` is a UTC instant 300 s out; the Next-Automation label is local."""
    house = await make_house(fan="whf", start=start, **{**_HOUSE, "startup": "cold"})
    coordinator = house.coordinator
    expiry = datetime.fromisoformat(coordinator._startup_coalesce_expiry)
    assert expiry.utcoffset() == timedelta(0)
    assert abs((expiry - (dt_util.utcnow() + timedelta(seconds=300))).total_seconds()) < 10
    assert coordinator._startup_coalesce_active
    _label, shown = coordinator._compute_next_automation_action(coordinator._current_classification)
    assert shown == dt_util.as_local(expiry).strftime("%I:%M %p").lstrip("0")
    await house.close()


@pytest.mark.parametrize("start", _STARTS)
async def test_window_debounce_expiry_is_a_utc_instant_sixty_real_seconds_out(make_house, start, caplog) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(fan="whf", ca_config={"sensor_debounce_seconds": 60}, start=start, **_HOUSE)
    await _steps(house, 360)
    await house.open("window")
    stored = house.coordinator._door_open_timer_expiry
    assert stored, "the debounce started"
    expiry = datetime.fromisoformat(next(iter(stored.values())))
    assert expiry.utcoffset() == timedelta(0)
    assert abs((expiry - (dt_util.utcnow() + timedelta(seconds=60))).total_seconds()) < 15
    line = next(r.getMessage() for r in caplog.records if "debounce started" in r.getMessage())
    assert f"at {dt_util.as_local(expiry).strftime('%H:%M:%S')}" in line, line
    await house.close()
