"""Issue #1193: the harness maps ``occupancy_setback_suppressed_paused`` to its own outcome.

Occupant outcome: when the occupant leaves while a door/window pause owns the HVAC, the
away setback is deliberately NOT applied.  The simulator must report that production
decision as ``setback_suppressed_paused`` -- not as the ``unknown:`` placeholder it used
before, and not as ``setback_applied`` / ``paused`` -- so that a scenario asserting it
fails if production stops emitting the suppression or starts applying the setback.

These tests call the real mapping function (``_map_event_to_outcome``) and the real
``production_outcome_at`` lookup; nothing is re-implemented here.
"""

from __future__ import annotations

from tools.sim_harness.outcomes import ProductionDecision, _map_event_to_outcome, production_outcome_at

_TS = "2026-07-10T10:00:00"


def test_suppressed_paused_maps_to_its_own_outcome() -> None:
    decision = _map_event_to_outcome("occupancy_setback_suppressed_paused", {}, _TS)

    assert decision is not None
    assert decision.event_type == "occupancy_setback_suppressed_paused"
    assert decision.outcome == "setback_suppressed_paused"
    assert decision.target_temp is None
    assert not decision.outcome.startswith("unknown:")


def test_suppressed_paused_is_distinct_from_applied_setback_and_pause() -> None:
    suppressed = _map_event_to_outcome("occupancy_setback_suppressed_paused", {}, _TS)
    applied = _map_event_to_outcome("occupancy_setback", {"ceiling": 79.0}, _TS)

    assert suppressed is not None and applied is not None
    assert applied.outcome == "setback_applied"
    assert suppressed.outcome != applied.outcome
    assert suppressed.outcome != "paused"


def test_suppressed_paused_is_visible_to_outcome_lookup() -> None:
    decision = _map_event_to_outcome("occupancy_setback_suppressed_paused", {}, _TS)
    assert isinstance(decision, ProductionDecision)

    assert production_outcome_at([decision], _TS) == "setback_suppressed_paused"
