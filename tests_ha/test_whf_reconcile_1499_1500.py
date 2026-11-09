"""Real-HA tests for Issues #1499 and #1500: the fan reconcile never invents a whole-house fan run.

Occupant stories (whole_house_fan mode):

* #1500, command-only fan (no state feedback): the house is closed up and the AC is cooling with the thermostat
  blower set to "on". When the AC reaches its setpoint the thermostat reports cooling -> fan (its blower keeps
  running). The fan reconcile read that blower as the whole-house fan, so Climate Advisor sent a turn-off to a fan it
  had already turned off, logged a nat-vent exit and armed the reactivation lockout -- after every such AC cycle.
  With automation off the same reading left the fan "running" on paper: card "active", a false "fan running with the
  house closed up" alert. Now, with no reading of the fan, the reconcile reads Climate Advisor's own record.
* #1499, observe-only: a fan Climate Advisor does not own is found running without warrant while automation is off.
  The reconcile set ``_fan_active`` True for its stop, the stop came back ``DRY_RUN`` (#1304 keeps the flags), and
  the flag stayed set. Now observe-only sends nothing and records nothing for a fan CA does not own; on re-enable
  the WHF safety check stops the fan on a sealed house as before.

Prove a test is not vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.components.climate import FAN_ON, HVACAction

from tests_ha.house.builder import SIM_DOMAIN

pytestmark = pytest.mark.house

_FALSE_FAN_ROWS = ("nat_vent_reconcile_exit", "fan_deactivated", "whf_sealed_observe_notified")


def _closed_warm_evening(fan: str) -> dict:
    return {
        "fan": fan,
        "profile": "scripted",
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 7, 15, 21, 0, tzinfo=UTC),
        "initial_indoor_f": 78.0,
        "outdoor_f": 85.0,
        "high_f": 92.0,
        "low_f": 72.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0},
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


def _fan_commands(house, since: int) -> list:
    return [c for c in house.calls[since:] if c.origin.startswith("ca:") and c.domain == "fan"]


def _thermostat_reports_its_running_blower(house, monkeypatch) -> None:
    """A real thermostat in cool mode reports ``fan`` while its blower runs with the compressor off (the generated
    days model this, #1500's nightly day). The dev sim reports ``idle`` there; this test models the real reading."""
    ent = house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]
    cls = type(ent)
    base = cls.hvac_action.fget

    def _action(self):
        action = base(self)
        return HVACAction.FAN if action == HVACAction.IDLE and self._fan_mode == FAN_ON else action

    monkeypatch.setattr(cls, "hvac_action", property(_action))


async def _ac_cycle_ends_with_blower_on(house) -> None:
    """The occupant sets the blower on; the AC cools to its setpoint and the thermostat reports cooling -> fan."""
    assert house.thermostat.action == "cooling", "precondition: the AC is cooling"
    await house.advance(minutes=6)  # past the 300 s start-up window
    await house.occupant_sets_fan_mode("on")
    await house.advance(minutes=6)  # past the 30 s fresh-command guard and the toggle floor
    house.set_indoor(70.0)  # well below any cool setpoint: the compressor stops, the blower runs on
    await house.advance(minutes=2)
    assert house.thermostat.action == "fan", "precondition: the thermostat reports its blower (cooling -> fan)"


@pytest.mark.parametrize("automation_on", [True, False], ids=["automation_on", "observe_only"])
async def test_command_only_blower_is_not_read_as_the_whole_house_fan(
    make_house, monkeypatch, automation_on: bool
) -> None:
    house = await make_house(**_closed_warm_evening("whf_command_only"))
    _thermostat_reports_its_running_blower(house, monkeypatch)
    engine = house.coordinator.automation_engine
    if not automation_on:
        await house.set_automation(False)
    events = _record_events(house)
    calls_before = len(house.calls)
    notes_before = len(house.notifications)

    await _ac_cycle_ends_with_blower_on(house)

    assert house.thermostat.fan_mode == "on", "precondition: the occupant's blower is on"
    assert not house.fan.is_on, "precondition: the whole-house fan is off"
    assert engine._fan_active is False, "the blower was taken for the whole-house fan (stale 'running' record)"
    assert not _fan_commands(house, calls_before), "a turn-off was sent to a whole-house fan that is already off"
    rows = [n for n, _ in events if n in _FALSE_FAN_ROWS]
    assert not rows, f"an Activity row for a whole-house fan run that never happened: {rows}"
    assert len(house.notifications) == notes_before, "a 'fan running with the house closed up' alert went out"
    assert str(house.coordinator._compute_whf_status()).startswith("inactive"), "the fan card says the fan runs"


async def _unowned_fan_found_running(house, *, automation_on: bool) -> tuple[list, int]:
    """A whole-house fan CA does not own runs on a sealed house; the 30-minute backstop's reconcile finds it."""
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    if not automation_on:
        await house.set_automation(False)
    real_override = engine.handle_fan_manual_override
    engine.handle_fan_manual_override = lambda *a, **k: None  # its start went unseen (no override on record)
    try:
        await house.occupant_presses_fan(True)
    finally:
        engine.handle_fan_manual_override = real_override
    if not automation_on:
        # Observe-only: the fan runs on (the sealed-house check only notifies). With automation on that check stops it
        # ~30 s after the press, so the control reconciles straight away.
        await house.advance(minutes=6)
    assert house.fan.is_on and engine._fan_active is False and not engine._fan_override_active, "precondition"

    events = _record_events(house)
    calls_before = len(house.calls)
    await engine.reconcile_fan_on_startup(
        indoor=78.0, outdoor=85.0, thermostat_fan_running=True, any_sensor_open=False, trigger="backstop_30min"
    )
    await house.advance(seconds=1)
    return events, calls_before


async def test_observe_only_reconcile_records_nothing_for_a_fan_it_does_not_own(make_house) -> None:
    house = await make_house(**_closed_warm_evening("whf"))
    engine = house.coordinator.automation_engine
    lockout_before = engine._nat_vent_outdoor_exit_time
    events, calls_before = await _unowned_fan_found_running(house, automation_on=False)

    assert engine._fan_active is False, "observe-only left the reconcile's temporary 'CA owns the fan' flag set"
    assert not _fan_commands(house, calls_before), "observe-only sent a fan command"
    rows = [n for n, _ in events if n in _FALSE_FAN_ROWS]
    assert not rows, f"observe-only wrote a row for a stop it did not send: {rows}"
    assert engine._nat_vent_outdoor_exit_time == lockout_before, "observe-only armed the #790 lockout"
    assert engine._last_unwarranted_fan_correction_at is None, "observe-only stamped the #446 cooldown"

    await house.set_automation(True)
    await house.advance(seconds=60)
    assert not house.fan.is_on, "after re-enable the WHF safety check must still stop the fan on a sealed house"


async def test_control_automation_on_reconcile_still_stops_the_unwarranted_fan(make_house) -> None:
    house = await make_house(**_closed_warm_evening("whf"))
    events, calls_before = await _unowned_fan_found_running(house, automation_on=True)

    assert _fan_commands(house, calls_before), "automation on: the reconcile's stop is unchanged"
    assert "nat_vent_reconcile_exit" in [n for n, _ in events]
    await house.advance(seconds=30)
    assert not house.fan.is_on


def _open_cool_evening() -> dict:
    return {
        "fan": "whf",
        "profile": "scripted",
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 7, 15, 21, 0, tzinfo=UTC),
        "initial_indoor_f": 78.0,
        "outdoor_f": 62.0,
        "high_f": 88.0,
        "low_f": 58.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 60},
    }


async def test_mirror_observe_only_reconcile_does_not_adopt_a_fan_as_a_paper_session(make_house) -> None:
    """Mirror of #1499: the adopt branch (fan running, nat-vent warranted) in observe-only."""
    house = await make_house(**_open_cool_evening())
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.set_automation(False)
    await house.open("window")
    await house.advance(minutes=2)
    real_override = engine.handle_fan_manual_override
    engine.handle_fan_manual_override = lambda *a, **k: None  # its start went unseen (no override on record)
    try:
        await house.occupant_presses_fan(True)
    finally:
        engine.handle_fan_manual_override = real_override
    await house.advance(minutes=6)
    assert house.fan.is_on and not engine._fan_active and not engine._natural_vent_active, "precondition"

    events = _record_events(house)
    calls_before = len(house.calls)
    await engine.reconcile_fan_on_startup(
        indoor=78.0, outdoor=62.0, thermostat_fan_running=True, any_sensor_open=True, trigger="backstop_30min"
    )
    await house.advance(seconds=1)

    assert not _fan_commands(house, calls_before)
    assert engine._natural_vent_active is False, "observe-only recorded a nat-vent session it never started"
    assert engine._fan_active is False, "observe-only adopted the fan as Climate Advisor's"
    rows = [n for n, _ in events if n in ("fan_activated", "nat_vent_started")]
    assert not rows, f"observe-only wrote a start row for a session it did not start: {rows}"
