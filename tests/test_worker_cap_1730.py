"""Issue #1730 phase 4: at most (cores - 1) worker processes run at once, across every zone.

Occupant view: a house with several zones used to start one worker interpreter per zone at the same moment (every zone
refits at startup); on a small board that starved Home Assistant. Now the extra jobs wait for a free slot. Waiting must
never wedge: an unload or a Home Assistant stop cancels waiting jobs (they take no slot), and a cancelled, timed-out or
failed running job gives its slot back.

The child processes are fakes (a gate per process), so this is deterministic and fast.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.climate_advisor import process_offload as po

_LOG = logging.getLogger("test_worker_cap_1730")
_OK = b'{"ok": true, "result": 1}\n'


class FakeProc:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.returncode: int | None = None
        self.killed = False
        self.stdout = _OK
        self.exit_code = 0

    async def communicate(self, raw: bytes):
        await self.gate.wait()
        self.returncode = self.exit_code
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


@pytest.fixture
def procs(monkeypatch) -> list[FakeProc]:
    made: list[FakeProc] = []

    async def fake_exec(*_a, **_k):
        p = FakeProc()
        made.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(po, "worker_limit_override", 2)
    return made


async def _ex(fn, *a):
    return fn(*a)


def _run(job: str = "v4_fit", timeout_s: float = 60.0) -> asyncio.Task:
    return asyncio.ensure_future(
        po.async_run_job(job, {}, executor_job=_ex, logger=_LOG, timeout_s=timeout_s, mode="process")
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def test_worker_limit_is_cores_minus_one_and_at_least_one(monkeypatch) -> None:
    monkeypatch.setattr(po, "worker_limit_override", None)
    for cores, expect in ((8, 7), (4, 3), (2, 1), (1, 1), (None, 1)):
        monkeypatch.setattr(po.os, "cpu_count", lambda c=cores: c, raising=False)
        monkeypatch.setattr(po.os, "process_cpu_count", lambda c=cores: c, raising=False)
        assert po.worker_limit() == expect
    monkeypatch.setattr(po, "worker_limit_override", 0)
    assert po.worker_limit() == 1  # never zero: a job would wait forever


def test_worker_limit_uses_the_cores_this_process_may_use_when_python_knows_them(monkeypatch) -> None:
    """Python 3.13+: ``os.process_cpu_count()`` (affinity, cgroup-style limits) wins over the machine's ``cpu_count``;
    older Pythons fall back to ``os.cpu_count()``."""
    monkeypatch.setattr(po, "worker_limit_override", None)
    monkeypatch.setattr(po.os, "cpu_count", lambda: 16, raising=False)
    monkeypatch.setattr(po.os, "process_cpu_count", lambda: 3, raising=False)
    assert po.worker_limit() == 2
    monkeypatch.delattr(po.os, "process_cpu_count")  # an older Python has no such function
    assert po.worker_limit() == 15


def test_the_job_over_the_cap_waits_until_a_slot_frees(procs) -> None:
    async def go() -> None:
        a, b, c = _run(), _run("cycle_refit"), _run("v4_night_replay")
        await _settle()
        assert len(procs) == 2 and not c.done()  # N + 1 jobs: one waits, and has spawned nothing
        procs[0].gate.set()
        await _settle()
        assert a.result() == 1
        assert len(procs) == 3  # the waiter took the freed slot
        procs[1].gate.set()
        procs[2].gate.set()
        assert await b == 1 and await c == 1

    asyncio.run(go())


def test_a_waiting_job_is_cancelled_without_taking_or_losing_a_slot(procs) -> None:
    async def go() -> None:
        a, b, waiter = _run(), _run(), _run()
        await _settle()
        assert len(procs) == 2
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert len(procs) == 2  # it never spawned
        procs[0].gate.set()
        await a
        later = _run()  # the slot a freed is usable at once: the cancelled waiter did not keep it
        await _settle()
        assert len(procs) == 3
        procs[1].gate.set()
        procs[2].gate.set()
        await b
        await later

    asyncio.run(go())


def test_cancelling_a_running_job_kills_its_worker_and_frees_the_slot(procs) -> None:
    async def go() -> None:
        a, b, c = _run(), _run(), _run()
        await _settle()
        assert len(procs) == 2
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        assert procs[0].killed  # the existing contract (#1722) still holds
        await _settle()
        assert len(procs) == 3  # c took a's slot
        for p in procs[1:]:
            p.gate.set()
        await b
        await c

    asyncio.run(go())


def test_unload_cancelling_everything_does_not_hang_and_returns_every_slot(procs) -> None:
    async def go() -> None:
        tasks = [_run() for _ in range(6)]  # 2 running, 4 waiting
        await _settle()
        assert len(procs) == 2
        for t in tasks:
            t.cancel()
        done = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
        assert all(isinstance(r, asyncio.CancelledError) for r in done)
        assert all(p.killed for p in procs)
        sem = po._slot_semaphore()
        assert sem._value == 2 and not sem._waiters  # every slot back, nobody still queued
        fresh = _run()  # and the cap is usable again straight away
        await _settle()
        assert len(procs) == 3
        procs[2].gate.set()
        await fresh

    asyncio.run(go())


def test_a_timed_out_worker_gives_its_slot_back(procs) -> None:
    async def go() -> None:
        slow_a, slow_b = _run(timeout_s=0.05), _run(timeout_s=0.05)
        waiter = _run()
        await _settle()
        assert len(procs) == 2
        for t in (slow_a, slow_b):
            with pytest.raises(po.ProcessJobError, match="timed out"):
                await t
        await _settle()
        assert len(procs) == 3  # the waiter started once a timeout freed a slot
        procs[2].gate.set()
        assert await waiter == 1

    asyncio.run(go())


def test_a_failed_worker_gives_its_slot_back(procs) -> None:
    async def go() -> None:
        a, b, waiter = _run(), _run(), _run()
        await _settle()
        procs[0].exit_code = 1
        procs[0].gate.set()
        with pytest.raises(po.ProcessJobError, match="exited with code 1"):
            await a
        await _settle()
        assert len(procs) == 3
        procs[1].gate.set()
        procs[2].gate.set()
        await b
        await waiter

    asyncio.run(go())


def test_each_event_loop_has_its_own_slots(procs, monkeypatch) -> None:
    """A restart of Home Assistant (a new loop) or the next test must not inherit a semaphore bound to a dead loop."""
    monkeypatch.setattr(po, "worker_limit_override", 1)

    async def two_contending() -> int:
        first, second = _run(), _run()  # the second must queue behind the first
        await _settle()
        procs[-1].gate.set()
        await _settle()
        procs[-1].gate.set()
        return await first + await second

    assert asyncio.run(two_contending()) == 2
    assert asyncio.run(two_contending()) == 2


def test_two_coordinators_share_the_cap(procs, monkeypatch) -> None:
    from tools.sim_harness.build_coordinator import build_headless_coordinator

    monkeypatch.setattr(po, "default_mode", "process")
    monkeypatch.setattr(po, "worker_limit_override", 1)
    a, _h1, _s1, _e1 = build_headless_coordinator(skip_startup_coalesce=True)
    b, _h2, _s2, _e2 = build_headless_coordinator(skip_startup_coalesce=True)

    async def go() -> None:
        ta = asyncio.ensure_future(a._process_job("cycle_refit", dict, 30.0))
        tb = asyncio.ensure_future(b._process_job("cycle_refit", dict, 30.0))
        await _settle()
        assert len(procs) == 1  # two zones, one slot: the second zone waits
        procs[0].gate.set()
        await _settle()
        assert len(procs) == 2
        procs[1].gate.set()
        assert await ta == 1 and await tb == 1

    asyncio.run(go())  # a loop of its own: the shared harness loop would keep this test's limit
