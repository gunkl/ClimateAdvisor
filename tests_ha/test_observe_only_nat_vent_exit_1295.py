"""Real-HA tests for Issue #1295: observe-only records no window pause for a nat-vent exit it never acted on.

Occupant story: automation is switched off (observe-only) and a window is open on a mild evening. Climate Advisor
"ventilates" on paper (in observe-only it sends nothing, so the thermostat keeps cooling). The outdoor air then warms
past the indoor air and that ventilation session ends with the window still open. Climate Advisor sends nothing,
which is right, but it used to record that it had paused the AC for the open window. The AC kept running into the
open window, and after automation was switched back on the pause guards read "already paused": nothing turned the
AC off until the window closed. Now no pause is recorded, and leaving observe-only pauses the AC for real within
about 30 s (rule 3).

Mirror of #1293 (``_pause_for_door_window_action`` / ``_suppress_hvac_for_whf``), on the nat-vent exit path
(``_exit_nat_vent`` -> ``_hold_thermostat_off_for_open_window``). Prove a test is not vacuous with
``scripts/revert_proof.py``.

Issue #1304 (owner decision 2026-11-03, "no paper sessions") changed the set-up: observe-only no longer starts a
session it never sent (the fan command returns ``DRY_RUN``), so the session is started for real with automation on and
automation is switched off afterwards. Its exit in observe-only is then not sent either: the fan really keeps running,
so the session is KEPT and nothing is recorded (no exit row, no pause). The #1295 coverage kept here: observe-only
records no window pause, and re-enabling ends the session and records the pause for real (rule 3), with nothing left
stuck when the window closes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60


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
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
    }


def _fan_running(house, fan: str) -> bool:
    if fan == "whf":
        return house.fan.is_on
    return house.thermostat.fan_mode == "on"


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_observe_only_nat_vent_exit_records_no_pause_and_reenable_pauses_for_real(make_house, fan) -> None:
    house = await make_house(**_mild_evening(fan))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: automation on starts a real session"
    assert _fan_running(house, fan), "precondition: the fan really runs"

    await house.set_automation(False)
    await house.advance(minutes=1)
    events = _record_events(house)
    calls_before = len(house.calls)
    house.set_outdoor(80.0, high_f=82.0, low_f=79.0)  # held warm: the scripted day would cool it again
    for _ in range(60):  # the outdoor reading is polled every 30 minutes; the exit then needs its 90 s sustain
        await house.advance(minutes=1)

    # Issue #1304: the outdoor-rise exit's fan stop is not sent, so the session (and the running fan) stay.
    assert engine._natural_vent_active, "observe-only ended on paper a session whose fan is still running"
    assert _fan_running(house, fan), "observe-only must not stop the fan"
    assert not [c for c in house.calls[calls_before:] if c.origin.startswith("ca:")], "observe-only sent a command"
    exit_rows = [n for n, _ in events if n.startswith("nat_vent_") and "exit" in n]
    assert not exit_rows, f"observe-only wrote an exit row for a stop it never sent: {exit_rows}"
    assert not engine._paused_by_door, "observe-only recorded a window pause it never sent"

    calls_before = len(house.calls)
    await house.set_automation(True)
    for _ in range(10):
        await house.advance(minutes=1)
        if not engine._natural_vent_active:
            break
    await house.advance(seconds=30)

    assert engine._natural_vent_active is False, "re-enable: the session must end for real"
    assert not _fan_running(house, fan), "re-enable: the fan must really stop"
    assert house.thermostat.mode == "off", "rule 3: the AC must not run into the open window"
    assert engine._paused_by_door, "the real pause must be recorded once the exit really ran"
    assert [c for c in house.calls[calls_before:] if c.origin.startswith("ca:")], "the stop was sent by CA"

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 120)
    assert house.thermostat.mode != "off", "closing the window must re-arm the HVAC (nothing left stuck)"
    assert not engine._paused_by_door
