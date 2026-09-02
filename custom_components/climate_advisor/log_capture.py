"""Real WARNING+/ERROR log-record capture for the AI Investigator (Issue #578).

The investigator's "System Errors/Warnings" section used to check whether a
CA-internal event's `type` string happened to contain the substring "error"
or "warning" (e.g. `state_contradiction_warning`) — coincidental naming, not
severity. CA's own `_event_log` entries (`coordinator._emit_event()`) have no
severity field at all, so that check was almost always a false negative
regardless of real problems. Meanwhile the codebase already has dozens of
`_LOGGER.warning()`/`.error()` call sites that were never surfaced anywhere.

Rather than touching every call site (which would not be DRY), this module
attaches one `logging.Handler` to the `custom_components.climate_advisor`
logger namespace at integration setup, capturing every WARNING+ record the
package already emits into a ring buffer for free.
"""

from __future__ import annotations

import contextvars
import datetime
import functools
import logging
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from .const import LOG_CAPTURE_CAP

_HANDLER_LOGGER_NAME = "custom_components.climate_advisor"

# Issue #812: zone attribution for captured log records.
#
# Every _LOGGER call site in this package uses a bare
# `logging.getLogger(__name__)` with no zone/entry tag — with 2+ zones
# (config entries, each with its own ClimateAdvisorCoordinator) running
# concurrently, a WARNING from zone B's update cycle was indistinguishable
# from one from zone A's in the shared ring buffer below, so the AI
# Investigator's "System Errors/Warnings" section for zone A could show
# zone B's problems mislabeled as zone A's.
#
# Design: a `ContextVar` set for the duration of each zone's own async work
# (see `zone_scope()`), read back here in `emit()`. This tags every record
# without touching the hundreds of existing `_LOGGER.warning()`/`.error()`
# call sites across the package.
#
# IMPORTANT — executor boundary (verified empirically, not assumed): a
# `ContextVar` set inside an `async def` coroutine does NOT automatically
# propagate into work submitted via `hass.async_add_executor_job()`. HA's
# wrapper delegates to `asyncio.loop.run_in_executor()`, which (unlike
# `loop.call_soon()`/Task creation) submits the callable to the
# `ThreadPoolExecutor` via `executor.submit()` directly — it does not wrap
# the call in `contextvars.copy_context().run(...)`. A standalone
# asyncio + contextvars + ThreadPoolExecutor script reproduced this
# directly in this environment: a zone label set in a coroutine before
# `await loop.run_in_executor(None, fn)` read back as the ContextVar's
# *default* value inside `fn`, not the value set in the calling coroutine.
# Any log line emitted from code reached via `hass.async_add_executor_job()`
# (e.g. `learning.save_state()`, `chart_log.save()`, `_state_persistence.*`)
# therefore needs the zone label re-applied explicitly inside the executor
# thread — see `bind_zone_for_executor()` below, used by
# `ClimateAdvisorCoordinator._executor_job()` in coordinator.py.
_current_zone_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "climate_advisor_zone_label", default=None
)


@contextmanager
def zone_scope(zone_label: str | None):
    """Set the ambient zone label for the duration of a ``with`` block.

    Covers directly-awaited async code and code scheduled as an asyncio Task
    (e.g. via ``hass.async_create_task``) — both inherit the current
    ``contextvars.Context`` automatically. Does NOT cover work offloaded via
    ``hass.async_add_executor_job()`` — see module docstring above and
    ``bind_zone_for_executor()``.
    """
    token = _current_zone_label.set(zone_label)
    try:
        yield
    finally:
        _current_zone_label.reset(token)


def current_zone_label() -> str | None:
    """Return the zone label active in the current context, if any."""
    return _current_zone_label.get()


def bind_zone_for_executor(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so it re-establishes the calling zone label in an executor thread.

    Captures the zone label at the call site (inside the coroutine, where the
    ambient value is still correct) and re-applies it inside the executor
    thread before calling ``fn``, so any ``_LOGGER`` call made by ``fn`` (or
    anything it calls) is still tagged correctly by
    ``ClimateAdvisorLogRingBuffer.emit()``. See module docstring for why this
    is necessary — plain ``ContextVar`` propagation does not cross the
    ``hass.async_add_executor_job()`` boundary.

    Sets ``__wrapped__`` (via ``functools.wraps``) so callers/tests that need
    to identify the original target (e.g. a test double keyed off function
    identity) can unwrap via ``inspect.unwrap()`` or ``fn.__wrapped__``.
    """
    zone_label = _current_zone_label.get()

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        token = _current_zone_label.set(zone_label)
        try:
            return fn(*args, **kwargs)
        finally:
            _current_zone_label.reset(token)

    return _wrapped


# Deliberately NOT stored under hass.data[DOMAIN]: api.py's _get_coordinator()
# resolves via zone_registry.get_coordinator()/get_default_coordinator()
# (Issue #796 Gap 4), both of which assume every value in hass.data[DOMAIN]
# IS a coordinator (e.g. `next(iter(entries.values()))`) — inserting this
# handler into that same dict before the coordinator is added would make
# every REST view resolve the log handler instead of the coordinator and
# crash with AttributeError.
_HASS_DATA_KEY = "climate_advisor_log_capture"


class ClimateAdvisorLogRingBuffer(logging.Handler):
    """A `logging.Handler` that ring-buffers WARNING+ records for later reads."""

    def __init__(self, capacity: int = LOG_CAPTURE_CAP) -> None:
        super().__init__(level=logging.WARNING)
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = record.msg if isinstance(record.msg, str) else "<unformattable log message>"
            # ISO 8601 with explicit UTC offset — ai_skills_context.py's _fmt_time()
            # converts to local time for display, same as every other timestamp
            # in the investigator context (event log, activity timeline).
            record_time = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
            # Must not raise if the contextvar was never set (e.g. a log line
            # emitted at integration setup/teardown, outside any zone_scope())
            # — falls back to None, rendered as an explicit "unknown zone"
            # marker by ai_skills_context.py rather than silently attributed
            # to whichever zone happened to run most recently.
            try:
                zone = _current_zone_label.get()
            except Exception:
                zone = None
            self._records.append(
                {
                    "time": record_time.isoformat(),
                    "level": record.levelname,
                    "logger_name": record.name,
                    "message": message,
                    "zone": zone,
                }
            )
        except Exception:
            self.handleError(record)

    def get_records(self) -> list[dict[str, Any]]:
        """Return captured records, oldest first."""
        return list(self._records)


def install(hass: Any) -> ClimateAdvisorLogRingBuffer:
    """Attach a `ClimateAdvisorLogRingBuffer` to the package logger and return it.

    Idempotent: if a handler is already attached (e.g. a config-entry reload),
    the existing handler is reused instead of stacking a second one.
    """
    package_logger = logging.getLogger(_HANDLER_LOGGER_NAME)
    existing = hass.data.get(_HASS_DATA_KEY)
    if isinstance(existing, ClimateAdvisorLogRingBuffer) and existing in package_logger.handlers:
        return existing

    handler = ClimateAdvisorLogRingBuffer()
    package_logger.addHandler(handler)
    hass.data[_HASS_DATA_KEY] = handler
    return handler


def uninstall(hass: Any) -> None:
    """Detach and forget the handler stored on `hass.data`, if any."""
    handler = hass.data.pop(_HASS_DATA_KEY, None)
    if isinstance(handler, ClimateAdvisorLogRingBuffer):
        logging.getLogger(_HANDLER_LOGGER_NAME).removeHandler(handler)


def get_handler(hass: Any) -> ClimateAdvisorLogRingBuffer | None:
    """Return the installed handler, if any, for read-only consumers."""
    handler = hass.data.get(_HASS_DATA_KEY)
    return handler if isinstance(handler, ClimateAdvisorLogRingBuffer) else None
