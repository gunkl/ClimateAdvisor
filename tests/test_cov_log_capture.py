"""log_capture ring buffer: a broken log record never breaks capture (Silver coverage).

Occupant view: the AI Investigator's warnings list keeps working even when one of Climate Advisor's log calls has a
bad format string, so the person asking "why did it do that?" still sees the real warnings.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from custom_components.climate_advisor import log_capture


def _record(msg, args=(), level=logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("custom_components.climate_advisor.x", level, __file__, 1, msg, args, None)


class TestEmitFallbacks:
    def test_bad_format_args_keep_raw_message(self):
        handler = log_capture.ClimateAdvisorLogRingBuffer()
        handler.emit(_record("value %d", ("not-a-number",)))
        (rec,) = handler.get_records()
        assert rec["message"] == "value %d"
        assert rec["level"] == "WARNING"

    def test_unformattable_non_string_message_gets_marker(self):
        class _Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        handler = log_capture.ClimateAdvisorLogRingBuffer()
        handler.emit(_record(_Unprintable()))
        (rec,) = handler.get_records()
        assert rec["message"] == "<unformattable log message>"

    def test_zone_lookup_failure_records_unknown_zone(self, monkeypatch):
        broken = MagicMock()
        broken.get.side_effect = LookupError("gone")
        monkeypatch.setattr(log_capture, "_current_zone_label", broken)
        handler = log_capture.ClimateAdvisorLogRingBuffer()
        handler.emit(_record("hello"))
        (rec,) = handler.get_records()
        assert rec["zone"] is None
        assert rec["message"] == "hello"

    def test_unexpected_failure_goes_to_handle_error_and_records_nothing(self):
        handler = log_capture.ClimateAdvisorLogRingBuffer()
        handler.handleError = MagicMock()
        record = _record("hello")
        record.created = "not-a-timestamp"
        handler.emit(record)
        handler.handleError.assert_called_once_with(record)
        assert handler.get_records() == []
