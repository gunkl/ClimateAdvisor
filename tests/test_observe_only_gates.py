"""Issue #1108: observe-only (automation switched off) really means hands-off.

Occupant experience being guarded: with the Climate Advisor automation switch OFF, the
person expects CA to watch and log only. Before this fix CA could still (a) turn their
thermostat fan off once after a reload/restart (orphan release), (b) switch the thermostat
mode and leave the setpoint 1°F off target up to 15 min after a rejected write (setpoint
nudge), and (c) send an unexpected ``turn_off`` RF transmission / control-entity flip to a
dual-entity whole-house fan that is already physically off (drift correction).

Every test drives the REAL ``AutomationEngine``. The gates sit at the three command choke
points (``_release_orphaned_hvac_fan``, ``_call_climate``, ``_call_fan_service_with_context``)
plus a mandatory early return in the setpoint ``_retry_callback`` so no false
``setpoint_nudge`` Activity Log line is written for a nudge that never happens.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import (
    AutomationEngine,
    ClimateCallOutcome,
    ClimateCallResult,
    HvacCommandResult,
)
from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_WHOLE_HOUSE
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


def _make_engine(extra_config: dict | None = None) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    state = MagicMock()
    state.state = "heat"
    state.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 70.0, "fan_modes": ["auto", "on"]}
    hass.states.get.return_value = state
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    config.update(extra_config or {})
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def _run_scheduled(engine, captured, delay):
    """Fire the captured async_call_later callback registered with ``delay`` (runs its task)."""
    cb = next(cb for d, cb in captured if d == delay)
    coros: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: coros.append(coro))
    cb(None)
    for coro in coros:
        asyncio.run(coro)


# --- (a) orphan release -----------------------------------------------------------------


def _thermo(fan_attr: str, action: str) -> FakeState:
    return FakeState(
        state="heat",
        attributes={
            "current_temperature": 72.0,
            "fan_mode": fan_attr,
            "hvac_action": action,
            "fan_modes": ["auto", "on"],
            "hvac_modes": ["off", "heat", "cool"],
            "temperature": 70.0,
        },
    )


def _orphan_setup():
    co, fh, sch, ev = build_headless_coordinator(
        config={"fan_mode": "disabled", "comfort_heat": 70, "comfort_cool": 78}, skip_startup_coalesce=True
    )
    co._read_live_remote_timer_provenance = lambda: None
    co._resolved_sensors = []
    ce = co.config["climate_entity"]
    fh.states.set(ce, _thermo("on", "fan"))
    return co, fh, sch, ev, ce


def test_orphan_release_in_observe_only_sends_nothing_and_clears_the_pending_release(caplog):
    """Automation off: the fan the occupant sees running is left alone, and the report says so."""
    co, fh, sch, ev, _ce = _orphan_setup()
    ae = co.automation_engine
    with sch.installed():
        ae.restore_state({"fan_active": True, "fan_on_since": sch.now().isoformat()})
        assert ae._release_orphan_fan_pending is True
        ae.dry_run = True
        fh.action_log.clear()
        with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
            run_coro(co._do_startup_coalesce())
            sch.advance_to(sch.now())
    fan_calls = [a for a in fh.action_log if a["domain"] == "climate" and a["service"] == "set_fan_mode"]
    assert fan_calls == [], "observe-only must not turn the occupant's thermostat fan off"
    assert ae._release_orphan_fan_pending is False, "skip-and-clear (D1): the release is dropped, not deferred"
    rel = [e[1] for e in ev if e[0] == "fan_orphan_released"]
    assert len(rel) == 1
    assert rel[0]["dry_run"] is True
    assert rel[0]["command_sent"] is None
    assert rel[0]["old_fan_mode"] == "on"
    assert any("[DRY RUN]" in r.getMessage() and "release" in r.getMessage() for r in caplog.records)


def test_orphan_release_with_automation_on_is_unchanged():
    """Control: automation on still hands the blower back once (by design, §9e-B)."""
    co, fh, sch, ev, _ce = _orphan_setup()
    ae = co.automation_engine
    with sch.installed():
        ae.restore_state({"fan_active": True, "fan_on_since": sch.now().isoformat()})
        run_coro(co._do_startup_coalesce())
        sch.advance_to(sch.now())
    fan_calls = [a for a in fh.action_log if a["domain"] == "climate" and a["service"] == "set_fan_mode"]
    assert len(fan_calls) == 1 and fan_calls[0]["data"]["fan_mode"] == "auto"
    rel = [e[1] for e in ev if e[0] == "fan_orphan_released"]
    assert len(rel) == 1 and rel[0]["command_sent"] == "auto" and not rel[0].get("dry_run")


def test_orphan_release_renderer_says_fan_left_as_is_in_observe_only():
    from custom_components.climate_advisor import ai_skills_context as c

    label, settings = c.EVENT_RENDERERS["fan_orphan_released"](
        {"old_fan_mode": "on", "command_sent": None, "dry_run": True}, "F"
    )
    assert "released" not in label.lower()
    assert "observe-only" in (label + settings).lower()
    assert "None" not in settings
    # Normal branch unchanged.
    label2, _ = c.EVENT_RENDERERS["fan_orphan_released"]({"old_fan_mode": "on", "command_sent": "auto"}, "F")
    assert label2 == "Fan released after configuration change"


# --- (b) setpoint nudge -----------------------------------------------------------------


def test_nudge_retry_after_automation_switched_off_sends_nothing_and_logs_no_nudge():
    """Rejected write -> 15-min nudge retry armed -> automation switched off -> retry fires.

    Occupant: nothing reaches the thermostat (no mode switch, no 1°F-off setpoint) and the
    Activity Log shows no nudge or "mode not applied" line for a command never sent (J1).
    """
    engine = _make_engine()
    emitted: list = []
    engine._emit_event_callback = lambda n, d: emitted.append((n, d))
    wrong = MagicMock()
    wrong.state = "off"  # thermostat Off -> the nudge path would switch mode first
    wrong.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 60.0}
    engine.hass.states.get = MagicMock(return_value=wrong)
    engine._setpoint_reject_streak = 1  # next rejection is the 2nd -> NUDGE_THEN_TARGET
    captured: list = []
    with (
        patch.object(automation_module, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
        patch.object(automation_module, "callback", side_effect=lambda fn: fn),
    ):
        asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
        _run_scheduled(engine, captured, 10)  # verify -> REASSERT -> arms the 900 s retry
        assert any(d == 900 for d, _ in captured)
        # Automation switched off while the retry is pending.
        engine.dry_run = True
        engine.hass.services.async_call.reset_mock()
        emitted.clear()
        engine._last_commanded_hvac_mode = "sentinel"
        _run_scheduled(engine, captured, 900)
    climate_calls = [c for c in engine.hass.services.async_call.call_args_list if c.args[0] == "climate"]
    assert climate_calls == []
    names = [n for n, _ in emitted]
    assert "setpoint_nudge" not in names
    assert "hvac_mode_not_applied" not in names
    assert engine._last_commanded_hvac_mode == "sentinel"


def test_ensure_mode_for_write_backstop_in_observe_only_is_quiet():
    """Choke-point backstop (J1c): a direct mode-switch-before-write in observe-only sends
    nothing, stops the write, and writes no false "thermostat did not apply mode" line."""
    engine = _make_engine()
    emitted: list = []
    engine._emit_event_callback = lambda n, d: emitted.append((n, d))
    off = MagicMock()
    off.state = "off"
    off.attributes = {"hvac_modes": ["off", "heat", "cool"]}
    engine.hass.states.get = MagicMock(return_value=off)
    engine.dry_run = True
    engine._last_commanded_hvac_mode = "sentinel"
    assert asyncio.run(engine._ensure_hvac_mode_for_write("heat", engine._write_seq)) is False
    engine.hass.services.async_call.assert_not_called()
    assert "hvac_mode_not_applied" not in [n for n, _ in emitted]
    assert engine._last_commanded_hvac_mode == "sentinel"


def test_call_climate_in_observe_only_returns_distinct_dry_run_outcome(caplog):
    engine = _make_engine()
    engine.dry_run = True
    with caplog.at_level(logging.INFO, logger=AUTOMATION_LOGGER):
        result = asyncio.run(engine._call_climate("set_temperature", {"entity_id": "climate.thermostat"}, "lbl"))
    assert result.outcome is ClimateCallOutcome.DRY_RUN
    assert result.outcome is not ClimateCallOutcome.REJECTED
    engine.hass.services.async_call.assert_not_called()
    assert any("[DRY RUN] Would call climate.set_temperature" in r.getMessage() for r in caplog.records)


def _dry_run_call(*_a, **_k):
    async def _r():
        return ClimateCallResult(ClimateCallOutcome.DRY_RUN)

    return _r()


def test_callers_treat_dry_run_outcome_as_stop_without_a_note():
    """Every caller of ``_call_climate`` stops on DRY_RUN without failure/rejection notes."""
    engine = _make_engine()
    emitted: list = []
    engine._emit_event_callback = lambda n, d: emitted.append((n, d))
    engine._call_climate = MagicMock(side_effect=_dry_run_call)
    engine._note_mode_rejected = MagicMock()
    engine._note_hvac_command_failed = MagicMock()
    engine._schedule_mode_alignment_recheck = MagicMock()
    with patch.object(automation_module, "async_call_later") as later:
        assert asyncio.run(engine._set_hvac_mode("cool", reason="t")) is HvacCommandResult.DRY_RUN
        assert asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat")) is HvacCommandResult.DRY_RUN
        later.assert_not_called()  # no verify / retry armed for a command never sent
    off = MagicMock()
    off.state = "off"
    off.attributes = {"hvac_modes": ["off", "heat", "cool"]}
    engine.hass.states.get = MagicMock(return_value=off)
    engine.dry_run = False  # reach the choke point itself (outer gate bypassed)
    assert asyncio.run(engine._ensure_hvac_mode_for_write("heat", engine._write_seq)) is False
    engine._note_mode_rejected.assert_not_called()
    engine._note_hvac_command_failed.assert_not_called()
    engine._schedule_mode_alignment_recheck.assert_not_called()
    assert "hvac_mode_not_applied" not in [n for n, _ in emitted]


# --- (c) whole-house-fan drift off ------------------------------------------------------


def test_drift_off_command_in_observe_only_sends_no_turn_off():
    """Dual-entity WHF: control reads "on", physical fan off for 2 ticks, automation off.

    Occupant: no unexpected RF transmission / control-entity flip (J3) while CA is hands-off.
    """
    engine = _make_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whf"})
    control = MagicMock()
    control.state = "on"
    control.attributes = {"fan_mode": "auto", "hvac_action": ""}
    engine.hass.states.get = MagicMock(return_value=control)
    engine._get_fan_physical_state_callback = MagicMock(return_value=False)
    engine._is_recent_fan_command_callback = MagicMock(return_value=False)
    engine._clear_fan_flags_and_start_grace = MagicMock()
    engine._sensor_check_callback = MagicMock(return_value=True)
    coros: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: coros.append(coro))
    engine.dry_run = True
    engine._fan_active = True
    engine._reconcile_fan_physical_drift()
    engine._reconcile_fan_physical_drift()
    assert coros, "the drift off-command task must still be reached (else this test is vacuous)"
    for coro in coros:
        asyncio.run(coro)
    fan_calls = [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("fan", "turn_off")]
    assert fan_calls == []
    assert engine._recent_fan_command_context_ids == [], "no context recorded for a command never sent"
    assert engine._fan_command_pending is False
