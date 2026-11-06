"""Tests for _compute_target_band_schedule (Issue #119 — dynamic Target Band).

The function is a pure module-level function in coordinator.py, so it can be
imported and tested directly without a running HA instance.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.coordinator import _compute_target_band_schedule  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    """Return a UTC-aware datetime on a fixed base date at the given hour:minute."""
    base = datetime(2025, 6, 15, tzinfo=UTC)
    return base.replace(hour=hour, minute=minute) + timedelta(days=day_offset)


_BASE_CONFIG = {
    "comfort_heat": 70.0,
    "comfort_cool": 75.0,
    "setback_heat": 60.0,
    "setback_cool": 80.0,
    "sleep_heat": 66.0,
    "sleep_cool": 78.0,
    "wake_time": "07:00",
    "sleep_time": "22:00",
}

# "now" is at 08:00 on the same base date (within wake ramp period)
_NOW = _ts(8)


# ---------------------------------------------------------------------------
# Test 1 — home, awake hours → comfort band
# ---------------------------------------------------------------------------
class TestHomeAwakeBand:
    def test_home_awake_band_is_comfort(self):
        """Timestamps during awake hours (wake+2h…sleep) should return comfort band."""
        ts_awake = _ts(12)  # noon — well inside the awake window
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0
        assert entry["upper"] == 75.0


# ---------------------------------------------------------------------------
# Test 2 — home, sleep hours → sleep setback band
# ---------------------------------------------------------------------------
class TestHomeSleepBand:
    def test_home_sleep_band_is_sleep_setback(self):
        """Timestamps before wake_time (pre-wake / deep-sleep) return sleep band."""
        ts_sleep = _ts(2)  # 02:00 — before wake_time of 07:00
        result = _compute_target_band_schedule([ts_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# Test 3 — away today → flat setback band
# ---------------------------------------------------------------------------
class TestAwayTodayBand:
    def test_away_today_band_is_setback(self):
        """Away occupancy + today's date → flat setback band, no schedule ramp."""
        ts_today = _ts(14)  # 14:00 today
        result = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "away", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 60.0
        assert entry["upper"] == 80.0


# ---------------------------------------------------------------------------
# Test 4 — vacation today → deep setback band (extra ±3°F)
# ---------------------------------------------------------------------------
class TestVacationTodayBand:
    def test_vacation_today_band_is_deep_setback(self):
        """Vacation occupancy + today → setback ± VACATION_SETBACK_EXTRA (3°F)."""
        ts_today = _ts(14)  # 14:00 today
        result = _compute_target_band_schedule([ts_today], _BASE_CONFIG, "vacation", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 57.0  # 60 - 3
        assert entry["upper"] == 83.0  # 80 + 3


# ---------------------------------------------------------------------------
# Test 5 — away but tomorrow → uses home schedule, not setback
# ---------------------------------------------------------------------------
class TestAwayTomorrowUsesSchedule:
    def test_away_tomorrow_uses_schedule(self):
        """Away occupancy + future day → home wake/sleep schedule applies (not setback)."""
        ts_tomorrow_sleep = _ts(2, day_offset=1)  # 02:00 tomorrow — sleep hours
        result = _compute_target_band_schedule([ts_tomorrow_sleep], _BASE_CONFIG, "away", _NOW)
        assert len(result) == 1
        entry = result[0]
        # Tomorrow future → pre-wake sleep band
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# Test 6 — wake ramp period interpolates between sleep and comfort
# ---------------------------------------------------------------------------
class TestWakeRampInterpolates:
    def test_ramp_period_interpolates(self):
        """At wake_h + 1h (midpoint of 2h ramp), values should be halfway between sleep and comfort."""
        # wake_time=07:00, ramp=2h → at 08:00 (wake_h + 1h) frac = 0.5
        ts_ramp = _ts(8)  # exactly 1h into the 2h wake ramp
        result = _compute_target_band_schedule([ts_ramp], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        # lower = 66 + 0.5 * (70 - 66) = 68.0
        # upper = 78 + 0.5 * (75 - 78) = 76.5
        assert entry["lower"] == 68.0
        assert entry["upper"] == 76.5


# ---------------------------------------------------------------------------
# Test 7 — returns one entry per timestamp
# ---------------------------------------------------------------------------
class TestBandLengthMatchesTimestamps:
    def test_band_returns_one_entry_per_timestamp(self):
        """Result length must equal input timestamp list length."""
        timestamps = [_ts(h) for h in range(0, 8)]
        result = _compute_target_band_schedule(timestamps, _BASE_CONFIG, "home", _NOW)
        assert len(result) == 8


# ---------------------------------------------------------------------------
# Test 8 — guest mode uses home schedule
# ---------------------------------------------------------------------------
class TestGuestModeUsesHomeSchedule:
    def test_guest_mode_uses_home_schedule(self):
        """Guest occupancy should behave identically to home occupancy."""
        ts_awake = _ts(12)
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "guest", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0
        assert entry["upper"] == 75.0


# ---------------------------------------------------------------------------
# Test 9 — sleep ramp at bedtime (1h ramp from comfort → sleep band)
# ---------------------------------------------------------------------------
class TestSleepRampInterpolates:
    def test_sleep_ramp_interpolates(self):
        """At sleep_h + 0.5h (midpoint of 1h ramp), values should be halfway between comfort and sleep."""
        # sleep_time=22:00, 1h ramp → at 22:30 frac = 0.5
        ts_sleep_ramp = _ts(22, minute=30)
        result = _compute_target_band_schedule([ts_sleep_ramp], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        # lower = 70 + 0.5 * (66 - 70) = 68.0
        # upper = 75 + 0.5 * (78 - 75) = 76.5
        assert entry["lower"] == 68.0
        assert entry["upper"] == 76.5


# ---------------------------------------------------------------------------
# Test 10 — post-sleep (after sleep_h + 1h) → full sleep band
# ---------------------------------------------------------------------------
class TestPostSleepBand:
    def test_post_sleep_is_sleep_band(self):
        """Timestamps after sleep_h + 1h (post-ramp) should return the full sleep band."""
        # sleep_time=22:00; post-sleep starts at 23:00
        ts_post_sleep = _ts(23, minute=30)
        result = _compute_target_band_schedule([ts_post_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0
        assert entry["upper"] == 78.0


# ---------------------------------------------------------------------------
# NEW-1: vacation future days stay in setback (I5)
# ---------------------------------------------------------------------------
class TestVacationFutureDaysSetback:
    def test_vacation_future_days_stay_in_setback(self):
        """Vacation future days must stay at deep setback, not revert to home schedule.

        Away mode only applies setback to today; vacation mode applies setback to ALL days
        because there is no scheduled return time during a vacation.
        """
        ts_tomorrow_waking = _ts(14, day_offset=1)  # 14:00 tomorrow
        result = _compute_target_band_schedule([ts_tomorrow_waking], _BASE_CONFIG, "vacation", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 57.0, (  # setback_heat(60) - VACATION_EXTRA(3)
            f"Vacation tomorrow: expected lower=57.0, got {entry['lower']}"
        )
        assert entry["upper"] == 83.0, (  # setback_cool(80) + VACATION_EXTRA(3)
            f"Vacation tomorrow: expected upper=83.0, got {entry['upper']}"
        )


# ---------------------------------------------------------------------------
# NEW-2: midnight wraparound for night-owl schedules (I6)
# ---------------------------------------------------------------------------
class TestMidnightWraparound:
    def test_noon_is_awake_for_late_night_schedule(self):
        """Night-owl schedule (sleep=01:00, wake=09:00): noon (12:00) must be in awake band.

        Without wraparound normalization, noon falls through to 'post-sleep' (sleep band)
        because h=12 > sleep_h=1 and h > sleep_h+1=2, which the naïve comparisons
        misidentify as post-sleep.
        """
        night_owl_config = {
            **_BASE_CONFIG,
            "sleep_time": "01:00",
            "wake_time": "09:00",
        }
        ts_noon = _ts(12)
        result = _compute_target_band_schedule([ts_noon], night_owl_config, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 70.0, f"Night-owl noon: expected awake lower=70.0, got {entry['lower']}"
        assert entry["upper"] == 75.0, f"Night-owl noon: expected awake upper=75.0, got {entry['upper']}"


# NEW-3 (revised Issue #1076): the trend setback_modifier does NOT shift away/vacation bands.
# The engine's select_comfort_band() never applies it (dropped by #249 P3), so the chart must
# show exactly what the thermostat holds.
# ---------------------------------------------------------------------------
class TestSetbackModifier:
    def test_signature_no_longer_accepts_setback_modifier(self):
        """The parameter was removed: nothing can shift the setback edges by the trend modifier."""
        with pytest.raises(TypeError):
            _compute_target_band_schedule([_ts(14)], _BASE_CONFIG, "away", _NOW, setback_modifier=2.0)  # type: ignore[call-arg]

    def test_classification_modifier_does_not_shift_away_band(self):
        """Invariant guard: a classification trend modifier never shifts the away setback edges."""
        cls = SimpleNamespace(setback_modifier=2.0, hvac_mode="heat", day_type="cold")
        result = _compute_target_band_schedule([_ts(14)], _BASE_CONFIG, "away", _NOW, classification=cls)
        assert (result[0]["lower"], result[0]["upper"]) == (60.0, 80.0)

    def test_restore_hold_uses_unshifted_setback(self):
        """Invariant guard: the restore-hold band uses the unshifted setback edges."""
        cls = SimpleNamespace(setback_modifier=-3.0, hvac_mode="cool", day_type="hot")
        result = _compute_target_band_schedule(
            [_NOW - timedelta(hours=1)], _BASE_CONFIG, "home", _NOW, classification=cls, restore_hold=True
        )
        assert (result[0]["lower"], result[0]["upper"]) == (60.0, 80.0)

    @pytest.mark.parametrize("modifier", [-3.0, -2.0, 0.0, 2.0, 3.0])
    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_chart_band_equals_select_comfort_band(self, occupancy, modifier):
        """Parity guard (invariant, not a regression catcher): chart band equals the engine's real
        band selector for away/vacation, for any trend modifier."""
        from custom_components.climate_advisor.automation import select_comfort_band

        cls = SimpleNamespace(setback_modifier=modifier, hvac_mode="cool", day_type="hot")
        engine_band = select_comfort_band(
            cls, _BASE_CONFIG, occupancy_mode=occupancy, in_sleep_window=False, aggressive_savings=False
        )
        chart = _compute_target_band_schedule([_ts(14)], _BASE_CONFIG, occupancy, _NOW, classification=cls)
        assert chart[0]["lower"] == engine_band.floor
        assert chart[0]["upper"] == engine_band.ceiling

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_build_target_band_for_trend_day_matches_select_comfort_band(self, occupancy):
        """Real coordinator entry point (_build_target_band_for) with classification.setback_modifier=3.0:
        fails if a caller re-passes the modifier to _compute_target_band_schedule (signature removed)."""
        import types

        from custom_components.climate_advisor.automation import select_comfort_band
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        cls = object.__new__(DayClassification)
        for k, v in dict(
            day_type="hot",
            hvac_mode="cool",
            setback_modifier=3.0,
            pre_condition=False,
            pre_condition_target=None,
            trend_direction="warming",
            trend_magnitude=6.0,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
        ).items():
            object.__setattr__(cls, k, v)
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = dict(_BASE_CONFIG)
        coord._current_classification = cls
        coord._occupancy_mode = occupancy
        coord._occupancy_restore_held_cache = False
        coord._hourly_forecast_temps = [{"datetime": _ts(14).isoformat(), "temperature": 90.0}]
        coord._compute_pre_cool_trigger_time = lambda: None
        coord._tou_precondition_window_tuple = lambda: None
        with patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x):
            band = types.MethodType(ClimateAdvisorCoordinator._build_target_band_for, coord)(_NOW, None)
        engine_band = select_comfort_band(
            cls, _BASE_CONFIG, occupancy_mode=occupancy, in_sleep_window=False, aggressive_savings=False
        )
        assert band[0]["lower"] == engine_band.floor
        assert band[0]["upper"] == engine_band.ceiling


# ---------------------------------------------------------------------------
# NEW-4: compute_bedtime_setback used for sleep band when thermal model available (G1/G2)
# ---------------------------------------------------------------------------
class TestComputeBedtimeSetbackIntegration:
    def test_compute_bedtime_setback_used_when_model_available(self):
        """With thermal_model + classification, sleep lower = raw configured sleep_heat (Fix #333).

        Config has explicit sleep_heat=66. compute_bedtime_setback now ignores setback_modifier,
        returning the raw sleep_heat = 66.0 (unclamped since Issue #1366).
        The modifier is applied by handle_pre_cool() as a separate mid-night event.
        """
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        thermal_model = {
            "confidence": "low",
            "k_passive": -0.3,
            "k_active_heat": 4.0,
            "k_active_cool": -4.0,
            "heating_rate_f_per_hour": 4.0,
            "cooling_rate_f_per_hour": -4.0,
        }
        classification = SimpleNamespace(hvac_mode="heat", setback_modifier=2.0)
        result = _compute_target_band_schedule(
            [ts_sleep],
            _BASE_CONFIG,
            "home",
            _NOW,
            thermal_model=thermal_model,
            classification=classification,
        )
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0, (
            f"With thermal_model+classification: expected lower=66.0 "
            f"(raw sleep_heat, no modifier), got {entry['lower']}"
        )

    def test_compute_bedtime_setback_cool_used_when_model_available(self):
        """With thermal_model + cool classification, sleep upper = raw configured sleep_cool (Fix #333).

        Warming trend (modifier=-2.0) is no longer applied in compute_bedtime_setback().
        Result = the raw sleep_cool = 78.0 (unclamped since Issue #1366).
        The pre-cool mid-night event (handle_pre_cool) applies the modifier separately.
        """
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        thermal_model = {
            "confidence": "low",
            "k_passive": -0.3,
            "k_active_heat": 4.0,
            "k_active_cool": -4.0,
            "heating_rate_f_per_hour": 4.0,
            "cooling_rate_f_per_hour": -4.0,
        }
        classification = SimpleNamespace(hvac_mode="cool", setback_modifier=-2.0)
        result = _compute_target_band_schedule(
            [ts_sleep],
            _BASE_CONFIG,
            "home",
            _NOW,
            thermal_model=thermal_model,
            classification=classification,
        )
        assert len(result) == 1
        entry = result[0]
        assert entry["upper"] == 78.0, (
            f"With cool thermal_model+classification: expected upper=78.0 "
            f"(raw sleep_cool, no modifier), got {entry['upper']}"
        )

    @pytest.mark.parametrize(
        ("hvac_mode", "overrides", "edge"),
        [
            ("heat", {"setback_heat": 60.0, "sleep_heat": 58.0}, "lower"),  # sleep heat below away heat
            ("cool", {"setback_cool": 80.0, "sleep_cool": 82.0}, "upper"),  # sleep cool above away cool
        ],
    )
    def test_sleep_band_outside_away_limits_matches_engine(self, hvac_mode, overrides, edge):
        """Issue #1366: the chart's overnight edge equals select_comfort_band()'s sleep edge (what the
        thermostat holds) even when the sleep temp is beyond the away setback -- no clamp to away."""
        from custom_components.climate_advisor.automation import select_comfort_band

        config = {**_BASE_CONFIG, **overrides}
        classification = SimpleNamespace(hvac_mode=hvac_mode, setback_modifier=0.0, day_type="cold")
        result = _compute_target_band_schedule(
            [_ts(2)], config, "home", _NOW, thermal_model={}, classification=classification
        )
        engine_band = select_comfort_band(
            classification, config, occupancy_mode="home", in_sleep_window=True, aggressive_savings=False
        )
        engine_edge = engine_band.floor if edge == "lower" else engine_band.ceiling
        assert engine_edge == overrides[f"sleep_{hvac_mode}"]
        assert result[0][edge] == engine_edge, (
            f"Chart shows {result[0][edge]}°F overnight but the thermostat holds {engine_edge}°F"
        )

    # ---------------------------------------------------------------------------
    # NEW-5: config fallback when no thermal model (G1/G2)
    # ---------------------------------------------------------------------------
    def test_config_fallback_when_no_model(self):
        """Without thermal_model, sleep lower bound = config sleep_heat (no adaptive calc)."""
        ts_sleep = _ts(2)  # 02:00 — deep sleep window
        result = _compute_target_band_schedule([ts_sleep], _BASE_CONFIG, "home", _NOW)
        assert len(result) == 1
        entry = result[0]
        assert entry["lower"] == 66.0, (
            f"Without thermal_model: expected lower=66.0 (config default), got {entry['lower']}"
        )


# ---------------------------------------------------------------------------
# TOU pre-conditioning override (Issue #786)
# ---------------------------------------------------------------------------
class TestTouPreconditionWindow:
    """tou_precondition_window overrides lower/upper during the pre-conditioning lead
    time — the additive-override-layer mechanism, same shape as pre_cool_target."""

    def test_cool_mode_overrides_lower_inside_window(self):
        """mode="cool" (banking toward the floor) overrides lower; upper is untouched."""
        ts_awake = _ts(12)  # noon — normally comfort band [70, 75]
        window = (_ts(11), _ts(13), 64.0, "cool")
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW, tou_precondition_window=window)
        assert result[0]["lower"] == 64.0
        assert result[0]["upper"] == 75.0

    def test_heat_mode_overrides_upper_inside_window(self):
        """mode="heat" (banking toward the ceiling) overrides upper; lower is untouched."""
        ts_awake = _ts(12)
        window = (_ts(11), _ts(13), 79.0, "heat")
        result = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW, tou_precondition_window=window)
        assert result[0]["lower"] == 70.0
        assert result[0]["upper"] == 79.0

    def test_no_override_outside_window(self):
        """A timestamp outside [window_start, window_end) is unaffected."""
        ts_outside = _ts(15)
        window = (_ts(11), _ts(13), 64.0, "cool")
        result = _compute_target_band_schedule([ts_outside], _BASE_CONFIG, "home", _NOW, tou_precondition_window=window)
        assert result[0]["lower"] == 70.0
        assert result[0]["upper"] == 75.0

    def test_window_end_is_exclusive(self):
        """A timestamp exactly at window_end is NOT overridden (half-open interval,
        matching resolve_tou_phase()'s own [start, end) convention)."""
        window_end = _ts(13)
        window = (_ts(11), window_end, 64.0, "cool")
        result = _compute_target_band_schedule([window_end], _BASE_CONFIG, "home", _NOW, tou_precondition_window=window)
        assert result[0]["lower"] == 70.0

    def test_overrides_sleep_band_too(self):
        """The override applies regardless of which base-band branch computed the value —
        here overriding during a sleep-window timestamp, not just the awake comfort band."""
        ts_sleep = _ts(2)
        window = (_ts(1), _ts(3), 62.0, "cool")
        result = _compute_target_band_schedule([ts_sleep], _BASE_CONFIG, "home", _NOW, tou_precondition_window=window)
        assert result[0]["lower"] == 62.0

    def test_none_window_is_a_no_op(self):
        """Omitting tou_precondition_window (the default) must produce identical output to
        not passing it at all — no accidental behavior change for every existing caller."""
        ts_awake = _ts(12)
        with_none = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW, tou_precondition_window=None)
        without_param = _compute_target_band_schedule([ts_awake], _BASE_CONFIG, "home", _NOW)
        assert with_none == without_param


# ---------------------------------------------------------------------------
# Pure pre-cool-trigger-time extraction (Issue #514, Phase 2 step 1)
#
# _compute_pre_cool_trigger_time() was previously only reachable as a bound
# coordinator method (reading self._current_classification/self.config and
# calling dt_util.now() internally) — this is the root enabler that let
# _build_predicted_indoor_future() (a module-level function with no `self`)
# drift into its own independent, not-identical inline reimplementation
# instead of calling the canonical logic. The method is now a thin wrapper
# around the pure, module-level _compute_pre_cool_trigger_time_pure().
# ---------------------------------------------------------------------------
class TestPreCoolTriggerTimePureExtraction:
    """Bit-identical-output proof: the method wrapper and the new pure function
    must return the exact same value for the exact same inputs, mandatory before
    any call site is switched to consume the pure function directly."""

    def _make_coordinator(self, classification, config: dict):
        """Bare ClimateAdvisorCoordinator bound to the real
        _compute_pre_cool_trigger_time method — object.__new__() + types.MethodType()
        (the established partial-instantiation pattern for coordinator unit tests,
        see test_contact_status.py / test_daily_record_accuracy.py)."""
        import types

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = config
        coord._current_classification = classification
        coord._compute_pre_cool_trigger_time = types.MethodType(
            ClimateAdvisorCoordinator._compute_pre_cool_trigger_time, coord
        )
        return coord

    def _classification(self, **overrides):
        defaults = dict(
            day_type="hot",
            trend_direction="stable",
            trend_magnitude=1.0,
            today_high=95.0,
            today_low=72.0,
            tomorrow_high=96.0,
            tomorrow_low=73.0,
            hvac_mode="cool",
            windows_recommended=True,
            window_open_time=None,
            window_close_time=time(20, 0),
            setback_modifier=0.0,
            pre_condition=False,
            pre_condition_target=None,
        )
        defaults.update(overrides)
        c = object.__new__(DayClassification)
        for k, v in defaults.items():
            object.__setattr__(c, k, v)
        return c

    def test_wrapper_matches_pure_function_nat_vent_close_branch(self):
        """Primary branch: nat-vent window_close_time + delay."""
        from custom_components.climate_advisor.coordinator import (
            _compute_pre_cool_trigger_time_pure,
            dt_util,
        )

        classification = self._classification()
        config = dict(_BASE_CONFIG)
        fixed_now = _ts(14)

        with (
            patch.object(dt_util, "now", return_value=fixed_now),
            patch.object(
                dt_util,
                "as_local",
                side_effect=lambda x: x if x.tzinfo is not None else x.replace(tzinfo=UTC),
            ),
        ):
            coord = self._make_coordinator(classification, config)
            method_result = coord._compute_pre_cool_trigger_time()
            pure_result = _compute_pre_cool_trigger_time_pure(classification, config, fixed_now)

        assert method_result is not None, "fixture must actually reach the nat-vent-close branch"
        assert method_result == pure_result

    def test_wrapper_matches_pure_function_wake_time_fallback_branch(self):
        """Fallback branch: no window_close_time -> wake_time - offset."""
        from custom_components.climate_advisor.coordinator import (
            _compute_pre_cool_trigger_time_pure,
            dt_util,
        )

        classification = self._classification(window_close_time=None, windows_recommended=False)
        config = dict(_BASE_CONFIG)
        fixed_now = _ts(14)

        with (
            patch.object(dt_util, "now", return_value=fixed_now),
            patch.object(
                dt_util,
                "as_local",
                side_effect=lambda x: x if x.tzinfo is not None else x.replace(tzinfo=UTC),
            ),
        ):
            coord = self._make_coordinator(classification, config)
            method_result = coord._compute_pre_cool_trigger_time()
            pure_result = _compute_pre_cool_trigger_time_pure(classification, config, fixed_now)

        assert method_result is not None, "fixture must actually reach the wake-time fallback branch"
        assert method_result == pure_result

    def test_wrapper_matches_pure_function_ineligible_returns_none(self):
        """Not pre-cool-eligible (mild day, hvac off) -> both paths return None identically."""
        from custom_components.climate_advisor.coordinator import (
            _compute_pre_cool_trigger_time_pure,
            dt_util,
        )

        classification = self._classification(
            day_type="mild",
            hvac_mode="off",
            today_high=72.0,
            tomorrow_high=72.0,
            trend_direction="stable",
            trend_magnitude=0.0,
        )
        config = dict(_BASE_CONFIG)
        fixed_now = _ts(14)

        with patch.object(dt_util, "now", return_value=fixed_now):
            coord = self._make_coordinator(classification, config)
            method_result = coord._compute_pre_cool_trigger_time()

        pure_result = _compute_pre_cool_trigger_time_pure(classification, config, fixed_now)

        assert method_result is None
        assert pure_result is None
        assert method_result == pure_result


# ---------------------------------------------------------------------------
# Issue #1087 -- aggressive savings widens the awake band the thermostat is armed with
# ---------------------------------------------------------------------------
class TestAggressiveSavingsWidensAwakeBand:
    """Occupant view: with "Prefer savings" on, the thermostat holds the awake band wider; the chart
    (and the predicted-indoor curve built on it) must show that band, not the plain comfort range."""

    _SAVINGS = {**_BASE_CONFIG, "aggressive_savings": True}

    def test_awake_band_includes_margin(self):
        from custom_components.climate_advisor.const import CEILING_ESCALATION_SAVINGS_MARGIN_F as margin

        entry = _compute_target_band_schedule([_ts(12)], self._SAVINGS, "home", _NOW)[0]
        assert entry["lower"] == 70.0 - margin
        assert entry["upper"] == 75.0 + margin

    def test_wake_ramp_interpolates_to_the_widened_edges(self):
        # 1h into the 2h ramp: lower = 66 + 0.5*(68-66) = 67.0, upper = 78 + 0.5*(77-78) = 77.5
        entry = _compute_target_band_schedule([_ts(8)], self._SAVINGS, "home", _NOW)[0]
        assert (entry["lower"], entry["upper"]) == (67.0, 77.5)

    def test_sleep_ramp_interpolates_from_the_widened_edges(self):
        # 30min into the 1h sleep ramp (sleep 22:00): lower = 68 + 0.5*(66-68) = 67.0, upper = 77 + 0.5*(78-77) = 77.5
        entry = _compute_target_band_schedule([_ts(22, 30)], self._SAVINGS, "home", _NOW)[0]
        assert (entry["lower"], entry["upper"]) == (67.0, 77.5)

    def test_setback_and_sleep_bands_are_not_widened(self):
        away = _compute_target_band_schedule([_ts(12)], self._SAVINGS, "away", _NOW)[0]
        assert (away["lower"], away["upper"]) == (60.0, 80.0)
        sleep = _compute_target_band_schedule([_ts(3)], self._SAVINGS, "home", _NOW)[0]
        assert (sleep["lower"], sleep["upper"]) == (66.0, 78.0)

    @pytest.mark.parametrize("hvac_mode", ["heat", "cool"])
    @pytest.mark.parametrize("aggressive", [True, False])
    def test_awake_chart_edges_equal_what_the_engine_arms(self, hvac_mode, aggressive):
        from custom_components.climate_advisor.automation import select_comfort_band

        config = {**_BASE_CONFIG, "aggressive_savings": aggressive}
        classification = SimpleNamespace(hvac_mode=hvac_mode, day_type="warm")
        band = select_comfort_band(
            classification, config, occupancy_mode="home", in_sleep_window=False, aggressive_savings=aggressive
        )
        entry = _compute_target_band_schedule([_ts(12)], config, "home", _NOW)[0]
        assert (entry["lower"], entry["upper"]) == (band.floor, band.ceiling)
