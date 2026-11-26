"""Issue #1722: when a worker may start (Home Assistant stopping) and how many cycle refit workers run at once.

Occupant view: a Home Assistant stop or restart must not begin a fresh multi-second fit just as the house is going
down (the stop-time save is waiting, and the worker would be killed a moment later); and a house with several zones must
not start one refit interpreter per zone at the same moment on a small board (the startup refit runs in every zone).

Real coordinator methods; ``process_offload.async_run_job`` is spied.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.climate_advisor import process_offload
from tests.test_cycle_wiring_1512 import _heat_cycle, _Rig
from tests.test_process_offload_wiring_1722 import Spy, _model_result, _spy
from tests.test_v4_shadow_wiring import _build, _fake_model_dict, _record, _seed_cycle_inputs
from tests.v4_fake_predictor import FakePredictor, install
from tools.sim_harness._loop import run_coro


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePredictor:
    return install(monkeypatch)


# ---------------------------------------------------------------------------
# every worker start path refuses once Home Assistant is stopping
# ---------------------------------------------------------------------------


def test_v4_fit_does_not_start_once_ha_is_stopping(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    spy = _spy(monkeypatch, v4_fit=_model_result(coord), v4_night_replay=_record())
    coord._ha_stopping = True  # what EVENT_HOMEASSISTANT_STOP sets (``_async_homeassistant_stop``)
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is False
        scheduler.advance_by(0)
    assert spy.calls == [] and coord._v4_fit_running is False and getattr(coord, "_v4_fit_task", None) is None
    coord._ha_stopping = False  # control: the same call starts a worker when HA is running
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
        scheduler.advance_by(0)
    assert spy.jobs[0] == "v4_fit"


def test_night_replay_does_not_start_once_ha_is_stopping(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    spy = _spy(monkeypatch, v4_night_replay=_record())
    coord._ha_stopping = True

    async def scenario() -> None:
        coord._spawn = lambda coro, background=False: asyncio.ensure_future(coro)
        assert coord._v4_start_night_replay("night_replay") is False
        assert coord._v4_night_replay_running is False and coord._v4_night_replay_task_handle is None
        coord._ha_stopping = False  # control
        assert coord._v4_start_night_replay("night_replay") is True
        await asyncio.wait_for(coord._v4_night_replay_task_handle, 5)

    with scheduler.installed():
        run_coro(scenario())
    assert spy.jobs == ["v4_night_replay"]  # only the control started one


def test_cycle_refit_does_not_start_once_ha_is_stopping(tmp_path) -> None:
    rig = _Rig(tmp_path)
    try:
        rig.c._ha_stopping = True
        rig.c._cycle_spawn_refit(startup=True)
        assert rig.tasks == []  # nothing scheduled
        rig.c._ha_stopping = False  # control
        rig.c._cycle_spawn_refit(startup=True)
        assert len(rig.tasks) == 1
    finally:
        rig.close()


def test_a_cycle_closed_during_the_stop_is_stored_but_not_refitted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The other refit start path: ``_cycle_collect`` -> ``_cycle_process`` of a just-closed cycle."""
    rig = _Rig(tmp_path)
    try:
        spy = Spy(cycle_refit=RuntimeError("must not start"))
        monkeypatch.setattr(process_offload, "async_run_job", spy)
        rig.c._ha_stopping = True
        _heat_cycle(rig)  # the cycle closes and is processed
        assert len(rig.c._cycle_store.cycles("heat")) == 1
        assert spy.calls == [], "a refit worker was started while Home Assistant was stopping"
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# one cycle refit worker at a time across zones
# ---------------------------------------------------------------------------


def test_two_zones_cycle_refit_workers_run_one_after_the_other(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "z1").mkdir()
    (tmp_path / "z2").mkdir()
    rig1 = _Rig(tmp_path / "z1")
    _heat_cycle(rig1)
    rig1.patch.stop()  # each rig patches the coordinator module's clock; one at a time
    rig2 = _Rig(tmp_path / "z2")
    try:
        _heat_cycle(rig2)
        assert rig1.c._cycle_store.cycles("heat") and rig2.c._cycle_store.cycles("heat")
        rig2.c.hass = rig1.c.hass  # one Home Assistant, two zones
        real = process_offload.async_run_job
        state: dict[str, Any] = {"running": 0, "max": 0, "done": 0}

        async def slow(job: str, payload: dict, **kw: Any):
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
            try:
                await asyncio.sleep(0.05)  # both refits are waiting on the loop, as two zones' startup refits are
                return await real(job, payload, **{**kw, "mode": "thread"})
            finally:
                state["running"] -= 1
                state["done"] += 1

        monkeypatch.setattr(process_offload, "async_run_job", slow)

        async def both() -> None:
            await asyncio.gather(rig1.c._cycle_refit("heat", 1e9), rig2.c._cycle_refit("heat", 1e9))

        asyncio.run(both())
        assert state["done"] == 2
        assert state["max"] == 1, "two cycle refit workers ran at once"
    finally:
        rig2.close()
