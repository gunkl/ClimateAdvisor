"""The operation timeline in shadow inside the REAL coordinator (Issue #1230 R3a).

Occupant view: the timeline is computed beside the plan on the real house and shown only in the debug payload. It must
command nothing (no thermostat or fan call), survive any error silently, regenerate when someone leaves home, and be
cheap enough to run every cycle. Runs the real coordinator headless (FakeHass + FakeScheduler).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_advisor import operation_timeline as ot
from tests.timeline_fixtures import classification
from tools.sim_harness.build_coordinator import build_headless_coordinator

_START = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)


def _hourly(start: datetime, hours: int = 60) -> list[dict]:
    first = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return [
        {"datetime": (first + timedelta(hours=h)).isoformat(), "temperature": 50.0 + (h % 24) * 0.5}
        for h in range(hours)
    ]


def _build(*, classified: bool = True):
    coord, fake_hass, scheduler, _events = build_headless_coordinator(
        start_time=_START,
        skip_startup_coalesce=True,
        climate_attributes={"current_temperature": 69, "temperature": 68},
    )
    coord._hourly_forecast_temps = _hourly(scheduler.now())
    coord.automation_engine._thermal_model = {
        "k_passive": -0.03,
        "k_active_heat": 5.5,
        "k_active_cool": -6.0,
        "observation_count_heat": 80,
        "observation_count_cool": 80,
    }
    if classified:
        coord._current_classification = classification("mild")
        coord.automation_engine._current_classification = coord._current_classification
    return coord, fake_hass, scheduler


def test_a_run_fills_the_debug_payload_with_the_contract_dict_and_the_cost() -> None:
    coord, _hass, scheduler = _build()
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        debug = coord.get_debug_state()["operation_timeline"]
    assert debug["shadow"] is False and debug["schema_version"] == 1 and debug["steps"]
    assert json.loads(json.dumps(debug))["zone"] == coord._entry_id
    assert debug["last_run"]["regenerated"] is True and debug["last_run"]["ms"] < 1000
    assert ot.from_dict(debug) is not None
    assert "detail" in debug and "candidates" in debug["detail"]


def test_the_shadow_commands_nothing() -> None:
    coord, fake_hass, scheduler = _build()
    before = len(fake_hass.action_log) if hasattr(fake_hass, "action_log") else 0
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        coord._timeline_shadow_run("occupancy")
    assert (len(fake_hass.action_log) if hasattr(fake_hass, "action_log") else 0) == before


def test_no_classification_yet_means_no_timeline_and_no_error() -> None:
    coord, _hass, scheduler = _build(classified=False)
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        debug = coord.timeline_debug()
    assert debug["shadow"] is False and "steps" not in debug


def test_an_engine_event_marks_the_plan_stale_and_the_tick_regenerates_it() -> None:
    coord, _hass, scheduler = _build()
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        first = coord._timeline_last["generation"]
        coord._timeline_note_event("some_unrelated_event")
        assert coord._timeline_dirty is None
        coord._timeline_note_event("override_confirmed")
        assert coord._timeline_dirty == "override_confirmed"
        coord.automation_engine._manual_override_active = True
        coord._timeline_tick()
    assert coord._timeline_dirty is None
    assert coord._timeline_last["trigger"] == "override_confirmed"
    assert coord._timeline_last["regenerated"] is True and coord._timeline_last["generation"] == first + 1


def test_an_occupancy_trigger_forces_a_regeneration() -> None:
    coord, _hass, scheduler = _build()
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        coord._timeline_shadow_run("cycle")
        assert coord._timeline_last["regenerated"] is False
        coord._timeline_shadow_run("occupancy")
    assert coord._timeline_last["regenerated"] is True and "occupancy" in coord._timeline_last["reasons"]


def test_a_failure_is_swallowed_logged_once_at_warning_then_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    coord, _hass, scheduler = _build()
    monkeypatch.setattr(coord, "_timeline_snapshot", lambda c: 1 / 0)
    caplog.set_level(logging.DEBUG)
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        coord._timeline_shadow_run("cycle")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "operation timeline" in r.message]
    assert len(warnings) == 1
    assert coord._timeline_failed_logged is True


def test_a_partially_built_coordinator_simply_has_no_shadow() -> None:
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    bare = object.__new__(ClimateAdvisorCoordinator)
    bare._timeline_shadow_run("cycle")  # no latch attribute: nothing happens, nothing raises
    bare._timeline_note_event("override_confirmed")
    bare._timeline_tick()
