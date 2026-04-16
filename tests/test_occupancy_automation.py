"""Tests for Issue #85 — Automation engine occupancy awareness.

Verifies that apply_classification, handle_morning_wakeup, handle_bedtime,
and _set_temperature_for_mode respect the engine's occupancy mode.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ── Helpers ──────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
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


def _make_classification(
    day_type: str = "warm",
    hvac_mode: str = "cool",
    setback_modifier: float = 0.0,
    **kwargs,
) -> DayClassification:
    """Create a DayClassification bypassing __post_init__."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = kwargs.get("trend_direction", "stable")
    obj.trend_magnitude = kwargs.get("trend_magnitude", 0)
    obj.today_high = kwargs.get("today_high", 80.0)
    obj.today_low = kwargs.get("today_low", 60.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 80.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 60.0)
    obj.pre_condition = kwargs.get("pre_condition", False)
    obj.pre_condition_target = kwargs.get("pre_condition_target")
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    obj.window_opportunity_morning = kwargs.get("window_opportunity_morning", False)
    obj.window_opportunity_evening = kwargs.get("window_opportunity_evening", False)
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


# ── apply_classification occupancy tests ────────────────────────


class TestApplyClassificationOccupancy:
    """apply_classification should respect occupancy mode."""

    def test_away_reapplies_setback_instead_of_comfort(self):
        """When away, classification cycle should apply setback, not comfort."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.apply_classification(c))

        # Should have called set_temperature with setback_cool (80), not comfort_cool (75)
        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) == 1
        set_temp = temp_calls[0][0][2]["temperature"]
        assert set_temp == 80  # setback_cool, not comfort_cool (75)

    def test_away_heat_reapplies_heat_setback(self):
        """When away in heat mode, classification should apply heat setback."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) == 1
        set_temp = temp_calls[0][0][2]["temperature"]
        assert set_temp == 60  # setback_heat, not comfort_heat (70)

    def test_vacation_skips_classification_entirely(self):
        """When on vacation, classification should not change temperature at all."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.apply_classification(c))

        # No service calls should have been made
        engine.hass.services.async_call.assert_not_called()

    def test_home_applies_comfort_normally(self):
        """When home, classification should apply comfort temps as usual."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 75  # comfort_cool

    def test_guest_applies_comfort_normally(self):
        """Guest mode should behave like home — full comfort."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 70  # comfort_heat


# ── handle_morning_wakeup occupancy tests ───────────────────────


class TestMorningWakeupOccupancy:
    """handle_morning_wakeup should skip when not home/guest."""

    def test_wakeup_skipped_when_away(self):
        """Morning wakeup should not restore comfort when user is away."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.handle_morning_wakeup())

        engine.hass.services.async_call.assert_not_called()

    def test_wakeup_skipped_when_vacation(self):
        """Morning wakeup should not restore comfort during vacation."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.handle_morning_wakeup())

        engine.hass.services.async_call.assert_not_called()

    def test_wakeup_runs_when_home(self):
        """Morning wakeup should work normally when home."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine.handle_morning_wakeup())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 70  # comfort_heat

    def test_wakeup_runs_when_guest(self):
        """Morning wakeup should work when guests are present."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")

        asyncio.run(engine.handle_morning_wakeup())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 75  # comfort_cool


# ── handle_bedtime occupancy tests ──────────────────────────────


class TestBedtimeOccupancy:
    """handle_bedtime should skip during vacation."""

    def test_bedtime_skipped_when_vacation(self):
        """Vacation deep setback should not be overwritten by bedtime setback."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.handle_bedtime())

        engine.hass.services.async_call.assert_not_called()

    def test_bedtime_skipped_when_away(self):
        """Issue #101: Away setback is already active — bedtime should not override it."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.handle_bedtime())

        # Bedtime is skipped when AWAY — away setback wins
        engine.hass.services.async_call.assert_not_called()

    def test_bedtime_runs_when_home(self):
        """Bedtime should run normally when home."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1

    def test_bedtime_runs_when_guest(self):
        """Guest mode should apply bedtime setback like home."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1

    def test_bedtime_uses_sleep_heat_when_home(self):
        """Issue #101: sleep_heat=67 in config → bedtime sets to 67°F, not comfort_heat-4."""
        engine = _make_engine(
            config_overrides={
                "comfort_heat": 70,
                "setback_heat": 60,
                "sleep_heat": 67.0,
            }
        )
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[0][0][2]["temperature"]
        assert set_temp == pytest.approx(67.0, abs=0.1)


# ── _set_temperature_for_mode safety net tests ──────────────────


class TestSetTemperatureForModeOccupancy:
    """_set_temperature_for_mode should redirect to setback when away/vacation."""

    def test_redirects_to_away_setback(self):
        """When away, _set_temperature_for_mode should apply away setback."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) == 1
        set_temp = temp_calls[0][0][2]["temperature"]
        assert set_temp == 80  # setback_cool

    def test_redirects_to_vacation_setback(self):
        """When on vacation, _set_temperature_for_mode should apply deep setback."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) == 1
        set_temp = temp_calls[0][0][2]["temperature"]
        # vacation heat = setback_heat + setback_modifier - VACATION_SETBACK_EXTRA
        # = 60 + 0 - 3 = 57
        assert set_temp == 57

    def test_applies_comfort_when_home(self):
        """When home, _set_temperature_for_mode should apply comfort as usual."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) == 1
        set_temp = temp_calls[0][0][2]["temperature"]
        assert set_temp == 75  # comfort_cool


# ── set_occupancy_mode tests ────────────────────────────────────


class TestSetOccupancyMode:
    """set_occupancy_mode should update the internal state."""

    def test_sets_mode(self):
        engine = _make_engine()
        assert engine._occupancy_mode == "home"
        engine.set_occupancy_mode("away")
        assert engine._occupancy_mode == "away"

    def test_handlers_set_mode_internally(self):
        """Occupancy handlers should also set the internal mode."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        asyncio.run(engine.handle_occupancy_away())
        assert engine._occupancy_mode == "away"

        asyncio.run(engine.handle_occupancy_home())
        assert engine._occupancy_mode == "home"

        asyncio.run(engine.handle_occupancy_vacation())
        assert engine._occupancy_mode == "vacation"

    def test_logs_mode_change(self, caplog):
        engine = _make_engine()
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            engine.set_occupancy_mode("away")
        assert "home → away" in caplog.text
