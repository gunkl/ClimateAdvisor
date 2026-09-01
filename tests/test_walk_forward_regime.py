"""Tests for Issue #802's forward regime walk: _compute_day_hvac_modes() (the extracted
per-day classifier) and _walk_forward_regime() (the coupled nat-vent gate/exit +
ceiling-guard escalation forward walk that replaced the old standalone temperature-
inequality heuristic in _compute_predicted_activity()).

All temperature values here are raw (internal Fahrenheit) — matching how
decide_nat_vent_gate()/decide_nat_vent_exit()/decide_ode_ceiling_guard() are used
elsewhere in production (see _compute_next_automation_action()'s own nat-vent-start
prediction, Issue #528), and how this plan's own investigation found the pre-existing
_compute_effective_target_forward()/_compute_predicted_activity() call site was
incorrectly mixing display-unit-converted band values with raw config thresholds.
"""

from __future__ import annotations

import importlib
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()


def _mod():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


_BASE_CONFIG = {
    "comfort_heat": 68.0,
    "comfort_cool": 76.0,
    "sleep_heat": 64.0,
    "sleep_cool": 72.0,
    "wake_time": "06:00:00",
    "sleep_time": "22:00:00",
    "fan_mode": "hvac_fan",
    "natural_vent_delta": 3.0,
    "nat_vent_hysteresis_f": 1.0,
    "aggressive_savings": False,
}


def _ts(hour: int, minute: int = 0, day_offset: int = 0) -> str:
    base = datetime(2026, 7, 13, hour, minute, tzinfo=None) + timedelta(days=day_offset)
    return base.isoformat()


def _band(entries: list[tuple[str, float, float]]) -> list[dict]:
    return [{"ts": ts, "lower": lower, "upper": upper} for ts, lower, upper in entries]


def _series(entries: list[tuple[str, float]]) -> list[dict]:
    return [{"ts": ts, "temp": temp} for ts, temp in entries]


# ===========================================================================
# _compute_day_hvac_modes() — bit-identical extraction proof (Assumption Audit #1)
# ===========================================================================


class TestComputeDayHvacModesExtraction:
    def _hourly_forecast(self) -> list[dict]:
        # Spans a mild day (max 70F -> "off") so today's classification-override path
        # is exercised too.
        return [
            {"datetime": "2026-07-13T06:00:00+00:00", "temperature": 60.0},
            {"datetime": "2026-07-13T14:00:00+00:00", "temperature": 70.0},
            {"datetime": "2026-07-14T06:00:00+00:00", "temperature": 92.0},
        ]

    def test_bit_identical_to_build_predicted_indoor_future_day_modes(self) -> None:
        """The extracted _compute_day_hvac_modes() must produce exactly the same
        per-day classification _build_predicted_indoor_future() used to compute inline
        before Issue #802 — verified by checking the same hot/off day-mode split its own
        threshold logic implies, and that today's entry is overridden by the live
        classification exactly as before."""
        from unittest.mock import MagicMock

        mod = _mod()
        classification = MagicMock()
        classification.hvac_mode = "off"
        now = datetime(2026, 7, 13, 12, 0, tzinfo=None)

        # dt_util.as_local() is a MagicMock in this stub environment and breaks date()
        # comparisons — same fix CLAUDE.md documents for _build_predicted_indoor_future().
        with patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x):
            day_modes = mod._compute_day_hvac_modes(self._hourly_forecast(), now, classification)

        assert day_modes[date(2026, 7, 13)] == "off"  # overridden by classification, not the 70F max
        assert day_modes[date(2026, 7, 14)] == "cool"  # 92F max >= THRESHOLD_HOT

    def test_no_classification_uses_pure_threshold_classification(self) -> None:
        mod = _mod()
        now = datetime(2026, 7, 13, 12, 0, tzinfo=None)
        with patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x):
            day_modes = mod._compute_day_hvac_modes(self._hourly_forecast(), now, None)
        # No override -> today's own max (70F, in the "off" band per THRESHOLD_WARM/MILD)
        assert day_modes[date(2026, 7, 13)] == "off"
        assert day_modes[date(2026, 7, 14)] == "cool"

    def test_no_valid_entries_returns_empty_dict(self) -> None:
        mod = _mod()
        now = datetime(2026, 7, 13, 12, 0, tzinfo=None)
        assert mod._compute_day_hvac_modes([], now, None) == {}
        assert mod._compute_day_hvac_modes(None, now, None) == {}

    def test_build_predicted_indoor_future_still_returns_empty_on_no_valid_entries(self) -> None:
        """_build_predicted_indoor_future() must still short-circuit to [] the same way
        it did before the extraction, when _compute_day_hvac_modes() returns {}."""
        mod = _mod()
        now = datetime(2026, 7, 13, 12, 0, tzinfo=None)
        result = mod._build_predicted_indoor_future(
            [{"datetime": "not-a-timestamp", "temperature": 70.0}],
            _BASE_CONFIG,
            now,
        )
        assert result == []


# ===========================================================================
# _walk_forward_regime()
# ===========================================================================


class TestWalkForwardRegimeExitReasons:
    """Each exit reason must flip session_active=False starting at the correct hour,
    via the REAL decide_nat_vent_exit() — not a reimplemented approximation."""

    def test_comfort_floor_exit(self) -> None:
        mod = _mod()
        ts1, ts2 = _ts(12), _ts(13)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0), (ts2, 68.0)])  # ts2 hits the daytime floor
        forecast_outdoor = _series([(ts1, 65.0), (ts2, 65.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            True,  # already active
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is False

    def test_away_ceiling_exit(self) -> None:
        mod = _mod()
        ts1, ts2 = _ts(12), _ts(13)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0), (ts2, 76.0)])  # ts2 hits comfort_cool
        forecast_outdoor = _series([(ts1, 60.0), (ts2, 60.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "away",
            None,
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is False

    def test_proactive_floor_exit_with_confident_thermal_model(self) -> None:
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0)])
        forecast_outdoor = _series([(ts1, 60.0)])
        # k_passive=-2.0, indoor-outdoor=10 -> passive_rate=-20F/hr -> time_to_floor
        # = (70-68)/20 = 0.1hr < 1.0hr -> PROACTIVE_FLOOR fires immediately.
        thermal_model = {"k_passive": -2.0, "confidence_k_passive": "high"}

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            thermal_model,
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is False

    def test_outdoor_rise_exit(self) -> None:
        mod = _mod()
        ts1, ts2 = _ts(12), _ts(13)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0), (ts2, 70.0)])
        forecast_outdoor = _series([(ts1, 60.0), (ts2, 71.0)])  # ts2: outdoor >= indoor

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is False

    def test_ceiling_threshold_exit(self) -> None:
        mod = _mod()
        ts1, ts2 = _ts(12), _ts(13)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 72.0), (ts2, 82.0)])
        # comfort_cool(76) + nat_vent_delta(3) = 79 threshold; ts2 outdoor=80 > 79,
        # and outdoor(80) < indoor(82) so OUTDOOR_RISE does not pre-empt it.
        forecast_outdoor = _series([(ts1, 65.0), (ts2, 80.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is False

    def test_manual_override_conflict_exit(self) -> None:
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 72.0)])
        forecast_outdoor = _series([(ts1, 60.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            True,  # manual_override_active
            "heat",
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is False


class TestWalkForwardRegimeThermalConfidenceNone:
    def test_proactive_floor_never_fires_without_confidence_no_crash(self) -> None:
        """Assumption Audit #3: with thermal_confidence == 'none' (or no thermal_model at
        all), PROACTIVE_FLOOR must gracefully never fire — session stays active through
        conditions that would otherwise trigger it, and nothing raises."""
        mod = _mod()
        ts1, ts2, ts3 = _ts(12), _ts(13), _ts(14)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0), (ts3, 68.0, 76.0)])
        # Same shape as the PROACTIVE_FLOOR test above (would fire if confident), but with
        # no thermal_model — comfort floor / away / outdoor-rise / ceiling all also safe.
        predicted_indoor = _series([(ts1, 70.0), (ts2, 70.0), (ts3, 70.0)])
        forecast_outdoor = _series([(ts1, 60.0), (ts2, 60.0), (ts3, 60.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,  # no thermal model
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is True
        assert result[ts3]["nat_vent_active"] is True


class TestWalkForwardRegimeReentry:
    def test_gate_reactivates_session_after_outdoor_rise_exit_and_recovery(self) -> None:
        """Exit via OUTDOOR_RISE, then outdoor cools back down enough to satisfy
        decide_nat_vent_gate() at a later hour -> session re-activates. This is real
        predicted behavior (the same hysteresis-aware thresholds the live engine uses),
        not flicker."""
        mod = _mod()
        ts1, ts2, ts3 = _ts(12), _ts(13), _ts(14)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0), (ts3, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0), (ts2, 70.0), (ts3, 74.0)])
        forecast_outdoor = _series(
            [
                (ts1, 60.0),  # active, safe
                (ts2, 71.0),  # outdoor >= indoor -> OUTDOOR_RISE exit
                (ts3, 65.0),  # outdoor(65) < indoor(74)-hyst(1)=73, indoor>68, outdoor<79 -> gate True
            ]
        )

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            True,
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts2]["nat_vent_active"] is False
        assert result[ts3]["nat_vent_active"] is True


class TestWalkForwardRegimeDayModeBoundary:
    def test_heat_cool_day_never_walks_nat_vent(self) -> None:
        """Assumption Audit #4: a day classified heat/cool is never fed to
        decide_nat_vent_gate()/decide_nat_vent_exit() at all -- confirmed by conditions
        that WOULD activate nat-vent (favorable outdoor/indoor gap) producing
        nat_vent_active=False purely because the day's mode isn't 'off'."""
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0)])
        forecast_outdoor = _series([(ts1, 60.0)])  # would satisfy the gate on an off day

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            False,
        )
        assert result[ts1] == {"nat_vent_active": False, "hvac_mode": "cool"}

    def test_off_day_into_forecast_hot_day_switches_regime_at_boundary(self) -> None:
        """Multi-day range: an off/nat-vent-eligible day followed by a day the forecast
        classifies 'cool' switches the regime exactly at the day boundary."""
        mod = _mod()
        ts_day1 = _ts(20)  # 20:00 on day 1
        ts_day2 = _ts(6, day_offset=1)  # 06:00 on day 2
        day_modes = {date(2026, 7, 13): "off", date(2026, 7, 14): "cool"}
        band = _band([(ts_day1, 68.0, 76.0), (ts_day2, 68.0, 76.0)])
        predicted_indoor = _series([(ts_day1, 70.0), (ts_day2, 74.0)])
        forecast_outdoor = _series([(ts_day1, 60.0), (ts_day2, 60.0)])

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            None,
            False,
            None,
            None,
            False,
        )
        assert result[ts_day1]["hvac_mode"] == "off"
        assert result[ts_day2]["hvac_mode"] == "cool"
        assert result[ts_day2]["nat_vent_active"] is False


class TestWalkForwardRegimeCeilingGuardEscalation:
    """Third finding: an off-classified day can escalate to active cooling mid-day via
    decide_ode_ceiling_guard() — coupled with nat-vent's own session_active state through
    the guard's DORMANT outcome."""

    def test_escalation_fires_within_lead_time_and_not_before(self) -> None:
        mod = _mod()
        ts1 = _ts(10)  # 3h before the breach
        ts2 = _ts(12)  # 1h before the breach
        ts_breach = _ts(13)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0), (ts2, 74.0), (ts_breach, 77.0)])
        # outdoor stays warmer than indoor-hysteresis so decide_nat_vent_gate() never
        # activates a session -- isolates this test to the ceiling-guard path alone.
        forecast_outdoor = _series([(ts1, 75.0), (ts2, 75.0)])
        thermal_model = {"k_passive": -1.0, "confidence_k_passive": "high"}

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            thermal_model,
            False,
            None,
            76.0,  # ceiling_threshold
            False,
        )
        assert result[ts1] == {"nat_vent_active": False, "hvac_mode": "off"}, (
            "hours_to_breach=3.0h exceeds the 2.0h fallback lead time -> STANDING_BY, no escalation yet"
        )
        assert result[ts2] == {"nat_vent_active": False, "hvac_mode": "cool"}, (
            "hours_to_breach=1.0h is within the 2.0h fallback lead time -> ESCALATE"
        )

    def test_dormancy_suppresses_escalation_when_nat_vent_active(self) -> None:
        """Same breach shape as above, but nat-vent is genuinely active and dormant
        (outdoor <= indoor <= ceiling_threshold) at the evaluated hour -> no escalation,
        even though a breach exists later in the predicted curve."""
        mod = _mod()
        ts1 = _ts(12)
        ts_breach = _ts(14)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0), (ts_breach, 77.0)])
        forecast_outdoor = _series([(ts1, 70.0)])  # outdoor <= indoor
        thermal_model = {"k_passive": -1.0, "confidence_k_passive": "high"}

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            thermal_model,
            False,
            None,
            76.0,
            True,  # already active -> exit chain runs first (finds NONE) -> stays active
        )
        assert result[ts1] == {"nat_vent_active": True, "hvac_mode": "off"}

    def test_coupling_session_exit_within_hour_correctly_unlocks_escalation(self) -> None:
        """Assumption Audit #7's direct proof: nat-vent's session_active is resolved
        BEFORE the ceiling-guard check within the SAME hour, not carried over stale from
        the prior hour. Hour 1: session active + dormant (breach suppressed). Hour 2: the
        SAME session exits via COMFORT_FLOOR, and the ceiling guard -- fed that hour's
        freshly-updated (now False) session_active -- correctly stops treating it as
        dormant and escalates, all within hour 2 itself."""
        mod = _mod()
        ts1, ts2, ts3 = _ts(12), _ts(13), _ts(14)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0), (ts2, 68.0), (ts3, 77.0)])
        forecast_outdoor = _series([(ts1, 70.0), (ts2, 65.0)])
        thermal_model = {"k_passive": -1.0, "confidence_k_passive": "high"}

        result = mod._walk_forward_regime(
            day_modes,
            predicted_indoor,
            forecast_outdoor,
            band,
            _BASE_CONFIG,
            "home",
            thermal_model,
            False,
            None,
            76.0,
            True,  # active at ts1
        )
        assert result[ts1] == {"nat_vent_active": True, "hvac_mode": "off"}, "ts1: dormant, suppresses escalation"
        assert result[ts2] == {"nat_vent_active": False, "hvac_mode": "cool"}, (
            "ts2: COMFORT_FLOOR exits the session THIS hour, and that fresh False state "
            "(not ts1's stale True) is what the ceiling guard sees -- dormancy no longer "
            "holds, so it escalates within the same hour, proving the sequencing is real"
        )


class TestWalkForwardRegimeGenuineReuseNotAParallelCopy:
    """Non-Negotiable Goal 1: the walk must call the REAL production functions, not a
    reimplemented approximation of them. Proven by monkeypatching each real function and
    confirming the walk's output changes accordingly."""

    def test_patching_decide_nat_vent_gate_changes_walk_output(self) -> None:
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0)])
        # Deliberately fails the real gate (outdoor too warm relative to indoor).
        forecast_outdoor = _series([(ts1, 76.0)])

        baseline = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, False
        )
        assert baseline[ts1]["nat_vent_active"] is False, "sanity check: the real gate genuinely rejects this input"

        with patch("custom_components.climate_advisor.coordinator.decide_nat_vent_gate", return_value=True):
            patched = mod._walk_forward_regime(
                day_modes,
                predicted_indoor,
                forecast_outdoor,
                band,
                _BASE_CONFIG,
                "home",
                None,
                False,
                None,
                None,
                False,
            )
        assert patched[ts1]["nat_vent_active"] is True, (
            "patching decide_nat_vent_gate() must change the walk's output -- if it didn't, "
            "the walk is calling a reimplemented copy instead of the real imported function"
        )

    def test_patching_decide_nat_vent_exit_changes_walk_output(self) -> None:
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 74.0)])
        forecast_outdoor = _series([(ts1, 60.0)])  # safe, real exit chain returns NONE

        baseline = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, True
        )
        assert baseline[ts1]["nat_vent_active"] is True, "sanity check: nothing exits the real chain here"

        from custom_components.climate_advisor.nat_vent_exit import NatVentExitDecision, NatVentExitReason

        with patch(
            "custom_components.climate_advisor.coordinator.decide_nat_vent_exit",
            return_value=NatVentExitDecision(reason=NatVentExitReason.OUTDOOR_RISE),
        ):
            patched = mod._walk_forward_regime(
                day_modes,
                predicted_indoor,
                forecast_outdoor,
                band,
                _BASE_CONFIG,
                "home",
                None,
                False,
                None,
                None,
                True,
            )
        assert patched[ts1]["nat_vent_active"] is False, (
            "patching decide_nat_vent_exit() must change the walk's output -- if it didn't, "
            "the walk is calling a reimplemented copy instead of the real imported function"
        )

    def test_patching_decide_ode_ceiling_guard_changes_walk_output(self) -> None:
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0)])  # well under any breach threshold
        forecast_outdoor = _series([(ts1, 60.0)])

        baseline = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, 76.0, False
        )
        assert baseline[ts1]["hvac_mode"] == "off", "sanity check: no breach, the real guard does not escalate"

        from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardDecision, OdeCeilingGuardOutcome

        with patch(
            "custom_components.climate_advisor.coordinator.decide_ode_ceiling_guard",
            return_value=OdeCeilingGuardDecision(outcome=OdeCeilingGuardOutcome.ESCALATE),
        ):
            patched = mod._walk_forward_regime(
                day_modes,
                predicted_indoor,
                forecast_outdoor,
                band,
                _BASE_CONFIG,
                "home",
                None,
                False,
                None,
                76.0,
                False,
            )
        assert patched[ts1]["hvac_mode"] == "cool", (
            "patching decide_ode_ceiling_guard() must change the walk's output -- if it "
            "didn't, the walk is calling a reimplemented copy instead of the real imported "
            "function"
        )


class TestLiveInstanceReproduction:
    """Non-Negotiable Goal 5: reproduce the exact live-instance scenario that prompted
    this investigation end-to-end through get_chart_data() and confirm the Target line
    is now continuous through the nat-vent-eligible stretch, not just 'fewer gaps.'

    Real config from the live instance: comfort_heat=68, comfort_cool=74, sleep_heat=64,
    sleep_cool=72, wake_time=06:30, sleep_time=20:30, hvac_mode=off today, windows
    currently open (self._any_sensor_open() would report True on the real instance;
    stubbed here via a mocked automation_engine)."""

    def _make_coord(self):
        import types
        from datetime import UTC as _UTC
        from pathlib import Path

        from custom_components.climate_advisor.chart_log import ChartStateLog

        mod = _mod()
        ClimateAdvisorCoordinator = mod.ClimateAdvisorCoordinator
        coord = object.__new__(ClimateAdvisorCoordinator)

        coord.config = {
            "temp_unit": "fahrenheit",
            "comfort_heat": 68.0,
            "comfort_cool": 74.0,
            "sleep_heat": 64.0,
            "sleep_cool": 72.0,
            "wake_time": "06:30:00",
            "sleep_time": "20:30:00",
            "setback_heat": 60.0,
            "setback_cool": 80.0,
            "fan_mode": "whole_house_fan",
            "natural_vent_delta": 3.0,
            "nat_vent_hysteresis_f": 1.0,
            "aggressive_savings": False,
        }

        mock_learning = MagicMock()
        mock_learning.get_thermal_model = MagicMock(
            return_value={
                "confidence": "none",
                "confidence_k_passive": "none",
                "observation_count_heat": 0,
                "observation_count_cool": 0,
                "observation_count_passive": 0,
                "observation_count_fan_only": 0,
                "observation_count_vent": 0,
                "observation_count_solar": 0,
                "observation_count_swing_heat": 0,
                "observation_count_swing_cool": 0,
                "heating_rate_f_per_hour": None,
                "cooling_rate_f_per_hour": None,
                "k_passive": None,
                "k_vent": None,
                "k_vent_window": None,
                "k_solar": None,
                "learning_health": {},
                "swing_heat_f_display": 1.5,
                "swing_cool_f_display": 1.5,
                "swing_heat_f": None,
                "swing_cool_f": None,
                "confidence_swing_heat": "none",
                "confidence_swing_cool": "none",
                "solar_phase_offset_h": None,
                "avg_r_squared_passive": None,
                "last_observation_date": None,
            }
        )
        coord.learning = mock_learning
        coord.hass = MagicMock()

        # A realistic diurnal outdoor curve (cosine, trough ~60F pre-dawn, peak ~76F
        # mid-afternoon) — mild enough to stay "off"-classified all day, matching the live
        # scenario this reproduces. With predicted indoor pinned near the comfort midpoint
        # (71F, no confident thermal model), nat-vent is gate-eligible whenever outdoor
        # drops below ~70F (roughly the cooler two-thirds of the cycle) and legitimately
        # inactive near the afternoon peak — real physics, not a flicker artifact.
        import math

        _now_dt = datetime(2026, 8, 31, 15, 33, 0, tzinfo=_UTC)

        def _outdoor_at(h: int) -> float:
            hour_of_day = (15 + h) % 24
            return 68.0 + 8.0 * math.cos(2 * math.pi * (hour_of_day - 15) / 24)

        coord._hourly_forecast_temps = [
            {"datetime": (_now_dt + timedelta(hours=h)).isoformat(), "temperature": round(_outdoor_at(h), 1)}
            for h in range(0, 30)
        ]

        classification = MagicMock()
        classification.hvac_mode = "off"
        classification.today_high = 78.0
        classification.today_low = 62.0
        classification.window_open_time = None
        classification.window_close_time = None
        coord._current_classification = classification
        coord._occupancy_mode = "home"

        automation_engine = MagicMock()
        automation_engine._natural_vent_active = True
        automation_engine._manual_override_active = False
        automation_engine._manual_override_mode = None
        automation_engine._ceiling_threshold = MagicMock(return_value=74.0)
        coord.automation_engine = automation_engine

        chart_log = ChartStateLog(Path(str(id(self))), max_days=365)
        coord._chart_log = chart_log

        coord.get_chart_data = types.MethodType(ClimateAdvisorCoordinator.get_chart_data, coord)
        coord._build_learning_health = types.MethodType(ClimateAdvisorCoordinator._build_learning_health, coord)
        coord._get_indoor_temp = MagicMock(return_value=71.0)
        coord._any_sensor_open = MagicMock(return_value=True)

        return coord, _now_dt

    def test_target_line_is_continuous_through_nat_vent_eligible_stretch(self, tmp_path) -> None:
        coord, now_dt = self._make_coord()
        # Reuse the real ChartStateLog machinery but avoid touching disk for this test.
        coord._chart_log._entries = []

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x),
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now_dt),
        ):
            result = coord.get_chart_data("24h")

        predicted_activity = result["predicted_activity"]
        effective_target_forecast = result["effective_target_forecast"]
        assert len(predicted_activity) > 10, "sanity check: the forecast actually produced future hours"

        # Real diurnal physics: nat-vent is legitimately active during the cooler
        # two-thirds of the cycle and legitimately inactive near the afternoon outdoor
        # peak — the fix is not "always active," it's "session persists through a
        # sustained active stretch instead of flickering hour-to-hour like the old
        # heuristic did." Proven via longest-contiguous-run, not raw coverage percentage.
        fan_active_flags = [bool(e["fan_active"]) for e in predicted_activity]
        longest_run = 0
        current_run = 0
        for flag in fan_active_flags:
            current_run = current_run + 1 if flag else 0
            longest_run = max(longest_run, current_run)
        assert longest_run >= 6, (
            f"longest contiguous fan_active=True run is only {longest_run} hours -- the old "
            "heuristic's flicker bug (session re-derived from scratch every hour, no "
            f"memory) produces short/no runs; a real session should persist for a sustained "
            f"stretch. fan_active sequence: {fan_active_flags}"
        )

        # Every hour the walk says nat-vent is active, the Target line must show a real
        # value (tier 2 firing) -- no gaps WITHIN an active stretch.
        target_by_ts = {e["ts"]: e["target"] for e in effective_target_forecast}
        for entry in predicted_activity:
            if entry["fan_active"]:
                assert target_by_ts.get(entry["ts"]) is not None, (
                    f"predicted_activity says fan_active=True at {entry['ts']} but the "
                    "Target line has no value there -- tier 2 should always resolve when "
                    "the walk says the session is active"
                )

        # The specific live-instance symptom: David observed a dead-flat line at exactly
        # (comfort_heat+comfort_cool)/2 = 71.0 for the ENTIRE forecast, including through
        # the night. Confirm the line now actually varies (steps down for the sleep
        # window), not still pinned at one value throughout.
        distinct_targets = {round(e["target"], 1) for e in effective_target_forecast if e["target"] is not None}
        assert len(distinct_targets) > 1, (
            "Target line is still a single flat value across the whole forecast -- the "
            "original reported symptom (a misleading flat line) is not actually fixed"
        )
