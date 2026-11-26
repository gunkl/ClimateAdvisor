"""Worker processes on real Home Assistant (Issue #1722): a refit really runs in a child, and unload kills it.

Occupant view: the cycle refit and the v4 fits run in a separate process so Home Assistant stays responsive. Saving
settings (a reload) or removing a zone while one runs must not hang and must not leave a stray process behind.

This tier runs worker jobs in a thread by default (``tests_ha/conftest.py``: a child process is invisible to the
simulated clock). These tests switch ``process_offload.default_mode`` to ``"process"`` and, for the same reason, hold
the clock still: they await the job directly, or poll with REAL sleeps, and never ``house.advance()`` while a worker
runs. The settle helper cannot see a subprocess pipe, so every wait here is on the process itself.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.climate_advisor import process_offload
from tests_ha.test_cycle_wiring_realha_1512 import _FINE_SENSOR, _HOUSE, _seed_fine_sensor, _slices

pytestmark = pytest.mark.house

_REAL_SLEEP = time.sleep  # freezegun never patches sleep
_UNLOAD_BUDGET_S = 8.0  # real seconds; a kill and reap takes about 0.05 s, the job's own sleep is 60 s

_SLEEPER = '''\
"""A job that outlives the test unless the parent kills it."""
import time


def sleeper(payload):
    time.sleep(60)
    return None
'''


def _process_mode(monkeypatch) -> None:
    monkeypatch.setattr(process_offload, "default_mode", "process")


def _real_now() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


async def _wait_real(predicate, what: str, limit_s: float = 30.0) -> None:
    """Wait for ``predicate`` in REAL time while the simulated clock stays frozen (loop turns + blocking sleeps)."""
    deadline = _real_now() + limit_s
    while not predicate():
        assert _real_now() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0)
        _REAL_SLEEP(0.005)


@pytest.fixture
def spawned(monkeypatch):
    """Every child the production code starts (the real ``create_subprocess_exec`` is kept). Does NOT switch the mode:
    a test builds its house in this tier's thread mode first, then calls :func:`_process_mode`."""
    procs: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    yield procs
    for proc in procs:  # a failing test must not leave a 60 s sleeper behind
        if proc.returncode is None:
            proc.kill()


@pytest.fixture
def sleeping_job(monkeypatch, tmp_path: Path):
    """Register a job module that sleeps 60 s as the named allowlisted job (the test-only registry)."""
    (tmp_path / "ha_sleeper_1722.py").write_text(_SLEEPER)
    saved_path = list(process_offload.sys.path)

    def register(job: str) -> None:
        process_offload._register_job_for_tests(job, "ha_sleeper_1722", "sleeper", str(tmp_path))

    yield register
    process_offload._TEST_JOBS.clear()
    process_offload._TEST_PATHS.clear()
    process_offload.sys.path[:] = saved_path


async def _house_with_cycles(make_house):
    house = await make_house(
        **{
            **_HOUSE,
            "ca_config": {"indoor_temp_source": "sensor", "indoor_temp_entity": _FINE_SENSOR, "primary_engine": "v3"},
            "before_ca_setup": _seed_fine_sensor,
        }
    )
    await _slices(house, 4 * 120)  # four hours of runs, fitted in a thread (this tier's default)
    assert house.coordinator._cycle_store.cycles("heat"), "the sim recorded no heating cycle"
    return house


async def test_cycle_refit_runs_in_a_worker_process_and_its_result_is_stored(make_house, spawned, monkeypatch) -> None:
    house = await _house_with_cycles(make_house)
    coord = house.coordinator
    cache = coord.learning._state.thermal_model_cache
    now_ts = dt_util.now().timestamp()
    await coord._cycle_refit("heat", now_ts)  # in a thread (this tier's default): what the worker must reproduce
    learned = dict(cache["cycle_fit_heat"])
    assert learned["ne"] >= 1 and spawned == []
    del cache["cycle_fit_heat"]  # forget it: only the worker can put it back
    _process_mode(monkeypatch)

    await coord._cycle_refit("heat", now_ts)

    assert len(spawned) == 1, "the refit did not start a worker process"
    assert spawned[0].returncode == 0 and spawned[0].pid != os.getpid()
    fit = cache["cycle_fit_heat"]
    assert fit["ne"] == learned["ne"] and fit["q"] == pytest.approx(learned["q"], rel=1e-6)
    assert fit["applied"] is True and cache["k_active_heat"] == pytest.approx(fit["q"], abs=1e-3)


async def test_unload_during_a_cycle_refit_kills_and_reaps_the_worker_promptly(
    make_house, spawned, sleeping_job, monkeypatch
) -> None:
    house = await _house_with_cycles(make_house)
    coord, entry, hass = house.coordinator, house.ca_entry, house.hass
    sleeping_job("cycle_refit")
    _process_mode(monkeypatch)
    coord._cycle_last_refit.clear()
    coord._cycle_spawn_refit(startup=True)
    await _wait_real(lambda: spawned and coord._cycle_refit_task is not None, "the refit worker to start")
    proc = spawned[0]
    assert proc.returncode is None  # the worker is running its 60 s job

    started = _real_now()
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await _wait_real(unload.done, "the unload to finish", _UNLOAD_BUDGET_S)
    elapsed = _real_now() - started

    assert unload.result() is True and entry.state is ConfigEntryState.NOT_LOADED
    assert elapsed < _UNLOAD_BUDGET_S
    assert proc.returncode == -9, "the worker was not killed and reaped"
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)  # reaped: no zombie, no live process


async def test_reload_during_a_v4_fit_kills_and_reaps_the_worker_promptly(
    make_house, spawned, sleeping_job, monkeypatch
) -> None:
    house = await make_house(**{**_HOUSE, "ca_config": {"primary_engine": "v4"}})
    coord, entry, hass = house.coordinator, house.ca_entry, house.hass
    sleeping_job("v4_fit")
    _process_mode(monkeypatch)
    assert coord._v4_start_fit("nightly") is True
    await _wait_real(lambda: spawned, "the v4 fit worker to start")
    proc = spawned[0]
    assert proc.returncode is None

    started = _real_now()
    reload = hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    await _wait_real(reload.done, "the reload to finish", _UNLOAD_BUDGET_S)

    assert _real_now() - started < _UNLOAD_BUDGET_S
    assert entry.state is ConfigEntryState.LOADED and house.coordinator is not coord
    assert proc.returncode == -9, "the worker was not killed and reaped"
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


async def _stop_home_assistant(hass) -> None:
    """What a Home Assistant restart does first: the STOP event (entries are NOT unloaded on a restart)."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)


async def test_ha_stop_kills_a_running_v4_fit_worker(make_house, spawned, sleeping_job, monkeypatch) -> None:
    """A restart never unloads the entry, so HA does not cancel the fit's background task: only the stop handler's own
    ``_v4_cancel_fit`` ends the worker (the unload test above passes without it, HA cancels that task itself)."""
    house = await make_house(**{**_HOUSE, "ca_config": {"primary_engine": "v4"}})
    coord = house.coordinator
    sleeping_job("v4_fit")
    _process_mode(monkeypatch)
    assert coord._v4_start_fit("nightly") is True
    await _wait_real(lambda: spawned, "the v4 fit worker to start")

    await _stop_home_assistant(house.hass)
    await _wait_real(lambda: spawned[0].returncode is not None, "the worker to be killed", _UNLOAD_BUDGET_S)

    assert spawned[0].returncode == -9
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0].pid, 0)
    assert coord._v4_start_fit("nightly") is False  # and no new worker starts while HA is stopping
    assert len(spawned) == 1


async def test_ha_stop_kills_a_running_cycle_refit_worker(make_house, spawned, sleeping_job, monkeypatch) -> None:
    house = await _house_with_cycles(make_house)
    coord = house.coordinator
    sleeping_job("cycle_refit")
    _process_mode(monkeypatch)
    coord._cycle_last_refit.clear()
    coord._cycle_spawn_refit(startup=True)
    await _wait_real(lambda: spawned and coord._cycle_refit_task is not None, "the refit worker to start")

    await _stop_home_assistant(house.hass)
    await _wait_real(lambda: spawned[0].returncode is not None, "the worker to be killed", _UNLOAD_BUDGET_S)

    assert spawned[0].returncode == -9
    coord._cycle_spawn_refit(startup=True)  # HA is stopping: no second worker
    await asyncio.sleep(0)
    assert len(spawned) == 1
