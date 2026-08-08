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
