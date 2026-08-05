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

import datetime
import logging
from collections import deque
from typing import Any

from .const import LOG_CAPTURE_CAP

_HANDLER_LOGGER_NAME = "custom_components.climate_advisor"

# Deliberately NOT stored under hass.data[DOMAIN]: api.py's _get_coordinator()
# does `next(iter(hass.data[DOMAIN].values()))` to find "the" coordinator, and
# Python dicts are insertion-ordered — inserting this handler into that same
# dict before the coordinator is added would make every REST view resolve the
# log handler instead of the coordinator and crash with AttributeError.
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
            self._records.append(
                {
                    "time": record_time.isoformat(),
                    "level": record.levelname,
                    "logger_name": record.name,
                    "message": message,
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
