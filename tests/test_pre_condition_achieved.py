"""Tests for Issue #295 — pre-cool achievement gate.

Verifies that AutomationEngine sets _pre_condition_achieved=True once the home
reaches the pre-cool target temperature, and that select_comfort_band() stops
lowering the ceiling once the flag is set.

Occupant experience: On a hot day (forecast 90°F, comfort_cool=75°F,
pre_condition_target=-2°F), the AC pre-cools to 73°F. Once the home reaches
73°F, the system should revert to 75°F as the ceiling — not keep holding 73°F
all afternoon.  Without this fix the user pays extra energy all day to maintain
73°F instead of their configured 75°F comfort ceiling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine, select_comfort_band
from custom_components.climate_advisor.classifier import DayClassification

# ── Helpers ──────────────────────────────────────────────────────────────────


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_thermostat_state(mode: str = "cool") -> MagicMock:
    """Return a mock thermostat state with heat+cool capabilities.

    _apply_comfort_band reads attributes.hvac_modes + supported_features.
    Without these attrs the band no-ops and _set_temperature is never reached.
    """
    s = MagicMock()
    s.state = mode
    s.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": 1,
        "current_temperature": 76.0,  # default indoor temp injected via thermostat
    }
    return s


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with hot-day test config."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
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


def _make_hot_classification(**kwargs) -> DayClassification:
    """Create a hot-day DayClassification with pre_condition=True."""
    obj = object.__new__(DayClassification)
    obj.day_type = "hot"
    obj.hvac_mode = "cool"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 0
    obj.today_high = kwargs.get("today_high", 90.0)
    obj.today_low = kwargs.get("today_low", 65.0)
    obj.tomorrow_high = kwargs.get("tomorrow_high", 88.0)
    obj.tomorrow_low = kwargs.get("tomorrow_low", 65.0)
    obj.pre_condition = kwargs.get("pre_condition", True)
    obj.pre_condition_target = kwargs.get("pre_condition_target", -2.0)
    obj.windows_recommended = kwargs.get("windows_recommended", False)
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    obj.window_opportunity_morning = False
    obj.window_opportunity_evening = False
    obj.window_opportunity_morning_start = None
    obj.window_opportunity_morning_end = None
    obj.window_opportunity_evening_start = None
    obj.window_opportunity_evening_end = None
    return obj


# ── Tests: initial state ──────────────────────────────────────────────────────


class TestFlagInitialState:
    def test_flag_starts_false(self):
        """New AutomationEngine has _pre_condition_achieved = False."""
        engine = _make_engine()
        assert engine._pre_condition_achieved is False

    def test_date_starts_none(self):
        """New AutomationEngine has _pre_condition_achieved_date = None."""
        engine = _make_engine()
        assert engine._pre_condition_achieved_date is None


# ── Tests: select_comfort_band — pure function ────────────────────────────────


class TestSelectComfortBandPreCool:
    """select_comfort_band ceiling-lowering guard with pre_condition_achieved parameter."""

    def _band(self, pre_condition_achieved: bool, pre_condition_target: float = -2.0):
        classification = _make_hot_classification(pre_condition_target=pre_condition_target)
        config = {"comfort_heat": 70, "comfort_cool": 75}
        return select_comfort_band(
            classification,
            config,
            occupancy_mode="home",
            in_sleep_window=False,
            aggressive_savings=False,
            pre_condition_achieved=pre_condition_achieved,
        )

    def test_ceiling_lowered_when_not_achieved(self):
        """indoor_temp=76, comfort_cool=75, pre_condition_target=-2 → ceiling=73 (pre-cool active)."""
        band = self._band(pre_condition_achieved=False)
        assert band.ceiling == 73.0, (
            f"Expected ceiling=73.0 (75 + -2) when pre-cool not yet achieved, got {band.ceiling}"
        )

    def test_ceiling_normal_when_achieved(self):
        """When pre_condition_achieved=True, ceiling stays at comfort_cool (75), NOT 73."""
        band = self._band(pre_condition_achieved=True)
        assert band.ceiling == 75.0, f"Expected ceiling=75.0 (comfort_cool) when pre-cool achieved, got {band.ceiling}"

    def test_ceiling_lowered_default_parameter(self):
        """Default pre_condition_achieved=False still lowers ceiling (backward compat)."""
        classification = _make_hot_classification()
        config = {"comfort_heat": 70, "comfort_cool": 75}
        band = select_comfort_band(
            classification,
            config,
            occupancy_mode="home",
            in_sleep_window=False,
            aggressive_savings=False,
            # pre_condition_achieved not passed — defaults to False
        )
        assert band.ceiling == 73.0


# ── Tests: achievement detection via apply_classification ─────────────────────


class TestAchievementDetection:
    """apply_classification sets the achievement flag when indoor_temp reaches target."""

    def _run_apply(
        self,
        engine: AutomationEngine,
        classification: DayClassification,
        indoor_temp: float | None,
        today: str = "2026-06-13",
    ) -> None:
        """Run apply_classification in a patched date context, stubbing out HA calls."""
        fixed_dt = datetime.fromisoformat(f"{today}T09:00:00")

        async def _run():
            with patch(
                "custom_components.climate_advisor.automation.dt_util.now",
                return_value=fixed_dt,
            ):
                await engine.apply_classification(
                    classification,
                    indoor_temp=indoor_temp,
                )

        asyncio.run(_run())

    def test_flag_set_when_indoor_at_target(self):
        """apply_classification with indoor_temp=73.0 (exactly at target 73) → flag becomes True."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)
        # target = comfort_cool + pre_condition_target = 75 + (-2) = 73
        self._run_apply(engine, c, indoor_temp=73.0)
        assert engine._pre_condition_achieved is True, (
            "Expected _pre_condition_achieved=True when indoor_temp=73.0 <= target=73.0"
        )

    def test_flag_set_when_indoor_below_target(self):
        """apply_classification with indoor_temp=71.0 (below target 73) → flag becomes True."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)
        self._run_apply(engine, c, indoor_temp=71.0)
        assert engine._pre_condition_achieved is True, (
            "Expected _pre_condition_achieved=True when indoor_temp=71.0 <= target=73.0"
        )

    def test_flag_not_set_when_indoor_above_target(self):
        """apply_classification with indoor_temp=74.0 (above target 73) → flag stays False."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)
        self._run_apply(engine, c, indoor_temp=74.0)
        assert engine._pre_condition_achieved is False, (
            "Expected _pre_condition_achieved=False when indoor_temp=74.0 > target=73.0"
        )

    def test_flag_not_set_when_indoor_temp_none(self):
        """apply_classification with indoor_temp=None → flag stays False (can't evaluate)."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)
        self._run_apply(engine, c, indoor_temp=None)
        assert engine._pre_condition_achieved is False

    def test_flag_not_set_when_pre_condition_false(self):
        """apply_classification with pre_condition=False → flag stays False."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition=False, pre_condition_target=None)
        self._run_apply(engine, c, indoor_temp=70.0)
        assert engine._pre_condition_achieved is False

    def test_flag_already_true_stays_true(self):
        """Once flag=True, a subsequent call with indoor_temp above target doesn't reset it."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)
        # First call: achieve the target
        self._run_apply(engine, c, indoor_temp=73.0)
        assert engine._pre_condition_achieved is True
        # Second call: indoor drifted up to 74 (above target) — flag must stay True
        self._run_apply(engine, c, indoor_temp=74.0)
        assert engine._pre_condition_achieved is True, (
            "Flag must remain True once set — drift above target must not clear it"
        )


# ── Tests: daily reset ────────────────────────────────────────────────────────


class TestDailyReset:
    def test_flag_resets_on_new_day(self):
        """Flag set on 2026-06-13 resets to False when apply_classification called on 2026-06-14."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)

        async def _run(today: str, indoor_temp: float | None):
            fixed_dt = datetime.fromisoformat(f"{today}T09:00:00")
            with patch(
                "custom_components.climate_advisor.automation.dt_util.now",
                return_value=fixed_dt,
            ):
                await engine.apply_classification(c, indoor_temp=indoor_temp)

        # Day 1: set the flag
        asyncio.run(_run("2026-06-13", 73.0))
        assert engine._pre_condition_achieved is True
        assert engine._pre_condition_achieved_date == "2026-06-13"

        # Day 2: flag should reset before achievement check
        asyncio.run(_run("2026-06-14", 74.0))  # 74 > target 73 — doesn't re-achieve
        assert engine._pre_condition_achieved is False, "Flag must reset to False on a new calendar day"
        assert engine._pre_condition_achieved_date == "2026-06-14"

    def test_flag_persists_within_same_day(self):
        """Flag set at 09:00 is still True when apply_classification called at 15:00 same day."""
        engine = _make_engine()
        c = _make_hot_classification(pre_condition_target=-2.0)

        async def _run(hour: int, indoor_temp: float | None):
            fixed_dt = datetime.fromisoformat(f"2026-06-13T{hour:02d}:00:00")
            with patch(
                "custom_components.climate_advisor.automation.dt_util.now",
                return_value=fixed_dt,
            ):
                await engine.apply_classification(c, indoor_temp=indoor_temp)

        # Morning: achieve
        asyncio.run(_run(9, 73.0))
        assert engine._pre_condition_achieved is True

        # Afternoon: drift up but same day — flag stays True
        asyncio.run(_run(15, 74.0))
        assert engine._pre_condition_achieved is True


# ── Tests: state persistence ──────────────────────────────────────────────────


class TestStatePersistence:
    def test_flag_in_serializable_state(self):
        """get_serializable_state() includes pre_condition_achieved and pre_condition_achieved_date."""
        engine = _make_engine()
        engine._pre_condition_achieved = True
        engine._pre_condition_achieved_date = "2026-06-13"

        state = engine.get_serializable_state()

        assert "pre_condition_achieved" in state, "pre_condition_achieved missing from serialized state"
        assert "pre_condition_achieved_date" in state, "pre_condition_achieved_date missing from serialized state"
        assert state["pre_condition_achieved"] is True
        assert state["pre_condition_achieved_date"] == "2026-06-13"

    def test_flag_false_in_serializable_state(self):
        """get_serializable_state() correctly serializes False state."""
        engine = _make_engine()
        state = engine.get_serializable_state()
        assert state["pre_condition_achieved"] is False
        assert state["pre_condition_achieved_date"] is None

    def test_restore_state_restores_flag(self):
        """restore_state() restores pre_condition_achieved and pre_condition_achieved_date."""
        engine = _make_engine()
        engine.restore_state(
            {
                "pre_condition_achieved": True,
                "pre_condition_achieved_date": "2026-06-13",
            }
        )
        assert engine._pre_condition_achieved is True
        assert engine._pre_condition_achieved_date == "2026-06-13"

    def test_restore_state_defaults_flag_false(self):
        """restore_state() with missing key defaults _pre_condition_achieved to False."""
        engine = _make_engine()
        engine.restore_state({})
        assert engine._pre_condition_achieved is False
        assert engine._pre_condition_achieved_date is None

    def test_state_round_trip(self):
        """get_serializable_state() + restore_state() round-trip preserves flag correctly."""
        engine1 = _make_engine()
        engine1._pre_condition_achieved = True
        engine1._pre_condition_achieved_date = "2026-06-13"

        serialized = engine1.get_serializable_state()

        engine2 = _make_engine()
        engine2.restore_state(serialized)

        assert engine2._pre_condition_achieved is True
        assert engine2._pre_condition_achieved_date == "2026-06-13"
