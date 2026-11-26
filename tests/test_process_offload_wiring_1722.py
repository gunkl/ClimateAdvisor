"""Issue #1722: the coordinator runs its four heavy fits in a worker process, and a failed worker changes nothing.

Occupant view: the nightly v4 fit, its night replay, the display-node fit and the heat/cool cycle refit used to hold
the Python GIL for seconds on a Raspberry Pi, freezing Home Assistant (lights, dashboards) while they ran. They now run
in a child process. What must not change: the model and rates learned are the same, a worker that fails or times out
keeps the previous model / rates exactly as an exception in the old executor job did, and unloading the integration
stops a running replay (killing its worker).

Real coordinator methods (the ``tests/test_v4_shadow_wiring.py`` harness; the ``tests/test_cycle_wiring_1512.py``
rig for the cycle refit). ``process_offload.async_run_job`` is spied: the job name, the payload type, and that the
payload was built through ``_executor_job`` (never on the loop).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from custom_components.climate_advisor import cycle_model, night_replay, process_offload, v4_shadow
from custom_components.climate_advisor.const import (
    WORKER_TIMEOUT_CYCLE_REFIT_S,
    WORKER_TIMEOUT_V4_DISPLAY_FIT_S,
    WORKER_TIMEOUT_V4_FIT_S,
    WORKER_TIMEOUT_V4_NIGHT_REPLAY_S,
)
from custom_components.climate_advisor.process_offload import ProcessJobError
from tests.test_cycle_wiring_1512 import _heat_cycle, _Rig
from tests.test_v4_shadow_real_engine import ENGINE, _history
from tests.test_v4_shadow_wiring import _build, _fake_model_dict, _record, _seed_cycle_inputs
from tests.v4_fake_predictor import FakePredictor, install
from tools.sim_harness._loop import run_coro


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakePredictor:
    return install(monkeypatch)


class Spy:
    """Replaces ``process_offload.async_run_job``: records every call; returns a canned result or raises it."""

    def __init__(self, **by_job: Any) -> None:
        self.by_job = by_job
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, job: str, payload: dict, *, executor_job: Any, logger: Any, timeout_s: float, mode=None):
        self.calls.append({"job": job, "payload": payload, "timeout_s": timeout_s, "logger": logger})
        result = self.by_job[job]
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def jobs(self) -> list[str]:
        return [c["job"] for c in self.calls]


def _spy(monkeypatch: pytest.MonkeyPatch, **by_job: Any) -> Spy:
    spy = Spy(**by_job)
    monkeypatch.setattr(process_offload, "async_run_job", spy)
    return spy


class Executor:
    """Wraps ``coord._executor_job``: records what is submitted and knows when code runs INSIDE the executor."""

    def __init__(self, coord: Any) -> None:
        self.submitted: list[Any] = []
        self.active = 0
        real = coord._executor_job

        async def wrapped(fn: Any, *args: Any):
            self.submitted.append(fn)
            self.active += 1
            try:
                out = real(fn, *args)
                return await out if hasattr(out, "__await__") else out
            finally:
                self.active -= 1

        coord._executor_job = wrapped

    def guard(self, monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> list[bool]:
        """Wrap ``module.name`` (a payload builder): each call records whether it ran inside the executor."""
        real = getattr(module, name)
        ran_in_executor: list[bool] = []

        def wrapper(*a: Any, **kw: Any):
            ran_in_executor.append(self.active > 0)
            return real(*a, **kw)

        monkeypatch.setattr(module, name, wrapper)
        return ran_in_executor


def _model_result(coord, **over: Any) -> list:
    return [_fake_model_dict(coord, fitted_at="2026-11-27T03:15:00+00:00", **over), 1.5, "fit line"]


# ---------------------------------------------------------------------------
# each site runs its job through process_offload; the payload is built in the executor
# ---------------------------------------------------------------------------


def test_v4_fit_runs_as_a_worker_job_with_its_payload_built_in_the_executor(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    spy = _spy(monkeypatch, v4_fit=_model_result(coord), v4_night_replay=_record())
    ex = Executor(coord)
    built = ex.guard(monkeypatch, v4_shadow, "fit_job_payload")
    with scheduler.installed():
        assert coord._v4_start_fit("nightly") is True
        scheduler.advance_by(0)
    assert built == [True], "the payload (row windowing, snapshot prefetch) must be built inside the executor"
    assert spy.jobs[0] == "v4_fit"
    assert spy.calls[0]["timeout_s"] == WORKER_TIMEOUT_V4_FIT_S
    assert spy.calls[0]["logger"] is logging.getLogger("custom_components.climate_advisor.coordinator")
    payload = spy.calls[0]["payload"]
    assert payload["primary"] == coord.config["climate_entity"] and payload["chart_entries"]
    json.dumps(payload)  # data only: it goes down a pipe
    # and the stored model is the worker's result, unpacked as before
    assert coord._v4_store.v4_model["fitted_at"] == "2026-11-27T03:15:00+00:00"
    assert coord._v4_last_fit["ok"] is True and coord._v4_last_fit["seconds"] == 1.5


def test_v4_display_fit_runs_as_a_worker_job(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    stored = _fake_model_dict(coord)
    coord._v4_store.v4_model = stored
    widened = dict(stored, sensors=[*stored["sensors"], "sensor.bedroom"])
    spy = _spy(monkeypatch, v4_display_fit=[widened, 0.4, "display line"])
    ex = Executor(coord)
    built = ex.guard(monkeypatch, v4_shadow, "display_fit_job_payload")
    now = scheduler.now()
    with scheduler.installed():
        run_coro(
            coord._v4_run_display_fit(now, coord.config["climate_entity"], ["sensor.bedroom"], (), ["22:30", "06:30"])
        )
    assert built == [True]
    assert spy.jobs == ["v4_display_fit"] and spy.calls[0]["timeout_s"] == WORKER_TIMEOUT_V4_DISPLAY_FIT_S
    assert spy.calls[0]["payload"]["sleep_window"] == ["22:30", "06:30"]
    assert coord._v4_store.v4_model == widened  # the worker's list unpacked to (model, seconds, line)


def test_v4_night_replay_runs_as_a_worker_job(fake: FakePredictor, monkeypatch: pytest.MonkeyPatch) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    spy = _spy(monkeypatch, v4_night_replay=_record())
    ex = Executor(coord)
    built = ex.guard(monkeypatch, night_replay, "nightly_job_payload")
    with scheduler.installed():
        run_coro(coord._v4_run_night_replay("nightly"))
    assert built == [True]
    assert spy.jobs == ["v4_night_replay"] and spy.calls[0]["timeout_s"] == WORKER_TIMEOUT_V4_NIGHT_REPLAY_S
    assert coord._v4_store.night_validation == _record()


def test_the_sleep_rule_travels_as_data_only_with_a_sleep_sensor(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    coord.config.update({"sleep_time": "22:30", "wake_time": "06:30"})
    coord.config.pop("sleep_indoor_temp_entity", None)
    assert coord._v4_node_inputs()["sleep_window"] is None  # no sleep sensor: no predicate, as before
    coord.config["sleep_indoor_temp_entity"] = "sensor.bedroom"
    assert coord._v4_node_inputs()["sleep_window"] == ["22:30", "06:30"]


def test_cycle_refit_runs_as_a_worker_job_and_weights_are_applied_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rig = _Rig(tmp_path)
    try:
        _heat_cycle(rig)  # a closed cycle in the store (the refit that ran inside already used the real worker path)
        store = rig.c._cycle_store
        cid = store.cycles("heat")[0]["id"]
        order: list[str] = []
        real_set = store.set_weights
        monkeypatch.setattr(store, "set_weights", lambda w: order.append("set_weights") or real_set(w))
        real = process_offload.async_run_job
        calls: list[tuple[str, float]] = []

        async def spy(job: str, payload: dict, **kw: Any):
            calls.append((job, kw["timeout_s"]))
            order.append("job")
            assert sorted(payload) == ["cycles", "mode", "now_ts", "snap"]
            return await real(job, payload, **kw)

        monkeypatch.setattr(process_offload, "async_run_job", spy)
        ex = Executor(rig.c)
        built = ex.guard(monkeypatch, cycle_model, "refit_job_payload")
        asyncio.run(rig.c._cycle_refit("heat", 1e9))
        assert built == [True]
        assert calls == [("cycle_refit", WORKER_TIMEOUT_CYCLE_REFIT_S)]
        assert order == ["job", "set_weights"]  # the weights are applied after the job returns
        assert store.cycles("heat")[0]["id"] == cid
        assert rig.c._cycle_refit_task is None  # cleared once the refit is over
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# a failed worker (ProcessJobError) is handled exactly as an exception in the old executor job
# ---------------------------------------------------------------------------


def test_failed_v4_fit_worker_keeps_the_previous_model_and_records_the_failure(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    previous = _fake_model_dict(coord, fitted_at="2026-11-14T03:15:00+00:00")
    coord._v4_store.v4_model = dict(previous)
    _spy(monkeypatch, v4_fit=ProcessJobError("Worker job v4_fit timed out after 900 s"))
    with scheduler.installed():
        assert coord._v4_start_fit("nightly")
        scheduler.advance_by(0)
    assert coord._v4_store.v4_model == previous
    assert coord._v4_last_fit["ok"] is False and coord._v4_last_fit["reason"] == "nightly"
    assert coord._v4_store.last_fit_error == "fit failed; see the log"
    assert coord._v4_fit_running is False  # the next night can try again
    assert any("keeping the previous model" in r.getMessage() for r in caplog.records)


def test_failed_v4_fit_worker_does_not_hold_the_domain_fit_lock(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    _seed_cycle_inputs(coord, scheduler)
    _spy(monkeypatch, v4_fit=ProcessJobError("killed"))
    with scheduler.installed():
        assert coord._v4_start_fit("nightly")
        scheduler.advance_by(0)
    assert coord._v4_fit_lock().locked() is False


def test_failed_display_fit_worker_keeps_the_stored_model(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _hass, scheduler = _build()
    stored = _fake_model_dict(coord)
    coord._v4_store.v4_model = stored
    coord._v4_store.last_fit = {"ok": True, "reason": "nightly"}
    _spy(monkeypatch, v4_display_fit=ProcessJobError("exited with code 1"))
    with scheduler.installed():
        run_coro(coord._v4_run_display_fit(scheduler.now(), coord.config["climate_entity"], [], (), None))
    assert coord._v4_store.v4_model is stored
    assert coord._v4_store.last_fit == {"ok": True, "reason": "nightly"}  # a display failure is not a fit failure
    assert any("v4 display-node fit failed; keeping the stored model" in r.getMessage() for r in caplog.records)


def test_failed_night_replay_worker_keeps_the_previous_record_and_logs_once(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    coord._v4_store.night_validation = _record(generated="old")
    _spy(monkeypatch, v4_night_replay=ProcessJobError("timed out"))
    with scheduler.installed(), caplog.at_level(logging.DEBUG):
        run_coro(coord._v4_run_night_replay("nightly"))
        run_coro(coord._v4_run_night_replay("nightly"))
    assert coord._v4_store.night_validation["generated"] == "old"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "night strategy replay" in r.message]
    assert len(warnings) == 1  # the second failure is DEBUG, as before


def test_failed_cycle_refit_worker_keeps_the_previous_rates(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _Rig(tmp_path)
    try:
        _heat_cycle(rig)  # a fit exists now
        cache = rig.c.learning._state.thermal_model_cache
        before = json.dumps(cache, sort_keys=True, default=str)
        weights_before = [(c["id"], c.get("w")) for c in rig.c._cycle_store.cycles("heat")]
        _spy(monkeypatch, cycle_refit=ProcessJobError("Worker job cycle_refit timed out after 600 s"))
        calls: list[Any] = []
        monkeypatch.setattr(rig.c._cycle_store, "set_weights", lambda w: calls.append(w))
        caplog.set_level(logging.WARNING)
        rig.c._cycle_last_refit.clear()
        rig.c._cycle_spawn_refit(startup=True)
        rig.drain()
        assert json.dumps(cache, sort_keys=True, default=str) == before  # nothing applied
        assert calls == []  # and no weights from a job that never returned
        assert [(c["id"], c.get("w")) for c in rig.c._cycle_store.cycles("heat")] == weights_before
        assert any(
            "Cycle model update failed; the previous heating/cooling rates stay" in r.getMessage()
            for r in caplog.records
        )
        assert rig.c._cycle_refit_task is None
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# unload
# ---------------------------------------------------------------------------


def test_unload_cancels_a_running_night_replay_and_its_worker(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    state: dict[str, Any] = {"cancelled": False}

    async def hang(job: str, payload: dict, **_kw: Any):
        state["started"].set()
        try:
            await asyncio.Event().wait()  # a worker that never answers
        except asyncio.CancelledError:
            state["cancelled"] = True  # process_offload kills and reaps the child here
            raise

    monkeypatch.setattr(process_offload, "async_run_job", hang)

    async def scenario() -> None:
        state["started"] = asyncio.Event()
        coord._spawn = lambda coro, background=False: asyncio.ensure_future(coro)  # the harness hass keeps no handle
        assert coord._v4_start_night_replay("night_replay") is True
        task = coord._v4_night_replay_task_handle
        assert task is not None and coord._v4_night_replay_running is True
        await asyncio.wait_for(state["started"].wait(), 5)
        coord._v4_cancel_fit()  # what async_shutdown calls
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert task.cancelled()
        assert coord._v4_night_replay_task_handle is None and coord._v4_night_replay_running is False

    with scheduler.installed():
        run_coro(scenario())
    assert state["cancelled"] is True


def test_the_replay_handle_is_cleared_when_the_replay_finishes(
    fake: FakePredictor, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord, _hass, scheduler = _build()
    coord._v4_store.v4_model = _fake_model_dict(coord)
    _seed_cycle_inputs(coord, scheduler)
    _spy(monkeypatch, v4_night_replay=_record())

    async def scenario() -> None:
        coord._spawn = lambda coro, background=False: asyncio.ensure_future(coro)
        assert coord._v4_start_night_replay("night_replay") is True
        task = coord._v4_night_replay_task_handle
        await asyncio.wait_for(task, 5)
        assert coord._v4_night_replay_task_handle is None

    with scheduler.installed():
        run_coro(scenario())


def test_unload_cancels_a_running_cycle_refit_so_the_cycle_save_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``_persist_cycles`` waits on the cycle lock the refit holds; a worker refit can hold it for minutes."""
    rig = _Rig(tmp_path)
    try:
        _heat_cycle(rig)
        state: dict[str, Any] = {"cancelled": False}

        async def hang(job: str, payload: dict, **_kw: Any):
            state["started"].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        monkeypatch.setattr(process_offload, "async_run_job", hang)

        async def scenario() -> None:
            state["started"] = asyncio.Event()
            rig.c._cycle_last_refit.clear()
            rig.c._spawn = lambda coro: asyncio.ensure_future(coro)
            rig.c._cycle_spawn_refit(startup=True)
            await asyncio.wait_for(state["started"].wait(), 5)
            task = rig.c._cycle_refit_task
            assert task is not None and not task.done()
            rig.c._cycle_cancel_refit()  # async_shutdown and the HA-stop save call this first
            await asyncio.wait_for(rig.c._persist_cycles(), 5)  # would wait out the refit's timeout otherwise
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled() and rig.c._cycle_refit_task is None

        asyncio.run(scenario())
        assert state["cancelled"] is True
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# a real child process end to end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(ENGINE is None, reason="predictor_v4 not available in this tree")
def test_real_process_mode_fit_stores_the_same_model_as_thread_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v4_shadow, "_predictor", lambda: ENGINE)

    def fit_once(mode: str) -> tuple[dict, dict]:
        coord, _hass, scheduler = _build()
        _history(coord, days=6)
        monkeypatch.setattr(process_offload, "default_mode", mode)
        with scheduler.installed():
            assert coord._v4_start_fit("first")
            scheduler.advance_by(0)
        assert coord._v4_last_fit["ok"] is True, coord._v4_last_fit
        return coord._v4_store.v4_model, coord._v4_last_fit

    spawned_by_job: dict[str, int] = {}
    real_job = process_offload.async_run_job
    real_exec = asyncio.create_subprocess_exec
    current: list[str] = []

    async def counting_exec(*a: Any, **kw: Any):
        spawned_by_job[current[-1]] = spawned_by_job.get(current[-1], 0) + 1
        return await real_exec(*a, **kw)

    async def tracking_job(job: str, payload: dict, **kw: Any):
        current.append(job)
        try:
            return await real_job(job, payload, **kw)
        finally:
            current.pop()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    monkeypatch.setattr(process_offload, "async_run_job", tracking_job)
    thread_model, _ = fit_once("thread")
    assert spawned_by_job == {}  # thread mode starts no child
    process_model, last = fit_once("process")
    assert spawned_by_job.get("v4_fit") == 1, f"the v4 fit did not run in a worker process: {spawned_by_job}"
    assert process_model is not None and process_model.get("fit") is not None
    assert process_model == thread_model  # the worker learned exactly what the in-process fit learned
    assert last["reason"] == "first"
