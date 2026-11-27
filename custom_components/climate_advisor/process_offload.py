"""Run long pure-Python jobs in a worker process (Issue #1722).

CPU-bound pure-Python work in Home Assistant's executor thread starves the event loop
through the GIL. This module runs such jobs in a short-lived child interpreter instead.
Data goes in and out as JSON; the child's log records are relayed to the caller's logger.

Stdlib-only: this module must not import ``homeassistant`` or any sibling module at import
time, because the child imports it before the job module. Job modules are resolved lazily
from the fixed :data:`JOBS` allowlist. The one exception is ``store_json`` (orjson and the stdlib only), imported inside
the functions that encode or decode: the payload and the result travel one element per line (Issue #1730), so a few MB
never hold the GIL (and so the event loop) in one long call.
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
_WHERE_CAP: Final = 300  # the "(at file:line in func)" suffix of a job error
#: One relayed log message. Generous so a traceback survives; a longer one keeps its head and its TAIL (the failing
#: frame).
_LOG_LINE_CAP: Final = 8000
_LOG_HEAD_KEEP: Final = 500
_CHILD_NICE: Final = 10
#: Environment names the child never sees (compared upper-cased): the Supervisor/Hassio tokens, any secret-shaped name.
_SECRET_NAMES: Final = frozenset({"SUPERVISOR_TOKEN", "HASSIO_TOKEN"})
_SECRET_SUFFIXES: Final = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PASSWD", "_PASS")

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


def _child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the worker child inherits: ``environ`` (default ``os.environ``) minus anything secret-shaped.

    Home Assistant OS runs the core with ``SUPERVISOR_TOKEN`` in its environment; a job has no use for it, so the
    child never receives it. Dropped (case-insensitive): ``SUPERVISOR_TOKEN``, ``HASSIO_TOKEN`` and every name ending in
    ``_TOKEN``, ``_KEY``, ``_SECRET``, ``_PASSWORD``, ``_PASSWD`` or ``_PASS``. Everything else (``PATH``, ``LANG``,
    ``TZ``, ``PYTHONTZPATH``, ...) is kept: the job needs the time zone data and the interpreter's own settings.
    """
    source = os.environ if environ is None else environ
    return {
        k: v
        for k, v in source.items()
        if (name := k.upper()) not in _SECRET_NAMES and not name.endswith(_SECRET_SUFFIXES)
    }


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
    result = run_job(job, _decode(_encode_value(payload)))
    return _decode(_encode_value(result))


def _encode(job: str, payload: dict[str, Any], log_level: int = logging.INFO) -> bytes:
    """JSON-encode the ``{"job", "payload", "log_level"}`` message for the child's stdin.

    ``log_level`` (the caller logger's effective level) travels in the envelope, not the payload: the child logs at
    that level, so DEBUG lines are relayed exactly when the parent would show them.
    """
    return _encode_value({"job": job, "payload": payload, "log_level": log_level})


def _encode_value(value: Any) -> bytes:
    """``value`` as JSON bytes, one element per line. Lossless (the stdlib's NaN/Infinity tokens, not ``null``): a job
    function can see the same floats in a worker process as in a thread."""
    from . import store_json  # noqa: PLC0415 - lazy: nothing sibling is imported when this module loads

    return store_json.dumps(value, depth=3, lossless=True)


def _decode(raw: bytes) -> Any:
    """Decode the child's stdout envelope (any JSON; the caller checks it is an object)."""
    from . import store_json  # noqa: PLC0415

    return store_json.loads(raw)


def _cap_message(msg: str) -> str:
    """Cap one relayed message at :data:`_LOG_LINE_CAP`, keeping its head and its tail (a traceback's failing frame)."""
    if len(msg) <= _LOG_LINE_CAP:
        return msg
    marker = "\n...[truncated]...\n"
    return msg[:_LOG_HEAD_KEEP] + marker + msg[-(_LOG_LINE_CAP - _LOG_HEAD_KEEP - len(marker)) :]


def _relay_logs(logger: logging.Logger, job: str, stderr: bytes) -> None:
    """Relay the child's JSON log lines; non-JSON lines go out at WARNING (capped).

    Each record goes through ``logging.getLogger(record name)``, the record's own module logger, so a per-module log
    level the user set still applies; ``logger`` takes the lines that carry no usable name.
    """
    for line in stderr.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        target = logger
        try:
            rec = json.loads(line)
            level = int(rec["level"])
            msg = str(rec["msg"])
            name = rec.get("name")
            if isinstance(name, str) and name:
                target = logging.getLogger(name)
        except ValueError, KeyError, TypeError, AttributeError:
            level, msg = logging.WARNING, line
        target.log(level, "worker %s: %s", job, _cap_message(msg))


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
    raw = await executor_job(_encode, job, payload, logger.getEffectiveLevel())
    # -I: isolated (no /config, cwd or PYTHON* on the import path). -I also ignores PYTHONDONTWRITEBYTECODE, so -B
    # is needed or the child would write __pycache__ files into the integration folder.
    args = [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, str(_PKG_DIR), str(_PKG_DIR.parent)]
    if _TEST_JOBS or _TEST_PATHS:
        args.append(json.dumps({"jobs": _TEST_JOBS, "paths": _TEST_PATHS}))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
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
    try:
        envelope = await executor_job(_decode, stdout)
    except ValueError as err:  # not JSON (JSONDecodeError and UnicodeDecodeError are both ValueError)
        raise ProcessJobError(f"Worker job {job} returned an unreadable result: {err}"[:_ERROR_CAP]) from err
    if not isinstance(envelope, dict):
        raise ProcessJobError(f"Worker job {job} returned an unreadable result: not a JSON object")
    if not envelope.get("ok"):
        error = str(envelope.get("error"))[: _ERROR_CAP + _WHERE_CAP]
        raise ProcessJobError(f"Worker job {job} failed: {error}")
    return envelope.get("result")


# --------------------------------------------------------------------------- child side


class _JsonLineHandler(logging.Handler):
    """Writes each record as a JSON line (level, name, msg) to a stream (stderr)."""

    def __init__(self, stream: IO[str], level: int = logging.INFO) -> None:
        super().__init__(level)
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


def _protect_stdout(stdout_fd: int = 1, stderr_fd: int = 2) -> IO[bytes]:
    """Keep the real stdout for the envelope and point fd 1 at stderr.

    Anything a job (or a C extension) writes straight to fd 1 then lands in stderr, where the parent relays it as a
    log line, instead of corrupting the envelope. Returns a binary stream on the saved real stdout.
    """
    saved = os.dup(stdout_fd)
    os.dup2(stderr_fd, stdout_fd)
    return os.fdopen(saved, "wb")


def _error_text(err: BaseException) -> str:
    """``"Type: message (at file:line in func)"``: the message capped, the failing frame always kept."""
    text = f"{type(err).__name__}: {err}"[:_ERROR_CAP]
    frames = traceback.extract_tb(err.__traceback__)
    if frames:
        last = frames[-1]
        text += f" (at {Path(last.filename).name}:{last.lineno} in {last.name})"[:_WHERE_CAP]
    return text


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

    raw = sys.stdin.buffer.read()
    out = _protect_stdout()  # fd 1 now goes to stderr
    sys.stdout = sys.stderr  # stray prints must not corrupt the envelope either
    root = logging.getLogger()
    old_level = root.level
    handler = _JsonLineHandler(sys.stderr)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        try:
            msg = _decode(raw)
            level = msg.get("log_level")  # the caller's effective level (see _encode)
            level = level if isinstance(level, int) and not isinstance(level, bool) else logging.INFO
            handler.setLevel(level)
            root.setLevel(level)
            envelope: dict[str, Any] = {"ok": True, "result": run_job(msg["job"], msg["payload"])}
            body = _encode_value(envelope)
        except Exception as err:  # noqa: BLE001 - reported to the parent
            logging.getLogger(__name__).error("Worker job failed", exc_info=True)
            body = _encode_value({"ok": False, "error": _error_text(err)})
        out.write(body)
        out.flush()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    return 0
