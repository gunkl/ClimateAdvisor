"""Tests for Issue #936: ``_apply_economizer_fsm_state_after_activation()`` must not
apply a COOL_DOWN/MAINTAIN state when the fan-ON command was rate-limited/deferred
rather than actually issued.

Occupant-first framing: without this fix, the system could believe the economizer's
window-cooling assist fan is running when the "turn on" command was actually still
pending (deferred by the Issue #641 5-minute anti-cycling cooldown). The economizer
status would claim ventilation is assisting while nothing is actually moving air, and
-- because ``_apply_economizer_fsm_state()`` would already have written the pre-await
``to_state``, the FSM's own ``current_state`` on the next tick reads back as matching
that decision again, so ``result.changed`` comes back ``False`` and ``_activate_fan()``
is never retried -- the economizer would be stuck silently "active" but non-functional
until some other event (day reclassification, leaving the time window) forced it back
to INACTIVE.

This is the direct mirror of Issue #935's ``_apply_nat_vent_fsm_state_after_activation()``
fix (see ``tests/test_nat_vent_activation_rate_limit_guard.py``), same fallback
direction on entry: a rate-limited/overridden result means the fan never actually
started, so INACTIVE is applied instead, letting the FSM's own eligibility re-check
naturally retry activation on its own next cycle.

This file covers two things:
  1. A direct unit test on the real ``_apply_economizer_fsm_state_after_activation()``
     method -- deterministic, no re-implementation of its branching logic (project
     doctrine against mirror tests).
  2. An integration-level test driving the real public
     ``check_window_cooling_opportunity()`` entry point with ``_activate_fan()`` mocked,
     confirming the FSM actually retries activation on a later tick once the fan
     command stops being deferred (the "does it get stuck forever" question a purely
     direct unit test on the apply method alone cannot answer).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    DAY_TYPE_HOT,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.economizer_lifecycle import EconomizerLifecycleState


def _consume_coroutine(coro):
    coro.close()


def _make_engine() -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
        CONF_FAN_ENTITY: "fan.attic",
    }
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


# ---------------------------------------------------------------------------
# 1. Direct unit test on _apply_economizer_fsm_state_after_activation()
# ---------------------------------------------------------------------------


class TestAppliesInactiveWhenCommandDidNotReachTheFan:
    """OVERRIDDEN and RATE_LIMITED_NEW/RATE_LIMITED_DUP all mean the same thing for
    this purpose: the real fan-ON command never actually reached the fan, so the
    pre-await COOL_DOWN/MAINTAIN decision is stale and must not be applied --
    INACTIVE is applied instead so the FSM's own eligibility re-check can retry on
    its own next cycle."""

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.OVERRIDDEN,
            FanCommandResult.RATE_LIMITED_NEW,
            FanCommandResult.RATE_LIMITED_DUP,
        ],
    )
    @pytest.mark.parametrize(
        "to_state",
        [EconomizerLifecycleState.COOL_DOWN, EconomizerLifecycleState.MAINTAIN],
    )
    def test_applies_inactive_instead_of_stale_decision(
        self, result: FanCommandResult, to_state: EconomizerLifecycleState
    ) -> None:
        engine = _make_engine()
        engine._economizer_active = False
        engine._economizer_phase = "inactive"

        engine._apply_economizer_fsm_state_after_activation(to_state, result)

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.OVERRIDDEN,
            FanCommandResult.RATE_LIMITED_NEW,
            FanCommandResult.RATE_LIMITED_DUP,
        ],
    )
    def test_does_not_leave_a_stale_active_flag_set(self, result: FanCommandResult) -> None:
        # If a previous session were somehow still marked active, this guard must
        # still drive it to INACTIVE rather than leaving stale True state behind --
        # matches _apply_economizer_fsm_state(INACTIVE)'s own unconditional writes.
        engine = _make_engine()
        engine._economizer_active = True
        engine._economizer_phase = "cool-down"

        engine._apply_economizer_fsm_state_after_activation(EconomizerLifecycleState.COOL_DOWN, result)

        assert engine._economizer_active is False
        assert engine._economizer_phase == "inactive"


class TestAppliesDecisionWhenCommandActuallyReachedTheFan:
    """EXECUTED, ALREADY_IN_STATE, DISABLED, and SUPPRESSED all mean no override
    intervened mid-await and no command was deferred -- the pre-await to_state
    decision is still correct and must be applied normally. Control group proving
    the RATE_LIMITED guard doesn't suppress the normal case."""

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.EXECUTED,
            FanCommandResult.ALREADY_IN_STATE,
            FanCommandResult.DISABLED,
            FanCommandResult.SUPPRESSED,
        ],
    )
    def test_applies_cool_down(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._economizer_active = False
        engine._economizer_phase = "inactive"

        engine._apply_economizer_fsm_state_after_activation(EconomizerLifecycleState.COOL_DOWN, result)

        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.EXECUTED,
            FanCommandResult.ALREADY_IN_STATE,
            FanCommandResult.DISABLED,
            FanCommandResult.SUPPRESSED,
        ],
    )
    def test_applies_maintain(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._economizer_active = False
        engine._economizer_phase = "inactive"

        engine._apply_economizer_fsm_state_after_activation(EconomizerLifecycleState.MAINTAIN, result)

        assert engine._economizer_active is True
        assert engine._economizer_phase == "maintain"


# ---------------------------------------------------------------------------
# 2. Integration test: does the retry actually happen on a later tick?
# ---------------------------------------------------------------------------


class TestEconomizerActivationRetryAfterRateLimit:
    """Drives the real check_window_cooling_opportunity() entry point with
    _activate_fan() mocked, confirming a rate-limited activation attempt leaves
    the economizer INACTIVE (not stuck claiming a stale decision), and that a
    later tick with the same favorable conditions genuinely retries and applies
    the real to_state once the fan command is no longer deferred."""

    def test_rate_limited_activation_does_not_apply_state(self):
        engine = _make_engine()
        engine._current_classification = _make_hot_classification()
        engine._activate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True  # economizer FSM decided to engage, even though the fan didn't turn on
        assert engine._economizer_active is False, (
            "a rate-limited fan-ON command must not make the economizer claim it is active"
        )
        assert engine._economizer_phase == "inactive"

    def test_retry_applies_real_state_once_rate_limit_clears(self):
        """Same conditions, second tick: _activate_fan() now returns EXECUTED
        (rate-limit floor cleared) -- the FSM must retry and apply the real
        to_state this time, proving the system doesn't stay stuck INACTIVE."""
        engine = _make_engine()
        engine._current_classification = _make_hot_classification()
        engine._activate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )
        assert engine._economizer_active is False, "first tick must not have applied a stale decision"

        engine._activate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        result = asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=73.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert result is True
        assert engine._economizer_active is True
        assert engine._economizer_phase == "cool-down"
