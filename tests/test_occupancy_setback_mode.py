"""Tests for Issue #222 — occupancy setback uses actual thermostat mode.

`handle_occupancy_away()` and `handle_occupancy_vacation()` must read the
thermostat's actual hvac_mode from HA state rather than the classification's
desired hvac_mode.  The two can diverge when a day that started hot (cool
mode) is later reclassified to heat — without this guard the old code would
direct the AC to chill an empty home to the heat setback temperature.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

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
        "setback_heat": 61,
        "setback_cool": 82,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(hvac_mode: str = "cool", setback_modifier: float = 0.0) -> DayClassification:
    """Create a minimal DayClassification bypassing __post_init__."""
    obj = object.__new__(DayClassification)
    obj.day_type = "warm" if hvac_mode == "cool" else "cold"
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0
    obj.today_high = 80.0
    obj.today_low = 60.0
    obj.tomorrow_high = 80.0
    obj.tomorrow_low = 60.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = setback_modifier
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _thermostat_state(mode: str) -> MagicMock:
    """Return a mock HA state object whose .state is ``mode``."""
    s = MagicMock()
    s.state = mode
    return s


def _last_set_temperature(engine: AutomationEngine) -> float | None:
    """Extract the temperature from the most recent climate.set_temperature call."""
    calls = engine.hass.services.async_call.call_args_list
    temp_calls = [c for c in calls if len(c[0]) >= 3 and c[0][0] == "climate" and c[0][1] == "set_temperature"]
    if not temp_calls:
        return None
    return temp_calls[-1][0][2]["temperature"]


# ── handle_occupancy_away — 4 cases ─────────────────────────────


class TestHandleOccupancyAwayActualMode:
    """handle_occupancy_away() selects setback branch from thermostat state, not classification."""

    def test_thermostat_cool_classification_heat_applies_cool_setback(self):
        """Bug scenario: thermostat=cool, classification=heat → must use setback_cool (82), NOT setback_heat (61)."""
        engine = _make_engine()
        # Classification says heat (e.g., rolled over at night)…
        engine._current_classification = _make_classification(hvac_mode="heat")
        # …but the actual thermostat is still in cool mode
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 82, f"Expected setback_cool=82, got {temp}"

    def test_thermostat_heat_classification_cool_applies_heat_setback(self):
        """Reverse: thermostat=heat, classification=cool → must use setback_heat (61), NOT setback_cool (82)."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("heat")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 61, f"Expected setback_heat=61, got {temp}"

    def test_thermostat_cool_classification_cool_applies_cool_setback(self):
        """Normal path (no mismatch): thermostat=cool, classification=cool → setback_cool (82)."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp == 82, f"Expected setback_cool=82, got {temp}"

    def test_thermostat_off_logs_and_skips_setpoint(self):
        """When thermostat is off no setpoint should be sent."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("off")

        asyncio.run(engine.handle_occupancy_away())

        temp = _last_set_temperature(engine)
        assert temp is None, f"Expected no setpoint when HVAC is off, got {temp}"


# ── handle_occupancy_vacation — 4 cases ─────────────────────────


class TestHandleOccupancyVacationActualMode:
    """handle_occupancy_vacation() selects setback branch from thermostat state, not classification."""

    def test_thermostat_cool_classification_heat_applies_cool_setback(self):
        """Bug scenario: thermostat=cool, classification=heat → must use setback_cool branch."""
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA  # 82 + extra
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation cool setback={expected}, got {temp}"

    def test_thermostat_heat_classification_cool_applies_heat_setback(self):
        """Reverse: thermostat=heat, classification=cool → must use setback_heat branch."""
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("heat")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_heat"] - VACATION_SETBACK_EXTRA  # 61 - extra
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation heat setback={expected}, got {temp}"

    def test_thermostat_cool_classification_cool_applies_cool_setback(self):
        """Normal path: thermostat=cool, classification=cool → cool vacation setback."""
        from custom_components.climate_advisor.automation import VACATION_SETBACK_EXTRA

        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("cool")

        asyncio.run(engine.handle_occupancy_vacation())

        expected = engine.config["setback_cool"] + VACATION_SETBACK_EXTRA
        temp = _last_set_temperature(engine)
        assert temp == expected, f"Expected vacation cool setback={expected}, got {temp}"

    def test_thermostat_off_logs_and_skips_setpoint(self):
        """When thermostat is off no setpoint should be sent."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="cool")
        engine.hass.states.get.return_value = _thermostat_state("off")

        asyncio.run(engine.handle_occupancy_vacation())

        temp = _last_set_temperature(engine)
        assert temp is None, f"Expected no setpoint when HVAC is off, got {temp}"
