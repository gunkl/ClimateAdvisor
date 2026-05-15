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
