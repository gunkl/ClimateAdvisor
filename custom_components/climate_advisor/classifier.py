"""Day type classification and forecast analysis for Climate Advisor."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

from .const import (
    DAY_TYPE_HOT,
    DAY_TYPE_WARM,
    DAY_TYPE_MILD,
    DAY_TYPE_COOL,
    DAY_TYPE_COLD,
    THRESHOLD_HOT,
    THRESHOLD_WARM,
    THRESHOLD_MILD,
    THRESHOLD_COOL,
    TREND_THRESHOLD_SIGNIFICANT,
    TREND_THRESHOLD_MODERATE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ForecastSnapshot:
    """A snapshot of relevant forecast data."""

    today_high: float
    today_low: float
    tomorrow_high: float
    tomorrow_low: float
    current_outdoor_temp: float
    current_indoor_temp: float | None = None
    current_humidity: float | None = None
    timestamp: datetime | None = None


@dataclass
class DayClassification:
    """The result of classifying a day and its trend."""

    day_type: str
    trend_direction: str  # "warming", "cooling", "stable"
    trend_magnitude: float  # degrees of change
    today_high: float
    today_low: float
    tomorrow_high: float
    tomorrow_low: float

    # Computed recommendations
    hvac_mode: str = ""  # "heat", "cool", "off", "auto"
    pre_condition: bool = False  # Should we pre-heat or pre-cool?
    pre_condition_target: float | None = None
    windows_recommended: bool = False
    window_open_time: time | None = None
    window_close_time: time | None = None
    setback_modifier: float = 0.0  # Degrees to adjust setback based on trend

    def __post_init__(self):
        """Compute recommendations based on classification."""
        self._compute_recommendations()

    def _compute_recommendations(self):
        """Derive actionable recommendations from the day type and trend."""

        # Determine HVAC mode
        if self.day_type == DAY_TYPE_HOT:
            self.hvac_mode = "cool"
            self.pre_condition = True
            self.pre_condition_target = -2.0  # 2°F below cooling setpoint
        elif self.day_type == DAY_TYPE_WARM:
            self.hvac_mode = "off"
            self.windows_recommended = True
            self.window_open_time = time(8, 0)
            self.window_close_time = time(18, 0)
        elif self.day_type == DAY_TYPE_MILD:
            self.hvac_mode = "off"
            self.windows_recommended = True
            self.window_open_time = time(10, 0)
            self.window_close_time = time(17, 0)
        elif self.day_type == DAY_TYPE_COOL:
            self.hvac_mode = "heat"
        elif self.day_type == DAY_TYPE_COLD:
            self.hvac_mode = "heat"

        # Trend modifiers
        if self.trend_direction == "cooling" and self.trend_magnitude >= TREND_THRESHOLD_SIGNIFICANT:
            # Big cold front coming — pre-heat and conservative setback
            self.pre_condition = True
            self.pre_condition_target = 3.0  # 3°F above comfort
            self.setback_modifier = 3.0  # Don't set back as far
        elif self.trend_direction == "warming" and self.trend_magnitude >= TREND_THRESHOLD_SIGNIFICANT:
            # Warming trend — more aggressive setback tonight
            self.setback_modifier = -3.0  # Set back further, tomorrow handles it
        elif self.trend_direction == "cooling" and self.trend_magnitude >= TREND_THRESHOLD_MODERATE:
            self.pre_condition = True
            self.pre_condition_target = 2.0
            self.setback_modifier = 2.0
        elif self.trend_direction == "warming" and self.trend_magnitude >= TREND_THRESHOLD_MODERATE:
            self.setback_modifier = -2.0


def classify_day(forecast: ForecastSnapshot) -> DayClassification:
    """Classify the day type and trend from forecast data.

    Args:
        forecast: Current forecast snapshot with today/tomorrow temps.

    Returns:
        DayClassification with day type, trend, and recommendations.
    """
    today_high = forecast.today_high
    tomorrow_high = forecast.tomorrow_high

    # Classify day type based on today's high
    if today_high >= THRESHOLD_HOT:
        day_type = DAY_TYPE_HOT
    elif today_high >= THRESHOLD_WARM:
        day_type = DAY_TYPE_WARM
    elif today_high >= THRESHOLD_MILD:
        day_type = DAY_TYPE_MILD
    elif today_high >= THRESHOLD_COOL:
        day_type = DAY_TYPE_COOL
    else:
        day_type = DAY_TYPE_COLD

    # Determine trend by comparing tomorrow to today
    high_delta = tomorrow_high - today_high
    low_delta = forecast.tomorrow_low - forecast.today_low
    avg_delta = (high_delta + low_delta) / 2

    if avg_delta > 2:
        trend_direction = "warming"
    elif avg_delta < -2:
        trend_direction = "cooling"
    else:
        trend_direction = "stable"

    trend_magnitude = abs(avg_delta)

    return DayClassification(
        day_type=day_type,
        trend_direction=trend_direction,
        trend_magnitude=trend_magnitude,
        today_high=today_high,
        today_low=forecast.today_low,
        tomorrow_high=tomorrow_high,
        tomorrow_low=forecast.tomorrow_low,
    )
