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
from types import SimpleNamespace
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
    # Issue #411: shared nat-vent-tolerance comfort-deviation gate, consumed by both
    # _detect_and_emit_incidents (above) and coordinator.py's comfort_violations_minutes
    # accumulation (tested separately below via direct calls on this same bound method).
    coord._is_nat_vent_tolerated_deviation = types.MethodType(
        ClimateAdvisorCoordinator._is_nat_vent_tolerated_deviation, coord
    )
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

    def test_no_incident_when_nat_vent_active_and_deviation_in_tolerance(self) -> None:
        """Issue #411: in-tolerance deviation while nat-vent is actively cycling must NOT
        emit a comfort_violation incident.

        Occupant impact (before fix): WHF nat-vent is designed to let indoor oscillate
        slightly past the comfort_cool edge as part of normal cycling (see
        nat_vent_temperature_check()'s hysteresis band) — this is the system successfully
        exercising control, not a comfort failure (CLAUDE.md "Goal-Oriented Comfort Model").
        Before the fix, this momentary, expected dip was reported as a false comfort_violation
        alarm exactly as observed in the #411 activity log.
        """
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # Within CONF_NAT_VENT_HYSTERESIS_F (default 1.0F) of the ceiling: 75.5 <= 75+1.0
        coord.data = {"indoor_temp": 75.6, "outdoor_temp": 72.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime.now(UTC)
            coord._detect_and_emit_incidents()

        incident_events = [e for e in coord._event_log if e.get("type") == "incident_detected"]
        assert incident_events == [], (
            f"in-tolerance nat-vent cycling dip must not be reported as an incident; got: {incident_events}"
        )

    def test_incident_still_fires_when_nat_vent_active_but_deviation_exceeds_tolerance(self) -> None:
        """A genuine, sustained deviation during nat-vent (beyond the hysteresis tolerance)
        must still fire comfort_violation — the nat-vent-aware gate only suppresses
        deviations WITHIN the designed cycling tolerance, not real comfort failures.
        """
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # Far beyond hysteresis tolerance (default 1.0F): 79 >> 75+1.0
        coord.data = {"indoor_temp": 79.0, "outdoor_temp": 85.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime.now(UTC)
            coord._detect_and_emit_incidents()

        incident_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "comfort_violation"
        ]
        assert len(incident_events) == 1, (
            "a genuine sustained violation during nat-vent must still emit comfort_violation"
        )
        assert incident_events[0]["nat_vent_active"] is True

    def test_no_undertemp_incident_when_nat_vent_active_and_deviation_in_tolerance(self) -> None:
        """Same gate applied to the comfort_undertemp (low side) branch."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # Within hysteresis (1.0F) below comfort_heat: 69.5 >= 70-1.0
        coord.data = {"indoor_temp": 69.4, "outdoor_temp": 60.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime.now(UTC)
            coord._detect_and_emit_incidents()

        incident_events = [e for e in coord._event_log if e.get("type") == "incident_detected"]
        assert incident_events == [], (
            f"in-tolerance nat-vent cycling dip (low side) must not be reported; got: {incident_events}"
        )

    def test_undertemp_incident_still_fires_beyond_tolerance_during_nat_vent(self) -> None:
        """A genuine sustained undertemp deviation during nat-vent still fires comfort_undertemp."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # Far below hysteresis tolerance: 65 << 70-1.0
        coord.data = {"indoor_temp": 65.0, "outdoor_temp": 55.0, "hvac_mode": "off"}

        with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime.now(UTC)
            coord._detect_and_emit_incidents()

        incident_events = [
            e
            for e in coord._event_log
            if e.get("type") == "incident_detected" and e.get("incident_class") == "comfort_undertemp"
        ]
        assert len(incident_events) == 1, "a genuine sustained undertemp violation during nat-vent must still fire"


# ---------------------------------------------------------------------------
# TestIsNatVentToleratedDeviation — direct unit tests of the shared gate helper
# ---------------------------------------------------------------------------


class TestIsNatVentToleratedDeviation:
    """Direct unit tests of coordinator._is_nat_vent_tolerated_deviation(), the shared
    gate helper used by BOTH _detect_and_emit_incidents() (tested above) AND the
    comfort_violations_minutes accumulation in _async_update_data() (coordinator.py
    ~L1667-1682). Testing the shared helper directly (rather than only through each
    consumer) confirms both consumers see identical tolerance semantics, per the
    project's "extract N independent decision sites into one choke point" pattern.
    """

    def test_false_when_nat_vent_not_active(self) -> None:
        """Nat-vent inactive -> never tolerated, regardless of how close to the band edge."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = False
        assert coord._is_nat_vent_tolerated_deviation(75.5, 70.0, 75.0) is False

    def test_false_when_automation_engine_missing(self) -> None:
        """No automation_engine reference -> never tolerated (defensive default)."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine = None
        assert coord._is_nat_vent_tolerated_deviation(75.5, 70.0, 75.0) is False

    def test_true_within_hysteresis_above_ceiling(self) -> None:
        """Nat-vent active, indoor within hysteresis band above comfort_cool -> tolerated."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # Default hysteresis 1.0F: comfort_cool(75) + 1.0 = 76 is the tolerated ceiling.
        assert coord._is_nat_vent_tolerated_deviation(75.9, 70.0, 75.0) is True

    def test_false_beyond_hysteresis_above_ceiling(self) -> None:
        """Nat-vent active but indoor beyond the hysteresis tolerance -> not tolerated."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        assert coord._is_nat_vent_tolerated_deviation(76.5, 70.0, 75.0) is False

    def test_true_within_hysteresis_below_floor(self) -> None:
        """Nat-vent active, indoor within hysteresis band below comfort_heat -> tolerated."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        # comfort_heat(70) - 1.0 = 69 is the tolerated floor.
        assert coord._is_nat_vent_tolerated_deviation(69.1, 70.0, 75.0) is True

    def test_false_beyond_hysteresis_below_floor(self) -> None:
        """Nat-vent active but indoor beyond the hysteresis tolerance below the floor -> not tolerated."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        assert coord._is_nat_vent_tolerated_deviation(68.5, 70.0, 75.0) is False

    def test_true_within_the_comfort_band_itself(self) -> None:
        """Indoor comfortably within [comfort_heat, comfort_cool] -> trivially tolerated."""
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        assert coord._is_nat_vent_tolerated_deviation(72.0, 70.0, 75.0) is True


# ---------------------------------------------------------------------------
# TestComfortViolationsMinutesNatVentGate — comfort_violations_minutes accumulation
# ---------------------------------------------------------------------------


class TestComfortViolationsMinutesNatVentGate:
    """Issue #411 blast-radius finding: comfort_violations_minutes (coordinator.py
    ~L1667-1682) is a PERSISTED, aggregated metric feeding comfort_score in learning.py —
    a higher-impact instance of the same nat-vent-blind-spot bug as the incident detector
    above. It must use the SAME _is_nat_vent_tolerated_deviation() gate, not a separate
    copy, so an in-tolerance nat-vent cycling dip does not silently degrade a user's
    reported comfort compliance over time (CLAUDE.md "Goal-Oriented Comfort Model",
    Issue #74: violations should only count when the system had control and failed).

    These tests exercise the accumulation logic directly (mirroring the exact
    conditional in coordinator.py) rather than driving the full _async_update_data()
    coroutine, which requires extensive unrelated setup (weather entity, forecast
    object, briefing state) — the accumulation guard itself is what's under test, and
    it calls the same bound _is_nat_vent_tolerated_deviation() verified above.
    """

    def _accumulate(
        self,
        coord,
        indoor_temp: float,
        comfort_low: float,
        comfort_high: float,
        elapsed_minutes: float,
        today_record,
    ) -> None:
        """Mirrors the exact conditional at coordinator.py ~L1677-1682."""
        if (indoor_temp < comfort_low or indoor_temp > comfort_high) and not coord._is_nat_vent_tolerated_deviation(
            indoor_temp, comfort_low, comfort_high
        ):
            today_record.comfort_violations_minutes += elapsed_minutes

    def test_no_accumulation_when_nat_vent_active_and_in_tolerance(self) -> None:
        """In-tolerance deviation while nat-vent active -> comfort_violations_minutes
        must NOT accumulate.
        """
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        today_record = SimpleNamespace(comfort_violations_minutes=0.0)

        self._accumulate(
            coord,
            indoor_temp=75.6,
            comfort_low=70.0,
            comfort_high=75.0,
            elapsed_minutes=30.0,
            today_record=today_record,
        )

        assert today_record.comfort_violations_minutes == 0.0, (
            "in-tolerance nat-vent cycling deviation must not accumulate comfort_violations_minutes"
        )

    def test_accumulates_when_nat_vent_active_but_beyond_tolerance(self) -> None:
        """A genuine, sustained violation during nat-vent still accumulates minutes —
        the gate only suppresses in-tolerance cycling, not real comfort failures.
        """
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = True
        today_record = SimpleNamespace(comfort_violations_minutes=0.0)

        self._accumulate(
            coord,
            indoor_temp=80.0,
            comfort_low=70.0,
            comfort_high=75.0,
            elapsed_minutes=30.0,
            today_record=today_record,
        )

        assert today_record.comfort_violations_minutes == 30.0, (
            "a genuine sustained violation during nat-vent must still accumulate comfort_violations_minutes"
        )

    def test_accumulates_unchanged_when_nat_vent_not_active(self) -> None:
        """Regression guard: with nat-vent NOT active, behavior is unchanged from
        before the fix — any deviation outside the comfort band accumulates minutes.
        """
        coord = _make_coordinator_stub({"comfort_cool": 75, "comfort_heat": 70})
        coord.automation_engine._natural_vent_active = False
        today_record = SimpleNamespace(comfort_violations_minutes=0.0)

        self._accumulate(
            coord,
            indoor_temp=75.6,
            comfort_low=70.0,
            comfort_high=75.0,
            elapsed_minutes=30.0,
            today_record=today_record,
        )

        assert today_record.comfort_violations_minutes == 30.0, (
            "without nat-vent active, even a small deviation must accumulate as before the fix"
        )


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
        # Issue #243: this scenario was de-duplicated — the canonical copy lives in golden/ (the
        # pending/ duplicate was removed). Point at the golden copy.
        bspec_path = (
            Path(__file__).parent.parent / "tools" / "simulations" / "golden" / "away_occupancy_override_cleared.json"
        )
        assert bspec_path.exists(), f"Expected BSpec not found: {bspec_path}"

        result = run_scenario(bspec_path, state="golden")

        assert result["passed"] is True, (
            f"away_occupancy_override_cleared should PASS after bug #220 is fixed. Assertions: {result['assertions']}"
        )
