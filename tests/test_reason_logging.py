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
    """Create an AutomationEngine with mocked HA dependencies.

    Under P3 the engine calls ``_apply_comfort_band`` which reads thermostat capabilities.
    The climate state must expose ``hvac_modes`` + ``supported_features`` so the band path
    reaches the logging primitives (``_set_hvac_mode`` / ``_set_temperature_dual``).
    Default: full dual-setpoint thermostat (heat_cool + TARGET_TEMPERATURE_RANGE).
    """
    from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    # Dual-capable climate state so _apply_comfort_band produces log output.
    # Use a real dict for attributes: _get_thermostat_capabilities uses attrs.get(),
    # _get_indoor_temp_f uses state.attributes.get("current_temperature").
    _hvac_modes = ["off", "heat", "cool", "heat_cool"]
    _features = CLIMATE_FEATURE_TARGET_TEMP_RANGE
    climate_state = MagicMock()
    climate_state.state = "heat_cool"
    climate_state.attributes = {
        "hvac_modes": _hvac_modes,
        "supported_features": _features,
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
    """Reason logging for apply_classification() — P3 band model.

    P3 routes all classification actions through ``_apply_comfort_band``, which calls
    ``_set_hvac_mode`` + ``_set_temperature_dual`` (dual-capable thermostat).  The log
    messages now come from those primitives with the classification reason embedded.
    """

    def test_heat_mode_logs_reason(self, caplog):
        """Cold day → band arming: 'Set dual temperature' log containing the daily classification
        reason with 'cold day'.  Mode change only fires if thermostat is not already in heat_cool.

        Old assertion: mode log contained 'cold day'; temp log existed.
        P3: _set_temperature_dual logs 'Set dual temperature [70°F / 80°F] — daily classification — cold day …'.
        The engine default is current_mode='heat_cool', so _set_hvac_mode is idempotent (no mode call).
        Only the temperature log is guaranteed; mode log may or may not fire.
        """
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
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "daily classification" in temp_msgs[0]
        assert "cold day" in temp_msgs[0]

    def test_cool_mode_with_precool_logs_band_reason(self, caplog):
        """Hot day with pre-cool → band arming logs 'Set dual temperature' with daily-classification
        reason.  The pre-cool offset lowers the band ceiling; the reason string reflects the band.

        Old assertion: 'pre-cool offset' appeared in the temperature log string.
        P3: _set_temperature_dual logs the classification reason (not 'pre-cool offset'); the
        pre-cool offset is baked into the ceiling value in the band (e.g. '72°F' instead of '75°F').
        """
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
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        # Band reason always contains "daily classification"
        assert "daily classification" in temp_msgs[0]

    def test_off_mode_logs_reason(self, caplog):
        """Mild day (hvac_mode='off') → band arming: dual-temperature log with daily-classification
        reason containing 'mild day'.

        Old assertion: 'HVAC not needed' + 'mild day' in a set_hvac_mode('off') log; no temp log.
        P3: 'HVAC not needed' no longer exists — the band is always armed (never 'off').  The engine
        logs 'Set dual temperature [60°F / 75°F] — daily classification — mild day …'.
        Mode change only fires if thermostat is not already in heat_cool (default is heat_cool).
        """
        engine = _make_automation_engine()
        c = _make_classification(day_type="mild", hvac_mode="off")
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # P3: dual temperature log always present; mode log only if mode changes
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "mild day" in temp_msgs[0]


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
        # Match both single-setpoint ("Set temperature") and dual-setpoint ("Set dual temperature")
        # log variants; "Action recorded" lines use "temp" (not "temperature") so are excluded.
        temp_msgs = [m for m in messages if "temperature" in m]
        assert len(mode_msgs) >= 1
        assert "door/window closed" in mode_msgs[0]
        assert "restoring heat mode" in mode_msgs[0]
        assert len(temp_msgs) >= 1
        assert "restoring comfort" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_occupancy_away / home
# ---------------------------------------------------------------------------


class TestOccupancyLogging:
    """Reason logging for occupancy handlers — P3 band model.

    P3 routes handle_occupancy_away() through _apply_comfort_band with reason
    'occupancy away — setback band'.  The log messages come from _set_hvac_mode /
    _set_temperature_dual, which embed that reason string.

    Old assertions checked mode-specific reason phrases ('heat setback', 'base 60',
    'modifier 2') derived from the old setback-by-current-mode dispatch.  P3 replaces
    that dispatch with a single band reason; the setback values are reflected in the
    dual setpoint numbers in the log line.
    """

    def test_occupancy_away_heat_logs_reason(self, caplog):
        """handle_occupancy_away(): dual-temp log must contain 'occupancy away' + 'setback'.

        Old assertion: 'heat setback', 'base 60', 'modifier 2' in the temp log.
        P3: _set_temperature_dual logs 'Set dual temperature [60°F / 80°F] — occupancy away — setback band'.
        """
        from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat", setback_modifier=2.0)
        engine._current_classification = c
        # Keep capability attributes so _apply_comfort_band reaches the logging primitives
        _hvac_modes = ["off", "heat", "cool", "heat_cool"]
        _features = CLIMATE_FEATURE_TARGET_TEMP_RANGE
        thermostat_state = MagicMock()
        thermostat_state.state = "heat"
        thermostat_state.attributes = {
            "hvac_modes": _hvac_modes,
            "supported_features": _features,
            "current_temperature": 72.0,
        }
        engine.hass.states.get.return_value = thermostat_state

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "occupancy away" in temp_msgs[0]
        assert "setback" in temp_msgs[0]

    def test_occupancy_away_cool_logs_reason(self, caplog):
        """handle_occupancy_away(): dual-temp log must contain 'occupancy away' + 'setback'.

        Old assertion: 'cool setback', 'base 80', 'modifier 1' in the temp log.
        P3: same band reason regardless of day type; setback values in the dual setpoint numbers.
        """
        from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE

        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool", setback_modifier=1.0)
        engine._current_classification = c
        _hvac_modes = ["off", "heat", "cool", "heat_cool"]
        _features = CLIMATE_FEATURE_TARGET_TEMP_RANGE
        thermostat_state = MagicMock()
        thermostat_state.state = "cool"
        thermostat_state.attributes = {
            "hvac_modes": _hvac_modes,
            "supported_features": _features,
            "current_temperature": 72.0,
        }
        engine.hass.states.get.return_value = thermostat_state

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "occupancy away" in temp_msgs[0]
        assert "setback" in temp_msgs[0]

    def test_occupancy_home_logs_reason(self, caplog):
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_home())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # Match both single-setpoint ("Set temperature") and dual-setpoint ("Set dual temperature")
        # log variants; "Action recorded" lines use "temp" (not "temperature") so are excluded.
        temp_msgs = [m for m in messages if "temperature" in m]
        assert len(temp_msgs) == 1
        assert "occupancy home" in temp_msgs[0]
        assert "heat comfort" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_bedtime
# ---------------------------------------------------------------------------


class TestBedtimeLogging:
    """Reason logging for bedtime handler — P3 band model.

    P3 routes handle_bedtime() through _apply_comfort_band with reason
    'bedtime — sleep band [<floor>/<ceiling>]'.  The log messages come from
    _set_temperature_dual which embeds that reason string.

    Old assertions checked mode-specific phrases ('heat setback', 'comfort 70', 'modifier 2')
    derived from the old setback-by-classification dispatch.  P3 replaces that with a single
    sleep-band reason; the setback values appear as the dual setpoint numbers.
    """

    def test_bedtime_heat_logs_reason(self, caplog):
        """handle_bedtime(): dual-temp log must contain 'bedtime' + 'sleep band'.

        Old assertion: 'heat setback', 'comfort 70', 'modifier 2' in the temp log.
        P3: _set_temperature_dual logs 'Set dual temperature [66°F / 78°F] — bedtime — sleep band [66/78]'.
        """
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat", setback_modifier=2.0)
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_bedtime())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "bedtime" in temp_msgs[0]
        assert "sleep band" in temp_msgs[0]

    def test_bedtime_cool_logs_reason(self, caplog):
        """handle_bedtime(): dual-temp log must contain 'bedtime' + 'sleep band'.

        Old assertion: 'cool setback', 'comfort 75' in the temp log.
        P3: same sleep-band reason regardless of day type.
        """
        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_bedtime())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "bedtime" in temp_msgs[0]
        assert "sleep band" in temp_msgs[0]


# ---------------------------------------------------------------------------
# handle_morning_wakeup
# ---------------------------------------------------------------------------


class TestMorningWakeupLogging:
    """Reason logging for morning wakeup handler — P3 band model.

    P3 routes handle_morning_wakeup() through _apply_comfort_band with reason
    'morning wake-up — comfort band [<floor>/<ceiling>]'.

    Old assertions checked mode-specific comfort phrases ('heat comfort', 'cool comfort').
    P3 logs 'morning wake-up — comfort band [70/80]' (cold) or 'morning wake-up — comfort band [60/75]'
    (hot); 'morning wake-up' is preserved; 'heat comfort'/'cool comfort' are not.
    """

    def test_morning_wakeup_heat_logs_reason(self, caplog):
        """handle_morning_wakeup(): dual-temp log must contain 'morning wake-up' + 'comfort band'.

        Old assertion: 'morning wake-up' + 'heat comfort' in temp log.
        P3: _set_temperature_dual logs '… morning wake-up — comfort band [70/80]'.
        """
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_morning_wakeup())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "morning wake-up" in temp_msgs[0]
        assert "comfort band" in temp_msgs[0]

    def test_morning_wakeup_cool_logs_reason(self, caplog):
        """handle_morning_wakeup(): dual-temp log must contain 'morning wake-up' + 'comfort band'.

        Old assertion: 'morning wake-up' + 'cool comfort' in temp log.
        P3: same comfort-band reason regardless of day type.
        """
        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_morning_wakeup())

        messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        temp_msgs = [m for m in messages if "Set" in m and "temperature" in m.lower()]
        assert len(temp_msgs) >= 1
        assert "morning wake-up" in temp_msgs[0]
        assert "comfort band" in temp_msgs[0]
