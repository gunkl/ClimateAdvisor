"""Tests for the two-phase economizer (window cooling) feature (Issue #27).

Tests cover:
- Phase 1 (cool-down): the #249 band holds comfort_cool; the economizer assists with the fan and
  does not override the HVAC mode/setpoint (Issue #264)
- Phase 2 (maintain): band stays armed when indoor <= comfort, ventilation holds (Issue #249)
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

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    DAY_TYPE_HOT,
    DAY_TYPE_WARM,
    FAN_MODE_HVAC,
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
        """Indoor 80°F > comfort 75°F, outdoor 73°F → cool-down phase; the #249 band holds
        comfort_cool, so the economizer assists with the fan and does NOT override the HVAC mode (#264)."""
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
        # Issue #264: the #249 comfort band already holds comfort_cool — the economizer no longer
        # flips the HVAC mode/setpoint (that would fight the band). Cool-down now only assists with the fan.
        mode_calls = _get_hvac_mode_calls(engine)
        assert not any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls), "economizer must not override the band mode"
        temp_calls = _get_set_temp_calls(engine)
        assert not any(c[0][2]["temperature"] == 75 for c in temp_calls), "economizer must not set the band's setpoint"

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
# Issue #327: free-cooling-direction guard (outdoor < indoor required)
# ---------------------------------------------------------------------------


class TestEconomizerDirectionGuard:
    """Issue #327: the economizer must not run the fan when outdoor is not cooler than
    indoor — pulling in air that is not cooler gives no free cooling. Mirrors nat-vent's
    outdoor<indoor gate. indoor/outdoor are both below comfort_cool here so the
    comfort_cool+delta threshold is satisfied and only the direction guard can block."""

    def test_direction_guard_blocks_when_outdoor_not_cooler(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=74.5,  # >= indoor; still <= comfort_cool(75)+delta
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=18,  # evening window
            )
        )

        assert result is False
        assert engine._economizer_active is False

    def test_direction_guard_allows_when_outdoor_cooler(self):
        """Control: same setup but outdoor < indoor → economizer still eligible."""
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=74.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_active is True


# ---------------------------------------------------------------------------
# Phase 2: Maintain (indoor at or below comfort → AC off)
# ---------------------------------------------------------------------------


class TestEconomizerMaintain:
    """Phase 2: AC off when indoor <= comfort, ventilation holds."""

    def test_maintain_turns_ac_off_when_indoor_at_comfort(self):
        """Indoor 75°F == comfort 75°F → maintain phase; band stays armed (no HVAC off call).

        #249: economizer maintain no longer calls set_hvac_mode("off") — the comfort
        band stays armed and the open window handles cooling via natural ventilation.
        """
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
        # #249: maintain phase leaves the comfort band armed; no set_hvac_mode("off") call
        mode_calls = _get_hvac_mode_calls(engine)
        assert not any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)

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
        """Indoor drops from above to at comfort → transitions cool-down → maintain.

        #249: the cool-down→maintain transition no longer calls set_hvac_mode("off");
        the comfort band stays armed once indoor is at/below the comfort ceiling.
        """
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
        # #249: maintain phase leaves band armed; no set_hvac_mode("off") on transition
        mode_calls = _get_hvac_mode_calls(engine)
        assert not any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)

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
        """With aggressive_savings, indoor above comfort still uses ventilation only.

        #249: aggressive_savings maintain no longer calls set_hvac_mode("off") — the
        comfort band stays armed; AC compressor self-arbitrates with the open window.
        """
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
        # #249: maintain/savings mode leaves band armed; no set_hvac_mode("off") call
        mode_calls = _get_hvac_mode_calls(engine)
        assert not any(c[0][2]["hvac_mode"] == "off" for c in mode_calls)
        # Should NOT have set cool mode (savings skips AC assist)
        assert not any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls)

    def test_comfort_mode_cooldown_assists_with_fan_not_ac_override(self):
        """Issue #264: with aggressive_savings=False, cool-down assists with the fan; the #249 band
        (not the economizer) holds comfort_cool, so the economizer no longer flips the HVAC mode."""
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
        assert not any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls), "economizer must not override the band mode"


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
# Issue #936: _deactivate_economizer() rate-limit gating (mirrors #931's
# _end_nat_vent_session() fix on the economizer side).
# ---------------------------------------------------------------------------


class TestEconomizerDeactivationRateLimitGuard:
    """Occupant-first framing: without this fix, a rate-limited fan-off command
    (deferred up to 5 minutes by the Issue #641 anti-cycling limiter) would make
    the engine believe the economizer session ended while the fan/AC-suppression
    is still physically in whatever state it was in -- with nothing left to retry
    turning it off, since check_window_cooling_opportunity() derives its FSM
    current_state from these same _economizer_active/_economizer_phase flags on
    its next tick. _deactivate_economizer() now only clears those flags once the
    paired fan-off command actually reached the fan.
    """

    def test_rate_limited_new_preserves_session(self):
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"

    def test_rate_limited_dup_preserves_session(self):
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_DUP)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        assert engine._economizer_active is True
        assert engine._economizer_phase == "maintain"

    def test_executed_clears_session(self):
        """Control: a real fan-off command (EXECUTED) still clears the session
        normally -- proves the guard doesn't just always preserve state."""
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_already_in_state_clears_session(self):
        """Control: fan already off (ALREADY_IN_STATE) also clears the session --
        not just EXECUTED is treated as "the command reached the fan"."""
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.ALREADY_IN_STATE)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_retry_clears_session_once_rate_limit_clears(self):
        """First tick: deferred, session preserved. Second tick (simulating the
        retry once the rate-limit floor clears): a real EXECUTED result now
        clears the flags -- proves the retry actually happens on a later tick
        rather than the session getting stuck active forever."""
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))
        assert engine._economizer_active is True, "first tick must preserve the session"
        assert engine._economizer_phase == "cool-down"

        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    def test_hvac_resume_runs_unconditionally_regardless_of_fan_result(self):
        """The HVAC-resume block runs every time, independent of whether the fan
        command was rate-limited -- mirrors _exit_nat_vent()'s sensors-closed
        branch, which always resumes HVAC independent of the fan command's
        outcome; only the session bookkeeping flags are gated."""
        engine = _make_automation_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._current_classification = _make_hot_classification()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        asyncio.run(engine._deactivate_economizer(outdoor_temp=80.0))

        mode_calls = _get_hvac_mode_calls(engine)
        assert any(c[0][2]["hvac_mode"] == "cool" for c in mode_calls), (
            "HVAC resume must run even when the fan-off command was rate-limited"
        )


# ---------------------------------------------------------------------------
# Issue #936 verification note #2: regression pin for the no-op tick.
# ---------------------------------------------------------------------------


class TestEconomizerUnchangedTickNoFlagWrite:
    """When the FSM decides to stay in the same phase (EconomizerTransition.changed
    is False), _check_window_cooling_opportunity_fsm() must not write
    _economizer_active/_economizer_phase at all. This pins the removal of the old
    unconditional pre-await `self._apply_economizer_fsm_state(result.to_state)` call
    that used to run before the `if not result.changed: return True` guard."""

    def test_no_op_tick_does_not_call_apply_fsm_state(self):
        engine = _make_automation_engine()
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._apply_economizer_fsm_state = MagicMock()
        engine._apply_economizer_fsm_state_after_activation = MagicMock()

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=78.0,  # still above comfort -> stays in cool-down, no-op tick
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        engine._apply_economizer_fsm_state.assert_not_called()
        engine._apply_economizer_fsm_state_after_activation.assert_not_called()
        # Flags are untouched, exactly as they started
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"
        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #936 verification note #3: COOL_DOWN->MAINTAIN with the fan already
# active must never accidentally fall back to INACTIVE.
# ---------------------------------------------------------------------------


class TestEconomizerMaintainTransitionFanAlreadyActive:
    """The stranded-fan bug shape WOULD have occurred here if the INACTIVE
    fallback in _apply_economizer_fsm_state_after_activation() were reachable
    when the fan is already running during a COOL_DOWN->MAINTAIN transition.
    _activate_fan()'s own idempotency guard returns ALREADY_IN_STATE (not a
    rate-limit/override outcome) when the fan is already active, so the real
    to_state (MAINTAIN) must still be applied -- this locks that down."""

    def test_already_active_fan_applies_real_maintain_state(self):
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"
        engine._fan_active = True  # fan already running from the cool-down phase

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=74.0,  # now at/below comfort -> maintain
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is True
        assert engine._economizer_active is True
        assert engine._economizer_phase == "maintain"


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
