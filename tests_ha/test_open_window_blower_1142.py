"""Real-HA tests for Issue #1142: blower (``hvac_fan``) ventilation never heats or cools into an open window.

Occupant stories (hvac_fan install, the thermostat's own blower is the ventilation fan):

* D7 at home. A mild evening, the window is opened and the blower runs for free cooling. Before the fix Climate
  Advisor left the thermostat armed to cool at the comfort ceiling ("AC assist"); warm the house and the AC ran
  with the window open. Now the thermostat stays Off while the blower ventilates.
* D7 away. The same start while the household is away wrote ``cool`` at the occupied ceiling, below the away
  setback, and only the next 30-minute cycle put the setback back.
* D4. The outdoor air warms past the indoor air while the window is still open: ventilation ends, the window
  pause is recorded, and the thermostat must be Off at that moment, not at the next cycle. Closing the window
  afterwards switches the mode on once, without flapping.
* D1 / D2 (whole-house fan). A hot evening where only the soft start qualifies: the occupant must not get an
  "HVAC paused" message for a pause the fan cancels seconds later.

Prove the tests are not vacuous with ``scripts/revert_proof.py``.
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
    "initial_indoor_f": 73.0,
    "outdoor_f": 66.0,
    "high_f": 68.0,
    "low_f": 62.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
}


def _ca_climate_writes(house, *, since: int = 0):
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.domain == "climate" and c.service in ("set_hvac_mode", "set_temperature")
    ]


async def _ventilating(make_house, *, away: bool = False):
    house = await make_house(**_MILD_EVENING)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    if away:
        await engine.handle_occupancy_away()
        await house.advance(minutes=1)
        house.away_marker = len(house.calls)  # the away setback itself is written above; judge what follows
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: the open window started natural ventilation"
    assert house.thermostat.fan_mode == "on", "precondition: Climate Advisor is running the blower"
    return house, engine


async def test_blower_ventilation_at_home_leaves_the_thermostat_off(make_house) -> None:
    house, engine = await _ventilating(make_house)
    assert house.thermostat.mode == "off", f"AC assist armed with the window open: {house.thermostat.mode}"
    house.set_indoor(77.0)  # the house warms with the window still open
    await house.advance(minutes=10)
    assert house.thermostat.action not in ("cooling", "heating"), "rule 3: AC ran with the window open"


async def test_blower_ventilation_while_away_never_writes_cool_below_the_setback(make_house) -> None:
    house, engine = await _ventilating(make_house, away=True)
    setback = float(engine.config.get("setback_cool", 80.0))
    low_cool = [
        c
        for c in _ca_climate_writes(house, since=house.away_marker)
        if c.data.get("hvac_mode") == "cool" and (c.data.get("temperature") or 999) < setback - 0.6
    ]
    assert not low_cool, f"cool written below the away setback {setback}: {low_cool}"


async def test_outdoor_rise_exit_with_window_open_leaves_thermostat_off_and_close_does_not_flap(make_house) -> None:
    house, engine = await _ventilating(make_house)
    house.set_outdoor(80.0, high_f=82.0, low_f=62.0)
    for _ in range(90):  # CA polls the outdoor temperature every 30 minutes; the exit then needs its 90 s sustain
        await house.advance(minutes=1)
        if not engine._natural_vent_active:
            break
    assert engine._natural_vent_active is False, "precondition: the outdoor-rise exit ran"
    assert engine._paused_by_door is True, "precondition: the window pause is recorded"
    assert house.thermostat.mode == "off", (
        f"armed with the window open at the moment ventilation ended: {house.thermostat.mode}"
    )

    before = len(house.calls)
    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 120)
    modes = [c.data.get("hvac_mode") for c in _ca_climate_writes(house, since=before) if c.data.get("hvac_mode")]
    assert len(set(modes)) <= 1, f"modes flapped after the window closed: {modes}"


_HOT_EVENING_WHF = {
    "fan": "whf",
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    "initial_indoor_f": 80.0,
    "outdoor_f": 79.0,
    "high_f": 85.0,
    "low_f": 66.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
}


async def test_soft_start_evening_gets_no_hvac_paused_message(make_house) -> None:
    house = await make_house(**_HOT_EVENING_WHF)
    await house.advance(minutes=6)
    engine = house.coordinator.automation_engine
    coordinator = house.coordinator
    now = dt_util.now()
    # today's outdoor readings peaked at 85 an hour ago and have declined to 79.6: only the soft start qualifies
    coordinator._outdoor_temp_history = [(now.isoformat(), t) for t in (84.0, 85.0, 84.0, 83.0, 82.0)] + list(
        coordinator._outdoor_temp_history
    )
    engine._outdoor_temp_today_peak = 85.0
    engine._outdoor_temp_today_sample_count = len(coordinator._outdoor_temp_history)
    sent: list[tuple[str, str]] = []
    real_notify = engine._notify

    async def _spy(message, title, notification_type, **kwargs):
        sent.append((notification_type, message))
        return await real_notify(message, title, notification_type, **kwargs)

    engine._notify = _spy
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 180)
    assert engine._natural_vent_active, "precondition: the soft start ran"
    paused = [m for t, m in sent if t == "door_window_pause"]
    assert not paused, f"false 'HVAC paused' message before the fan started: {paused}"
