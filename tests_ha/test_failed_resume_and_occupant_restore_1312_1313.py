"""Real-HA tests for Issues #1312 and #1313: two more restores that did not land are re-sent (#1308's chain).

#1312 occupant story: a window paused the AC, and the occupant presses Resume on the dashboard. The thermostat
refuses the restore (a cloud blip). Climate Advisor used to start the manual "dashboard_resume" grace anyway and
reply "Manual grace period started", so the AC stayed off for the whole grace while Climate Advisor believed it had
been restored. Owner decision 2026-11-03 ("same as #1308"): the pause stays cleared (the occupant asked for that), the
restore is re-sent on the bounded 15/60/300 s chain, and the manual grace starts only once the restore lands.

#1313 (retired by Issue #1325): the occupant raising the setpoint on a door-paused (Off) thermostat is no override at
all (a thermostat that is Off takes no setpoint), so there is no occupant mode to restore and re-send; see
tests_ha/test_setpoint_while_off_ignored_1325.py.

The failure is made at the device: the simulated thermostat raises for a while (a cloud blip).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor.api import ClimateAdvisorResumeFromPauseView

pytestmark = pytest.mark.house

SIM_DOMAIN = "ca_dev_thermostat_sim"
_DEBOUNCE_S = 60

# #1306's hot afternoon: an open window pauses the AC.
_HOT = {
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


def _refuse_mode(house, monkeypatch, mode: str, *, for_s: float | None) -> list[int]:
    sim = house.hass.data[SIM_DOMAIN][house.sim_entry.entry_id]["thermostat_entity"]
    real = sim.async_set_hvac_mode
    refused = [0]
    first: list = []

    async def _set(hvac_mode):
        if str(hvac_mode) == mode:
            now = dt_util.utcnow()
            if not first:
                first.append(now)
            if for_s is None or (now - first[0]).total_seconds() < for_s:
                refused[0] += 1
                raise HomeAssistantError("thermostat cloud unreachable")
        await real(hvac_mode)

    real_temp = sim.async_set_temperature

    async def _set_temp(**kwargs):
        # A cloud blip refuses a setpoint write that carries the refused mode too (the band write sends one).
        if str(kwargs.get("hvac_mode")) == mode and first:
            now = dt_util.utcnow()
            if for_s is None or (now - first[0]).total_seconds() < for_s:
                refused[0] += 1
                raise HomeAssistantError("thermostat cloud unreachable")
        await real_temp(**kwargs)

    monkeypatch.setattr(sim, "async_set_hvac_mode", _set)
    monkeypatch.setattr(sim, "async_set_temperature", _set_temp)
    return refused


def _tee_events(house) -> list:
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    events: list = []

    def _tee(event_type: str, data: dict) -> None:
        events.append((event_type, dict(data)))
        if forward:
            forward(event_type, data)

    engine._emit_event_callback = _tee
    return events


def _graces(events, trigger: str) -> list:
    return [d for t, d in events if t == "grace_started" and d.get("trigger") == trigger]


def _rows(events) -> list:
    return [d for t, d in events if t == "hvac_mode_not_applied" and d.get("kind") == "restore_retry"]


def _ca_mode_writes(house, since: int, mode: str) -> list:
    return [
        c
        for c in house.calls[since:]
        if c.origin.startswith("ca:") and c.service == "set_hvac_mode" and c.data.get("hvac_mode") == mode
    ]


async def _press_dashboard_resume(house) -> dict:
    request = SimpleNamespace(
        app={"hass": house.hass}, query={}, method="POST", path="/api/climate_advisor/resume_from_pause", remote="t"
    )
    response = await ClimateAdvisorResumeFromPauseView().post(request)
    return json.loads(response.text)


async def _seconds_until_mode(house, mode: str, limit_s: int) -> int | None:
    for t in range(limit_s):
        if house.thermostat.mode == mode:
            return t
        await house.advance(seconds=1)
    return None


async def _hot_paused(make_house):
    house = await make_house(**_HOT)
    engine = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await house.open("window")
    await house.advance(seconds=_DEBOUNCE_S + 60)
    assert engine._paused_by_door and house.thermostat.mode == "off", "precondition: the window paused the AC"
    return house, engine


# --- #1312: dashboard Resume -----------------------------------------------------------------------------------


async def test_failed_resume_restore_is_resent_and_manual_grace_waits(make_house, monkeypatch, caplog) -> None:
    house, engine = await _hot_paused(make_house)
    events = _tee_events(house)
    refused = _refuse_mode(house, monkeypatch, "cool", for_s=13)

    body = await _press_dashboard_resume(house)
    assert refused[0] >= 1, "precondition: the Resume's restore was refused"
    assert not engine._paused_by_door, "the occupant's Resume clears the pause"
    assert not engine._grace_active and not _graces(events, "dashboard_resume"), "no grace before the restore lands"
    assert "grace period started" not in body["message"].lower(), f"the reply claims a grace: {body}"
    assert body.get("restore_pending") is True, body

    for _ in range(30):
        await house.advance(seconds=1)
        assert engine._pending_restore is None or not engine._paused_by_door, "re-paused while the Resume was pending"
        if _graces(events, "dashboard_resume"):
            break
    # (A later re-pause by the next cycle during the dashboard_resume grace is a separate, pre-existing behavior;
    # this test pins the Resume's restore landing and its grace, not what the cycle does afterwards.)
    assert len(_graces(events, "dashboard_resume")) == 1, "the AC did not come back within ~30 s of the Resume"
    assert engine._grace_active and engine._last_resume_source == "manual", "the manual grace starts once it lands"
    assert engine._pending_restore is None and engine._restore_resend_cancel is None, "nothing pending (G2)"
    assert len(_rows(events)) == 1, "G5: one Activity row"
    assert "re-sending the restore" in caplog.text


async def test_control_resume_without_failure_starts_the_grace_at_once(make_house) -> None:
    house, engine = await _hot_paused(make_house)
    events = _tee_events(house)
    body = await _press_dashboard_resume(house)
    await house.advance(seconds=1)
    assert house.thermostat.mode == "cool"
    assert len(_graces(events, "dashboard_resume")) == 1 and engine._grace_active
    assert "Manual grace period started" in body["message"]
    assert engine._pending_restore is None and not _rows(events)
