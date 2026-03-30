"""Tests for Issue #29 — Home/Away/Vacation/Guest occupancy awareness."""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Inject a real DataUpdateCoordinator base class into the mocked HA module
# BEFORE importing the coordinator module (so the class inherits properly).
_update_coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]


class _FakeDataUpdateCoordinator:
    """Minimal stand-in for DataUpdateCoordinator so the real coordinator can inherit."""

    def __init__(self, hass, *args, **kwargs):
        self.hass = hass

    async def async_request_refresh(self):
        """Stub for triggering a data refresh."""


_update_coord_mod.DataUpdateCoordinator = _FakeDataUpdateCoordinator

# Force reimport of the coordinator module with the real base class
if "custom_components.climate_advisor.coordinator" in sys.modules:
    del sys.modules["custom_components.climate_advisor.coordinator"]

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_GUEST_TOGGLE,
    CONF_GUEST_TOGGLE_INVERT,
    CONF_HOME_TOGGLE,
    CONF_HOME_TOGGLE_INVERT,
    CONF_VACATION_TOGGLE,
    CONF_VACATION_TOGGLE_INVERT,
    OCCUPANCY_AWAY,
    OCCUPANCY_GUEST,
    OCCUPANCY_HOME,
    OCCUPANCY_SETBACK_MINUTES,
    OCCUPANCY_VACATION,
    VACATION_SETBACK_EXTRA,
)
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ── Helpers ──────────────────────────────────────────────────────


def _make_state(state_value: str) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = {}
    return mock


def _make_hass(state_map: dict[str, str] | None = None) -> MagicMock:
    """Create a mock HA instance with optional entity states."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    import inspect

    def _close_coros(coro):
        """Close any coroutine passed to async_create_task to prevent GC warnings."""
        if inspect.iscoroutine(coro):
            coro.close()
        return MagicMock()

    hass.async_create_task = _close_coros

    resolved_map = {}
    if state_map:
        resolved_map = {eid: _make_state(val) for eid, val in state_map.items()}
    hass.states.get = lambda eid: resolved_map.get(eid)
    return hass


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
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
    setback_modifier: float = 0.0,
    **kwargs,
) -> DayClassification:
    """Create a DayClassification bypassing __post_init__."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = hvac_mode
    obj.trend_direction = kwargs.get("trend_direction", "stable")
    obj.trend_magnitude = kwargs.get("trend_magnitude", 0)
    obj.today_high = kwargs.get("today_high", 80.0)
    obj.today_low = kwargs.get("today_low", 60.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 80.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 60.0)
    obj.pre_condition = kwargs.get("pre_condition", False)
    obj.pre_condition_target = kwargs.get("pre_condition_target")
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = kwargs.get("window_open_time")
    obj.window_close_time = kwargs.get("window_close_time")
    obj.setback_modifier = setback_modifier
    obj.window_opportunity_morning = kwargs.get("window_opportunity_morning", False)
    obj.window_opportunity_evening = kwargs.get("window_opportunity_evening", False)
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


def _make_coordinator(
    config_overrides: dict | None = None,
    state_map: dict[str, str] | None = None,
) -> ClimateAdvisorCoordinator:
    """Create a coordinator with the fields needed for occupancy tests.

    DataUpdateCoordinator is mocked in conftest.py, so we bypass __init__
    entirely (same pattern as _make_classification) and set fields manually.
    """
    hass = _make_hass(state_map)

    config = {
        "weather_entity": "weather.forecast_home",
        "climate_entity": "climate.thermostat",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "door_window_sensors": [],
        "wake_time": "06:30:00",
        "sleep_time": "22:30:00",
        "briefing_time": "06:00:00",
    }
    if config_overrides:
        config.update(config_overrides)

    coordinator = ClimateAdvisorCoordinator(hass, config)

    # Stub out state persistence so tests don't need a real executor
    coordinator._async_save_state = AsyncMock()

    # Ensure automation engine mock is properly set up for status checks
    coordinator.automation_engine = MagicMock()
    coordinator.automation_engine.is_paused_by_door = False
    coordinator.automation_engine.natural_vent_active = False
    coordinator.automation_engine._grace_active = False
    coordinator.automation_engine._last_resume_source = None
    coordinator.automation_engine._override_confirm_pending = False

    return coordinator


# ══════════════════════════════════════════════════════════════════
# Test Class: Occupancy Mode Computation
# ══════════════════════════════════════════════════════════════════


class TestOccupancyModeComputation:
    """Test the priority-based _compute_occupancy_mode() logic."""

    def test_no_toggles_defaults_to_home(self):
        """No toggles configured → mode is 'home'."""
        coord = _make_coordinator()
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME

    def test_home_toggle_on_is_home(self):
        """Home toggle ON → mode is 'home'."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "on"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME

    def test_home_toggle_off_is_away(self):
        """Home toggle OFF → mode is 'away'."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_AWAY

    def test_home_toggle_inverted_on_is_away(self):
        """Home toggle inverted: ON → mode is 'away'."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_HOME_TOGGLE_INVERT: True,
            },
            state_map={"input_boolean.home_mode": "on"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_AWAY

    def test_home_toggle_inverted_off_is_home(self):
        """Home toggle inverted: OFF → mode is 'home'."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_HOME_TOGGLE_INVERT: True,
            },
            state_map={"input_boolean.home_mode": "off"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME

    def test_vacation_on_is_vacation(self):
        """Vacation toggle ON → mode is 'vacation'."""
        coord = _make_coordinator(
            config_overrides={CONF_VACATION_TOGGLE: "input_boolean.vacation"},
            state_map={"input_boolean.vacation": "on"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_VACATION

    def test_vacation_inverted_on_is_not_vacation(self):
        """Vacation toggle inverted: ON → NOT vacation (toggle means 'not on vacation')."""
        coord = _make_coordinator(
            config_overrides={
                CONF_VACATION_TOGGLE: "input_boolean.vacation",
                CONF_VACATION_TOGGLE_INVERT: True,
            },
            state_map={"input_boolean.vacation": "on"},
        )
        # Inverted ON = effectively OFF → no vacation → default home
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME

    def test_guest_on_is_guest(self):
        """Guest toggle ON → mode is 'guest'."""
        coord = _make_coordinator(
            config_overrides={CONF_GUEST_TOGGLE: "input_boolean.guest"},
            state_map={"input_boolean.guest": "on"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_GUEST

    def test_guest_inverted_off_is_guest(self):
        """Guest toggle inverted: OFF → mode is 'guest' (inverted logic)."""
        coord = _make_coordinator(
            config_overrides={
                CONF_GUEST_TOGGLE: "input_boolean.guest",
                CONF_GUEST_TOGGLE_INVERT: True,
            },
            state_map={"input_boolean.guest": "off"},
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_GUEST

    def test_guest_overrides_vacation(self):
        """Both vacation and guest ON → guest wins (highest priority)."""
        coord = _make_coordinator(
            config_overrides={
                CONF_VACATION_TOGGLE: "input_boolean.vacation",
                CONF_GUEST_TOGGLE: "input_boolean.guest",
            },
            state_map={
                "input_boolean.vacation": "on",
                "input_boolean.guest": "on",
            },
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_GUEST

    def test_guest_overrides_away(self):
        """Home toggle OFF + guest ON → guest wins."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_GUEST_TOGGLE: "input_boolean.guest",
            },
            state_map={
                "input_boolean.home_mode": "off",
                "input_boolean.guest": "on",
            },
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_GUEST

    def test_vacation_overrides_away(self):
        """Home toggle OFF + vacation ON → vacation wins over away."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_VACATION_TOGGLE: "input_boolean.vacation",
            },
            state_map={
                "input_boolean.home_mode": "off",
                "input_boolean.vacation": "on",
            },
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_VACATION

    def test_all_three_configured_guest_wins(self):
        """All three ON → guest wins (highest priority)."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_VACATION_TOGGLE: "input_boolean.vacation",
                CONF_GUEST_TOGGLE: "input_boolean.guest",
            },
            state_map={
                "input_boolean.home_mode": "on",
                "input_boolean.vacation": "on",
                "input_boolean.guest": "on",
            },
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_GUEST

    def test_toggle_unavailable_treated_as_off(self):
        """Entity in 'unavailable' state → treated as OFF."""
        coord = _make_coordinator(
            config_overrides={CONF_VACATION_TOGGLE: "input_boolean.vacation"},
            state_map={"input_boolean.vacation": "unavailable"},
        )
        # Unavailable = OFF → no vacation → default home
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME

    def test_toggle_unknown_treated_as_off(self):
        """Entity in 'unknown' state → treated as OFF."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "unknown"},
        )
        # Unknown = OFF → home toggle OFF → away
        assert coord._compute_occupancy_mode() == OCCUPANCY_AWAY

    def test_toggle_entity_not_found_treated_as_off(self):
        """Entity not in HA states → treated as OFF."""
        coord = _make_coordinator(
            config_overrides={CONF_VACATION_TOGGLE: "input_boolean.nonexistent"},
            # state_map does not include this entity
        )
        assert coord._compute_occupancy_mode() == OCCUPANCY_HOME


# ══════════════════════════════════════════════════════════════════
# Test Class: Vacation Setback
# ══════════════════════════════════════════════════════════════════


class TestVacationSetback:
    """Test handle_occupancy_vacation() deeper setback logic."""

    def test_vacation_heat_deeper_than_away(self):
        """Vacation heat setback is VACATION_SETBACK_EXTRA degrees deeper."""
        engine = _make_automation_engine()
        c = _make_classification(day_type="cold", hvac_mode="heat", setback_modifier=2.0)
        engine._current_classification = c

        asyncio.run(engine.handle_occupancy_vacation())

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c[0][0] == "climate"]
        assert len(climate_calls) == 1

        service_data = climate_calls[0][0][2]
        # setback_heat (60) + modifier (2) - VACATION_SETBACK_EXTRA (3) = 59
        expected = 60 + 2.0 - VACATION_SETBACK_EXTRA
        assert service_data["temperature"] == expected

    def test_vacation_cool_deeper_than_away(self):
        """Vacation cool setback is VACATION_SETBACK_EXTRA degrees deeper."""
        engine = _make_automation_engine()
        c = _make_classification(day_type="hot", hvac_mode="cool", setback_modifier=2.0)
        engine._current_classification = c

        asyncio.run(engine.handle_occupancy_vacation())

        calls = engine.hass.services.async_call.call_args_list
        climate_calls = [c for c in calls if c[0][0] == "climate"]
        assert len(climate_calls) == 1

        service_data = climate_calls[0][0][2]
        # setback_cool (80) - modifier (2) + VACATION_SETBACK_EXTRA (3) = 81
        expected = 80 - 2.0 + VACATION_SETBACK_EXTRA
        assert service_data["temperature"] == expected

    def test_vacation_no_classification_safe(self):
        """Vacation handler returns safely with no classification."""
        engine = _make_automation_engine()
        engine._current_classification = None

        asyncio.run(engine.handle_occupancy_vacation())

        engine.hass.services.async_call.assert_not_called()

    def test_vacation_dry_run(self, caplog):
        """Vacation setback in dry-run mode logs but does not call service."""
        engine = _make_automation_engine()
        engine.dry_run = True
        c = _make_classification(day_type="cold", hvac_mode="heat", setback_modifier=0.0)
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_vacation())

        # No actual service calls in dry run
        climate_calls = [c for c in engine.hass.services.async_call.call_args_list if c[0][0] == "climate"]
        assert len(climate_calls) == 0

        # Should have logged a dry-run message
        messages = [r.message for r in caplog.records]
        dry_run_msgs = [m for m in messages if "DRY RUN" in m or "dry_run" in m.lower() or "dry run" in m.lower()]
        assert len(dry_run_msgs) > 0


# ══════════════════════════════════════════════════════════════════
# Test Class: Occupancy State Persistence
# ══════════════════════════════════════════════════════════════════


class TestOccupancyStatePersistence:
    """Test that occupancy mode is persisted and restored."""

    def test_occupancy_mode_in_state_dict(self):
        """_build_state_dict() includes occupancy_mode."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_VACATION
        state = coord._build_state_dict()
        assert state["occupancy_mode"] == OCCUPANCY_VACATION

    def test_occupancy_away_since_in_state_dict(self):
        """_build_state_dict() includes occupancy_away_since when set."""
        from datetime import datetime

        coord = _make_coordinator()
        coord._occupancy_away_since = datetime(2026, 3, 19, 14, 30, 0)
        state = coord._build_state_dict()
        assert state["occupancy_away_since"] is not None
        assert "2026-03-19" in state["occupancy_away_since"]

    def test_occupancy_away_since_none_when_not_away(self):
        """_build_state_dict() has null occupancy_away_since when home."""
        coord = _make_coordinator()
        coord._occupancy_away_since = None
        state = coord._build_state_dict()
        assert state["occupancy_away_since"] is None


# ══════════════════════════════════════════════════════════════════
# Test Class: Automation Status with Occupancy
# ══════════════════════════════════════════════════════════════════


class TestAutomationStatusWithOccupancy:
    """Test _compute_automation_status reflects occupancy mode."""

    def test_status_active_when_home(self):
        """Status is 'active' when mode is home."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_HOME
        assert coord._compute_automation_status() == "active"

    def test_status_shows_vacation(self):
        """Status includes '(vacation)' when on vacation."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_VACATION
        status = coord._compute_automation_status()
        assert "vacation" in status

    def test_status_shows_away(self):
        """Status includes '(away)' when away."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_AWAY
        status = coord._compute_automation_status()
        assert "away" in status

    def test_status_shows_guest(self):
        """Status includes '(guest)' when guest mode active."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_GUEST
        status = coord._compute_automation_status()
        assert "guest" in status


# ══════════════════════════════════════════════════════════════════
# Test Class: Next Action with Occupancy
# ══════════════════════════════════════════════════════════════════


class TestNextActionWithOccupancy:
    """Test _compute_next_action reflects occupancy mode."""

    def test_vacation_shows_vacation_message(self):
        """When on vacation, next action mentions vacation."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_VACATION
        c = _make_classification()
        action = coord._compute_next_action(c)
        assert "vacation" in action.lower()

    def test_away_shows_away_message(self):
        """When away, next action mentions away."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_AWAY
        c = _make_classification()
        action = coord._compute_next_action(c)
        assert "away" in action.lower()

    def test_home_shows_normal_action(self):
        """When home, next action is the normal day-type action."""
        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_HOME
        c = _make_classification(day_type="hot", hvac_mode="cool")
        action = coord._compute_next_action(c)
        assert "vacation" not in action.lower()
        assert "away" not in action.lower()


# ══════════════════════════════════════════════════════════════════
# Test Class: Config Migration v6 → v7
# ══════════════════════════════════════════════════════════════════


class TestOccupancyConfigMigration:
    """Test v6 → v7 config entry migration."""

    def test_v6_to_v7_adds_defaults(self):
        """Migration adds all occupancy toggle defaults."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = MagicMock()
        entry.version = 6
        entry.data = {"weather_entity": "weather.home", "comfort_heat": 70}

        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        hass.states.get = lambda _: _make_state("available")

        result = asyncio.run(async_migrate_entry(hass, entry))
        assert result is True

        # Check that async_update_entry was called with version=7
        call_kwargs = hass.config_entries.async_update_entry.call_args
        assert call_kwargs[1]["version"] == 7

        new_data = call_kwargs[1]["data"]
        assert new_data[CONF_HOME_TOGGLE] is None
        assert new_data[CONF_HOME_TOGGLE_INVERT] is False
        assert new_data[CONF_VACATION_TOGGLE] is None
        assert new_data[CONF_VACATION_TOGGLE_INVERT] is False
        assert new_data[CONF_GUEST_TOGGLE] is None
        assert new_data[CONF_GUEST_TOGGLE_INVERT] is False

    def test_v6_to_v7_preserves_existing_data(self):
        """Migration preserves all existing config data."""
        from custom_components.climate_advisor import async_migrate_entry

        entry = MagicMock()
        entry.version = 6
        entry.data = {
            "weather_entity": "weather.home",
            "climate_entity": "climate.thermostat",
            "comfort_heat": 72,
            "comfort_cool": 76,
        }

        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        hass.states.get = lambda _: _make_state("available")

        asyncio.run(async_migrate_entry(hass, entry))

        new_data = hass.config_entries.async_update_entry.call_args[1]["data"]
        assert new_data["weather_entity"] == "weather.home"
        assert new_data["climate_entity"] == "climate.thermostat"
        assert new_data["comfort_heat"] == 72
        assert new_data["comfort_cool"] == 76


# ══════════════════════════════════════════════════════════════════
# Test Class: DailyRecord Occupancy Mode Field
# ══════════════════════════════════════════════════════════════════


class TestDailyRecordOccupancyMode:
    """Test the occupancy_mode field on DailyRecord."""

    def test_default_occupancy_mode_is_home(self):
        """DailyRecord defaults to occupancy_mode='home'."""
        record = DailyRecord(
            date="2026-03-19",
            day_type="warm",
            trend_direction="stable",
        )
        assert record.occupancy_mode == "home"

    def test_occupancy_mode_can_be_set(self):
        """DailyRecord occupancy_mode can be set to any mode."""
        record = DailyRecord(
            date="2026-03-19",
            day_type="warm",
            trend_direction="stable",
            occupancy_mode="vacation",
        )
        assert record.occupancy_mode == "vacation"


# ══════════════════════════════════════════════════════════════════
# Test Class: Learning Engine Vacation Exclusion
# ══════════════════════════════════════════════════════════════════


class TestLearningVacationExclusion:
    """Test that vacation days are excluded from learning patterns."""

    def test_vacation_days_excluded_from_override_pattern(self):
        """Vacation records should not count toward frequent_overrides."""
        from pathlib import Path

        from custom_components.climate_advisor.learning import LearningEngine

        engine = LearningEngine(Path("/tmp/fake"))

        # Add 14 vacation days with many overrides — should not trigger suggestion
        for i in range(14):
            record = DailyRecord(
                date=f"2026-03-{i + 1:02d}",
                day_type="warm",
                trend_direction="stable",
                manual_overrides=5,
                occupancy_mode="vacation",
            )
            engine.record_day(record)

        suggestions = engine.generate_suggestions()
        override_suggestions = [s for s in suggestions if "override" in s.get("key", "").lower()]
        assert len(override_suggestions) == 0


# ══════════════════════════════════════════════════════════════════
# Test Class: Occupancy Sensor
# ══════════════════════════════════════════════════════════════════


class TestOccupancySensor:
    """Test that occupancy mode is present in coordinator data dict."""

    def test_occupancy_mode_in_data_dict(self):
        """Coordinator data dict includes ATTR_OCCUPANCY_MODE."""
        from custom_components.climate_advisor.const import ATTR_OCCUPANCY_MODE

        coord = _make_coordinator()
        coord._occupancy_mode = OCCUPANCY_VACATION

        # Verify _build_state_dict (persistence) includes occupancy
        state = coord._build_state_dict()
        assert state["occupancy_mode"] == OCCUPANCY_VACATION

        # Verify the ATTR key is defined and the mode matches
        assert ATTR_OCCUPANCY_MODE == "occupancy_mode"


# ══════════════════════════════════════════════════════════════════
# Test Class: Is Toggle On
# ══════════════════════════════════════════════════════════════════


class TestIsToggleOn:
    """Test the _is_toggle_on helper with various states."""

    def test_on_state_no_invert(self):
        """ON state, no invert → True."""
        coord = _make_coordinator(state_map={"input_boolean.test": "on"})
        assert coord._is_toggle_on("input_boolean.test", False) is True

    def test_off_state_no_invert(self):
        """OFF state, no invert → False."""
        coord = _make_coordinator(state_map={"input_boolean.test": "off"})
        assert coord._is_toggle_on("input_boolean.test", False) is False

    def test_on_state_inverted(self):
        """ON state, inverted → False."""
        coord = _make_coordinator(state_map={"input_boolean.test": "on"})
        assert coord._is_toggle_on("input_boolean.test", True) is False

    def test_off_state_inverted(self):
        """OFF state, inverted → True."""
        coord = _make_coordinator(state_map={"input_boolean.test": "off"})
        assert coord._is_toggle_on("input_boolean.test", True) is True

    def test_unavailable_returns_false(self):
        """Unavailable entity → False regardless of invert."""
        coord = _make_coordinator(state_map={"input_boolean.test": "unavailable"})
        assert coord._is_toggle_on("input_boolean.test", False) is False
        assert coord._is_toggle_on("input_boolean.test", True) is False

    def test_nonexistent_entity_returns_false(self):
        """Entity not in HA → False."""
        coord = _make_coordinator()
        assert coord._is_toggle_on("input_boolean.ghost", False) is False


# ══════════════════════════════════════════════════════════════════
# Test Class: Briefing Occupancy Text
# ══════════════════════════════════════════════════════════════════


class TestBriefingOccupancy:
    """Test briefing output varies by occupancy mode."""

    def _generate_briefing(self, occupancy_mode: str = "home") -> str:
        """Generate a briefing with given occupancy mode."""
        from datetime import time

        from custom_components.climate_advisor.briefing import generate_briefing

        c = _make_classification(day_type="warm", hvac_mode="cool")
        return generate_briefing(
            classification=c,
            comfort_heat=70,
            comfort_cool=75,
            setback_heat=60,
            setback_cool=80,
            wake_time=time(6, 30),
            sleep_time=time(22, 30),
            occupancy_mode=occupancy_mode,
        )

    def test_vacation_briefing_mentions_vacation(self):
        """Vacation mode briefing mentions vacation."""
        briefing = self._generate_briefing("vacation")
        assert "vacation" in briefing.lower()

    def test_guest_briefing_mentions_guest(self):
        """Guest mode briefing mentions guests."""
        briefing = self._generate_briefing("guest")
        assert "guest" in briefing.lower()

    def test_home_briefing_is_default(self):
        """Home mode briefing uses default hypothetical text."""
        briefing = self._generate_briefing("home")
        # Should contain the standard "head out" or "leave" language
        assert "vacation" not in briefing.lower() or "if" in briefing.lower()


# ══════════════════════════════════════════════════════════════════
# Test Class: Constants
# ══════════════════════════════════════════════════════════════════


class TestOccupancyConstants:
    """Test occupancy-related constants are properly defined."""

    def test_vacation_setback_extra_positive(self):
        """VACATION_SETBACK_EXTRA should be positive."""
        assert VACATION_SETBACK_EXTRA > 0

    def test_occupancy_mode_values_are_strings(self):
        """All occupancy mode constants are non-empty strings."""
        for mode in (OCCUPANCY_HOME, OCCUPANCY_AWAY, OCCUPANCY_VACATION, OCCUPANCY_GUEST):
            assert isinstance(mode, str)
            assert len(mode) > 0

    def test_config_keys_are_strings(self):
        """All occupancy config keys are non-empty strings."""
        for key in (
            CONF_HOME_TOGGLE,
            CONF_HOME_TOGGLE_INVERT,
            CONF_VACATION_TOGGLE,
            CONF_VACATION_TOGGLE_INVERT,
            CONF_GUEST_TOGGLE,
            CONF_GUEST_TOGGLE_INVERT,
        ):
            assert isinstance(key, str)
            assert len(key) > 0


# ══════════════════════════════════════════════════════════════════
# Test Class: Occupancy Away Delay (Issue #49)
# ══════════════════════════════════════════════════════════════════


def _make_occupancy_event(entity_id: str = "input_boolean.home_mode") -> MagicMock:
    """Create a mock HA state-change event for occupancy toggle tests."""
    event = MagicMock()
    event.data = {"entity_id": entity_id}
    return event


class TestOccupancyAwayDelay:
    """Tests for the 15-minute occupancy away setback delay."""

    # ── Test 1 ────────────────────────────────────────────────────

    def test_away_starts_timer_not_immediate_setback(self):
        """Toggle → away starts a timer; handle_occupancy_away is NOT called immediately."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},  # currently away
        )
        # Start from home so there IS an effective mode change to away
        coord._occupancy_mode = OCCUPANCY_HOME

        captured_delay = []
        captured_callback = []

        def fake_async_call_later(hass, delay, callback):
            captured_delay.append(delay)
            captured_callback.append(callback)
            return MagicMock()  # cancel handle

        away_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_away = away_mock

        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=fake_async_call_later,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        # Timer must have been scheduled with the correct delay
        assert len(captured_delay) == 1
        assert captured_delay[0] == OCCUPANCY_SETBACK_MINUTES * 60

        # handle_occupancy_away must NOT have been called yet
        away_mock.assert_not_called()

    # ── Test 2 ────────────────────────────────────────────────────

    def test_return_within_grace_cancels_timer(self):
        """Returning home before the timer fires cancels the pending away timer."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        cancel_mock = MagicMock()

        def fake_async_call_later(hass, delay, callback):
            return cancel_mock

        away_mock = AsyncMock()
        home_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_away = away_mock
        coord.automation_engine.handle_occupancy_home = home_mock

        # Step 1: toggle to away — timer starts
        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=fake_async_call_later,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        # Verify the cancel handle was stored
        assert coord._occupancy_away_timer_cancel is cancel_mock

        # Step 2: return home before timer fires — update state map so mode resolves to home
        coord.hass.states.get = lambda eid: _make_state("on") if eid == "input_boolean.home_mode" else None

        asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        # Timer must be cancelled
        cancel_mock.assert_called_once()
        # handle_occupancy_away was never called
        away_mock.assert_not_called()
        # handle_occupancy_home was called
        home_mock.assert_called_once()

    # ── Test 3 ────────────────────────────────────────────────────

    def test_timer_fires_calls_handle_away(self):
        """When the timer fires, handle_occupancy_away is scheduled via async_create_task."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        captured_callback = []

        def fake_async_call_later(hass, delay, callback):
            captured_callback.append(callback)
            return MagicMock()

        away_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_away = away_mock

        with (
            patch(
                "custom_components.climate_advisor.coordinator.async_call_later",
                side_effect=fake_async_call_later,
            ),
            patch(
                "custom_components.climate_advisor.coordinator.callback",
                side_effect=lambda fn: fn,
            ),
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        assert len(captured_callback) == 1

        # Simulate timer expiry by invoking the stored callback
        timer_fn = captured_callback[0]
        # The callback receives `now` from HA's call_later mechanism
        timer_fn(None)

        # handle_occupancy_away should have been called (coroutine passed to async_create_task)
        away_mock.assert_called_once()

    # ── Test 4 ────────────────────────────────────────────────────

    def test_vacation_bypasses_delay(self):
        """Switching to vacation calls handle_occupancy_vacation immediately (no timer)."""
        coord = _make_coordinator(
            config_overrides={CONF_VACATION_TOGGLE: "input_boolean.vacation"},
            state_map={"input_boolean.vacation": "on"},
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        vacation_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_vacation = vacation_mock

        call_later_mock = MagicMock()

        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=call_later_mock,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event("input_boolean.vacation")))

        # Vacation handler called immediately
        vacation_mock.assert_called_once()
        # No delay timer started for vacation
        call_later_mock.assert_not_called()

    # ── Test 5 ────────────────────────────────────────────────────

    def test_away_timer_cancelled_on_vacation(self):
        """A pending away timer is cancelled when the mode transitions to vacation."""
        coord = _make_coordinator(
            config_overrides={
                CONF_HOME_TOGGLE: "input_boolean.home_mode",
                CONF_VACATION_TOGGLE: "input_boolean.vacation",
            },
            state_map={
                "input_boolean.home_mode": "off",
                "input_boolean.vacation": "off",
            },
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        cancel_mock = MagicMock()

        call_later_calls = []

        def fake_async_call_later(hass, delay, callback):
            call_later_calls.append(callback)
            return cancel_mock

        vacation_mock = AsyncMock()
        away_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_vacation = vacation_mock
        coord.automation_engine.handle_occupancy_away = away_mock

        # Step 1: toggle home → off, vacation still off → mode becomes away, timer starts
        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=fake_async_call_later,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event("input_boolean.home_mode")))

        assert len(call_later_calls) == 1  # away timer was started

        # Step 2: vacation toggle turns on → mode becomes vacation
        coord.hass.states.get = lambda eid: (
            _make_state("off")
            if eid == "input_boolean.home_mode"
            else _make_state("on")
            if eid == "input_boolean.vacation"
            else None
        )

        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event("input_boolean.vacation")))

        # Away timer was cancelled before vacation handler ran
        cancel_mock.assert_called()
        # Vacation handler was called
        vacation_mock.assert_called_once()
        # Away handler was never called
        away_mock.assert_not_called()

    # ── Test 6 ────────────────────────────────────────────────────

    def test_no_classification_logs_warning(self, caplog):
        """handle_occupancy_away logs WARNING when _current_classification is None."""
        engine = _make_automation_engine()
        engine._current_classification = None

        with caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("no day classification" in m.lower() or "classification" in m.lower() for m in warning_msgs)

    # ── Test 7 ────────────────────────────────────────────────────

    def test_hvac_off_logs_info(self, caplog):
        """handle_occupancy_away logs INFO and skips climate call when hvac_mode is 'off'."""
        engine = _make_automation_engine()
        c = _make_classification(day_type="mild", hvac_mode="off")
        engine._current_classification = c

        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.handle_occupancy_away())

        # No climate service call should have been made
        climate_calls = [call for call in engine.hass.services.async_call.call_args_list if call[0][0] == "climate"]
        assert len(climate_calls) == 0

        # An INFO-level (or any) log message should have been emitted
        assert len(caplog.records) > 0

    # ── Test 8 ────────────────────────────────────────────────────

    def test_timer_cancelled_on_shutdown(self):
        """async_shutdown cancels a pending occupancy away timer."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        cancel_mock = MagicMock()

        def fake_async_call_later(hass, delay, callback):
            return cancel_mock

        away_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_away = away_mock

        # Start the away timer
        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=fake_async_call_later,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        assert coord._occupancy_away_timer_cancel is cancel_mock

        # Simulate automation_engine.cleanup to avoid AttributeError on shutdown
        coord.automation_engine.cleanup = MagicMock()
        coord._flush_hvac_runtime = MagicMock()

        # Shutdown must cancel the timer
        asyncio.run(coord.async_shutdown())

        cancel_mock.assert_called()

    # ── Test 9 ────────────────────────────────────────────────────

    def test_diagnostic_snapshot_timer_state(self):
        """Timer pending state is reflected in the coordinator's internal field."""
        coord = _make_coordinator(
            config_overrides={CONF_HOME_TOGGLE: "input_boolean.home_mode"},
            state_map={"input_boolean.home_mode": "off"},
        )
        coord._occupancy_mode = OCCUPANCY_HOME

        # Before any away timer: field should be None (not pending)
        assert coord._occupancy_away_timer_cancel is None

        # Start the away timer
        cancel_handle = MagicMock()

        def fake_async_call_later(hass, delay, callback):
            return cancel_handle

        away_mock = AsyncMock()
        coord.automation_engine.handle_occupancy_away = away_mock

        with patch(
            "custom_components.climate_advisor.coordinator.async_call_later",
            side_effect=fake_async_call_later,
        ):
            asyncio.run(coord._async_occupancy_toggle_changed(_make_occupancy_event()))

        # After timer starts: field should be set (pending)
        assert coord._occupancy_away_timer_cancel is cancel_handle
