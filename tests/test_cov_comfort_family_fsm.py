"""Edge branches of the comfort-family FSM shell (Silver test-coverage).

Occupant view: a clock the guard cannot read (naive vs aware) must never lock the thermostat on the wrong side;
an unrecognised default family leaves the family unknown rather than guessing heat or cool.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.climate_advisor.comfort_family_decision import ComfortFamilyOutcome
from custom_components.climate_advisor.comfort_family_fsm import (
    ComfortFamilyDwellState,
    ComfortFamilyEvent,
    ComfortFamilyEventKind,
    ComfortFamilyFsmInputs,
    ComfortFamilyState,
    transition,
)

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
H = ComfortFamilyState.HEATING
C = ComfortFamilyState.COOLING


def _event(indoor=73.0, **overrides) -> ComfortFamilyEvent:
    params = {
        "default_family": "cooling",
        "indoor": indoor,
        "floor": 68.0,
        "ceiling": 74.0,
        "margin_f": 2.0,
        "natural_vent_active": False,
        "whf_owns_hvac": False,
        "now": _NOW,
        "min_dwell_seconds": 600.0,
    }
    params.update(overrides)
    return ComfortFamilyEvent(kind=ComfortFamilyEventKind.TICK, inputs=ComfortFamilyFsmInputs(**params))


def test_unrecognised_default_family_leaves_an_unknown_state_unknown() -> None:
    # A band too narrow for the margin: the leaf holds the default family, which names neither side.
    t = transition(None, _event(indoor=71.0, floor=70.0, ceiling=72.0, default_family="auto"))
    assert t.decision.outcome is ComfortFamilyOutcome.HOLD and t.decision.target_family == "auto"
    assert t.to_state is None and not t.changed


def test_a_naive_mode_change_mark_is_ignored_and_the_aware_one_still_locks() -> None:
    dwell = ComfortFamilyDwellState(dwell_since=_NOW - timedelta(seconds=60))  # aware, inside the window
    naive_mark = (_NOW - timedelta(seconds=30)).replace(tzinfo=None)
    t = transition(H, _event(last_mode_change_at=naive_mark), dwell)
    assert t.locked_out is True and t.to_state == H  # the unusable naive mark did not clear the lock


def test_an_unusable_dwell_clock_does_not_lock_the_switch() -> None:
    naive = (_NOW - timedelta(seconds=30)).replace(tzinfo=None)  # only mark, naive vs the aware "now"
    t = transition(H, _event(), ComfortFamilyDwellState(dwell_since=naive))
    assert t.locked_out is False and t.to_state == C
    assert t.dwell_state.dwell_since == _NOW
