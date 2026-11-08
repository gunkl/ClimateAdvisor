"""Issue #1080 -- the read-only band resolver must agree with what the setpoint handlers arm.

Occupant view: during a time-of-use window while the house is away or on vacation, the
thermostat is armed with a widened (cheaper-to-hold) band. The dashboard note, the incident
records and the manual-override matching all read the band through
``AutomationEngine._resolved_comfort_band()``; if that omits the widening they describe a band the
thermostat is not actually holding.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import (
    AutomationEngine,
    fallback_edges_without_classification,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import OCCUPANCY_AWAY, OCCUPANCY_HOME, OCCUPANCY_VACATION
from custom_components.climate_advisor.engine_inputs import TouAvWindow

_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "notify_service": "notify.notify",
    "temp_unit": "fahrenheit",
}


def _consume_coroutine(coro):
    coro.close()


def _engine() -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = "cool"
    state.attributes = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1}
    hass.states.get.return_value = state
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=dict(_CONFIG),
    )


def _classification(hvac_mode: str) -> DayClassification:
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
    obj.setback_modifier = 0.0
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _written_temperatures(engine: AutomationEngine) -> list[float]:
    temps: list[float] = []
    for call in engine.hass.services.async_call.await_args_list:
        data = call.args[2] if len(call.args) > 2 else call.kwargs.get("service_data", {})
        if call.args[1] == "set_temperature" and "temperature" in data:
            temps.append(float(data["temperature"]))
    return temps


class TestResolvedBandMatchesArmedBand:
    @pytest.mark.parametrize(
        ("occupancy", "hvac_mode", "expected_edge"),
        [
            (OCCUPANCY_AWAY, "cool", 83.0),  # setback_cool 80 + 3 expansion on a cool day
            (OCCUPANCY_VACATION, "cool", 86.0),  # vacation: 80 + 3 extra + 3 expansion
        ],
    )
    def test_cool_day_tou_window_resolver_equals_handler_write(self, occupancy, hvac_mode, expected_edge):
        engine = _engine()
        engine.inputs.tou_av = TouAvWindow(expansion_f=3.0)
        c = _classification(hvac_mode)
        engine._current_classification = c
        engine.set_occupancy_mode(occupancy)

        band = engine._resolved_comfort_band(c)
        assert band.ceiling == expected_edge

        handler = engine.handle_occupancy_away if occupancy == OCCUPANCY_AWAY else engine.handle_occupancy_vacation
        asyncio.run(handler())
        assert expected_edge in _written_temperatures(engine), (
            "the handler must arm the very edge the read-only resolver reports"
        )

    def test_heat_day_tou_window_widens_only_the_floor(self):
        engine = _engine()
        engine.inputs.tou_av = TouAvWindow(expansion_f=3.0)
        c = _classification("heat")
        engine.set_occupancy_mode(OCCUPANCY_AWAY)
        band = engine._resolved_comfort_band(c)
        assert (band.floor, band.ceiling) == (57.0, 80.0)

    def test_no_expansion_outside_a_tou_window(self):
        engine = _engine()
        engine.inputs.tou_av = TouAvWindow()
        engine.set_occupancy_mode(OCCUPANCY_AWAY)
        band = engine._resolved_comfort_band(_classification("cool"))
        assert (band.floor, band.ceiling) == (60.0, 80.0)

    def test_home_never_widens_even_if_expansion_is_set(self):
        engine = _engine()
        engine.inputs.tou_av = TouAvWindow(expansion_f=3.0)
        engine.set_occupancy_mode(OCCUPANCY_HOME)
        band = engine._resolved_comfort_band(_classification("cool"))
        assert (band.floor, band.ceiling) == (70.0, 75.0)


class TestFallbackEdgesWithoutClassification:
    def test_daytime_uses_comfort_edges(self):
        from datetime import datetime

        config = {**_CONFIG, "sleep_heat": 64.0, "sleep_cool": 72.0, "sleep_time": "22:30", "wake_time": "07:00"}
        assert fallback_edges_without_classification(datetime(2026, 7, 21, 14, 0), config) == (70, 75)

    def test_sleep_window_uses_sleep_edges(self):
        from datetime import datetime

        config = {**_CONFIG, "sleep_heat": 64.0, "sleep_cool": 72.0, "sleep_time": "22:30", "wake_time": "07:00"}
        assert fallback_edges_without_classification(datetime(2026, 7, 21, 2, 0), config) == (64.0, 72.0)
