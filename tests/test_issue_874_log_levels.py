"""Tests for Issue #874's log-level cleanup fixes (Fixes 1-5).

Fix 7 (stuck-grace root cause) is covered by a golden-candidate simulation
scenario under tools/simulations/pending/ instead of a unit test here, per
this project's Simulation Skill workflow.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE, CONF_FAN_MODE

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"
COORDINATOR_LOGGER = "custom_components.climate_advisor.coordinator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    _hvac_modes = ["off", "heat", "cool", "heat_cool"]
    climate_state = MagicMock()
    climate_state.state = "heat_cool"
    climate_state.attributes = {
        "hvac_modes": _hvac_modes,
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        "current_temperature": 72.0,
    }
    hass.states.get.return_value = climate_state

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(**overrides) -> DayClassification:
    obj = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 2.0,
        "today_high": 78.0,
        "today_low": 58.0,
        "tomorrow_high": 79.0,
        "tomorrow_low": 59.0,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
    }
    defaults.update(overrides)
    obj.__dict__.update(defaults)
    return obj


def _make_coordinator_for_next_action():
    """Minimal coordinator stub bound to the real _compute_next_action()."""
    import types

    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"temp_unit": "fahrenheit", "comfort_cool": 75, "comfort_heat": 70}
    coord._occupancy_mode = "home"
    coord._nat_vent_plan = None
    coord._compute_next_action = types.MethodType(ClimateAdvisorCoordinator._compute_next_action, coord)
    return coord


def _make_coordinator_for_outdoor_temp(hourly_forecast_temps=None):
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"outdoor_temp_source": "weather_service", "temp_unit": "fahrenheit"}
    coord.hass = MagicMock()
    coord._hourly_forecast_temps = hourly_forecast_temps
    coord._hourly_interp_unavailable_streak = 0
    coord._hourly_forecast_confirmed_unsupported = False
    coord._get_outdoor_temp = ClimateAdvisorCoordinator._get_outdoor_temp.__get__(coord)
    coord._get_hourly_forecast_data = ClimateAdvisorCoordinator._get_hourly_forecast_data.__get__(coord)
    return coord


def _make_coordinator_for_whf_status(
    fan_active=False,
    fan_override_active=False,
    natural_vent_active=False,
    physical_state=None,
    recent_fan_command=False,
):
    from custom_components.climate_advisor.const import FAN_MODE_WHOLE_HOUSE
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = object.__new__(ClimateAdvisorCoordinator)
    hass = MagicMock()
    cs = MagicMock()
    cs.attributes = {"fan_mode": "auto", "hvac_action": ""}
    hass.states.get.return_value = cs
    coord.hass = hass
    coord.config = {"climate_entity": "climate.thermostat"}

    ae = MagicMock()
    ae.config = {CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE}
    ae._fan_active = fan_active
    ae._fan_override_active = fan_override_active
    ae._natural_vent_active = natural_vent_active
    coord.automation_engine = ae

    coord._get_fan_physical_state = MagicMock(return_value=physical_state)
    coord._is_recent_fan_command = MagicMock(return_value=recent_fan_command)
    coord._compute_whf_status = ClimateAdvisorCoordinator._compute_whf_status.__get__(coord)
    return coord


# ---------------------------------------------------------------------------
# Fix 1 — Guard-block warnings -> INFO
# ---------------------------------------------------------------------------


class TestFix1WhfWriteBlockedInfo:
    def test_log_whf_write_blocked_helper_logs_info_not_warning(self, caplog):
        engine = _make_automation_engine()
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            engine._log_whf_write_blocked("door/window open — binary_sensor.kitchen_window, was cool mode")
        assert not any(r.levelno == logging.WARNING for r in caplog.records)
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("HVAC write blocked" in m and "whole-house fan owns thermostat" in m for m in info_msgs)

    def test_set_hvac_mode_blocked_by_whf_logs_info_not_warning(self, caplog):
        engine = _make_automation_engine()
        engine._whf_owns_hvac = MagicMock(return_value=True)
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine._set_hvac_mode("cool", reason="test"))
        assert not any(r.levelno == logging.WARNING for r in caplog.records)
        assert any("HVAC write blocked" in r.message for r in caplog.records if r.levelno == logging.INFO)

    def test_door_window_paused_band_suppression_logs_info_not_warning(self, caplog):
        engine = _make_automation_engine()
        # Simulate the door/window paused gate so apply_classification hits the
        # DEFER_PAUSED branch that used to WARNING.
        engine._paused_by_door = True
        c = _make_classification(day_type="warm", hvac_mode="cool")
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))
        assert not any(r.levelno == logging.WARNING and "door/window open" in r.message for r in caplog.records)
        assert any(
            r.levelno == logging.INFO and "apply_classification: door/window open" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Fix 2 — _decide(warn=True) next-action advisories -> INFO
# ---------------------------------------------------------------------------


class TestFix2DecideNextActionInfo:
    def test_close_windows_advisory_logs_info_not_warning(self, caplog):
        """'Close windows — outdoor's not helping now' used to warn=True."""
        coord = _make_coordinator_for_next_action()
        c = _make_classification(
            day_type="warm",
            windows_recommended=False,
        )
        with caplog.at_level(logging.INFO, logger=COORDINATOR_LOGGER):
            result = coord._compute_next_action(
                c,
                indoor_temp=80.0,
                outdoor_temp=85.0,
                windows_physically_open=True,
                ae=None,
            )
        assert "close windows" in result.lower()
        assert not any(r.levelno == logging.WARNING and "Next-action" in r.message for r in caplog.records)
        assert any(r.levelno == logging.INFO and "Next-action" in r.message for r in caplog.records)

    def test_windows_wont_help_advisory_logs_info_not_warning(self, caplog):
        """'Outdoor isn't cooler than indoor yet — windows/fan won't help' used to warn=True."""
        coord = _make_coordinator_for_next_action()
        c = _make_classification(day_type="warm", windows_recommended=False)
        with caplog.at_level(logging.INFO, logger=COORDINATOR_LOGGER):
            result = coord._compute_next_action(
                c,
                indoor_temp=80.0,
                outdoor_temp=85.0,
                windows_physically_open=False,
                ae=None,
            )
        assert "won't help" in result
        assert not any(r.levelno == logging.WARNING and "Next-action" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix 3 — Hourly forecast interpolation retry-style escalation
# ---------------------------------------------------------------------------


class TestFix3HourlyForecastEscalation:
    def test_first_three_failures_log_info(self, caplog):
        coord = _make_coordinator_for_outdoor_temp(hourly_forecast_temps=[])
        with caplog.at_level(logging.INFO, logger=COORDINATOR_LOGGER):
            for _ in range(3):
                coord._get_outdoor_temp({"temperature": 65})
        assert not any(
            r.levelno == logging.WARNING and "Hourly forecast interpolation unavailable" in r.message
            for r in caplog.records
        )
        assert coord._hourly_interp_unavailable_streak == 3

    def test_fourth_failure_escalates_to_warning(self, caplog):
        coord = _make_coordinator_for_outdoor_temp(hourly_forecast_temps=[])
        with caplog.at_level(logging.INFO, logger=COORDINATOR_LOGGER):
            for _ in range(4):
                coord._get_outdoor_temp({"temperature": 65})
        warn_msgs = [
            r for r in caplog.records if r.levelno == logging.WARNING and "interpolation unavailable" in r.message
        ]
        assert len(warn_msgs) == 1

    def test_success_resets_streak(self, caplog):
        from custom_components.climate_advisor import coordinator as _coord_mod

        coord = _make_coordinator_for_outdoor_temp(hourly_forecast_temps=[])
        now = datetime(2026, 9, 7, 12, 0, 0)
        with (
            patch.object(_coord_mod.dt_util, "now", return_value=now),
            caplog.at_level(logging.INFO, logger=COORDINATOR_LOGGER),
        ):
            for _ in range(3):
                coord._get_outdoor_temp({"temperature": 65})
            assert coord._hourly_interp_unavailable_streak == 3

            # A successful interpolation resets the streak.
            good_forecast = [
                {"datetime": (now - timedelta(hours=1)).isoformat(), "temperature": 60},
                {"datetime": (now + timedelta(hours=1)).isoformat(), "temperature": 64},
            ]
            coord._hourly_forecast_temps = good_forecast
            coord._get_outdoor_temp({"temperature": 65})
        assert coord._hourly_interp_unavailable_streak == 0

    def test_confirmed_unsupported_logs_one_time_error(self, caplog):
        """The DEBUG call site (weather.get_forecasts hourly type failing outright)
        fires a one-time ERROR the first time, then stays DEBUG-only afterward."""
        coord = _make_coordinator_for_outdoor_temp()
        coord.hass.states.get.return_value = MagicMock()
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("hourly not supported"))
        coord.config["weather_entity"] = "weather.forecast_home"

        with caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOGGER):
            asyncio.run(coord._get_hourly_forecast_data())
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert coord._hourly_forecast_confirmed_unsupported is True

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOGGER):
            asyncio.run(coord._get_hourly_forecast_data())
        assert not any(r.levelno == logging.ERROR for r in caplog.records)
        assert any(r.levelno == logging.DEBUG for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix 4 — Pre-cool overshoot warning gated on recent setpoint command
# ---------------------------------------------------------------------------


class TestFix4PreCoolOvershootGating:
    def _run_morning_wakeup(self, engine, indoor_temp: float):
        engine._current_classification = _make_classification(day_type="cold", hvac_mode="heat")
        asyncio.run(engine.handle_morning_wakeup(indoor_temp=indoor_temp))

    def test_no_recent_command_still_warns(self, caplog):
        engine = _make_automation_engine()
        engine._temp_command_time = None
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            self._run_morning_wakeup(engine, indoor_temp=60.0)  # below comfort_heat=70
        assert any(r.levelno == logging.WARNING and "pre-cool overshoot" in r.message for r in caplog.records)

    def test_recent_command_logs_info_not_warning(self, caplog):
        from custom_components.climate_advisor import automation as _automation_mod

        engine = _make_automation_engine()
        now = datetime(2026, 9, 7, 6, 30, 0)
        engine._temp_command_time = now
        with (
            patch.object(_automation_mod.dt_util, "now", return_value=now),
            caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER),
        ):
            self._run_morning_wakeup(engine, indoor_temp=60.0)
        assert not any(r.levelno == logging.WARNING and "pre-cool overshoot" in r.message for r in caplog.records)
        assert any(r.levelno == logging.INFO and "pre-cool overshoot" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix 5 — Dedup the WHF "x6" warning
# ---------------------------------------------------------------------------


class TestFix5WhfStatusWarningDedup:
    def test_repeated_reads_within_cycle_warn_only_once(self):
        from custom_components.climate_advisor import coordinator as _coord_mod

        coord = _make_coordinator_for_whf_status(
            fan_active=True,
            physical_state=False,
            recent_fan_command=False,
        )
        now = datetime(2026, 9, 7, 12, 0, 0)
        with (
            patch.object(_coord_mod.dt_util, "now", return_value=now),
            patch.object(_coord_mod, "_LOGGER") as mock_logger,
        ):
            for _ in range(6):
                result = coord._compute_whf_status()
        assert result == "inactive"
        assert mock_logger.warning.call_count == 1

    def test_new_occurrence_after_window_warns_again(self):
        from custom_components.climate_advisor import coordinator as _coord_mod

        coord = _make_coordinator_for_whf_status(
            fan_active=True,
            physical_state=False,
            recent_fan_command=False,
        )
        now = datetime(2026, 9, 7, 12, 0, 0)
        with patch.object(_coord_mod, "_LOGGER") as mock_logger:
            with patch.object(_coord_mod.dt_util, "now", return_value=now):
                coord._compute_whf_status()
            # Simulate time passing beyond the dedup window.
            with patch.object(_coord_mod.dt_util, "now", return_value=now + timedelta(seconds=61)):
                coord._compute_whf_status()
        assert mock_logger.warning.call_count == 2
