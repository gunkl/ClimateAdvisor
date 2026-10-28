"""Climate command visibility (Issue #1049, Part of #1048).

Step 2 of the #1048 fix makes thermostat command failures *visible* without changing which
commands are sent: ``_set_hvac_mode`` reports an ``HvacCommandResult``; the three
``climate.*`` writes go through ``_call_climate`` (blocking, non-cancelling wait, HA
validation errors classified apart from real failures); ``hvac_command_failed`` is announced
once per episode with a recovery; incident payload temperatures are internal degrees F; the
command-time stamps are refreshed after an awaited call; and ``_set_hvac_mode`` bumps
``_write_seq`` so a pending retry cannot turn heat back on through a door/window pause.

Every test drives the REAL ``AutomationEngine`` (no mirrored logic).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import (
    AutomationEngine,
    ClimateCallOutcome,
    HvacCommandResult,
)
from custom_components.climate_advisor.incident_payload import build_incident_payload, is_climate_validation_error

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


class ServiceValidationError(Exception):
    """Stand-in carrying HA's class name (classification is by name, stub-safe)."""


def _make_engine(temp_unit: str = "fahrenheit") -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.states = MagicMock()
    state = MagicMock()
    state.state = "heat"
    state.attributes = {"hvac_modes": ["off", "heat", "cool"], "temperature": 70.0}
    hass.states.get.return_value = state
    config = {
        "temp_unit": temp_unit,
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


class TestCallClimate:
    def test_passes_blocking_true_as_keyword(self):
        engine = _make_engine()
        result = asyncio.run(engine._call_climate("set_hvac_mode", {"entity_id": "climate.thermostat"}, "label"))
        assert result.outcome is ClimateCallOutcome.OK
        args, kwargs = engine.hass.services.async_call.call_args
        assert args[:2] == ("climate", "set_hvac_mode")
        assert kwargs == {"blocking": True, "context": ANY}  # Issue #1109: CA context attached

    def test_timeout_is_unknown_state_not_failure_and_does_not_cancel_the_call(self, caplog):
        engine = _make_engine()
        release = {}
        finished = []

        async def slow_call(*_a, **_k):
            release["event"] = asyncio.Event()
            await release["event"].wait()
            finished.append(True)

        engine.hass.services.async_call = slow_call

        async def run():
            with (
                patch.object(automation_module, "CLIMATE_CALL_TIMEOUT_S", 0.05),
                caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER),
            ):
                result = await engine._call_climate("set_hvac_mode", {"entity_id": "x"}, "HVAC command failed")
            # The in-flight call must survive the timeout (non-cancelling wait) and still complete.
            release["event"].set()
            await asyncio.sleep(0.01)
            return result

        result = asyncio.run(run())
        assert result.outcome is ClimateCallOutcome.TIMEOUT
        assert finished == [True], "timeout must not cancel the in-flight service call"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_real_failure_logs_error_with_label(self, caplog):
        engine = _make_engine()
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR, logger=AUTOMATION_LOGGER):
            result = asyncio.run(engine._call_climate("set_temperature", {}, "Temperature command failed — x"))
        assert result.outcome is ClimateCallOutcome.FAILED
        assert any("Temperature command failed — x: boom" in r.message for r in caplog.records)

    def test_validation_error_is_classified_rejected_and_not_an_error(self, caplog):
        engine = _make_engine()
        engine.hass.services.async_call.side_effect = ServiceValidationError("mode not supported")
        with caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER):
            result = asyncio.run(engine._call_climate("set_hvac_mode", {}, "HVAC command failed"))
        assert result.outcome is ClimateCallOutcome.REJECTED
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_is_climate_validation_error_by_name_only(self):
        assert is_climate_validation_error(ServiceValidationError("x"))
        assert not is_climate_validation_error(RuntimeError("x"))


class TestHvacCommandResult:
    def test_executed(self):
        engine = _make_engine()
        assert asyncio.run(engine._set_hvac_mode("heat", reason="t")) is HvacCommandResult.EXECUTED

    def test_dry_run(self):
        engine = _make_engine()
        engine.dry_run = True
        assert asyncio.run(engine._set_hvac_mode("heat", reason="t")) is HvacCommandResult.DRY_RUN
        engine.hass.services.async_call.assert_not_called()

    def test_whf_blocked(self):
        engine = _make_engine()
        engine._whf_owns_hvac = lambda: True
        assert asyncio.run(engine._set_hvac_mode("heat", reason="t")) is HvacCommandResult.WHF_BLOCKED

    def test_failed(self):
        engine = _make_engine()
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        assert asyncio.run(engine._set_hvac_mode("heat", reason="t")) is HvacCommandResult.FAILED

    def test_validation_rejection_is_failed_without_incident(self):
        engine = _make_engine()
        engine.hass.services.async_call.side_effect = ServiceValidationError("mode not offered")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        assert asyncio.run(engine._set_hvac_mode("heat_cool", reason="t")) is HvacCommandResult.FAILED
        # Visible as one Activity Log row, but NOT an hvac_command_failed incident / episode.
        assert [n for n, _ in emitted] == ["hvac_mode_not_applied"]
        assert emitted[0][1]["kind"] == "rejected"
        assert engine._hvac_cmd_fail_announced is None

    def test_unconfirmed_on_timeout(self):
        engine = _make_engine()

        async def hang(*_a, **_k):
            await asyncio.Event().wait()

        engine.hass.services.async_call = hang
        with patch.object(automation_module, "CLIMATE_CALL_TIMEOUT_S", 0.02):
            result = asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert result is HvacCommandResult.UNCONFIRMED


class TestEpisodeGating:
    def test_repeated_failures_emit_once_then_one_recovery(self):
        engine = _make_engine()
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        engine.hass.services.async_call.side_effect = RuntimeError("boom")

        for _ in range(3):
            asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert [(n, d.get("incident_class")) for n, d in emitted] == [("incident_detected", "hvac_command_failed")], (
            "repeat failures within one episode must not re-announce"
        )

        engine.hass.services.async_call.side_effect = None
        asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
        assert [n for n, _ in emitted] == ["incident_detected", "hvac_command_recovered"]
        assert "minutes_down" in emitted[-1][1]

        # A new failure after recovery opens a new episode.
        engine.hass.services.async_call.side_effect = RuntimeError("again")
        asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert emitted[-1][1]["incident_class"] == "hvac_command_failed"
        assert len(emitted) == 3

    def test_success_with_no_open_episode_emits_nothing(self):
        engine = _make_engine()
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert emitted == []


class TestPayloadUnits:
    def test_failed_temperature_payload_setpoint_is_internal_fahrenheit_for_celsius_user(self):
        engine = _make_engine(temp_unit="celsius")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
        payload = emitted[0][1]
        assert payload["setpoint_f"] == 72.0  # NOT 22.2 (the Celsius service value)
        assert engine.hass.services.async_call.call_args[0][2]["temperature"] < 30  # service got C

    def test_engine_payloads_carry_the_shared_envelope(self):
        engine = _make_engine()
        engine._occupancy_mode = "away"
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        payload = emitted[0][1]
        expected = build_incident_payload(
            "hvac_command_failed",
            payload["incident_id"],
            comfort_heat=70,
            comfort_cool=75,
            occupancy_mode="away",
            extra={"hvac_mode": "heat", "climate_entity": "climate.thermostat", "error": "boom"},
        )
        assert payload == expected

    def test_nudge_value_is_internal_fahrenheit_for_celsius_user(self):
        engine = _make_engine(temp_unit="celsius")
        emitted = []
        # Issue #1073: thermostat_mode_set is an orthogonal audit row (tested in test_mode_write_events.py).
        engine._emit_event_callback = lambda n, d: emitted.append((n, d)) if n != "thermostat_mode_set" else None
        wrong = MagicMock()
        wrong.state = "heat"
        wrong.attributes = {"temperature": 20.0}  # 68F reported vs 72F commanded
        engine.hass.states.get = MagicMock(return_value=wrong)
        captured: list = []
        engine._setpoint_reject_streak = 1  # next rejection is the 2nd -> nudge

        with (
            patch.object(automation_module, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
            patch.object(automation_module, "callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
            _run_scheduled(engine, captured, 10)
            _run_scheduled(engine, captured, 900)

        nudge = [d for n, d in emitted if n == "setpoint_nudge"]
        assert len(nudge) == 1
        assert abs(nudge[0]["nudge_value"] - 71.0) < 0.01  # heat nudges -1F, in F
        assert nudge[0]["real_target"] == 72.0


def _run_scheduled(engine, captured, delay):
    """Fire the captured async_call_later callback registered with ``delay`` (runs its task)."""
    cb = next(cb for d, cb in captured if d == delay)
    coros: list = []
    engine.hass.async_create_task = MagicMock(side_effect=lambda coro: coros.append(coro))
    cb(None)
    for coro in coros:
        asyncio.run(coro)


class TestRestampAfterAwaitedCall:
    def test_all_three_command_stamps_are_refreshed_after_the_call(self):
        engine = _make_engine()
        clock = [dt.datetime(2026, 1, 12, 7, 0, 0, tzinfo=dt.UTC)]

        async def slow_cloud_call(*_a, **_k):
            clock[0] += dt.timedelta(seconds=25)  # cloud thermostat answers ~25 s later

        engine.hass.services.async_call = slow_cloud_call
        with patch.object(automation_module.dt_util, "now", side_effect=lambda: clock[0]):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
        end = clock[0]
        assert engine._temp_command_time == end
        assert engine._last_commanded_hvac_time == end
        assert engine._hvac_command_time == end


class TestWriteSeqBumpedByModeCommand:
    def test_pending_retry_does_not_fire_after_a_pause(self):
        """A rejected-setpoint retry scheduled before a door/window pause must go stale.

        Occupant impact: without the bump the 15-minute retry would re-send the heat setpoint
        (and mode) through the open window after CA turned the thermostat Off.
        """
        engine = _make_engine()
        wrong = MagicMock()
        wrong.state = "heat"
        wrong.attributes = {"temperature": 60.0}  # reports far from the 72F commanded -> REASSERT
        engine.hass.states.get = MagicMock(return_value=wrong)
        captured: list = []

        with (
            patch.object(automation_module, "async_call_later", side_effect=lambda h, d, cb: captured.append((d, cb))),
            patch.object(automation_module, "callback", side_effect=lambda fn: fn),
        ):
            asyncio.run(engine._set_temperature(72.0, reason="t", mode="heat"))
            _run_scheduled(engine, captured, 10)  # validation -> schedules the 900 s retry
            assert any(d == 900 for d, _ in captured)

            asyncio.run(engine._set_hvac_mode("off", reason="door/window pause"))
            calls_before = engine.hass.services.async_call.call_count
            _run_scheduled(engine, captured, 900)  # the retry fires after the pause

        assert engine.hass.services.async_call.call_count == calls_before, "stale retry must not re-send a setpoint"
        assert not [
            c for c in engine.hass.services.async_call.call_args_list[calls_before:] if c[0][1] == "set_temperature"
        ]

    def test_mode_command_bumps_write_seq_but_dry_run_does_not(self):
        engine = _make_engine()
        before = engine._write_seq
        asyncio.run(engine._set_hvac_mode("off", reason="t"))
        assert engine._write_seq == before + 1
        engine.dry_run = True
        asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert engine._write_seq == before + 1
