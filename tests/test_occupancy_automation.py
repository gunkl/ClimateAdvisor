"""Tests for Issue #85 — Automation engine occupancy awareness.

Verifies that apply_classification, handle_morning_wakeup, handle_bedtime,
and _set_temperature_for_mode respect the engine's occupancy mode.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ── Helpers ──────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_thermostat_state(mode: str = "cool") -> MagicMock:
    """Return a mock thermostat state with both heat and cool capabilities.

    #249 P3: _apply_comfort_band reads attributes.hvac_modes + supported_features to
    decide which command shape to emit.  Without these attrs the band no-ops silently.
    """
    s = MagicMock()
    s.state = mode
    s.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,  # single-setpoint, no heat_cool
    }
    return s


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with standard test config."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    # Default thermostat state with both heat/cool capability so bands can arm.
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


# ── apply_classification occupancy tests ────────────────────────


class TestApplyClassificationOccupancy:
    """apply_classification should respect occupancy mode."""

    def test_away_reapplies_setback_instead_of_comfort(self):
        """When away, classification cycle arms the away band (ceiling=setback_cool), not comfort.

        #249 P3: band active='ceiling' on a cool-capable thermostat → set_temperature(setback_cool=80).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("away")
        engine.hass.states.get.return_value = _make_thermostat_state("cool")

        asyncio.run(engine.apply_classification(c))

        # Away band active='ceiling' → set_temperature(setback_cool=80), not comfort_cool (75).
        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        assert set_temp == 80  # setback_cool (away ceiling), not comfort_cool (75)

    def test_away_heat_reapplies_heat_setback(self):
        """When away in heat mode, band still arms ceiling (cool side) on cool-capable thermostat.

        #249 P3: away band is always active='ceiling'; a cool-capable device gets
        set_temperature(setback_cool=80) regardless of the classification's hvac_mode.
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        # Away band active='ceiling'; thermostat has cool → set_temperature(setback_cool=80).
        assert set_temp == 80  # setback_cool (away ceiling), not setback_heat (60)

    def test_vacation_skips_classification_entirely(self):
        """When on vacation, classification should not change temperature at all."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.apply_classification(c))

        # No service calls should have been made
        engine.hass.services.async_call.assert_not_called()

    def test_home_applies_comfort_normally(self):
        """When home, classification arms the daytime comfort band (ceiling=comfort_cool for cool day).

        #249 P3: cool day band active='ceiling' → set_temperature(comfort_cool=75).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("home")
        engine.hass.states.get.return_value = _make_thermostat_state("cool")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        # Cool day band: active='ceiling' → set_temperature(comfort_cool=75).
        assert set_temp == 75  # comfort_cool

    def test_guest_applies_comfort_normally(self):
        """Guest mode should behave like home — full comfort band armed (floor=comfort_heat for heat day).

        #249 P3: heat day band active='floor' → set_temperature(comfort_heat=70).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        # Heat day band: active='floor' → set_temperature(comfort_heat=70).
        assert set_temp == 70  # comfort_heat


# ── handle_morning_wakeup occupancy tests ───────────────────────


class TestMorningWakeupOccupancy:
    """handle_morning_wakeup should skip when not home/guest."""

    def test_wakeup_skipped_when_away(self):
        """Morning wakeup should not restore comfort when user is away."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.handle_morning_wakeup())

        engine.hass.services.async_call.assert_not_called()

    def test_wakeup_skipped_when_vacation(self):
        """Morning wakeup should not restore comfort during vacation."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.handle_morning_wakeup())

        engine.hass.services.async_call.assert_not_called()

    def test_wakeup_runs_when_home(self):
        """Morning wakeup arms the daytime comfort band — heat day floor=comfort_heat.

        #249 P3: wakeup now calls _apply_comfort_band which needs thermostat capabilities.
        Heat day active='floor' → set_temperature(comfort_heat=70).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.handle_morning_wakeup())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 70  # comfort_heat (heat day band floor)

    def test_wakeup_runs_when_guest(self):
        """Morning wakeup works when guests are present — cool day ceiling=comfort_cool.

        #249 P3: wakeup now calls _apply_comfort_band which needs thermostat capabilities.
        Cool day active='ceiling' → set_temperature(comfort_cool=75).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")
        engine.hass.states.get.return_value = _make_thermostat_state("cool")

        asyncio.run(engine.handle_morning_wakeup())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        set_temp = temp_calls[-1][0][2]["temperature"]
        assert set_temp == 75  # comfort_cool (cool day band ceiling)


# ── handle_bedtime occupancy tests ──────────────────────────────


class TestBedtimeOccupancy:
    """handle_bedtime should skip during vacation."""

    def test_bedtime_skipped_when_vacation(self):
        """Vacation deep setback should not be overwritten by bedtime setback."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")

        asyncio.run(engine.handle_bedtime())

        engine.hass.services.async_call.assert_not_called()

    def test_bedtime_skipped_when_away(self):
        """Issue #101: Away setback is already active — bedtime should not override it."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("away")

        asyncio.run(engine.handle_bedtime())

        # Bedtime is skipped when AWAY — away setback wins
        engine.hass.services.async_call.assert_not_called()

    def test_bedtime_runs_when_home(self):
        """Bedtime arms the sleep band — needs thermostat capabilities to produce a service call.

        #249 P3: bedtime now calls _apply_comfort_band; stubs must carry hvac_modes attributes.
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1

    def test_bedtime_runs_when_guest(self):
        """Guest mode applies the sleep band like home — needs thermostat capabilities.

        #249 P3: bedtime now calls _apply_comfort_band; stubs must carry hvac_modes attributes.
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("guest")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1

    def test_bedtime_uses_sleep_heat_when_home(self):
        """sleep_heat=67 in config → bedtime sleep band floor=67°F.

        #249 P3: the sleep band floor comes from config sleep_heat; the band arms with
        set_temperature(floor=67) on a heat-capable thermostat.
        """
        engine = _make_engine(
            config_overrides={
                "comfort_heat": 70,
                "setback_heat": 60,
                "sleep_heat": 67.0,
            }
        )
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("home")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine.handle_bedtime())

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        assert set_temp == pytest.approx(67.0, abs=0.1)  # sleep band floor = sleep_heat


# ── _set_temperature_for_mode safety net tests ──────────────────


class TestSetTemperatureForModeOccupancy:
    """_set_temperature_for_mode should redirect to setback when away/vacation."""

    def test_redirects_to_away_setback(self):
        """When away, _set_temperature_for_mode routes to handle_occupancy_away (band ceiling).

        #249 P3: away band active='ceiling' → cool-capable thermostat gets
        set_temperature(setback_cool=80).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("away")
        engine.hass.states.get.return_value = _make_thermostat_state("cool")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        # Away band active='ceiling' on cool-capable thermostat → setback_cool.
        assert set_temp == 80  # setback_cool

    def test_redirects_to_vacation_setback(self):
        """When on vacation, _set_temperature_for_mode routes to handle_occupancy_vacation (band ceiling+extra).

        #249 P3: vacation band active='ceiling' → cool-capable thermostat gets
        set_temperature(setback_cool + VACATION_SETBACK_EXTRA = 80 + 3 = 83).
        """
        engine = _make_engine()
        c = _make_classification(hvac_mode="heat", day_type="cold")
        engine._current_classification = c
        engine.set_occupancy_mode("vacation")
        engine.hass.states.get.return_value = _make_thermostat_state("heat")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        # Vacation band active='ceiling' on cool-capable thermostat: setback_cool + VACATION_SETBACK_EXTRA.
        # = 80 + 3 = 83
        assert set_temp == 83

    def test_applies_comfort_when_home(self):
        """When home, _set_temperature_for_mode should apply comfort as usual."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c
        engine.set_occupancy_mode("home")

        asyncio.run(engine._set_temperature_for_mode(c, reason="test"))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call[0][0] == "climate" and call[0][1] == "set_temperature"]
        # Double-write (Issue #299): pre-write + target write = 2 calls
        assert len(temp_calls) == 2
        set_temp = temp_calls[1][0][2]["temperature"]  # target write
        assert set_temp == 75  # comfort_cool


# ── set_occupancy_mode tests ────────────────────────────────────


class TestSetOccupancyMode:
    """set_occupancy_mode should update the internal state."""

    def test_sets_mode(self):
        engine = _make_engine()
        assert engine._occupancy_mode == "home"
        engine.set_occupancy_mode("away")
        assert engine._occupancy_mode == "away"

    def test_handlers_set_mode_internally(self):
        """Occupancy handlers should also set the internal mode."""
        engine = _make_engine()
        c = _make_classification(hvac_mode="cool")
        engine._current_classification = c

        asyncio.run(engine.handle_occupancy_away())
        assert engine._occupancy_mode == "away"

        asyncio.run(engine.handle_occupancy_home())
        assert engine._occupancy_mode == "home"

        asyncio.run(engine.handle_occupancy_vacation())
        assert engine._occupancy_mode == "vacation"

    def test_logs_mode_change(self, caplog):
        engine = _make_engine()
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            engine.set_occupancy_mode("away")
        assert "home → away" in caplog.text
