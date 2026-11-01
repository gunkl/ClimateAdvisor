"""Shared pytest fixtures for Climate Advisor tests."""

from __future__ import annotations

import os
import sys

# Ensure the project root is on sys.path so imports from
# custom_components.climate_advisor resolve correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install homeassistant.* stub modules — single source of truth lives in
# tools/sim_harness/ha_stubs.py so the test suite and headless harness
# stay automatically in sync.
from tools.sim_harness.ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

# Now safe to import Climate Advisor modules
import pytest  # noqa: E402

from custom_components.climate_advisor.classifier import ForecastSnapshot  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ha_dt():
    """Issue #1186: restore the shared ``homeassistant.util.dt`` stub's real defaults around
    every test so a clock frozen (or an attribute assigned) by one test/file never leaks."""
    dt_mod = sys.modules["homeassistant.util.dt"]
    dt_mod.reset()
    yield
    dt_mod.reset()


@pytest.fixture
def frozen_now():
    """Freeze ``dt_util.now()``/``utcnow()`` for the test: ``frozen_now(datetime(...))``.

    The value is returned as given (naive stays naive, so a test keeps its intended
    semantics); ``utcnow`` returns it made aware in UTC. Reset by ``_reset_ha_dt``.
    """
    dt_mod = sys.modules["homeassistant.util.dt"]

    def _freeze(value):
        dt_mod.now = lambda time_zone=None: value
        dt_mod.utcnow = lambda: value if value.tzinfo else value.replace(tzinfo=dt_mod.UTC)
        return value

    return _freeze


@pytest.fixture
def basic_forecast() -> ForecastSnapshot:
    """A typical mid-season ForecastSnapshot with stable trend."""
    return ForecastSnapshot(
        today_high=72.0,
        today_low=55.0,
        tomorrow_high=73.0,
        tomorrow_low=56.0,
        current_outdoor_temp=65.0,
        current_indoor_temp=70.0,
        current_humidity=45.0,
    )


# Issue #591: shared no-duplicate-decision assertion, generalized from the
# `len(x_events) == 1` idiom independently re-derived across ~12 test files
# (originally test_override_dedup.py for Issue #96). Event types that legitimately
# repeat with identical content across genuinely distinct decision points — NOT
# accidental multi-trigger echoes of the same decision — must be listed here rather
# than silently passing.
#
# Note: this check has no concept of elapsed time — it only looks at whether two
# CONSECUTIVE entries in an event list have identical (type, payload). occupancy_setback,
# hvac_write_blocked_whf_active, and morning_wakeup are all guarded in production code by
# a WINDOWED (window_seconds=600) _recent_duplicate() check, not a blanket exemption — a
# real same-cycle duplicate is already suppressed before it ever reaches the event log. But
# a genuine hours-apart re-confirmation (e.g. bedtime reapplying an away setback, or two
# separate real wake-ups) still appears as two real, separate entries here, and in a sparse
# scenario timeline can easily be adjacent with identical payload — so they still need to be
# listed here even though the underlying code IS protected. See the matching comments at
# their emit sites in automation.py for the code-level guard.
LEGITIMATELY_REPEATING_EVENT_TYPES = frozenset(
    {
        "occupancy_setback",
        "hvac_write_blocked_whf_active",
        "morning_wakeup",
        "occupancy_comfort_restored",
        # Issue #1050: windowed (_recent_duplicate, 600 s) at the emit site; hours-apart arrivals
        # held for the same reason legitimately repeat identical payloads.
        "occupancy_restore_deferred",
        "fan_activated",
        "fan_deactivated",
        "nat_vent_fan_on",
        "fan_manual_override",
        "sensor_opened",
        "all_sensors_closed",
        "setpoint_override_detected",
        "override_confirmed",
        "override_cleared",
        "grace_started",
        # Issue #1049: a recurring mismatch/recover/mismatch cycle against a thermostat that
        # keeps ignoring mode commands legitimately re-emits identical payloads; the
        # once-per-episode gate (next_fallback_episode) is what bounds them, not payload novelty.
        "hvac_mode_not_applied",
        "hvac_mode_applied_recovered",
        "hvac_command_recovered",
        # Issue #1048 change 9: a stall that recovers and re-opens (equipment ran briefly, then
        # the house drifted out of band again) re-emits identical payloads; the 3 h sustain clock
        # and the once-per-episode announcement gate bound them, not payload novelty.
        "hvac_output_stall",
        "hvac_output_stall_recovered",
    }
)


def assert_no_duplicate_events(
    events: list[tuple[str, dict]],
    *,
    within_seconds: float | None = None,
    allowlist: frozenset[str] = LEGITIMATELY_REPEATING_EVENT_TYPES,
) -> None:
    """Assert no event in ``events`` (a list of ``(event_type, payload)`` tuples, in emission
    order, as produced by the common ``engine._emit_event_callback = lambda name, payload:
    emitted.append((name, payload))`` test pattern) is immediately followed by another event
    of the same type with an identical payload — the Issue #584/#591 duplicate-decision shape.

    ``within_seconds`` is accepted for call-site documentation purposes (e.g. "within the same
    5-minute revisit window") but is NOT separately enforced here — callers that need real
    elapsed-time control should advance a mocked clock between calls, since this helper only
    inspects the emitted event list, not wall-clock time.

    Event types in ``allowlist`` are skipped — they are expected to legitimately repeat.
    """
    for (prev_type, prev_payload), (cur_type, cur_payload) in zip(events, events[1:], strict=False):
        if prev_type != cur_type or prev_type in allowlist:
            continue
        assert prev_payload != cur_payload, (
            f"Duplicate decision: {cur_type!r} emitted twice in a row with identical payload "
            f"{cur_payload!r} — this is the Issue #584/#591 duplicate-decision bug shape. If "
            f"this event type legitimately repeats with identical content across genuinely "
            f"distinct decision points, add it to LEGITIMATELY_REPEATING_EVENT_TYPES in "
            f"conftest.py with a comment explaining why."
        )


# ---------------------------------------------------------------------------
# Missing-dependency safety net (Issue #1126)
#
# Required packages are installed by `uv sync --only-group test`. A test that
# skips because a package is absent (or stubbed) would silently stop guarding
# behaviour on that machine, so such skips are converted into failures.
# ---------------------------------------------------------------------------
import re as _re  # noqa: E402

# Matches only messages that say a package/module is absent or stubbed. The bare word
# "unavailable" is deliberately NOT here: this domain skips legitimately on "sensor unavailable"
# style conditions, and those must stay skips.
_MISSING_DEP_SKIP = _re.compile(
    r"not installed|no module named|is a stub|importorskip|could not import|cannot import name"
    r"|(?:dependency|module|package|library) (?:is |are )?(?:missing|unavailable|not available)",
    _re.IGNORECASE,
)


def _missing_dep_reason(report) -> str | None:
    """Return the skip reason when a skipped report is a missing-dependency skip, else None."""
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    return reason if _MISSING_DEP_SKIP.search(reason) else None


def _fail_missing_dep(report, reason: str) -> None:
    report.outcome = "failed"
    report.longrepr = (
        f"Skipped for a missing dependency, which is not allowed: {reason!r}. "
        "Run `python scripts/test.py` (uv sync --only-group test) to install the required packages."
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and (reason := _missing_dep_reason(report)):
        _fail_missing_dep(report, reason)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    """Same rule at collection time: module-level ``importorskip`` / ``skip(allow_module_level=True)``."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped and (reason := _missing_dep_reason(report)):
        _fail_missing_dep(report, reason)


# ---------------------------------------------------------------------------
# Unawaited-coroutine leak guard (Issue #1126)
#
# FakeHass.async_create_task() hands coroutines (the thermostat state listener,
# ``_notify_in_background._send`` ...) to the FakeScheduler queue, which only runs them
# when the virtual clock is advanced. A test that calls ``run_coro(coord._async_update_data())``
# without advancing leaves them queued; they are then garbage-collected unawaited during a
# LATER, unrelated test, where warnings-as-errors fails that victim at random. Closing the
# never-run coroutines at teardown is what ``_consume_coroutine`` does for MagicMock hosts.
# A test that needs the queued work's effects must still drain it itself (advance_by(0)).
# ---------------------------------------------------------------------------
import weakref as _weakref  # noqa: E402

from tools.sim_harness.fake_scheduler import FakeScheduler as _FakeScheduler  # noqa: E402

_LIVE_SCHEDULERS: _weakref.WeakSet = _weakref.WeakSet()
_orig_scheduler_init = _FakeScheduler.__init__


def _tracking_scheduler_init(self, *args, **kwargs):
    _orig_scheduler_init(self, *args, **kwargs)
    _LIVE_SCHEDULERS.add(self)


_FakeScheduler.__init__ = _tracking_scheduler_init


@pytest.fixture(autouse=True)
def _close_undrained_scheduler_tasks():
    yield
    for scheduler in list(_LIVE_SCHEDULERS):
        queue = scheduler._task_queue
        while queue:
            coro = queue.pop()
            close = getattr(coro, "close", None)
            if close is not None:
                close()
