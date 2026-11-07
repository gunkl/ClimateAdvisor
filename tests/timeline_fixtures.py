"""Builders for operation-timeline tests (Issue #1230 R3a): a realistic snapshot for any day type.

The band comes from the REAL ``select_comfort_band`` (``automation.band_resolver_for``) and the day type from the REAL
``classify_day``, so a test cannot pass against a band or classification the engine would never produce. Only the
weather and the thermal model are synthetic: a sine day around the requested high/low and zone-1-like learned rates.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any

from custom_components.climate_advisor.automation import band_resolver_for
from custom_components.climate_advisor.classifier import ForecastSnapshot, classify_day
from custom_components.climate_advisor.operation_timeline import TimelineSnapshot

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # noqa: BLE001 - tz database missing (Windows): fixed offset keeps tests deterministic
    from datetime import timezone

    TZ = timezone(timedelta(hours=-8))

CONFIG: dict[str, Any] = {
    "comfort_heat": 68.0,
    "comfort_cool": 74.0,
    "sleep_heat": 66.0,
    "sleep_cool": 72.0,
    "setback_heat": 62.0,
    "setback_cool": 80.0,
    "wake_time": "06:30",
    "sleep_time": "20:30",
    "aggressive_savings": False,
}

# (high, low) per day type: the classifier thresholds are 85 / 75 / 60 / 45.
DAY_TYPES: dict[str, tuple[float, float]] = {
    "hot": (96.0, 70.0),
    "warm": (80.0, 58.0),
    "mild": (68.0, 50.0),
    "cool": (52.0, 38.0),
    "cold": (38.0, 24.0),
}

MODEL: dict[str, Any] = {
    "k_passive": -0.0315,
    "k_active_heat": 5.58,
    "k_active_cool": -6.13,
    "k_vent_window": -0.126,
    "k_solar": 0.75,
    "solar_phase_offset_h": 3.0,
    "swing_heat_f": 1.0,
    "swing_cool_f": 1.0,
    "observation_count_heat": 65,
    "observation_count_cool": 286,
    "observation_count_passive": 106,
    "observation_count_vent_window": 91,
    "confidence": "high",
}


def sine_day(day: date, high: float, low: float, tz: Any = TZ) -> list[tuple[datetime, float]]:
    """Hourly forecast for ``day`` (midnight to the next midnight): coldest 05:00, warmest 16:00."""
    mid, amp = (high + low) / 2.0, (high - low) / 2.0
    out = []
    for h in range(25):
        ts = datetime.combine(day, time(0, 0), tzinfo=tz) + timedelta(hours=h)
        out.append((ts, round(mid + amp * math.cos(2 * math.pi * (ts.hour - 16) / 24), 1)))
    return out


def classification(day_type: str, *, comfort_heat: float = 68.0, comfort_cool: float = 74.0):
    high, low = DAY_TYPES[day_type]
    return classify_day(
        ForecastSnapshot(
            today_high=high,
            today_low=low,
            tomorrow_high=high,
            tomorrow_low=low,
            current_outdoor_temp=low + 4,
        ),
        comfort_heat=comfort_heat,
        comfort_cool=comfort_cool,
    )


def make_snapshot(
    day_type: str = "mild",
    *,
    now: datetime | None = None,
    tomorrow: str | None = None,
    occupancy: str = "home",
    fan_mode: str = "whole_house_fan",
    sleep_sensor: str | None = None,
    indoor: float | None = None,
    model: dict[str, Any] | None = MODEL,
    config: dict[str, Any] | None = None,
    **overrides: Any,
) -> TimelineSnapshot:
    cfg = dict(CONFIG if config is None else config)
    now = now or datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    c = classification(day_type, comfort_heat=cfg["comfort_heat"], comfort_cool=cfg["comfort_cool"])
    today = now.date()
    tom = tomorrow or day_type
    c_tom = classification(tom, comfort_heat=cfg["comfort_heat"], comfort_cool=cfg["comfort_cool"])
    h1, l1 = DAY_TYPES[day_type]
    h2, l2 = DAY_TYPES[tom]
    hourly = sine_day(today, h1, l1) + sine_day(today + timedelta(days=1), h2, l2)[1:]
    hourly += sine_day(today + timedelta(days=2), h2, l2)[1:]
    outdoor = next(v for t, v in hourly if t >= now)
    start_indoor = indoor if indoor is not None else (cfg["comfort_heat"] + cfg["comfort_cool"]) / 2.0 - 1.0
    snap = TimelineSnapshot(
        now=now,
        wake_time=time(*map(int, cfg["wake_time"].split(":"))),
        sleep_time=time(*map(int, cfg["sleep_time"].split(":"))),
        fan_mode=fan_mode,
        sleep_sensor_label=sleep_sensor,
        primary_sensor_label="hallway",
        band_at=band_resolver_for(c, cfg),
        occupancy_mode=occupancy,
        day_modes={
            today: c.hvac_mode,
            today + timedelta(days=1): c_tom.hvac_mode,
            today + timedelta(days=2): c_tom.hvac_mode,
        },
        day_type=c.day_type,
        windows_recommended=c.windows_recommended,
        window_open_time=c.window_open_time,
        window_close_time=c.window_close_time,
        opportunity_spans=tuple(
            span
            for span in (
                (c.window_opportunity_morning_start, c.window_opportunity_morning_end)
                if c.window_opportunity_morning
                else None,
                (c.window_opportunity_evening_start, c.window_opportunity_evening_end)
                if c.window_opportunity_evening
                else None,
            )
            if span is not None and span[0] is not None and span[1] is not None
        ),
        indoor=start_indoor,
        indoor_primary=start_indoor,
        outdoor=outdoor,
        thermostat_mode=c.hvac_mode if c.hvac_mode != "off" else "heat_cool",
        hourly=hourly,
        forecast_age_h=0.5,
        thermal_model=dict(model) if model else None,
    )
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap
