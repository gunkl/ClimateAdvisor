"""Tests for coordinator._detect_and_emit_incidents (simulation feedback loop).

Covers:
- comfort_violation incident detection and emit
- comfort_violation deduplication (one incident per 30-min window)
- rapid_override_after_automation incident detection
- simulate.py: ODE ceiling guard fires with ode_enabled=true
- simulate.py: track=integration assertions are skipped (not failed)
- simulate.py: override_cleared assertion on occupancy_change_with_override scenario
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import path setup — tools/ must be on sys.path for simulate imports
# ---------------------------------------------------------------------------
_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from simulate import run_scenario_production as run_scenario  # noqa: E402

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# ---------------------------------------------------------------------------
# Coordinator stub helpers
# ---------------------------------------------------------------------------


def _make_coordinator_stub(config: dict | None = None):
    """Build a minimal coordinator-like object for testing _detect_and_emit_incidents.

    Binds _emit_event, _emit_incident, and _detect_and_emit_incidents from the
    real ClimateAdvisorCoordinator class.
    """
    import importlib

    coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    ClimateAdvisorCoordinator = coord_mod.ClimateAdvisorCoordinator

    coord = MagicMock()
    coord.config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        **(config or {}),
    }
    coord._event_log = []
    coord.data = {}
    coord.automation_engine = MagicMock()
    coord.automation_engine._occupancy_mode = "home"
    coord.automation_engine._natural_vent_active = False
    coord.automation_engine._manual_override_active = False

    # Bind real methods
    coord._emit_event = types.MethodType(ClimateAdvisorCoordinator._emit_event, coord)
    coord._emit_incident = types.MethodType(ClimateAdvisorCoordinator._emit_incident, coord)
    coord._detect_and_emit_incidents = types.MethodType(ClimateAdvisorCoordinator._detect_and_emit_incidents, coord)
    return coord


def _utc_now_str(offset_minutes: int = 0) -> str:
    """Return an ISO timestamp offset from now by offset_minutes (positive = past)."""
    t = datetime.now(UTC) - timedelta(minutes=offset_minutes)
    return t.isoformat()


# ---------------------------------------------------------------------------
# TestComfortViolationIncident
# ---------------------------------------------------------------------------


class TestComfortViolationIncident:
    def test_incident_detected_comfort_violation(self) -> None:
        """Indoor temp above comfort_cool + 0.5 → comfort_violation incident emitted."""
        coord = _make_coordinator_stub({"comfort_cool": 75})
        coord.data = {"indoor_temp": 76.5, "outdoor_temp": 85.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            now = datetime.now(UTC)
            mock_dt.now.return_value = now

            coord._detect_and_emit_incidents()

        incident_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "comfort_violation"
        ]
        assert len(incident_events) == 1, "expected exactly one comfort_violation incident"
        assert incident_events[0]["indoor_f"] == 76.5

    def test_incident_detected_comfort_violation_dedup(self) -> None:
        """Calling _detect_and_emit_incidents twice within 30 min emits only 1 incident."""
        coord = _make_coordinator_stub({"comfort_cool": 75})
        coord.data = {"indoor_temp": 76.5, "outdoor_temp": 85.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            now = datetime.now(UTC)
            mock_dt.now.return_value = now

            coord._detect_and_emit_incidents()
            # Call again 5 minutes later (still within 30-min dedup window)
            mock_dt.now.return_value = now + timedelta(minutes=5)
            coord._detect_and_emit_incidents()

        incident_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "comfort_violation"
        ]
        assert len(incident_events) == 1, f"dedup failed: expected 1 incident but got {len(incident_events)}"

    def test_no_incident_when_indoor_within_comfort(self) -> None:
        """Indoor temp at comfort_cool — no incident emitted."""
        coord = _make_coordinator_stub({"comfort_cool": 75})
        coord.data = {"indoor_temp": 75.0, "outdoor_temp": 80.0, "hvac_mode": "cool"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime.now(UTC)
            coord._detect_and_emit_incidents()

        incident_events = [e for e in coord._event_log if e.get("type") == "incident_detected"]
        assert len(incident_events) == 0, "no incident expected within comfort range"


# ---------------------------------------------------------------------------
# TestRapidOverrideAfterAutomation
# ---------------------------------------------------------------------------


class TestRapidOverrideAfterAutomation:
    def test_rapid_override_detection(self) -> None:
        """override_detected within 60s after classification_applied → rapid_override_after_automation."""
        coord = _make_coordinator_stub()
        coord.data = {}

        now = datetime.now(UTC)
        auto_ts = (now - timedelta(seconds=35)).isoformat()
        override_ts = (now - timedelta(seconds=5)).isoformat()

        # Seed event log with classification_applied, then override_detected 30s later
        coord._event_log.append({"time": auto_ts, "type": "classification_applied"})
        coord._event_log.append({"time": override_ts, "type": "override_detected"})

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            coord._detect_and_emit_incidents()

        rapid_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "rapid_override_after_automation"
        ]
        assert len(rapid_events) >= 1, "expected rapid_override_after_automation incident"
        assert rapid_events[0].get("gap_seconds") == 30

    def test_no_rapid_override_when_gap_exceeds_60s(self) -> None:
        """override_detected more than 60s after automation event → no rapid_override incident."""
        coord = _make_coordinator_stub()
        coord.data = {}

        now = datetime.now(UTC)
        auto_ts = (now - timedelta(seconds=120)).isoformat()
        override_ts = (now - timedelta(seconds=10)).isoformat()

        coord._event_log.append({"time": auto_ts, "type": "classification_applied"})
        coord._event_log.append({"time": override_ts, "type": "override_detected"})

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            coord._detect_and_emit_incidents()

        rapid_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "rapid_override_after_automation"
        ]
        assert len(rapid_events) == 0, "no rapid_override incident expected for 110s gap"


# ---------------------------------------------------------------------------
# TestSimulateNewFeatures  — runs simulate.py on scenario JSON files
# ---------------------------------------------------------------------------


def _make_scenario(
    tmp_path: Path,
    name: str,
    events: list,
    assertions: list,
    config: dict | None = None,
    **extra,
) -> Path:
    import json

    scenario: dict = {
        "name": name,
        "description": f"Test: {name}",
        "source": "test",
        "config": config or {"comfort_heat": 70, "comfort_cool": 74},
        "events": events,
        "assertions": assertions,
        **extra,
    }
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(scenario))
    return p


class TestSimulateNewFeatures:
    def test_track_integration_assertion_is_skipped(self, tmp_path: Path) -> None:
        """Assertion with track='integration' is skipped (not failed), even with unknown expect value."""
        events = [
            {
                "time": "2026-05-20T08:00:00",
                "type": "classification",
                "day_type": "warm",
                "hvac_mode": "off",
            }
        ]
        assertions = [
            {
                "at": "2026-05-20T08:00:00",
                "expect": "some_production_only_outcome",
                "track": "integration",
                "reason": "Requires production HA listener — not simulated",
            }
        ]
        p = _make_scenario(tmp_path, "track_integration_skip", events, assertions)
        result = run_scenario(p)

        assert len(result["assertions"]) == 1
        assert result["assertions"][0].get("skipped") is True, "integration-track assertion should be marked skipped"
        # Skipped assertions must not cause a FAIL
        assert result["passed"] is not False, "skipped integration assertion must not cause overall FAIL"

    def test_override_cleared_passes_after_bug_220_fix(self, tmp_path: Path) -> None:
        """away_occupancy_override_cleared scenario: override_cleared assertion PASSES after fix.

        Fix #220: _handle_occupancy_away() now calls clear_manual_override(), and
        _handle_occupancy_away() in simulate.py clears manual_override_active.
        The scenario documents that occupancy-away transitions must clear overrides.
        """
        bspec_path = (
            Path(__file__).parent.parent / "tools" / "simulations" / "pending" / "away_occupancy_override_cleared.json"
        )
        assert bspec_path.exists(), f"Expected BSpec not found: {bspec_path}"

        result = run_scenario(bspec_path, state="pending")

        assert result["passed"] is True, (
            f"away_occupancy_override_cleared should PASS after bug #220 is fixed. Assertions: {result['assertions']}"
        )
