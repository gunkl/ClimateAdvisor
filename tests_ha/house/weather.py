"""A small real WeatherEntity for the house (design finding F5).

A plain ``weather`` *state* makes Climate Advisor log ``weather.get_forecasts service call
failed ... falling back to forecast attribute`` every cycle, so the real forecast service path is
never exercised. A real entity answers ``weather.get_forecasts`` with a dated daily and hourly
forecast, the way every real weather integration does.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from homeassistant.components.weather import Forecast, WeatherEntity, WeatherEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.util import dt as dt_util


class HouseWeather(WeatherEntity):
    """Outdoor conditions plus a forecast, all set by the test in degrees Fahrenheit."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_name = "House Weather"
    _attr_unique_id = "ca_house_weather"
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_condition = "sunny"

    def __init__(self, entity_id: str = "weather.house") -> None:
        self.entity_id = entity_id
        self._temp_f = 70.0
        self._high_f = 78.0
        self._low_f = 58.0
        self._days = 5

    @property
    def native_temperature(self) -> float:
        return self._temp_f

    def set_conditions(
        self,
        *,
        temp_f: float | None = None,
        high_f: float | None = None,
        low_f: float | None = None,
        condition: str | None = None,
    ) -> None:
        """Change outdoor conditions and the forecast envelope (state is written by the caller)."""
        if temp_f is not None:
            self._temp_f = float(temp_f)
        if high_f is not None:
            self._high_f = float(high_f)
        if low_f is not None:
            self._low_f = float(low_f)
        if condition is not None:
            self._attr_condition = condition

    async def async_forecast_daily(self) -> list[Forecast] | None:
        today = dt_util.start_of_local_day()
        return [
            Forecast(
                datetime=(today + timedelta(days=d)).isoformat(),
                native_temperature=self._high_f,
                native_templow=self._low_f,
                condition=self._attr_condition,
            )
            for d in range(self._days)
        ]

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """A smooth low-at-05:00 / high-at-15:00 curve for the next 48 hours."""
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        out: list[Forecast] = []
        mid = (self._high_f + self._low_f) / 2.0
        amp = (self._high_f - self._low_f) / 2.0
        for h in range(48):
            ts: datetime = start + timedelta(hours=h)
            temp = mid + amp * math.sin((ts.hour + ts.minute / 60.0 - 9.0) / 24.0 * 2.0 * math.pi)
            out.append(
                Forecast(datetime=ts.isoformat(), native_temperature=round(temp, 1), condition=self._attr_condition)
            )
        return out
