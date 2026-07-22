"""Tests for the Climate Advisor REST API module."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.api import (
    API_VIEWS,
    _get_coordinator,
)
from custom_components.climate_advisor.const import (
    API_AUTOMATION_STATE,
    API_BRIEFING,
    API_CANCEL_OVERRIDE,
    API_CHART_DATA,
    API_CONFIG,
    API_FORCE_RECLASSIFY,
    API_LEARNING,
    API_RESPOND_SUGGESTION,
    API_SEND_BRIEFING,
    API_STATUS,
    API_TOGGLE_AUTOMATION,
    ATTR_CURRENT_SETPOINT,
    ATTR_DAY_TYPE,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_OVERRIDE_CONFIRM_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_WELCOME_HOME_DEBOUNCE,
    CONFIG_METADATA,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_OVERRIDE_CONFIRM_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS,
    DOMAIN,
)
from custom_components.climate_advisor.learning import DailyRecord


class TestGetCoordinator:
    """Tests for _get_coordinator helper."""

    def test_returns_coordinator_when_loaded(self):
        coord = MagicMock()
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": coord}}
        assert _get_coordinator(hass) is coord

    def test_returns_none_when_not_loaded(self):
        hass = MagicMock()
        hass.data = {}
        assert _get_coordinator(hass) is None

    def test_returns_none_when_domain_empty(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        assert _get_coordinator(hass) is None


class TestAPIConstants:
    """Test that API path constants are properly defined."""

    def test_all_paths_under_base(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
            API_CANCEL_OVERRIDE,
            API_TOGGLE_AUTOMATION,
        ]
        for path in paths:
            assert path.startswith("/api/climate_advisor/"), f"{path} has wrong prefix"

    def test_paths_are_unique(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
            API_CANCEL_OVERRIDE,
            API_TOGGLE_AUTOMATION,
        ]
        assert len(paths) == len(set(paths))


class TestAPIViewList:
    """Test the API_VIEWS registry."""

    def test_correct_count(self):
        assert len(API_VIEWS) == 23

    def test_all_are_callable(self):
        for view_cls in API_VIEWS:
            assert callable(view_cls)


class TestCoordinatorDataContract:
    """Test that the data contract between coordinator and API is correct."""

    def test_status_data_fields(self):
        """API status view expects these keys in coordinator.data."""
        required_keys = [
            ATTR_DAY_TYPE,
            "trend_direction",
            "trend_magnitude",
            "automation_status",
            "compliance_score",
            "next_human_action",
        ]
        coord_data = {
            ATTR_DAY_TYPE: "warm",
            "trend_direction": "stable",
            "trend_magnitude": 2.5,
            "automation_status": "active",
            "compliance_score": 0.92,
            "next_human_action": "Open windows at 08:00 AM",
        }
        for key in required_keys:
            assert key in coord_data

    def test_chart_data_structure(self):
        """Chart data should have all expected series."""
        chart_data = {
            # predicted_outdoor removed — now consumed from state_log.pred_outdoor (historical)
            # and forecast_outdoor (future), merged in the frontend
            "predicted_indoor": [{"ts": "2026-03-18T08:00:00+00:00", "temp": 70.0}],
            "forecast_outdoor": [{"ts": "2026-03-18T08:00:00+00:00", "temp": 60.0}],
            "actual_outdoor": [{"time": "2026-03-18T08:00:00", "temp": 62.0}],
            "actual_indoor": [{"time": "2026-03-18T08:00:00", "temp": 70.0}],
            "current_hour": 14.5,
        }
        assert "predicted_indoor" in chart_data
        assert "forecast_outdoor" in chart_data
        assert "actual_outdoor" in chart_data
        assert "actual_indoor" in chart_data
        assert "current_hour" in chart_data

    def test_debug_state_structure(self):
        """Debug state should have all expected fields."""
        debug_state = {
            "paused_by_door": False,
            "pre_pause_mode": None,
            "grace_active": False,
            "last_resume_source": None,
            "door_window_sensors": {},
            "pending_debounce_timers": [],
            "classification": None,
        }
        assert "paused_by_door" in debug_state
        assert "grace_active" in debug_state
        assert "door_window_sensors" in debug_state

    def test_daily_record_serializable(self):
        """DailyRecord should be serializable for the learning endpoint."""
        from dataclasses import asdict

        record = DailyRecord(date="2026-03-18", day_type="warm", trend_direction="stable")
        data = asdict(record)
        assert data["date"] == "2026-03-18"
        assert data["day_type"] == "warm"
        assert data["manual_overrides"] == 0
        assert data["door_window_pause_events"] == 0


class TestConfigViewDisplayTransform:
    """Tests for seconds-to-minutes display transform in config settings."""

    SECONDS_KEYS = (
        CONF_SENSOR_DEBOUNCE,
        CONF_MANUAL_GRACE_PERIOD,
        CONF_AUTOMATION_GRACE_PERIOD,
        CONF_OVERRIDE_CONFIRM_PERIOD,
        CONF_WELCOME_HOME_DEBOUNCE,
    )

    def test_seconds_keys_have_display_transform(self):
        """All seconds-based config keys should declare a display_transform."""
        for key in self.SECONDS_KEYS:
            meta = CONFIG_METADATA[key]
            assert meta.get("display_transform") == "seconds_to_minutes", (
                f"{key} missing display_transform in CONFIG_METADATA"
            )

    def test_seconds_to_minutes_conversion_values(self):
        """Default seconds values should convert to expected minutes."""
        cases = [
            (DEFAULT_SENSOR_DEBOUNCE_SECONDS, 10),
            (DEFAULT_MANUAL_GRACE_SECONDS, 30),
            (DEFAULT_AUTOMATION_GRACE_SECONDS, 5),
            (DEFAULT_OVERRIDE_CONFIRM_SECONDS, 10),
            (DEFAULT_WELCOME_HOME_DEBOUNCE_SECONDS, 60),
        ]
        for seconds, expected_minutes in cases:
            assert seconds // 60 == expected_minutes

    def test_transform_not_applied_to_non_time_keys(self):
        """Non-time settings should not have a display_transform."""
        non_time_keys = [k for k in CONFIG_METADATA if k not in self.SECONDS_KEYS]
        for key in non_time_keys:
            assert "display_transform" not in CONFIG_METADATA[key], f"{key} should not have display_transform"

    def test_none_value_safe_with_transform(self):
        """Seconds-to-minutes transform should not crash on None values."""
        value = None
        transform = "seconds_to_minutes"
        if transform == "seconds_to_minutes" and isinstance(value, (int, float)):
            value = value // 60
        assert value is None


class TestToggleAutomationView:
    """Tests for the toggle_automation API endpoint."""

    def test_toggle_disables_when_enabled(self):
        """Toggling when enabled should disable automation."""
        coord = MagicMock()
        coord.automation_enabled = True
        coord.set_automation_enabled = MagicMock()

        new_state = not coord.automation_enabled
        coord.set_automation_enabled(new_state)

        coord.set_automation_enabled.assert_called_once_with(False)

    def test_toggle_enables_when_disabled(self):
        """Toggling when disabled should enable automation."""
        coord = MagicMock()
        coord.automation_enabled = False
        coord.set_automation_enabled = MagicMock()

        new_state = not coord.automation_enabled
        coord.set_automation_enabled(new_state)

        coord.set_automation_enabled.assert_called_once_with(True)

    def test_toggle_automation_constant_defined(self):
        """API_TOGGLE_AUTOMATION should be under the base path."""
        assert API_TOGGLE_AUTOMATION.startswith("/api/climate_advisor/")
        assert "toggle_automation" in API_TOGGLE_AUTOMATION


# ---------------------------------------------------------------------------
# Helpers for Celsius unit tests (replicate api.py view logic without instantiation)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# View-driving helpers — exercise the real HomeAssistantView subclasses
# (HomeAssistantView is a real minimal base class as of Issue #452, so these
# no longer need to hand-replicate the view's response dict).
# ---------------------------------------------------------------------------


def _make_view_request(coordinator, climate_state=None):
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}
    hass.states.get.return_value = climate_state
    req = MagicMock()
    req.app = {"hass": hass}
    return req


def _simulate_status_get(coordinator, climate_state=None):
    """Call the real ClimateAdvisorStatusView.get() and return its json body."""
    import asyncio

    from custom_components.climate_advisor.api import ClimateAdvisorStatusView

    view = ClimateAdvisorStatusView()
    request = _make_view_request(coordinator, climate_state)
    resp = asyncio.run(view.get(request))
    return resp.json_data


def _simulate_learning_get(coordinator):
    """Call the real ClimateAdvisorLearningView.get() and return its json body."""
    import asyncio

    from custom_components.climate_advisor.api import ClimateAdvisorLearningView

    view = ClimateAdvisorLearningView()
    request = _make_view_request(coordinator)
    resp = asyncio.run(view.get(request))
    return resp.json_data


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__, for select_comfort_band() tests."""
    from custom_components.climate_advisor.classifier import DayClassification

    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "mild",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 58,
        "tomorrow_high": 79,
        "tomorrow_low": 59,
        "hvac_mode": "heat",
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


def _status_get_with_climate_state(
    config: dict, climate_state, indoor_temp=70.0, outdoor_temp=None, occupancy_mode="home", classification=None
):
    """Build a minimal coordinator and drive the real status view, for setpoint/ca-target tests."""
    coord = MagicMock()
    coord.config = config
    coord.data = {}
    coord._get_indoor_temp.return_value = indoor_temp
    coord._last_outdoor_temp = outdoor_temp
    coord.automation_enabled = True
    coord._occupancy_mode = occupancy_mode
    coord.current_classification = classification if classification is not None else _make_classification()
    ae = MagicMock()
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._fan_override_active = False
    ae._pre_condition_achieved = False
    ae.is_paused_by_door = False
    coord.automation_engine = ae
    coord._compute_contact_details.return_value = []
    return _simulate_status_get(coord, climate_state)


class TestCATargetSleepAware:
    """Issue #402/#462: ca_target_heat/cool must reflect the real live-setpoint band
    (via select_comfort_band(), the same resolver every setpoint-writing code path
    uses) during the sleep window and for away/vacation occupancy — not the flat
    daytime comfort_heat/comfort_cool, and not the display-only sleep/day heuristic
    that used to ignore occupancy mode entirely. Otherwise the divergence indicators
    compare the real thermostat setpoint against the wrong value, masking exactly the
    kind of stuck/frozen-target bug #402 covers.
    """

    _CONFIG = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "sleep_heat": 64.0,
        "sleep_cool": 72.0,
        "sleep_time": "22:30",
        "wake_time": "07:00",
    }

    def _get_ca_targets(self, config: dict, now, occupancy_mode="home"):
        """Drive the real ClimateAdvisorStatusView.get(), patching dt_util.now()."""
        from unittest.mock import patch

        from homeassistant.util import dt as dt_util

        state = MagicMock()
        state.state = "heat"
        state.attributes = {"temperature": 70, "target_temp_low": None, "target_temp_high": None}
        with patch.object(dt_util, "now", return_value=now):
            response = _status_get_with_climate_state(config, state, occupancy_mode=occupancy_mode)
        return response["ca_target_heat"], response["ca_target_cool"]

    def test_daytime_uses_comfort_band(self):
        from datetime import datetime

        now = datetime(2026, 7, 21, 14, 0, 0)  # 14:00 — outside sleep window
        heat, cool = self._get_ca_targets(self._CONFIG, now)
        assert heat == 68.0
        assert cool == 74.0

    def test_sleep_window_uses_sleep_band(self):
        from datetime import datetime

        now = datetime(2026, 7, 21, 2, 0, 0)  # 02:00 — inside 22:30-07:00 sleep window
        heat, cool = self._get_ca_targets(self._CONFIG, now)
        assert heat == 64.0
        assert cool == 72.0
        assert heat != 68.0, "must not fall back to the flat daytime comfort_heat overnight"
        assert cool != 74.0, "must not fall back to the flat daytime comfort_cool overnight"

    def test_sleep_window_falls_back_to_default_sleep_temps_when_not_configured(self):
        """Issue #462: select_comfort_band()'s sleep-branch fallback is DEFAULT_SLEEP_HEAT/
        DEFAULT_SLEEP_COOL (64/72), not comfort_heat/comfort_cool (68/74) — the old inline
        implementation's fallback didn't match the live setpoint resolver's actual default."""
        from datetime import datetime

        config = {"comfort_heat": 68.0, "comfort_cool": 74.0, "sleep_time": "22:30", "wake_time": "07:00"}
        now = datetime(2026, 7, 21, 2, 0, 0)
        heat, cool = self._get_ca_targets(config, now)
        assert heat == 64.0
        assert cool == 72.0

    def test_away_mode_uses_setback_band_not_comfort_or_sleep(self):
        """Issue #462 regression: the old inline branch ignored occupancy entirely, so
        away mode showed the comfort/sleep band even though the thermostat was really
        being held at the setback band."""
        from datetime import datetime

        now = datetime(2026, 7, 21, 14, 0, 0)  # daytime — old code would have shown comfort band
        config = {**self._CONFIG, "setback_heat": 60.0, "setback_cool": 80.0}
        heat, cool = self._get_ca_targets(config, now, occupancy_mode="away")
        assert heat == 60.0
        assert cool == 80.0
        assert heat != 68.0
        assert cool != 74.0

    def test_vacation_mode_uses_deep_setback_band(self):
        """Issue #462 regression: vacation mode must show the wider vacation setback
        (setback ± VACATION_SETBACK_EXTRA), not the comfort/sleep band."""
        from datetime import datetime

        from custom_components.climate_advisor.const import VACATION_SETBACK_EXTRA

        now = datetime(2026, 7, 21, 14, 0, 0)
        config = {**self._CONFIG, "setback_heat": 60.0, "setback_cool": 80.0}
        heat, cool = self._get_ca_targets(config, now, occupancy_mode="vacation")
        assert heat == 60.0 - VACATION_SETBACK_EXTRA
        assert cool == 80.0 + VACATION_SETBACK_EXTRA


class TestStatusSetpointExtraction:
    """Issue #266: the status endpoint must expose the dual band setpoints in heat_cool mode."""

    _CONFIG = {"comfort_heat": 68.0, "comfort_cool": 74.0}

    def test_heat_cool_band_exposes_dual_setpoints(self):
        state = MagicMock()
        state.state = "heat_cool"
        state.attributes = {"temperature": None, "target_temp_low": 64, "target_temp_high": 72}
        response = _status_get_with_climate_state(self._CONFIG, state)
        assert response[ATTR_CURRENT_SETPOINT] is None  # single setpoint is absent in the heat_cool band
        assert response["target_temp_low"] == 64
        assert response["target_temp_high"] == 72

    def test_cool_mode_exposes_single_setpoint(self):
        state = MagicMock()
        state.state = "cool"
        state.attributes = {"temperature": 74, "target_temp_low": None, "target_temp_high": None}
        response = _status_get_with_climate_state(self._CONFIG, state)
        assert response[ATTR_CURRENT_SETPOINT] == 74
        assert response["target_temp_low"] is None and response["target_temp_high"] is None

    def test_off_mode_exposes_no_setpoints(self):
        state = MagicMock()
        state.state = "off"
        state.attributes = {"temperature": 74, "target_temp_low": 64, "target_temp_high": 72}
        response = _status_get_with_climate_state(self._CONFIG, state)
        assert response[ATTR_CURRENT_SETPOINT] is None
        assert response["target_temp_low"] is None and response["target_temp_high"] is None


class TestStatusViewCelsiusUnit:
    """Status API must convert temperatures and include 'unit' when temp_unit=celsius."""

    def _make_coordinator(self, temp_unit="celsius", indoor_temp=68.0, outdoor_temp=86.0, trend_magnitude=9.0):
        coord = MagicMock()
        coord.config = {"temp_unit": temp_unit, "climate_entity": "climate.test"}
        coord.data = {
            ATTR_DAY_TYPE: "mild",
            ATTR_TREND: "stable",
            ATTR_TREND_MAGNITUDE: trend_magnitude,
            "automation_status": "active",
            "compliance_score": 1.0,
        }
        coord._get_indoor_temp.return_value = indoor_temp
        coord._last_outdoor_temp = outdoor_temp
        coord.automation_enabled = True
        coord._occupancy_mode = "home"
        ae = MagicMock()
        ae._manual_override_active = False
        ae._override_confirm_pending = False
        ae._fan_override_active = False
        ae.is_paused_by_door = False
        coord.automation_engine = ae
        coord._compute_contact_details.return_value = []
        coord.yesterday_record = None
        coord.tomorrow_plan = None
        return coord

    def test_status_celsius_converts_indoor_temp(self):
        """indoor_temp must be in Celsius when unit=celsius configured."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", indoor_temp=68.0)
        response = _simulate_status_get(coord)
        assert response["unit"] == "celsius"  # KeyError before fix
        assert response["indoor_temp"] == pytest.approx(20.0, abs=0.1)  # 68°F → 20°C

    def test_status_celsius_converts_trend_magnitude(self):
        """trend_magnitude must be converted delta when unit=celsius."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", trend_magnitude=9.0)
        response = _simulate_status_get(coord)
        assert response["trend_magnitude"] == pytest.approx(5.0, abs=0.1)  # 9°F delta → 5°C delta

    def test_status_includes_unit_field(self):
        """Status response must include a 'unit' field."""
        coord = self._make_coordinator(temp_unit="fahrenheit", indoor_temp=72.0)
        response = _simulate_status_get(coord)
        assert "unit" in response  # KeyError before fix
        assert response["unit"] == "fahrenheit"

    def test_status_celsius_converts_outdoor_temp(self):
        """outdoor_temp must be in Celsius when unit=celsius configured (Issue #367)."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", outdoor_temp=86.0)
        response = _simulate_status_get(coord)
        assert "outdoor_temp" in response
        assert response["outdoor_temp"] == pytest.approx(30.0, abs=0.1)  # 86°F → 30°C

    def test_status_outdoor_temp_none_when_unavailable(self):
        """outdoor_temp must be None when coordinator has no outdoor reading (Issue #367)."""
        coord = self._make_coordinator(temp_unit="fahrenheit", outdoor_temp=None)
        coord._last_outdoor_temp = None
        response = _simulate_status_get(coord)
        assert response["outdoor_temp"] is None

    def test_status_fahrenheit_outdoor_temp_passthrough(self):
        """outdoor_temp passes through unconverted when unit=fahrenheit (Issue #367)."""
        coord = self._make_coordinator(temp_unit="fahrenheit", outdoor_temp=92.0)
        response = _simulate_status_get(coord)
        assert response["outdoor_temp"] == 92.0


class TestLearningViewCelsiusUnit:
    """Learning API must convert comfort range temps and include 'unit' when temp_unit=celsius."""

    def _make_coordinator(self, temp_unit="celsius", comfort_heat=68, comfort_cool=76):
        coord = MagicMock()
        coord.config = {
            "temp_unit": temp_unit,
            "comfort_heat": comfort_heat,
            "comfort_cool": comfort_cool,
        }
        coord.today_record = None
        coord.yesterday_record = None
        coord.tomorrow_plan = None
        coord.learning = MagicMock()
        coord.learning.generate_suggestions.return_value = []
        coord.learning.get_last_suggestion_keys.return_value = []
        coord.learning.get_compliance_summary.return_value = {}
        return coord

    def test_learning_celsius_converts_comfort_range(self):
        """comfort_range_low/high must be in Celsius when configured."""
        import pytest

        coord = self._make_coordinator(temp_unit="celsius", comfort_heat=68, comfort_cool=76)
        response = _simulate_learning_get(coord)
        assert response["unit"] == "celsius"  # KeyError before fix
        assert response["comfort_range_low"] == pytest.approx(20.0, abs=0.1)  # 68°F → 20°C
        assert response["comfort_range_high"] == pytest.approx(24.4, abs=0.1)  # 76°F → 24.4°C

    def test_learning_includes_unit_field(self):
        """Learning response must include a 'unit' field."""
        coord = self._make_coordinator(temp_unit="fahrenheit")
        response = _simulate_learning_get(coord)
        assert "unit" in response  # KeyError before fix


class TestCancelViewsGraceCancellation:
    """Issue #508: both Cancel-override API views must cancel grace, not just the override flag.

    Regression guardrail — a future third cancel-style endpoint that composes primitives by
    hand instead of calling ``AutomationEngine.cancel_override()`` is now the only way to
    reintroduce this bug, and it would need its own omitted test to slip through silently.

    Uses a real AutomationEngine (not a MagicMock) so the real cancel_override() logic runs
    end-to-end through the view, per the "never mirror the logic under test" doctrine.
    """

    def _make_engine(self):
        from unittest.mock import AsyncMock

        from custom_components.climate_advisor.automation import AutomationEngine
        from custom_components.climate_advisor.const import CONF_OVERRIDE_CONFIRM_PERIOD

        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        hass.states = MagicMock()
        state = MagicMock()
        state.state = "cool"
        state.attributes = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1}
        hass.states.get.return_value = state

        config = {
            "comfort_heat": 70,
            "comfort_cool": 75,
            "notify_service": "notify.notify",
            CONF_OVERRIDE_CONFIRM_PERIOD: 0,
        }
        return AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=[],
            notify_service=config["notify_service"],
            config=config,
        )

    def _make_request(self, coordinator) -> MagicMock:
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": coordinator}}
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
        req = MagicMock()
        req.app = {"hass": hass}
        return req

    def _post(self, view_cls, coordinator):
        import asyncio

        request = self._make_request(coordinator)
        view = view_cls()
        return asyncio.run(view.post(request))

    def test_cancel_override_view_cancels_grace_for_thermostat_override(self):
        from custom_components.climate_advisor.api import ClimateAdvisorCancelOverrideView

        ae = self._make_engine()
        ae.handle_manual_override()
        assert ae._grace_active is True

        coordinator = MagicMock()
        coordinator.automation_engine = ae
        coordinator._current_classification = None

        self._post(ClimateAdvisorCancelOverrideView, coordinator)

        assert ae._manual_override_active is False
        assert ae._grace_active is False

    def test_cancel_fan_override_view_cancels_grace(self):
        """The Issue #508 regression: cancelling a FAN-only override must also cancel grace.

        Before the fix, ClimateAdvisorCancelFanOverrideView.post() called only
        clear_fan_override(), leaving _grace_active stuck True for the rest of the original
        (potentially 8-hour, RF-remote-timer) grace duration.
        """
        from custom_components.climate_advisor.api import ClimateAdvisorCancelFanOverrideView

        ae = self._make_engine()
        ae.handle_fan_manual_override(fan_before="auto", fan_after="on")
        assert ae._manual_override_active is False  # fan overrides never set this flag
        assert ae._fan_override_active is True
        assert ae._grace_active is True

        coordinator = MagicMock()
        coordinator.automation_engine = ae
        coordinator._current_classification = None

        response = self._post(ClimateAdvisorCancelFanOverrideView, coordinator)

        assert ae._fan_override_active is False
        assert ae._grace_active is False
        assert response.json_data["status"] == "ok"
        assert "cleared" in response.json_data["message"].lower()

    def test_cancel_fan_override_view_noop_message_when_nothing_active(self):
        from custom_components.climate_advisor.api import ClimateAdvisorCancelFanOverrideView

        ae = self._make_engine()
        coordinator = MagicMock()
        coordinator.automation_engine = ae
        coordinator._current_classification = None

        response = self._post(ClimateAdvisorCancelFanOverrideView, coordinator)

        assert response.json_data["status"] == "ok"
        assert "no active fan override" in response.json_data["message"].lower()

    def test_cancel_fan_override_view_schedules_reclassify(self):
        """Root cause #2: cancelling the fan override must schedule a reclassify, not just clear flags."""
        from unittest.mock import patch

        from custom_components.climate_advisor.api import ClimateAdvisorCancelFanOverrideView

        ae = self._make_engine()
        ae.handle_fan_manual_override(fan_before="auto", fan_after="on")

        coordinator = MagicMock()
        coordinator.automation_engine = ae
        coordinator._current_classification = None

        with patch("homeassistant.helpers.event.async_call_later") as mock_call_later:
            self._post(ClimateAdvisorCancelFanOverrideView, coordinator)
            mock_call_later.assert_called_once()
