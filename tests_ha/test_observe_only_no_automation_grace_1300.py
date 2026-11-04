"""Real-HA tests for Issue #1300: observe-only starts no automation grace for a restore it never sent.

Occupant story: automation is switched off (observe-only). A window closes, or a ventilation session ends because the
window closed. Climate Advisor sends no restore, which is right, but it used to start its own 5-minute "automation"
grace as if it had just turned the heat/AC back on. For those minutes it acted as though it had made a change it
never made, and the occupant's own thermostat change in that window was read against that grace. Now no grace starts
in observe-only; with automation on the grace starts exactly as before.

Same "state recorded for a DRY_RUN command" family as #1293/#1295. Sites: the window-close restore
(``_restore_comfort_after_door_window_close``), ``_exit_nat_vent``'s sensors-closed branch and the slow-loop
comfort-floor exit (``check_natural_vent_conditions``), all through
``_start_automation_grace_unless_restore_unsent``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60


def _hot_afternoon() -> dict:
    return {
        "fan": None,
        "profile": "scripted",
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 7, 15, 20, 0, tzinfo=UTC),  # 13:00 local-ish: no scheduled routine nearby
        "initial_indoor_f": 76.0,
        "outdoor_f": 92.0,
        "high_f": 95.0,
        "low_f": 70.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
    }


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


def _automation_grace(engine) -> bool:
    return bool(engine._grace_active) and engine._last_resume_source == "automation"


async def _paused_for_open_window(house):
    """Automation on: open the window on a hot afternoon -> the AC is paused for real (the pause is recorded)."""
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.thermostat.mode == "cool", "precondition: the thermostat is cooling"
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"
    return engine


async def test_observe_only_window_close_starts_no_automation_grace(make_house) -> None:
    house = await make_house(**_hot_afternoon())
    engine = await _paused_for_open_window(house)

    await house.set_automation(False)
    await house.advance(seconds=30)
    calls_before = len(house.calls)
    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    ca_climate = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "climate"]
    assert not ca_climate, f"observe-only sent a restore: {ca_climate!r}"
    assert house.thermostat.mode == "off", "observe-only: the thermostat is left as it is"
    assert not _automation_grace(engine), (
        "observe-only started an automation grace for a restore it never sent "
        f"(trigger={engine._last_grace_trigger!r}, source={engine._last_resume_source!r})"
    )

    # The occupant turns the AC on themselves right after: observe-only treats it as their change, with no
    # Climate Advisor grace standing in front of it.
    await house.occupant_sets_thermostat(mode="cool", setpoint_f=74.0)
    await house.advance(seconds=30)
    assert house.thermostat.mode == "cool", "the occupant's change must stand"
    assert not _automation_grace(engine), "an automation grace appeared after the occupant's change"


async def test_automation_on_window_close_still_starts_automation_grace(make_house) -> None:
    """Control: with automation on the restore is sent and the 5-minute automation grace starts as before."""
    house = await make_house(**_hot_afternoon())
    engine = await _paused_for_open_window(house)

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert house.thermostat.mode == "cool", "the AC must come back when the window closes"
    assert _automation_grace(engine), "automation on: the post-restore automation grace must still start"
    assert engine._last_grace_trigger == "sensor_closed_resume"


@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_observe_only_nat_vent_exit_on_window_close_starts_no_automation_grace(make_house, fan) -> None:
    # Issue #1304 ("no paper sessions"): observe-only no longer starts a session it never sent, so the session is
    # started for real with automation on, then automation is switched off before the window closes. The close's
    # fan stop comes back DRY_RUN: the fan really keeps running, so the session is kept (no paper exit) -- and no
    # restore and no automation grace either, which is this test's #1300 point.
    house = await make_house(**_mild_evening(fan))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: a real nat-vent session is running"
    for _ in range(35):
        await house.advance(seconds=10)  # past the 5-minute fan toggle floor: the close's stop must not be deferred
    await house.set_automation(False)
    await house.advance(minutes=1)

    calls_before = len(house.calls)
    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert engine._natural_vent_active, "observe-only sent no stop: the fan runs on, so the session is kept (#1304)"
    ca_climate = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "climate"]
    assert not ca_climate, f"observe-only sent a restore: {ca_climate!r}"
    assert not _automation_grace(engine), (
        "observe-only started a nat_vent_exit_resume grace for a restore it never sent "
        f"(trigger={engine._last_grace_trigger!r})"
    )


@pytest.mark.parametrize("fan", ["hvac_fan", "whf"])
async def test_automation_on_nat_vent_exit_on_window_close_still_starts_grace(make_house, fan) -> None:
    """Control for the nat-vent sensors-closed branch."""
    house = await make_house(**_mild_evening(fan))
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._natural_vent_active, "precondition: a nat-vent session is running"
    for _ in range(35):
        await house.advance(seconds=10)

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)

    assert engine._natural_vent_active is False, "precondition: the window close ended the session"
    assert _automation_grace(engine), "automation on: the nat-vent exit resume grace must still start"
    assert engine._last_grace_trigger == "nat_vent_exit_resume"
