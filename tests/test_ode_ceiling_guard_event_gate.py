"""Tests for Issue #934: nat_vent_ceiling_escalation event must not fire for a
rate-limited/deferred fan-stop command.

The ODE ceiling guard's ESCALATE branch (``AutomationEngine._apply_ode_ceiling_guard_decision()``)
calls ``_deactivate_fan()`` and then emitted ``nat_vent_ceiling_escalation``
unconditionally, regardless of the returned ``FanCommandResult``. Its siblings
(the away-ceiling exit sites, automation.py ~L4839/~L5231, and the shared
``_exit_nat_vent()`` helper ~L7747) all correctly gate their own event emission
on ``result is not FanCommandResult.RATE_LIMITED_DUP`` — a DUP result means the
fan-stop command never actually executed and was already reported once by the
deferral itself (Issue #649). This file proves the ceiling-escalation site now
matches that pattern: a RATE_LIMITED_DUP result suppresses the event, while an
EXECUTED result still emits it normally.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.classification_fsm import (
    CeilingGuardEligibility,
    ClassificationDecision,
    ClassificationFsmEventKind,
)
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardDecision, OdeCeilingGuardOutcome

_DT_NOW = datetime(2026, 7, 15, 14, 0, 0)

sys.modules["homeassistant.util.dt"].now = lambda: _DT_NOW

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime


def _make_engine(indoor_f: float = 76.0, comfort_cool: float = 74.0) -> AutomationEngine:
    """Real AutomationEngine, HA layer mocked — same pattern as
    tests/test_natvent_away_ceiling.py's _make_engine()."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "off"
    climate_state.attributes = {"current_temperature": indoor_f, "temperature": None}

    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": 68.0,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": 3.0,
        "notify_service": "notify.notify",
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )

    engine._natural_vent_active = True
    engine._paused_by_door = False
    engine._last_outdoor_temp = 90.0

    return engine


def _escalate_decision() -> ClassificationDecision:
    """A ClassificationDecision whose ceiling_decision is ESCALATE — the only
    branch of _apply_ode_ceiling_guard_decision() that calls _deactivate_fan()
    and emits nat_vent_ceiling_escalation."""
    ceiling_decision = OdeCeilingGuardDecision(
        outcome=OdeCeilingGuardOutcome.ESCALATE,
        breach_ts=_DT_NOW + timedelta(hours=2),
        hours_to_breach=2.0,
        lead_min=30.0,
        should_deactivate_fan=True,
    )
    return ClassificationDecision(
        event_kind=ClassificationFsmEventKind.CYCLE_EVALUATED,
        gate=MagicMock(),
        ceiling_eligibility=CeilingGuardEligibility.EVALUATED,
        ceiling_decision=ceiling_decision,
        at=_DT_NOW,
    )


async def _run_guard(engine: AutomationEngine) -> list[tuple]:
    events: list[tuple] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))

    classification = MagicMock(hvac_mode="off")
    decision = _escalate_decision()

    await engine._apply_ode_ceiling_guard_decision(classification, predicted_indoor=[{"ts": "x"}], decision=decision)
    return events


class TestCeilingEscalationEventGate:
    """Issue #934: nat_vent_ceiling_escalation must reflect what actually happened."""

    def test_rate_limited_dup_suppresses_event(self) -> None:
        """A RATE_LIMITED_DUP _deactivate_fan() result means the fan-stop command
        never executed (already reported once by the deferral itself) — no
        nat_vent_ceiling_escalation event should fire."""
        engine = _make_engine()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_DUP)

        events = asyncio.run(_run_guard(engine))

        escalation_events = [e for e in events if e[0] == "nat_vent_ceiling_escalation"]
        assert not escalation_events, f"expected no event for RATE_LIMITED_DUP, got {escalation_events}"

    def test_executed_result_still_emits_event(self) -> None:
        """An EXECUTED _deactivate_fan() result means the command really ran —
        nat_vent_ceiling_escalation must still fire (regression guard for the
        gate itself: it must not become an unconditional suppression)."""
        engine = _make_engine()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)

        events = asyncio.run(_run_guard(engine))

        escalation_events = [e for e in events if e[0] == "nat_vent_ceiling_escalation"]
        assert len(escalation_events) == 1, f"expected 1 event for EXECUTED, got {escalation_events}"
        payload = escalation_events[0][1]
        assert payload["indoor"] == 76.0
        assert payload["comfort_cool"] == 74.0
        assert payload["hours_to_breach"] == 2.0

    def test_rate_limited_new_still_emits_event(self) -> None:
        """RATE_LIMITED_NEW is the FIRST report of a deferral window — unlike
        RATE_LIMITED_DUP (a repeat), this is new information and must still be
        reported, matching the away-ceiling sibling gates' behavior."""
        engine = _make_engine()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)

        events = asyncio.run(_run_guard(engine))

        escalation_events = [e for e in events if e[0] == "nat_vent_ceiling_escalation"]
        assert len(escalation_events) == 1, f"expected 1 event for RATE_LIMITED_NEW, got {escalation_events}"
