"""Harness mapping of the setback / bedtime / wake-up rows (Issues #1267 / #1596, owner-approved option 1).

Occupant framing: these rows are emitted after the band write and say whether it reached the thermostat. A row whose
band was NOT sent (whole-house fan owns the thermostat, blower hold, open window, no capable mode, test mode, failed)
must not be judged "setback_applied" / "comfort_restored": that would let a scenario pass while the occupant's
thermostat never got the setback. Such a row is not a decision of its own -- the write path's event (or the session in
force) stays the outcome. Sent rows and rows from old logs (no ``outcome``) keep their mapping.
"""

from __future__ import annotations

import pytest

from tools.sim_harness.outcomes import ProductionDecision, _map_event_to_outcome, production_outcome_at

_TS = "2026-07-15T22:30:00"
_ROWS = {
    "occupancy_setback": (
        {"mode": "away", "occupancy": "away", "floor": 62.0, "ceiling": 80.0},
        "setback_applied",
        80.0,
    ),
    "bedtime_setback": ({"mode": "cool", "floor": 64.0, "ceiling": 72.0, "active": "ceiling"}, "setback_applied", 72.0),
    "morning_wakeup": ({"mode": "cool", "floor": 68.0, "ceiling": 74.0, "active": "ceiling"}, "comfort_restored", 74.0),
}
_NOT_SENT = [
    {"outcome": "blocked", "held_reason": "whf_session"},
    {"outcome": "not_sent", "held_reason": "blower_hold"},
    {"outcome": "not_sent", "held_reason": "window_open"},
    {"outcome": "not_sent", "held_reason": "no_capable_mode"},
    {"outcome": "dry_run"},
    {"outcome": "failed"},
    {"outcome": "superseded"},
]


@pytest.mark.parametrize("kind", list(_ROWS))
def test_sent_row_keeps_its_outcome(kind):
    payload, outcome, target = _ROWS[kind]
    d = _map_event_to_outcome(kind, {**payload, "outcome": "sent"}, _TS)
    assert (d.outcome, d.target_temp) == (outcome, target)


@pytest.mark.parametrize("kind", list(_ROWS))
def test_legacy_row_without_outcome_keeps_its_outcome(kind):
    payload, outcome, target = _ROWS[kind]
    d = _map_event_to_outcome(kind, dict(payload), _TS)
    assert (d.outcome, d.target_temp) == (outcome, target)


@pytest.mark.parametrize("fields", _NOT_SENT, ids=lambda f: f.get("held_reason") or f["outcome"])
@pytest.mark.parametrize("kind", list(_ROWS))
def test_not_sent_row_is_not_a_decision(kind, fields):
    payload, _outcome, _target = _ROWS[kind]
    assert _map_event_to_outcome(kind, {**payload, **fields}, _TS) is None


def test_not_sent_row_leaves_the_write_paths_outcome_in_force():
    """The away transition while the whole-house fan runs: the outcome in force stays the refused write."""
    rows = [
        ProductionDecision(_TS, "hvac_write_blocked_whf_active", "hvac_write_blocked_whf_active"),
        _map_event_to_outcome(
            "occupancy_setback",
            {**_ROWS["occupancy_setback"][0], "outcome": "blocked", "held_reason": "whf_session"},
            _TS,
        ),
    ]
    assert production_outcome_at([r for r in rows if r is not None], _TS) == "hvac_write_blocked_whf_active"
