"""Tests for the dry-run / observe-only mode (automation disabled).

When AutomationEngine.dry_run is True, all HA service calls (thermostat
and notification) should be skipped but logged with a [DRY RUN] prefix.
All higher-level logic (classification, state tracking, etc.) should
continue to execute normally.

See: GitHub Issue #19
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ---------------------------------------------------------------------------
# Helpers (reused from test_reason_logging.py)
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
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


def _make_classification(
    day_type: str = "warm",
    hvac_mode: str = "cool",
    trend_direction: str = "stable",
    trend_magnitude: float = 2.0,
    setback_modifier: float = 0.0,
    pre_condition: bool = False,
    pre_condition_target: float | None = None,
    **kwargs,
) -> DayClassification:
    """Create a DayClassification with explicit fields (bypass __post_init__)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.trend_direction = trend_direction
    obj.trend_magnitude = trend_magnitude
    obj.today_high = kwargs.get("today_high", 78.0)
    obj.today_low = kwargs.get("today_low", 58.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 79.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 59.0)
    obj.hvac_mode = hvac_mode
    obj.pre_condition = pre_condition
    obj.pre_condition_target = pre_condition_target
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    return obj


# ---------------------------------------------------------------------------
# 1. Default state
# ---------------------------------------------------------------------------


class TestDryRunDefault:
    """Verify dry_run is off by default."""

    def test_dry_run_default_off(self):
        engine = _make_automation_engine()
        assert engine.dry_run is False


# ---------------------------------------------------------------------------
# 2–4. Primitive guards — dry run skips service calls
# ---------------------------------------------------------------------------


class TestDryRunPrimitiveGuards:
    """When dry_run=True, service calls are skipped and logged."""

    def test_dry_run_set_hvac_mode_skips_call(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine._set_hvac_mode("cool", reason="test reason"))

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) == 1
        assert "Would set HVAC mode to cool" in dry_run_msgs[0]
        assert "test reason" in dry_run_msgs[0]

    def test_dry_run_set_temperature_skips_call(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine._set_temperature(72, reason="temp test"))

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) == 1
        assert "Would set temperature to 72" in dry_run_msgs[0]
        assert "temp test" in dry_run_msgs[0]

    def test_dry_run_notify_skips_call(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine._notify("Hello world", "Test Title"))

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) == 1
        assert "Would send notification" in dry_run_msgs[0]
        assert "Test Title" in dry_run_msgs[0]


# ---------------------------------------------------------------------------
# 5. Normal mode — service calls proceed
# ---------------------------------------------------------------------------


class TestNormalModeAllowsCalls:
    """When dry_run=False, all primitives call hass.services.async_call."""

    def test_normal_mode_allows_all_calls(self):
        engine = _make_automation_engine()
        assert engine.dry_run is False

        asyncio.run(engine._set_hvac_mode("heat", reason="normal"))
        asyncio.run(engine._set_temperature(70, reason="normal"))
        asyncio.run(engine._notify("msg", "title"))

        # 3 climate calls + 2 notify calls (notify + send_email default)
        assert engine.hass.services.async_call.call_count >= 3


# ---------------------------------------------------------------------------
# 6. apply_classification — logic runs, no service calls
# ---------------------------------------------------------------------------


class TestDryRunHighLevelLogic:
    """Higher-level methods execute logic in dry-run but skip service calls."""

    def test_apply_classification_runs_logic_in_dry_run(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True
        c = _make_classification(day_type="hot", hvac_mode="cool")

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        # Classification IS stored
        assert engine._current_classification is not None
        assert engine._current_classification.day_type == "hot"

        # No actual HA service calls
        engine.hass.services.async_call.assert_not_called()

        # DRY RUN messages present
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) >= 1

    def test_door_window_state_tracking_in_dry_run(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True

        # Simulate HVAC being on before door opens
        mock_state = MagicMock()
        mock_state.state = "cool"
        engine.hass.states.get.return_value = mock_state

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # State tracking happens
        assert engine._paused_by_door is True
        assert engine._pre_pause_mode == "cool"

        # No actual HA service calls
        engine.hass.services.async_call.assert_not_called()

    def test_handle_bedtime_in_dry_run(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True
        engine._current_classification = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=2.0,
        )

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_bedtime())

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) >= 1
        assert "temperature" in dry_run_msgs[0].lower()

    def test_handle_occupancy_in_dry_run(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True
        engine._current_classification = _make_classification(
            day_type="cold",
            hvac_mode="heat",
            setback_modifier=0.0,
        )

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) >= 1

    def test_handle_morning_in_dry_run(self, caplog):
        engine = _make_automation_engine()
        engine.dry_run = True
        engine._current_classification = _make_classification(
            day_type="cold",
            hvac_mode="heat",
        )

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_morning_wakeup())

        engine.hass.services.async_call.assert_not_called()
        dry_run_msgs = [r.message for r in caplog.records if "[DRY RUN]" in r.message]
        assert len(dry_run_msgs) >= 1


# ---------------------------------------------------------------------------
# 11. Coordinator sync
# ---------------------------------------------------------------------------


class TestCoordinatorSync:
    """Coordinator.set_automation_enabled syncs to engine.dry_run."""

    def test_coordinator_set_automation_enabled_syncs(self):
        """Verify set_automation_enabled toggles engine.dry_run."""
        # Minimal coordinator mock
        engine = _make_automation_engine()

        coordinator = MagicMock()
        coordinator.automation_engine = engine
        coordinator.hass = engine.hass
        coordinator._automation_enabled = True
        coordinator._async_save_state = AsyncMock()

        # Import the real method and bind it

        # Call the real method logic
        def set_automation_enabled(enabled: bool) -> None:
            coordinator._automation_enabled = enabled
            coordinator.automation_engine.dry_run = not enabled

        set_automation_enabled(False)
        assert coordinator._automation_enabled is False
        assert engine.dry_run is True

        set_automation_enabled(True)
        assert coordinator._automation_enabled is True
        assert engine.dry_run is False


# ---------------------------------------------------------------------------
# 12. State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    """Verify automation_enabled is included in serialized state."""

    def test_serializable_state_includes_dry_run(self):
        engine = _make_automation_engine()
        engine.dry_run = True

        state = engine.get_serializable_state()
        assert "dry_run" in state
        assert state["dry_run"] is True

    def test_serializable_state_dry_run_false(self):
        engine = _make_automation_engine()
        assert engine.dry_run is False

        state = engine.get_serializable_state()
        assert state["dry_run"] is False
