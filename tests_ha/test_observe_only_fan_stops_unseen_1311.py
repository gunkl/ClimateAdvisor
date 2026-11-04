"""Real-HA tests for Issue #1311: observe-only records a fan that stopped by itself as stopped.

Occupant story: automation is switched off (observe-only) while a whole-house-fan ventilation session that started
with automation on is running. The fan stops by itself and Climate Advisor misses the fan-off event. The fan reconcile
then sees the fan physically off -- but in observe-only ``_deactivate_fan()`` returns ``DRY_RUN`` before its bookkeeping
(#1304), so Climate Advisor kept believing the fan was running and kept the "fan owns the HVAC" note. After automation
was switched back on, its next fan start was skipped as "already running" while the fan was off: no free cooling.

Now the reconcile records what it observed (``_fan_active`` False) and releases the ownership the way #1305 decided
(released, the restore is not sent and writes no row). Nothing is sent. With automation on nothing changes.
Prove a test is not vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.climate_advisor.automation import FanCommandResult

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60


def _mild_evening() -> dict:
    return {
        "fan": "whf",
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


def _record_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    seen: list = []

    def _tee(event_type: str, data: dict) -> None:
        seen.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee
    return seen


async def _fan_stops_unseen(house, *, automation_on: bool) -> tuple[list, int]:
    """A real session runs; (observe-only:) automation goes off; the fan stops by itself, its off event is missed;
    the fan reconcile then observes it off (the 30-minute backstop's call)."""
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active and house.fan.is_on, "precondition: a real session runs the fan"
    assert engine._whf_owns_hvac(), "precondition: the fan session owns the HVAC (it turned the AC off)"
    if not automation_on:
        await house.set_automation(False)
    await house.advance(minutes=6)  # past the toggle floor and the 30 s fresh-command guard

    real_off = engine.on_fan_turned_off
    engine.on_fan_turned_off = lambda *a, **k: None  # the fan-off event is missed
    try:
        await house.occupant_presses_fan(False)  # the fan stops by itself (timer run-out, power blip...)
    finally:
        engine.on_fan_turned_off = real_off
    assert not house.fan.is_on
    assert engine._fan_active, "precondition: the stop went unseen"

    events = _record_events(house)
    calls_before = len(house.calls)
    await engine.reconcile_fan_on_startup(
        indoor=73.0, outdoor=66.0, thermostat_fan_running=False, any_sensor_open=True, trigger="backstop_30min"
    )
    await house.advance(seconds=1)
    return events, calls_before


async def test_observe_only_unseen_fan_stop_is_recorded_and_restart_works_after_reenable(make_house) -> None:
    house = await make_house(**_mild_evening())
    engine = house.coordinator.automation_engine
    events, calls_before = await _fan_stops_unseen(house, automation_on=False)

    assert engine._fan_active is False, "observe-only: the fan stopped but is still recorded as running"
    assert not engine._whf_owns_hvac(), "observe-only: a stopped fan must not keep owning the HVAC"
    assert engine._natural_vent_active is False
    assert not [c for c in house.calls[calls_before:] if c.origin.startswith("ca:")], "observe-only sent a command"
    false_rows = [n for n, _ in events if n in ("fan_deactivated", "stranded_hvac_suppression_restored")]
    assert not false_rows, f"observe-only wrote a row for something it did not do: {false_rows}"

    await house.set_automation(True)
    result = await engine._activate_fan(reason="test: next start after re-enable")
    assert result is not FanCommandResult.ALREADY_IN_STATE, "the next start was skipped as 'already running'"
    await house.advance(seconds=30)
    assert house.fan.is_on, "after re-enable the fan must really start again"


async def test_control_automation_on_unseen_fan_stop_unchanged(make_house) -> None:
    house = await make_house(**_mild_evening())
    engine = house.coordinator.automation_engine
    events, _ = await _fan_stops_unseen(house, automation_on=True)

    assert engine._fan_active is False
    assert not engine._whf_owns_hvac()
    assert "fan_deactivated" in [n for n, _ in events], "automation on: the reconcile's fan-off row is unchanged"
