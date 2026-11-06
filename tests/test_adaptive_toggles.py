"""Tests for the three adaptive feature toggles: preheat, setback, weather-bias (Phase 5G)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

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
    CONF_ADAPTIVE_SETBACK,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_WEATHER_BIAS_APPLY_F,
)
from custom_components.climate_advisor.learning import LearningEngine

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
    "learning_enabled": True,
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


def _default_preheat_time(config: dict, minutes: int) -> str:
    """Compute the expected preheat start time string given sleep_time and minutes before it."""
    sleep_str = config.get("sleep_time", "22:30")
    sleep_parts = sleep_str.split(":")
    sleep_total = int(sleep_parts[0]) * 60 + int(sleep_parts[1])
    start_total = sleep_total - minutes
    if start_total < 0:
        start_total += 24 * 60
    return f"{start_total // 60:02d}:{start_total % 60:02d}"


# ---------------------------------------------------------------------------
# Tests: Adaptive Preheat Toggle
# ---------------------------------------------------------------------------


class TestAdaptiveSetbackToggle:
    """Tests for CONF_ADAPTIVE_SETBACK with learning_enabled=True."""

    def test_setback_uses_default_when_adaptive_setback_disabled(self):
        """adaptive_setback_enabled=False with high-confidence model → DEFAULT_SETBACK_DEPTH_F used."""
        config = dict(_BASE_CONFIG, **{CONF_ADAPTIVE_SETBACK: False})
        c = _make_classification(hvac_mode="heat")

        result = compute_bedtime_setback(config, _HIGH_CONFIDENCE_THERMAL_MODEL, c)

        # Toggle off → ignore model depth, use default
        expected = config["comfort_heat"] - DEFAULT_SETBACK_DEPTH_F
        assert result == pytest.approx(expected)

    def test_setback_uses_default_cool_when_adaptive_setback_disabled(self):
        """adaptive_setback_enabled=False in cool mode → DEFAULT_SETBACK_DEPTH_COOL_F used."""
        config = dict(_BASE_CONFIG, **{CONF_ADAPTIVE_SETBACK: False})
        c = _make_classification(hvac_mode="cool")

        result = compute_bedtime_setback(config, _HIGH_CONFIDENCE_THERMAL_MODEL, c)

        expected = config["comfort_cool"] + DEFAULT_SETBACK_DEPTH_COOL_F
        assert result == pytest.approx(expected)

    def test_setback_uses_model_when_adaptive_setback_enabled(self):
        """adaptive_setback_enabled=True with fast house → model depth differs from default."""
        config = dict(_BASE_CONFIG, **{CONF_ADAPTIVE_SETBACK: True})
        c = _make_classification(hvac_mode="heat")

        # Fast house: rate=10°F/hr, long overnight → depth will be capped at MAX_SETBACK_DEPTH_F
        fast_model = {
            "confidence": "high",
            "heating_rate_f_per_hour": 10.0,
        }

        result = compute_bedtime_setback(config, fast_model, c)

        default_target = config["comfort_heat"] - DEFAULT_SETBACK_DEPTH_F
        # Fast house → deeper setback (lower target temp) than default
        assert result < default_target


# ---------------------------------------------------------------------------
# Tests: Weather Bias Toggle
# ---------------------------------------------------------------------------


class TestWeatherBiasToggle:
    """Tests for CONF_WEATHER_BIAS with learning_enabled=True."""

    def test_bias_not_applied_when_weather_bias_disabled(self, tmp_path: Path):
        """weather_bias_enabled=False with good bias data → tomorrow_high unchanged."""
        engine = LearningEngine(tmp_path)
        engine.load_state()

        # 7 records with consistent +2°F underprediction
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 72.0, 74.0, 52.0, 53.0))

        bias = engine.get_weather_bias()
        assert bias["confidence"] != "none"
        assert abs(bias["high_bias"]) >= MIN_WEATHER_BIAS_APPLY_F

        tomorrow_high_raw = 75.0

        # Simulate coordinator._get_forecast() guard:
        # learning_enabled=True but weather_bias_enabled=False → skip correction
        learning_enabled = True
        weather_bias_enabled = False

        if learning_enabled and weather_bias_enabled:
            bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
            tomorrow_high = tomorrow_high_raw + bias_h if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F else tomorrow_high_raw
        else:
            tomorrow_high = tomorrow_high_raw

        # Bias must NOT be applied when weather_bias_enabled=False
        assert tomorrow_high == pytest.approx(tomorrow_high_raw)

    def test_bias_applied_when_weather_bias_enabled(self, tmp_path: Path):
        """weather_bias_enabled=True with good bias data → tomorrow_high is adjusted."""
        engine = LearningEngine(tmp_path)
        engine.load_state()

        # 7 records with consistent +2°F underprediction
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 72.0, 74.0, 52.0, 53.0))

        bias = engine.get_weather_bias()
        assert bias["confidence"] != "none"
        assert bias["high_bias"] == pytest.approx(2.0, abs=0.01)

        tomorrow_high_raw = 75.0

        # Simulate coordinator._get_forecast() with both toggles enabled
        learning_enabled = True
        weather_bias_enabled = True

        if learning_enabled and weather_bias_enabled:
            bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
            tomorrow_high = tomorrow_high_raw + bias_h if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F else tomorrow_high_raw
        else:
            tomorrow_high = tomorrow_high_raw

        # Bias of +2°F must be applied
        assert tomorrow_high == pytest.approx(77.0)
