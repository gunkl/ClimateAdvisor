"""Tests for build_state_cross_validation_context() and build_override_details_context()
(moved from ai_skills_activity.py's async_build_activity_context, Issue #563).

These are two of the few genuinely unique, non-redundant pieces of the old activity
context builder — most of the rest (classification, automation state, config, etc.)
duplicated what the investigator's existing context providers already compute.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.ai_skills_context import (
    build_override_details_context,
    build_state_cross_validation_context,
    get_provider_registry,
)

_NOW = datetime.datetime(2026, 7, 10, 14, 0, 0, tzinfo=datetime.UTC)


def _make_coordinator(data=None, config=None, learning=None, automation_engine=None, today_record=None):
    coord = MagicMock()
    coord.data = data or {}
    coord.config = config or {"climate_entity": "climate.thermostat", "comfort_heat": 68.0, "comfort_cool": 74.0}
    coord.learning = learning
    coord.automation_engine = automation_engine
    coord._today_record = today_record
    coord._event_log = []
    coord._build_learning_health = MagicMock(return_value={})
    return coord


def _make_hass(current_temperature=71.0):
    hass = MagicMock()
    climate_state = MagicMock()
    climate_state.attributes = {"current_temperature": current_temperature}
    hass.states.get.return_value = climate_state
    return hass


class TestBuildStateCrossValidationContext:
    def test_within_comfort_band_reports_ok(self):
        coord = _make_coordinator(data={"hvac_mode": "heat", "hvac_action": "idle"})
        hass = _make_hass(current_temperature=71.0)
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[OK]" in ctx
        assert "within comfort band" in ctx

    def test_below_comfort_band_flags(self):
        coord = _make_coordinator(data={"hvac_mode": "heat", "hvac_action": "idle"})
        hass = _make_hass(current_temperature=60.0)
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[FLAG]" in ctx

    def test_shortfall_within_learned_swing_is_not_flagged(self):
        """A 0.5°F shortfall must not flag when the learned thermal swing is
        wider than the gap — regression guard for the deadband/swing logic
        (moved here from the old ai_skills_activity.py mirror-test, which
        re-implemented this arithmetic instead of calling the real function)."""
        learning = MagicMock()
        learning.get_thermal_model.return_value = {
            "swing_heat_f_display": 1.5,
            "swing_cool_f_display": 1.5,
        }
        coord = _make_coordinator(
            data={"hvac_mode": "heat", "hvac_action": "idle"},
            config={"climate_entity": "climate.thermostat", "comfort_heat": 68.0, "comfort_cool": 74.0},
            learning=learning,
        )
        hass = _make_hass(current_temperature=67.5)  # gap = 0.5°F < swing = 1.5°F
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[FLAG]" not in ctx
        assert "[OK]" in ctx

    def test_shortfall_beyond_learned_swing_still_flags(self):
        """A gap larger than the learned swing must still flag — the deadband
        suppresses noise, it doesn't hide real violations."""
        learning = MagicMock()
        learning.get_thermal_model.return_value = {
            "swing_heat_f_display": 1.5,
            "swing_cool_f_display": 1.5,
        }
        coord = _make_coordinator(
            data={"hvac_mode": "heat", "hvac_action": "idle"},
            config={"climate_entity": "climate.thermostat", "comfort_heat": 68.0, "comfort_cool": 74.0},
            learning=learning,
        )
        hass = _make_hass(current_temperature=65.0)  # gap = 3.0°F > swing = 1.5°F
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[FLAG]" in ctx
        assert "below" in ctx

    def test_hvac_off_but_action_heating_flags_warning(self):
        coord = _make_coordinator(data={"hvac_mode": "off", "hvac_action": "heating", "fan_status": "inactive"})
        hass = _make_hass(current_temperature=71.0)
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[WARNING]" in ctx
        assert "hvac_mode=off" in ctx

    def test_hvac_off_with_ca_fan_running_is_expected_not_flagged(self):
        coord = _make_coordinator(data={"hvac_mode": "off", "hvac_action": "fan", "fan_status": "active"})
        hass = _make_hass(current_temperature=71.0)
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "[WARNING]" not in ctx

    def test_no_climate_entity_current_temp_unavailable_does_not_crash(self):
        coord = _make_coordinator(data={"hvac_mode": "heat", "hvac_action": "idle"}, config={})
        hass = _make_hass()
        ctx = asyncio.run(build_state_cross_validation_context(hass, coord))
        assert "=== STATE CROSS-VALIDATION ===" in ctx


class TestBuildOverrideDetailsContext:
    def test_no_overrides_shows_count_zero(self):
        coord = _make_coordinator()
        ctx = asyncio.run(build_override_details_context(None, coord))
        assert "Setpoint override count: 0" in ctx
        assert "no setpoint overrides recorded today" in ctx

    def test_no_active_override_reported(self):
        ae = MagicMock()
        ae._manual_override_active = False
        coord = _make_coordinator(automation_engine=ae)
        ctx = asyncio.run(build_override_details_context(None, coord))
        assert "Current override:  none active" in ctx

    def test_stuck_grace_detected_when_expired_and_not_active(self):
        ae = MagicMock()
        ae._manual_override_active = True
        ae._grace_active = False
        ae._grace_end_time = (_NOW - datetime.timedelta(minutes=5)).isoformat()
        ae._manual_override_time = None
        coord = _make_coordinator(automation_engine=ae)
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_override_details_context(None, coord))
        assert "STUCK GRACE DETECTED" in ctx

    def test_no_stuck_grace_when_grace_still_active(self):
        ae = MagicMock()
        ae._manual_override_active = True
        ae._grace_active = True
        ae._grace_end_time = (_NOW + datetime.timedelta(minutes=5)).isoformat()
        ae._manual_override_time = None
        coord = _make_coordinator(automation_engine=ae)
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.now", return_value=_NOW):
            ctx = asyncio.run(build_override_details_context(None, coord))
        assert "STUCK GRACE DETECTED" not in ctx

    def test_fan_ownership_section_present(self):
        coord = _make_coordinator()
        ctx = asyncio.run(build_override_details_context(None, coord))
        assert "=== FAN OWNERSHIP HISTORY ===" in ctx
        assert "no fan ownership transitions" in ctx

    def test_fan_ownership_transition_recorded(self):
        # The fan-ownership cutoff (unlike the rest of this function) uses real
        # datetime.datetime.now() directly, matching the original source it was
        # moved from — so the fixture event needs a real-ish recent timestamp,
        # not the fixed _NOW used elsewhere in this test file.
        recent = datetime.datetime.now(datetime.UTC)
        event_log = [{"type": "fan_activated", "time": recent}]
        coord = _make_coordinator()
        coord._event_log = event_log
        with patch("custom_components.climate_advisor.ai_skills_context.dt_util.as_local", side_effect=lambda x: x):
            ctx = asyncio.run(build_override_details_context(None, coord, hours=24))
        assert "CA owns fan" in ctx


class TestProviderRegistration:
    def test_both_registered_in_provider_registry(self):
        registry = get_provider_registry()
        names = [p.name for p in registry.select()]
        assert "state_cross_validation" in names
        assert "override_details" in names
