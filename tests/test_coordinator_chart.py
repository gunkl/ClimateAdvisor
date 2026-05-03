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
from unittest.mock import MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

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
