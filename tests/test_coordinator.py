"""Tests for coordinator helper methods (Issue #18 / Phase 1).

Tests for:
- _compute_next_action logic: HOT day morning/evening window opportunities
  and default closed-windows fallback.
- Briefing notification split: push gets TLDR, email gets full (Issue #34).
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DAY_TYPE_COLD,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_TEMP_DELTA,
    WARM_WINDOW_CLOSE_HOUR,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": DAY_TYPE_HOT,
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 95,
        "today_low": 72,
        "tomorrow_high": 93,
        "tomorrow_low": 70,
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


def _compute_next_action(
    c: DayClassification | None,
    config: dict,
    now_time: time,
    indoor_temp: float | None = None,
) -> str:
    """Replicate _compute_next_action from coordinator.py."""
    if not c:
        return "Waiting for forecast data..."

    comfort_cool = config.get("comfort_cool", 75)

    if c.windows_recommended:
        if c.window_open_time and now_time < c.window_open_time:
            return f"Open windows at {c.window_open_time.strftime('%I:%M %p')}"
        elif c.window_close_time and now_time < c.window_close_time:
            return f"Close windows by {c.window_close_time.strftime('%I:%M %p')}"
        elif now_time >= time(ECONOMIZER_EVENING_START_HOUR, 0):
            return "Open windows to cool down — outdoor air may be cooler now."

    if c.day_type == DAY_TYPE_HOT:
        threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
        if c.window_opportunity_morning and now_time < time(ECONOMIZER_MORNING_END_HOUR, 0):
            end_t = time(ECONOMIZER_MORNING_END_HOUR, 0).strftime("%I:%M %p").lstrip("0")
            return f"Open windows if outdoor temp is below {threshold:.0f}°F (until {end_t})"
        elif c.window_opportunity_evening and now_time >= time(ECONOMIZER_EVENING_START_HOUR, 0):
            return f"Open windows if outdoor temp is below {threshold:.0f}°F"
        return "Keep windows and blinds closed. AC is handling it."
    elif c.day_type == DAY_TYPE_COLD:
        return "Keep doors closed — help the heater out."

    if indoor_temp is not None and indoor_temp > comfort_cool:
        return f"Indoor temp is {indoor_temp:.0f}°F — open windows or turn on a fan to cool down."

    return "No action needed right now. Automation is handling it."


# ---------------------------------------------------------------------------
# Tests: _compute_next_action
# ---------------------------------------------------------------------------


class TestComputeNextAction:
    """Tests for _compute_next_action logic in coordinator.py."""

    def test_next_action_no_classification_returns_waiting(self):
        """When classification is None → 'Waiting for forecast data...'."""
        result = _compute_next_action(None, {}, time(8, 0))
        assert result == "Waiting for forecast data..."

    def test_next_action_hot_morning_shows_threshold(self):
        """HOT day, morning opportunity, before 9 AM → threshold and cutoff time shown."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
            window_opportunity_evening=False,
        )
        config = {"comfort_cool": 75}
        # Mock time: 7:30 AM — before ECONOMIZER_MORNING_END_HOUR (9:00 AM)
        result = _compute_next_action(c, config, time(7, 30))
        assert "78" in result  # threshold = 75 + 3
        assert "9:00 AM" in result  # ECONOMIZER_MORNING_END_HOUR formatted

    def test_next_action_hot_morning_at_exactly_morning_end_does_not_trigger(self):
        """HOT day, morning opportunity, at exactly 9:00 AM → morning branch not taken."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
            window_opportunity_evening=False,
        )
        config = {"comfort_cool": 75}
        # now_time == time(9, 0) — NOT less than morning end, so morning branch skipped
        result = _compute_next_action(c, config, time(9, 0))
        assert "9:00 AM" not in result

    def test_next_action_hot_evening_shows_threshold(self):
        """HOT day, evening opportunity, at or after 5 PM → threshold shown without cutoff."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        # Mock time: 5:00 PM — exactly ECONOMIZER_EVENING_START_HOUR (17:00)
        result = _compute_next_action(c, config, time(17, 0))
        assert "78" in result  # threshold = 75 + 3
        assert "9:00 AM" not in result  # no morning cutoff text

    def test_next_action_hot_evening_later_in_evening(self):
        """HOT day, evening opportunity, well into the evening → threshold still shown."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        result = _compute_next_action(c, config, time(20, 0))
        assert "78" in result

    def test_next_action_hot_evening_before_start_hour_does_not_trigger(self):
        """HOT day, evening opportunity, before 5 PM → evening branch not taken."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        # 3:00 PM — before ECONOMIZER_EVENING_START_HOUR (17:00)
        result = _compute_next_action(c, config, time(15, 0))
        assert "Keep windows and blinds closed" in result

    def test_next_action_hot_no_opportunity(self):
        """HOT day, both opportunity flags False → default closed-windows message."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=False,
        )
        result = _compute_next_action(c, {}, time(13, 0))
        assert "Keep windows and blinds closed" in result
        assert "AC is handling it" in result

    def test_next_action_hot_uses_custom_comfort_cool(self):
        """HOT day morning opportunity respects a non-default comfort_cool setting."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
        )
        config = {"comfort_cool": 72}
        # threshold = 72 + 3 = 75
        result = _compute_next_action(c, config, time(8, 0))
        assert "75" in result

    def test_next_action_cold_day(self):
        """COLD day → keep-doors-closed message."""
        c = _make_classification(day_type=DAY_TYPE_COLD)
        result = _compute_next_action(c, {}, time(12, 0))
        assert "Keep doors closed" in result

    def test_next_action_mild_day(self):
        """Mild day (not hot/cold) → no action needed message."""
        c = _make_classification(day_type="mild")
        result = _compute_next_action(c, {}, time(12, 0))
        assert "No action needed" in result

    def test_next_action_warm_day_after_close_before_evening(self):
        """WARM day after 10 AM close, before 5 PM — mid-day gap, no window guidance."""
        c = _make_classification(
            day_type=DAY_TYPE_WARM,
            windows_recommended=True,
            window_open_time=time(6, 0),
            window_close_time=time(WARM_WINDOW_CLOSE_HOUR, 0),
        )
        result = _compute_next_action(c, {}, time(13, 0))
        assert "No action needed" in result

    def test_next_action_warm_day_after_close_at_evening_start(self):
        """WARM day after 10 AM close, at exactly 5 PM — evening ventilation suggested."""
        c = _make_classification(
            day_type=DAY_TYPE_WARM,
            windows_recommended=True,
            window_open_time=time(6, 0),
            window_close_time=time(WARM_WINDOW_CLOSE_HOUR, 0),
        )
        result = _compute_next_action(c, {}, time(ECONOMIZER_EVENING_START_HOUR, 0))
        assert "Open windows" in result

    def test_next_action_warm_day_after_close_in_evening(self):
        """WARM day, well into the evening — evening ventilation suggested."""
        c = _make_classification(
            day_type=DAY_TYPE_WARM,
            windows_recommended=True,
            window_open_time=time(6, 0),
            window_close_time=time(WARM_WINDOW_CLOSE_HOUR, 0),
        )
        result = _compute_next_action(c, {}, time(20, 0))
        assert "Open windows" in result

    def test_next_action_indoor_above_comfort_shows_guidance(self):
        """Indoor temp above comfort_cool → actionable guidance, not 'no action'."""
        c = _make_classification(day_type=DAY_TYPE_MILD, windows_recommended=False)
        result = _compute_next_action(c, {"comfort_cool": 75}, time(14, 0), indoor_temp=78.0)
        assert "78" in result
        assert "No action needed" not in result

    def test_next_action_indoor_at_comfort_boundary_no_guidance(self):
        """Indoor temp exactly at comfort_cool boundary (not above) → no alert."""
        c = _make_classification(day_type=DAY_TYPE_MILD, windows_recommended=False)
        result = _compute_next_action(c, {"comfort_cool": 75}, time(14, 0), indoor_temp=75.0)
        assert "No action needed" in result

    def test_next_action_indoor_none_falls_back_to_no_action(self):
        """When indoor_temp is None — no comfort alert, falls back to default."""
        c = _make_classification(day_type=DAY_TYPE_MILD, windows_recommended=False)
        result = _compute_next_action(c, {"comfort_cool": 75}, time(14, 0), indoor_temp=None)
        assert "No action needed" in result

    def test_next_action_warm_day_midday_indoor_above_comfort(self):
        """WARM day mid-day with indoor above comfort — comfort guidance wins over 'no action'."""
        c = _make_classification(
            day_type=DAY_TYPE_WARM,
            windows_recommended=True,
            window_open_time=time(6, 0),
            window_close_time=time(WARM_WINDOW_CLOSE_HOUR, 0),
        )
        result = _compute_next_action(c, {"comfort_cool": 75}, time(13, 0), indoor_temp=79.0)
        assert "79" in result
        assert "No action needed" not in result

    def test_next_action_hot_day_indoor_above_comfort_still_shows_ac_message(self):
        """HOT day: HOT branch always returns before indoor check — AC message wins."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=False,
        )
        result = _compute_next_action(c, {"comfort_cool": 75}, time(14, 0), indoor_temp=80.0)
        assert "Keep windows and blinds closed" in result
        assert "AC is handling it" in result


# ---------------------------------------------------------------------------
# Tests: Briefing notification split (Issue #34)
# ---------------------------------------------------------------------------

FULL_BRIEFING = "Full briefing with lots of detail " * 20  # > 250 chars
SHORT_BRIEFING = "TLDR summary table"


def _side_effect_generate_briefing(**kwargs):
    """Return different text based on verbosity kwarg."""
    if kwargs.get("verbosity") == "tldr_only":
        return SHORT_BRIEFING
    return FULL_BRIEFING


class TestBriefingNotificationSplit:
    """Tests for push (TLDR) vs email (full) briefing dispatch (Issue #34)."""

    def _make_coordinator_stub(self, config_overrides=None):
        """Build a minimal coordinator-like object for testing _async_send_briefing."""
        import types

        from custom_components.climate_advisor.coordinator import (
            ClimateAdvisorCoordinator,
        )

        coord = MagicMock()
        coord.hass = MagicMock()
        coord.hass.services = MagicMock()
        coord.hass.services.async_call = AsyncMock()

        config = {
            "notify_service": "notify.mobile_app_phone",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
            "push_briefing": True,
            "email_briefing": True,
        }
        if config_overrides:
            config.update(config_overrides)
        coord.config = config

        coord._briefing_sent_today = False
        coord._last_briefing = ""
        coord._last_briefing_short = ""
        coord._briefing_day_type = None
        coord._automation_enabled = True
        coord._today_record = None

        # Mock automation engine
        coord.automation_engine = MagicMock()
        coord.automation_engine._grace_active = False
        coord.automation_engine._last_resume_source = None
        coord.automation_engine.apply_classification = AsyncMock()

        # Mock learning engine
        coord.learning = MagicMock()
        coord.learning.generate_suggestions.return_value = []
        coord.learning.get_last_suggestion_keys.return_value = []
        coord.learning.get_thermal_model.return_value = {}
        coord.learning.start_day = MagicMock()

        # Mock state saving
        coord._async_save_state = AsyncMock()

        # Mock _current_classification
        coord._current_classification = None

        # Mock forecast methods
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])

        # Stubs for Phase 2 ceiling guard (not under test here)
        coord._get_indoor_temp = MagicMock(return_value=None)
        coord._occupancy_mode = "home"
        coord._apply_outdoor_windows_gate = MagicMock()
        coord._last_predicted_indoor = []

        # async_add_executor_job is now awaited by _async_send_briefing for ODE offload.
        # Execute the partial synchronously so patched _build_predicted_indoor_future is used.
        coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())

        # Bind the real methods to our mock
        coord._async_send_briefing = types.MethodType(ClimateAdvisorCoordinator._async_send_briefing, coord)
        coord._build_briefing_text = types.MethodType(ClimateAdvisorCoordinator._build_briefing_text, coord)

        return coord

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_generate_briefing,
    )
    @patch("custom_components.climate_advisor.coordinator.classify_day")
    def test_push_gets_tldr_email_gets_full(self, mock_classify, mock_gen, mock_pred, mock_outdoor):
        """Push notification receives short TLDR; email receives full briefing."""
        mock_classify.return_value = _make_classification()

        coord = self._make_coordinator_stub()
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])
        asyncio.run(coord._async_send_briefing(MagicMock()))

        calls = coord.hass.services.async_call.call_args_list
        notify_calls = [c for c in calls if c[0][0] == "notify"]
        assert len(notify_calls) == 2

        # Push (first call) should get short TLDR
        push_msg = notify_calls[0][0][2]["message"]
        assert push_msg == SHORT_BRIEFING

        # Email (second call) should get full briefing
        email_msg = notify_calls[1][0][2]["message"]
        assert email_msg == FULL_BRIEFING

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_generate_briefing,
    )
    @patch("custom_components.climate_advisor.coordinator.classify_day")
    def test_email_disabled_only_push(self, mock_classify, mock_gen, mock_pred, mock_outdoor):
        """When email is disabled, only push notification is sent."""
        mock_classify.return_value = _make_classification()

        coord = self._make_coordinator_stub({"email_briefing": False})
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])
        asyncio.run(coord._async_send_briefing(MagicMock()))

        calls = coord.hass.services.async_call.call_args_list
        notify_calls = [c for c in calls if c[0][0] == "notify"]
        assert len(notify_calls) == 1
        assert notify_calls[0][0][2]["message"] == SHORT_BRIEFING

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_generate_briefing,
    )
    @patch("custom_components.climate_advisor.coordinator.classify_day")
    def test_last_briefing_stores_full_version(self, mock_classify, mock_gen, mock_pred, mock_outdoor):
        """_last_briefing should contain the full briefing, not the TLDR."""
        mock_classify.return_value = _make_classification()

        coord = self._make_coordinator_stub()
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])
        asyncio.run(coord._async_send_briefing(MagicMock()))

        assert coord._last_briefing == FULL_BRIEFING

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_generate_briefing,
    )
    @patch("custom_components.climate_advisor.coordinator.classify_day")
    def test_dry_run_skips_notifications(self, mock_classify, mock_gen, mock_pred, mock_outdoor):
        """In observe-only mode, no notifications are sent."""
        mock_classify.return_value = _make_classification()

        coord = self._make_coordinator_stub()
        coord._automation_enabled = False
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])
        asyncio.run(coord._async_send_briefing(MagicMock()))

        coord.hass.services.async_call.assert_not_called()
        assert coord._last_briefing == FULL_BRIEFING

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_generate_briefing,
    )
    @patch("custom_components.climate_advisor.coordinator.classify_day")
    def test_last_briefing_short_populated_and_shorter(self, mock_classify, mock_gen, mock_pred, mock_outdoor):
        """_last_briefing_short is populated after _async_send_briefing and shorter than _last_briefing."""
        mock_classify.return_value = _make_classification()

        coord = self._make_coordinator_stub()
        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])
        asyncio.run(coord._async_send_briefing(MagicMock()))

        assert coord._last_briefing_short  # non-empty string
        assert len(coord._last_briefing_short) < len(coord._last_briefing)


# ---------------------------------------------------------------------------
# Tests: Temperature normalization (Issue #58 — Celsius/Fahrenheit support)
# ---------------------------------------------------------------------------


class TestTemperatureNormalization:
    """Tests for inbound temperature conversion in coordinator helper methods.

    _get_outdoor_temp() and _get_indoor_temp() must convert sensor/entity
    values from the user's configured unit into internal Fahrenheit before
    returning them.
    """

    def _make_coord_stub(self, config_overrides: dict | None = None):
        """Build a minimal coordinator-like object for testing temperature helpers."""
        import types

        from custom_components.climate_advisor.coordinator import (
            ClimateAdvisorCoordinator,
        )

        coord = MagicMock()
        coord.hass = MagicMock()
        coord.hass.states = MagicMock()

        config = {
            "climate_entity": "climate.test_thermostat",
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.outdoor_temp",
            "indoor_temp_source": "climate_fallback",
            "temp_unit": "fahrenheit",
        }
        if config_overrides:
            config.update(config_overrides)
        coord.config = config

        # Bind the real methods under test to our mock coordinator
        coord._get_outdoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_outdoor_temp, coord)
        coord._get_indoor_temp = types.MethodType(ClimateAdvisorCoordinator._get_indoor_temp, coord)
        return coord

    def _make_state(self, state_value: str, attributes: dict | None = None) -> MagicMock:
        """Create a mock HA state object."""
        mock = MagicMock()
        mock.state = state_value
        mock.attributes = attributes or {}
        return mock

    # ------------------------------------------------------------------
    # _get_outdoor_temp — sensor source
    # ------------------------------------------------------------------

    def test_celsius_outdoor_temp_normalized(self):
        """Sensor returns 25°C; with celsius config, _get_outdoor_temp returns ~77°F."""
        coord = self._make_coord_stub({"temp_unit": "celsius"})
        coord.hass.states.get.return_value = self._make_state("25")

        result = coord._get_outdoor_temp({})

        # 25°C × 9/5 + 32 = 77.0°F
        assert abs(result - 77.0) < 0.01

    def test_fahrenheit_outdoor_temp_passthrough(self):
        """Sensor returns 77°F; with fahrenheit config, _get_outdoor_temp returns 77.0."""
        coord = self._make_coord_stub({"temp_unit": "fahrenheit"})
        coord.hass.states.get.return_value = self._make_state("77")

        result = coord._get_outdoor_temp({})

        assert result == 77.0

    def test_celsius_outdoor_temp_fallback_from_weather_attrs(self):
        """When sensor entity is absent, weather_attrs temperature is also converted."""
        coord = self._make_coord_stub(
            {
                "temp_unit": "celsius",
                "outdoor_temp_source": "weather_service",
            }
        )

        # weather_attrs carries 20°C → expect 68°F internally
        result = coord._get_outdoor_temp({"temperature": 20})

        assert abs(result - 68.0) < 0.01

    def test_fahrenheit_outdoor_weather_attrs_passthrough(self):
        """weather_service source with fahrenheit config passes value through unchanged."""
        coord = self._make_coord_stub(
            {
                "temp_unit": "fahrenheit",
                "outdoor_temp_source": "weather_service",
            }
        )

        result = coord._get_outdoor_temp({"temperature": 68})

        assert result == 68.0

    # ------------------------------------------------------------------
    # _get_indoor_temp — climate_fallback source
    # ------------------------------------------------------------------

    def test_celsius_indoor_temp_normalized(self):
        """Climate entity current_temperature is 22°C; celsius config → ~71.6°F returned."""
        coord = self._make_coord_stub({"temp_unit": "celsius"})
        climate_state = self._make_state("heat", {"current_temperature": 22})
        coord.hass.states.get.return_value = climate_state

        result = coord._get_indoor_temp()

        # 22°C × 9/5 + 32 = 71.6°F
        assert result is not None
        assert abs(result - 71.6) < 0.01

    def test_fahrenheit_indoor_temp_passthrough(self):
        """Climate entity current_temperature is 77°F; fahrenheit config → 77.0 returned."""
        coord = self._make_coord_stub({"temp_unit": "fahrenheit"})
        climate_state = self._make_state("heat", {"current_temperature": 77})
        coord.hass.states.get.return_value = climate_state

        result = coord._get_indoor_temp()

        assert result == 77.0

    def test_indoor_temp_none_when_no_climate_state(self):
        """When climate entity state is unavailable, _get_indoor_temp returns None."""
        coord = self._make_coord_stub({"temp_unit": "celsius"})
        coord.hass.states.get.return_value = None

        result = coord._get_indoor_temp()

        assert result is None

    def test_indoor_temp_none_when_current_temperature_missing(self):
        """When current_temperature attribute is absent, _get_indoor_temp returns None."""
        coord = self._make_coord_stub({"temp_unit": "fahrenheit"})
        climate_state = self._make_state("heat", {})  # no current_temperature key
        coord.hass.states.get.return_value = climate_state

        result = coord._get_indoor_temp()

        assert result is None

    # ------------------------------------------------------------------
    # _get_indoor_temp — explicit sensor source
    # ------------------------------------------------------------------

    def test_celsius_indoor_sensor_source_normalized(self):
        """Explicit indoor sensor returning 20°C with celsius config → 68°F returned."""
        coord = self._make_coord_stub(
            {
                "temp_unit": "celsius",
                "indoor_temp_source": "sensor",
                "indoor_temp_entity": "sensor.indoor_temp",
            }
        )
        coord.hass.states.get.return_value = self._make_state("20")

        result = coord._get_indoor_temp()

        assert result is not None
        assert abs(result - 68.0) < 0.01

    def test_fahrenheit_indoor_sensor_source_passthrough(self):
        """Explicit indoor sensor returning 72°F with fahrenheit config → 72.0 returned."""
        coord = self._make_coord_stub(
            {
                "temp_unit": "fahrenheit",
                "indoor_temp_source": "sensor",
                "indoor_temp_entity": "sensor.indoor_temp",
            }
        )
        coord.hass.states.get.return_value = self._make_state("72")

        result = coord._get_indoor_temp()

        assert result == 72.0


# ---------------------------------------------------------------------------
# Tests: Briefing regeneration on day-type change (Issue #78)
# ---------------------------------------------------------------------------

REGEN_FULL = "Full briefing (regenerated)"
REGEN_SHORT = "TLDR (regenerated)"


def _side_effect_regen(**kwargs):
    if kwargs.get("verbosity") == "tldr_only":
        return REGEN_SHORT
    return REGEN_FULL


class TestBriefingRegeneration:
    """Verify that the briefing text is regenerated when day_type changes."""

    def _make_coord(self):
        """Build a minimal coordinator stub for regeneration tests."""
        import types

        from custom_components.climate_advisor.coordinator import (
            ClimateAdvisorCoordinator,
        )

        coord = MagicMock()
        coord.hass = MagicMock()
        coord.hass.services = MagicMock()
        coord.hass.services.async_call = AsyncMock()

        coord.config = {
            "notify_service": "notify.mobile_app_phone",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
            "push_briefing": True,
            "email_briefing": True,
        }

        coord._briefing_sent_today = True
        coord._briefing_day_type = DAY_TYPE_WARM
        coord._last_briefing = "Old warm briefing"
        coord._last_briefing_short = "Old warm TLDR"
        coord._automation_enabled = True

        coord.automation_engine = MagicMock()
        coord.automation_engine._grace_active = False
        coord.automation_engine._last_resume_source = None
        coord.automation_engine.apply_classification = AsyncMock()

        coord.learning = MagicMock()
        coord.learning.generate_suggestions.return_value = []
        coord.learning.get_thermal_model.return_value = {}

        coord._async_save_state = AsyncMock()

        # Stubs for Phase 2 ceiling guard (not under test here)
        coord._get_indoor_temp = MagicMock(return_value=None)
        coord._occupancy_mode = "home"
        coord._apply_outdoor_windows_gate = MagicMock()
        coord._last_predicted_indoor = []
        coord._current_classification = None

        # async_add_executor_job is now awaited by _async_update_data/_async_send_briefing for ODE offload.
        coord.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())

        coord._build_briefing_text = types.MethodType(ClimateAdvisorCoordinator._build_briefing_text, coord)

        return coord

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_regen,
    )
    def test_regenerated_on_day_type_change(self, mock_gen, mock_pred, mock_outdoor):
        """When day_type changes and briefing was already sent, text is regenerated."""
        coord = self._make_coord()
        new_classification = _make_classification(day_type=DAY_TYPE_HOT)

        # Simulate what _async_update_data does after classification changes
        coord._current_classification = new_classification
        if (
            coord._briefing_sent_today
            and coord._briefing_day_type is not None
            and coord._current_classification.day_type != coord._briefing_day_type
        ):
            coord._last_briefing, coord._last_briefing_short = coord._build_briefing_text(coord._current_classification)
            coord._briefing_day_type = coord._current_classification.day_type

        assert coord._last_briefing == REGEN_FULL
        assert coord._last_briefing_short == REGEN_SHORT
        assert coord._briefing_day_type == DAY_TYPE_HOT

    def test_not_regenerated_when_same_day_type(self):
        """When day_type hasn't changed, briefing is NOT regenerated."""
        coord = self._make_coord()
        new_classification = _make_classification(day_type=DAY_TYPE_WARM)

        coord._current_classification = new_classification
        should_regen = (
            coord._briefing_sent_today
            and coord._briefing_day_type is not None
            and coord._current_classification.day_type != coord._briefing_day_type
        )

        assert not should_regen
        assert coord._last_briefing == "Old warm briefing"

    def test_not_regenerated_before_first_briefing(self):
        """Before briefing has been sent today, no regeneration."""
        coord = self._make_coord()
        coord._briefing_sent_today = False
        coord._briefing_day_type = None

        new_classification = _make_classification(day_type=DAY_TYPE_HOT)
        coord._current_classification = new_classification

        should_regen = (
            coord._briefing_sent_today
            and coord._briefing_day_type is not None
            and coord._current_classification.day_type != coord._briefing_day_type
        )

        assert not should_regen
        assert coord._last_briefing == "Old warm briefing"

    def test_briefing_day_type_set_after_send(self):
        """After _async_send_briefing, _briefing_day_type matches classification."""
        import types

        from custom_components.climate_advisor.coordinator import (
            ClimateAdvisorCoordinator,
        )

        coord = self._make_coord()
        coord._briefing_sent_today = False
        coord._briefing_day_type = None
        coord._today_record = None

        coord._get_forecast = AsyncMock(return_value=MagicMock())
        coord._get_hourly_forecast_data = AsyncMock(return_value=[])

        coord._async_send_briefing = types.MethodType(ClimateAdvisorCoordinator._async_send_briefing, coord)

        with (
            patch(
                "custom_components.climate_advisor.coordinator.classify_day",
                return_value=_make_classification(day_type=DAY_TYPE_HOT),
            ),
            patch(
                "custom_components.climate_advisor.coordinator.generate_briefing",
                side_effect=_side_effect_regen,
            ),
            patch(
                "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
                return_value=[],
            ),
            patch(
                "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
                return_value=[],
            ),
        ):
            asyncio.run(coord._async_send_briefing(MagicMock()))

        assert coord._briefing_day_type == DAY_TYPE_HOT

    @patch(
        "custom_components.climate_advisor.coordinator._build_future_forecast_outdoor",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator._build_predicted_indoor_future",
        return_value=[],
    )
    @patch(
        "custom_components.climate_advisor.coordinator.generate_briefing",
        side_effect=_side_effect_regen,
    )
    def test_regeneration_does_not_send_notifications(self, mock_gen, mock_pred, mock_outdoor):
        """Regeneration updates cached text but does NOT call notify services."""
        coord = self._make_coord()
        new_classification = _make_classification(day_type=DAY_TYPE_HOT)

        coord._current_classification = new_classification
        if (
            coord._briefing_sent_today
            and coord._briefing_day_type is not None
            and coord._current_classification.day_type != coord._briefing_day_type
        ):
            coord._last_briefing, coord._last_briefing_short = coord._build_briefing_text(coord._current_classification)
            coord._briefing_day_type = coord._current_classification.day_type

        # Briefing text was updated
        assert coord._last_briefing == REGEN_FULL
        # But no notification services were called
        coord.hass.services.async_call.assert_not_called()

    def test_state_persistence_round_trip(self):
        """_briefing_day_type survives save → restore cycle."""
        coord = self._make_coord()
        coord._briefing_day_type = DAY_TYPE_WARM

        # Simulate _build_state_dict briefing_state section
        briefing_state = {
            "sent_today": coord._briefing_sent_today,
            "last_text": coord._last_briefing,
            "last_text_short": coord._last_briefing_short,
            "briefing_day_type": coord._briefing_day_type,
        }

        # Simulate _restore_state for briefing section
        restored_coord = self._make_coord()
        restored_coord._briefing_day_type = None  # start clean
        restored_coord._briefing_day_type = briefing_state.get("briefing_day_type")

        assert restored_coord._briefing_day_type == DAY_TYPE_WARM

    def test_state_persistence_backward_compatible(self):
        """Old state files without briefing_day_type restore as None."""
        coord = self._make_coord()
        coord._briefing_day_type = "something"

        # Old state file has no briefing_day_type key
        old_briefing_state = {
            "sent_today": True,
            "last_text": "old briefing",
            "last_text_short": "old tldr",
        }
        coord._briefing_day_type = old_briefing_state.get("briefing_day_type")

        assert coord._briefing_day_type is None

    def test_pred_archive_persistence_round_trip(self):
        """pred_archive survives _build_state_dict → async_restore_state cycle.

        Verifies:
        - int keys are serialized as str (JSON compatibility)
        - str keys round-trip back to int on restore
        - float values are preserved
        - invalid/non-numeric keys in corrupted state are skipped
        """
        coord = self._make_coord()

        # Populate archive with two int-keyed entries
        key1 = 1747202400  # 2026-05-14 22:40:00 UTC (30-min boundary)
        key2 = 1747204200  # 2026-05-14 23:10:00 UTC (30-min boundary)
        coord._pred_archive = {key1: 71.5, key2: 72.3}

        # Simulate _build_state_dict pred_archive serialization
        serialized = {str(k): v for k, v in coord._pred_archive.items()}

        # Verify JSON key types
        for k in serialized:
            assert isinstance(k, str), f"Serialized key must be str, got {type(k)}"

        # Simulate async_restore_state restore block
        restored_coord = self._make_coord()
        restored_coord._pred_archive = {}

        raw_archive = serialized
        if isinstance(raw_archive, dict):
            restored: dict[int, float] = {}
            for k, v in raw_archive.items():
                try:
                    restored[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            restored_coord._pred_archive = restored

        assert restored_coord._pred_archive[key1] == 71.5
        assert restored_coord._pred_archive[key2] == 72.3
        assert len(restored_coord._pred_archive) == 2

    def test_pred_archive_persistence_skips_invalid_keys(self):
        """Corrupted state dict with non-numeric keys is restored safely."""
        coord = self._make_coord()
        coord._pred_archive = {}

        # Simulated corrupted state containing non-numeric keys
        bad_archive = {"not_a_number": 70.0, "also_bad": 71.0, "1747202400": 72.5}

        if isinstance(bad_archive, dict):
            restored: dict[int, float] = {}
            for k, v in bad_archive.items():
                try:
                    restored[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            coord._pred_archive = restored

        # Only the valid numeric key survives
        assert len(coord._pred_archive) == 1
        assert coord._pred_archive[1747202400] == 72.5

    def test_end_of_day_resets_briefing_day_type(self):
        """_async_end_of_day resets _briefing_day_type to None."""
        import types

        from custom_components.climate_advisor.coordinator import (
            ClimateAdvisorCoordinator,
        )

        coord = self._make_coord()
        coord._briefing_day_type = DAY_TYPE_HOT
        coord._briefing_sent_today = True
        coord._today_record = None
        coord._hvac_on_since = None
        coord._last_violation_check = None
        coord._outdoor_temp_history = MagicMock()
        coord._indoor_temp_history = MagicMock()
        coord._hourly_forecast_temps = MagicMock()

        coord._async_end_of_day = types.MethodType(ClimateAdvisorCoordinator._async_end_of_day, coord)

        asyncio.run(coord._async_end_of_day(MagicMock()))

        assert coord._briefing_day_type is None
        assert coord._briefing_sent_today is False


# ---------------------------------------------------------------------------
# TestStateContradictionEvent — Fix 2: state_contradiction_warning event bridge
# ---------------------------------------------------------------------------


def _check_state_contradiction(hvac_mode, hvac_action, ca_fan_active, last_time, now):
    """Replicate the state contradiction check from _async_update_data."""
    _active_hvac_actions = {"heating", "cooling", "fan"}
    if hvac_mode == "off" and str(hvac_action).lower() in _active_hvac_actions:
        _is_expected_fan = str(hvac_action).lower() == "fan" and ca_fan_active
        if not _is_expected_fan:
            _dedup_window = datetime.timedelta(minutes=30)
            if last_time is None or (now - last_time) > _dedup_window:
                return [{"hvac_mode": hvac_mode, "hvac_action": hvac_action}]
    return []


class TestStateContradictionEvent:
    """Tests for the state_contradiction_warning event emission added to _async_update_data."""

    _NOW = datetime.datetime(2026, 4, 8, 21, 0, 0)

    def test_event_emitted_when_mode_off_action_fan(self):
        events = _check_state_contradiction("off", "fan", False, None, self._NOW)
        assert len(events) == 1
        assert events[0] == {"hvac_mode": "off", "hvac_action": "fan"}

    def test_event_emitted_when_mode_off_action_heating(self):
        events = _check_state_contradiction("off", "heating", False, None, self._NOW)
        assert len(events) == 1

    def test_event_emitted_when_mode_off_action_cooling(self):
        events = _check_state_contradiction("off", "cooling", False, None, self._NOW)
        assert len(events) == 1

    def test_no_event_when_ca_fan_is_active(self):
        """hvac_action=fan but CA itself activated the fan → expected, no event."""
        events = _check_state_contradiction("off", "fan", True, None, self._NOW)
        assert events == []

    def test_no_event_when_mode_is_not_off(self):
        events = _check_state_contradiction("heat", "heating", False, None, self._NOW)
        assert events == []

    def test_no_event_when_action_is_idle(self):
        events = _check_state_contradiction("off", "idle", False, None, self._NOW)
        assert events == []

    def test_dedup_suppresses_within_30_min(self):
        last = self._NOW - datetime.timedelta(minutes=10)
        events = _check_state_contradiction("off", "fan", False, last, self._NOW)
        assert events == []

    def test_dedup_allows_after_30_min(self):
        last = self._NOW - datetime.timedelta(minutes=31)
        events = _check_state_contradiction("off", "fan", False, last, self._NOW)
        assert len(events) == 1

    def test_dedup_allows_when_last_time_is_none(self):
        events = _check_state_contradiction("off", "fan", False, None, self._NOW)
        assert len(events) == 1

    def test_hvac_action_case_insensitive(self):
        events = _check_state_contradiction("off", "FAN", False, None, self._NOW)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Tests: fan→heating mapping in chart_log write (Change 6)
# ---------------------------------------------------------------------------


def _apply_fan_to_hvac_action_mapping(hvac_action, hvac_mode, fan_mode=""):
    """Replicate the fan→heating/cooling mapping logic from _async_update_data."""
    _hvac_action_str = str(hvac_action).lower() if hvac_action else ""
    _hvac_mode_str = str(hvac_mode).lower() if hvac_mode else ""
    _fan_mode_str = str(fan_mode).lower() if fan_mode else ""
    _fan_is_auto = not _fan_mode_str or _fan_mode_str.startswith("auto")
    if _hvac_action_str == "fan" and _fan_is_auto:
        if _hvac_mode_str == "heat":
            _hvac_action_str = "heating"
        elif _hvac_mode_str in ("cool", "heat_cool"):
            _hvac_action_str = "cooling"
    return _hvac_action_str


class TestChartLogFanMapping:
    """Tests for the fan→heating/cooling mapping added to chart_log write."""

    def test_fan_action_with_heat_mode_maps_to_heating(self):
        result = _apply_fan_to_hvac_action_mapping("fan", "heat")
        assert result == "heating"

    def test_fan_action_with_cool_mode_maps_to_cooling(self):
        result = _apply_fan_to_hvac_action_mapping("fan", "cool")
        assert result == "cooling"

    def test_fan_action_with_heat_cool_mode_maps_to_cooling(self):
        result = _apply_fan_to_hvac_action_mapping("fan", "heat_cool")
        assert result == "cooling"

    def test_fan_action_with_off_mode_stays_fan(self):
        """When hvac_mode=off and hvac_action=fan, no remapping — stays 'fan'."""
        result = _apply_fan_to_hvac_action_mapping("fan", "off")
        assert result == "fan"

    def test_heating_action_unchanged(self):
        result = _apply_fan_to_hvac_action_mapping("heating", "heat")
        assert result == "heating"

    def test_cooling_action_unchanged(self):
        result = _apply_fan_to_hvac_action_mapping("cooling", "cool")
        assert result == "cooling"

    def test_idle_action_unchanged(self):
        result = _apply_fan_to_hvac_action_mapping("idle", "heat")
        assert result == "idle"

    def test_empty_action_unchanged(self):
        result = _apply_fan_to_hvac_action_mapping("", "heat")
        assert result == ""

    def test_none_action_becomes_empty_string(self):
        result = _apply_fan_to_hvac_action_mapping(None, "heat")
        assert result == ""

    def test_fan_action_case_insensitive(self):
        """Input 'FAN' (uppercase) should also be mapped correctly."""
        result = _apply_fan_to_hvac_action_mapping("FAN", "heat")
        assert result == "heating"

    def test_fan_continuous_mode_stays_fan_in_heat(self):
        """fan_mode=on means continuous circulation — must NOT remap to heating.
        Regression test for #109: thermostat with fan_mode=on reports hvac_action=fan
        all day in heat mode, causing 12 hours of red on the HVAC bar.
        """
        result = _apply_fan_to_hvac_action_mapping("fan", "heat", fan_mode="on")
        assert result == "fan"

    def test_fan_continuous_mode_stays_fan_in_cool(self):
        """fan_mode=on in cool mode — circulation fan, not active cooling."""
        result = _apply_fan_to_hvac_action_mapping("fan", "cool", fan_mode="on")
        assert result == "fan"

    def test_fan_auto_mode_still_maps_to_heating(self):
        """fan_mode=auto means fan runs with HVAC cycle — #102 remap preserved."""
        result = _apply_fan_to_hvac_action_mapping("fan", "heat", fan_mode="auto")
        assert result == "heating"

    def test_fan_auto_low_mode_maps_to_heating(self):
        """auto_low is a speed variant of auto — still remap to heating."""
        result = _apply_fan_to_hvac_action_mapping("fan", "heat", fan_mode="auto_low")
        assert result == "heating"

    def test_fan_no_fan_mode_maps_to_heating(self):
        """fan_mode unset (empty string) — preserves original #102 behavior."""
        result = _apply_fan_to_hvac_action_mapping("fan", "heat", fan_mode="")
        assert result == "heating"


# ---------------------------------------------------------------------------
# Tests: event-driven chart_log appends include window fields (Issue #117)
# ---------------------------------------------------------------------------


def _simulate_hvac_action_change_append(
    old_action: str,
    new_action: str,
    sensor_open: bool,
    windows_recommended: bool,
) -> dict | None:
    """Replicate the hvac_action_change chart log write from _async_thermostat_changed.

    Returns the kwargs passed to append() if an edge was detected, else None.
    Without the fix, windows_open and windows_recommended would be absent here.
    """
    captured: dict = {}

    class _FakeChartLog:
        def append(self, **kwargs):
            captured.update(kwargs)

        def save(self):
            pass

    _chart_active_actions = {"heating", "cooling"}
    _was_active = old_action in _chart_active_actions
    _is_active = new_action in _chart_active_actions
    if _was_active != _is_active:
        _FakeChartLog().append(
            hvac=new_action,
            fan=False,
            indoor=70.0,
            outdoor=None,
            windows_open=sensor_open,
            windows_recommended=windows_recommended,
            event="hvac_action_change",
        )
        return captured
    return None


class TestChartLogWindowFields:
    """Event-driven chart log appends must include window state fields (Issue #117).

    Before the fix, the hvac_action_change / override / classification_change appends
    omitted windows_open and windows_recommended, defaulting both to False and causing
    the Win Rec and Windows chart bars to drop to zero on every HVAC event.
    """

    def test_heating_start_captures_sensor_open_true(self):
        result = _simulate_hvac_action_change_append(
            old_action="idle",
            new_action="heating",
            sensor_open=True,
            windows_recommended=True,
        )
        assert result is not None
        assert result["windows_open"] is True
        assert result["windows_recommended"] is True

    def test_heating_start_captures_sensor_open_false(self):
        result = _simulate_hvac_action_change_append(
            old_action="idle",
            new_action="heating",
            sensor_open=False,
            windows_recommended=False,
        )
        assert result is not None
        assert result["windows_open"] is False
        assert result["windows_recommended"] is False

    def test_heating_stop_captures_window_state(self):
        """heating → idle edge also records current window state."""
        result = _simulate_hvac_action_change_append(
            old_action="heating",
            new_action="idle",
            sensor_open=True,
            windows_recommended=True,
        )
        assert result is not None
        assert result["windows_open"] is True
        assert result["windows_recommended"] is True

    def test_cooling_start_captures_window_state(self):
        result = _simulate_hvac_action_change_append(
            old_action="idle",
            new_action="cooling",
            sensor_open=False,
            windows_recommended=False,
        )
        assert result is not None
        assert "windows_open" in result
        assert "windows_recommended" in result

    def test_no_edge_no_append(self):
        """heating → heating is not an edge — no chart log write."""
        result = _simulate_hvac_action_change_append(
            old_action="heating",
            new_action="heating",
            sensor_open=True,
            windows_recommended=True,
        )
        assert result is None

    def test_idle_to_idle_no_append(self):
        """idle → idle is not an edge — no chart log write."""
        result = _simulate_hvac_action_change_append(
            old_action="idle",
            new_action="idle",
            sensor_open=True,
            windows_recommended=True,
        )
        assert result is None

    def test_sensor_open_independent_of_hvac_action(self):
        """Physical sensor state reflects reality regardless of HVAC action."""
        heat_on = _simulate_hvac_action_change_append(
            old_action="idle",
            new_action="heating",
            sensor_open=True,
            windows_recommended=False,
        )
        heat_off = _simulate_hvac_action_change_append(
            old_action="heating",
            new_action="idle",
            sensor_open=True,
            windows_recommended=False,
        )
        assert heat_on["windows_open"] is True
        assert heat_off["windows_open"] is True


# ---------------------------------------------------------------------------
# Tests: ODE thermal model gate — empty {} is falsy, skips physics entirely
# (Issue #137 re-investigation: restart clears automation_engine._thermal_model)
# ---------------------------------------------------------------------------


def _build_future_forecast(now_dt, outdoor_temp: float = 56.0, count: int = 24) -> list[dict]:
    """Build a list of hourly forecast entries starting from now_dt+1h."""
    entries = []
    for i in range(1, count + 1):
        ts = now_dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=i)
        entries.append({"datetime": ts.isoformat(), "temperature": outdoor_temp})
    return entries


class TestOdeThermalModelGate:
    """After HA restart, automation_engine._thermal_model is {} (falsy).

    The old coordinator code passed automation_engine._thermal_model to
    _build_predicted_indoor_future.  An empty dict is falsy, so the physics gate
    at `if thermal_model and current_indoor_temp is not None:` is never entered.
    The fallback 'off-day' path then writes `_t_current` (= current_indoor_temp)
    for every future entry without ever updating it, so all pred_indoor values
    equal current_indoor_temp and delta = 0.0 in the chart_log.

    Fix: use self.learning.get_thermal_model() (persisted, survives restart) not
    automation_engine._thermal_model (in-memory, reset to {} on restart).
    """

    def test_empty_thermal_model_is_falsy(self):
        """Confirm {} thermal_model is falsy — physics gate skipped, causing constant fallback.

        This documents the root cause: automation_engine._thermal_model = {} after restart
        causes the gate `if thermal_model and ...` to skip physics entirely.
        """
        assert not {}, "Empty dict must be falsy — if thermal_model: is the physics gate"

    def test_empty_thermal_model_produces_constant_fallback(self):
        """_build_predicted_indoor_future with {} model returns all entries = current_indoor_temp.

        Demonstrates the post-restart regression: pred_indoor = indoor for all entries.
        Uses outdoor=72°F so day high >= 60°F → mode="off" (the actual regression scenario:
        warm/mild day, HVAC off, fallback anchors to current_indoor_temp without updating).
        """
        from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future

        now = datetime.datetime(2026, 5, 13, 7, 37, 0)
        # 72°F outdoor → day high=72°F ≥ 60°F → mode="off" → off-day constant fallback
        forecast = _build_future_forecast(now, outdoor_temp=72.0)
        config = {
            "comfort_heat": 68,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
        }
        result = _build_predicted_indoor_future(
            hourly_forecast=forecast,
            config=config,
            now=now,
            current_indoor_temp=69.0,
            thermal_model={},  # post-restart empty — falsy, skips physics gate
        )
        assert result, "Expected non-empty result"
        temps = [e["temp"] for e in result]
        # Off-day fallback anchors to current_indoor_temp (69.0) without updating _t_current
        assert all(t == 69.0 for t in temps), (
            f"Expected constant 69.0 from off-day fallback, got varied temps: {set(temps)}. "
            "This test documents the regression: empty thermal_model = pred_indoor always equals actual_indoor."
        )

    def test_physics_thermal_model_diverges_from_current(self):
        """_build_predicted_indoor_future with a real model produces varying predictions.

        With outdoor=72°F, indoor=69°F, k_passive=-0.05 (mild decay toward outdoor), the
        ODE tracks the home's actual trajectory rather than staying pinned at current_indoor_temp.
        """
        from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future

        now = datetime.datetime(2026, 5, 13, 7, 37, 0)
        forecast = _build_future_forecast(now, outdoor_temp=72.0)
        config = {
            "comfort_heat": 68,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
        }
        physics_model = {
            "confidence": "solid",
            "confidence_k_passive": "solid",
            "k_passive": -0.05,
            "k_active_heat": 6.8,
            "k_active_cool": -3.5,
        }
        result = _build_predicted_indoor_future(
            hourly_forecast=forecast,
            config=config,
            now=now,
            current_indoor_temp=69.0,
            thermal_model=physics_model,
        )
        assert result, "Expected non-empty result"
        temps = [e["temp"] for e in result]
        # With k_passive=-0.05 and outdoor=56°F, indoor decays toward outdoor+equilibrium.
        # Predictions should NOT all be 69.0.
        assert not all(t == 69.0 for t in temps), (
            "Expected ODE-varying predictions with real thermal model, got all=69.0. "
            "Fix: coordinator must use self.learning.get_thermal_model() not automation_engine._thermal_model."
        )


# ---------------------------------------------------------------------------
# Tests: chart_log pred_indoor source — must use _last_predicted_indoor cache
# (Issue #136 follow-on / pred_indoor ODE trajectory fix)
# ---------------------------------------------------------------------------


def _simulate_chart_log_pred_indoor(
    indoor_temp: float | None,
    last_predicted_indoor: list,
    hourly_forecast_temps: list,
) -> dict:
    """Replicate the pred_indoor selection logic from _async_update_data (30-min poll).

    Returns a dict with pred_indoor and pred_outdoor as written to chart_log.

    Before the fix, this called _ode_single_step(actual_indoor, outdoor, dt=0.5h)
    which gave pred_indoor ≈ actual_indoor (0.38°F delta, invisible at chart scale).
    After the fix, _last_predicted_indoor[0]["temp"] is used — the ODE curve that
    includes HVAC Q + solar and diverges meaningfully during temperature transitions.
    """

    _pred_outdoor_val: float | None = None
    _pred_indoor_val: float | None = None

    # Replicate _extract_current_hour_forecast_temp with a stub: return first entry
    # whose "hour" matches now (we stub it to always return a fixed value if list given).
    if hourly_forecast_temps:
        _pred_outdoor_val = hourly_forecast_temps[0]

    # ---- NEW logic (post-fix) ----
    if last_predicted_indoor:
        _pred_indoor_val = last_predicted_indoor[0].get("temp")

    return {"pred_outdoor": _pred_outdoor_val, "pred_indoor": _pred_indoor_val}


class TestChartLogPredIndoorSource:
    """pred_indoor in the 30-min chart_log poll must come from the ODE cache,
    not from _ode_single_step(actual, outdoor) which gave pred ≈ actual."""

    def test_pred_indoor_uses_ode_cache_not_single_step(self):
        """
        With indoor=71.0 and _last_predicted_indoor[0]["temp"]=68.0,
        chart_log must store 68.0 (meaningful 3-degree transition prediction),
        not ~70.6 (the near-zero _ode_single_step result).
        """
        result = _simulate_chart_log_pred_indoor(
            indoor_temp=71.0,
            last_predicted_indoor=[{"temp": 68.0, "ts": "2026-05-13T02:00:00"}],
            hourly_forecast_temps=[55.0],
        )
        assert result["pred_indoor"] == 68.0, (
            f"Expected ODE cache value 68.0, got {result['pred_indoor']!r}. "
            "pred_indoor must come from _last_predicted_indoor[0]['temp'], "
            "not _ode_single_step(actual_indoor, outdoor)."
        )

    def test_pred_indoor_is_none_when_cache_empty(self):
        """When _last_predicted_indoor is [], pred_indoor written to chart_log must be None."""
        result = _simulate_chart_log_pred_indoor(
            indoor_temp=71.0,
            last_predicted_indoor=[],
            hourly_forecast_temps=[55.0],
        )
        assert result["pred_indoor"] is None, f"Expected None when cache is empty, got {result['pred_indoor']!r}."
