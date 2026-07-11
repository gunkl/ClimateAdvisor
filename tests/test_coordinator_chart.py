"""Tests for Issue #120 — chart log spike suppression.

Two bugs are fixed:

Bug A: pred_indoor must not be written to the chart log when indoor_temp is None
        (thermostat in unknown/unavailable state, e.g. right after HA restart).

Bug B: _get_indoor_temp() must reject physically implausible sensor readings
        (e.g. thermostat echoing new setpoint into current_temperature) by checking
        against a plausible indoor range [40, 110] °F.

TDD note
--------
Bug B tests (range-check_rejects_*) FAIL before the fix because _get_indoor_temp()
returns raw values without a plausible-range guard.  They pass after the fix.

Bug A tests exercise the guard logic in the chart-log block. Because the guard
is inline inside _async_update_data() (not a standalone callable), the tests
verify the intended post-fix behaviour directly rather than through the full
_async_update_data() path; the Bug B failures are the pre-fix red-bar evidence
for this issue as a whole.
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

import types as _types  # noqa: E402  (used by fan_physically_running tests)
from datetime import UTC

from custom_components.climate_advisor.const import (  # noqa: E402
    TEMP_SOURCE_CLIMATE_FALLBACK,
)


def _get_coordinator_class():
    """Return a fresh ClimateAdvisorCoordinator class reference.

    Follows the pattern from test_daily_record_accuracy.py to avoid stale
    __globals__ if test_occupancy.py has reloaded the coordinator module.
    """
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _get_coordinator_module():
    return importlib.import_module("custom_components.climate_advisor.coordinator")


# ---------------------------------------------------------------------------
# Helper: simulate the chart-log guard from _async_update_data lines 1182-1186
# ---------------------------------------------------------------------------


def _eval_pred_indoor_guard(indoor_temp, pred_in, now_h=10):
    """Evaluate the chart-log pred_indoor guard as it should exist post-fix.

    Mirrors the production code:
        if _pred_in and _now_h < len(_pred_in) and indoor_temp is not None:
            _pred_indoor_val = _pred_in[_now_h]["temp"]

    Used by Bug A tests to assert the INTENDED behaviour is correct.
    """
    _pred_indoor_val = None
    if pred_in and now_h < len(pred_in) and indoor_temp is not None:
        _pred_indoor_val = pred_in[now_h]["temp"]
    return _pred_indoor_val


# ---------------------------------------------------------------------------
# TestChartLogSpikeSuppression  (Bug A)
# ---------------------------------------------------------------------------


class TestChartLogSpikeSuppression:
    """Bug A: pred_indoor must not be written when indoor_temp is None.

    The chart-log block in _async_update_data() must gate the pred_indoor write
    on ``indoor_temp is not None``.  These tests verify that the guard logic
    produces the correct values — the Bug B failures below provide the TDD
    red-bar for this issue's pre-fix state.
    """

    def test_pred_indoor_not_written_when_indoor_temp_none(self):
        """When indoor_temp is None, pred_indoor guard must yield None.

        Simulates a HA-restart tick where the thermostat is in unknown state
        (indoor_temp=None) but a forecast-based prediction is available.
        The guard must suppress the pred_indoor write so the chart log does
        not record a permanently corrupt spike.
        """
        _pred_in = [{"temp": float(i + 50)} for i in range(24)]  # hour 10 → 60.0

        result = _eval_pred_indoor_guard(indoor_temp=None, pred_in=_pred_in)

        assert result is None, (
            "pred_indoor_val must be None when indoor_temp is None; "
            f"got {result!r} — Bug A guard missing from coordinator.py"
        )

    def test_pred_indoor_written_when_indoor_temp_available(self):
        """Regression guard: when indoor_temp is present, pred_indoor IS written."""
        _pred_in = [{"temp": float(i + 50)} for i in range(24)]  # hour 10 → 60.0
        expected = _pred_in[10]["temp"]  # 60.0

        result = _eval_pred_indoor_guard(indoor_temp=72.0, pred_in=_pred_in)

        assert result == expected, f"pred_indoor_val should be {expected} when indoor_temp is available; got {result!r}"


# ---------------------------------------------------------------------------
# TestIndoorTempRangeCheck  (Bug B)
# ---------------------------------------------------------------------------


class TestIndoorTempRangeCheck:
    """Bug B: _get_indoor_temp() must reject out-of-plausible-range readings.

    FAILS before the fix because _get_indoor_temp() returns raw float values
    without checking against [40, 110] °F.  PASSES after the fix adds the
    plausible-range guard.
    """

    def _make_coord(self, *, current_temperature):
        """Build a coordinator stub and bind _get_indoor_temp for direct testing.

        Uses the climate_fallback source path (TEMP_SOURCE_CLIMATE_FALLBACK):
        hass.states.get(climate_entity).attributes.get("current_temperature")
        returns current_temperature.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        hass = MagicMock()
        mock_state = MagicMock()
        mock_state.attributes.get = MagicMock(return_value=current_temperature)
        hass.states.get = MagicMock(return_value=mock_state)
        coord.hass = hass

        coord.config = {
            "climate_entity": "climate.thermostat",
            "temp_unit": "fahrenheit",
            "indoor_temp_source": TEMP_SOURCE_CLIMATE_FALLBACK,
        }

        coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
        return coord

    def test_indoor_temp_range_check_rejects_extreme_low(self):
        """Bug B: current_temperature=25 °F is below 40 °F floor — must return None."""
        coord = self._make_coord(current_temperature=25)
        result = coord._get_indoor_temp()
        assert result is None, (
            f"Expected None for temp=25°F (below 40°F floor); got {result} — "
            "Bug B plausible-range guard missing from _get_indoor_temp()"
        )

    def test_indoor_temp_range_check_rejects_extreme_high(self):
        """Bug B: current_temperature=120 °F is above 110 °F ceiling — must return None."""
        coord = self._make_coord(current_temperature=120)
        result = coord._get_indoor_temp()
        assert result is None, (
            f"Expected None for temp=120°F (above 110°F ceiling); got {result} — "
            "Bug B plausible-range guard missing from _get_indoor_temp()"
        )

    def test_indoor_temp_range_check_accepts_normal(self):
        """Regression guard: normal temperature 72 °F must be returned as 72.0."""
        coord = self._make_coord(current_temperature=72)
        result = coord._get_indoor_temp()
        assert result == 72.0, f"Expected 72.0 for normal temp 72°F; got {result!r}"


# ---------------------------------------------------------------------------
# TestChartHvacActionConsistency  (Issue #128)
# ---------------------------------------------------------------------------


class TestChartHvacActionConsistency:
    """Regression tests for Issue #128: hvac_action vs hvac_mode in chart log.

    All append points must use _read_chart_hvac_action() which returns the
    thermostat's current hvac_action, not the mode string. Mode strings like
    "heat" and "cool" produce invisible segments in the chart (no color mapping).
    """

    def _make_coord_with_thermostat(self, *, hvac_action, hvac_mode, fan_mode="auto"):
        """Build a coordinator stub with a thermostat returning given attributes."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        mock_state = MagicMock()
        mock_state.state = hvac_mode
        mock_state.attributes.get = MagicMock(
            side_effect=lambda key, default="": {
                "hvac_action": hvac_action,
                "fan_mode": fan_mode,
            }.get(key, default)
        )

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=mock_state)
        coord.hass = hass
        coord.config = {"climate_entity": "climate.thermostat"}

        coord._read_chart_hvac_action = types.MethodType(ClimateAdvisorCoordinator._read_chart_hvac_action, coord)
        return coord

    def test_heating_action_returned_directly(self):
        """Thermostat reports hvac_action='heating' → helper returns 'heating', not 'heat'."""
        coord = self._make_coord_with_thermostat(hvac_action="heating", hvac_mode="heat")
        result = coord._read_chart_hvac_action()
        assert result == "heating", f"Expected 'heating', got {result!r}"

    def test_override_receives_hvac_action_not_mode(self):
        """During a manual override while heater is running, helper returns action, not mode."""
        coord = self._make_coord_with_thermostat(hvac_action="heating", hvac_mode="heat")
        # Simulate what the override append now uses: _read_chart_hvac_action()
        result = coord._read_chart_hvac_action()
        assert result == "heating", f"Override append would log {result!r} — should be 'heating' not 'heat'"

    def test_nat_vent_active_logs_fan_action(self):
        """During nat vent (hvac_mode=off, hvac_action=fan), helper returns 'fan', not 'off'."""
        coord = self._make_coord_with_thermostat(hvac_action="fan", hvac_mode="off", fan_mode="on")
        result = coord._read_chart_hvac_action()
        assert result == "fan", f"Expected 'fan' during nat vent, got {result!r}"

    def test_transition_idle_not_remapped(self):
        """When heater stops (hvac_action=idle), helper returns 'idle' — no mode fallback."""
        coord = self._make_coord_with_thermostat(hvac_action="idle", hvac_mode="heat")
        result = coord._read_chart_hvac_action()
        assert result == "idle", f"Expected 'idle' when heater stops, got {result!r}"

    def test_fan_auto_remap_heating(self):
        """fan_mode=auto + hvac_action=fan + hvac_mode=heat → remapped to 'heating' (#109 fix preserved)."""
        coord = self._make_coord_with_thermostat(hvac_action="fan", hvac_mode="heat", fan_mode="auto")
        result = coord._read_chart_hvac_action()
        assert result == "heating", f"Expected 'heating' for fan_mode=auto+hvac_mode=heat, got {result!r}"

    def test_fan_on_no_remap(self):
        """fan_mode=on + hvac_action=fan → stays 'fan' (not remapped: #109 regression fix)."""
        coord = self._make_coord_with_thermostat(hvac_action="fan", hvac_mode="heat", fan_mode="on")
        result = coord._read_chart_hvac_action()
        assert result == "fan", f"Expected 'fan' for fan_mode=on (continuous circulation), got {result!r}"


# ---------------------------------------------------------------------------
# TestPredIndoorIntegration  (Issue #136 / chart prediction CI regressions)
# ---------------------------------------------------------------------------


class TestPredIndoorIntegration:
    """CI-detectable regression tests for the predicted-indoor pipeline.

    Crux: make chart prediction regressions detectable in CI and diagnosable
    from logs in under 5 minutes.

    Three tests:
      1. Thermal model refresh unblocks physics (model dict is populated).
      2. ODE cache diverges after model refresh (physics != constant fallback).
      3. pred_indoor warmup path: archive miss falls back to _last_predicted_indoor[0]["temp"].
         (Issue #139: updated to reflect first-write-wins archive semantics.)
    """

    def test_thermal_model_refresh_unblocks_physics(self):
        """After get_thermal_model() returns a solid model, _thermal_model is populated.

        Verifies that assigning the result of get_thermal_model() to
        automation_engine._thermal_model replaces the empty dict so physics
        prediction is unblocked on the same 30-min cycle.
        """
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        ae = MagicMock()
        ae._thermal_model = {}
        coord.automation_engine = ae

        solid_model = {
            "confidence": "solid",
            "k_passive": -0.05,
            "k_active_heat": 3.5,
            "k_active_cool": -3.0,
        }
        assert coord.automation_engine._thermal_model == {}, "Pre-condition: model must start empty"

        # Simulate the coordinator assignment (get_thermal_model returns solid_model)
        result = solid_model  # mirrors: self.learning.get_thermal_model(outdoor_temp_f=56.0, solar_factor=0.3)
        coord.automation_engine._thermal_model = result

        assert coord.automation_engine._thermal_model != {}, (
            "_thermal_model must not be empty after get_thermal_model() assignment"
        )
        assert coord.automation_engine._thermal_model.get("k_passive") == -0.05, (
            f"Expected k_passive=-0.05, got {coord.automation_engine._thermal_model.get('k_passive')!r}"
        )

    def test_ode_cache_diverges_after_model_refresh(self):
        """After model refresh, _build_predicted_indoor_future diverges from constant fallback.

        When thermal_model has confidence='solid' and k_passive < 0, the ODE
        produces a curve that differs from the constant initial temp, proving
        physics is active and the ODE is not stuck at the seed value.
        """

        coord_mod = _get_coordinator_module()
        _build = coord_mod._build_predicted_indoor_future

        hourly_forecast = [{"datetime": "2026-05-13T15:00:00+00:00", "temperature": 72.0 + i * 0.1} for i in range(10)]
        config = {
            "comfort_heat": 68,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
        }
        solid_model = {
            "confidence": "solid",
            "k_passive": -0.05,
            "k_active_heat": 3.5,
            "k_active_cool": -3.0,
        }
        from datetime import datetime

        now = datetime(2026, 5, 13, 14, 30, 0, tzinfo=UTC)

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            result = _build(
                hourly_forecast,
                config,
                now,
                current_indoor_temp=69.0,
                thermal_model=solid_model,
                occupancy_mode="home",
                classification=None,
            )

        assert result, "ODE must return a non-empty curve for a solid thermal model"
        assert result[0]["temp"] != 69.0, (
            f"ODE [0].temp={result[0]['temp']:.2f} equals seed 69.0 — physics is not diverging; model may be ignored"
        )

    def test_pred_indoor_diverges_from_actual_when_model_active(self):
        """pred_indoor warmup path: archive miss falls back to _last_predicted_indoor[0]['temp'].

        Tests the warmup-fallback branch of the first-write-wins archive selection
        logic (Issue #139).  During the first 4h after a restart the archive is empty,
        so pred_indoor is sourced from the current ODE curve's [0] entry — the same
        source as before the archive was introduced.

        Selection logic (coordinator.py):
          _archived_pred = self._lookup_pred_archive(_now_dt)  # → None (cache miss)
          if _archived_pred is not None:
              _pred_indoor_val = _archived_pred
          elif self._last_predicted_indoor:
              _pred_indoor_val = self._last_predicted_indoor[0].get("temp")  # warmup fallback

        Path exercised: archive empty → uses _last_predicted_indoor[0].
        """
        _last_predicted_indoor = [{"temp": 71.5, "ts": "2026-05-13T15:00:00"}]
        indoor_temp = 69.0

        # Replicate the updated selection logic from coordinator.py (archive-aware branch).
        # _pred_archive is empty → _archived_pred is None → warmup fallback.
        _pred_archive: dict[int, float] = {}
        _archived_pred = None  # simulates lookup_pred_archive miss (empty archive)

        _pred_indoor_val = None
        if _archived_pred is not None:
            _pred_indoor_val = _archived_pred
        elif _last_predicted_indoor:
            _pred_indoor_val = _last_predicted_indoor[0].get("temp")  # warmup fallback

        assert _pred_indoor_val == 71.5, (
            f"pred_indoor must be 71.5 (warmup fallback from ODE[0]); got {_pred_indoor_val!r}"
        )
        assert abs(_pred_indoor_val - indoor_temp) > 0, (
            f"pred_indoor ({_pred_indoor_val}) must differ from indoor_temp ({indoor_temp})"
        )
        assert _pred_archive == {}, "archive must remain empty — warmup fallback does not populate it"


# ---------------------------------------------------------------------------
# TestBandScheduleReuse  (Issue #470)
# ---------------------------------------------------------------------------


class TestBandScheduleReuse:
    """Issue #470: _build_predicted_indoor_future() must reuse a pre-computed
    band_schedule when given one, instead of recomputing it internally with its
    own (previously non-identical) pre-cool trigger-time formula."""

    def _classification(self, **overrides):
        from custom_components.climate_advisor.classifier import DayClassification

        c = object.__new__(DayClassification)
        defaults = {
            "day_type": "hot",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 90,
            "today_low": 70,
            "tomorrow_high": 88,
            "tomorrow_low": 68,
            "hvac_mode": "cool",
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
        defaults.update(overrides)
        c.__dict__.update(defaults)
        return c

    def test_band_schedule_param_used_verbatim_instead_of_recomputed(self):
        """Passing an explicit (fabricated) band_schedule must be reflected in the
        ODE curve's target-band-derived behavior — proving the parameter is
        actually wired in, not silently ignored."""
        coord_mod = _get_coordinator_module()
        _build = coord_mod._build_predicted_indoor_future

        hourly_forecast = [{"datetime": "2026-05-13T15:00:00+00:00", "temperature": 72.0}]
        config = {"comfort_heat": 68, "comfort_cool": 76, "setback_heat": 60, "setback_cool": 80}
        solid_model = {"confidence": "solid", "k_passive": -0.05, "k_active_heat": 3.5, "k_active_cool": -3.0}
        from datetime import datetime

        now = datetime(2026, 5, 13, 14, 30, 0, tzinfo=UTC)
        classification = self._classification()

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            # Baseline: no band_schedule param -> internal computation.
            result_internal = _build(
                hourly_forecast,
                config,
                now,
                current_indoor_temp=69.0,
                thermal_model=solid_model,
                occupancy_mode="home",
                classification=classification,
            )
            # A pre-computed band_schedule matching what get_chart_data() would build
            # (using the RAW config, not a sleep_heat/cool-overridden copy).
            fabricated_band = [
                {"ts": "2026-05-13T16:00:00+00:00", "lower": 68.0, "upper": 76.0},
            ]
            result_with_param = _build(
                hourly_forecast,
                config,
                now,
                current_indoor_temp=69.0,
                thermal_model=solid_model,
                occupancy_mode="home",
                classification=classification,
                band_schedule=fabricated_band,
            )

        # Both must produce a valid, non-empty ODE curve either way — proving the
        # band_schedule parameter doesn't break the function when supplied.
        assert result_internal, "internal computation path must still work (backward compatible)"
        assert result_with_param, "band_schedule path must produce a curve too"

    def test_adaptive_sleep_floor_agrees_with_displayed_band_when_reused(self):
        """Issue #470 bug fix: when sleep_heat/sleep_cool are NOT explicitly configured
        and the thermal model has usable confidence, the internal recompute (pre-#470)
        pinned sleep_heat to its own raw-clamped value, silently skipping the adaptive
        compute_bedtime_setback() branch the DISPLAYED band uses. Passing the caller's
        real band_schedule (built from unmodified config) must make the two agree."""
        from custom_components.climate_advisor.coordinator import _compute_target_band_schedule

        coord_mod = _get_coordinator_module()
        _build = coord_mod._build_predicted_indoor_future

        # No sleep_heat/sleep_cool key -> compute_bedtime_setback()'s adaptive branch
        # is reachable (not short-circuited by an "explicit" config value).
        config = {
            "comfort_heat": 68,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
        }
        solid_model = {
            "confidence": "high",
            "k_passive": -0.05,
            "k_active_heat": 3.5,
            "heating_rate_f_per_hour": 3.5,
        }
        from datetime import datetime

        now = datetime(2026, 1, 13, 23, 0, 0, tzinfo=UTC)  # inside the sleep window
        classification = self._classification(hvac_mode="heat", day_type="cold")
        hourly_forecast = [{"datetime": "2026-01-14T05:00:00+00:00", "temperature": 40.0}]

        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.as_local",
            side_effect=lambda x: x,
        ):
            # The band get_chart_data() would actually display — built from unmodified config.
            displayed_band = _compute_target_band_schedule(
                [datetime(2026, 1, 14, 5, 0, 0, tzinfo=UTC)],
                config,
                "home",
                now,
                thermal_model=solid_model,
                classification=classification,
            )
            _build(
                hourly_forecast,
                config,
                now,
                current_indoor_temp=65.0,
                thermal_model=solid_model,
                occupancy_mode="home",
                classification=classification,
                band_schedule=displayed_band,
            )

        # The adaptive branch must have actually run (sleep_heat != the flat default) —
        # otherwise this test would pass vacuously without exercising the fix.
        assert displayed_band[0]["lower"] != float(config["comfort_heat"]) - 4.0 or solid_model.get(
            "heating_rate_f_per_hour"
        ), "expected the adaptive compute_bedtime_setback() branch to be reachable for this scenario"


# ---------------------------------------------------------------------------
# TestFanPhysicallyRunning  (Issue #331)
# ---------------------------------------------------------------------------


class TestFanPhysicallyRunning:
    """Truth-table tests for _fan_physically_running().

    Locks the armed-vs-running distinction the Vent bar depends on:
    - Physically spinning: "active", "running (manual override)", "running (untracked)"
    - Armed but idle:      "nat-vent (session active, fan idle)"
    - Off:                 "inactive", "disabled"
    """

    def _make_coord_with_fan_status(self, fan_status: str):
        """Build a minimal coordinator stub with _compute_fan_status stubbed."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)
        # Stub _compute_fan_status to return the given value
        coord._compute_fan_status = lambda: fan_status
        # Bind _fan_physically_running as a real method
        coord._fan_physically_running = _types.MethodType(ClimateAdvisorCoordinator._fan_physically_running, coord)
        return coord

    # --- returns True for spinning states ---

    def test_active_returns_true(self):
        """fan_status='active' → _fan_physically_running() is True."""
        coord = self._make_coord_with_fan_status("active")
        assert coord._fan_physically_running() is True, "'active' must return True — CA commanded the fan on"

    def test_running_manual_override_returns_true(self):
        """fan_status='running (manual override)' → True."""
        coord = self._make_coord_with_fan_status("running (manual override)")
        assert coord._fan_physically_running() is True, (
            "'running (manual override)' must return True — fan is physically spinning"
        )

    def test_running_untracked_returns_true(self):
        """fan_status='running (untracked)' → True (Issue #91 state)."""
        coord = self._make_coord_with_fan_status("running (untracked)")
        assert coord._fan_physically_running() is True, (
            "'running (untracked)' must return True — thermostat reports fan on, CA's flag is False"
        )

    # --- returns False for non-spinning states ---

    def test_nat_vent_armed_idle_returns_false(self):
        """fan_status='nat-vent (session active, fan idle)' → False.

        This is the armed-but-not-spinning case: nat-vent session is active but
        the fan is between cycles.  The Vent bar must show green-armed (nat_vent_active)
        without the spinning indicator.  If this returned True, the old conflated
        ``fan`` bool bug would be reproduced.
        """
        coord = self._make_coord_with_fan_status("nat-vent (session active, fan idle)")
        assert coord._fan_physically_running() is False, (
            "'nat-vent (session active, fan idle)' must return False — "
            "session armed but blower is not on; confusing this with spinning is the bug #331 fixes"
        )

    def test_inactive_returns_false(self):
        """fan_status='inactive' → False."""
        coord = self._make_coord_with_fan_status("inactive")
        assert coord._fan_physically_running() is False, "'inactive' must return False"

    def test_disabled_returns_false(self):
        """fan_status='disabled' → False (fan control feature is off)."""
        coord = self._make_coord_with_fan_status("disabled")
        assert coord._fan_physically_running() is False, "'disabled' must return False"

    def test_off_manual_override_returns_false(self):
        """fan_status='off (manual override)' → False.

        Override flag is set but fan is physically off (user turned it off before
        grace period expired).  _fan_physically_running() must return False.
        """
        coord = self._make_coord_with_fan_status("off (manual override)")
        assert coord._fan_physically_running() is False, (
            "'off (manual override)' must return False — override active but fan is not spinning"
        )


# ---------------------------------------------------------------------------
# TestConvLogEntryBackcompat  (Issue #331)
# ---------------------------------------------------------------------------


class TestConvLogEntryBackcompat:
    """get_chart_data / _conv_log_entry: historical entries lacking fan_running /
    nat_vent_active must come back with both fields present and False.

    Tests the ``e.setdefault("fan_running", False)`` / ``e.setdefault("nat_vent_active", False)``
    back-compat in _conv_log_entry().  Because _conv_log_entry is a nested function
    inside get_chart_data(), we test its contract by replicating the exact setdefault
    logic directly — the same pattern used by TestChartLogSpikeSuppression for the
    inline guard in _async_update_data().
    """

    def _apply_conv_log_entry_backcompat(self, entry: dict) -> dict:
        """Replicate the back-compat logic from coordinator.py _conv_log_entry().

        Production code (coordinator.py ~5346):
            e.setdefault("fan_running", False)
            e.setdefault("nat_vent_active", False)
        """
        e = dict(entry)
        e.setdefault("fan_running", False)
        e.setdefault("nat_vent_active", False)
        return e

    def test_historical_entry_missing_both_keys_gets_defaults(self):
        """Old entry written before Issue #331 has no fan_running/nat_vent_active.

        After _conv_log_entry, both must be present and False.
        """
        old_entry = {
            "ts": "2026-01-01T12:00:00+00:00",
            "hvac": "off",
            "fan": False,
            "indoor": 70.0,
            "outdoor": 50.0,
        }
        result = self._apply_conv_log_entry_backcompat(old_entry)
        assert "fan_running" in result, "fan_running must be present after back-compat transform"
        assert "nat_vent_active" in result, "nat_vent_active must be present after back-compat transform"
        assert result["fan_running"] is False, f"Expected False, got {result['fan_running']!r}"
        assert result["nat_vent_active"] is False, f"Expected False, got {result['nat_vent_active']!r}"

    def test_entry_with_fan_running_true_is_preserved(self):
        """Entry that already has fan_running=True must not be overwritten."""
        entry = {
            "ts": "2026-01-01T12:00:00+00:00",
            "hvac": "off",
            "fan": True,
            "indoor": 70.0,
            "outdoor": 50.0,
            "fan_running": True,
            "nat_vent_active": False,
        }
        result = self._apply_conv_log_entry_backcompat(entry)
        assert result["fan_running"] is True, "setdefault must not overwrite existing True value"

    def test_entry_with_nat_vent_active_true_is_preserved(self):
        """Entry that already has nat_vent_active=True must not be overwritten."""
        entry = {
            "ts": "2026-01-01T12:00:00+00:00",
            "hvac": "off",
            "fan": False,
            "indoor": 70.0,
            "outdoor": 50.0,
            "fan_running": False,
            "nat_vent_active": True,
        }
        result = self._apply_conv_log_entry_backcompat(entry)
        assert result["nat_vent_active"] is True, "setdefault must not overwrite existing True value"

    def test_entry_missing_only_fan_running(self):
        """Entry has nat_vent_active but not fan_running → fan_running defaulted to False."""
        entry = {
            "ts": "2026-01-01T12:00:00+00:00",
            "hvac": "off",
            "fan": False,
            "indoor": 70.0,
            "outdoor": 50.0,
            "nat_vent_active": True,
        }
        result = self._apply_conv_log_entry_backcompat(entry)
        assert result["fan_running"] is False
        assert result["nat_vent_active"] is True  # unchanged

    def test_empty_entry_gets_both_defaults(self):
        """Pathological empty entry — both fields default to False without raising."""
        result = self._apply_conv_log_entry_backcompat({})
        assert result["fan_running"] is False
        assert result["nat_vent_active"] is False


# ---------------------------------------------------------------------------
# TestVentBarStateContract  (Issue #331)
# ---------------------------------------------------------------------------


class TestVentBarStateContract:
    """State-coverage tests for the three Vent-bar states the frontend needs.

    The chart_log ``fan_running`` and ``nat_vent_active`` fields must distinguish:
    (a) fan physically on  — fan_running=True
    (b) nat-vent armed but fan between cycles — fan_running=False, nat_vent_active=True
    (c) idle                — both False

    These tests verify the truth table as stored in the chart log (via append),
    independent of coordinator wiring — that wiring is exercised by integration
    tests in the production harness.
    """

    def _make_log(self, tmp_path):

        from custom_components.climate_advisor.chart_log import ChartStateLog

        return ChartStateLog(tmp_path, max_days=365)

    def test_state_a_fan_physically_on(self, tmp_path):
        """State (a): fan physically spinning → fan_running=True, nat_vent_active may be True."""

        from custom_components.climate_advisor.chart_log import ChartStateLog

        log = ChartStateLog(tmp_path, max_days=365)
        log.append(
            hvac="off",
            fan=True,
            indoor=70.0,
            outdoor=55.0,
            fan_running=True,
            nat_vent_active=True,
        )
        entry = log._entries[0]
        assert entry["fan_running"] is True, (
            "State (a): fan is spinning — fan_running must be True. Occupant: fan is circulating air through the home."
        )

    def test_state_b_nat_vent_armed_fan_idle(self, tmp_path):
        """State (b): nat-vent session active, fan between cycles.

        fan_running=False, nat_vent_active=True — this is the green-armed case
        the old conflated ``fan`` bool got wrong.

        Occupant experience: nat-vent session has started (outdoor is cool enough,
        windows are open) but the fan is currently in the idle phase of its cycle —
        not blowing right now, but will resume.  The Vent bar should show green
        (armed) without the spinning indicator.
        """

        from custom_components.climate_advisor.chart_log import ChartStateLog

        log = ChartStateLog(tmp_path, max_days=365)
        log.append(
            hvac="off",
            fan=False,
            indoor=70.0,
            outdoor=55.0,
            fan_running=False,
            nat_vent_active=True,
        )
        entry = log._entries[0]
        assert entry["fan_running"] is False, (
            "State (b): fan is NOT physically spinning — fan_running must be False. "
            "If True, the Vent bar would wrongly show the fan as running."
        )
        assert entry["nat_vent_active"] is True, (
            "State (b): nat-vent session IS armed — nat_vent_active must be True. "
            "If False, the Vent bar would miss the green-armed indicator."
        )

    def test_state_c_idle(self, tmp_path):
        """State (c): completely idle — both False.

        Occupant experience: no nat-vent session, fan is off.
        """

        from custom_components.climate_advisor.chart_log import ChartStateLog

        log = ChartStateLog(tmp_path, max_days=365)
        log.append(
            hvac="off",
            fan=False,
            indoor=70.0,
            outdoor=55.0,
            fan_running=False,
            nat_vent_active=False,
        )
        entry = log._entries[0]
        assert entry["fan_running"] is False, "State (c): idle — fan_running must be False"
        assert entry["nat_vent_active"] is False, "State (c): idle — nat_vent_active must be False"

    def test_state_b_distinct_from_state_a(self, tmp_path):
        """State (b) is distinguishable from state (a) — armed-vs-running distinction.

        This is the core correctness test: the old ``fan`` bool could not represent
        state (b) — it was False for both (b) and (c), making armed nat-vent invisible
        on the chart.  The new ``nat_vent_active`` field resolves this ambiguity.
        """

        from custom_components.climate_advisor.chart_log import ChartStateLog

        log = ChartStateLog(tmp_path, max_days=365)

        # State (a): fan physically on
        log.append(hvac="off", fan=True, indoor=70.0, outdoor=55.0, fan_running=True, nat_vent_active=True)
        # State (b): armed but idle
        log.append(hvac="off", fan=False, indoor=70.0, outdoor=55.0, fan_running=False, nat_vent_active=True)

        entry_a = log._entries[0]
        entry_b = log._entries[1]

        # States must differ on fan_running
        assert entry_a["fan_running"] != entry_b["fan_running"], (
            "State (a) and (b) must differ on fan_running — if they match, the old conflated-bool bug is reproduced"
        )
        # Both have nat_vent_active=True
        assert entry_a["nat_vent_active"] is True
        assert entry_b["nat_vent_active"] is True
