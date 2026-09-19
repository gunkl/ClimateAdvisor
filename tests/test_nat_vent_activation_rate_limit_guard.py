"""Tests for Issue #935: ``_apply_nat_vent_fsm_state_after_activation()`` must not
apply an ACTIVE_* state when the fan-ON command was rate-limited/deferred rather
than actually issued.

Occupant-first framing: without this fix, the system could believe the whole-house
fan is running and free-cooling the home when the "turn on" command was actually
still pending (deferred by the Issue #641 5-minute anti-cycling cooldown). This
leaves the home warming up unprotected for up to 5 minutes while the status page
claims nat-vent is active, and — because the idle-open re-evaluation loop is gated
on ``not self._natural_vent_active`` — nothing retries the activation once the
cooldown clears, since the flag already (wrongly) says a session is running.

This is the mirror-image of Issue #931's ``_end_nat_vent_session()`` fix (see
``tests/test_end_nat_vent_session.py``), but the CORRECT fallback state is
opposite: on exit (#931), a rate-limited result means the fan never actually
stopped, so the flags must be PRESERVED as active so the exit retry loop keeps
trying. On entry (#935), a rate-limited result means the fan never actually
started, so the flag must be cleared to INACTIVE so the entry retry loop (gated on
``not self._natural_vent_active``) naturally tries again on its own next cycle.

This is a direct unit test on the real ``_apply_nat_vent_fsm_state_after_activation()``
method — deterministic, no simulator/virtual clock involved, and no re-implementation
of its branching logic (see project doctrine against mirror tests).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState


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


class TestAppliesInactiveWhenCommandDidNotReachTheFan:
    """OVERRIDDEN (Issue #706) and RATE_LIMITED_NEW/RATE_LIMITED_DUP (Issue #935)
    all mean the same thing for this purpose: the real fan-ON command never
    actually reached the fan, so the pre-await ACTIVE_* decision is stale and
    must not be applied — INACTIVE is applied instead so the entry retry loop
    can try again on its own next cycle."""

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
        [NatVentLifecycleState.ACTIVE_FULL_GATE, NatVentLifecycleState.ACTIVE_SOFT_START],
    )
    def test_applies_inactive_instead_of_stale_active_state(
        self, result: FanCommandResult, to_state: NatVentLifecycleState
    ) -> None:
        engine = _make_engine()
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._paused_by_door = False

        engine._apply_nat_vent_fsm_state_after_activation(to_state, result)

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False

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
        # still drive it to INACTIVE rather than leaving stale True state behind —
        # matches _apply_nat_vent_fsm_state(INACTIVE)'s own unconditional writes.
        engine = _make_engine()
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True

        engine._apply_nat_vent_fsm_state_after_activation(NatVentLifecycleState.ACTIVE_FULL_GATE, result)

        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False


class TestAppliesDecisionWhenCommandActuallyReachedTheFan:
    """EXECUTED, ALREADY_IN_STATE, and DISABLED all mean no override intervened
    mid-await and no command was deferred — the pre-await to_state decision is
    still correct and must be applied normally. Control group proving the
    widened RATE_LIMITED guard doesn't suppress the normal case."""

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.EXECUTED,
            FanCommandResult.ALREADY_IN_STATE,
            FanCommandResult.DISABLED,
        ],
    )
    def test_applies_active_full_gate(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False

        engine._apply_nat_vent_fsm_state_after_activation(NatVentLifecycleState.ACTIVE_FULL_GATE, result)

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is False

    @pytest.mark.parametrize(
        "result",
        [
            FanCommandResult.EXECUTED,
            FanCommandResult.ALREADY_IN_STATE,
            FanCommandResult.DISABLED,
        ],
    )
    def test_applies_active_soft_start(self, result: FanCommandResult) -> None:
        engine = _make_engine()
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False

        engine._apply_nat_vent_fsm_state_after_activation(NatVentLifecycleState.ACTIVE_SOFT_START, result)

        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True
