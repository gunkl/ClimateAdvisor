"""Run long pure-Python jobs in a worker process (Issue #1722).

CPU-bound pure-Python work in Home Assistant's executor thread starves the event loop
through the GIL. This module runs such jobs in a short-lived child interpreter instead.
Data goes in and out as JSON; the child's log records are relayed to the caller's logger.

Stdlib-only: this module must not import ``homeassistant`` or any sibling module at import
time, because the child imports it before the job module. Job modules are resolved lazily
from the fixed :data:`JOBS` allowlist.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import sys
import time
import traceback
import types
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import IO, Any, Final

JOBS: Final[Mapping[str, str]] = {
    "v4_fit": "v4_shadow:run_fit_job",
    "v4_display_fit": "v4_shadow:run_display_fit_job",
    "v4_night_replay": "night_replay:run_nightly_job",
    "cycle_refit": "cycle_model:refit_job",
}

#: ``"process"`` (production) or ``"thread"`` (unit tier, set in ``tests/conftest.py``).
default_mode: str = "process"

_PKG_DIR: Final = Path(__file__).resolve().parent
_PACKAGE: Final = "custom_components.climate_advisor"
_ERROR_CAP: Final = 500
_LOG_LINE_CAP: Final = 500
_CHILD_NICE: Final = 10

# Test-only registry. Production never populates these (see _register_job_for_tests).
_TEST_JOBS: dict[str, tuple[str, str]] = {}
_TEST_PATHS: list[str] = []

# Tiny on purpose: it only builds the namespace packages (so the package __init__, which
# needs Home Assistant, never runs) and hands over to _child_main.
_BOOTSTRAP: Final = """\
import sys, types
p, c = sys.argv[1:3]
a = types.ModuleType("custom_components"); a.__path__ = [c]
b = types.ModuleType("custom_components.climate_advisor"); b.__path__ = [p]
a.climate_advisor = b
sys.modules.update({"custom_components": a, "custom_components.climate_advisor": b})
from custom_components.climate_advisor.process_offload import _child_main
raise SystemExit(_child_main())
"""


class ProcessJobError(RuntimeError):
    """A worker job failed, timed out, or its process died."""


def _register_job_for_tests(name: str, module: str, function: str, path: str | None = None) -> None:
    """Test hook: register ``module:function`` (an absolute module name) as job ``name``.

    ``path`` is added to ``sys.path`` in the child (and here) so the module is importable.
    Production code never calls this; the child receives the registry as an optional
    third argv element that production never sends.
    """
    _TEST_JOBS[name] = (module, function)
    if path is not None and path not in _TEST_PATHS:
        _TEST_PATHS.append(path)
        sys.path.append(path)


def run_job(job: str, payload: dict[str, Any]) -> Any:
    """Resolve ``job`` from the allowlist and call it with ``payload``.

    The single code path for both the child and thread mode. The function is looked up by
    attribute at call time so monkeypatching works in thread mode.

    Raises:
        ValueError: ``job`` is not in the allowlist.
    """
    if job in _TEST_JOBS:
        module_name, func_name = _TEST_JOBS[job]
    elif job in JOBS:
        mod, func_name = JOBS[job].split(":")
        module_name = f"{_PACKAGE}.{mod}"
    else:
        raise ValueError(f"Unknown worker job: {job!r}")
    module = importlib.import_module(module_name)
    return getattr(module, func_name)(payload)


def _thread_run(job: str, payload: dict[str, Any]) -> Any:
    """Thread-mode body: identical data types to process mode via a JSON round-trip."""
    result = run_job(job, json.loads(json.dumps(payload)))
    return json.loads(json.dumps(result))


def _encode(job: str, payload: dict[str, Any]) -> bytes:
    """JSON-encode the ``{"job", "payload"}`` message for the child's stdin."""
    return json.dumps({"job": job, "payload": payload}).encode()


def _decode(raw: bytes) -> dict[str, Any]:
    """Decode the child's stdout envelope."""
    out: dict[str, Any] = json.loads(raw.decode())
    return out


def _relay_logs(logger: logging.Logger, job: str, stderr: bytes) -> None:
    """Relay the child's JSON log lines; non-JSON lines go out at WARNING (capped)."""
    for line in stderr.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            level = int(rec["level"])
            msg = str(rec["msg"])
        except ValueError, KeyError, TypeError:
            level, msg = logging.WARNING, line
        logger.log(level, "worker %s: %s", job, msg[:_LOG_LINE_CAP])


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and wait for it, surviving further cancellation until it is reaped."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    waiter = asyncio.ensure_future(proc.wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def async_run_job(
    job: str,
    payload: dict[str, Any],
    *,
    executor_job: Callable[..., Awaitable[Any]],
    logger: logging.Logger,
    timeout_s: float,
    mode: str | None = None,
) -> Any:
    """Run ``job`` off the event loop and return its JSON-able result.

    Args:
        job: allowlisted job name (:data:`JOBS`).
        payload: JSON-able input.
        executor_job: awaitable ``fn(*args)`` runner (the coordinator's ``_executor_job``).
        logger: receives the relayed child log records and diagnostics.
        timeout_s: process mode only; the child is killed and reaped after this long.
        mode: ``"process"`` or ``"thread"``; defaults to :data:`default_mode`.

    Raises:
        ValueError: unknown job (checked up front in both modes).
        ProcessJobError: the child failed, timed out, or died.
    """
    if job not in JOBS and job not in _TEST_JOBS:
        raise ValueError(f"Unknown worker job: {job!r}")
    if (mode or default_mode) == "thread":
        return await executor_job(_thread_run, job, payload)

    started = time.monotonic()
    raw = await executor_job(_encode, job, payload)
    args = [sys.executable, "-I", "-c", _BOOTSTRAP, str(_PKG_DIR), str(_PKG_DIR.parent)]
    if _TEST_JOBS or _TEST_PATHS:
        args.append(json.dumps({"jobs": _TEST_JOBS, "paths": _TEST_PATHS}))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.warning("Worker process unavailable; running in a thread: job=%s", job)
        return await executor_job(_thread_run, job, payload)

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(raw), timeout_s)
    except TimeoutError:
        await _kill_and_reap(proc)
        raise ProcessJobError(f"Worker job {job} timed out after {timeout_s:g} s") from None
    except asyncio.CancelledError:
        await _kill_and_reap(proc)
        raise

    _relay_logs(logger, job, stderr)
    logger.debug(
        "Worker job done: job=%s seconds=%.2f bytes_in=%d bytes_out=%d",
        job,
        time.monotonic() - started,
        len(raw),
        len(stdout),
    )
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace")[-_ERROR_CAP:]
        raise ProcessJobError(f"Worker job {job} exited with code {proc.returncode}: {tail}")
    envelope = await executor_job(_decode, stdout)
    if not envelope.get("ok"):
        raise ProcessJobError(f"Worker job {job} failed: {str(envelope.get('error'))[:_ERROR_CAP]}")
    return envelope.get("result")


# --------------------------------------------------------------------------- child side


class _JsonLineHandler(logging.Handler):
    """Writes each record as a JSON line (level, name, msg) to a stream (stderr)."""

    def __init__(self, stream: IO[str]) -> None:
        super().__init__(logging.INFO)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """Write one record; tracebacks are folded into ``msg``."""
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info))
            self._stream.write(json.dumps({"level": record.levelno, "name": record.name, "msg": msg}) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)


def _install_ha_stub() -> None:
    """Install the minimal ``homeassistant.util.dt`` the hass-free modules import."""

    def utcnow() -> datetime:
        return datetime.now(UTC)

    def now(tz: tzinfo | None = None) -> datetime:
        return datetime.now(tz or UTC)

    def as_utc(d: datetime) -> datetime:
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)

    ha = types.ModuleType("homeassistant")
    util = types.ModuleType("homeassistant.util")
    dt = types.ModuleType("homeassistant.util.dt")
    dt.utcnow = utcnow  # type: ignore[attr-defined]
    dt.now = now  # type: ignore[attr-defined]
    dt.as_utc = as_utc  # type: ignore[attr-defined]
    util.dt = dt  # type: ignore[attr-defined]
    ha.util = util  # type: ignore[attr-defined]
    sys.modules.update({"homeassistant": ha, "homeassistant.util": util, "homeassistant.util.dt": dt})


def _child_main() -> int:
    """Worker entry: read a payload on stdin, run the job, write the envelope on stdout.

    Returns 0 on success and on a handled job failure (reported as ``{"ok": false}``);
    anything uncaught propagates and exits non-zero.
    """
    if len(sys.argv) > 3:  # test-only registry; production never sends it
        extra = json.loads(sys.argv[3])
        for name, (mod, func) in extra["jobs"].items():
            _TEST_JOBS[name] = (mod, func)
        sys.path.extend(extra["paths"])
    _install_ha_stub()
    with contextlib.suppress(OSError, AttributeError):
        os.nice(_CHILD_NICE)

    root = logging.getLogger()
    old_level = root.level
    handler = _JsonLineHandler(sys.stderr)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    raw = sys.stdin.buffer.read()
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints must not corrupt the envelope
    try:
        try:
            envelope: dict[str, Any] = {"ok": True, "result": run_job_from_wire(raw)}
            body = json.dumps(envelope)
        except Exception as err:  # noqa: BLE001 - reported to the parent
            logging.getLogger(__name__).error("Worker job failed", exc_info=True)
            body = json.dumps({"ok": False, "error": f"{type(err).__name__}: {err}"[:_ERROR_CAP]})
        out.write(body.encode())
        out.flush()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    return 0


def run_job_from_wire(raw: bytes) -> Any:
    """Decode the child's stdin message ``{"job": ..., "payload": ...}`` and run it."""
    msg = json.loads(raw.decode())
    return run_job(msg["job"], msg["payload"])
