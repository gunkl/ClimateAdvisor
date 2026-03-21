"""Tests for plain-text reason logging on thermostat adjustments.

Every call to _set_hvac_mode and _set_temperature must include a reason
parameter that appears in the WARNING-level log output (Issue #37: elevated
from INFO so actions are visible in HA's default log level).  These tests verify
that each call site in AutomationEngine produces the expected reason string.

See: GitHub Issue #16
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
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
    trend_direction: str = "stable",
    trend_magnitude: float = 2.0,
    setback_modifier: float = 0.0,
    pre_condition: bool = False,
    pre_condition_target: float | None = None,
    **kwargs,
) -> DayClassification:
    """Create a DayClassification with explicit fields (bypass __post_init__)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = trend_direction
    obj.trend_magnitude = trend_magnitude
    obj.today_high = kwargs.get("today_high", 78.0)
    obj.today_low = kwargs.get("today_low", 58.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 79.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 59.0)
    obj.hvac_mode = hvac_mode
    obj.pre_condition = pre_condition
    obj.pre_condition_target = pre_condition_target
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    return obj


# ---------------------------------------------------------------------------
# apply_classification
# ---------------------------------------------------------------------------


class TestApplyClassificationLogging:
    """Reason logging for apply_classification()."""

    def test_heat_mode_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            trend_direction="cooling",
            trend_magnitude=5.0,
        )
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # Should have mode + temperature info logs
        mode_msgs = [m for m in messages if "Set HVAC mode" in m]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(mode_msgs) >= 1
        assert len(temp_msgs) >= 1
        assert "daily classification" in mode_msgs[0]
        assert "cold day" in mode_msgs[0]
        assert "daily classification" in temp_msgs[0]

    def test_cool_mode_with_precool_logs_offset(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(
            day_type="hot",
            hvac_mode="cool",
            trend_direction="warming",
            trend_magnitude=8.0,
            pre_condition=True,
            pre_condition_target=-3.0,
        )
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) >= 1
        assert "pre-cool offset" in temp_msgs[0]
        assert "daily classification" in temp_msgs[0]

    def test_off_mode_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="mild", hvac_mode="off")
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        mode_msgs = [m for m in messages if "Set HVAC mode" in m]
        assert len(mode_msgs) == 1
        assert "HVAC not needed" in mode_msgs[0]
        assert "mild day" in mode_msgs[0]
        # No temperature log for off mode
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 0


# ---------------------------------------------------------------------------
# handle_door_window_open / closed
# ---------------------------------------------------------------------------


class TestDoorWindowLogging:
    """Reason logging for door/window open and close handlers."""

    def test_door_open_logs_reason(self, caplog):
        engine = _make_automation_engine()
        # Simulate a thermostat in cool mode
        state = MagicMock()
        state.state = "cool"
        engine.hass.states.get.return_value = state

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_door_window_open("binary_sensor.kitchen_window"))

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        mode_msgs = [m for m in messages if "Set HVAC mode" in m]
        assert len(mode_msgs) == 1
        assert "door/window open" in mode_msgs[0]
        assert "binary_sensor.kitchen_window" in mode_msgs[0]
        assert "cool mode" in mode_msgs[0]

    def test_door_closed_logs_reason(self, caplog):
        engine = _make_automation_engine()
        # Set up paused state
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_all_doors_windows_closed())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        mode_msgs = [m for m in messages if "Set HVAC mode" in m]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(mode_msgs) >= 1
        assert "door/window closed" in mode_msgs[0]
        assert "restoring heat mode" in mode_msgs[0]
        assert len(temp_msgs) >= 1
        assert "restoring comfort" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_occupancy_away / home
# ---------------------------------------------------------------------------


class TestOccupancyLogging:
    """Reason logging for occupancy handlers."""

    def test_occupancy_away_heat_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=2.0,
        )
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "occupancy away" in temp_msgs[0]
        assert "heat setback" in temp_msgs[0]
        assert "base 60" in temp_msgs[0]
        assert "modifier 2" in temp_msgs[0]

    def test_occupancy_away_cool_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(
            day_type="hot",
            hvac_mode="cool",
            setback_modifier=1.0,
        )
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "occupancy away" in temp_msgs[0]
        assert "cool setback" in temp_msgs[0]
        assert "base 80" in temp_msgs[0]
        assert "modifier 1" in temp_msgs[0]

    def test_occupancy_home_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_home())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "occupancy home" in temp_msgs[0]
        assert "heat comfort" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_bedtime
# ---------------------------------------------------------------------------


class TestBedtimeLogging:
    """Reason logging for bedtime handler."""

    def test_bedtime_heat_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=2.0,
        )
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_bedtime())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "bedtime" in temp_msgs[0]
        assert "heat setback" in temp_msgs[0]
        assert "comfort 70" in temp_msgs[0]
        assert "modifier 2" in temp_msgs[0]

    def test_bedtime_cool_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_bedtime())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "bedtime" in temp_msgs[0]
        assert "cool setback" in temp_msgs[0]
        assert "comfort 75" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_morning_wakeup
# ---------------------------------------------------------------------------


class TestMorningWakeupLogging:
    """Reason logging for morning wakeup handler."""

    def test_morning_wakeup_heat_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_morning_wakeup())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "morning wake-up" in temp_msgs[0]
        assert "heat comfort" in temp_msgs[0]

    def test_morning_wakeup_cool_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_morning_wakeup())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set temperature" in m]
        assert len(temp_msgs) == 1
        assert "morning wake-up" in temp_msgs[0]
        assert "cool comfort" in temp_msgs[0]
