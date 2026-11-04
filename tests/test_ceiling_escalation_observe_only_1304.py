"""Issue #1304: observe-only records no "Nat-vent escalated to AC" for an escalation it never sent.

Occupant: with automation switched off, a ventilation session is recorded and the ODE ceiling guard decides to
escalate to the AC. Nothing is sent, the fan and thermostat do not change -- yet the Activity record said "Nat-vent
escalated to AC -- nat-vent ended, AC armed". Now observe-only writes no ``nat_vent_ceiling_escalation`` row and logs
``[DRY RUN]`` at INFO instead; a fan stop that comes back ``FanCommandResult.DRY_RUN`` also keeps the session (its
fan is still running). Control: with automation on the escalation is unchanged.

Reachability: ``apply_classification()`` reaches the guard with a session recorded only when no fan is configured
(Issue #1142 D7 returns at DEFER_NAT_VENT for every configured fan, and the classification FSM reports
NOT_EVALUATED_BLOWER_NAT_VENT for the blower) -- the issue's probe shape, tested end to end below. The DRY_RUN fan
result on this branch is defense in depth: tested on the real guard shell (``_apply_ode_ceiling_guard_decision``) with
the real ESCALATE decision, the fan mode switched to ``hvac_fan`` only after the decision was made.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import patch

from custom_components.climate_advisor.automation import FanCommandResult
from tests.test_warm_day_comfort_gap import (
    AUTOMATION_LOGGER,
    _make_engine,
    _make_predicted_indoor,
    _make_warm_off_classification,
    _set_thermal_model,
)

_NOW = datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)


def _engine(*, dry_run: bool):
    """A recorded session, indoor 76 F past the 74 F ceiling and predicted to keep rising: the guard escalates."""
    engine = _make_engine(config_overrides={"comfort_cool": 74.0}, indoor_temp=76.0, current_mode="off")
    engine.dry_run = dry_run
    engine._last_outdoor_temp = 70.0
    engine._natural_vent_active = True
    _set_thermal_model(engine, k_passive=-0.05, conf="medium", k_active_cool=None)
    events: list[tuple[str, dict]] = []
    engine._emit_event_callback = lambda n, d: events.append((n, d))
    results: list[FanCommandResult] = []
    real_deactivate = engine._deactivate_fan

    async def _spy(**kwargs):
        result = await real_deactivate(**kwargs)
        results.append(result)
        return result

    engine._deactivate_fan = _spy
    return engine, events, results


def _escalation_rows(events) -> list:
    return [d for n, d in events if n == "nat_vent_ceiling_escalation"]


def _apply_classification(engine) -> None:
    curve = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        asyncio.run(engine.apply_classification(_make_warm_off_classification(), predicted_indoor=curve))


def _guard_shell_with_running_blower(engine) -> None:
    classification = _make_warm_off_classification()
    curve = _make_predicted_indoor(start_hour_utc=10, temps=[76.0, 77.0, 78.0])
    with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
        mock_dt.now.return_value = _NOW
        decision = engine._resolve_classification_fsm_state(classification=classification, predicted_indoor=curve)
        assert decision.ceiling_decision is not None, "precondition: the guard evaluated"
        engine.config["fan_mode"] = "hvac_fan"
        engine.hass.states.get.return_value.attributes["fan_modes"] = ["auto", "on"]
        engine._fan_active = True
        asyncio.run(engine._apply_ode_ceiling_guard_decision(classification, curve, decision))


def test_observe_only_escalation_writes_no_row(caplog):
    caplog.set_level(logging.INFO, logger=AUTOMATION_LOGGER)
    engine, events, results = _engine(dry_run=True)
    _apply_classification(engine)

    assert results, "precondition: the guard escalated (asked for the fan stop)"
    assert engine.hass.services.async_call.call_args_list == [], "observe-only sent a command"
    assert _escalation_rows(events) == [], "observe-only recorded an escalation to AC that never happened"
    assert any(
        r.levelno == logging.INFO and "[DRY RUN] ODE ceiling guard would end nat-vent" in r.getMessage()
        for r in caplog.records
    )


def test_control_automation_on_escalation_writes_row():
    engine, events, results = _engine(dry_run=False)
    _apply_classification(engine)

    rows = _escalation_rows(events)
    assert len(rows) == 1 and rows[0]["ceiling"] == 74.0
    assert engine._natural_vent_active is False
    calls = engine.hass.services.async_call.call_args_list
    assert any(c.args[1] == "set_hvac_mode" and c.args[2].get("hvac_mode") == "cool" for c in calls)


def test_observe_only_dry_run_fan_stop_keeps_the_session():
    engine, events, results = _engine(dry_run=True)
    _guard_shell_with_running_blower(engine)

    assert results == [FanCommandResult.DRY_RUN]
    assert engine.hass.services.async_call.call_args_list == [], "observe-only sent a command"
    assert _escalation_rows(events) == []
    assert engine._natural_vent_active is True, "the session was ended on paper while its blower keeps running"
    assert engine._fan_active is True


def test_control_automation_on_blower_stop_ends_the_session():
    engine, events, results = _engine(dry_run=False)
    _guard_shell_with_running_blower(engine)

    assert results == [FanCommandResult.EXECUTED]
    assert len(_escalation_rows(events)) == 1
    assert engine._natural_vent_active is False
    assert any(c.args[1] == "set_fan_mode" for c in engine.hass.services.async_call.call_args_list)
