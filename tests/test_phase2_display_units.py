"""Issue #1048 (Phase 2) — Celsius households see correct numbers: AI cross-check, Target Temperature,
pre-heat amount, sensor attributes, settings API.

Each class invokes the real production code (no mirrored logic).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.climate_advisor.ai_skills_context import build_state_cross_validation_context


def _coord(unit: str, comfort_heat: float = 71.6, comfort_cool: float = 75.2, swing_f: float = 1.5):
    coord = MagicMock()
    coord.data = {"hvac_mode": "heat", "hvac_action": "idle"}
    coord.config = {
        "climate_entity": "climate.thermostat",
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "temp_unit": unit,
    }
    coord.learning = MagicMock()
    coord.learning.get_thermal_model.return_value = {
        "swing_heat_f_display": swing_f,
        "swing_cool_f_display": swing_f,
    }
    coord._build_learning_health = MagicMock(return_value={})
    return coord


def _hass(current_temperature: float):
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"current_temperature": current_temperature}
    hass.states.get.return_value = state
    return hass


class TestAiCrossCheckCelsius:
    def test_real_shortfall_reported_in_celsius(self):
        ctx = asyncio.run(build_state_cross_validation_context(_hass(19.0), _coord("celsius")))
        assert "[FLAG]" in ctx
        assert "19.0\u00b0C" in ctx
        assert "22.0\u00b0C" in ctx
        assert "below by 3.0\u00b0C" in ctx
        assert "71.6" not in ctx
        assert "52.6" not in ctx

    def test_in_band_celsius_reading_is_not_flagged(self):
        """22.5 degC (72.5 degF) is inside 71.6-75.2 degF; the old code compared 22.5 < 71.6 and flagged it."""
        ctx = asyncio.run(build_state_cross_validation_context(_hass(22.5), _coord("celsius")))
        assert "[FLAG]" not in ctx
        assert "[OK]" in ctx

    def test_shortfall_within_swing_not_flagged_celsius(self):
        # 21.5 degC = 70.7 degF: 0.9 degF short, swing 1.5 degF -> within deadband
        ctx = asyncio.run(build_state_cross_validation_context(_hass(21.5), _coord("celsius")))
        assert "[FLAG]" not in ctx

    def test_fahrenheit_flag_text(self):
        ctx = asyncio.run(build_state_cross_validation_context(_hass(65.0), _coord("fahrenheit", 68.0, 74.0)))
        assert "[FLAG] Indoor 65.0\u00b0F < comfort_heat 68.0\u00b0F" in ctx
        assert "below by 3.0\u00b0F" in ctx


# ---------------------------------------------------------------------------
# Change 13 — Target Temperature sensor
# ---------------------------------------------------------------------------

import inspect  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest.mock import patch  # noqa: E402

from custom_components.climate_advisor.const import ATTR_EFFECTIVE_TARGET_TEMP  # noqa: E402
from tests.test_hvac_session_detection import _get_coordinator_class, _make_update_data_coord  # noqa: E402


class TestEffectiveTargetTempUnits:
    def _update(self, unit: str, setpoint: float) -> dict:
        coord = _make_update_data_coord(hvac_mode="cool", hvac_action="cooling")
        coord.config["temp_unit"] = unit
        coord.hass.states.get.return_value.attributes["temperature"] = setpoint
        with patch(
            "custom_components.climate_advisor.coordinator.dt_util.now", return_value=datetime(2026, 4, 8, 21, 0, 0)
        ):
            return asyncio.run(coord._async_update_data())

    def test_celsius_setpoint_is_converted_to_internal_fahrenheit(self):
        """22 degC cooling setpoint -> 71.6 degF (the sensor then converts back for display).

        Before: the raw 22.0 was passed straight through and displayed as -5.6 degC.
        """
        result = self._update("celsius", 22.0)
        assert round(result[ATTR_EFFECTIVE_TARGET_TEMP], 1) == 71.6

    def test_fahrenheit_setpoint_unchanged(self):
        result = self._update("fahrenheit", 72.0)
        assert result[ATTR_EFFECTIVE_TARGET_TEMP] == 72.0

    def test_update_data_never_feeds_a_raw_attribute_into_the_effective_target(self):
        src = inspect.getsource(_get_coordinator_class()._async_update_data_impl)
        assert "target_temp=_target_temp_f" in src
        assert "target_temp=_target_temp," not in src


# ---------------------------------------------------------------------------
# Change 15 — Debug tab pre-heat amount is a delta, not an absolute temperature
# ---------------------------------------------------------------------------


class TestPreConditionDelta:
    def _debug(self, unit: str, delta_f: float) -> dict:
        from custom_components.climate_advisor.classifier import DayClassification

        fake = MagicMock()
        fake.config = {"temp_unit": unit}
        fake._resolved_sensors = []
        fake._startup_coalesce_active = False
        fake.automation_engine._decision_lock_held_since = None
        fake.automation_engine._manual_override_active = False
        fake._current_classification = DayClassification(
            day_type="cold",
            trend_direction="stable",
            trend_magnitude=0.0,
            today_high=40.0,
            today_low=30.0,
            tomorrow_high=40.0,
            tomorrow_low=30.0,
            pre_condition=True,
            pre_condition_target=delta_f,
        )
        return _get_coordinator_class().get_debug_state(fake)["classification"]

    def test_celsius_pre_heat_amount_is_scaled_not_offset(self):
        """3 degF of pre-heat is 1.7 degC; the absolute conversion gave -16.1."""
        assert self._debug("celsius", 3.0)["pre_condition_target"] == 1.7

    def test_fahrenheit_unchanged(self):
        assert self._debug("fahrenheit", 3.0)["pre_condition_target"] == 3.0


# ---------------------------------------------------------------------------
# Change 16 — sensor attributes in the user's unit
# ---------------------------------------------------------------------------


def _sensor_coordinator(unit: str, data: dict | None = None):
    coord = MagicMock()
    coord.data = data or {}
    coord.config = {"temp_unit": unit, "comfort_heat": 71.6, "comfort_cool": 75.2}
    coord.today_record = None
    coord.learning.get_thermal_model.return_value = {}
    coord._build_learning_health.return_value = {}
    return coord


class TestSensorAttributeUnits:
    def _compliance_attrs(self, unit: str) -> dict:
        from custom_components.climate_advisor.sensor import ClimateAdvisorComplianceSensor

        entry = MagicMock()
        entry.entry_id = "e"
        return ClimateAdvisorComplianceSensor(_sensor_coordinator(unit), entry).extra_state_attributes

    def test_comfort_range_in_celsius(self):
        attrs = self._compliance_attrs("celsius")
        assert attrs["comfort_range_low"] == 22.0
        assert attrs["comfort_range_high"] == 24.0

    def test_comfort_range_in_fahrenheit(self):
        attrs = self._compliance_attrs("fahrenheit")
        assert attrs["comfort_range_low"] == 71.6
        assert attrs["comfort_range_high"] == 75.2

    def test_trend_magnitude_is_a_scaled_delta(self):
        from custom_components.climate_advisor.const import ATTR_TREND_MAGNITUDE
        from custom_components.climate_advisor.sensor import ClimateAdvisorDayTypeSensor

        entry = MagicMock()
        entry.entry_id = "e"
        sensor = ClimateAdvisorDayTypeSensor(_sensor_coordinator("celsius", {ATTR_TREND_MAGNITUDE: 9.0}), entry)
        assert sensor.extra_state_attributes["trend_magnitude"] == 5.0


class TestConfigViewTemperatureUnits:
    """Settings tab: absolute temperatures in the user's unit (with `unit` in the response);
    the six tolerance deltas too (scale-only conversion, Issue #1063)."""

    ABS_KEYS = (
        "comfort_heat",
        "comfort_cool",
        "setback_heat",
        "setback_cool",
        "sleep_heat",
        "sleep_cool",
        "vacation_heat",  # Issue #1347
        "vacation_cool",  # Issue #1347
        "threshold_hot",
        "threshold_warm",
        "threshold_mild",
        "threshold_cool",
    )
    DELTA_KEYS = (
        "max_setback_depth_f",
        "comfort_deadband_hot_f",
        "comfort_deadband_warm_f",
        "comfort_deadband_mild_f",
        "comfort_deadband_cool_f",
        "comfort_deadband_cold_f",
    )

    def _settings(self, config: dict) -> dict[str, dict]:
        from custom_components.climate_advisor.api import ClimateAdvisorConfigView
        from tests.test_api import _make_view_request

        coord = MagicMock()
        coord.config = config
        coord._outdoor_sensor_fallback_reason = None
        resp = asyncio.run(ClimateAdvisorConfigView().get(_make_view_request(coord)))
        return {s["key"]: s for s in resp.json_data["settings"]}

    def test_metadata_declares_the_transform_on_exactly_the_declared_keys(self):
        from custom_components.climate_advisor.const import CONFIG_METADATA

        abs_keys = {k for k, m in CONFIG_METADATA.items() if m.get("display_transform") == "temp_abs"}
        delta_keys = {k for k, m in CONFIG_METADATA.items() if m.get("display_transform") == "temp_delta"}
        assert abs_keys == set(self.ABS_KEYS)
        assert delta_keys == set(self.DELTA_KEYS)
        assert all("unit" not in m for m in CONFIG_METADATA.values())

    def test_celsius_absolute_settings_are_converted_with_unit(self):
        s = self._settings({"temp_unit": "celsius", "comfort_heat": 71.6, "comfort_cool": 75.2, "threshold_hot": 84.2})
        assert (s["comfort_heat"]["value"], s["comfort_heat"]["unit"]) == (22.0, "\u00b0C")
        assert s["comfort_cool"]["value"] == 24.0
        assert (s["threshold_hot"]["value"], s["threshold_hot"]["unit"]) == (29.0, "\u00b0C")

    def test_fahrenheit_absolute_settings_unchanged_with_unit(self):
        s = self._settings({"comfort_heat": 71.6})
        assert (s["comfort_heat"]["value"], s["comfort_heat"]["unit"]) == (71.6, "\u00b0F")

    def test_delta_settings_follow_the_users_unit(self):
        """Issue #1063 (supersedes the #1048 'deltas stay degF' rule): scale-only conversion, no +32."""
        cases = {"celsius": (1.1, 4.4, "\u00b0C"), "fahrenheit": (2.0, 8, "\u00b0F")}
        for unit, (deadband, depth, symbol) in cases.items():
            s = self._settings({"temp_unit": unit, "comfort_deadband_hot_f": 2.0, "max_setback_depth_f": 8})
            assert (s["comfort_deadband_hot_f"]["value"], s["comfort_deadband_hot_f"]["unit"]) == (deadband, symbol)
            assert s["max_setback_depth_f"]["value"] == depth

    def test_non_temperature_settings_carry_no_unit(self):
        s = self._settings({"temp_unit": "celsius"})
        assert "unit" not in s["wake_time"]

    def test_tolerance_labels_no_longer_embed_the_unit(self):
        s = self._settings({})
        for key in self.DELTA_KEYS:
            assert "\u00b0F" not in s[key]["label"]


# ---------------------------------------------------------------------------
# Change 17 (non-automation half) — wording in the user's unit
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402


class TestWordingUnits:
    def test_pre_cool_status_text_celsius(self):
        fake = MagicMock()
        fake.config = {"temp_unit": "celsius"}
        fake._pre_cool_trigger_scheduled = False
        fake._compute_pre_cool_trigger_time.return_value = _dt.datetime(2026, 7, 1, 23, 0)
        with (
            patch(
                "custom_components.climate_advisor.coordinator.dt_util.now", return_value=_dt.datetime(2026, 7, 1, 20)
            ),
            patch("custom_components.climate_advisor.coordinator.compute_pre_cool_target", return_value=71.6),
            patch("custom_components.climate_advisor.coordinator.resolve_pre_cool_modifier", return_value=0.0),
            patch("custom_components.climate_advisor.coordinator.async_track_point_in_time"),
        ):
            _get_coordinator_class()._maybe_schedule_pre_cool(fake)
        assert fake._pre_cool_status == "Pre-cool scheduled (22\u00b0C @ 11:00 PM)"

    def test_config_context_celsius(self):
        from custom_components.climate_advisor.ai_skills_context import build_config_context

        coord = MagicMock()
        coord.config = {"temp_unit": "celsius", "comfort_heat": 71.6, "comfort_cool": 75.2, "setback_heat": 64.4}
        ctx = asyncio.run(build_config_context(None, coord))
        assert "comfort_heat (lower bound): 22 " in ctx
        assert "comfort_band: [22, 24]\u00b0C" in ctx
        assert "setback_heat:    18" in ctx
        assert "71.6" not in ctx

    def test_config_context_fahrenheit_unchanged(self):
        from custom_components.climate_advisor.ai_skills_context import build_config_context

        coord = MagicMock()
        coord.config = {"comfort_heat": 68, "comfort_cool": 75}
        ctx = asyncio.run(build_config_context(None, coord))
        assert "comfort_heat (lower bound): 68 " in ctx
        assert "comfort_band: [68, 75]\u00b0F" in ctx

    def test_daily_summaries_celsius(self):
        from custom_components.climate_advisor.ai_skills_context import _build_daily_summaries

        coord = MagicMock()
        coord.config = {"temp_unit": "celsius"}
        coord.learning._state.records = [
            {
                "date": (_dt.date.today() - _dt.timedelta(days=1)).isoformat(),
                "day_type": "warm",
                "avg_indoor_temp": 71.6,
                "observed_high_f": 77.0,
                "observed_low_f": 59.0,
            }
        ]
        text = "\n".join(_build_daily_summaries(coord, 72))
        assert "22.0\u00b0C" in text
        assert "25\u00b0C/15\u00b0C" in text

    def test_classification_margin_and_deadband_celsius(self):
        from custom_components.climate_advisor.ai_skills_context import (
            _render_classification_applied,
            _render_comfort_family_switch,
        )

        _, settings = _render_classification_applied(
            {
                "day_type": "warm",
                "today_high": 77.0,
                "applied_threshold_f": 75.0,
                "threshold_margin_f": 1.8,
                "trend_magnitude": 0,
            },
            "celsius",
        )
        assert "(+1.0\u00b0C)" in settings
        _, detail = _render_comfort_family_switch({"deadband_applied_f": 1.8, "reason": "r"}, "celsius")
        assert "deadband applied: 1.0\u00b0C" in detail

    def test_classification_margin_fahrenheit_unchanged(self):
        from custom_components.climate_advisor.ai_skills_context import _render_classification_applied

        _, settings = _render_classification_applied(
            {"day_type": "warm", "today_high": 77.0, "applied_threshold_f": 75.0, "threshold_margin_f": -1.5},
            "fahrenheit",
        )
        assert "(-1.5\u00b0F)" in settings


class TestInvestigatorFallbackSetpointUnit:
    def _fallback(self, unit: str) -> str:
        from custom_components.climate_advisor.ai_skills_investigator import investigation_fallback
        from tests.test_ai_investigator import _make_coordinator, _make_fallback_event

        events = [
            _make_fallback_event("setpoint_rejected", hours_ago=2.0, commanded=71.6),
            _make_fallback_event("setpoint_rejected", hours_ago=1.0, commanded=71.6),
        ]
        coord = _make_coordinator(event_log=events)
        coord.config = {**(coord.config or {}), "temp_unit": unit}
        return investigation_fallback(coord)["data_quality"]

    def test_celsius(self):
        assert "Setpoint 22\u00b0C rejected 2 times" in self._fallback("celsius")

    def test_fahrenheit_unchanged(self):
        assert "Setpoint 71.6\u00b0F rejected 2 times" in self._fallback("fahrenheit")
