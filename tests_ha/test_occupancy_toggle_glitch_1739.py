"""Real-HA test for Issue #1739: a glitching vacation toggle sends no "Welcome home!".

Occupant story: you are on vacation and your vacation switch is shared with HomeKit. The bridge drops the switch to
``unavailable`` or ``unknown``, the entity briefly disappears, or HomeKit flips it off and back on for a few seconds.
None of that means you came home: the mode stays Vacation, no welcome push is sent, the vacation setback stays on the
thermostat and the chart gets no occupancy-change row. A real "off" that holds for the 60-second confirmation is the
control: it moves the house to Home and sends the welcome.

Drives the real coordinator listener, confirmation timer and ``AutomationEngine.handle_occupancy_home`` (nothing in
the occupancy path is mocked); the notification is caught at the engine's ``_notify``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.const import OCCUPANCY_TOGGLE_CONFIRM_SECONDS

pytestmark = pytest.mark.house

_VAC = "input_boolean.vacation"
_COLD = {
    "start": datetime(2026, 1, 15, 18, 0, tzinfo=UTC),
    "initial_indoor_f": 64.0,
    "outdoor_f": 35.0,
    "high_f": 45.0,
    "low_f": 28.0,
    "initial_hvac_mode": "heat",
    "extra_states": {_VAC: "on"},
    "ca_config": {"vacation_toggle_entity": _VAC},
}


def _spy_notifications(engine) -> list[str]:
    sent: list[str] = []
    real_notify = engine._notify

    async def _spy(message, title, notification_type=None, **kwargs):
        sent.append(getattr(message, "key", str(message)))
        return await real_notify(message, title, notification_type=notification_type, **kwargs)

    engine._notify = _spy
    return sent


def _occupancy_rows(house, since: int) -> list[dict]:
    return [
        r
        for r in house.coordinator._chart_log.raw_entries()[since:]
        if r.get("event") in ("occupancy_change", "occupancy_setback")
    ]


def _restore_events(house) -> list[dict]:
    return [e for e in house.coordinator._event_log if str(e.get("type", "")).startswith("occupancy_comfort_restore")]


async def _on_vacation(make_house):
    house = await make_house(**_COLD)
    await house.advance(minutes=15)
    co = house.coordinator
    assert co._occupancy_mode == "vacation", "precondition: vacation from the live toggle"
    return house


async def _glitch(house, kind: str) -> None:
    """One HomeKit-style glitch of the vacation switch, ending back on."""
    if kind == "blip":  # a real off -> on flip that reverts inside the confirmation window
        house.hass.states.async_set(_VAC, "off")
        await house.advance(seconds=20)
    elif kind == "missing":  # the bridge briefly drops the entity
        house.hass.states.async_remove(_VAC)
        await house.advance(seconds=OCCUPANCY_TOGGLE_CONFIRM_SECONDS * 3)
    else:
        house.hass.states.async_set(_VAC, kind)
        await house.advance(seconds=OCCUPANCY_TOGGLE_CONFIRM_SECONDS * 3)  # long past the confirmation window
    assert house.coordinator._occupancy_mode == "vacation", f"{kind}: the mode moved during the glitch"
    house.hass.states.async_set(_VAC, "on")
    await house.advance(seconds=OCCUPANCY_TOGGLE_CONFIRM_SECONDS * 2)


@pytest.mark.parametrize("kind", ["unavailable", "unknown", "missing", "blip"])
async def test_glitching_vacation_toggle_sends_no_welcome_and_changes_nothing(make_house, kind) -> None:
    house = await _on_vacation(make_house)
    co = house.coordinator
    engine = co.automation_engine
    sent = _spy_notifications(engine)
    setpoint = house.thermostat.setpoint_f
    rows_mark = len(co._chart_log.raw_entries())
    restores_before = len(_restore_events(house))
    away_minutes = co._today_record.occupancy_away_minutes if co._today_record else None
    away_since = co._occupancy_away_since
    calls_mark = len(house.calls)

    await _glitch(house, kind)

    assert co._occupancy_mode == "vacation"
    assert engine._occupancy_mode == "vacation"
    assert co._occupancy_toggle_confirm_cancel is None, "a confirmation was left armed"
    assert not [k for k in sent if "welcome_home" in k], f"#1739: a welcome was sent on vacation: {sent}"
    assert house.thermostat.setpoint_f == pytest.approx(setpoint, abs=0.01), "the vacation setback moved"
    assert _occupancy_rows(house, rows_mark) == [], "a chart occupancy row for a glitch"
    assert len(_restore_events(house)) == restores_before, "a comfort restore for a glitch"
    assert co._occupancy_away_since == away_since, "the away clock was reset"
    if away_minutes is not None:
        assert co._today_record.occupancy_away_minutes == pytest.approx(away_minutes), "away minutes miscounted"
    writes = [c for c in house.calls[calls_mark:] if c.origin.startswith("ca:") and c.domain == "climate"]
    assert writes == [], f"a thermostat write (setback re-send or comfort restore) for a glitch: {writes}"

    # Control: a real "off" that holds past the window is an arrival: Home, and the welcome is sent.
    house.hass.states.async_set(_VAC, "off")
    await house.advance(seconds=OCCUPANCY_TOGGLE_CONFIRM_SECONDS - 10)
    assert co._occupancy_mode == "vacation", "the arrival acted before its 60 s confirmation"
    await house.advance(seconds=20)
    assert co._occupancy_mode == "home"
    assert [k for k in sent if "welcome_home" in k], f"control: the confirmed arrival sent no welcome: {sent}"
    assert len(_occupancy_rows(house, rows_mark)) == 1


async def test_a_chatty_toggle_does_not_hold_off_a_real_arrival(make_house) -> None:
    """A real "off" is acted on 60 s later even when the switch keeps sending updates (attributes, dropouts)."""
    house = await _on_vacation(make_house)
    co = house.coordinator
    sent = _spy_notifications(co.automation_engine)
    house.hass.states.async_set(_VAC, "off")
    for n in range(3):  # every 20 s: no new reading, so the confirmation keeps running
        await house.advance(seconds=20)
        if n == 1:
            house.hass.states.async_set(_VAC, "unavailable")
        else:
            house.hass.states.async_set(_VAC, "off", {"update": n})
    await house.advance(seconds=10)
    assert co._occupancy_mode == "home", "the arrival was held off by updates that carried no new reading"
    assert [k for k in sent if "welcome_home" in k], sent
