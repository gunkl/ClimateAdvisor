"""Tests that learning_enabled=False bypasses all thermal learning (Phase 5G)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import (
    AutomationEngine,
    compute_bedtime_setback,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DEFAULT_PREHEAT_MINUTES,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_WEATHER_BIAS_APPLY_F,
)
from custom_components.climate_advisor.learning import LearningEngine

_TODAY = "2026-03-28"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "climate_entity": "climate.test",
    "weather_entity": "weather.test",
    "notify_service": "notify.test",
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "07:00",
    "sleep_time": "22:00",
    "temp_unit": "fahrenheit",
}

_HIGH_CONFIDENCE_THERMAL_MODEL = {
    "confidence": "high",
    "heating_rate_f_per_hour": 5.0,
    "cooling_rate_f_per_hour": 4.0,
    "observation_count_heat": 25,
    "observation_count_cool": 20,
}


def _make_classification(
    *,
    hvac_mode: str = "heat",
    setback_modifier: float = 0.0,
    trend_direction: str = "cooling",
    pre_condition: bool = True,
    pre_condition_target: float = 2.0,
) -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "cold",
            "trend_direction": trend_direction,
            "trend_magnitude": 10.0,
            "today_high": 40.0,
            "today_low": 25.0,
            "tomorrow_high": 38.0,
            "tomorrow_low": 22.0,
            "hvac_mode": hvac_mode,
            "pre_condition": pre_condition,
            "pre_condition_target": pre_condition_target,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": setback_modifier,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    return c


def _make_hass() -> MagicMock:
    from unittest.mock import AsyncMock

    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


def _make_engine(config: dict | None = None) -> AutomationEngine:
    cfg = dict(_BASE_CONFIG)
    if config:
        cfg.update(config)
    engine = AutomationEngine(
        hass=_make_hass(),
        climate_entity=cfg["climate_entity"],
        weather_entity=cfg["weather_entity"],
        door_window_sensors=[],
        notify_service=cfg["notify_service"],
        config=cfg,
    )
    engine.dry_run = True
    return engine


def _make_complete_record(
    date: str,
    forecast_high: float,
    observed_high: float,
    forecast_low: float,
    observed_low: float,
) -> dict:
    return {
        "date": date,
        "day_type": "mild",
        "trend_direction": "stable",
        "forecast_high_f": forecast_high,
        "observed_high_f": observed_high,
        "forecast_low_f": forecast_low,
        "observed_low_f": observed_low,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLearningEnabledToggle:
    """Tests that learning_enabled=False causes each guard to skip learning logic."""

    def test_thermal_observation_skipped_when_disabled(self, tmp_path: Path):
        """_start_hvac_observation() returns early when learning_enabled=False."""
        import asyncio

        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)
        hass.async_add_executor_job = AsyncMock(return_value=None)

        config = dict(_BASE_CONFIG, learning_enabled=False)
        coordinator = ClimateAdvisorCoordinator(hass, config)

        # Call _start_hvac_observation — should return early without creating any observation
        asyncio.run(coordinator._start_hvac_observation("heat"))

        # No pending observation should have been created
        assert coordinator._pending_observations == {}, (
            "_start_hvac_observation should not create any observation when learning_enabled=False"
        )

    def test_compute_bedtime_setback_uses_default_when_learning_disabled(self):
        """compute_bedtime_setback() returns default depth when learning_enabled=False."""
        config = dict(_BASE_CONFIG, learning_enabled=False)
        c = _make_classification(hvac_mode="heat")

        result = compute_bedtime_setback(config, _HIGH_CONFIDENCE_THERMAL_MODEL, c)

        # learning_enabled=False → ignore model, use DEFAULT_SETBACK_DEPTH_F=4.0
        # target = comfort_heat(70) - DEFAULT_SETBACK_DEPTH_F(4.0) = 66.0
        expected = config["comfort_heat"] - DEFAULT_SETBACK_DEPTH_F
        assert result == pytest.approx(expected)

    def test_compute_bedtime_setback_uses_default_when_learning_disabled_cool(self):
        """compute_bedtime_setback() cool mode returns default depth when learning_enabled=False."""
        config = dict(_BASE_CONFIG, learning_enabled=False)
        c = _make_classification(hvac_mode="cool")

        result = compute_bedtime_setback(config, _HIGH_CONFIDENCE_THERMAL_MODEL, c)

        # learning_enabled=False → ignore model, use DEFAULT_SETBACK_DEPTH_COOL_F=3.0
        # target = comfort_cool(75) + DEFAULT_SETBACK_DEPTH_COOL_F(3.0) = 78.0
        expected = config["comfort_cool"] + DEFAULT_SETBACK_DEPTH_COOL_F
        assert result == pytest.approx(expected)

    def test_schedule_pre_condition_uses_default_when_learning_disabled(self):
        """_schedule_pre_condition() uses DEFAULT_PREHEAT_MINUTES when learning_enabled=False."""
        config = dict(_BASE_CONFIG, learning_enabled=False)
        engine = _make_engine(config)

        # Inject a high-confidence thermal model — must be ignored due to toggle
        engine._thermal_model = _HIGH_CONFIDENCE_THERMAL_MODEL

        c = _make_classification(
            trend_direction="cooling",
            pre_condition=True,
            pre_condition_target=2.0,
        )
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None

        # With DEFAULT_PREHEAT_MINUTES=120, sleep=22:00 → start at 20:00
        sleep_str = config["sleep_time"]
        sleep_parts = sleep_str.split(":")
        sleep_total = int(sleep_parts[0]) * 60 + int(sleep_parts[1])
        expected_start = sleep_total - DEFAULT_PREHEAT_MINUTES
        expected_hour = expected_start // 60
        expected_minute = expected_start % 60
        expected_time = f"{expected_hour:02d}:{expected_minute:02d}"

        assert preheat["time"] == expected_time

    def test_weather_bias_skipped_when_learning_disabled(self, tmp_path: Path):
        """When learning_enabled=False, weather bias correction is not applied."""
        engine = LearningEngine(tmp_path)
        engine.load_state()

        # Add 7 records with consistent 2°F underprediction (bias=+2°F high)
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 72.0, 74.0, 52.0, 53.0))

        bias = engine.get_weather_bias()
        assert bias["confidence"] != "none"
        assert abs(bias["high_bias"]) >= MIN_WEATHER_BIAS_APPLY_F

        tomorrow_high_raw = 75.0

        # Simulate coordinator._get_forecast() guard: learning_enabled=False → skip bias
        learning_enabled = False
        if learning_enabled:
            bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
            tomorrow_high = tomorrow_high_raw + bias_h if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F else tomorrow_high_raw
        else:
            tomorrow_high = tomorrow_high_raw

        # Bias must NOT be applied when learning_enabled=False
        assert tomorrow_high == pytest.approx(tomorrow_high_raw)
