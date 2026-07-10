"""Tests for status pane improvements (Issue #18b / #23).

Tests for:
- _compute_automation_status logic
- _compute_next_automation_action logic
- ClimateAdvisorNextActionSensor name rename
"""

from __future__ import annotations

import sys
from datetime import date, time
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
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


def _make_automation_engine(
    *,
    is_paused_by_door: bool = False,
    natural_vent_active: bool = False,
    grace_active: bool = False,
    last_resume_source: str | None = None,
) -> MagicMock:
    """Create a mock AutomationEngine with given state flags.

    Also defaults the newer gates _compute_automation_status() checks
    (override-confirm pending, stuck-grace detection, resumed-from-pause) to
    inactive so callers exercising the original 4-flag surface see the same
    "active"/"disabled"/"paused"/"grace period" outcomes as before.
    """
    ae = MagicMock()
    ae.is_paused_by_door = is_paused_by_door
    ae.natural_vent_active = natural_vent_active
    ae._grace_active = grace_active
    ae._last_resume_source = last_resume_source
    ae._manual_override_active = False
    ae._override_confirm_pending = False
    ae._grace_end_time = None
    ae._resumed_from_pause = False
    ae._is_within_planned_window_period = MagicMock(return_value=False)
    return ae


def _make_real_coordinator(automation_enabled: bool, automation_engine, occupancy_mode: str = "home"):
    """Build a bare ClimateAdvisorCoordinator bound to the real status-computation methods.

    Uses object.__new__() + types.MethodType() (the established coordinator
    partial-instantiation pattern — see test_daily_record_accuracy.py) rather than
    replicating the method bodies, so these tests exercise the real
    ClimateAdvisorCoordinator._compute_automation_status/_compute_next_automation_action.
    """
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord._automation_enabled = automation_enabled
    coord._startup_coalesce_active = False
    coord._startup_coalesce_expiry = None
    coord._startup_timer_fired = False
    coord._current_classification = None
    coord._occupancy_mode = occupancy_mode
    coord.automation_engine = automation_engine
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._door_open_timers = {}
    coord._door_open_timer_expiry = {}
    coord._pre_cool_trigger_dt = None
    coord._pre_cool_target = None
    coord.config = {}
    coord._compute_automation_status = types.MethodType(ClimateAdvisorCoordinator._compute_automation_status, coord)
    coord._compute_next_automation_action = types.MethodType(
        ClimateAdvisorCoordinator._compute_next_automation_action, coord
    )
    return coord


def _compute_automation_status(automation_enabled: bool, automation_engine) -> str:
    """Call the real ClimateAdvisorCoordinator._compute_automation_status()."""
    coord = _make_real_coordinator(automation_enabled, automation_engine)
    return coord._compute_automation_status()


def _compute_next_automation_action(
    c,
    automation_engine,
    config: dict,
    now_time: time,
) -> tuple[str, str]:
    """Call the real ClimateAdvisorCoordinator._compute_next_automation_action().

    now_time (a plain time-of-day) is combined with a fixed date and patched in
    as dt_util.now()/as_local() — the real method now works in full datetimes
    (to correctly order cross-midnight events like pre-cool), not bare times.
    """
    from datetime import date, datetime
    from unittest.mock import patch

    from custom_components.climate_advisor import coordinator as _coord_mod

    coord = _make_real_coordinator(True, automation_engine)
    coord.config = config

    now_dt = datetime.combine(date(2026, 7, 10), now_time)
    with (
        patch.object(_coord_mod.dt_util, "now", return_value=now_dt),
        patch.object(_coord_mod.dt_util, "as_local", side_effect=lambda x: x),
    ):
        return coord._compute_next_automation_action(c)


# ---------------------------------------------------------------------------
# Tests: _compute_automation_status
# ---------------------------------------------------------------------------


class TestComputeAutomationStatus:
    """Tests for _compute_automation_status logic."""

    def test_automation_status_active(self):
        """No pause, no grace → 'active'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(True, ae)
        assert result == "active"

    def test_automation_status_paused_by_door(self):
        """paused_by_door=True → 'paused — door/window open'."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(True, ae)
        assert result == "paused — door/window open"

    def test_automation_status_grace_period(self):
        """grace_active=True → contains 'grace period'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "manual" in result

    def test_automation_status_grace_period_no_source(self):
        """grace_active=True with no resume source → defaults to 'automation'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source=None)
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "automation" in result

    def test_automation_status_disabled(self):
        """_automation_enabled=False → 'disabled'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(False, ae)
        assert result == "disabled"

    def test_disabled_takes_priority_over_paused(self):
        """Disabled overrides paused state."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(False, ae)
        assert result == "disabled"


# ---------------------------------------------------------------------------
# Tests: _compute_next_automation_action
# ---------------------------------------------------------------------------


class TestComputeNextAutomationAction:
    """Tests for _compute_next_automation_action logic."""

    def test_no_classification_returns_waiting(self):
        """When classification is None → 'Waiting for classification...'"""
        ae = _make_automation_engine()
        action, t = _compute_next_automation_action(None, ae, {}, time(8, 0))
        assert action == "Waiting for classification..."
        assert t == ""

    def test_paused_by_door_returns_waiting_message(self):
        """When paused_by_door → returns paused message regardless of schedule."""
        ae = _make_automation_engine(is_paused_by_door=True)
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "paused" in action.lower()
        assert "door" in action.lower()

    def test_grace_period_active_returns_grace_message(self):
        """When grace period active → returns grace message."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "grace period" in action.lower()
        assert "manual" in action.lower()

    def test_before_briefing_time_returns_briefing_event(self):
        """Time before briefing_time → first event is 'Send daily briefing'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "06:00:00", "wake_time": "06:30:00", "sleep_time": "22:30:00"}
        # Current time is 05:00 — before briefing at 06:00
        action, t = _compute_next_automation_action(c, ae, config, time(5, 0))
        assert action == "Send daily briefing"
        assert t == "6:00 AM"

    def test_before_bedtime_cool_day_returns_cool_setback(self):
        """Time after wakeup but before bedtime on cool day → bedtime cool setback."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_cool": 75,
            "sleep_cool": 72,
        }
        # Current time is 14:00 — after briefing and wakeup, before sleep
        action, t = _compute_next_automation_action(c, ae, config, time(14, 0))
        assert "Bedtime" in action
        assert "cool setback" in action
        # The real method reads CONF_SLEEP_COOL directly (not a comfort_cool+delta
        # formula) — matches select_comfort_band(in_sleep_window=True), see
        # coordinator.py's _compute_next_automation_action() comment.
        assert "72°F" in action

    def test_before_bedtime_heat_day_returns_heat_setback(self):
        """Time before bedtime on heat day → bedtime heat setback with correct temp."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat", setback_modifier=2.0)
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_heat": 70,
            "sleep_heat": 64,
        }
        # Current time is 20:00 — before sleep at 22:30
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert "Bedtime" in action
        assert "heat setback" in action
        # The real method reads CONF_SLEEP_HEAT directly, not a comfort_heat-4+modifier
        # formula (that formula predates the sleep_heat/sleep_cool config keys).
        assert "64°F" in action

    def test_after_all_events_returns_no_more_actions(self):
        """After all scheduled events have passed → 'No more actions today'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 23:00 — after all events
        action, t = _compute_next_automation_action(c, ae, config, time(23, 0))
        assert action == "No more actions today"
        assert t == ""

    def test_wakeup_event_for_heat_mode(self):
        """Before wakeup time in heat mode → morning wake-up heat comfort."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 06:05 — between briefing and wake_time
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert "Morning wake-up" in action
        assert "heat" in action

    def test_off_mode_wakeup_returns_check(self):
        """Before wakeup in off mode → 'Morning wake-up check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert action == "Morning wake-up check"

    def test_off_mode_bedtime_returns_check(self):
        """Before bedtime in off mode → 'Bedtime check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert action == "Bedtime check"


# ---------------------------------------------------------------------------
# Tests: Sensor name rename
# ---------------------------------------------------------------------------


class TestNextActionSensorRename:
    """Verify sensor names via source inspection.

    Sensor classes cannot be instantiated without a real HA runtime (metaclass
    conflict from MagicMock stubs), so we verify the source code directly.
    """

    @pytest.fixture(autouse=True)
    def _read_sensor_source(self):
        """Read sensor.py source once for all tests in this class."""
        import pathlib

        sensor_path = (
            pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "sensor.py"
        )
        self.source = sensor_path.read_text()

    def test_next_action_sensor_name_is_your_next_action(self):
        """Sensor display name should be 'Your Next Action'."""
        assert '"Your Next Action"' in self.source

    def test_new_automation_action_sensor_name(self):
        """Next Automation Action sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationSensor" in self.source
        assert '"Next Automation Action"' in self.source

    def test_new_automation_time_sensor_name(self):
        """Next Automation Time sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationTimeSensor" in self.source
        assert '"Next Automation Time"' in self.source


# ---------------------------------------------------------------------------
# Tests: New constants exist
# ---------------------------------------------------------------------------


class TestNewConstants:
    """Verify the new attribute constants were added to const.py."""

    def test_attr_next_automation_action_constant(self):
        """ATTR_NEXT_AUTOMATION_ACTION should equal 'next_automation_action'."""
        assert ATTR_NEXT_AUTOMATION_ACTION == "next_automation_action"

    def test_attr_next_automation_time_constant(self):
        """ATTR_NEXT_AUTOMATION_TIME should equal 'next_automation_time'."""
        assert ATTR_NEXT_AUTOMATION_TIME == "next_automation_time"


# ---------------------------------------------------------------------------
# Phase 5G: Compliance sensor thermal attributes
# ---------------------------------------------------------------------------


def _compliance_sensor_extra_state_attributes_with_thermal(coordinator):
    """Return the real ClimateAdvisorComplianceSensor.extra_state_attributes for `coordinator`.

    coordinator is already a real ClimateAdvisorCoordinator instance (constructed
    via _make_coordinator_with_learning), so the sensor can be instantiated directly —
    no replication needed.
    """
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.sensor import ClimateAdvisorComplianceSensor

    entry = MagicMock()
    entry.entry_id = "test_entry"
    sensor = ClimateAdvisorComplianceSensor(coordinator, entry)
    return sensor.extra_state_attributes


def _make_coordinator_with_learning(tmp_path):
    """Build a minimal coordinator with a real LearningEngine for thermal attribute tests."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
    from custom_components.climate_advisor.learning import LearningEngine

    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    hass.states.get = MagicMock(return_value=None)

    config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "notify_service": "notify.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "temp_unit": "fahrenheit",
    }

    coordinator = ClimateAdvisorCoordinator(hass, config)
    coordinator.data = {}
    coordinator.learning = LearningEngine(Path(tmp_path))
    coordinator.learning.load_state()
    coordinator.automation_engine = MagicMock()
    return coordinator


def _make_thermal_obs(mode: str = "heat", rate: float = 2.0) -> dict:
    """Build a v2 ThermalObservation dict. rate is used as k_active."""
    return {
        "event_id": "test-status-obs",
        "timestamp": "2026-03-27T10:00:00",
        "date": "2026-03-27",
        "hvac_mode": mode,
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": -0.05,
        "k_active": rate,  # used as k_active for legacy heating_rate_f_per_hour compat
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.75,
        "r_squared_active": 0.72,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "low",
        "schema_version": 2,
    }


def _inject_thermal_obs(learning, obs: dict) -> None:
    """Inject a v2 observation via record_thermal_observation with dt_util patched."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = date(2026, 3, 27)
    mock_dt.now.return_value.isoformat.return_value = "2026-03-27T12:00:00"
    with patch("custom_components.climate_advisor.learning.dt_util", mock_dt):
        learning.record_thermal_observation(obs)


def _make_bias_record(i: int, forecast_high: float, observed_high: float) -> dict:
    return {
        "date": f"2026-03-{i + 1:02d}",
        "day_type": "mild",
        "trend_direction": "stable",
        "forecast_high_f": forecast_high,
        "observed_high_f": observed_high,
        "forecast_low_f": 50.0,
        "observed_low_f": 51.0,
    }


class TestComplianceSensorThermalAttributes:
    """Tests for compliance sensor thermal attribute helper."""

    def test_thermal_attributes_present_when_model_has_data(self, tmp_path):
        """Inject observations → attrs have non-None rates."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 2.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        assert attrs[ATTR_THERMAL_HEATING_RATE] is not None

    def test_thermal_attributes_none_when_no_observations(self, tmp_path):
        """Empty learning engine → rates are None, confidence is 'none'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import (
            ATTR_THERMAL_CONFIDENCE,
            ATTR_THERMAL_COOLING_RATE,
            ATTR_THERMAL_HEATING_RATE,
        )

        assert attrs[ATTR_THERMAL_HEATING_RATE] is None
        assert attrs[ATTR_THERMAL_COOLING_RATE] is None
        assert attrs[ATTR_THERMAL_CONFIDENCE] == "none"

    def test_thermal_confidence_exposed(self, tmp_path):
        """Inject 5 observations → confidence == 'low'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 2.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_CONFIDENCE

        assert attrs[ATTR_THERMAL_CONFIDENCE] == "low"

    def test_thermal_rate_converted_to_celsius_when_unit_is_celsius(self, tmp_path):
        """With temp_unit='celsius', rate is scaled by 5/9."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "celsius"
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 9.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        # 9°F/hr × 5/9 = 5.0°C/hr
        assert attrs[ATTR_THERMAL_HEATING_RATE] == pytest.approx(5.0, abs=0.01)

    def test_thermal_rate_unchanged_when_unit_is_fahrenheit(self, tmp_path):
        """With temp_unit='fahrenheit', rate is not scaled."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "fahrenheit"
        for _ in range(5):
            _inject_thermal_obs(coordinator.learning, _make_thermal_obs("heat", 3.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_THERMAL_HEATING_RATE

        assert attrs[ATTR_THERMAL_HEATING_RATE] == pytest.approx(3.0, abs=0.01)

    def test_forecast_bias_converted_to_celsius_when_unit_is_celsius(self, tmp_path):
        """With celsius unit, forecast bias is scaled."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        coordinator.config["temp_unit"] = "celsius"
        # Add 7 records with 9°F high bias → 5°C after conversion
        for i in range(7):
            coordinator.learning._state.records.append(_make_bias_record(i, 70.0, 79.0))
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import ATTR_FORECAST_HIGH_BIAS

        # 9°F × 5/9 = 5.0°C
        assert attrs[ATTR_FORECAST_HIGH_BIAS] == pytest.approx(5.0, abs=0.01)

    def test_forecast_bias_zero_when_no_observations(self, tmp_path):
        """No records → bias attrs are 0.0, confidence is 'none'."""
        coordinator = _make_coordinator_with_learning(tmp_path)
        attrs = _compliance_sensor_extra_state_attributes_with_thermal(coordinator)

        from custom_components.climate_advisor.const import (
            ATTR_FORECAST_BIAS_CONFIDENCE,
            ATTR_FORECAST_HIGH_BIAS,
            ATTR_FORECAST_LOW_BIAS,
        )

        assert attrs[ATTR_FORECAST_HIGH_BIAS] == pytest.approx(0.0)
        assert attrs[ATTR_FORECAST_LOW_BIAS] == pytest.approx(0.0)
        assert attrs[ATTR_FORECAST_BIAS_CONFIDENCE] == "none"
