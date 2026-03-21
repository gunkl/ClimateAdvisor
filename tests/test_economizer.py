"""Tests for the two-phase economizer (window cooling) feature (Issue #27).

Tests cover:
- Phase 1 (cool-down): AC runs to cool to set temp when indoor > comfort
- Phase 2 (maintain): AC off when indoor <= comfort, ventilation holds
- Time-bounding: only morning (6-9) and evening (17-24)
- aggressive_savings: skip AC, ventilation only
- Guards: non-HOT days, windows closed, too warm outdoor, no classification
- No double phase transitions
- Deactivation when conditions change
- Serialization round-trip
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DAY_TYPE_HOT,
    DAY_TYPE_WARM,
)

# ---------------------------------------------------------------------------
# Helpers
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

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


def _make_hot_classification() -> DayClassification:
    """Build a HOT DayClassification (bypasses __post_init__)."""
    c = object.__new__(DayClassification)
    c.day_type = DAY_TYPE_HOT
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 90.0
    c.today_low = 70.0
    c.tomorrow_high = 90.0
    c.tomorrow_low = 70.0
    c.hvac_mode = "cool"
    c.pre_condition = True
    c.pre_condition_target = -2.0
    c.windows_recommended = False
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


def _make_warm_classification() -> DayClassification:
    """Build a WARM DayClassification."""
    c = object.__new__(DayClassification)
    c.day_type = DAY_TYPE_WARM
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 80.0
    c.today_low = 62.0
    c.tomorrow_high = 78.0
    c.tomorrow_low = 60.0
    c.hvac_mode = "off"
    c.pre_condition = False
    c.pre_condition_target = None
    c.windows_recommended = True
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


def _get_hvac_mode_calls(engine):
    """Extract set_hvac_mode calls from the mock."""
    return [c for c in engine.hass.services.async_call.call_args_list if c[0][1] == "set_hvac_mode"]


def _get_set_temp_calls(engine):
    """Extract set_temperature calls from the mock."""
    return [c for c in engine.hass.services.async_call.call_args_list if c[0][1] == "set_temperature"]


# ---------------------------------------------------------------------------
# Phase 1: Cool-down (indoor above comfort → AC runs)
# ---------------------------------------------------------------------------


class TestEconomizerCoolDown:
    """Phase 1: AC runs when indoor > comfort and outdoor is favorable."""

    def test_cooldown_activates_ac_when_indoor_above_comfort(self):
        """Indoor 80°F > comfort 75°F, outdoor 73°F → AC runs in cool mode."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,  # evening
            )
        )

        assert result is True
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"
        # Should set HVAC to cool mode and set temperature to comfort
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls)
        temp_calls = _get_set_temp_calls(engine)
        assert any(c[0][2]["temperature"] == 75 for c in temp_calls)

    def test_cooldown_no_repeat_calls_same_phase(self):
        """Calling again while still in cool-down doesn't re-issue commands."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=78.0,  # still above comfort
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_phase == "cool-down"
        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 2: Maintain (indoor at or below comfort → AC off)
# ---------------------------------------------------------------------------


class TestEconomizerMaintain:
    """Phase 2: AC off when indoor <= comfort, ventilation holds."""

    def test_maintain_turns_ac_off_when_indoor_at_comfort(self):
        """Indoor 75°F == comfort 75°F → AC off, maintain phase."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=75.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is True
        assert engine._economizer_active is True
        assert engine._economizer_phase == "maintain"
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)

    def test_maintain_when_indoor_below_comfort(self):
        """Indoor 72°F < comfort 75°F → AC off, maintain phase."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=72.0,
                windows_physically_open=True,
                current_hour=20,
            )
        )

        assert result is True
        assert engine._economizer_phase == "maintain"

    def test_transition_cooldown_to_maintain(self):
        """Indoor drops from above to at comfort → transitions cool-down → maintain."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=74.0,  # now at/below comfort
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is True
        assert engine._economizer_phase == "maintain"
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)

    def test_maintain_when_indoor_temp_is_none(self):
        """No indoor temp data → defaults to maintain (AC off)."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=None,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_phase == "maintain"


# ---------------------------------------------------------------------------
# Time-bounding
# ---------------------------------------------------------------------------


class TestEconomizerTimeBounding:
    """Economizer only active during morning (6-9) and evening (17-24)."""

    def test_active_during_morning(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=7,
            )
        )
        assert result is True

    def test_active_during_evening(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=20,
            )
        )
        assert result is True

    def test_inactive_during_midday(self):
        """Midday (hour 12) → economizer does not activate."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=12,
            )
        )
        assert result is False
        assert engine._economizer_active is False

    def test_deactivates_when_leaving_time_window(self):
        """Active economizer deactivates when time goes outside window."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=10,  # outside window
            )
        )
        assert result is False
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"


# ---------------------------------------------------------------------------
# aggressive_savings flag
# ---------------------------------------------------------------------------


class TestEconomizerAggressiveSavings:
    """When aggressive_savings=True, skip AC assist, ventilation only."""

    def test_savings_mode_goes_directly_to_maintain(self):
        """With aggressive_savings, indoor above comfort still uses ventilation only."""
        engine = _make_automation_engine({"aggressive_savings": True})
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_phase == "maintain"
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)
        # Should NOT have set cool mode
        assert not any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls)

    def test_comfort_mode_uses_ac_for_cooldown(self):
        """With aggressive_savings=False (default), AC runs for cool-down."""
        engine = _make_automation_engine({"aggressive_savings": False})
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_phase == "cool-down"
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class TestEconomizerGuards:
    """Economizer does not activate when conditions aren't met."""

    def test_ignores_non_hot_days(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_warm_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=68.0,
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert result is False
        engine.hass.services.async_call.assert_not_called()

    def test_returns_false_when_no_classification(self):
        engine = _make_automation_engine()
        engine._current_classification = None

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert result is False
        engine.hass.services.async_call.assert_not_called()

    def test_respects_windows_closed(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=76.0,
                windows_physically_open=False,
                current_hour=18,
            )
        )
        assert result is False
        engine.hass.services.async_call.assert_not_called()

    def test_does_not_activate_when_outdoor_too_warm(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=79.0,  # 75 + 3 = 78; 79 > threshold
                indoor_temp=76.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert result is False
        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Deactivation
# ---------------------------------------------------------------------------


class TestEconomizerDeactivation:
    """Economizer deactivates when conditions change."""

    def test_deactivates_when_temp_rises(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=80.0,
                indoor_temp=76.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert result is False
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"
        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls)

    def test_deactivates_when_windows_closed(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=75.0,
                windows_physically_open=False,
                current_hour=18,
            )
        )
        assert result is False
        assert engine._economizer_active is False

    def test_deactivates_on_non_hot_reclassification(self):
        """If day reclassifies from HOT to WARM mid-day, economizer stops."""
        engine = _make_automation_engine()
        engine._current_classification = _make_warm_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert result is False
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestEconomizerSerialization:
    """Economizer state is correctly serialized and restored."""

    def test_phase_included_in_serialization(self):
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        state = engine.get_serializable_state()
        assert state["economizer_active"] is True
        assert state["economizer_phase"] == "cool-down"

    def test_inactive_included_in_serialization(self):
        engine = _make_automation_engine()
        state = engine.get_serializable_state()
        assert state["economizer_active"] is False
        assert state["economizer_phase"] == "inactive"

    def test_restored_from_state(self):
        engine = _make_automation_engine()
        engine.restore_state({"economizer_active": True, "economizer_phase": "maintain"})
        assert engine._economizer_active is True
        assert engine._economizer_phase == "maintain"

    def test_defaults_on_restore(self):
        engine = _make_automation_engine()
        engine.restore_state({})
        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"
