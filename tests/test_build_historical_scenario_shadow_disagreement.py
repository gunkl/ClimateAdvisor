"""Tests for tools/build_historical_scenario.py's shadow_disagreement support (Issue #738).

Covers the pure/near-pure pieces of the incident->BSpec pipeline for the
`shadow_disagreement` incident class:
- _preceding_production_outcome(): finds production's real, mapped decision
  outcome at/before the incident timestamp, reusing
  tools/sim_harness/outcomes.py's _map_event_to_outcome() (the same mapping
  simulate.py applies at validation time) rather than re-deriving it.
- find_shadow_disagreement_windows(): extracts chart-log context windows around
  shadow_disagreement incident_detected events, mirroring the existing
  find_setpoint_mode_inconsistency_windows()/find_rapid_override_windows() shape.
- build_scenario_json(): produces a BSpec asserting on production's own real
  recorded decision (genuinely evaluable by the offline single-engine harness),
  with the shadow/FSM disagreement itself recorded in `notes` for human review
  rather than as a mechanically-checked assertion — the offline harness never
  constructs a second "shadow" engine to compare against.

Does not exercise the SSH/HA-API fetch path (fetch_chart_log_ssh, _fetch_event_log)
or main() end-to-end — those are inherently live/manual per the existing pipeline's
own design (docs/simulation-feedback-loop.md), same as the other find_*_windows
functions have never had that layer unit-tested either.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from build_historical_scenario import (  # noqa: E402
    _preceding_production_outcome,
    build_scenario_json,
    find_shadow_disagreement_windows,
)


def _chart_entry(ts: datetime, indoor: float, outdoor: float, hvac: str = "off", fan: bool = False) -> dict:
    return {
        "ts": ts.isoformat(),
        "indoor": indoor,
        "outdoor": outdoor,
        "hvac": hvac,
        "fan": fan,
        "windows_open": False,
    }


def _incident_event(
    ts: datetime,
    axis: str = "mirror",
    production_state: str = "active",
    comparison_state: str = "inactive",
    comparison_kind: str = "shadow",
    disagreement_seconds: float = 950.0,
) -> dict:
    return {
        "time": ts.isoformat(),
        "type": "incident_detected",
        "incident_class": "shadow_disagreement",
        "incident_id": ts.isoformat(),
        "axis": axis,
        "production_state": production_state,
        "comparison_state": comparison_state,
        "comparison_kind": comparison_kind,
        "disagreement_seconds": disagreement_seconds,
        "indoor_f": 72.0,
        "outdoor_f": 68.0,
        "hvac_mode": "off",
        "comfort_heat": 70.0,
        "comfort_cool": 75.0,
        "occupancy_mode": "home",
    }


def _classification_applied_event(ts: datetime) -> dict:
    """A real, mappable production decision event (see outcomes.py's
    _map_event_to_outcome — classification_applied -> 'classification_applied')."""
    return {"time": ts.isoformat(), "type": "classification_applied"}


def _warm_day_setback_event(ts: datetime, new_setpoint_f: float = 68.0) -> dict:
    """Maps to outcome 'setback_applied' with a target_temp — used to prove
    _preceding_production_outcome() genuinely reuses _map_event_to_outcome()
    rather than just echoing the raw event type string."""
    return {"time": ts.isoformat(), "type": "warm_day_setback_applied", "new_setpoint_f": new_setpoint_f}


class TestPrecedingProductionOutcome:
    """Direct unit tests of the pure helper introduced by the Issue #738 fix."""

    def test_finds_mapped_outcome_at_or_before_incident_ts(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now
        preceding_ts = now - timedelta(minutes=5)
        event_entries = [_classification_applied_event(preceding_ts)]

        outcome = _preceding_production_outcome(event_entries, incident_ts)

        assert outcome == "classification_applied"

    def test_reuses_real_outcome_mapping_not_raw_event_type(self) -> None:
        """warm_day_setback_applied maps to 'setback_applied', not its own event
        type string — proves this helper calls the real _map_event_to_outcome()
        rather than a shortcut like `return ev["type"]`."""
        now = datetime.now(UTC)
        incident_ts = now
        preceding_ts = now - timedelta(minutes=5)
        event_entries = [_warm_day_setback_event(preceding_ts)]

        outcome = _preceding_production_outcome(event_entries, incident_ts)

        assert outcome == "setback_applied"

    def test_ignores_events_after_incident_ts(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(minutes=10)
        after_ts = now  # after the incident
        event_entries = [_classification_applied_event(after_ts)]

        assert _preceding_production_outcome(event_entries, incident_ts) is None

    def test_ignores_incident_detected_events(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now
        preceding_ts = now - timedelta(minutes=5)
        event_entries = [_incident_event(preceding_ts)]

        assert _preceding_production_outcome(event_entries, incident_ts) is None

    def test_returns_none_when_no_events(self) -> None:
        assert _preceding_production_outcome([], datetime.now(UTC)) is None

    def test_picks_most_recent_of_multiple_preceding_events(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now
        earlier_ts = now - timedelta(minutes=20)
        later_ts = now - timedelta(minutes=5)
        event_entries = [
            _warm_day_setback_event(earlier_ts),
            _classification_applied_event(later_ts),
        ]

        outcome = _preceding_production_outcome(event_entries, incident_ts)

        assert outcome == "classification_applied"


class TestFindShadowDisagreementWindows:
    def test_finds_window_around_incident(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [
            _chart_entry(incident_ts - timedelta(minutes=10), 71.0, 65.0),
            _chart_entry(incident_ts, 72.0, 68.0),
            _chart_entry(incident_ts + timedelta(minutes=10), 72.5, 69.0),
        ]
        event_entries = [_incident_event(incident_ts, axis="mirror")]

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        assert len(windows) == 1
        window = windows[0]
        assert window["axis"] == "mirror"
        assert window["comparison_kind"] == "shadow"
        assert window["production_state"] == "active"
        assert window["comparison_state"] == "inactive"
        assert len(window["entries"]) == 3
        assert "shadow_disagreement_mirror" in window["description"]

    def test_no_event_entries_returns_empty(self) -> None:
        chart_entries = [_chart_entry(datetime.now(UTC), 72.0, 68.0)]
        assert find_shadow_disagreement_windows(chart_entries, [], hours=72) == []

    def test_ignores_other_incident_classes(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [
            {
                "time": incident_ts.isoformat(),
                "type": "incident_detected",
                "incident_class": "comfort_violation",
            }
        ]
        assert find_shadow_disagreement_windows(chart_entries, event_entries, hours=72) == []

    def test_ignores_events_outside_hours_cutoff(self) -> None:
        now = datetime.now(UTC)
        old_ts = now - timedelta(hours=200)
        chart_entries = [_chart_entry(old_ts, 72.0, 68.0)]
        event_entries = [_incident_event(old_ts)]
        assert find_shadow_disagreement_windows(chart_entries, event_entries, hours=72) == []

    def test_skips_window_with_no_matching_chart_entries(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        # Chart entries far outside the +/-30/15 min window around the incident.
        chart_entries = [_chart_entry(incident_ts - timedelta(hours=5), 72.0, 68.0)]
        event_entries = [_incident_event(incident_ts)]
        assert find_shadow_disagreement_windows(chart_entries, event_entries, hours=72) == []

    def test_axis_filter_selects_only_matching_axis(self) -> None:
        now = datetime.now(UTC)
        ts1 = now - timedelta(hours=1)
        ts2 = now - timedelta(hours=2)
        chart_entries = [
            _chart_entry(ts1, 72.0, 68.0),
            _chart_entry(ts2, 73.0, 70.0),
        ]
        event_entries = [
            _incident_event(ts1, axis="mirror"),
            _incident_event(ts2, axis="fan_mirror"),
        ]

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72, axis="fan_mirror")

        assert len(windows) == 1
        assert windows[0]["axis"] == "fan_mirror"

    def test_axis_none_includes_all_axes(self) -> None:
        now = datetime.now(UTC)
        ts1 = now - timedelta(hours=1)
        ts2 = now - timedelta(hours=2)
        chart_entries = [
            _chart_entry(ts1, 72.0, 68.0),
            _chart_entry(ts2, 73.0, 70.0),
        ]
        event_entries = [
            _incident_event(ts1, axis="mirror"),
            _incident_event(ts2, axis="fan_mirror"),
        ]

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72, axis=None)

        assert {w["axis"] for w in windows} == {"mirror", "fan_mirror"}

    def test_carries_disagreement_seconds_from_incident(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [_incident_event(incident_ts, disagreement_seconds=1234.0)]

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        assert windows[0]["disagreement_seconds"] == 1234.0

    def test_carries_preceding_production_outcome_when_derivable(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        preceding_ts = incident_ts - timedelta(minutes=5)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [
            _classification_applied_event(preceding_ts),
            _incident_event(incident_ts),
        ]

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        assert windows[0]["preceding_production_outcome"] == "classification_applied"

    def test_preceding_production_outcome_none_when_not_derivable(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [_incident_event(incident_ts)]  # no preceding decision event

        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        assert windows[0]["preceding_production_outcome"] is None


class TestBuildScenarioJsonShadowDisagreement:
    def test_asserts_on_production_real_outcome_not_shadow_agreement(self) -> None:
        """Issue #738 fix: the assertion must be genuinely evaluable by the
        offline single-engine harness — it asserts production's own recorded
        decision (checked via simulate.py's default production_outcome_at()
        fallback path), NOT a fake 'shadow_engine_agrees' expect string with no
        check_assertion() branch and no second engine to compare against."""
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        preceding_ts = incident_ts - timedelta(minutes=5)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [
            _classification_applied_event(preceding_ts),
            _incident_event(
                incident_ts,
                axis="override_grace_fsm",
                production_state="confirmed/active",
                comparison_state="pending/active",
                comparison_kind="fsm",
            ),
        ]
        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)
        assert len(windows) == 1

        scenario = build_scenario_json(windows[0], "shadow_disagreement", comfort_cool_f=75.0, comfort_heat_f=70.0)

        assert scenario["verdict"]["type"] == "pending"
        assert scenario["issue"] == "#738"

        incident_events = [e for e in scenario["events"] if e.get("type") == "incident_detected"]
        assert len(incident_events) == 1
        incident_event = incident_events[0]
        assert incident_event["incident_class"] == "shadow_disagreement"
        assert incident_event["axis"] == "override_grace_fsm"

        assert len(scenario["assertions"]) == 1
        assertion = scenario["assertions"][0]
        # The real, checkable production outcome — not the unevaluable
        # "shadow_engine_agrees" string.
        assert assertion["expect"] == "classification_applied"
        assert assertion["track"] == "logic"
        assert "shadow_engine_agrees" not in str(scenario["assertions"])

        # The disagreement itself (production vs. shadow/FSM state) is documented
        # in notes for human review, not asserted on.
        notes_blob = " ".join(scenario["notes"])
        assert "override_grace_fsm" in notes_blob
        assert "confirmed/active" in notes_blob
        assert "pending/active" in notes_blob

    def test_no_assertion_when_no_preceding_production_decision(self) -> None:
        """When nothing evaluable is derivable from the window (no preceding
        mappable production decision), ship with an empty assertions list —
        same precedent find_comfort_violations()/find_nat_vent_windows() set for
        incident types where 'what should have happened' isn't known offline.
        The disagreement must still be recorded in notes."""
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [_incident_event(incident_ts, axis="fan_mirror")]
        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        scenario = build_scenario_json(windows[0], "shadow_disagreement", comfort_cool_f=75.0, comfort_heat_f=70.0)

        assert scenario["assertions"] == []
        notes_blob = " ".join(scenario["notes"])
        assert "fan_mirror" in notes_blob

    def test_scenario_name_is_deterministic_from_window(self) -> None:
        now = datetime.now(UTC)
        incident_ts = now - timedelta(hours=1)
        chart_entries = [_chart_entry(incident_ts, 72.0, 68.0)]
        event_entries = [_incident_event(incident_ts, axis="mirror")]
        windows = find_shadow_disagreement_windows(chart_entries, event_entries, hours=72)

        scenario = build_scenario_json(windows[0], "shadow_disagreement", comfort_cool_f=75.0, comfort_heat_f=70.0)

        assert "shadow_disagreement" in scenario["name"]
        assert scenario["config"] == {"comfort_heat": 70.0, "comfort_cool": 75.0}
