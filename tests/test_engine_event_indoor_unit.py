"""Issue #1038: engine-emitted indoor temperatures are unit-correct and source-aware.

Occupant experience: on a Celsius install the Activity Log showed engine-written rows
(override detected, comfort band applied, occupancy setback/restore, nat-vent
manual-override exit) with the indoor temperature wildly wrong (21.5 C rendered as
-5.8 C) because ``_indoor_f_for_event`` read the raw thermostat attribute with no unit
conversion, no plausibility check, and no regard for the configured indoor source.

Every test drives the REAL engine method that emits the event and captures the
``_emit_event_callback`` payload.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.climate_advisor.automation as _automation_mod
from custom_components.climate_advisor.ai_skills_context import _render_timeline_events
from custom_components.climate_advisor.automation import AutomationEngine, ComfortBand
from custom_components.climate_advisor.const import CLIMATE_FEATURE_TARGET_TEMP_RANGE
from custom_components.climate_advisor.override_grace_fsm import OverrideGraceFsmEventKind

_NOW = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)


def _make_engine(
    *,
    climate_temp,
    unit: str = "fahrenheit",
    sensor_state: str | None = None,
    config_overrides: dict | None = None,
) -> tuple[AutomationEngine, list[tuple], list]:
    """Real engine; returns (engine, captured events, captured scheduled coroutines)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    scheduled: list = []
    hass.async_create_task = MagicMock(side_effect=scheduled.append)

    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {
        "current_temperature": climate_temp,
        "temperature": None,
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE | 1,
    }

    def _states_get(entity_id: str):
        if entity_id == "climate.thermostat":
            return climate_state
        if entity_id == "sensor.indoor_temp" and sensor_state is not None:
            s = MagicMock()
            s.state = sensor_state
            s.attributes = {}
            return s
        return None

    hass.states.get = _states_get

    config = {
        "comfort_heat": 68,
        "comfort_cool": 76,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "indoor_temp_source": "climate_fallback",
        "temp_unit": unit,
    }
    config.update(config_overrides or {})
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )
    events: list[tuple] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    return engine, events, scheduled


def _band_event_indoor(engine: AutomationEngine, events: list[tuple]) -> float | None:
    band = ComfortBand(floor=68.0, ceiling=76.0, active="ceiling", reason="test")
    with patch.object(_automation_mod.dt_util, "now", return_value=_NOW):
        asyncio.run(engine._apply_comfort_band(band, reason="test"))
    matches = [p for n, p in events if n == "comfort_band_applied"]
    assert len(matches) == 1, events
    return matches[0]["indoor_f"]


class TestComfortBandAppliedIndoor:
    def test_celsius_install_is_converted(self):
        engine, events, _ = _make_engine(climate_temp=21.5, unit="celsius")
        assert _band_event_indoor(engine, events) == pytest.approx(70.7, abs=0.05)

    def test_sensor_source_wins_over_thermostat(self):
        engine, events, _ = _make_engine(
            climate_temp=75.0,
            sensor_state="68",
            config_overrides={"indoor_temp_source": "sensor", "indoor_temp_entity": "sensor.indoor_temp"},
        )
        assert _band_event_indoor(engine, events) == pytest.approx(68.0)

    @pytest.mark.parametrize("bad", [float("nan"), 500.0])
    def test_implausible_reading_is_none(self, bad):
        engine, events, _ = _make_engine(climate_temp=bad)
        assert _band_event_indoor(engine, events) is None

    def test_fahrenheit_in_range_unchanged(self):
        engine, events, _ = _make_engine(climate_temp=72.5)
        assert _band_event_indoor(engine, events) == pytest.approx(72.5)


def _start_override(engine: AutomationEngine, scheduled: list) -> None:
    with (
        patch.object(_automation_mod.dt_util, "now", return_value=_NOW),
        patch.object(_automation_mod, "async_call_later", MagicMock(return_value=MagicMock())),
        patch.object(engine, "_whf_owns_hvac", return_value=True),
    ):
        engine._deactivate_fan = AsyncMock(return_value=_automation_mod.FanCommandResult.EXECUTED)
        engine.start_override_confirmation("normal", event_kind=OverrideGraceFsmEventKind.OVERRIDE_DETECTED)

        async def _drain():
            for coro in scheduled:
                await coro

        asyncio.run(_drain())


class TestOverrideEventsIndoor:
    def test_celsius_override_detected_and_nat_vent_exit(self):
        engine, events, scheduled = _make_engine(climate_temp=21.5, unit="celsius")
        _start_override(engine, scheduled)

        detected = [p for n, p in events if n == "override_detected"]
        exits = [p for n, p in events if n == "nat_vent_manual_override_exit"]
        assert len(detected) == 1 and len(exits) == 1, events
        assert detected[0]["indoor_f"] == pytest.approx(70.7, abs=0.05)
        assert exits[0]["indoor_temp"] == pytest.approx(70.7, abs=0.05)

    def test_sensor_source_override_events(self):
        engine, events, scheduled = _make_engine(
            climate_temp=75.0,
            sensor_state="68",
            config_overrides={"indoor_temp_source": "sensor", "indoor_temp_entity": "sensor.indoor_temp"},
        )
        _start_override(engine, scheduled)
        detected = [p for n, p in events if n == "override_detected"]
        exits = [p for n, p in events if n == "nat_vent_manual_override_exit"]
        assert detected[0]["indoor_f"] == pytest.approx(68.0)
        assert exits[0]["indoor_temp"] == pytest.approx(68.0)

    def test_implausible_reading_override_events_are_none(self):
        engine, events, scheduled = _make_engine(climate_temp=500.0)
        _start_override(engine, scheduled)
        detected = [p for n, p in events if n == "override_detected"]
        exits = [p for n, p in events if n == "nat_vent_manual_override_exit"]
        assert detected[0]["indoor_f"] is None
        assert exits[0]["indoor_temp"] is None


class TestOccupancySetbackIndoor:
    def test_celsius_occupancy_setback_indoor(self):
        engine, events, _ = _make_engine(climate_temp=21.5, unit="celsius")
        engine._current_classification = MagicMock(hvac_mode="cool")
        with patch.object(_automation_mod.dt_util, "now", return_value=_NOW):
            asyncio.run(engine.handle_occupancy_away())
        setbacks = [p for n, p in events if n == "occupancy_setback"]
        assert len(setbacks) == 1, [n for n, _ in events]
        assert setbacks[0]["indoor_f"] == pytest.approx(70.7, abs=0.05)


class TestRenderedCelsius:
    def test_celsius_event_renders_celsius_not_minus_5_8c(self):
        engine, events, _ = _make_engine(climate_temp=21.5, unit="celsius")
        _band_event_indoor(engine, events)
        name, payload = next((n, p) for n, p in events if n == "comfort_band_applied")
        raw = [{"time": (_NOW - _dt.timedelta(minutes=1)).isoformat(), "type": name, **payload}]
        rendered, _limited = _render_timeline_events(raw, {"temp_unit": "celsius"}, hours=1.0, now=_NOW)
        assert len(rendered) == 1
        cell = rendered[0].indoor
        assert cell.startswith("22"), cell  # 21.5 C rounded for display
        assert "-" not in cell, cell
