"""Tests for Issue #87 — window/HVAC interaction fixes.

Four fixes are validated here:

Fix 1: check_window_cooling_opportunity() returns False when _natural_vent_active is
       True — prevents the economizer from restarting AC while nat-vent is already
       handling cooling.

Fix 2: handle_door_window_open() allows nat-vent to bypass an active grace period
       when outdoor temperature is at or below the nat-vent threshold.

Fix 3: _re_pause_for_open_sensor() checks nat-vent conditions before re-pausing when
       a grace period expires with sensors still open.

Fix 4: _start_grace_period() sets _grace_end_time to a valid ISO timestamp; grace
       expiry clears it back to None.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor import automation as _ae_mod
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hot_classification() -> DayClassification:
    """Minimal hot-day DayClassification."""
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "hot",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 90,
            "today_low": 65,
            "tomorrow_high": 88,
            "tomorrow_low": 64,
            "hvac_mode": "cool",
            "pre_condition": True,
            "pre_condition_target": -2.0,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": True,
        }
    )
    return c


def _make_ae_stub(**overrides) -> AutomationEngine:
    """Build a minimal AutomationEngine stub using object.__new__.

    All HA async methods are replaced with AsyncMock / MagicMock so tests do
    not require a running Home Assistant instance.  The real methods under test
    are bound via types.MethodType so they execute against the stub's state.
    """
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.states = MagicMock()

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = "climate.thermostat"
    ae.config = {
        "comfort_cool": 75,
        "comfort_heat": 68,
        "temp_unit": "fahrenheit",
        "natural_vent_delta": 3,  # threshold = 75 + 3 = 78 °F
        "aggressive_savings": False,
        "economizer_temp_delta": 3,
        "sensor_debounce_seconds": 0,
        "manual_grace_period": 1800,
        "automation_grace_period": 300,
        "manual_grace_notify": False,
        "automation_grace_notify": False,
    }
    ae._current_classification = _make_hot_classification()
    ae._natural_vent_active = False
    ae._economizer_active = False
    ae._economizer_phase = "inactive"
    ae._paused_by_door = False
    ae._pre_pause_mode = None
    ae._grace_active = False
    ae._grace_end_time = None
    ae._last_resume_source = None
    ae._last_outdoor_temp = 65.0
    ae._get_indoor_temp_f = MagicMock(return_value=75.0)
    ae._fan_active = False
    ae._fan_on_since = None
    ae._manual_grace_cancel = None
    ae._automation_grace_cancel = None
    ae._sensor_check_callback = None
    ae._emit_event_callback = None
    ae.dry_run = False

    # Mock async service calls
    ae._set_hvac_mode = AsyncMock()
    ae._set_temperature = AsyncMock()
    ae._activate_fan = AsyncMock()
    ae._deactivate_fan = AsyncMock()
    ae._deactivate_economizer = AsyncMock()
    ae._notify = AsyncMock()
    ae._is_within_planned_window_period = MagicMock(return_value=False)

    for k, v in overrides.items():
        setattr(ae, k, v)

    # Bind the real methods under test
    ae.check_window_cooling_opportunity = types.MethodType(
        _ae_mod.AutomationEngine.check_window_cooling_opportunity, ae
    )
    ae.handle_door_window_open = types.MethodType(_ae_mod.AutomationEngine.handle_door_window_open, ae)
    ae._re_pause_for_open_sensor = types.MethodType(_ae_mod.AutomationEngine._re_pause_for_open_sensor, ae)
    ae._start_grace_period = types.MethodType(_ae_mod.AutomationEngine._start_grace_period, ae)
    ae._cancel_grace_timers = types.MethodType(_ae_mod.AutomationEngine._cancel_grace_timers, ae)
    ae.clear_manual_override = MagicMock()

    return ae


# ---------------------------------------------------------------------------
# Fix 1: Economizer nat-vent guard
# ---------------------------------------------------------------------------


class TestEconomizerNatVentGuard:
    """check_window_cooling_opportunity() must not activate AC while nat-vent is active."""

    def test_economizer_does_not_activate_when_nat_vent_active(self):
        """Returns False and does NOT call _set_hvac_mode when nat-vent is running."""
        ae = _make_ae_stub(_natural_vent_active=True, _economizer_active=False)

        result = asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is False
        # _set_hvac_mode must NOT have been called with "cool"
        for call in ae._set_hvac_mode.call_args_list:
            assert call.args[0] != "cool", "Should not activate AC when nat-vent is active"

    def test_economizer_activates_normally_without_nat_vent(self):
        """Returns True and calls _set_hvac_mode('cool') when nat-vent is NOT active."""
        ae = _make_ae_stub(_natural_vent_active=False, _economizer_active=False)

        result = asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,  # indoor > comfort_cool (75) → Phase 1
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is True
        ae._set_hvac_mode.assert_called_once()
        assert ae._set_hvac_mode.call_args.args[0] == "cool"

    def test_economizer_nat_vent_guard_does_not_deactivate_economizer(self):
        """When nat-vent is active and economizer was also running, deactivation is NOT triggered.

        The nat-vent guard returns False early (before the deactivation branch that
        fires when day_type is not hot or conditions are not eligible), so
        _deactivate_economizer should NOT be called.
        """
        ae = _make_ae_stub(_natural_vent_active=True, _economizer_active=True)

        asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        ae._deactivate_economizer.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2: Grace-period nat-vent bypass
# ---------------------------------------------------------------------------


class TestGraceNatVentBypass:
    """handle_door_window_open() should activate nat-vent through an active grace period
    when outdoor temperature is cool enough."""

    def test_grace_active_outdoor_cool_nat_vent_activates(self):
        """Grace active + outdoor ≤ threshold → HVAC off + nat-vent, NOT blocked."""
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="automation",
            _last_outdoor_temp=70.0,  # below threshold of 78 °F
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        ae._set_hvac_mode.assert_called_once()
        assert ae._set_hvac_mode.call_args.args[0] == "off"
        assert ae._natural_vent_active is True
        # Must NOT have set the pause flag
        assert ae._paused_by_door is False

    def test_grace_active_outdoor_warm_nat_vent_blocked(self):
        """Grace active + outdoor > threshold → blocked, no HVAC change, no pause."""
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="automation",
            _last_outdoor_temp=82.0,  # above threshold of 78 °F
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        ae._set_hvac_mode.assert_not_called()
        assert ae._paused_by_door is False

    def test_grace_active_outdoor_none_nat_vent_blocked(self):
        """Grace active + no outdoor data → blocked (safe fallback)."""
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="automation",
            _last_outdoor_temp=None,
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        ae._set_hvac_mode.assert_not_called()
        assert ae._paused_by_door is False


# ---------------------------------------------------------------------------
# Fix 3: _re_pause_for_open_sensor nat-vent check
# ---------------------------------------------------------------------------


class TestRePauseNatVent:
    """_re_pause_for_open_sensor() should activate nat-vent instead of pausing when
    outdoor conditions are favourable."""

    def test_re_pause_activates_nat_vent_when_outdoor_cool(self):
        """Outdoor ≤ threshold → nat-vent activated; paused_by_door stays False."""
        ae = _make_ae_stub(_last_outdoor_temp=70.0)

        asyncio.run(ae._re_pause_for_open_sensor())

        assert ae._natural_vent_active is True
        assert ae._paused_by_door is False
        ae._set_hvac_mode.assert_called_once()
        assert ae._set_hvac_mode.call_args.args[0] == "off"
        ae._activate_fan.assert_called_once()

    def test_re_pause_pauses_when_outdoor_warm(self):
        """Outdoor > threshold → fall through to regular re-pause."""
        ae = _make_ae_stub(_last_outdoor_temp=82.0)

        # Provide a mock thermostat state of "cool" so the re-pause logic fires
        mock_state = MagicMock()
        mock_state.state = "cool"
        ae.hass.states.get.return_value = mock_state

        asyncio.run(ae._re_pause_for_open_sensor())

        assert ae._paused_by_door is True
        assert ae._natural_vent_active is False
        ae._set_hvac_mode.assert_called_once()
        assert ae._set_hvac_mode.call_args.args[0] == "off"


# ---------------------------------------------------------------------------
# Fix 4: _start_grace_period sets _grace_end_time; expiry clears it
# ---------------------------------------------------------------------------


class TestGraceEndTime:
    """_start_grace_period() must populate _grace_end_time; grace expiry clears it."""

    def test_grace_end_time_set_on_start(self):
        """After _start_grace_period(), _grace_end_time is a parseable ISO string."""
        ae = _make_ae_stub()
        fixed_now = datetime(2026, 4, 5, 19, 0, 0)

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                return_value=MagicMock(),
            ),
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_now
            ae._start_grace_period("manual")

        assert ae._grace_end_time is not None
        # Must be parseable as an ISO datetime (raises ValueError if not)
        parsed = datetime.fromisoformat(ae._grace_end_time)
        assert isinstance(parsed, datetime)

    def test_grace_end_time_cleared_on_expiry(self):
        """When the grace callback fires with no open sensors, _grace_end_time → None."""
        ae = _make_ae_stub()

        captured_callback = {}

        def _fake_async_call_later(_hass, _delay, cb):
            captured_callback["fn"] = cb
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=_fake_async_call_later,
            ),
            patch(
                "custom_components.climate_advisor.automation.callback",
                side_effect=lambda fn: fn,
            ),
        ):
            ae._start_grace_period("automation")

        assert ae._grace_end_time is not None
        assert ae._grace_active is True

        # Simulate grace expiry: no sensors open, no planned window period
        ae._sensor_check_callback = None  # no open sensors
        ae._is_within_planned_window_period = MagicMock(return_value=False)

        # Fire the captured _grace_expired callback
        captured_callback["fn"](None)

        assert ae._grace_end_time is None
        assert ae._grace_active is False
