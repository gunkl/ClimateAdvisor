"""Issue #1067: the chart's predicted-indoor curve follows nearest-edge arming.

The real thermostat defends whichever comfort edge the house is outside of (floor -> heat,
ceiling -> cool). ``_build_predicted_indoor_future`` used to pick heat/cool only from the
day-level mode, so on a cool mild morning (day mode "off") that started under the floor the
curve drifted further below the floor while the thermostat was actually heating.

These tests call the real ``_build_predicted_indoor_future`` and the real pure leaf
``decide_comfort_family``; nothing is mirrored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor import coordinator as coord_mod
from custom_components.climate_advisor.comfort_family_decision import (
    ComfortFamilyDecision,
    ComfortFamilyOutcome,
)
from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future
from custom_components.climate_advisor.ode_ceiling_guard import (
    OdeCeilingGuardInputs,
    OdeCeilingGuardOutcome,
    decide_ode_ceiling_guard,
)

_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}
_NOW = datetime(2026, 4, 10, 5, 0, 0, tzinfo=UTC)
_MODEL = {"confidence": "low", "k_passive": -0.05, "k_active_heat": 3.0, "k_active_cool": -2.5}


def _dt_util_mock() -> MagicMock:
    mock = MagicMock()
    mock.now.return_value = _NOW
    mock.as_local.side_effect = lambda dt: dt
    return mock


def _entries(outdoor: list[float]) -> list[dict]:
    return [{"datetime": (_NOW + timedelta(hours=i + 1)).isoformat(), "temperature": t} for i, t in enumerate(outdoor)]


def _flat_band(entries: list[dict], lower: float = 70.0, upper: float = 75.0) -> list[dict]:
    return [{"ts": e["datetime"], "lower": lower, "upper": upper} for e in entries]


def _predict(entries, *, indoor, band=None, tou=None, model=_MODEL):
    with patch.object(coord_mod, "dt_util", _dt_util_mock()):
        return _build_predicted_indoor_future(
            entries,
            _CONFIG,
            _NOW,
            current_indoor_temp=indoor,
            thermal_model=model,
            band_schedule=band if band is not None else _flat_band(entries),
            tou_precondition_window=tou,
        )


def _arming_disabled():
    """Patch the leaf to NOT_APPLICABLE: reproduces the pre-#1067 day-mode-only behaviour."""
    na = ComfortFamilyDecision(outcome=ComfortFamilyOutcome.NOT_APPLICABLE, reason="test: arming disabled")
    return patch.object(coord_mod, "decide_comfort_family", return_value=na)


def _hourly(result: list[dict], entries: list[dict]) -> list[float]:
    wanted = {e["datetime"] for e in entries}
    return [p["temp"] for p in result if p["ts"] in wanted]


class TestCoolMorningHeadsTowardFloor:
    def test_mild_day_below_floor_heats_instead_of_drifting_down(self):
        """Day high 62 -> mode "off". Indoor 66 < floor 70: the thermostat heats, the curve rises."""
        entries = _entries([48.0, 49.0, 51.0, 54.0, 57.0, 60.0, 62.0, 62.0])
        armed = _hourly(_predict(entries, indoor=66.0), entries)
        with _arming_disabled():
            legacy = _hourly(_predict(entries, indoor=66.0), entries)
        assert legacy[0] < 66.0 and legacy[-1] < 66.0, "premise: day-mode-only curve drifts below the start"
        assert armed[0] > 66.0
        assert armed[-1] > legacy[-1]
        assert max(armed) <= 70.0 + 1e-6, "heating is clamped at the floor setpoint"

    def test_leaf_reads_ode_state_and_skips_non_physics(self):
        """The leaf reads the ODE state on physics hours and is not called when no hour uses physics."""
        entries = _entries([48.0, 49.0, 51.0, 54.0])
        seen: list[float] = []
        real = coord_mod.decide_comfort_family

        def spy(inp):
            seen.append(inp.indoor)
            return real(inp)

        with patch.object(coord_mod, "decide_comfort_family", side_effect=spy):
            _predict(entries, indoor=66.0, model=None)
        assert seen == [], "no leaf call when no hour uses physics"
        with patch.object(coord_mod, "decide_comfort_family", side_effect=spy):
            _predict(entries, indoor=66.0)
        assert seen[0] == 66.0
        assert len(seen) == len(entries)
        assert seen[1] != 66.0, "second hour reads the advanced ODE state"

    def test_fallback_ramp_path_unchanged(self):
        """No thermal model -> setpoint-schedule fallback is deliberately untouched in this slice."""
        entries = _entries([48.0, 50.0, 52.0])
        armed = _predict(entries, indoor=66.0, model=None)
        with _arming_disabled():
            legacy = _predict(entries, indoor=66.0, model=None)
        assert armed == legacy

    def test_heat_day_above_ceiling_keeps_day_mode_behaviour(self):
        """Cooling direction is deliberately UNCHANGED (ODE ceiling guard coupling): a heat day with
        indoor above the ceiling still follows the day mode (heat), bit-identical to the legacy curve."""
        entries = _entries([50.0, 52.0, 54.0, 55.0])
        armed = _predict(entries, indoor=78.0)
        with _arming_disabled():
            legacy = _predict(entries, indoor=78.0)
        assert armed == legacy

    def test_off_day_above_ceiling_is_not_clamped_at_ceiling(self):
        """Guard coupling: on an 'off' day an above-ceiling start must NOT be pulled to the ceiling,
        or decide_ode_ceiling_guard would see NO_BREACH_PREDICTED where it used to escalate."""
        entries = _entries([62.0, 62.0, 62.0, 62.0])
        armed = _hourly(_predict(entries, indoor=78.0), entries)
        with _arming_disabled():
            legacy = _hourly(_predict(entries, indoor=78.0), entries)
        assert armed == legacy
        assert armed[0] > 75.0, "still above the ceiling: not clamped"


class TestUnchangedWhenNotBreach:
    def test_in_band_warm_day_identical_to_day_mode_only(self):
        entries = _entries([70.0, 74.0, 78.0, 80.0, 80.0, 78.0])
        armed = _predict(entries, indoor=72.0)
        with _arming_disabled():
            legacy = _predict(entries, indoor=72.0)
        assert armed == legacy

    def test_hot_day_above_ceiling_identical(self):
        """Hot day already cools; nearest-edge arming picks the same family."""
        entries = _entries([86.0, 90.0, 92.0, 92.0])
        armed = _predict(entries, indoor=78.0)
        with _arming_disabled():
            legacy = _predict(entries, indoor=78.0)
        assert armed == legacy

    def test_no_indoor_seed_identical(self):
        entries = _entries([50.0, 52.0])
        armed = _predict(entries, indoor=None)
        with _arming_disabled():
            legacy = _predict(entries, indoor=None)
        assert armed == legacy


class TestNotApplicableRegimes:
    def test_tou_precondition_window_defers(self):
        """Inside an active TOU precondition window the selector defers, so no override applies."""
        entries = _entries([48.0, 49.0, 51.0])
        tou = (_NOW, _NOW + timedelta(hours=10), 68.0, "cool")
        deferred = _predict(entries, indoor=66.0, tou=tou)
        with _arming_disabled():
            legacy = _predict(entries, indoor=66.0, tou=tou)
        assert deferred == legacy


class TestCeilingGuardOutcomeShift:
    """Honest pin: floor-heating raises the curve's LATER hours, so the real ODE ceiling guard
    sees a breach it previously did not. Deliberate (the real thermostat does heat)."""

    _OUTDOOR = [55, 56, 58, 61, 64, 67, 70, 72, 76, 77, 78, 78, 78, 77, 75, 73]
    _SOLAR_MODEL = {
        "confidence": "low",
        "k_passive": -0.12,
        "k_active_heat": 3.0,
        "k_active_cool": -2.5,
        "k_solar": 0.7,
        "confidence_k_passive": "low",
    }

    def _guard(self, curve, indoor):
        return decide_ode_ceiling_guard(
            OdeCeilingGuardInputs(
                predicted_indoor=curve,
                hvac_mode="off",
                k_passive=-0.12,
                confidence_k_passive="low",
                k_active_cool=-2.5,
                comfort_cool=75.0,
                outdoor=55.0,
                indoor=indoor,
                natural_vent_active=False,
                ceiling_threshold=75.0,
                now=_NOW,
            )
        )

    def test_mild_off_day_below_floor_guard_outcome_changes(self):
        entries = _entries([float(t) for t in self._OUTDOOR])
        armed = _predict(entries, indoor=64.0, model=self._SOLAR_MODEL)
        with _arming_disabled():
            legacy = _predict(entries, indoor=64.0, model=self._SOLAR_MODEL)
        assert max(p["temp"] for p in legacy) < 74.5
        assert max(p["temp"] for p in armed) > 75.0, "later hours are higher after floor-heating"
        legacy_guard = self._guard(legacy, 64.0)
        armed_guard = self._guard(armed, 64.0)
        assert legacy_guard.outcome is OdeCeilingGuardOutcome.NO_BREACH_PREDICTED
        assert armed_guard.outcome is not OdeCeilingGuardOutcome.NO_BREACH_PREDICTED
