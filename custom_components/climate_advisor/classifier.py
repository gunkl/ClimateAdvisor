"""Day type classification and forecast analysis for Climate Advisor."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

from .const import (
    CLASSIFICATION_HYSTERESIS_F,
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_COMFORT_COOL,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_MORNING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
    THRESHOLD_COOL,
    THRESHOLD_HOT,
    THRESHOLD_MILD,
    THRESHOLD_WARM,
    TREND_THRESHOLD_MODERATE,
    TREND_THRESHOLD_SIGNIFICANT,
    WARM_WINDOW_CLOSE_HOUR,
    WARM_WINDOW_OPEN_HOUR,
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
    window_opportunity_morning: bool = False  # Morning window cooling possible on hot days
    window_opportunity_evening: bool = False  # Evening window cooling possible on hot days
    window_opportunity_morning_start: time | None = None
    window_opportunity_morning_end: time | None = None
    window_opportunity_evening_start: time | None = None
    window_opportunity_evening_end: time | None = None

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
            # Check if morning/evening temps might be favorable for window cooling
            # If today's low is within 5°F of a typical comfort_cool (75°F), windows could help
            if self.today_low <= 80:  # Today's low is moderate enough for window opportunity
                self.window_opportunity_morning = True
                self.window_opportunity_morning_start = time(ECONOMIZER_MORNING_START_HOUR, 0)
                self.window_opportunity_morning_end = time(ECONOMIZER_MORNING_END_HOUR, 0)
            if self.tomorrow_low <= 80:  # Tomorrow's low suggests cool evening
                self.window_opportunity_evening = True
                self.window_opportunity_evening_start = time(ECONOMIZER_EVENING_START_HOUR, 0)
                self.window_opportunity_evening_end = time(0, 0)  # midnight
            _LOGGER.debug(
                "HOT day window opportunity — today_low=%.1f (morning=%s), "
                "tomorrow_low=%.1f (evening=%s), threshold=80°F",
                self.today_low,
                self.window_opportunity_morning,
                self.tomorrow_low,
                self.window_opportunity_evening,
            )
        elif self.day_type == DAY_TYPE_WARM:
            self.hvac_mode = "off"
            self.window_open_time = time(WARM_WINDOW_OPEN_HOUR, 0)
            self.window_close_time = time(WARM_WINDOW_CLOSE_HOUR, 0)
            # Only recommend windows if morning low is cool enough to help —
            # same delta the HOT-day economizer uses.
            if self.today_low <= DEFAULT_COMFORT_COOL - ECONOMIZER_TEMP_DELTA:
                self.windows_recommended = True
        elif self.day_type == DAY_TYPE_MILD:
            self.hvac_mode = "off"
            self.windows_recommended = True
            self.window_open_time = time(10, 0)
            self.window_close_time = time(17, 0)
        elif self.day_type == DAY_TYPE_COOL or self.day_type == DAY_TYPE_COLD:
            self.hvac_mode = "heat"

        _LOGGER.debug(
            "Recommendations — type=%s, mode=%s, windows=%s",
            self.day_type,
            self.hvac_mode,
            self.windows_recommended,
        )

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

        if self.pre_condition or self.setback_modifier != 0:
            _LOGGER.debug(
                "Trend modifier — pre_condition=%s, setback_modifier=%.1f°F",
                self.pre_condition,
                self.setback_modifier,
            )


# Ordered classification bands from cold→hot, with their lower thresholds.
_DAY_TYPE_ORDER = [DAY_TYPE_COLD, DAY_TYPE_COOL, DAY_TYPE_MILD, DAY_TYPE_WARM, DAY_TYPE_HOT]
_BAND_LOWER_THRESHOLD = {
    DAY_TYPE_COOL: THRESHOLD_COOL,
    DAY_TYPE_MILD: THRESHOLD_MILD,
    DAY_TYPE_WARM: THRESHOLD_WARM,
    DAY_TYPE_HOT: THRESHOLD_HOT,
}


def _should_stick(today_high: float, previous_day_type: str, margin: float) -> bool:
    """Return True if the temperature is in the dead zone around a threshold.

    When a prior classification exists, the temperature must move beyond
    the threshold by *margin* degrees to justify switching.  This prevents
    bouncing when the forecast fluctuates near a boundary.
    """
    prev_idx = _DAY_TYPE_ORDER.index(previous_day_type)

    # Upper boundary: to move UP, temp must reach next band's threshold + margin
    if prev_idx < len(_DAY_TYPE_ORDER) - 1:
        upper_type = _DAY_TYPE_ORDER[prev_idx + 1]
        upper_threshold = _BAND_LOWER_THRESHOLD[upper_type]
        if today_high >= upper_threshold and today_high < upper_threshold + margin:
            return True

    # Lower boundary: to move DOWN, temp must drop below own threshold - margin
    if prev_idx > 0:
        own_threshold = _BAND_LOWER_THRESHOLD[previous_day_type]
        if today_high < own_threshold and today_high >= own_threshold - margin:
            return True

    return False


def classify_day(
    forecast: ForecastSnapshot,
    previous_day_type: str | None = None,
) -> DayClassification:
    """Classify the day type and trend from forecast data.

    Args:
        forecast: Current forecast snapshot with today/tomorrow temps.
        previous_day_type: If set, apply hysteresis to prevent threshold
            bouncing when the forecast fluctuates near a boundary.

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

    # Apply hysteresis: if we have a prior classification, only change
    # if the temperature has moved beyond the threshold by the margin.
    if (
        previous_day_type is not None
        and day_type != previous_day_type
        and _should_stick(today_high, previous_day_type, CLASSIFICATION_HYSTERESIS_F)
    ):
        _LOGGER.debug(
            "Hysteresis — today_high=%.0f°F would be %s but sticking with %s",
            today_high,
            day_type,
            previous_day_type,
        )
        day_type = previous_day_type

    _LOGGER.debug("Day type — today_high=%.0f°F, classified=%s", today_high, day_type)

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

    _LOGGER.debug(
        "Trend — high_delta=%.1f°F, low_delta=%.1f°F, avg=%.1f°F, direction=%s, magnitude=%.1f°F",
        high_delta,
        low_delta,
        avg_delta,
        trend_direction,
        trend_magnitude,
    )

    return DayClassification(
        day_type=day_type,
        trend_direction=trend_direction,
        trend_magnitude=trend_magnitude,
        today_high=today_high,
        today_low=forecast.today_low,
        tomorrow_high=tomorrow_high,
        tomorrow_low=forecast.tomorrow_low,
    )
