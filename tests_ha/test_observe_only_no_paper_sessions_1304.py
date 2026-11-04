"""Real-HA tests for Issue #1304: observe-only records no ventilation session it never started or ended.

Occupant story: automation is switched off (observe-only). A window is open on a mild evening, so Climate Advisor
would ventilate -- but it sends nothing, and the fan stays off. Before, it still recorded a ventilation session
"on paper" (the fan command reported EXECUTED without being sent); the Activity record then showed sessions starting
and ending, and the ODE ceiling guard wrote "Nat-vent escalated to AC -- nat-vent ended, AC armed" while the fan and
thermostat never changed. Owner decision (2026-11-03, "no paper sessions"): a fan command in observe-only returns
``FanCommandResult.DRY_RUN`` and every caller treats it as "nothing happened".

The other half keeps the house picture right: a session that REALLY started while automation was on keeps running
after automation is switched off (its fan is still on), so it stays recorded until it really ends.

Prove a test is not vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60
_START_ROWS = ("nat_vent_soft_start_entered", "fan_activated", "nat_vent_fan_on")


def _mild_evening(fan: str) -> dict:
    return {
        "fan": fan,
        "profile": "scripted",
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
        "initial_indoor_f": 73.0,
        "outdoor_f": 66.0,
        "high_f": 68.0,
        "low_f": 62.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S, "comfort_cool": 74.0},
    }


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


def _ca_calls(calls) -> list:
    return [c for c in calls if c.origin.startswith("ca:")]


def _ca_fan_calls(calls) -> list:
    return [
        c
        for c in _ca_calls(calls)
        if c.domain in ("fan", "switch") or (c.domain == "climate" and c.service == "set_fan_mode")
    ]


def _fan_running(house, fan: str) -> bool:
    if fan == "whf":
        return house.fan.is_on
    return house.thermostat.fan_mode == "on"


def _start_rows(events) -> list:
    return [
        (n, d)
        for n, d in events
        if n in _START_ROWS or (n == "sensor_opened" and d.get("result") == "natural_ventilation")
    ]


async def _real_session(house, fan: str) -> None:
    """Automation ON: the window opens and Climate Advisor really starts a ventilation session."""
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: automation on starts a real session"
    assert _fan_running(house, fan), "precondition: the fan really runs"


@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
@pytest.mark.parametrize("automation_on", [False, True], ids=["observe_only", "control_automation_on"])
async def test_window_open_starts_no_paper_session(make_house, fan, automation_on) -> None:
    house = await make_house(**_mild_evening(fan))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    if not automation_on:
        await house.set_automation(False)
        await house.advance(minutes=1)
    events = _record_events(house)
    calls_before = len(house.calls)

    await house.open("window")
    await house.advance(minutes=15)  # debounce, then several re-check ticks

    new_calls = house.calls[calls_before:]
    if automation_on:
        # Control: unchanged -- the session starts, the fan command goes out, the start row is written.
        assert engine._natural_vent_active
        assert _ca_fan_calls(new_calls), "automation on: the fan-on command must be sent"
        assert _fan_running(house, fan)
        assert _start_rows(events), "automation on: the ventilation start is recorded"
        return
    assert engine._natural_vent_active is False, "observe-only recorded a ventilation session it never started"
    assert not _ca_fan_calls(new_calls), "observe-only sent a fan command"
    assert not _fan_running(house, fan), "the fan must stay off"
    assert not _start_rows(events), f"observe-only wrote a ventilation start row: {_start_rows(events)}"


@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_real_session_window_close_in_observe_only_is_kept_and_ends_after_reenable(make_house, fan) -> None:
    """Every window closes while observe-only: the fan really keeps running, so the session stays recorded and no exit
    row is written. Re-enabling ends it for real: the WHF safety check stops a whole-house fan on the sealed house
    (re-enable trigger, within seconds); the blower's closed-window exit retry ends its session within 5 minutes."""
    house = await make_house(**_mild_evening(fan))
    engine = house.coordinator.automation_engine
    await _real_session(house, fan)

    await house.set_automation(False)
    await house.advance(minutes=1)
    events = _record_events(house)
    calls_before = len(house.calls)
    await house.close_contact("window")
    await house.advance(minutes=20)

    assert engine._natural_vent_active, "observe-only ended on paper a session whose fan is still running"
    assert _fan_running(house, fan), "observe-only must not stop the fan"
    assert not _ca_calls(house.calls[calls_before:]), "observe-only sent a command"
    exit_rows = [n for n, _ in events if n.startswith("nat_vent_") and "exit" in n]
    assert not exit_rows, f"observe-only wrote an exit row for a stop it never sent: {exit_rows}"

    await house.set_automation(True)
    for _ in range(7):
        await house.advance(minutes=1)
        if not engine._natural_vent_active:
            break
    assert engine._natural_vent_active is False, "re-enable: the session must end for real"
    assert not _fan_running(house, fan), "re-enable: the fan must really stop behind the closed windows"
    assert not engine._paused_by_door, "nothing is open: no pause"
    await house.advance(minutes=2)
    assert house.thermostat.mode == "cool", "the plan (cool at the 74 F ceiling) resumes, nothing left stuck"
