"""Silver test-coverage (#1644): the guard and failure branches of the automation engine's re-send chains,
observe-only notices and small state readers.

Occupant view: these are the paths that decide whether Climate Advisor re-sends an Off to a thermostat still
heating into an open window, re-sends a restore that never landed, or stays out of the way because the occupant
took over, a fan session owns the HVAC, or automation is switched off. Every test drives the REAL engine method
and checks what it sent (or did not send), what it recorded, or what it returned.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.climate_advisor.automation as _automation_mod
from custom_components.climate_advisor.automation import (
    AutomationEngine,
    FanCommandResult,
    HvacCommandResult,
    PendingRestore,
    _parse_forecast_dt,
)
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.whf_safety_decision import WhfSafetyAction, WhfSafetyDecision

_NOW = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)
_DT_NOW = "custom_components.climate_advisor.automation.dt_util.now"


def _consume_coroutine(coro):
    coro.close()


def _make_engine(
    *,
    thermostat: str | None = "cool",
    attributes: dict | None = None,
    config: dict | None = None,
    sensors: list[str] | None = None,
) -> tuple[AutomationEngine, list[tuple]]:
    """A real engine whose thermostat reads ``thermostat``; returns (engine, captured Activity events)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    climate_state = MagicMock()
    climate_state.state = thermostat
    climate_state.attributes = attributes if attributes is not None else {"current_temperature": 72.0}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state if thermostat is not None else None)
    cfg = {
        "comfort_heat": 68,
        "comfort_cool": 76,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    cfg.update(config or {})
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=sensors if sensors is not None else ["binary_sensor.window"],
        notify_service="notify.notify",
        config=cfg,
    )
    events: list[tuple] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    engine._natural_vent_active = False
    engine._fan_override_active = False
    return engine, events


# ---------------------------------------------------------------------------
# _retry_open_window_off (Issues #1302, #1307)
# ---------------------------------------------------------------------------


class TestRetryOpenWindowOff:
    """A window-open Off re-send acts only while its reason still holds; otherwise nothing is sent."""

    def _run(self, engine: AutomationEngine, *, session: str = "pause", attempt: int = 0) -> AsyncMock:
        set_mode = AsyncMock(return_value=HvacCommandResult.EXECUTED)
        engine._set_hvac_mode = set_mode
        asyncio.run(engine._retry_open_window_off("window open", attempt, session=session))
        return set_mode

    def test_shut_down_engine_sends_nothing(self):
        engine, _ = _make_engine()
        engine._paused_by_door = True
        engine._shut_down = True
        assert self._run(engine).await_count == 0

    def test_observe_only_sends_nothing(self):
        engine, events = _make_engine()
        engine._paused_by_door = True
        engine.dry_run = True
        assert self._run(engine).await_count == 0
        assert events == []

    def test_blower_session_ended_sends_nothing(self):
        engine, _ = _make_engine(config={CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._natural_vent_active = False
        assert self._run(engine, session="blower").await_count == 0

    def test_blower_without_window_settled_open_sends_nothing(self):
        engine, _ = _make_engine(config={CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._natural_vent_active = True
        with patch.object(engine, "_any_monitored_sensor_settled_open", return_value=False):
            assert self._run(engine, session="blower").await_count == 0

    def test_blower_with_window_open_resends_off_and_reports_once(self):
        """Thermostat still cooling into an open window during blower ventilation: the Off goes out again."""
        engine, events = _make_engine(config={CONF_FAN_MODE: FAN_MODE_HVAC})
        engine._natural_vent_active = True
        with patch.object(engine, "_any_monitored_sensor_settled_open", return_value=True):
            set_mode = self._run(engine, session="blower")
        assert set_mode.await_args.args[0] == "off"
        rows = [p for n, p in events if n == "hvac_mode_not_applied"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "open_window_retry"
        assert rows[0]["actual_mode"] == "cool"

    def test_later_attempt_writes_no_second_activity_row(self):
        engine, events = _make_engine()
        engine._paused_by_door = True
        set_mode = self._run(engine, attempt=1)
        assert set_mode.await_count == 1
        assert [n for n, _ in events if n == "hvac_mode_not_applied"] == []

    def test_pause_ended_sends_nothing(self):
        engine, _ = _make_engine()
        engine._paused_by_door = False
        assert self._run(engine).await_count == 0

    def test_occupant_owns_thermostat_sends_nothing(self):
        engine, _ = _make_engine()
        engine._paused_by_door = True
        engine._manual_override_active = True
        assert self._run(engine).await_count == 0

    def test_fan_session_owns_hvac_sends_nothing(self):
        engine, _ = _make_engine()
        engine._paused_by_door = True
        with patch.object(engine, "_whf_owns_hvac", return_value=True):
            assert self._run(engine).await_count == 0

    def test_nat_vent_owns_hvac_during_pause_sends_nothing(self):
        engine, _ = _make_engine()
        engine._paused_by_door = True
        engine._natural_vent_active = True
        assert self._run(engine).await_count == 0

    def test_thermostat_already_off_sends_nothing(self):
        engine, _ = _make_engine(thermostat="off")
        engine._paused_by_door = True
        assert self._run(engine).await_count == 0

    def test_error_is_logged_not_raised(self, caplog):
        engine, _ = _make_engine()
        engine._paused_by_door = True
        engine.hass.states.get.side_effect = RuntimeError("boom")
        self._run(engine)
        assert "Open-window Off retry failed" in caplog.text


# ---------------------------------------------------------------------------
# _retry_restore / _restore_resend_stop_reason (Issues #1308, #1312)
# ---------------------------------------------------------------------------


class TestRetryRestore:
    """A restore that did not land is re-sent, dropped, or finished depending on who owns the thermostat now."""

    def _engine(self, *, thermostat: str = "off", kind: str = "automation", trigger: str | None = "sensor_closed"):
        engine, events = _make_engine(thermostat=thermostat)
        engine._pending_restore = PendingRestore(target="heat", trigger=trigger, reason="window closed", kind=kind)
        engine._restore_resend_cancel = MagicMock()
        engine._last_commanded_hvac_mode = None
        engine._current_classification = None
        engine._set_hvac_mode = AsyncMock(return_value=HvacCommandResult.FAILED)
        engine._set_temperature_for_mode = AsyncMock(return_value=HvacCommandResult.EXECUTED)
        engine._start_grace_period = MagicMock()
        return engine, events

    def test_shut_down_does_nothing(self):
        engine, _ = self._engine()
        engine._shut_down = True
        asyncio.run(engine._retry_restore(0))
        assert engine._pending_restore is not None
        assert engine._set_hvac_mode.await_count == 0

    def test_no_pending_restore_does_nothing(self):
        engine, _ = self._engine()
        engine._pending_restore = None
        asyncio.run(engine._retry_restore(0))
        assert engine._set_hvac_mode.await_count == 0

    def test_observe_only_drops_the_restore(self):
        engine, _ = self._engine()
        cancel = engine._restore_resend_cancel
        engine.dry_run = True
        asyncio.run(engine._retry_restore(0))
        assert engine._pending_restore is None
        cancel.assert_called_once()
        assert engine._set_hvac_mode.await_count == 0
        engine._start_grace_period.assert_not_called()

    def test_occupant_change_stops_without_grace(self):
        engine, _ = self._engine()
        engine._override_confirm_pending = True
        asyncio.run(engine._retry_restore(0))
        assert engine._pending_restore is None
        assert engine._set_hvac_mode.await_count == 0
        engine._start_grace_period.assert_not_called()

    def test_already_landed_finishes_and_starts_grace(self):
        engine, _ = self._engine(thermostat="heat")
        asyncio.run(engine._retry_restore(0))
        assert engine._pending_restore is None
        assert engine._set_hvac_mode.await_count == 0
        engine._start_grace_period.assert_called_once_with("automation", trigger="sensor_closed")

    def test_comfort_write_used_when_classification_known(self):
        engine, events = self._engine()
        engine._current_classification = MagicMock()
        asyncio.run(engine._retry_restore(0))
        engine._set_temperature_for_mode.assert_awaited_once()
        assert engine._set_hvac_mode.await_count == 0
        assert engine._pending_restore is None  # EXECUTED -> landed
        engine._start_grace_period.assert_called_once()
        rows = [p for n, p in events if n == "hvac_mode_not_applied"]
        assert rows and rows[0]["kind"] == "restore_retry" and rows[0]["requested_mode"] == "heat"

    def test_newer_restore_replacing_this_one_is_left_alone(self):
        engine, _ = self._engine()
        newer = PendingRestore(target="cool", trigger=None, reason="fan release")

        async def _swap(*_a, **_k):
            engine._pending_restore = newer
            return HvacCommandResult.FAILED

        engine._set_hvac_mode = AsyncMock(side_effect=_swap)
        asyncio.run(engine._retry_restore(0))
        assert engine._pending_restore is newer
        engine._start_grace_period.assert_not_called()

    def test_exhausted_chain_drops_the_restore(self):
        """After the last re-send the restore is dropped (G2): the 30-minute cycle re-applies the plan."""
        engine, _ = self._engine()
        asyncio.run(engine._retry_restore(len(_automation_mod.OPEN_WINDOW_OFF_RETRY_DELAYS_S)))
        assert engine._set_hvac_mode.await_count == 1
        assert engine._pending_restore is None
        engine._start_grace_period.assert_not_called()

    def test_failed_resend_rearms_the_chain(self):
        engine, _ = self._engine()
        with patch.object(engine, "_rearm_timer") as rearm:
            asyncio.run(engine._retry_restore(0))
        rearm.assert_called_once()
        assert rearm.call_args.args[0] == "_restore_resend_cancel"
        assert engine._pending_restore is not None

    def test_error_is_logged_not_raised(self, caplog):
        engine, _ = self._engine()
        engine.hass.states.get.side_effect = RuntimeError("boom")
        asyncio.run(engine._retry_restore(0))
        assert "Restore re-send failed" in caplog.text

    def test_stop_reasons(self):
        engine, _ = self._engine()
        pending = engine._pending_restore
        engine._paused_by_door = True
        assert engine._restore_resend_stop_reason(pending) == "the door/window pause is recorded again"
        engine._paused_by_door = False
        with patch.object(engine, "_any_monitored_sensor_settled_open", return_value=True):
            assert engine._restore_resend_stop_reason(pending) == "a window is open again"
            # The occupant's own Resume with the window open is not stopped by the open window.
            resume = PendingRestore(target="heat", trigger="dashboard_resume", reason="resume", kind="dashboard_resume")
            assert engine._restore_resend_stop_reason(resume) is None
        engine._natural_vent_active = True
        assert engine._restore_resend_stop_reason(pending) == "a fan session owns the HVAC"


# ---------------------------------------------------------------------------
# _reschedule_grace_timer (restart restore)
# ---------------------------------------------------------------------------


class TestRescheduleGraceTimer:
    def test_manual_grace_rearmed_into_manual_slot(self):
        engine, _ = _make_engine()
        engine._last_resume_source = None
        cancel = MagicMock()
        with patch.object(_automation_mod, "_call_later", return_value=cancel) as call_later:
            engine._reschedule_grace_timer(120.0)
        assert engine._manual_grace_cancel is cancel
        assert call_later.call_args.args[1] == 120.0
        # The re-armed expiry runs the real expiry handler without a notification.
        with patch.object(engine, "_on_grace_expired") as expired:
            call_later.call_args.args[2](None)
        expired.assert_called_once_with("manual", int(engine._grace_duration_seconds), False)

    def test_automation_grace_rearmed_into_automation_slot(self):
        engine, _ = _make_engine()
        engine._last_resume_source = "automation"
        cancel = MagicMock()
        with patch.object(_automation_mod, "_call_later", return_value=cancel):
            engine._reschedule_grace_timer(30.0)
        assert engine._automation_grace_cancel is cancel


# ---------------------------------------------------------------------------
# _fan_start_runtime_refusal (Issue #1225)
# ---------------------------------------------------------------------------


class TestFanStartRuntimeRefusal:
    def _engine(self, fan_mode: str = FAN_MODE_WHOLE_HOUSE):
        engine, _ = _make_engine(config={CONF_FAN_MODE: fan_mode, CONF_FAN_ENTITY: "fan.whf"})
        engine._fan_readback_gave_up = False
        return engine

    def test_disabled(self):
        assert self._engine(FAN_MODE_DISABLED)._fan_start_runtime_refusal() == "fan disabled"

    def test_fan_override(self):
        engine = self._engine()
        engine._fan_override_active = True
        assert engine._fan_start_runtime_refusal() == "fan override active"

    def test_fan_off_hold(self):
        engine = self._engine()
        with patch.object(engine, "_fan_off_hold_active", return_value=True):
            assert engine._fan_start_runtime_refusal() == "occupant fan-off hold"

    def test_fan_press_settling(self):
        engine = self._engine()
        with patch.object(engine, "_occupant_fan_press_blocks_start", return_value=True):
            assert engine._fan_start_runtime_refusal() == "occupant fan press settling"

    def test_protected_thermostat_change(self):
        engine = self._engine()
        with patch.object(engine, "_protected_active_mode", return_value="cool"):
            assert engine._fan_start_runtime_refusal() == "occupant's thermostat change protected"

    def test_readback_gave_up(self):
        engine = self._engine()
        engine._fan_readback_gave_up = True
        assert engine._fan_start_runtime_refusal() == "fan switch ignored the on-command"

    def test_hvac_fan_restriction(self):
        engine = self._engine(FAN_MODE_HVAC)
        with patch.object(engine, "_hvac_fan_restriction_block_reason", return_value="heating"):
            assert engine._fan_start_runtime_refusal() == "heating"
        with patch.object(engine, "_hvac_fan_restriction_block_reason", return_value=""):
            assert engine._fan_start_runtime_refusal() is None

    def test_whole_house_fan_free_to_start(self):
        assert self._engine()._fan_start_runtime_refusal() is None


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


class TestParseForecastDt:
    def test_empty_is_none(self):
        assert _parse_forecast_dt(None) is None
        assert _parse_forecast_dt("") is None

    def test_valid_iso(self):
        assert _parse_forecast_dt("2026-07-15T14:00:00+00:00") == _NOW

    def test_parser_error_is_none(self):
        with patch.object(_automation_mod.dt_util, "parse_datetime", side_effect=ValueError("bad")):
            assert _parse_forecast_dt("garbage") is None


class TestMinutesSinceIso:
    def test_none_and_garbage(self):
        assert AutomationEngine._minutes_since_iso(None, now=_NOW) is None
        assert AutomationEngine._minutes_since_iso("not a time", now=_NOW) is None

    def test_naive_timestamp_and_now_are_treated_as_utc(self):
        naive_now = _NOW.replace(tzinfo=None)
        assert AutomationEngine._minutes_since_iso("2026-07-15T13:30:00", now=naive_now) == pytest.approx(30.0)


class TestParseGraceEnd:
    def test_unset_and_naive_and_bad(self):
        engine, _ = _make_engine()
        engine._grace_end_time = None
        assert engine._parse_grace_end() is None
        engine._grace_end_time = "2026-07-15T14:00:00"
        assert engine._parse_grace_end() == _NOW
        with patch.object(_automation_mod.dt_util, "parse_datetime", side_effect=ValueError("bad")):
            assert engine._parse_grace_end() is None


class TestFanRuntimeMinutes:
    def test_inactive_is_zero(self):
        engine, _ = _make_engine()
        engine._fan_active = False
        assert engine._get_fan_runtime_minutes() == 0.0

    def test_naive_on_since_counted_as_utc(self):
        engine, _ = _make_engine()
        engine._fan_active = True
        engine._fan_on_since = "2026-07-15T13:15:00"
        with patch(_DT_NOW, return_value=_NOW):
            assert engine._get_fan_runtime_minutes() == pytest.approx(45.0)

    def test_naive_now_and_non_datetime_now(self):
        engine, _ = _make_engine()
        engine._fan_active = True
        engine._fan_on_since = "2026-07-15T13:50:00+00:00"
        with patch(_DT_NOW, return_value=_NOW.replace(tzinfo=None)):
            assert engine._get_fan_runtime_minutes() == pytest.approx(10.0)
        with patch(_DT_NOW, return_value="not a datetime"):
            assert engine._get_fan_runtime_minutes() == 0.0

    def test_unparseable_on_since_is_zero(self):
        engine, _ = _make_engine()
        engine._fan_active = True
        engine._fan_on_since = "yesterday"
        assert engine._get_fan_runtime_minutes() == 0.0


class TestLastWhfCommandDirection:
    def test_no_command(self):
        engine, _ = _make_engine()
        engine._whf_last_command = None
        assert engine.last_whf_command_direction() is None

    def test_uncomparable_time_is_none(self):
        engine, _ = _make_engine()
        engine._whf_last_command = ("on", _NOW.replace(tzinfo=None), None)
        with patch(_DT_NOW, return_value=_NOW):
            assert engine.last_whf_command_direction() is None

    def test_newer_blower_command_owns_the_echo_window(self):
        engine, _ = _make_engine()
        engine._whf_last_command = ("on", _NOW - timedelta(seconds=10), None)
        engine._fan_command_time = _NOW - timedelta(seconds=2)
        with patch(_DT_NOW, return_value=_NOW):
            assert engine.last_whf_command_direction() is None

    def test_naive_blower_time_is_none(self):
        engine, _ = _make_engine()
        engine._whf_last_command = ("off", _NOW - timedelta(seconds=10), None)
        engine._fan_command_time = (_NOW - timedelta(seconds=2)).replace(tzinfo=None)
        with patch(_DT_NOW, return_value=_NOW):
            assert engine.last_whf_command_direction() is None

    def test_recent_whf_command_matches(self):
        engine, _ = _make_engine()
        engine._whf_last_command = ("off", _NOW - timedelta(seconds=10), None)
        engine._fan_command_time = _NOW - timedelta(seconds=10)
        with patch(_DT_NOW, return_value=_NOW):
            assert engine.last_whf_command_direction() == "off"


class TestWhfCommandReadback:
    """A whole-house-fan switch that ignored "on" is read back as False so the AC is not suppressed forever."""

    def _engine(self, fan_state: MagicMock | None):
        engine, _ = _make_engine(config={CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whf"})
        engine._fan_toggle_command_time = _NOW
        climate = engine.hass.states.get.return_value

        engine.hass.states.get = MagicMock(side_effect=lambda eid: fan_state if eid == "fan.whf" else climate)
        return engine

    def _fan(self, state: str, changed):
        s = MagicMock()
        s.state = state
        s.attributes = {}
        s.last_changed = changed
        return s

    def test_not_a_whf_mode(self):
        engine = self._engine(None)
        assert engine._whf_command_readback_on(FAN_MODE_HVAC) is None

    def test_no_command_or_override(self):
        engine = self._engine(None)
        engine._fan_toggle_command_time = None
        assert engine._whf_command_readback_on(FAN_MODE_WHOLE_HOUSE) is None

    def test_no_fan_entity(self):
        engine = self._engine(None)
        engine.config[CONF_FAN_ENTITY] = ""
        assert engine._whf_command_readback_on(FAN_MODE_WHOLE_HOUSE) is None

    def test_off_since_before_command_is_false(self):
        engine = self._engine(self._fan("off", _NOW - timedelta(minutes=5)))
        assert engine._whf_command_readback_on(FAN_MODE_WHOLE_HOUSE) is False

    def test_off_after_command_is_not_judged(self):
        engine = self._engine(self._fan("off", _NOW + timedelta(minutes=1)))
        assert engine._whf_command_readback_on(FAN_MODE_WHOLE_HOUSE) is None

    def test_naive_last_changed_is_not_judged(self):
        engine = self._engine(self._fan("off", (_NOW - timedelta(minutes=5)).replace(tzinfo=None)))
        assert engine._whf_command_readback_on(FAN_MODE_WHOLE_HOUSE) is None


# ---------------------------------------------------------------------------
# Observe-only whole-house-fan notices (Issues #1144, #1288)
# ---------------------------------------------------------------------------


class TestObserveOnlyNotices:
    def _engine(self):
        engine, events = _make_engine(config={CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, CONF_FAN_ENTITY: "fan.whf"})
        engine.dry_run = True
        engine._whf_observe_notified = False
        engine._whf_observe_rule2_notified = False
        return engine, events

    def test_rule2_notice_failure_rearms_the_episode(self, caplog):
        engine, events = self._engine()
        engine._notify = AsyncMock(side_effect=RuntimeError("notify down"))
        decision = WhfSafetyDecision(WhfSafetyAction.STOP_MUTEX_FAN, "fan and AC together", backstop=True)
        asyncio.run(engine._observe_only_rule2_notice(decision, trigger="tick"))
        assert engine._whf_observe_rule2_notified is False
        assert "rule-2 notification failed" in caplog.text
        assert events == []

    def test_rule2_notice_sent_once(self):
        engine, events = self._engine()
        engine._notify = AsyncMock()
        engine._emit_event_callback = None
        decision = WhfSafetyDecision(WhfSafetyAction.STOP_MUTEX_FAN, "fan and AC together", backstop=True)
        asyncio.run(engine._observe_only_rule2_notice(decision, trigger="tick"))
        asyncio.run(engine._observe_only_rule2_notice(decision, trigger="tick"))
        assert engine._notify.await_count == 1
        assert engine._whf_observe_rule2_notified is True

    def test_sealed_notice_failure_rearms_the_episode(self, caplog):
        engine, events = self._engine()
        engine._notify = AsyncMock(side_effect=RuntimeError("notify down"))
        decision = WhfSafetyDecision(WhfSafetyAction.STOP_SEALED, "sealed", sealed=True)
        with patch.object(_automation_mod, "decide_whf_safety", return_value=decision):
            asyncio.run(engine._observe_only_whf_notice("tick"))
        assert engine._whf_observe_notified is False
        assert "WHF observe-only notification failed" in caplog.text
        assert [n for n, _ in events if n == "whf_sealed_observe_notified"] == []

    def test_rule2_state_inputs_tolerate_naive_timestamps(self):
        engine, _ = _make_engine()
        climate = MagicMock()
        climate.last_changed = datetime(2026, 7, 15, 13, 0, 0)  # naive vs aware now
        climate.attributes = {"hvac_action": "cooling"}
        engine._last_commanded_hvac_mode = "off"
        engine._last_commanded_hvac_time = datetime(2026, 7, 15, 13, 59, 0)
        snap = engine._whf_safety_snapshot()
        with patch(_DT_NOW, return_value=_NOW):
            inputs = engine._whf_rule2_state_inputs(climate, snap)
        assert inputs["thermostat_mode_for_s"] is None
        assert inputs["own_hvac_off_for_s"] is None


# ---------------------------------------------------------------------------
# Remote-offset actuation feedback, re-enable reconcile, open-window hold
# ---------------------------------------------------------------------------


class TestRemoteOffsetActuation:
    def _engine(self):
        engine, _ = _make_engine()
        engine._remote_raise_pending_feedback = True
        engine._remote_raise_at = _NOW
        engine._remote_target_mode = "heat"
        engine._remote_startup_latency_s = 120.0
        return engine

    def test_other_direction_is_ignored(self):
        engine = self._engine()
        engine.note_remote_offset_actuation("cool", _NOW + timedelta(seconds=60))
        assert engine._remote_raise_pending_feedback is True

    def test_start_before_raise_is_ignored(self):
        engine = self._engine()
        engine.note_remote_offset_actuation("heat", _NOW - timedelta(seconds=5))
        assert engine._remote_raise_pending_feedback is True

    def test_start_long_after_raise_ends_feedback_without_learning(self):
        engine = self._engine()
        engine.note_remote_offset_actuation("heat", _NOW + timedelta(hours=3))
        assert engine._remote_raise_pending_feedback is False
        assert engine._remote_startup_latency_s == 120.0


class TestReconcileAfterEnable:
    def test_switched_off_again_does_nothing(self):
        engine, _ = _make_engine()
        engine.dry_run = True
        engine._sync_paused_by_door_with_live_sensors = AsyncMock()
        asyncio.run(engine.async_reconcile_after_automation_enabled())
        engine._sync_paused_by_door_with_live_sensors.assert_not_awaited()

    def test_reconcile_error_is_logged(self, caplog):
        engine, _ = _make_engine()
        engine._sync_paused_by_door_with_live_sensors = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(engine.async_reconcile_after_automation_enabled())
        assert "Re-enable door/window reconcile failed" in caplog.text


class TestHoldThermostatOffForOpenWindow:
    def test_occupant_change_protected_is_left_alone(self):
        engine, _ = _make_engine(thermostat="cool")
        engine._manual_override_active = True
        engine._set_hvac_mode = AsyncMock()
        assert asyncio.run(engine._hold_thermostat_off_for_open_window("away ceiling")) is False
        engine._set_hvac_mode.assert_not_awaited()

    def test_observe_only_returns_none(self):
        engine, _ = _make_engine(thermostat="cool")
        engine._set_hvac_mode = AsyncMock(return_value=HvacCommandResult.DRY_RUN)
        assert asyncio.run(engine._hold_thermostat_off_for_open_window("away ceiling")) is None


# ---------------------------------------------------------------------------
# cleanup() cancels every outstanding timer
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cancels_tracked_timers_and_listeners(self):
        engine, _ = _make_engine()
        slots = (
            "_revisit_cancel",
            "_deferred_reconcile_cancel",
            "_override_confirm_cancel",
            "_setpoint_retry_cancel",
            "_mode_align_recheck_cancel",
            "_fan_on_verify_cancel",
            "_nat_vent_closed_exit_retry_cancel",
            "_open_window_off_retry_cancel",
            "_fan_off_verify_cancel",
        )
        cancels = {slot: MagicMock() for slot in slots}
        for slot, cancel in cancels.items():
            setattr(engine, slot, cancel)
        unsub = MagicMock()
        engine._active_listeners = [unsub]
        engine.cleanup()
        for slot, cancel in cancels.items():
            cancel.assert_called_once()
            assert getattr(engine, slot) is None, slot
        unsub.assert_called_once()
        assert engine._active_listeners == []
        assert engine._shut_down is True


# ---------------------------------------------------------------------------
# Comfort-hold episode log (Issue #1077)
# ---------------------------------------------------------------------------


class TestComfortHoldEpisode:
    def _decision(self, family: str, depth: float | None = 0.5, zone: str = "breach"):
        d = MagicMock()
        d.hold_candidate_family = family
        d.hold_depth_f = depth
        d.hold_zone = zone
        d.hold_tolerance_f = 1.0
        return d

    def test_other_candidate_closes_previous_episode(self):
        engine, _ = _make_engine()
        engine._comfort_holds = None
        engine._comfort_hold_episode = None
        assert engine._track_comfort_hold_episode(self._decision("cooling"), _NOW) is False
        assert engine._comfort_holds[0]["candidate_family"] == "cooling"
        later = _NOW + timedelta(minutes=5)
        assert engine._track_comfort_hold_episode(self._decision("heating"), later) is False
        assert engine._comfort_holds[0]["end_reason"] == "lapsed"
        assert engine._comfort_holds[-1]["candidate_family"] == "heating"

    def test_bad_end_timestamp_starts_a_new_episode(self):
        engine, _ = _make_engine()
        engine._comfort_hold_episode = None
        engine._comfort_holds = [{"candidate_family": "cooling", "end_ts": "not-a-time"}]
        assert engine._track_comfort_hold_episode(self._decision("cooling"), _NOW) is False
        assert len(engine._comfort_holds) == 2

    def test_extending_keeps_deepest_reading(self):
        engine, _ = _make_engine()
        engine._comfort_holds = []
        engine._comfort_hold_episode = None
        engine._track_comfort_hold_episode(self._decision("cooling", depth=0.5), _NOW)
        assert engine._track_comfort_hold_episode(self._decision("cooling", depth=None, zone="margin"), _NOW) is True
        assert engine._comfort_hold_episode["max_depth_f"] == 0.5
        assert engine._comfort_hold_episode["zone"] == "breach"


# ---------------------------------------------------------------------------
# Nat-vent away-ceiling exit from the per-tick temperature check (Issues #757, #931, #1304)
# ---------------------------------------------------------------------------


class TestNatVentTempCheckAwayCeiling:
    """Away and the house reaches the comfort ceiling: the free-cooling fan stops so the away setback takes over."""

    def _engine(self):
        engine, events = _make_engine(config={CONF_FAN_MODE: FAN_MODE_HVAC, "natural_vent_delta": 3.0})
        engine._natural_vent_active = True
        engine._occupancy_mode = "away"
        engine._confirm_nat_vent_exit = MagicMock(return_value=True)
        return engine, events

    def _run(self, engine, result):
        engine._deactivate_fan = AsyncMock(return_value=result)
        with (
            patch.object(engine, "_any_monitored_sensor_settled_open", return_value=False),
            patch.object(engine, "_any_monitored_sensor_open", return_value=False),
            patch(_DT_NOW, return_value=_NOW),
        ):
            asyncio.run(engine.nat_vent_temperature_check(77.0, outdoor=65.0))

    def test_fan_stopped_ends_session_and_reports(self):
        engine, events = self._engine()
        self._run(engine, FanCommandResult.EXECUTED)
        engine._deactivate_fan.assert_awaited_once()
        assert engine._natural_vent_active is False
        assert engine._nat_vent_outdoor_exit_time == _NOW
        rows = [p for n, p in events if n == "nat_vent_away_ceiling_exit"]
        assert len(rows) == 1 and rows[0]["source"] == "temp_check" and "fan_mode_change" not in rows[0]

    def test_observe_only_keeps_session_and_reports_nothing(self):
        engine, events = self._engine()
        self._run(engine, FanCommandResult.DRY_RUN)
        assert engine._natural_vent_active is True
        assert [n for n, _ in events if n == "nat_vent_away_ceiling_exit"] == []

    def test_rate_limited_stop_carries_deferred_marker(self):
        engine, events = self._engine()
        with patch.object(engine, "_deferred_fan_mode_change_marker", return_value={"deferred": True}):
            self._run(engine, FanCommandResult.RATE_LIMITED_NEW)
        rows = [p for n, p in events if n == "nat_vent_away_ceiling_exit"]
        assert rows and rows[0]["fan_mode_change"] == {"deferred": True}

    def test_repeat_rate_limited_stop_writes_no_duplicate_row(self):
        engine, events = self._engine()
        self._run(engine, FanCommandResult.RATE_LIMITED_DUP)
        assert [n for n, _ in events if n == "nat_vent_away_ceiling_exit"] == []


# ---------------------------------------------------------------------------
# Orphaned thermostat blower release (Issue #1103)
# ---------------------------------------------------------------------------


class TestReleaseOrphanedHvacFan:
    """Fan control was switched off while Climate Advisor ran the blower: it hands the blower back exactly once."""

    def _engine(self, *, fan_mode: str | None = "on", fan_modes: list[str] | None = None, present: bool = True):
        attrs = {"fan_mode": fan_mode, "fan_modes": fan_modes if fan_modes is not None else ["auto", "on"]}
        engine, events = _make_engine(attributes=attrs, thermostat="cool" if present else None)
        engine._call_service_with_context = AsyncMock()
        return engine, events

    def test_missing_thermostat_sends_nothing(self):
        engine, _ = self._engine(present=False)
        asyncio.run(engine._release_orphaned_hvac_fan())
        engine._call_service_with_context.assert_not_awaited()

    def test_blower_already_auto_sends_nothing(self):
        engine, _ = self._engine(fan_mode="auto")
        asyncio.run(engine._release_orphaned_hvac_fan())
        engine._call_service_with_context.assert_not_awaited()

    def test_no_off_command_available_sends_nothing(self):
        engine, _ = self._engine(fan_modes=["on"])
        with patch.object(_automation_mod, "resolve_fan_mode_command", return_value=None):
            asyncio.run(engine._release_orphaned_hvac_fan())
        engine._call_service_with_context.assert_not_awaited()

    def test_observe_only_without_activity_callback_sends_nothing(self):
        engine, _ = self._engine()
        engine.dry_run = True
        engine._emit_event_callback = None
        asyncio.run(engine._release_orphaned_hvac_fan())
        engine._call_service_with_context.assert_not_awaited()

    def test_release_sends_set_fan_mode_and_reports(self):
        engine, events = self._engine()
        asyncio.run(engine._release_orphaned_hvac_fan())
        call = engine._call_service_with_context.await_args
        assert call.args[:2] == ("climate", "set_fan_mode")
        assert call.args[2]["fan_mode"] == "auto"
        assert ("fan_orphan_released", {"old_fan_mode": "on", "command_sent": "auto"}) in events


# ---------------------------------------------------------------------------
# Fan card cancel while a thermostat change is active (Issue #1205)
# ---------------------------------------------------------------------------


class TestCancelFanSideOnly:
    def test_nothing_to_cancel(self):
        engine, _ = _make_engine()
        engine._grace_active = False
        with patch.object(engine, "_fan_off_hold_active", return_value=False):
            assert engine._cancel_fan_side_only("card") is False

    def test_fan_grace_cancelled_and_refresh_requested(self):
        """The fan's own grace ends; the thermostat change is left to its own confirmation."""
        engine, _ = _make_engine()
        engine._grace_active = True
        engine._last_grace_trigger = "fan_off"
        engine._cancel_grace_intentionally = MagicMock()
        engine._resolve_override_grace_fsm_state = MagicMock()
        engine.clear_fan_override = MagicMock()
        engine._post_grace_fan_check_callback = MagicMock()
        engine._request_refresh_callback = MagicMock()
        with patch.object(engine, "_fan_off_hold_active", return_value=True), patch.object(engine, "_end_fan_off_hold"):
            assert engine._cancel_fan_side_only("card") is True
        engine._cancel_grace_intentionally.assert_called_once_with("card", ends_fan_off_hold=True)
        engine.clear_fan_override.assert_called_once_with(reason="card")
        engine._post_grace_fan_check_callback.assert_called_once()
        engine._request_refresh_callback.assert_called_once()
        assert engine._last_grace_trigger is None


# ---------------------------------------------------------------------------
# Fan manual-origin label marker (Issues #829, #1205)
# ---------------------------------------------------------------------------


class TestFanManualOriginMarker:
    def test_no_live_run_clears_a_stale_marker(self):
        engine, _ = _make_engine()
        engine._fan_on_since = None
        engine._fan_manual_origin_unclosed = True
        engine._fan_manual_origin_run = "old"
        engine._fan_manual_origin_kind = "takeover"
        assert engine._arm_fan_manual_origin("site", "why") is False
        assert engine._fan_manual_origin_unclosed is False
        assert engine._fan_manual_origin_run is None
        assert engine._fan_manual_origin_kind is None

    def test_rearm_same_run_takes_newest_label_and_start(self):
        engine, _ = _make_engine()
        engine._fan_on_since = "2026-07-15T13:00:00+00:00"
        assert engine._arm_fan_manual_origin("a", "why") is True
        assert engine._arm_fan_manual_origin("b", "why", kind="restart", started="13:30") is True
        assert engine._fan_manual_origin_kind == "restart"
        assert engine._fan_manual_origin_site == "b"
        assert engine._fan_manual_origin_started == "13:30"

    def test_pending_needs_a_recorded_deferral_and_is_idempotent(self):
        engine, _ = _make_engine()
        engine._fan_manual_origin_pending = None
        engine._fan_rate_limited_until = None
        engine._raise_fan_manual_origin_pending("site", "why")
        assert engine._fan_manual_origin_pending is None
        engine._fan_rate_limited_until = _NOW
        engine._fan_start_seq = 3
        engine._raise_fan_manual_origin_pending("site", "why")
        assert engine._fan_manual_origin_pending == (_NOW, 3)
        engine._fan_start_seq = 4
        engine._raise_fan_manual_origin_pending("site", "again")
        assert engine._fan_manual_origin_pending == (_NOW, 3)
