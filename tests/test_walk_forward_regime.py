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


class TestWalkForwardRegimeReentry:
    def test_gate_reactivates_session_after_outdoor_rise_exit_and_recovery(self) -> None:
        """Exit via OUTDOOR_RISE, then outdoor cools back down enough to satisfy
        decide_nat_vent_gate() at a later hour -> session re-activates. This is real
        predicted behavior (the same hysteresis-aware thresholds the live engine uses),
        not flicker -- but only once a GENUINE ceiling breach has actually occurred
        that day (Issue #948).

        Corrected for Issue #948: the original fixture had outdoor recover to 65F
        immediately after the OUTDOOR_RISE exit (peaking at only 71F, never anywhere
        near comfort_cool=76) and asserted reactivation there -- that was the exact
        spurious pre-peak-dip reopen Issue #948 fixed, not "real predicted behavior".
        This fixture now inserts a genuine ceiling-breach hour (outdoor 90F >
        comfort_cool 76F) between the exit and the recovery, so the test still
        covers "reactivation after an exit", just after a real "got too hot" event
        has actually happened first.
        """
        mod = _mod()
        ts1, ts2, ts_breach, ts_recover = _ts(12), _ts(13), _ts(14), _ts(15)
        day_modes = {date(2026, 7, 13): "off"}
        band = _band([(ts1, 68.0, 76.0), (ts2, 68.0, 76.0), (ts_breach, 68.0, 76.0), (ts_recover, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 70.0), (ts2, 70.0), (ts_breach, 74.0), (ts_recover, 74.0)])
        forecast_outdoor = _series(
            [
                (ts1, 60.0),  # active, safe
                (ts2, 71.0),  # outdoor >= indoor -> OUTDOOR_RISE exit
                (ts_breach, 90.0),  # genuine ceiling breach (90 > comfort_cool 76) -- stays inactive (too hot)
                (ts_recover, 65.0),  # outdoor(65) < indoor(74)-hyst(1)=73, indoor>68, outdoor<79 -> gate True
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
        assert result[ts_breach]["nat_vent_active"] is False
        assert result[ts_recover]["nat_vent_active"] is True


class TestWalkForwardRegimeDayModeBoundary:
    def test_heat_day_never_walks_nat_vent(self) -> None:
        """Issue #878-followup (formerly Assumption Audit #4, which covered heat AND
        cool): a day classified 'heat' (Cold — classifier.py has no window-opportunity
        concept for Cold days) is never fed to decide_nat_vent_gate()/decide_nat_vent_exit()
        at all -- confirmed by conditions that WOULD activate nat-vent (favorable
        outdoor/indoor gap) producing nat_vent_active=False purely because the day's
        mode is 'heat'. 'cool' (Hot) days are covered separately below -- they now DO
        evaluate nat-vent (that was the whole point of this fix)."""
        mod = _mod()
        ts1 = _ts(12)
        day_modes = {date(2026, 7, 13): "heat"}
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
        assert result[ts1] == {"nat_vent_active": False, "hvac_mode": "heat"}

    def test_off_day_into_forecast_hot_day_switches_regime_at_boundary(self) -> None:
        """Multi-day range: an off/nat-vent-eligible day followed by a day the forecast
        classifies 'cool' switches the HVAC regime exactly at the day boundary. Issue
        #878-followup: day2's nat-vent eligibility is now genuinely evaluated too (was
        hardcoded False before this fix) -- outdoor here is cool enough to activate,
        proving the day-boundary switch correctly carries into a 'cool' day's own gate
        evaluation rather than silently disabling it."""
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
        assert result[ts_day2]["nat_vent_active"] is True


class TestWalkForwardRegimeHotDayNatVent:
    """Issue #878-followup (Defect E): 'cool'-mode days (Hot) now evaluate nat-vent
    gate/exit via the same real production functions 'off' days already used --
    previously hard-coded to nat_vent_active=False for the entire day."""

    def test_hot_day_activates_overnight_when_cool_enough(self) -> None:
        mod = _mod()
        ts1 = _ts(23)  # cool enough overnight
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 76.0)])
        forecast_outdoor = _series([(ts1, 70.0)])  # < comfort_cool(76) - hysteresis(1)

        result = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, False
        )
        assert result[ts1]["nat_vent_active"] is True
        assert result[ts1]["hvac_mode"] == "cool"

    def test_hot_day_midday_stays_inactive(self) -> None:
        mod = _mod()
        ts1 = _ts(13)
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts1, 68.0, 76.0)])
        predicted_indoor = _series([(ts1, 76.0)])
        forecast_outdoor = _series([(ts1, 90.0)])  # far above comfort_cool + nat_vent_delta

        result = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, False
        )
        assert result[ts1]["nat_vent_active"] is False
        assert result[ts1]["hvac_mode"] == "cool"

    def test_hot_day_two_separate_windows_both_activate(self) -> None:
        """Regression guard for five-whys #5: a midday exit must NOT persistently
        suppress a later same-day reactivation the way an 'off' day's escalated_to_cool
        would -- a Hot day's morning window and evening reopen are two independent
        opportunities."""
        mod = _mod()
        ts_morning, ts_midday, ts_evening = _ts(7), _ts(13), _ts(20)
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts_morning, 68.0, 76.0), (ts_midday, 68.0, 76.0), (ts_evening, 68.0, 76.0)])
        predicted_indoor = _series([(ts_morning, 74.0), (ts_midday, 74.0), (ts_evening, 74.0)])
        forecast_outdoor = _series(
            [
                (ts_morning, 65.0),  # cool -> activates
                (ts_midday, 85.0),  # hot -> exits (OUTDOOR_RISE and CEILING_THRESHOLD both fire)
                (ts_evening, 65.0),  # cool again -> reactivates
            ]
        )

        result = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, False
        )
        assert result[ts_morning]["nat_vent_active"] is True
        assert result[ts_midday]["nat_vent_active"] is False
        assert result[ts_evening]["nat_vent_active"] is True
        assert (
            result[ts_morning]["hvac_mode"]
            == result[ts_midday]["hvac_mode"]
            == result[ts_evening]["hvac_mode"]
            == "cool"
        )

    def test_hot_day_reactivation_gate_uses_comfort_cool_not_banked_indoor(self) -> None:
        """Direct proof of the DRY-cross-check fix: the gate's indoor input on a 'cool'
        day is comfort_cool, not the (possibly pre-cool-banked) predicted_indoor curve.
        Outdoor here sits between the banked curve's implied threshold and comfort_cool's
        -- only correct if comfort_cool is really what's being compared."""
        mod = _mod()
        ts1 = _ts(23)
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts1, 64.0, 70.0)])
        # predicted_indoor banked down to 70 (e.g. pre-cool banking) -- if the gate used
        # this value, outdoor(72) would need to be < 70-1=69 to activate, which it isn't.
        predicted_indoor = _series([(ts1, 70.0)])
        # outdoor(72) IS < comfort_cool(76) - hysteresis(1) = 75 -> activates only if the
        # gate compares against comfort_cool, not the banked 70.0 curve value.
        forecast_outdoor = _series([(ts1, 72.0)])

        result = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, _BASE_CONFIG, "home", None, False, None, None, False
        )
        assert result[ts1]["nat_vent_active"] is True, (
            "gate must compare outdoor against comfort_cool (76), not the banked "
            "predicted_indoor value (70) -- if it used the banked curve, outdoor=72 would "
            "not satisfy 72 < 70-1=69 and this would incorrectly stay inactive"
        )

    def test_cross_check_walk_forward_agrees_with_compute_nat_vent_plan(self) -> None:
        """Issue #878-followup DRY cross-check: reusing the exact live production curve
        from the plan's Context table, compute_nat_vent_plan() (Fix D) and
        _walk_forward_regime() (Fix E) must agree on the reopen hour (23:00), not one at
        23:00 and the other at 02:00. This is the regression guard for the gap the DRY
        re-check found -- a future edit could silently reintroduce the divergence by
        changing one call site's `indoor` substitution without the other."""
        from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan

        mod = _mod()
        config = dict(_BASE_CONFIG)
        config["comfort_cool"] = 74.0
        config["natural_vent_delta"] = 3.0
        config["nat_vent_hysteresis_f"] = 1.0

        # Live production data (v0.7.32 incident), 2026-09-08 21:00 through 2026-09-09 02:00.
        curve = [
            ("2026-09-08T21:00:00", 77.0, 73.8),
            ("2026-09-08T22:00:00", 74.0, 70.0),
            ("2026-09-08T23:00:00", 72.0, 70.2),
            ("2026-09-09T00:00:00", 71.0, 70.3),
            ("2026-09-09T01:00:00", 70.0, 70.3),
            ("2026-09-09T02:00:00", 69.0, 70.1),
        ]
        predicted_indoor = _series([(ts, indoor) for ts, _outdoor, indoor in curve])
        predicted_outdoor = _series([(ts, outdoor) for ts, outdoor, _indoor in curve])
        band = _band([(ts, 64.0, 72.0) for ts, _o, _i in curve])
        day_modes = {date(2026, 9, 8): "cool", date(2026, 9, 9): "cool"}

        walk_result = mod._walk_forward_regime(
            day_modes, predicted_indoor, predicted_outdoor, band, config, "home", None, False, None, None, False
        )
        walk_reopen_ts = next((ts for ts, r in walk_result.items() if r["nat_vent_active"]), None)
        assert walk_reopen_ts == "2026-09-08T23:00:00", (
            f"_walk_forward_regime() reopened at {walk_reopen_ts}, expected 23:00"
        )

        plan_result = compute_nat_vent_plan(
            predicted_indoor,
            predicted_outdoor,
            comfort_cool=74.0,
            window_open_time=None,
        )
        # This curve's own first entry (21:00, outdoor=77 >= indoor(73.8)-1) already
        # satisfies the close condition, so nat_vent_cutoff lands there (already_reached);
        # what matters for this cross-check is where evening_open_time lands relative to
        # that cutoff -- 23:00, matching the walk's own reopen hour above.
        assert plan_result["nat_vent_cutoff"] is not None
        assert plan_result["evening_open_time"] is not None
        assert plan_result["evening_open_time"].isoformat().startswith("2026-09-08T23:00:00"), (
            f"compute_nat_vent_plan() reopened at {plan_result['evening_open_time']}, expected 23:00 -- "
            "must match _walk_forward_regime()'s own reopen hour asserted above"
        )

    def test_cross_check_agrees_on_pre_peak_dip_not_spurious_early_reopen(self) -> None:
        """Issue #948 (Root Cause 2): _walk_forward_regime()'s hourly
        decide_nat_vent_gate() reactivation check has no concept of "has today's
        ceiling already been breached before this hour" -- the exact same missing
        guard as compute_nat_vent_plan()'s evening_open_time (see
        test_nat_vent_plan_single_source.py's TestEveningOpenTimeRequiresGenuinePeakBreach).
        Both are independent reimplementations of "has it cooled back down enough
        to reopen", and both currently reopen on a pre-peak dip that occurs well
        before the day's real ceiling breach.

        Curve (single Hot/"cool"-mode day, comfort_cool=76, real indoor curve
        pinned to a LOW 70F "overnight" value -- the low bar the close condition
        exploits, per the plan's Root Cause 2 writeup):
          09:00 outdoor=71 -- session starts active (initial_session_active=True)
                              and immediately exits: OUTDOOR_RISE (71 >= indoor 70).
                              This is "the close" -- note outdoor(71) is nowhere
                              near comfort_cool(76) yet.
          10:00 outdoor=65 -- pre-peak dip. The gate's reactivation check on a
                              "cool" day compares against comfort_cool (76), not
                              the real 70F indoor curve (Issue #878-followup's own
                              DRY substitution) -- 65 < 76-1=75, so the gate fires
                              here. This is the SPURIOUS reactivation: outdoor has
                              never actually exceeded comfort_cool anywhere yet.
          13:00 outdoor=90 -- the day's GENUINE ceiling breach (90 > comfort_cool 76).
          14:00 outdoor=85 -- still hot, stays inactive.
          18:00 outdoor=60 -- genuine post-peak decline -- the correct reopen hour,
                              now that a real breach has actually occurred.

        Both the current buggy _walk_forward_regime() and the current buggy
        compute_nat_vent_plan() agree with EACH OTHER at 10:00 (the spurious pre-
        peak dip) -- mutual agreement alone does not prove correctness, which is
        exactly why this test asserts against the objectively correct reopen hour
        (18:00), not just cross-agreement. This FAILS against current code on both
        sides: _walk_forward_regime() reactivates at 10:00, and
        compute_nat_vent_plan()'s evening_open_time also lands at 10:00.
        """
        from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan

        mod = _mod()
        config = dict(_BASE_CONFIG)
        config["comfort_cool"] = 76.0
        config["natural_vent_delta"] = 3.0
        config["nat_vent_hysteresis_f"] = 1.0

        ts_close, ts_dip, ts_breach, ts_still_hot, ts_reopen = (
            _ts(9),
            _ts(10),
            _ts(13),
            _ts(14),
            _ts(18),
        )
        day_modes = {date(2026, 7, 13): "cool"}
        band = _band([(ts, 68.0, 76.0) for ts in (ts_close, ts_dip, ts_breach, ts_still_hot, ts_reopen)])
        # Real indoor curve pinned low (70F) -- the "low overnight indoor" the close
        # condition exploits; the gate's own reactivation check substitutes
        # comfort_cool for a "cool" day regardless of this curve (see
        # _walk_forward_regime()'s docstring), which is exactly what lets the close
        # and the reactivation check disagree about what "hot enough" means.
        predicted_indoor = _series(
            [(ts_close, 70.0), (ts_dip, 70.0), (ts_breach, 70.0), (ts_still_hot, 70.0), (ts_reopen, 70.0)]
        )
        forecast_outdoor = _series(
            [(ts_close, 71.0), (ts_dip, 65.0), (ts_breach, 90.0), (ts_still_hot, 85.0), (ts_reopen, 60.0)]
        )

        walk_result = mod._walk_forward_regime(
            day_modes, predicted_indoor, forecast_outdoor, band, config, "home", None, False, None, None, True
        )
        walk_reopen_ts = next((ts for ts, r in walk_result.items() if r["nat_vent_active"]), None)
        assert walk_reopen_ts == ts_reopen, (
            f"_walk_forward_regime() reactivated at {walk_reopen_ts}, expected the genuine post-breach"
            f" reopen at {ts_reopen} -- {ts_dip} is the pre-peak-dip bug (Issue #948): outdoor never"
            " actually exceeded comfort_cool (76) by that hour, so reopening there has no real"
            " 'got too hot' event behind it."
        )

        plan_result = compute_nat_vent_plan(
            predicted_indoor,
            forecast_outdoor,
            comfort_cool=76.0,
            window_open_time=None,
        )
        assert plan_result["nat_vent_cutoff"] is not None
        assert plan_result["evening_open_time"] is not None
        assert plan_result["evening_open_time"].isoformat().startswith(ts_reopen), (
            f"compute_nat_vent_plan() reopened at {plan_result['evening_open_time']}, expected"
            f" {ts_reopen} -- must match _walk_forward_regime()'s own (corrected) reopen hour"
            " asserted above, not the pre-peak dip at 10:00."
        )


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
