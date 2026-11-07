"""Real-HA tests for Issue #1425: a short Away is charted as one flat Away period, not band spikes.

Occupant story (production, 2026-11-07): the windows are open, so Climate Advisor holds the HVAC off. You leave at
11:18 and come back at 11:50, then leave again at 12:45 and come back at 13:18. Nothing is sent to the thermostat:
the away setback is skipped while the windows are open. On the Status chart the target band jumped to the setback
(63-79 F) and straight back three times, once at 12:23 while you were home, and the Away bar was off by up to 15
minutes. Cause: no chart row was written at an occupancy change (the band changed at whichever 30-minute poll came
next), and the "restore still held" band was drawn after a setback that was never sent.

After the fix a chart row is written when you leave, when the setback is applied or skipped, and when you come home;
while the 15-minute away timer runs the band drawn is the home band the thermostat is still on; and coming home with
the windows open after a skipped setback draws the home band. Control: a setback that WAS sent is still drawn as
held after you come home with a window open (Issue #1050). Real Home Assistant, the real coordinator's own chart_log
rows. Both fan configurations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_HOME = "input_boolean.home"
_BASE = {
    "profile": "scripted",
    "contacts": ["window"],
    "startup": "settled",
    "start": datetime(2026, 11, 7, 17, 30, tzinfo=UTC),  # mid-morning: no wake/sleep ramp in the band
    "initial_indoor_f": 67.0,  # below the comfort floor: no free cooling through the open window
    "outdoor_f": 62.0,
    "high_f": 75.0,
    "low_f": 50.0,
    "initial_hvac_mode": "heat",
    "ca_config": {"home_toggle_entity": _HOME, "fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    "extra_states": {_HOME: "on"},
}


def _rows(house, since: int = 0) -> list[dict]:
    return house.coordinator._chart_log.raw_entries()[since:]


def _drawn(row: dict) -> tuple[float, float]:
    """The band the chart draws for a row: the hold when the row carries one (Issue #1406), else lower/upper."""
    if row.get("hold_lower") is not None and row.get("hold_upper") is not None:
        return float(row["hold_lower"]), float(row["hold_upper"])
    return float(row["lower"]), float(row["upper"])


def _setback(house) -> tuple[float, float]:
    cfg = house.coordinator.config
    return float(cfg["setback_heat"]), float(cfg["setback_cool"])


async def _settled(make_house, fan: str, *, window_open: bool):
    house = await make_house(fan=fan, **_BASE)
    await house.advance(minutes=10)
    if window_open:
        await house.open("window")
        await house.advance(minutes=2)
        assert house.coordinator.automation_engine._paused_by_door, "precondition: the open window pauses the HVAC"
    home_band = _drawn(_rows(house)[-1])
    assert home_band != _setback(house), "precondition: the home band differs from the away setback"
    return house, home_band


async def _set_home(house, on: bool) -> None:
    house.hass.states.async_set(_HOME, "on" if on else "off")
    await house.advance(seconds=5)


@pytest.mark.parametrize("fan", _FANS)
async def test_a_short_away_with_the_windows_open_is_one_flat_away_period(make_house, fan) -> None:
    house, home_band = await _settled(make_house, fan, window_open=True)
    engine = house.coordinator.automation_engine
    setback = _setback(house)
    writes_before = [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "climate"]

    # You leave: a row at the moment you left, Away, and the home band still drawn (the setback waits 15 minutes).
    mark = len(_rows(house))
    await _set_home(house, False)
    left = [r for r in _rows(house, mark) if r.get("event") == "occupancy_change"]
    assert len(left) == 1 and left[0].get("occupancy") == "away", "#1425: no chart row when you left"
    assert _drawn(left[0]) == home_band, "the setback is drawn before Climate Advisor applies it"

    # The away timer expires: a row for the setback moment, now drawn as the setback (skipped: the window is open).
    mark = len(_rows(house))
    await house.advance(minutes=16)
    applied = [r for r in _rows(house, mark) if r.get("event") == "occupancy_setback"]
    assert len(applied) == 1, "#1425: no chart row when the away setback was applied or skipped"
    assert _drawn(applied[0]) == setback

    # You come home 16 minutes later with the window still open: a row at that moment, home band.
    mark = len(_rows(house))
    await _set_home(house, True)
    back = [r for r in _rows(house, mark) if r.get("event") == "occupancy_change"]
    assert len(back) == 1 and back[0].get("occupancy") == "home", "#1425: no chart row when you came home"
    assert _drawn(back[0]) == home_band

    # The production 12:23 spike: every row while you are home (polls included) draws the home band, not a
    # "restore held" setback that the thermostat never received.
    await house.advance(minutes=35)
    home_rows = _rows(house, mark)
    assert any(r.get("event") is None for r in home_rows), "precondition: a 30-minute poll row while home"
    spikes = [(r["ts"], _drawn(r)) for r in home_rows if _drawn(r) == setback]
    assert not spikes, f"#1425: the away setback drawn while home, never sent to the thermostat: {spikes}"
    assert engine.occupancy_restore_hold_reason() is None

    # What the house does is unchanged: the window held the HVAC off, nothing was sent for the setback.
    writes_after = [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "climate"]
    sent = [c.data for c in writes_after[len(writes_before) :] if c.service == "set_temperature"]
    assert not sent, f"no setpoint is sent while the window is open: {sent}"


@pytest.mark.parametrize("fan", _FANS)
async def test_a_setback_that_was_sent_is_still_drawn_held_after_coming_home(make_house, fan) -> None:
    """Control (Issue #1050): the windows were closed while you were out, so the thermostat holds the setback; you come
    home and open a window: the restore waits and the band drawn is the setback the thermostat is still on."""
    house, _home_band = await _settled(make_house, fan, window_open=False)
    engine = house.coordinator.automation_engine
    setback = _setback(house)

    await _set_home(house, False)
    await house.advance(minutes=16)
    assert house.thermostat.setpoint_f == pytest.approx(setback[0], abs=1.0), "precondition: the setback is held"

    await house.open("window")
    await house.advance(minutes=2)
    mark = len(_rows(house))
    await _set_home(house, True)
    assert engine.occupancy_restore_hold_reason() is not None, "the restore waits for the window"
    back = [r for r in _rows(house, mark) if r.get("event") == "occupancy_change"]
    assert len(back) == 1 and _drawn(back[0]) == setback, "the held setback must still be drawn (#1050)"
