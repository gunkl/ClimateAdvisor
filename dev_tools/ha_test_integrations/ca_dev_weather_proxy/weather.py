"""Weather platform for CA Dev Weather Proxy.

Dev-only, never shipped — see dev_tools/ha_test_integrations/README.md.

Produces a smooth synthetic diurnal temperature curve:

    T(hour) = base_temp_f + diurnal_swing_f * sin(2*pi*(hour - phase_offset_h + 6)/24)

The +6 shift makes phase_offset_h the actual PEAK hour (not a sine
zero-crossing) so it matches its own field label/description directly.
With the default phase_offset_h=15, the curve peaks at local hour 15 (3pm)
and troughs 12 hours later/earlier at local hour 3 (3am) — a typical
outdoor diurnal shape.

NOTE ON HA VERSION: WeatherEntity's API shape used here (native_temperature,
condition, async_forecast_daily/async_forecast_hourly returning Forecast
dicts) has been stable since well before hacs.json's pinned minimum
homeassistant version (2024.6.0), but this was NOT verified against a
locally-installed `homeassistant` package — none exists in this repo/venv.
Test on a real Home Assistant instance before relying on it.
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from homeassistant.components.weather import Forecast, WeatherEntity, WeatherEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_TEMP_F,
    CONF_CONDITION,
    CONF_DIURNAL_SWING_F,
    CONF_PHASE_OFFSET_H,
)

_LOGGER = logging.getLogger(__name__)

_HOURS_PER_DAY = 24
_FORECAST_HOURS = 48
_FORECAST_DAYS = 7


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the synthetic weather entity from a config entry."""
    async_add_entities([SyntheticWeatherEntity(entry)])


def _sine_temp(hour_of_day: float, base_temp_f: float, diurnal_swing_f: float, phase_offset_h: float) -> float:
    """Return the synthetic outdoor temperature for a given fractional hour-of-day.

    phase_offset_h is the actual peak hour (see module docstring) — the +6
    shift below converts it into the sine's zero-crossing internally.
    """
    shifted = hour_of_day - phase_offset_h + _HOURS_PER_DAY / 4.0
    return base_temp_f + diurnal_swing_f * math.sin(2 * math.pi * shifted / _HOURS_PER_DAY)


class SyntheticWeatherEntity(WeatherEntity):
    """A synthetic weather source with a smooth sine-wave diurnal temperature curve."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the synthetic weather entity from its config entry data."""
        data = entry.data
        self._attr_unique_id = f"{entry.entry_id}_synthetic_weather"
        self._attr_name = data.get("name")

        self._base_temp_f: float = data[CONF_BASE_TEMP_F]
        self._diurnal_swing_f: float = data[CONF_DIURNAL_SWING_F]
        self._phase_offset_h: float = data[CONF_PHASE_OFFSET_H]
        self._condition: str = data[CONF_CONDITION]

    def _hour_of_day_now(self) -> float:
        now = dt_util.now()
        return now.hour + now.minute / 60.0

    @property
    def native_temperature(self) -> float:
        """Return the live synthetic outdoor temperature."""
        return _sine_temp(self._hour_of_day_now(), self._base_temp_f, self._diurnal_swing_f, self._phase_offset_h)

    @property
    def condition(self) -> str:
        """Return the configured static condition."""
        return self._condition

    async def async_forecast_daily(self) -> list[Forecast]:
        """Project the sine curve forward as a daily forecast (peak-of-day temps).

        Starts at day_offset=0 (today), not 1 — Climate Advisor's day
        classification looks up today's own forecast entry by date
        (coordinator.py's _get_forecast) and falls back to a single
        instantaneous reading (losing the whole point of a synthetic curve)
        if it's missing.
        """
        now = dt_util.now()
        forecasts: list[Forecast] = []
        for day_offset in range(_FORECAST_DAYS + 1):
            forecast_time = now + timedelta(days=day_offset)
            peak_hour = self._phase_offset_h
            trough_hour = self._phase_offset_h + _HOURS_PER_DAY / 2.0
            forecasts.append(
                Forecast(
                    datetime=forecast_time.isoformat(),
                    native_temperature=_sine_temp(
                        peak_hour, self._base_temp_f, self._diurnal_swing_f, self._phase_offset_h
                    ),
                    native_templow=_sine_temp(
                        trough_hour, self._base_temp_f, self._diurnal_swing_f, self._phase_offset_h
                    ),
                    condition=self._condition,
                )
            )
        return forecasts

    async def async_forecast_hourly(self) -> list[Forecast]:
        """Project the sine curve forward hour-by-hour.

        Starts at hour_offset=0 (the current hour), not 1 — without a
        current-hour entry, coordinator.py's _extract_current_hour_forecast_temp
        has no bracketing pair and falls into its edge-clamped-interpolation
        fallback path every cycle.
        """
        now = dt_util.now()
        forecasts: list[Forecast] = []
        for hour_offset in range(_FORECAST_HOURS + 1):
            forecast_time = now + timedelta(hours=hour_offset)
            hour_of_day = forecast_time.hour + forecast_time.minute / 60.0
            forecasts.append(
                Forecast(
                    datetime=forecast_time.isoformat(),
                    native_temperature=_sine_temp(
                        hour_of_day, self._base_temp_f, self._diurnal_swing_f, self._phase_offset_h
                    ),
                    condition=self._condition,
                )
            )
        return forecasts
