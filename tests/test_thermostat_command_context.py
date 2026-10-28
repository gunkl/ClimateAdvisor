"""Issue #1109: thermostat commands carry a recorded HA Context, like fan commands (diagnostic only).

Occupant experience being guarded: nothing changes day to day. When a thermostat change is
disputed ("I didn't touch it"), the log can now say whether the state change carried the
Context Climate Advisor sent with its own command. Owner decision D6: the context is
DIAGNOSTIC ONLY -- it must never change whether a thermostat change counts as a manual
override (the decision-neutral guard below).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import AutomationEngine, ClimateCallOutcome
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.learning import DailyRecord
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeEvent, FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"
COORDINATOR_LOGGER = "custom_components.climate_advisor.coordinator"


@pytest.fixture
def real_clock():
    """The plain-MagicMock engine has an unpatched dt_util; the context recency set needs a clock."""
    with patch.object(automation_module.dt_util, "now", return_value=datetime(2026, 1, 12, 7, 0, tzinfo=UTC)):
        yield


def _make_engine(extra: dict | None = None) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    state = MagicMock()
    state.state = "heat"
    state.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 70.0, "fan_modes": ["auto", "on"]}
    hass.states.get.return_value = state
    config = {"comfort_heat": 70, "comfort_cool": 75, "setback_heat": 60, "setback_cool": 80}
    config.update(extra or {})
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def test_call_climate_sends_a_context_whose_id_is_recorded_as_a_ca_climate_command(caplog, real_clock):
    engine = _make_engine()
    with caplog.at_level(logging.DEBUG, logger=AUTOMATION_LOGGER):
        result = asyncio.run(engine._call_climate("set_temperature", {"entity_id": "climate.thermostat"}, "lbl"))
    assert result.outcome is ClimateCallOutcome.OK
    kwargs = engine.hass.services.async_call.call_args.kwargs
    assert kwargs["blocking"] is True
    ctx = kwargs["context"]
    assert ctx is not None and ctx.id
    assert engine.command_context_matches("climate", ctx.id, None)
    # Kinds stay separate: a thermostat command must never be read as CA's own WHF fan command
    # by the fan-entity provenance check (that one DOES gate a decision).
    assert not engine.fan_command_context_matches(ctx.id, None)
    # Send-side DEBUG log carries the same id that was recorded.
    assert engine._recent_climate_command_context_ids[-1][0] == ctx.id
    assert any(ctx.id in r.getMessage() and "climate.set_temperature" in r.getMessage() for r in caplog.records)


def test_fan_command_context_still_recorded_under_fan_kind_only(real_clock):
    engine = _make_engine()
    asyncio.run(engine._call_fan_service_with_context("fan", "turn_on", "fan.whf"))
    ctx = engine.hass.services.async_call.call_args.kwargs["context"]
    assert engine.fan_command_context_matches(ctx.id, None)
    assert not engine.command_context_matches("climate", ctx.id, None)


def test_orphan_release_set_fan_mode_carries_a_recorded_climate_context(real_clock):
    engine = _make_engine()
    st = MagicMock()
    st.state = "heat"
    st.attributes = {"fan_mode": "on", "fan_modes": ["auto", "on"]}
    engine.hass.states.get.return_value = st
    asyncio.run(engine._release_orphaned_hvac_fan())
    call = engine.hass.services.async_call.call_args
    assert call.args[:2] == ("climate", "set_fan_mode")
    assert call.kwargs["blocking"] is True
    assert engine.command_context_matches("climate", call.kwargs["context"].id, None)


def test_off_assert_set_fan_mode_carries_a_recorded_climate_context(real_clock):
    engine = _make_engine({"fan_mode": "hvac_fan"})
    asyncio.run(engine._set_hvac_mode("off", reason="t"))
    fan_calls = [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("climate", "set_fan_mode")]
    assert len(fan_calls) == 1
    assert engine.command_context_matches("climate", fan_calls[0].kwargs["context"].id, None)


# --- decision-neutral guard (D6) ----------------------------------------------------------


def _thermo(setpoint: float) -> FakeState:
    return FakeState(
        state="heat",
        attributes={
            "current_temperature": 69.0,
            "temperature": setpoint,
            "hvac_action": "idle",
            "fan_mode": "auto",
            "fan_modes": ["auto", "on"],
            "hvac_modes": ["off", "heat", "cool"],
        },
    )


def _override_outcome(with_ca_context: bool):
    co, fh, sch, ev = build_headless_coordinator(
        config={"comfort_heat": 70, "comfort_cool": 78},
        climate_state="heat",
        climate_attributes=dict(_thermo(70.0).attributes),
        skip_startup_coalesce=True,
    )
    ce = co.config["climate_entity"]
    ae = co.automation_engine
    co._current_classification = DayClassification(
        day_type="cold",
        trend_direction="stable",
        trend_magnitude=0,
        today_high=40,
        today_low=25,
        tomorrow_high=40,
        tomorrow_low=25,
        hvac_mode="heat",
    )
    co._today_record = DailyRecord(date="2024-01-15", day_type="cold", trend_direction="stable")
    with sch.installed():
        # CA's own command: its context is what a matching state change would carry.
        run_coro(ae._call_climate("set_temperature", {"entity_id": ce, "temperature": 70.0}, "lbl"))
        ca_ctx = fh.action_log[-1].get("context")
        ae._last_commanded_hvac_mode = "heat"
        sch.advance_by(int(timedelta(minutes=10).total_seconds()))
        ev.clear()
        old, new = _thermo(70.0), _thermo(73.0)
        fh.states.set(ce, new)
        event = FakeEvent(
            event_type="state_changed",
            data={"entity_id": ce, "old_state": old, "new_state": new},
            context=ca_ctx if with_ca_context else None,
        )
        run_coro(co._async_thermostat_changed(event))
    return (
        ae._manual_override_active,
        ae._override_confirm_pending,
        co._today_record.manual_overrides,
        sorted(e[0] for e in ev),
    ), ca_ctx


def test_matching_ca_context_does_not_change_the_override_decision():
    """A thermostat setpoint change carrying CA's own command context is judged EXACTLY like one
    without it (D6: context is diagnostic only, never a suppression signal)."""
    with_ctx, ca_ctx = _override_outcome(with_ca_context=True)
    without_ctx, _ = _override_outcome(with_ca_context=False)
    assert with_ctx == without_ctx
    # Non-vacuous: the change really was detected as a manual override in both runs.
    assert with_ctx[0] or with_ctx[1]
    assert with_ctx[2] == 1


def test_thermostat_listener_logs_event_context_provenance_at_debug(caplog):
    co, fh, sch, ev = build_headless_coordinator(
        config={"comfort_heat": 70, "comfort_cool": 78},
        climate_state="heat",
        climate_attributes=dict(_thermo(70.0).attributes),
        skip_startup_coalesce=True,
    )
    ce = co.config["climate_entity"]
    from homeassistant.core import Context

    ctx = Context(user_id="user-123", parent_id="parent-456")
    old, new = _thermo(70.0), _thermo(72.0)
    with sch.installed(), caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOGGER):
        run_coro(
            co._async_thermostat_changed(
                FakeEvent(
                    event_type="state_changed", data={"entity_id": ce, "old_state": old, "new_state": new}, context=ctx
                )
            )
        )
    lines = [r.getMessage() for r in caplog.records if "thermostat state change provenance" in r.getMessage()]
    assert len(lines) == 1
    assert ctx.id in lines[0] and "parent-456" in lines[0] and "user-123" in lines[0]
    assert "context_matches_ca=False" in lines[0]
