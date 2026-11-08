"""Issue #1420: a scenario's pre_cool / wakeup / bedtime event is the coordinator's own timer, not a second call.

Production has one timer per schedule event (the daily wake_time / sleep_time timers, the overnight pre-cool
trigger).  A coordinator-backed session runs those timers for real, so the event in the same minute must only
move the clock; calling the engine handler as well wrote the setpoint twice, a write production never makes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from tools.sim_harness.run_production import FakeHassSession, run_production_scenario
from tools.sim_harness.scenario_model import daily_schedule_fires_at, record_pre_cool_fires
from tools.sim_harness.session import drive_scenario

ROOT = Path(__file__).resolve().parents[1] / "tools" / "simulations"
PRE_COOL_1417 = ROOT / "golden" / "hot_night_same_mode_pre_cool_wakeup_keeps_cool_1417.json"
WAKE_1194E = ROOT / "pending" / "remote_offset_1194e_sleep_window_entry_starts_at_zero_and_exit_leaves_no_offset.json"


def _setpoints(result, day_prefix: str) -> list[tuple[str, float]]:
    return [
        (str(a["ts"]), float(a["data"]["temperature"]))
        for a in result.action_log
        if a.get("service") == "set_temperature" and str(a["ts"]).startswith(day_prefix)
    ]


def test_coordinator_pre_cool_event_is_the_timer_one_write() -> None:
    """The coordinator's 02:30 pre-cool timer writes 70 once; the 02:30 event does not write it again."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    scenario = json.loads(PRE_COOL_1417.read_text(encoding="utf-8"))
    original = ClimateAdvisorCoordinator.__dict__["_async_pre_cool_trigger"]
    session = FakeHassSession(scenario, use_coordinator=True)
    result = drive_scenario(session, scenario)
    assert _setpoints(result, "2026-07-17 02:") == [("2026-07-17 02:30:00+00:00", 70.0)]
    assert session.coordinator._sim_pre_cool_fired_at == [datetime(2026, 7, 17, 2, 30, tzinfo=UTC)]
    assert ClimateAdvisorCoordinator.__dict__["_async_pre_cool_trigger"] is original  # recorder removed on close


def test_engine_only_pre_cool_event_still_calls_the_handler() -> None:
    """No coordinator, no timer: the event is the only pre-cool, as before."""
    scenario = json.loads(PRE_COOL_1417.read_text(encoding="utf-8"))
    result = run_production_scenario(scenario, use_coordinator=False)
    assert _setpoints(result, "2026-07-17 02:") == [("2026-07-17 02:30:00+00:00", 70.0)]


def test_coordinator_wakeup_event_is_the_wake_timer_one_write() -> None:
    """06:30 wake timer writes the day band once; the 06:30:05 wakeup event used to write it a second time."""
    scenario = json.loads(WAKE_1194E.read_text(encoding="utf-8"))
    result = run_production_scenario(scenario, use_coordinator=True)
    assert [v for _, v in _setpoints(result, "2026-11-03 06:30")] == [68.0]


def test_daily_schedule_uses_the_coordinators_time_and_default() -> None:
    at_2230 = datetime(2026, 7, 16, 22, 30, 5, tzinfo=UTC)
    assert daily_schedule_fires_at(SimpleNamespace(config={}), "bedtime", at_2230)  # production default 22:30
    assert not daily_schedule_fires_at(SimpleNamespace(config={"sleep_time": "22:00"}), "bedtime", at_2230)
    assert daily_schedule_fires_at(
        SimpleNamespace(config={"wake_time": "06:30"}), "wakeup", datetime(2026, 7, 17, 6, 30, tzinfo=UTC)
    )
    assert not daily_schedule_fires_at(None, "bedtime", at_2230)  # engine-only: no timer
    assert not daily_schedule_fires_at(SimpleNamespace(config={}), "pre_cool", at_2230)


def test_overlapping_recorders_restore_the_original_in_any_order() -> None:
    class _Coord:
        async def _async_pre_cool_trigger(self, now):  # pragma: no cover - never awaited here
            return now

    original = _Coord.__dict__["_async_pre_cool_trigger"]
    first, second = record_pre_cool_fires(_Coord), record_pre_cool_fires(_Coord)
    first.start()
    second.start()
    first.stop()
    assert _Coord.__dict__["_async_pre_cool_trigger"] is not original
    second.stop()
    assert _Coord.__dict__["_async_pre_cool_trigger"] is original
