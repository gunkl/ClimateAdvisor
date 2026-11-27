"""Issue #1730 phase 4: the self-measurement inside the real coordinator and the real setup/unload path.

Occupant view: nothing visible. The Debug card shows what Climate Advisor costs Home Assistant; the loop-lag timer runs
from the first zone's setup to the last zone's unload (or Home Assistant stopping) and never leaks past it.
"""

from __future__ import annotations

import json
import time

import pytest

from custom_components.climate_advisor import self_measure as sm
from tests.test_self_measure_1730 import FakeLoop
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import (
    async_unload_entry_like_ha,
    build_headless_coordinator,
    build_headless_multi_zone,
    setup_entry_like_ha,
)
from tools.sim_harness.fake_hass import FakeHass


def _burn(ms: float) -> int:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass
    return 3


def test_executor_job_records_its_thread_cpu_under_a_stable_name() -> None:
    coord, _hass, _sched, _ev = build_headless_coordinator(skip_startup_coalesce=True)

    async def go():
        import functools

        return await coord._executor_job(functools.partial(_burn, 20.0))

    assert run_coro(go()) == 3  # the result is untouched
    stat = coord._perf.jobs["test_self_measure_wiring_1730._burn"]
    assert stat.count == 1 and 15.0 <= stat.max_ms < 400.0


def test_the_zone_label_still_reaches_the_executor_function() -> None:
    """The timing wrapper sits inside ``bind_zone_for_executor``: #812's zone tagging is unchanged."""
    from custom_components.climate_advisor import log_capture

    coord, _hass, _sched, _ev = build_headless_coordinator(skip_startup_coalesce=True)
    seen = {}

    def probe() -> None:
        seen["zone"] = log_capture._current_zone_label.get()

    async def go():
        with log_capture.zone_scope("zone-x"):
            await coord._executor_job(probe)

    run_coro(go())
    assert seen["zone"] == "zone-x"


def test_a_coordinator_without_the_measurement_still_runs_jobs() -> None:
    """Tests (and tools) that build a coordinator with ``object.__new__`` have no ``_perf``: no wrapper, no error."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord, _hass, _sched, _ev = build_headless_coordinator(skip_startup_coalesce=True)
    del coord._perf
    assert not hasattr(coord, "_perf")

    async def go():
        return await ClimateAdvisorCoordinator._executor_job(coord, lambda: 5)

    assert run_coro(go()) == 5
    assert coord.performance_debug()["timeline_build"] == coord._timeline_cpu.as_dict()  # never raises


def test_the_timeline_build_keeps_last_max_and_ewma_of_its_thread_cpu() -> None:
    from tests.test_operation_timeline_wiring import _build

    coord, _hass, scheduler = _build()
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        coord._timeline_shadow_run("occupancy")  # a forced second run
    cpu = coord._timeline_cpu
    assert cpu.count == 2 and cpu.max_ms >= cpu.last_ms > 0.0 and cpu.ewma_ms > 0.0
    assert coord._perf.loop_costs["timeline build"].count == 2  # the install-wide judge sees it too


def test_the_debug_payload_carries_the_performance_section_in_a_json_able_shape() -> None:
    from tests.test_operation_timeline_wiring import _build

    coord, _hass, scheduler = _build()
    with scheduler.installed():
        coord._timeline_shadow_run("cycle")
        state = coord.get_debug_state()
    perf = json.loads(json.dumps(state["performance"]))
    assert set(perf) == {
        "executor_jobs",
        "loop_costs",
        "loop_lag",
        "overloaded",
        "over_minutes",
        "episodes",
        "budgets_ms",
        "timeline_build",
    }
    assert (
        set(perf["timeline_build"]) == {"count", "last_ms", "max_ms", "ewma_ms"}
        and perf["timeline_build"]["count"] == 1
    )
    assert perf["overloaded"] is False and perf["loop_lag"]["running"] is False  # a headless run has no loop timer


def test_a_failing_snapshot_never_breaks_the_debug_payload(monkeypatch) -> None:
    from tests.test_operation_timeline_wiring import _build

    coord, _hass, _scheduler = _build()

    def boom() -> dict:
        raise RuntimeError("x")

    monkeypatch.setattr(coord._perf, "snapshot", boom)
    assert coord.get_debug_state()["performance"] == {}


# --- lifecycle: start on setup, cancel on unload / HA stop, one probe for all zones -----------------------------------
@pytest.fixture
def fake_loop(monkeypatch) -> FakeLoop:
    loop = FakeLoop()
    monkeypatch.setattr(FakeHass, "loop", loop, raising=False)
    return loop


def _zones(n: int = 2):
    zones, hass, scheduler = build_headless_multi_zone(n)
    return [z["coordinator"] for z in zones.values()], [z["entry"] for z in zones.values()], hass, scheduler


def test_the_probe_starts_with_the_first_zone_and_stops_with_the_last(fake_loop) -> None:
    coords, entries, hass, scheduler = _zones(2)
    perf = sm.for_hass(hass)
    assert coords[0]._perf is coords[1]._perf is perf  # one probe per Home Assistant, not per zone
    assert perf.running and len(fake_loop.live) == 1
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(hass, entries[0]))
        assert perf.running and len(fake_loop.live) == 1  # a zone is still loaded
        run_coro(async_unload_entry_like_ha(hass, entries[1]))
    assert not perf.running and fake_loop.live == []  # cancelled: no timer outlives the last zone


def test_unload_twice_never_drops_another_zones_reference(fake_loop) -> None:
    """HA runs ``async_shutdown`` twice per unload; a double release would cancel the probe under the other zone."""
    coords, entries, hass, scheduler = _zones(2)
    perf = sm.for_hass(hass)
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(hass, entries[0]))
        run_coro(coords[0].async_shutdown())  # a third call
    assert perf.running and len(fake_loop.live) == 1


def test_home_assistant_stop_releases_the_zone_once_and_unload_afterwards_does_not_release_again(fake_loop) -> None:
    coords, entries, hass, scheduler = _zones(2)
    perf = sm.for_hass(hass)
    with scheduler.installed():
        coords[0]._async_homeassistant_stop(None)
        assert perf.running  # zone 2 still holds it
        run_coro(async_unload_entry_like_ha(hass, entries[0]))  # the mirror path after the stop event
        assert perf.running and len(fake_loop.live) == 1
        coords[1]._async_homeassistant_stop(None)
    assert not perf.running and fake_loop.live == []


def test_a_reload_starts_the_probe_again_and_leaves_one_timer(fake_loop) -> None:
    coords, entries, hass, scheduler = _zones(1)
    with scheduler.installed():
        run_coro(async_unload_entry_like_ha(hass, entries[0]))
        assert fake_loop.live == []
        new_coord = setup_entry_like_ha(hass, scheduler, entries[0])
    assert new_coord._perf is sm.for_hass(hass) and new_coord._perf.running
    assert len(fake_loop.live) == 1
