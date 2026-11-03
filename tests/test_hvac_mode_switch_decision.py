"""Explicit mode switch before a heat/cool setpoint write (Issue #1049 step 3, Part of #1048).

``decide_explicit_mode_switch`` (pure) plus the real-engine behaviour of
``_ensure_hvac_mode_for_write`` as used by ``_set_temperature`` and the nudge/retry path.
Real engine throughout; the Sensi-like end-to-end cases live in test_hvac_mode_switch_sensi.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import AutomationEngine, HvacCommandResult
from custom_components.climate_advisor.desired_state import decide_explicit_mode_switch
from tools.sim_harness.build_engine import build_headless_engine
from tools.sim_harness.fake_hass import QUIRK_SENSI_LIKE

ALL = ("off", "heat", "cool", "heat_cool")
ENTITY = "climate.test_thermostat"


class ServiceValidationError(Exception):
    """Carries HA's class name (classification is by name)."""


class TestDecideExplicitModeSwitch:
    @pytest.mark.parametrize("live", ["off", "cool", "auto", "heat_cool", "dry", "fan_only"])
    def test_real_mode_different_from_heat_switches(self, live):
        assert decide_explicit_mode_switch(
            commanded_mode="heat", live_state=live, advertised_modes=ALL + ("auto",), recent_switch_to_mode=False
        ) is (live != "heat")

    @pytest.mark.parametrize("live", [None, "", "unavailable", "unknown", 5, object()])
    def test_unreadable_live_state_never_switches(self, live):
        assert not decide_explicit_mode_switch(
            commanded_mode="heat", live_state=live, advertised_modes=ALL, recent_switch_to_mode=False
        )

    @pytest.mark.parametrize("commanded", ["off", "auto", "fan_only", "heat_cool", ""])
    def test_only_heat_and_cool_are_ever_switched_to(self, commanded):
        assert not decide_explicit_mode_switch(
            commanded_mode=commanded,
            live_state="off",
            advertised_modes=ALL + ("auto", "fan_only"),
            recent_switch_to_mode=False,
        )

    def test_mode_must_be_advertised(self):
        assert not decide_explicit_mode_switch(
            commanded_mode="cool", live_state="off", advertised_modes=("off", "heat"), recent_switch_to_mode=False
        )
        assert not decide_explicit_mode_switch(
            commanded_mode="cool", live_state="off", advertised_modes=(), recent_switch_to_mode=False
        )

    def test_recent_successful_switch_dedupes(self):
        assert not decide_explicit_mode_switch(
            commanded_mode="cool", live_state="off", advertised_modes=ALL, recent_switch_to_mode=True
        )


def _mock_engine(state: str = "off", modes=("off", "heat", "cool")) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    st = MagicMock()
    st.state = state
    st.attributes = {"hvac_modes": list(modes), "temperature": 70.0}
    hass.states = MagicMock()
    hass.states.get.return_value = st
    config = {
        "temp_unit": "fahrenheit",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    return AutomationEngine(
        hass=hass,
        climate_entity=ENTITY,
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def _services(engine):
    return [c.args[1] for c in engine.hass.services.async_call.call_args_list]


class TestEngineModeSwitchBeforeWrite:
    def test_mode_call_precedes_setpoint_when_off(self):
        engine = _mock_engine("off")
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert _services(engine) == ["set_hvac_mode", "set_temperature"]
        assert engine._last_mode_switch_result is HvacCommandResult.EXECUTED

    def test_no_mode_call_when_already_in_mode_or_unreadable_or_unadvertised(self):
        for state, modes in (("heat", ("off", "heat")), ("unavailable", ("off", "heat")), ("off", ("off", "cool"))):
            engine = _mock_engine(state, modes)
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            assert _services(engine) == ["set_temperature"], (state, modes)

    def test_post_fan_verify_style_off_or_auto_write_never_switches(self):
        for mode in ("off", "auto"):
            engine = _mock_engine("heat", ("off", "heat", "cool", "auto"))
            asyncio.run(engine._set_temperature(70.0, reason="t", mode=mode))
            assert _services(engine) == ["set_temperature"]

    def test_dry_run_and_whf_block_send_no_mode_call(self):
        engine = _mock_engine("off")
        engine.dry_run = True
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert _services(engine) == []
        engine = _mock_engine("off")
        engine._whf_owns_hvac = lambda: True
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert _services(engine) == []

    def test_explicit_mode_then_write_in_one_cycle_sends_one_mode_call(self):
        """The five explicit-mode+write pairs (e.g. the ODE ceiling guard): no duplicate set_hvac_mode."""
        engine = _mock_engine("off")  # state mock never changes: a cloud thermostat still echoing Off
        fixed = datetime(2026, 1, 12, 7, 0, 0, tzinfo=UTC)
        with patch.object(automation_module.dt_util, "now", return_value=fixed):
            asyncio.run(engine._set_hvac_mode("cool", reason="t"))
            asyncio.run(engine._set_temperature(75.0, reason="t", mode="cool"))
        assert _services(engine) == ["set_hvac_mode", "set_temperature"]

    def test_failed_explicit_mode_clears_marker_so_next_write_retries_the_mode(self):
        engine = _mock_engine("off")
        asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert engine._mode_switch_marker is not None
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_hvac_mode("cool", reason="t"))
        assert engine._mode_switch_marker is None

    def test_failed_mode_switch_aborts_the_setpoint_and_announces_once(self):
        engine = _mock_engine("off")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert set(_services(engine)) == {"set_hvac_mode"}, "setpoint must not be sent into the wrong mode"
        assert engine._last_mode_switch_result is HvacCommandResult.FAILED
        assert [d["incident_class"] for _, d in emitted] == ["hvac_command_failed"]
        assert engine._temp_command_pending is False

    def test_rejected_mode_switch_aborts_quietly(self, caplog):
        engine = _mock_engine("off")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        engine.hass.services.async_call.side_effect = ServiceValidationError("mode not offered")
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.automation"):
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert set(_services(engine)) == {"set_hvac_mode"}
        assert [n for n, _ in emitted] == ["hvac_mode_not_applied"], "visible, but not a failure incident"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_timeout_is_unknown_not_failed_and_the_setpoint_still_goes_out(self):
        engine = _mock_engine("off")
        calls = []

        async def call(domain, service, data, **kw):
            calls.append(service)
            if service == "set_hvac_mode":
                await asyncio.Event().wait()

        engine.hass.services.async_call = call
        with patch.object(automation_module, "CLIMATE_CALL_TIMEOUT_S", 0.02):
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert calls == ["set_hvac_mode", "set_temperature"]
        assert engine._last_mode_switch_result is HvacCommandResult.UNCONFIRMED

    def test_out_of_range_setpoint_is_skipped_quietly_not_an_incident(self, caplog):
        """Deep-setback targets outside the thermostat's min/max surface as HA validation errors."""
        engine = _mock_engine("heat")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None

        async def call(domain, service, data, **kw):
            raise ServiceValidationError("Provided temperature 40 is not valid. Accepted range is 45 to 95")

        engine.hass.services.async_call = call
        with caplog.at_level(logging.WARNING, logger="custom_components.climate_advisor.automation"):
            asyncio.run(engine._set_temperature(40.0, reason="deep setback", mode="heat"))
        assert emitted == []
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("rejected by Home Assistant validation" in r.message for r in caplog.records)

    def test_newer_write_during_the_mode_await_aborts_the_older_write(self):
        engine = _mock_engine("off")
        sent = []
        gate: dict = {}

        async def call(domain, service, data, **kw):
            sent.append((service, data.get("temperature")))
            if service == "set_hvac_mode" and "event" not in gate:
                gate["event"] = asyncio.Event()
                await gate["event"].wait()

        engine.hass.services.async_call = call

        async def scenario():
            older = asyncio.ensure_future(engine._set_temperature(68.0, reason="older", mode="heat"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert "event" in gate, "older write must be parked in the mode await"
            await engine._set_temperature(72.0, reason="newer", mode="heat")  # supersedes
            gate["event"].set()
            await older

        asyncio.run(scenario())
        temps = [t for s, t in sent if s == "set_temperature"]
        assert temps == [72.0], f"the superseded 68.0 write must never be sent: {sent}"

    def test_nudge_path_switches_mode_before_the_nudge_and_never_via_set_temperature(self):
        engine = _mock_engine("off")
        captured: list = []
        wrong = engine.hass.states.get.return_value
        wrong.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 60.0}
        engine._setpoint_reject_streak = 1  # next rejection is the 2nd -> nudge
        # Thermostat is in heat when the write lands, then falls back to off by retry time.
        wrong.state = "heat"
        with (
            patch.object(automation_module, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
            patch.object(automation_module, "callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
            cb10 = next(cb for d, cb in captured if d == 10)
            coros: list = []
            engine.hass.async_create_task = MagicMock(side_effect=lambda c: coros.append(c))
            engine._own_setpoint_writes = []  # echo window over, unanswered (#1194 e F1)
            cb10(None)
            asyncio.run(coros.pop())
            wrong.state = "off"
            before = len(engine.hass.services.async_call.call_args_list)
            cb900 = next(cb for d, cb in captured if d == 900)
            cb900(None)
            with patch.object(engine, "_set_temperature", new=AsyncMock()) as st:
                asyncio.run(coros.pop())
                st.assert_not_called()
        later = [c.args[1] for c in engine.hass.services.async_call.call_args_list[before:]]
        assert later == ["set_hvac_mode", "set_temperature"]


class TestSensiLikeAutoAndHeatCool:
    """In-phase investigation (iii): a thermostat left in Auto/heat_cool is switched like any mismatch."""

    @pytest.mark.parametrize("live", ["auto", "heat_cool"])
    def test_auto_modes_are_switched_to_heat(self, live):
        engine, hass, scheduler, _ = build_headless_engine(
            config={
                "comfort_heat": 70,
                "comfort_cool": 75,
                "setback_heat": 60,
                "setback_cool": 80,
                "fan_mode": "disabled",
            },
            climate_entity=ENTITY,
            climate_state=live,
            climate_attributes={
                "current_temperature": 64.0,
                "hvac_modes": ["off", "heat", "cool", "heat_cool", "auto"],
            },
            start_time=datetime(2026, 1, 12, 7, 0, 0, tzinfo=UTC),
        )
        hass.set_entity_quirk(ENTITY, QUIRK_SENSI_LIKE)
        with scheduler.installed():
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
        assert hass.states.get(ENTITY).state == "heat"


class TestPendingRetryRespectsManualControl:
    """In-phase investigation (i): a retry must not re-assert CA's setpoint over a user's change."""

    @pytest.mark.parametrize("flag", ["_manual_override_active", "_override_confirm_pending"])
    def test_retry_skipped_when_user_took_over_after_the_rejected_write(self, flag):
        engine = _mock_engine("heat")
        wrong = engine.hass.states.get.return_value
        wrong.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 60.0}  # rejected -> REASSERT
        captured: list = []
        with (
            patch.object(automation_module, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
            patch.object(automation_module, "callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
            coros: list = []
            engine.hass.async_create_task = MagicMock(side_effect=lambda c: coros.append(c))
            engine._own_setpoint_writes = []  # echo window over, unanswered (#1194 e F1)
            next(cb for d, cb in captured if d == 10)(None)
            asyncio.run(coros.pop())
            setattr(engine, flag, True)  # user changes the thermostat during the 15-minute wait
            before = engine.hass.services.async_call.call_count
            next(cb for d, cb in captured if d == 900)(None)
            asyncio.run(coros.pop())
        assert engine.hass.services.async_call.call_count == before
