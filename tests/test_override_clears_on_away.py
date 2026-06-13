"""Tests for Issue #220 — clear manual override on away/vacation occupancy transition.

Verifies that handle_occupancy_away() and handle_occupancy_vacation() clear any
active manual override before applying setback, and that handle_occupancy_home()
leaves the override intact.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

# ── Helpers ──────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_thermostat_state(mode: str = "cool") -> MagicMock:
    """Return a mock thermostat state with heat+cool capabilities.

    #249 P3: _apply_comfort_band reads attributes.hvac_modes + supported_features.
    Without these attrs the band no-ops and _set_temperature is never reached.
    """
    s = MagicMock()
    s.state = mode
    s.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,
    }
    return s


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    # Default thermostat with heat+cool capability so the band can arm.
    hass.states.get.return_value = _make_thermostat_state("cool")

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


def _arm_override(engine: AutomationEngine, mode: str = "cool") -> None:
    """Put engine into manual-override-active state."""
    engine._manual_override_active = True
    engine._manual_override_mode = mode
    engine._manual_override_time = "2026-06-07T10:00:00"


# ── Tests ────────────────────────────────────────────────────────


class TestAwayTransitionClearsOverride:
    """handle_occupancy_away clears override before setback runs."""

    def test_override_active_cleared_before_setback(self):
        """Away transition with active override: clear_manual_override called, then setback runs."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        _arm_override(engine)

        clear_calls = []
        set_temp_states = []

        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            clear_calls.append(reason)
            original_clear(reason=reason)

        async def _spy_set_temperature(temp, reason="", mode="cool"):
            # Capture the override state at the moment _set_temperature is called.
            set_temp_states.append(engine._manual_override_active)

        engine.clear_manual_override = _spy_clear
        engine._set_temperature = _spy_set_temperature

        asyncio.run(engine.handle_occupancy_away())

        assert clear_calls == ["occupancy_away"], "clear_manual_override should be called with reason='occupancy_away'"
        assert set_temp_states == [False], "_manual_override_active must be False when _set_temperature is called"

    def test_override_cleared_reason_is_occupancy_away(self):
        """Away clear uses reason='occupancy_away' (not grace_expired or other)."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")
        _arm_override(engine, mode="heat")

        reasons = []
        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            reasons.append(reason)
            original_clear(reason=reason)

        async def _noop_set_temperature(temp, reason="", mode="cool"):
            pass

        engine.clear_manual_override = _spy_clear
        engine._set_temperature = _noop_set_temperature

        asyncio.run(engine.handle_occupancy_away())

        assert reasons == ["occupancy_away"]

    def test_no_override_clear_not_called(self):
        """Away transition with no active override: clear_manual_override NOT called."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        # Override is NOT active — default state.

        clear_called = []
        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            clear_called.append(reason)
            original_clear(reason=reason)

        async def _noop_set_temperature(temp, reason="", mode="cool"):
            pass

        engine.clear_manual_override = _spy_clear
        engine._set_temperature = _noop_set_temperature

        asyncio.run(engine.handle_occupancy_away())

        assert clear_called == [], "clear_manual_override must NOT be called when no override is active"


class TestVacationTransitionClearsOverride:
    """handle_occupancy_vacation clears override before setback runs."""

    def test_override_active_cleared_before_setback(self):
        """Vacation transition with active override: clear_manual_override called with 'occupancy_vacation'."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        _arm_override(engine)

        clear_calls = []
        set_temp_states = []

        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            clear_calls.append(reason)
            original_clear(reason=reason)

        async def _spy_set_temperature(temp, reason="", mode="cool"):
            set_temp_states.append(engine._manual_override_active)

        engine.clear_manual_override = _spy_clear
        engine._set_temperature = _spy_set_temperature

        asyncio.run(engine.handle_occupancy_vacation())

        assert clear_calls == ["occupancy_vacation"], (
            "clear_manual_override should be called with reason='occupancy_vacation'"
        )
        assert set_temp_states == [False], "_manual_override_active must be False when _set_temperature is called"

    def test_no_override_clear_not_called(self):
        """Vacation transition with no active override: clear_manual_override NOT called."""
        engine = _make_engine()
        engine._current_classification = _make_classification(hvac_mode="heat")

        clear_called = []
        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            clear_called.append(reason)
            original_clear(reason=reason)

        async def _noop_set_temperature(temp, reason="", mode="cool"):
            pass

        engine.clear_manual_override = _spy_clear
        engine._set_temperature = _noop_set_temperature

        asyncio.run(engine.handle_occupancy_vacation())

        assert clear_called == [], "clear_manual_override must NOT be called when no override is active"


class TestHomeTransitionPreservesOverride:
    """handle_occupancy_home must NOT clear an active manual override."""

    def test_override_preserved_on_home(self):
        """Returning home must leave any active manual override intact."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat")
        engine._current_classification = c
        _arm_override(engine, mode="heat")

        clear_called = []
        original_clear = engine.clear_manual_override

        def _spy_clear(reason="grace_expired"):
            clear_called.append(reason)
            original_clear(reason=reason)

        engine.clear_manual_override = _spy_clear

        # _set_temperature_for_mode and _notify are also called by home handler —
        # stub them out so the test stays self-contained.
        async def _noop(*_a, **_kw):
            pass

        engine._set_temperature_for_mode = _noop
        engine._notify = _noop

        asyncio.run(engine.handle_occupancy_home())

        assert clear_called == [], (
            "handle_occupancy_home must NOT call clear_manual_override — "
            "user returning home may want to resume their manual preference"
        )
        assert engine._manual_override_active is True, (
            "_manual_override_active must still be True after home transition"
        )
