"""Tests for manual-override guards in handle_bedtime() and handle_morning_wakeup().

Covers:
- handle_bedtime() returns early when _manual_override_active=True,
  emits bedtime_setback_skipped event, does NOT clear the override
- handle_bedtime() proceeds, logs WARNING, and clears override
  when _manual_override_active=False
- handle_morning_wakeup() returns early when _manual_override_active=True,
  emits morning_wakeup_skipped event, does NOT clear the override
- handle_morning_wakeup() proceeds, logs WARNING, and clears override
  when _manual_override_active=False
- clear_manual_override(reason=...) includes the reason string in the log
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import OCCUPANCY_HOME

# Patch dt_util.now to return a real datetime (needed for isoformat() calls)
sys.modules["homeassistant.util.dt"].now = lambda: datetime(2026, 3, 20, 22, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_classification(hvac_mode: str = "heat") -> DayClassification:
    """Create a minimal DayClassification without __post_init__ validation."""
    obj = object.__new__(DayClassification)
    obj.day_type = "cold"
    obj.hvac_mode = hvac_mode
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0.0
    obj.today_high = 55.0
    obj.today_low = 40.0
    obj.tomorrow_high = 56.0
    obj.tomorrow_low = 41.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_active_override(engine: AutomationEngine, mode: str = "cool") -> None:
    """Put the engine into an active manual override state."""
    engine._manual_override_active = True
    engine._manual_override_mode = mode
    engine._manual_override_time = "2026-03-20T20:00:00"


# ---------------------------------------------------------------------------
# TestBedtimeSetbackOverrideGuard
# ---------------------------------------------------------------------------


class TestBedtimeSetbackOverrideGuard:
    """handle_bedtime() must skip when a manual override is active."""

    def test_active_override_skips_handler(self):
        """Returns early without clearing override when _manual_override_active=True."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine)

        asyncio.run(engine.handle_bedtime())

        # Override must still be active after early return
        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"

    def test_active_override_emits_skipped_event(self):
        """Emits bedtime_setback_skipped event with reason=manual_override."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_bedtime())

        assert any(
            name == "bedtime_setback_skipped" and payload.get("reason") == "manual_override" for name, payload in events
        ), f"Expected bedtime_setback_skipped/manual_override in {events}"

    def test_active_override_logs_skip(self, caplog):
        """Logs an INFO message naming the override mode when skipping."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine, mode="cool")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            asyncio.run(engine.handle_bedtime())

        assert any("manual override active" in r.message for r in caplog.records), (
            f"Expected skip log in: {[r.message for r in caplog.records]}"
        )

    def test_no_override_clears_and_warns(self, caplog):
        """When _manual_override_active=False, logs WARNING and clears override state."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        # No active override; set classification so handler can proceed
        engine._current_classification = _make_classification(hvac_mode="heat")

        with (
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.automation"),
            patch.object(engine, "_set_temperature", new_callable=AsyncMock),
        ):
            asyncio.run(engine.handle_bedtime())

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Bedtime setback: clearing" in m for m in warning_messages), (
            f"Expected warning log in: {warning_messages}"
        )
        # Override should remain cleared (was already False; cleared state is consistent)
        assert engine._manual_override_active is False

    def test_no_override_does_not_emit_skipped_event(self):
        """When override is inactive, bedtime_setback_skipped/manual_override is NOT emitted."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        engine._current_classification = _make_classification(hvac_mode="heat")

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch.object(engine, "_set_temperature", new_callable=AsyncMock):
            asyncio.run(engine.handle_bedtime())

        manual_override_skips = [
            (n, p) for n, p in events if n == "bedtime_setback_skipped" and p.get("reason") == "manual_override"
        ]
        assert manual_override_skips == [], f"Unexpected skip events: {manual_override_skips}"


# ---------------------------------------------------------------------------
# TestMorningWakeupOverrideGuard
# ---------------------------------------------------------------------------


class TestMorningWakeupOverrideGuard:
    """handle_morning_wakeup() must skip when a manual override is active."""

    def test_active_override_skips_handler(self):
        """Returns early without clearing override when _manual_override_active=True."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine)

        asyncio.run(engine.handle_morning_wakeup())

        assert engine._manual_override_active is True
        assert engine._manual_override_mode == "cool"

    def test_active_override_emits_skipped_event(self):
        """Emits morning_wakeup_skipped event with reason=manual_override."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine)

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        asyncio.run(engine.handle_morning_wakeup())

        assert any(
            name == "morning_wakeup_skipped" and payload.get("reason") == "manual_override" for name, payload in events
        ), f"Expected morning_wakeup_skipped/manual_override in {events}"

    def test_active_override_logs_skip(self, caplog):
        """Logs an INFO message naming the override mode when skipping."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        _set_active_override(engine, mode="heat")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            asyncio.run(engine.handle_morning_wakeup())

        assert any("manual override active" in r.message for r in caplog.records), (
            f"Expected skip log in: {[r.message for r in caplog.records]}"
        )

    def test_no_override_clears_and_warns(self, caplog):
        """When _manual_override_active=False, logs WARNING and clears override state."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        engine._current_classification = _make_classification(hvac_mode="heat")

        with (
            caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.automation"),
            patch.object(engine, "_set_temperature", new_callable=AsyncMock),
        ):
            asyncio.run(engine.handle_morning_wakeup())

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Morning wakeup: clearing" in m for m in warning_messages), (
            f"Expected warning log in: {warning_messages}"
        )
        assert engine._manual_override_active is False

    def test_no_override_does_not_emit_skipped_event(self):
        """When override is inactive, morning_wakeup_skipped/manual_override is NOT emitted."""
        engine = _make_engine()
        engine._occupancy_mode = OCCUPANCY_HOME
        engine._current_classification = _make_classification(hvac_mode="heat")

        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch.object(engine, "_set_temperature", new_callable=AsyncMock):
            asyncio.run(engine.handle_morning_wakeup())

        manual_override_skips = [
            (n, p) for n, p in events if n == "morning_wakeup_skipped" and p.get("reason") == "manual_override"
        ]
        assert manual_override_skips == [], f"Unexpected skip events: {manual_override_skips}"


# ---------------------------------------------------------------------------
# TestClearManualOverrideReason
# ---------------------------------------------------------------------------


class TestClearManualOverrideReason:
    """clear_manual_override(reason=...) must include the reason in log output."""

    def test_reason_appears_in_log_grace_expired(self, caplog):
        """reason='grace_expired' is logged when an active override is cleared."""
        engine = _make_engine()
        _set_active_override(engine, mode="heat")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            engine.clear_manual_override(reason="grace_expired")

        assert any("grace_expired" in r.message for r in caplog.records), (
            f"Expected 'grace_expired' in log: {[r.message for r in caplog.records]}"
        )

    def test_reason_appears_in_log_bedtime(self, caplog):
        """reason='bedtime' is logged when an active override is cleared at bedtime."""
        engine = _make_engine()
        _set_active_override(engine, mode="heat")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            engine.clear_manual_override(reason="bedtime")

        assert any("bedtime" in r.message for r in caplog.records), (
            f"Expected 'bedtime' in log: {[r.message for r in caplog.records]}"
        )

    def test_reason_appears_in_log_morning_wakeup(self, caplog):
        """reason='morning_wakeup' is logged when override cleared at wakeup."""
        engine = _make_engine()
        _set_active_override(engine, mode="cool")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            engine.clear_manual_override(reason="morning_wakeup")

        assert any("morning_wakeup" in r.message for r in caplog.records), (
            f"Expected 'morning_wakeup' in log: {[r.message for r in caplog.records]}"
        )

    def test_default_reason_is_grace_expired(self, caplog):
        """Default reason='grace_expired' is used when no reason is passed."""
        engine = _make_engine()
        _set_active_override(engine, mode="cool")

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            engine.clear_manual_override()

        assert any("grace_expired" in r.message for r in caplog.records), (
            f"Expected 'grace_expired' in log: {[r.message for r in caplog.records]}"
        )

    def test_no_log_when_override_not_active(self, caplog):
        """When _manual_override_active=False, no clearing log is emitted."""
        engine = _make_engine()
        # override is inactive by default

        with caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.automation"):
            engine.clear_manual_override(reason="bedtime")

        clearing_logs = [r for r in caplog.records if "Clearing manual override" in r.message]
        assert clearing_logs == [], f"Unexpected clearing log when no override active: {clearing_logs}"
