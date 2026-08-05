"""Tests for the real WARNING+/ERROR log-record ring buffer (Issue #578).

Replaces the AI Investigator's old "System Errors/Warnings" check, which only
tested whether a CA-internal event's `type` string happened to contain the
substring "error"/"warning" — coincidental naming, not severity.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from custom_components.climate_advisor.log_capture import (
    ClimateAdvisorLogRingBuffer,
    get_handler,
    install,
    uninstall,
)


def _make_hass(domain_data: dict | None = None) -> SimpleNamespace:
    """Minimal stand-in for HomeAssistant exposing only `.data`."""
    return SimpleNamespace(data={"climate_advisor": domain_data if domain_data is not None else {}})


def test_captures_warning_and_error_records():
    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger("custom_components.climate_advisor.test_log_capture")
    logger.addHandler(handler)
    try:
        logger.debug("should not be captured")
        logger.info("should not be captured either")
        logger.warning("fan reconcile fired")
        logger.error("thermal observation rejected")
    finally:
        logger.removeHandler(handler)

    records = handler.get_records()
    assert len(records) == 2
    assert records[0]["level"] == "WARNING"
    assert records[0]["message"] == "fan reconcile fired"
    assert records[1]["level"] == "ERROR"
    assert records[1]["message"] == "thermal observation rejected"


def test_records_have_timezone_aware_iso_timestamps():
    """The stored timestamp must carry a UTC offset so callers can convert to
    local time (Issue #578 code review caught a version that stored a
    naive-looking string, causing SYSTEM LOG RECORDS to render in UTC while
    every other investigator section renders in local time)."""
    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger("custom_components.climate_advisor.test_log_capture_tz")
    logger.addHandler(handler)
    try:
        logger.warning("test")
    finally:
        logger.removeHandler(handler)

    record_time = handler.get_records()[0]["time"]
    assert record_time.endswith("+00:00")


def test_ring_buffer_respects_capacity():
    handler = ClimateAdvisorLogRingBuffer(capacity=3)
    logger = logging.getLogger("custom_components.climate_advisor.test_log_capture_cap")
    logger.addHandler(handler)
    try:
        for i in range(5):
            logger.warning("warning %d", i)
    finally:
        logger.removeHandler(handler)

    records = handler.get_records()
    assert len(records) == 3
    # Oldest records are dropped first — only the last 3 survive.
    assert [r["message"] for r in records] == ["warning 2", "warning 3", "warning 4"]


def test_install_is_idempotent_on_reload():
    hass = _make_hass()
    handler_a = install(hass)
    handler_b = install(hass)

    assert handler_a is handler_b
    package_logger = logging.getLogger("custom_components.climate_advisor")
    assert package_logger.handlers.count(handler_a) == 1

    uninstall(hass)
    assert get_handler(hass) is None
    assert handler_a not in package_logger.handlers


def test_uninstall_without_install_is_a_noop():
    uninstall(_make_hass())


def test_install_never_touches_hass_data_domain_dict():
    """Regression test (Issue #578 code review): the handler must NOT be
    stored inside hass.data[DOMAIN]. api.py's _get_coordinator() does
    `next(iter(hass.data[DOMAIN].values()))` to find "the" coordinator, and
    Python dicts are insertion-ordered — if install() ran before the
    coordinator was added to hass.data[DOMAIN] and stored the handler there,
    every REST view would resolve the log handler instead of the coordinator
    and crash with AttributeError. Simulates the real async_setup_entry()
    ordering: install() runs first, the coordinator is added second."""
    domain_data: dict = {}
    hass = _make_hass(domain_data)

    install(hass)
    domain_data["some-entry-id"] = object()  # stand-in for the coordinator

    assert next(iter(domain_data.values())) is domain_data["some-entry-id"]
    assert "log_capture_handler" not in domain_data

    uninstall(hass)
