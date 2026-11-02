"""Issue #1048 (Phase 2, change 12) — override records are stored in internal °F and rendered in the user's unit.

Occupant outcome: a Celsius household that nudges the thermostat 21 -> 22 degC used to get a record of
21/22 "degF" (a -6 degC Activity Log line, "21.0F -> 22.0F" in the AI summary, "up by about 1 degF" in a
suggestion). Now the record is the real 69.8/71.6 degF and every display converts once.

Uses the real ``_async_thermostat_changed`` through the ``test_heat_cool_override`` stub factory.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from tests.test_heat_cool_override import (
    _NOW_BASE,
    _PATCH_CALLBACK,
    _PATCH_DT_UTIL,
    _make_coordinator_stub,
    _make_event,
)


def _run(coord, event) -> None:
    mock_dt = MagicMock()
    mock_dt.now.return_value = _NOW_BASE
    with patch(_PATCH_CALLBACK, side_effect=lambda fn: fn), patch(_PATCH_DT_UTIL, mock_dt):
        asyncio.run(coord._async_thermostat_changed(event))


class TestCoordinatorOverrideRecordUnits:
    def test_celsius_setpoint_override_is_recorded_in_fahrenheit(self):
        coord = _make_coordinator_stub()
        coord.config["temp_unit"] = "celsius"
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", old_temp=21.0, new_temp=22.0))
        detail = coord._today_record.override_details[0]
        assert detail["old_temp"] == 69.8
        assert detail["new_temp"] == 71.6
        assert detail["magnitude"] == 1.8
        assert detail["direction"] == "up"

    def test_celsius_override_event_payload_is_fahrenheit(self):
        coord = _make_coordinator_stub()
        coord.config["temp_unit"] = "celsius"
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", old_temp=21.0, new_temp=22.0))
        kwargs = coord.automation_engine.handle_manual_override.call_args.kwargs
        assert round(kwargs["old_setpoint_f"], 2) == 69.8
        assert round(kwargs["new_setpoint_f"], 2) == 71.6

    def test_fahrenheit_setpoint_override_unchanged(self):
        coord = _make_coordinator_stub()
        _run(coord, _make_event(old_hvac_mode="cool", new_hvac_mode="cool", old_temp=74.0, new_temp=72.0))
        detail = coord._today_record.override_details[0]
        assert (detail["old_temp"], detail["new_temp"], detail["magnitude"]) == (74.0, 72.0, 2.0)
        assert detail["direction"] == "down"
        kwargs = coord.automation_engine.handle_manual_override.call_args.kwargs
        assert (kwargs["old_setpoint_f"], kwargs["new_setpoint_f"]) == (74.0, 72.0)

    def test_celsius_heat_cool_override_uses_converted_high_setpoint(self):
        coord = _make_coordinator_stub()
        coord.config["temp_unit"] = "celsius"
        _run(
            coord,
            _make_event(
                old_hvac_mode="heat_cool",
                new_hvac_mode="heat_cool",
                old_target_high=24.0,
                new_target_high=25.0,
            ),
        )
        detail = coord._today_record.override_details[0]
        assert detail["old_temp"] == 75.2
        assert detail["new_temp"] == 77.0


# ---------------------------------------------------------------------------
# Consumers: AI override context + learning suggestions render in the user's unit
# ---------------------------------------------------------------------------

import asyncio as _asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from custom_components.climate_advisor.ai_skills_context import build_override_details_context  # noqa: E402
from custom_components.climate_advisor.learning import LearningEngine, LearningState  # noqa: E402


def _ai_coord(unit: str, details: list[dict]):
    coord = MagicMock()
    coord.config = {"temp_unit": unit}
    coord._today_record = MagicMock(manual_overrides=len(details), override_details=details)
    coord.automation_engine = None
    return coord


_DETAIL = [{"time": "09:00", "old_temp": 69.8, "new_temp": 71.6, "direction": "up", "magnitude": 1.8}]


class TestOverrideContextUnits:
    def test_celsius_context_shows_celsius(self):
        ctx = _asyncio.run(build_override_details_context(None, _ai_coord("celsius", _DETAIL)))
        assert "21.0\u00b0C -> 22.0\u00b0C" in ctx
        assert "+1.0\u00b0C" in ctx
        assert "69.8" not in ctx

    def test_fahrenheit_context_shows_fahrenheit(self):
        ctx = _asyncio.run(build_override_details_context(None, _ai_coord("fahrenheit", _DETAIL)))
        assert "69.8\u00b0F -> 71.6\u00b0F" in ctx
        assert "+1.8\u00b0F" in ctx


def _suggestion_engine(tmp_path: Path, unit: str, mag_f: float) -> LearningEngine:
    engine = LearningEngine(tmp_path, temp_unit=unit)
    engine._state = LearningState()
    for i in range(14):
        engine._state.records.append(
            {
                "date": f"2026-03-{i + 1:02d}",
                "day_type": "warm",
                "trend_direction": "stable",
                "occupancy_mode": "home",
                "manual_overrides": 1,
                "override_details": [{"time": "09:00", "direction": "up", "magnitude": mag_f}],
            }
        )
    return engine


class TestLearningSuggestionUnits:
    def test_celsius_override_suggestion_says_celsius(self, tmp_path):
        text = " ".join(_suggestion_engine(tmp_path, "celsius", 1.8).generate_suggestions())
        assert "up by about 1\u00b0C" in text
        assert "\u00b0F" not in text

    def test_fahrenheit_override_suggestion_unchanged(self, tmp_path):
        text = " ".join(_suggestion_engine(tmp_path, "fahrenheit", 2.0).generate_suggestions())
        assert "up by about 2\u00b0F" in text

    def test_default_constructor_is_fahrenheit(self, tmp_path):
        engine = LearningEngine(tmp_path)
        assert engine._temp_unit == "fahrenheit"

    def test_celsius_weather_bias_suggestion_says_celsius(self, tmp_path):
        engine = LearningEngine(tmp_path, temp_unit="celsius")
        engine._state = LearningState()
        engine._state.records = _suggestion_engine(tmp_path, "celsius", 0.0)._state.records
        engine.get_weather_bias = lambda: {"confidence": "high", "high_bias": -3.6, "low_bias": 0.0}
        text = " ".join(engine.generate_suggestions())
        assert "about 2.0\u00b0C higher" in text


class TestThermalObservationSetpointSource:
    def test_thermal_observation_does_not_read_a_nonexistent_attribute(self):
        """Climate entities have no ``target_temperature`` attribute — the read was always None."""
        import inspect

        from tests.test_hvac_session_detection import _get_coordinator_class

        cls = _get_coordinator_class()
        # Issue #1234: the setpoint read moved into the one shared helper (also used by the swing sampler).
        assert "self._read_thermostat_setpoint_f(_cs_sw" in inspect.getsource(cls._start_hvac_observation)
        src = inspect.getsource(cls._read_thermostat_setpoint_f)
        assert 'get("target_temperature")' not in src
        assert 'read_state_temp_f(state, "temperature"' in src
