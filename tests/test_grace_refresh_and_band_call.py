"""Tests for Fix 1 (grace-expiry refresh callback) and single service call in
_apply_comfort_band (Issue #301: single-setpoint only).

Fix 1 — Issue #290: When a grace period expires, the engine must call
_request_refresh_callback so the coordinator immediately pushes updated
sensor state to HA. Without this, the occupant sees stale sensor values
until the next 30-min poll.

Issue #301: _apply_comfort_band always uses _set_temperature() — a single
climate.set_temperature call with hvac_mode embedded in the payload.
The former dual-setpoint (heat_cool) path is removed; even heat_cool-capable
thermostats receive a single-mode setpoint call (cool or heat).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs — must run before any climate_advisor import ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import (  # noqa: E402
    AutomationEngine,
    ComfortBand,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
)

# ---------------------------------------------------------------------------
# Minimal engine factory — bypasses full HA wiring, sets only what each test
# needs.
# ---------------------------------------------------------------------------


def _minimal_engine() -> AutomationEngine:
    """Return an AutomationEngine with all HA interactions stubbed."""
    hass = MagicMock()
    hass.states.get.return_value = None
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

    engine = object.__new__(AutomationEngine)
    engine.__dict__.update(
        {
            "hass": hass,
            "climate_entity": "climate.thermostat",
            "weather_entity": "weather.home",
            "door_window_sensors": [],
            "notify_service": "notify.test",
            "config": {"temp_unit": "fahrenheit", "comfort_heat": 68, "comfort_cool": 76},
            "sensor_polarity_inverted": False,
            "dry_run": False,
            "_active_listeners": [],
            "_current_classification": None,
            "_paused_by_door": False,
            "_pre_pause_mode": None,
            "_manual_grace_cancel": None,
            "_automation_grace_cancel": None,
            "_grace_active": True,  # grace is active when expiry fires
            "_last_resume_source": "manual",
            "_grace_end_time": None,
            "_grace_duration_seconds": 0,
            "_manual_override_active": True,
            "_manual_override_mode": "cool",
            "_manual_override_source": None,
            "_manual_override_time": None,
            "_last_override_detected_time": None,
            "_sensor_check_callback": None,
            "_emit_event_callback": None,
            "_request_refresh_callback": None,
            "_post_grace_fan_check_callback": None,
            "_revisit_callback": None,
            "_revisit_cancel": None,
            "_fan_active": False,
            "_fan_override_active": False,
            "_fan_override_time": None,
            "_fan_command_pending": False,
            "_fan_on_since": None,
            "_pre_fan_hvac_mode": None,
            "_hvac_command_pending": False,
            "_temp_command_pending": False,
            "_temp_command_time": None,
            "_hvac_command_time": None,
            "_fan_command_time": None,
            "_last_commanded_hvac_mode": None,
            "_last_commanded_hvac_time": None,
            "_natural_vent_active": False,
            "_economizer_active": False,
            "_economizer_phase": "inactive",
            "_last_action_time": None,
            "_last_action_reason": None,
            "_nat_vent_outdoor_exit_time": None,
            "_override_confirm_pending": False,
            "_override_confirm_cancel": None,
            "_override_confirm_time": None,
            "_override_confirm_mode": None,
            "_override_confirm_source": None,
            "_fan_min_runtime_active": False,
            "_fan_min_cycle_cancel": None,
            "_today_record": None,
            "_last_classification_applied": None,
            "_resumed_from_pause": False,
            "_last_welcome_home_notified": None,
            "_thermal_model": {},
            "_hourly_forecast_temps": [],
            "_occupancy_mode": "home",
            "_write_seq": 0,
            "_pending_setpoint_single": None,
            "_pending_setpoint_mode": None,
        }
    )
    return engine


# ---------------------------------------------------------------------------
# FIX 1: Grace-expiry refresh callback
# ---------------------------------------------------------------------------


class TestGraceExpiryTriggersRefreshCallback:
    """Fix 1 — _request_refresh_callback must be called after grace expiry
    clears the override, on all three paths of _on_grace_expired."""

    def test_normal_expiry_calls_refresh_callback(self):
        """Normal path (no open sensors, not in planned window): callback fires."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Confirm no planned-window override and no sensor open
        engine._sensor_check_callback = None  # no sensors
        # _is_within_planned_window_period must return False
        engine._current_classification = None  # no classification → not in window

        engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_planned_window_path_calls_refresh_callback(self):
        """Planned-window path: callback fires after clearing grace."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Make _is_within_planned_window_period return True by patching
        engine._current_classification = MagicMock()
        engine._current_classification.windows_recommended = True

        import unittest.mock as mock

        with mock.patch.object(
            type(engine),
            "_is_within_planned_window_period",
            return_value=True,
        ):
            engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_sensor_still_open_path_calls_refresh_callback(self):
        """Re-pause path (sensor still open): callback fires after clearing grace."""
        engine = _minimal_engine()
        refresh_mock = MagicMock()
        engine._request_refresh_callback = refresh_mock

        # Sensor check returns True → re-pause path
        engine._sensor_check_callback = lambda: True

        # Stub _re_pause_for_open_sensor so hass.async_create_task gets a coroutine
        async def _fake_repause():
            pass

        engine._re_pause_for_open_sensor = _fake_repause  # type: ignore[method-assign]

        import unittest.mock as mock

        with mock.patch.object(
            type(engine),
            "_is_within_planned_window_period",
            return_value=False,
        ):
            engine._on_grace_expired(source="manual", duration=1800, should_notify=False)

        refresh_mock.assert_called_once()

    def test_no_callback_registered_does_not_raise(self):
        """If callback is None (not wired by coordinator), expiry still completes."""
        engine = _minimal_engine()
        engine._request_refresh_callback = None
        engine._sensor_check_callback = None

        # Must not raise
        engine._on_grace_expired(source="manual", duration=1800, should_notify=False)
        assert engine._grace_active is False


# ---------------------------------------------------------------------------
# Issue #301: Single service call in _apply_comfort_band (single-setpoint only)
# ---------------------------------------------------------------------------


class TestApplyComfortBandSingleServiceCall:
    """Issue #301 — _apply_comfort_band always issues ONE climate.set_temperature call.

    The dual-setpoint (heat_cool) path is removed. Even a heat_cool-capable thermostat
    receives a single-mode call: active="ceiling" → hvac_mode="cool" + temperature=ceiling;
    active="floor" → hvac_mode="heat" + temperature=floor.
    No separate set_hvac_mode call and no pre-write offset step.
    """

    def test_heat_cool_thermostat_ceiling_band_issues_one_call_with_cool_mode(self):
        """heat_cool-capable thermostat + active=ceiling → ONE set_temperature call, hvac_mode='cool'.

        Occupant impact (Issue #301): the Ecobee was reverting to its comfort program when
        CA sent heat_cool mode. Single-mode cool command is held properly by the thermostat.
        """
        engine = _minimal_engine()

        state_mock = MagicMock()
        state_mock.state = "cool"
        state_mock.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
        }
        engine.hass.states.get.return_value = state_mock

        band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")

        asyncio.run(engine._apply_comfort_band(band, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate"]
        # Single call (Issue #301): one set_temperature call with hvac_mode + temperature
        assert len(climate_calls) == 1, (
            f"Expected 1 climate service call (single-setpoint), got {len(climate_calls)}: {climate_calls}"
        )
        assert climate_calls[0].args[1] == "set_temperature", (
            "The single call must be set_temperature, not set_hvac_mode"
        )
        call_data = climate_calls[0].args[2]
        assert call_data.get("hvac_mode") == "cool", f"Ceiling band must send hvac_mode='cool', got {call_data}"
        assert call_data.get("temperature") == 76.0, (
            f"Ceiling band must send temperature=ceiling (76.0), got {call_data}"
        )
        # No dual-setpoint keys
        assert "target_temp_low" not in call_data
        assert "target_temp_high" not in call_data

    def test_heat_cool_thermostat_floor_band_issues_one_call_with_heat_mode(self):
        """heat_cool-capable thermostat + active=floor → ONE set_temperature call, hvac_mode='heat'.

        Issue #301: floor defense uses heat mode + single temperature, not dual setpoints.
        """
        engine = _minimal_engine()

        state_mock = MagicMock()
        state_mock.state = "heat"
        state_mock.attributes = {
            "hvac_modes": ["off", "heat", "cool", "heat_cool"],
            "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
        }
        engine.hass.states.get.return_value = state_mock

        band = ComfortBand(floor=68.0, ceiling=76.0, active="floor", reason="test")

        asyncio.run(engine._apply_comfort_band(band, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c.args[0] == "climate"]
        assert len(climate_calls) == 1, (
            f"Expected 1 climate service call (single-setpoint), got {len(climate_calls)}: {climate_calls}"
        )
        call_data = climate_calls[0].args[2]
        assert call_data.get("hvac_mode") == "heat", f"Floor band must send hvac_mode='heat', got {call_data}"
        assert call_data.get("temperature") == 68.0, f"Floor band must send temperature=floor (68.0), got {call_data}"


# ---------------------------------------------------------------------------
# Issue #444: comfort_band_applied event dedup — overlapping triggers (startup
# coalesce + its own follow-on refresh; grace-expiry re-application colliding with
# the regular cycle) must not each re-announce an identical band as a fresh event.
# ---------------------------------------------------------------------------


def _band_engine() -> AutomationEngine:
    """Like _minimal_engine() but with a heat_cool-capable thermostat and an
    _emit_event_callback that records every emitted (event_type, payload)."""
    engine = _minimal_engine()
    state_mock = MagicMock()
    state_mock.state = "cool"
    state_mock.attributes = {
        "hvac_modes": ["off", "heat", "cool", "heat_cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
    }
    engine.hass.states.get.return_value = state_mock
    engine._emitted_events = []
    engine._emit_event_callback = lambda etype, payload: engine._emitted_events.append((etype, payload))
    return engine


class TestApplyComfortBandEventDedup:
    """Issue #444 — the comfort_band_applied EVENT is deduped within a short window
    when the band is unchanged; the underlying set_temperature call is never deduped."""

    def test_identical_band_within_window_emits_event_once(self):
        """Revert-test: without the guard this emits 2 events, not 1."""
        engine = _band_engine()
        band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")

        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 7, 10, 10, 53, 0, tzinfo=UTC),
        ):
            asyncio.run(engine._apply_comfort_band(band, reason="coalesce path"))
            asyncio.run(engine._apply_comfort_band(band, reason="regular cycle path"))

        band_events = [e for e in engine._emitted_events if e[0] == "comfort_band_applied"]
        assert len(band_events) == 1, f"Expected exactly 1 comfort_band_applied event, got {len(band_events)}"

        # The underlying thermostat command is NEVER deduped — both calls must have fired.
        climate_calls = [c for c in engine.hass.services.async_call.call_args_list if c.args[0] == "climate"]
        assert len(climate_calls) == 2, (
            f"Expected 2 set_temperature calls (thermostat re-assertion is unconditional), got {len(climate_calls)}"
        )

    def test_different_band_always_emits_regardless_of_timing(self):
        """A genuinely different band must never be suppressed, even seconds apart."""
        engine = _band_engine()
        band_a = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")
        band_b = ComfortBand(floor=68.0, ceiling=74.0, active="ceiling", reason="test")  # different ceiling

        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 7, 10, 10, 53, 0, tzinfo=UTC),
        ):
            asyncio.run(engine._apply_comfort_band(band_a, reason="first"))
            asyncio.run(engine._apply_comfort_band(band_b, reason="second"))

        band_events = [e for e in engine._emitted_events if e[0] == "comfort_band_applied"]
        assert len(band_events) == 2, f"Different bands must both emit, got {len(band_events)}"

    def test_identical_band_after_dedup_window_emits_again(self):
        """Not a permanent suppression — a real re-announcement after the window fires normally."""
        engine = _band_engine()
        band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")

        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 7, 10, 10, 53, 0, tzinfo=UTC),
        ):
            asyncio.run(engine._apply_comfort_band(band, reason="first"))

        with patch(
            "custom_components.climate_advisor.automation.dt_util.now",
            return_value=datetime(2026, 7, 10, 11, 5, 0, tzinfo=UTC),  # 12 min later — past the 10-min window
        ):
            asyncio.run(engine._apply_comfort_band(band, reason="30-min cycle, unchanged"))

        band_events = [e for e in engine._emitted_events if e[0] == "comfort_band_applied"]
        assert len(band_events) == 2, (
            f"A re-announcement after COMFORT_BAND_EVENT_DEDUP_SECONDS must not be suppressed, got {len(band_events)}"
        )
