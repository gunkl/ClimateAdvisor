"""Real-HA tests for Issue #1305: observe-only records no "AC/heat restored" for a restore it never sent.

Occupant story: a cool morning, the window is open and Climate Advisor ventilates with the whole-house fan, holding
the AC Off. The house cools to the ventilation target and the fan cycles off (the session stays open). Then the
occupant switches automation off, and later closes the window. Climate Advisor sends nothing (observe-only), but the
Activity Report used to say "AC/heat restored (fan session ended)" although the thermostat still read Off. Now no
restore row is recorded; switching automation back on re-applies the plan and the AC returns for real.

Mirror of #1293/#1295/#1301, on ``_deactivate_fan``'s stranded-suppression branch (fan already inactive,
``_pre_fan_hvac_mode`` still set). Prove a test is not vacuous with ``scripts/revert_proof.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house


def _restore_events(house) -> list:
    return [e for e in house.coordinator._event_log if e.get("type") == "stranded_hvac_suppression_restored"]


def _ca_climate(calls) -> list:
    return [c for c in calls if c.origin.startswith("ca:") and c.domain == "climate"]


async def _cycled_off_whf_session(make_house):
    """WHF nat-vent session (AC held Off) whose fan has cycled off at the target; the session stays open."""
    house = await make_house(
        fan="whf",
        contacts=["window"],
        startup="settled",
        profile="scripted",
        start=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        initial_indoor_f=72.0,
        outdoor_f=65.0,
        high_f=70.0,
        low_f=55.0,
        initial_hvac_mode="cool",
        ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    )
    ae = house.coordinator.automation_engine
    for _ in range(36):
        await house.advance(seconds=10)  # past the 300 s start-up window
    await house.open("window")
    await house.advance(seconds=90)
    for _ in range(35):
        await house.advance(seconds=10)  # past the 5-minute fan toggle floor
    assert ae._fan_active and ae._pre_fan_hvac_mode == "cool", "precondition: the fan holds the AC Off"
    indoor = 72.0
    while ae._fan_active and indoor > 70.0:
        indoor -= 0.25
        house.set_indoor(indoor)
        for _ in range(6):
            await house.advance(seconds=10)
    assert not ae._fan_active and ae._natural_vent_active, "precondition: the fan cycled off, session still open"
    assert ae._pre_fan_hvac_mode == "cool", "precondition: the fan session still owns the AC"
    assert house.thermostat.mode == "off"
    return house, ae


async def test_observe_only_window_close_records_no_restore_and_reenable_restores(make_house) -> None:
    house, ae = await _cycled_off_whf_session(make_house)
    await house.set_automation(False)
    await house.advance(seconds=30)
    calls_before = len(house.calls)

    await house.close_contact("window")
    for _ in range(12):
        await house.advance(seconds=10)

    assert not ae._natural_vent_active, "precondition: the window close ended the session"
    assert not _ca_climate(house.calls[calls_before:]), "observe-only sent a thermostat command"
    assert house.thermostat.mode == "off", "observe-only: the thermostat stays where the fan session left it"
    assert not _restore_events(house), "observe-only recorded a restore it never sent"
    # The session is over, so the fan no longer owns the AC (nothing would release it later: #618).
    assert ae._pre_fan_hvac_mode is None and not ae._whf_owns_hvac()

    # G3/G4: one deliberate action (automation back on) settles to the normal plan.
    await house.set_automation(True)
    for _ in range(30):
        await house.advance(seconds=1)
    assert house.thermostat.mode == "cool", "re-enable must give the AC back (plan re-applied)"


async def test_control_automation_on_window_close_restores_and_records(make_house) -> None:
    house, ae = await _cycled_off_whf_session(make_house)
    calls_before = len(house.calls)

    await house.close_contact("window")
    for _ in range(12):
        await house.advance(seconds=10)

    modes = [c.data.get("hvac_mode") for c in _ca_climate(house.calls[calls_before:]) if c.service == "set_hvac_mode"]
    assert "cool" in modes, "automation on: the AC is restored"
    assert house.thermostat.mode == "cool"
    events = _restore_events(house)
    assert len(events) == 1 and events[0]["restore_mode"] == "cool"
    assert ae._pre_fan_hvac_mode is None
