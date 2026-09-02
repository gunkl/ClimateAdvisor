"""Tests for the real WARNING+/ERROR log-record ring buffer (Issue #578).

Replaces the AI Investigator's old "System Errors/Warnings" check, which only
tested whether a CA-internal event's `type` string happened to contain the
substring "error"/"warning" — coincidental naming, not severity.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import log_capture
from custom_components.climate_advisor.log_capture import (
    ClimateAdvisorLogRingBuffer,
    bind_zone_for_executor,
    get_handler,
    install,
    uninstall,
    zone_scope,
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


# --- Issue #812: zone attribution -----------------------------------------


def _capture_one_warning(zone_label: str | None, logger_name: str, message: str) -> dict:
    """Emit one WARNING under `zone_label` and return the captured record."""
    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        with zone_scope(zone_label):
            logger.warning(message)
    finally:
        logger.removeHandler(handler)
    return handler.get_records()[0]


def test_record_tagged_with_zone_during_scope():
    """A record captured while log_capture.zone_scope() is active is tagged
    with that zone — the core mechanism the AI Investigator's per-zone
    filtering (ai_skills_context.py) depends on."""
    record = _capture_one_warning("zone_a_entry_id", "custom_components.climate_advisor.test_zone_tag", "hello")
    assert record["zone"] == "zone_a_entry_id"


def test_record_outside_any_scope_gets_unknown_fallback():
    """A record captured with no zone_scope() active must fall back to None
    (log_capture's "unknown zone" marker), never silently attributed to
    whichever zone happened to run most recently. emit() must not raise even
    though the contextvar was never set in this context."""
    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger("custom_components.climate_advisor.test_zone_unset")
    logger.addHandler(handler)
    try:
        logger.warning("no zone context active")
    finally:
        logger.removeHandler(handler)

    record = handler.get_records()[0]
    assert record["zone"] is None


def test_zone_scope_resets_after_block_exits():
    """Once a zone_scope() block exits, a subsequent record captured outside
    any scope must NOT retain the previous zone's label — proves the
    ContextVar is reset, not just overwritten forward."""
    _capture_one_warning("zone_a", "custom_components.climate_advisor.test_zone_reset", "inside a")

    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger("custom_components.climate_advisor.test_zone_reset")
    logger.addHandler(handler)
    try:
        logger.warning("after a's scope exited")
    finally:
        logger.removeHandler(handler)

    record = handler.get_records()[0]
    assert record["zone"] is None


def test_concurrent_coordinators_do_not_cross_contaminate_zone_tags():
    """Two coordinators' update cycles running genuinely concurrently
    (asyncio.gather, not sequential awaits) must never leak one zone's label
    into the other's captured records. A broken/shared design (e.g. a plain
    module-level variable instead of a ContextVar) would pass a *sequential*
    version of this test trivially but fail here."""
    handler = ClimateAdvisorLogRingBuffer()
    logger = logging.getLogger("custom_components.climate_advisor.test_zone_concurrency")
    logger.addHandler(handler)

    async def _zone_cycle(zone_label: str, delay: float) -> None:
        with zone_scope(zone_label):
            await asyncio.sleep(delay)
            logger.warning("cycle warning for %s", zone_label)

    async def _run() -> None:
        # Staggered delays so the two cycles' bodies genuinely interleave
        # rather than one completing before the other starts.
        await asyncio.gather(
            _zone_cycle("zone_a", 0.05),
            _zone_cycle("zone_b", 0.01),
        )

    try:
        asyncio.run(_run())
    finally:
        logger.removeHandler(handler)

    records = {r["message"]: r["zone"] for r in handler.get_records()}
    assert records["cycle warning for zone_a"] == "zone_a"
    assert records["cycle warning for zone_b"] == "zone_b"


def test_contextvar_does_not_cross_executor_boundary_unwrapped():
    """Empirically confirms the part-1 finding this design depends on: a
    ContextVar set in a coroutine does NOT automatically propagate into work
    submitted via loop.run_in_executor() (what hass.async_add_executor_job()
    delegates to) — unlike loop.call_soon()/Task creation, executor.submit()
    is called directly with no contextvars.copy_context().run() wrapper.
    This is the reason bind_zone_for_executor() exists at all; if this test
    ever starts failing (i.e. propagation started working), the explicit
    wrapping in coordinator.py's _executor_job() would become redundant but
    still harmless — this test is the tripwire for that."""

    def _read_zone() -> str | None:
        return log_capture.current_zone_label()

    async def _run() -> str | None:
        with zone_scope("some_zone"):
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor() as pool:
                return await loop.run_in_executor(pool, _read_zone)

    result = asyncio.run(_run())
    assert result is None, (
        "ContextVar unexpectedly propagated across the executor boundary — "
        "if this fires, the empirical basis for bind_zone_for_executor() has "
        "changed and the design note in log_capture.py needs revisiting."
    )


def test_bind_zone_for_executor_restores_zone_inside_executor_thread():
    """bind_zone_for_executor() is the fix for the gap proven above: wrapping
    the executor target re-establishes the calling zone label inside the
    executor thread, so a _LOGGER call made by that target is tagged
    correctly."""

    def _read_zone() -> str | None:
        return log_capture.current_zone_label()

    async def _run() -> str | None:
        with zone_scope("zone_a"):
            wrapped = bind_zone_for_executor(_read_zone)
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor() as pool:
                return await loop.run_in_executor(pool, wrapped)

    result = asyncio.run(_run())
    assert result == "zone_a"


def test_bind_zone_for_executor_passes_args_through():
    """bind_zone_for_executor() must not swallow or reorder the wrapped
    function's own positional args — coordinator.py's _executor_job() relies
    on this to pass e.g. `learning.reset(scope)` unchanged."""

    def _add(a: int, b: int) -> int:
        return a + b

    wrapped = bind_zone_for_executor(_add)
    assert wrapped(2, 3) == 5


def test_bind_zone_for_executor_preserves_exceptions():
    """Exception propagation through the executor wrapper must be unchanged
    — a silently swallowed exception here would be worse than no zone
    tagging at all."""

    def _boom() -> None:
        raise ValueError("boom")

    wrapped = bind_zone_for_executor(_boom)
    with pytest.raises(ValueError, match="boom"):
        wrapped()


# --- Issue #812: consumer-side per-zone filtering in ai_skills_context.py --


def test_ai_skills_context_filters_to_investigated_zone():
    """Given a ring buffer mixing records from two zones plus an untagged
    record, investigating zone A's report must only surface zone A's records
    and the explicitly-labeled untagged one — never zone B's."""
    import asyncio as _asyncio

    from custom_components.climate_advisor import ai_skills_context

    hass = SimpleNamespace(data={})
    install(hass)
    logger = logging.getLogger("custom_components.climate_advisor.test_zone_filter")
    try:
        with zone_scope("zone_a"):
            logger.warning("zone a warning")
        with zone_scope("zone_b"):
            logger.warning("zone b warning")
        logger.warning("untagged warning")  # outside any scope

        coordinator = SimpleNamespace(zone_label="zone_a", _event_log=[])
        report = _asyncio.run(ai_skills_context.build_event_log_context(hass, coordinator, hours=24))
    finally:
        uninstall(hass)

    assert "zone a warning" in report
    assert "untagged warning" in report
    assert "zone b warning" not in report
