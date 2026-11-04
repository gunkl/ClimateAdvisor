"""Real-HA tests for Issue #1306: in observe-only, a dashboard Resume clears the pause but starts no grace.

Occupant story: on a hot afternoon a window opens and Climate Advisor pauses the AC (the pause is recorded while
automation is on). Automation is then switched off. The occupant presses Resume on the dashboard. Climate Advisor is
observe-only, so it sends nothing and the AC stays off -- but it used to start a manual "dashboard_resume" grace and
show "resumed -- door/window override" as if it had turned the AC back on. While that grace ran, switching automation
back on with the window still open did not re-pause, and Climate Advisor believed the occupant's resumed AC was
running when it was not.

Owner decision (2026-11-03, "clear pause, no grace"): the Resume drops the pause belief (the occupant said they don't
want it), but no grace starts for a restore that was never sent. Switching automation back on then re-applies the
plan (#1296) with the window closed, or re-pauses within ~30 s with the window still open (#1293 reconcile). With
automation on, the Resume is unchanged: the AC comes back and the manual grace starts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.api import ClimateAdvisorResumeFromPauseView

pytestmark = pytest.mark.house

_DEBOUNCE_S = 60


def _hot_afternoon() -> dict:
    return {
        "fan": None,
        "profile": "scripted",
        "contacts": ["window"],
        "startup": "settled",
        "start": datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        "initial_indoor_f": 76.0,
        "outdoor_f": 92.0,
        "high_f": 95.0,
        "low_f": 70.0,
        "initial_hvac_mode": "cool",
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": _DEBOUNCE_S},
    }


def _ca_climate(house, since: int) -> list:
    return [c for c in house.calls[since:] if c.origin.startswith("ca:") and c.domain == "climate"]


def _grace_events_since(house, since_iso: str) -> list:
    """``grace_started`` rows logged at/after ``since_iso`` (by time, not index: the log is pruned)."""
    return [e for e in house.coordinator._event_log if e.get("type") == "grace_started" and e["time"] >= since_iso]


async def _press_dashboard_resume(house) -> dict:
    """The occupant presses Resume on the dashboard: the REAL API view, as the frontend calls it."""
    request = SimpleNamespace(
        app={"hass": house.hass},
        query={},
        method="POST",
        path="/api/climate_advisor/resume_from_pause",
        remote="t",
    )
    response = await ClimateAdvisorResumeFromPauseView().post(request)
    await house.advance(seconds=1)
    return json.loads(response.text)


async def _seconds(house, n: int) -> None:
    for _ in range(int(n)):
        await house.advance(seconds=1)


async def _paused_then_observe_only(house):
    """Automation on: the open window pauses the AC (recorded). Then automation is switched off."""
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.thermostat.mode == "cool", "precondition: the thermostat is cooling"
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"
    await house.set_automation(False)
    await house.advance(seconds=30)
    assert engine._paused_by_door, "precondition: the pause recorded with automation on is still believed"
    return engine


async def _observe_only_resume(house, engine) -> None:
    calls_before = len(house.calls)
    events_before = dt_util.now().isoformat()
    body = await _press_dashboard_resume(house)
    await house.advance(seconds=30)

    assert not engine._paused_by_door, "the occupant's Resume must drop the pause belief"
    assert house.thermostat.mode == "off", "observe-only: the AC is left as it is"
    assert not _ca_climate(house, calls_before), f"observe-only sent a restore: {_ca_climate(house, calls_before)!r}"
    assert not engine._grace_active, (
        "observe-only started a grace for a restore it never sent "
        f"(trigger={engine._last_grace_trigger!r}, source={engine._last_resume_source!r})"
    )
    assert not _grace_events_since(house, events_before), "an Activity row claims a grace started"
    assert not engine._resumed_from_pause, "the Status card would claim 'resumed -- door/window override'"
    assert "grace" not in body["message"].lower() and "HVAC set to" not in body["message"], body


async def test_observe_only_resume_clears_pause_then_reenable_with_window_closed_applies_plan(make_house) -> None:
    house = await make_house(**_hot_afternoon())
    engine = await _paused_then_observe_only(house)
    await _observe_only_resume(house, engine)

    await house.close_contact("window")
    await house.advance(seconds=_DEBOUNCE_S + 30)
    assert house.thermostat.mode == "off", "observe-only: still nothing sent"

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 30)
    assert house.thermostat.mode == "cool", "re-enabling automation must re-apply the cooling plan within ~30 s"
    assert _ca_climate(house, calls_before), "no thermostat command after re-enable"
    assert not engine._paused_by_door


async def test_observe_only_resume_then_reenable_with_window_still_open_repauses(make_house) -> None:
    house = await make_house(**_hot_afternoon())
    engine = await _paused_then_observe_only(house)
    await _observe_only_resume(house, engine)
    await house.advance(minutes=2)

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _seconds(house, 30)
    assert engine._paused_by_door, "re-enable with the window still open must re-pause within ~30 s"
    assert house.thermostat.mode == "off", "the AC was turned on into the open window"
    assert not [c for c in _ca_climate(house, calls_before) if c.data.get("hvac_mode") == "cool"]
    assert not engine._grace_active, "no grace may be left standing"


async def test_control_automation_on_resume_restores_and_starts_manual_grace(make_house) -> None:
    house = await make_house(**_hot_afternoon())
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"

    calls_before = len(house.calls)
    events_before = dt_util.now().isoformat()
    body = await _press_dashboard_resume(house)
    await house.advance(seconds=30)

    assert not engine._paused_by_door
    assert house.thermostat.mode == "cool", "automation on: the Resume restores the AC"
    assert _ca_climate(house, calls_before)
    assert engine._grace_active and engine._last_resume_source == "manual"
    assert engine._last_grace_trigger == "dashboard_resume"
    assert engine._resumed_from_pause
    assert [e for e in _grace_events_since(house, events_before) if e.get("trigger") == "dashboard_resume"]
    assert "Manual grace period started" in body["message"], body
