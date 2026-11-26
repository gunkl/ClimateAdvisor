"""Issue #1722: process_offload runs jobs in a real worker process (no mirrors, real children)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from custom_components.climate_advisor import process_offload as po

_TESTS_DIR = str(Path(__file__).resolve().parent)
_MOD = "offload_test_jobs_1722"
_LOGGER = logging.getLogger("test_offload")


@pytest.fixture(autouse=True)
def _jobs():
    """Register the test-only jobs; restore the registry and sys.path afterwards."""
    saved_path = list(sys.path)
    for name in (
        "echo",
        "boom",
        "sleeper",
        "probe_isolation",
        "ha_stub_report",
        "import_closure",
        "noisy",
        "not_json",
        "hard_exit",
    ):
        po._register_job_for_tests(f"t_{name}", _MOD, name, _TESTS_DIR)
    yield
    po._TEST_JOBS.clear()
    po._TEST_PATHS.clear()
    sys.path[:] = saved_path


async def _executor(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _run(job: str, payload: dict[str, Any], mode: str, timeout_s: float = 60, logger=_LOGGER) -> Any:
    return asyncio.run(
        po.async_run_job(job, payload, executor_job=_executor, logger=logger, timeout_s=timeout_s, mode=mode)
    )


def test_real_child_runs_job_process_and_thread_same_types() -> None:
    payload = {"xs": [1, 2, 3], "nested": {"k": (4, 5)}}
    proc = _run("t_echo", payload, "process")
    thread = _run("t_echo", payload, "thread")
    assert proc == thread
    assert proc["sum"] == 6
    assert proc["t"] == [1, 2]  # tuple became a list in both modes
    assert proc["payload"]["nested"]["k"] == [4, 5]


@pytest.mark.parametrize("mode", ["process", "thread"])
def test_unknown_job_raises_value_error(mode: str) -> None:
    with pytest.raises(ValueError, match="nope"):
        _run("nope", {}, mode)


def test_run_job_unknown_and_allowlisted_resolution() -> None:
    with pytest.raises(ValueError):
        po.run_job("nope", {})
    assert set(po.JOBS) == {"v4_fit", "v4_display_fit", "v4_night_replay", "cycle_refit"}
    assert po.default_mode in ("process", "thread")


def test_child_exception_is_process_job_error_and_logs_relayed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="test_offload"), pytest.raises(po.ProcessJobError) as exc:
        _run("t_boom", {"x": 7}, "process")
    assert "RuntimeError: kaboom" in str(exc.value)
    by_level = {r.levelno: r.getMessage() for r in caplog.records if r.name == "test_offload"}
    assert "worker t_boom: about to fail x=7" in by_level[logging.WARNING]
    assert any(r.levelno == logging.INFO and "info line" in r.getMessage() for r in caplog.records)
    # the child's traceback is relayed too
    assert any("Traceback" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_non_json_stderr_relayed_at_warning_and_stdout_not_corrupted(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="test_offload"):
        assert _run("t_noisy", {}, "process") == "ok"
    assert any(r.levelno == logging.WARNING and "raw stderr line" in r.getMessage() for r in caplog.records)


def test_unserialisable_result_is_process_job_error() -> None:
    with pytest.raises(po.ProcessJobError, match="TypeError"):
        _run("t_not_json", {}, "process")


def test_child_dies_without_envelope_is_process_job_error() -> None:
    with pytest.raises(po.ProcessJobError, match="exited with code 3"):
        _run("t_hard_exit", {}, "process")


def _sleeper_call() -> Any:
    return po.async_run_job(
        "t_sleeper", {"s": 60}, executor_job=_executor, logger=_LOGGER, timeout_s=60, mode="process"
    )


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    procs: list[Any] = []
    real = asyncio.create_subprocess_exec

    async def spy(*a: Any, **k: Any) -> Any:
        p = await real(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return procs


def test_timeout_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = _capture_spawn(monkeypatch)
    with pytest.raises(po.ProcessJobError, match="timed out after 1 s"):
        _run("t_sleeper", {"s": 60}, "process", timeout_s=1)
    assert len(procs) == 1
    assert procs[0].returncode is not None  # reaped: no zombie
    assert procs[0].returncode < 0  # killed by signal


def test_cancel_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = _capture_spawn(monkeypatch)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            po.async_run_job(
                "t_sleeper", {"s": 60}, executor_job=_executor, logger=_LOGGER, timeout_s=60, mode="process"
            )
        )
        for _ in range(200):
            if procs:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(procs) == 1
    assert procs[0].returncode is not None


def test_second_cancel_during_reap_still_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    procs: list[Any] = []
    waited: list[int] = []
    real = asyncio.create_subprocess_exec

    async def spy(*a: Any, **k: Any) -> Any:
        p = await real(*a, **k)
        real_wait = p.wait

        async def slow_wait() -> int:
            await asyncio.sleep(0.5)  # keep the reap in flight so a second cancel lands in it
            code = await real_wait()
            waited.append(code)
            return code

        p.wait = slow_wait
        procs.append((p, real_wait))
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    async def scenario() -> None:
        task = asyncio.ensure_future(_sleeper_call())
        for _ in range(200):
            if procs:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()  # second cancel while reaping
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1)
        assert waited  # the reap ran to completion despite the second cancel
        assert procs[0][0].returncode is not None

    asyncio.run(scenario())


def test_allowlisted_job_resolves_in_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(po, "_PACKAGE", "tests")
    monkeypatch.setattr(po, "JOBS", {"e": f"{_MOD}:echo"})
    assert po.run_job("e", {"xs": [1, 2]})["sum"] == 3


def test_spawn_oserror_falls_back_to_thread_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*a: Any, **k: Any) -> Any:
        raise OSError("no fork")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with caplog.at_level(logging.DEBUG, logger="test_offload"):
        result = _run("t_echo", {"xs": [1]}, "process")
    assert result["sum"] == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Worker process unavailable; running in a thread: job=t_echo" in warnings[0].getMessage()


def test_other_spawn_errors_do_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*a: Any, **k: Any) -> Any:
        raise RuntimeError("different")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
    with pytest.raises(RuntimeError, match="different"):
        _run("t_echo", {"xs": [1]}, "process")


def test_default_mode_is_used_when_mode_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(po, "default_mode", "process")
    pids = asyncio.run(
        po.async_run_job("t_probe_isolation", {"name": "json"}, executor_job=_executor, logger=_LOGGER, timeout_s=60)
    )
    assert pids["pid"] != os.getpid()
    monkeypatch.setattr(po, "default_mode", "thread")
    pids = asyncio.run(
        po.async_run_job("t_probe_isolation", {"name": "json"}, executor_job=_executor, logger=_LOGGER, timeout_s=60)
    )
    assert pids["pid"] == os.getpid()


def test_isolated_child_ignores_cwd_and_pythonpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "cwd"
    pp = tmp_path / "pp"
    cwd.mkdir()
    pp.mkdir()
    (cwd / "planted_in_cwd_1722.py").write_text("X = 1\n")
    (pp / "planted_on_pythonpath_1722.py").write_text("X = 1\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PYTHONPATH", str(pp))
    for name in ("planted_in_cwd_1722", "planted_on_pythonpath_1722"):
        # Sanity: a non-isolated interpreter in the same environment WOULD import it.
        import subprocess

        plain = subprocess.run(
            [sys.executable, "-c", f"import {name}"], cwd=cwd, env=os.environ.copy(), capture_output=True, check=False
        )
        assert plain.returncode == 0
        res = _run("t_probe_isolation", {"name": name}, "process")
        assert res["found"] is False
        assert res["cwd_on_path"] is False


def test_child_homeassistant_stub_is_minimal() -> None:
    rep = _run("t_ha_stub_report", {}, "process")
    assert rep["ha"] == ["homeassistant", "homeassistant.util", "homeassistant.util.dt"]
    assert rep["dt"] == ["as_utc", "now", "utcnow"]
    assert rep["attr"] is True


def test_import_closure_of_job_modules_is_hass_free() -> None:
    """Guard: the allowlisted job modules must import in the child without Home Assistant."""
    mods = ["v4_shadow", "night_replay", "cycle_model", "predictor_v4"]
    ha = _run("t_import_closure", {"modules": mods}, "process")
    assert ha == ["homeassistant", "homeassistant.util", "homeassistant.util.dt"]


def test_job_modules_named_in_allowlist_exist() -> None:
    for target in po.JOBS.values():
        assert (Path(po.__file__).parent / f"{target.split(':')[0]}.py").exists()


# ---- child entry, in-process (so coverage sees it); real stdin/stdout/stderr semantics -----


def _run_child_inprocess(
    monkeypatch: pytest.MonkeyPatch, message: dict[str, Any], argv_extra: list[str]
) -> tuple[int, str, str]:
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(message).encode()))
    out_buf = io.BytesIO()
    stdout = io.TextIOWrapper(out_buf)
    stderr = io.StringIO()
    for mod in ("homeassistant", "homeassistant.util", "homeassistant.util.dt"):
        monkeypatch.setitem(sys.modules, mod, sys.modules[mod])
    monkeypatch.setattr(os, "nice", lambda n: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr(sys, "argv", ["-c", "/pkg", "/parent", *argv_extra])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(po, "_TEST_JOBS", dict(po._TEST_JOBS))
    monkeypatch.setattr(sys, "path", list(sys.path))
    rc = po._child_main()
    return rc, out_buf.getvalue().decode(), stderr.getvalue()


def test_child_main_success_and_failure_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = json.dumps({"jobs": {"c_echo": [_MOD, "echo"], "c_boom": [_MOD, "boom"]}, "paths": [_TESTS_DIR]})
    rc, out, _ = _run_child_inprocess(monkeypatch, {"job": "c_echo", "payload": {"xs": [2, 3]}}, [extra])
    assert rc == 0
    assert json.loads(out)["result"]["sum"] == 5
    rc, out, err = _run_child_inprocess(monkeypatch, {"job": "c_boom", "payload": {"x": 1}}, [extra])
    assert rc == 0
    assert json.loads(out) == {"ok": False, "error": "RuntimeError: kaboom"}
    levels = [json.loads(line)["level"] for line in err.splitlines()]
    assert logging.WARNING in levels and logging.ERROR in levels


def test_child_stub_functions_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta, timezone

    for mod in ("homeassistant", "homeassistant.util", "homeassistant.util.dt"):
        monkeypatch.setitem(sys.modules, mod, sys.modules[mod])
    po._install_ha_stub()
    dt = sys.modules["homeassistant.util.dt"]
    assert dt.utcnow().tzinfo is UTC
    assert dt.now().tzinfo is UTC
    tz = timezone(timedelta(hours=2))
    assert dt.now(tz).utcoffset() == timedelta(hours=2)
    assert dt.as_utc(datetime(2026, 1, 1)).tzinfo is UTC
    assert dt.as_utc(datetime(2026, 1, 1, 2, tzinfo=tz)).hour == 0


def test_json_line_handler_never_raises() -> None:
    class Bad(io.StringIO):
        def write(self, s: str) -> int:
            raise ValueError("closed")

    handler = po._JsonLineHandler(Bad())
    handler.handleError = lambda record: None  # type: ignore[method-assign]
    handler.emit(logging.LogRecord("n", logging.INFO, "f", 1, "m", None, None))


def test_relay_logs_ignores_blank_and_caps_long_lines(caplog: pytest.LogCaptureFixture) -> None:
    long = "x" * 5000
    with caplog.at_level(logging.INFO, logger="test_offload"):
        po._relay_logs(_LOGGER, "j", f"\n{long}\n".encode())
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1
    assert len(msgs[0]) < 600
