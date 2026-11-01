"""Issues #1195 and #1196: the blower's 5-minute toggle floor, exit side and entry side.

Occupant framing.

* #1195 (hvac_fan): the windows open, Climate Advisor runs the blower for free cooling, and the windows
  close within five minutes. The blower-off is held back by the toggle floor, the session is (correctly,
  #931) kept -- and nothing ever finished the exit. The blower kept running overnight with Status
  "nat-vent" and every window shut. Fix: the closed-window exit schedules its own retry for when the
  floor ends.
* #1196 (hvac_fan + hvac_fan_restrict_mode): a refused blower start (restriction) counted as a toggle, so
  the very next start attempt looked rate-limited, which armed the "manual fan-on not yet closed" marker.
  Later the occupant turned CA's blower off; the stale marker suppressed the 30-minute fan-off grace and
  CA turned the blower back on five minutes later. Fix: a toggle is stamped only for a command that
  went out (mirror of the #1144 deactivate-side fix), and the marker is armed only for a start that is
  really pending.

REAL ``AutomationEngine`` + the state-following fake thermostat from ``tests.test_restore_resolved_band``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    CONF_HVAC_FAN_RESTRICT_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
    HVAC_FAN_RESTRICT_HEAT,
)
from tests.test_restore_resolved_band import _classification, _make_engine, _run

_AUTOMATION = "custom_components.climate_advisor.automation"
_DAY = datetime(2026, 10, 28, 22, 0, 0, tzinfo=UTC)
_REASON = "outdoor warmed past the threshold"
_SENSOR = "binary_sensor.front_window"


def _blower_engine(*, mode: str = "off", sensor_open: bool = False, **cfg) -> AutomationEngine:
    """hvac_fan install on a mild evening (outdoor 65 < indoor 72)."""
    engine = _make_engine(indoor=72.0, mode=mode, **{CONF_FAN_MODE: FAN_MODE_HVAC, **cfg})
    engine._test_state.attributes["fan_modes"] = ["auto", "on"]
    engine._last_outdoor_temp = 65.0
    engine._hourly_forecast_temps = []
    engine._current_classification = _classification("mild")
    engine._sensor_check_callback = lambda: sensor_open
    engine._sensor_debounce_pending_callback = lambda: False
    return engine


def _fan_modes_sent(engine: AutomationEngine) -> list[str]:
    return [
        c.args[2]["fan_mode"] for c in engine.hass.services.async_call.call_args_list if c.args[1] == "set_fan_mode"
    ]


# ---------------------------------------------------------------------------
# #1195: the closed-window exit deferred by the floor finishes when the floor ends
# ---------------------------------------------------------------------------


def _ventilating_blower(*, sensor_open: bool = False) -> AutomationEngine:
    engine = _blower_engine(sensor_open=sensor_open)
    engine._natural_vent_active = True
    engine._fan_active = True
    engine._fan_on_since = (_DAY - timedelta(seconds=30)).isoformat()
    engine._fan_toggle_command_time = _DAY - timedelta(seconds=30)  # blower started 30 s ago
    return engine


def _close_windows(engine: AutomationEngine, timers: list):
    """The debounced all-closed handler runs (the windows closed 30 s after the blower started)."""

    def _capture(_hass, delay, fn):
        timers.append((delay, fn))
        return lambda: None

    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.async_call_later", side_effect=_capture),
        patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
    ):
        _run(engine.handle_all_doors_windows_closed())


def _fire_exit_retry(engine: AutomationEngine, timers: list, *, at: datetime) -> None:
    """Fire the scheduled exit retry the way the clock would, then run the task it spawns."""
    tasks: list = []
    engine.hass.async_create_task = MagicMock(side_effect=tasks.append)
    delay, fn = timers[-1]
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=at),
        patch(f"{_AUTOMATION}.async_call_later", return_value=lambda: None),
    ):
        fn(None)
        for coro in tasks:
            asyncio.run(coro)


def _retry_timers(timers: list) -> list:
    return [t for t in timers if t[0] > 200]  # the floor-sized timer, not the 30 s verify / 5 min grace timers


class TestDeferredClosedExitIsRetried:
    def test_windows_close_inside_the_floor_keeps_the_session_and_schedules_the_retry(self):
        engine = _ventilating_blower()
        timers: list = []
        _close_windows(engine, timers)
        assert engine._natural_vent_active is True, "blower-off deferred by the floor keeps the session (#931)"
        assert engine._fan_active is True
        floor_timers = [t for t in timers if 200 < t[0] < 299]
        assert floor_timers, f"nothing was scheduled to finish the exit when the floor ends: {timers}"
        assert 260 <= floor_timers[0][0] <= 298, "the retry is due when the 5-minute floor ends (270 s left + slack)"

    def test_retry_after_the_floor_stops_the_blower_ends_the_session_and_starts_the_grace(self):
        engine = _ventilating_blower()
        timers: list = []
        _close_windows(engine, timers)
        floor_timers = [t for t in timers if 200 < t[0] < 299]
        assert floor_timers
        _fire_exit_retry(engine, floor_timers, at=_DAY + timedelta(seconds=floor_timers[0][0]))
        assert engine._natural_vent_active is False, "the occupant's windows are shut: no more nat-vent"
        assert engine._fan_active is False
        assert "auto" in _fan_modes_sent(engine), "the blower went back to auto"
        assert engine._grace_active is True, "the usual nat_vent_exit_resume grace starts once the exit completes"

    def test_retry_does_nothing_when_a_window_reopened_meanwhile(self):
        engine = _ventilating_blower()
        timers: list = []
        _close_windows(engine, timers)
        floor_timers = [t for t in timers if 200 < t[0] < 299]
        engine._sensor_check_callback = lambda: True  # reopened: the session is legitimate again
        _fire_exit_retry(engine, floor_timers, at=_DAY + timedelta(seconds=floor_timers[0][0]))
        assert engine._natural_vent_active is True
        assert engine._fan_active is True
        assert _fan_modes_sent(engine) == []

    def test_retry_is_a_noop_when_the_session_already_ended(self):
        engine = _ventilating_blower()
        timers: list = []
        _close_windows(engine, timers)
        floor_timers = [t for t in timers if 200 < t[0] < 299]
        engine._natural_vent_active = False
        engine._fan_active = False
        _fire_exit_retry(engine, floor_timers, at=_DAY + timedelta(seconds=floor_timers[0][0]))
        assert _fan_modes_sent(engine) == []

    def test_no_retry_scheduled_when_the_exit_ran_immediately(self):
        """Positive control: floor already over -> the blower stops at once and no retry timer is needed."""
        engine = _ventilating_blower()
        engine._fan_toggle_command_time = _DAY - timedelta(minutes=20)
        timers: list = []
        _close_windows(engine, timers)
        assert engine._natural_vent_active is False
        assert not [t for t in timers if 200 < t[0] < 299]

    def test_cleanup_cancels_the_pending_retry(self):
        engine = _ventilating_blower()
        cancelled = []

        def _capture(_hass, _delay, _fn):
            return lambda: cancelled.append(1)

        with (
            patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
            patch(f"{_AUTOMATION}.async_call_later", side_effect=_capture),
        ):
            _run(engine.handle_all_doors_windows_closed())
            engine.cleanup()
        assert cancelled, "cleanup() must cancel the retry timer (no ghost timer on a dead engine)"


# ---------------------------------------------------------------------------
# #1196: a refused start is not a toggle
# ---------------------------------------------------------------------------


def _restricted_engine(**kw) -> AutomationEngine:
    """Thermostat in cool, blower restricted to heat: every blower start is refused."""
    return _blower_engine(mode="cool", **{CONF_HVAC_FAN_RESTRICT_MODE: HVAC_FAN_RESTRICT_HEAT}, **kw)


def _activate(engine: AutomationEngine, *, at: datetime = _DAY) -> FanCommandResult:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=at):
        return _run(engine._activate_fan(reason="nat-vent test"))


class TestRefusedStartIsNotAToggle:
    def test_restricted_start_is_suppressed_and_stamps_no_toggle(self):
        engine = _restricted_engine()
        assert engine._fan_toggle_command_time is None
        assert _activate(engine) is FanCommandResult.SUPPRESSED
        assert engine._fan_toggle_command_time is None, "a refused start sent nothing: it is not a toggle"
        assert engine._fan_command_time is None
        assert _fan_modes_sent(engine) == []

    def test_second_attempt_is_still_suppressed_not_rate_limited(self):
        engine = _restricted_engine()
        assert _activate(engine) is FanCommandResult.SUPPRESSED
        assert _activate(engine, at=_DAY + timedelta(seconds=5)) is FanCommandResult.SUPPRESSED
        assert engine._fan_rate_limited_until is None

    def test_restricted_start_inside_a_real_floor_is_suppressed_not_deferred(self):
        """A real toggle 10 s ago would defer the start -- but the restriction refuses it anyway, so no
        'deferred activation' is recorded to be applied later."""
        engine = _restricted_engine()
        real_toggle = _DAY - timedelta(seconds=10)
        engine._fan_toggle_command_time = real_toggle
        assert _activate(engine) is FanCommandResult.SUPPRESSED
        assert engine._fan_rate_limited_until is None
        assert engine._fan_toggle_command_time == real_toggle, "the earlier real toggle's stamp is untouched"

    def test_blower_without_an_on_mode_stamps_no_toggle(self):
        engine = _blower_engine()
        engine._test_state.attributes["fan_modes"] = ["auto"]  # nothing resolves to "on"
        assert _activate(engine) is FanCommandResult.SUPPRESSED
        assert engine._fan_toggle_command_time is None
        assert engine._fan_command_time is None

    def test_positive_control_a_sent_start_stamps_the_toggle(self):
        engine = _blower_engine()
        assert _activate(engine) is FanCommandResult.EXECUTED
        assert engine._fan_toggle_command_time == _DAY
        assert _fan_modes_sent(engine) == ["on"]
        # ...and the floor then genuinely holds an immediate stop
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY + timedelta(seconds=10)):
            assert _run(engine._deactivate_fan(reason="test")) is FanCommandResult.RATE_LIMITED_NEW


def _paused_restricted_engine(**kw) -> AutomationEngine:
    engine = _restricted_engine(**kw)
    engine._sensor_check_callback = lambda: True
    engine._paused_by_door = True
    engine._paused_with_hvac_already_off = False
    engine._pre_pause_mode = "cool"
    engine._paused_entity = _SENSOR
    engine._paused_since = _DAY - timedelta(minutes=20)
    return engine


class TestStaleMarkerNeverArmedForARefusedStart:
    def test_refresh_then_grace_expiry_with_a_restricted_blower_arms_no_marker(self):
        """The #1196 sequence: the refresh pass is refused, then the grace-expiry re-check in the same
        instant was 'rate-limited' by the refused attempt and armed the marker for a start that was never
        pending."""
        engine = _paused_restricted_engine()
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine.check_natural_vent_conditions())
            _run(engine._re_pause_for_open_sensor())
        assert engine._fan_active is False
        assert engine._fan_manual_origin_unclosed is False, "no CA blower run is pending: nothing to mark"
        assert engine._natural_vent_active is False

    def test_a_really_deferred_start_still_arms_the_marker(self):
        """Positive control (#829 stays intact): an unrestricted blower start deferred by a real floor IS
        CA's own pending run, so its eventual off must not look like a fresh manual override."""
        engine = _paused_restricted_engine()
        engine.config[CONF_HVAC_FAN_RESTRICT_MODE] = "both"
        engine._fan_toggle_command_time = _DAY - timedelta(seconds=10)
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine._re_pause_for_open_sensor())
        assert engine._fan_rate_limited_until is not None, "precondition: the start is genuinely deferred"
        assert engine._fan_manual_origin_unclosed is True

    def test_marker_is_not_armed_when_nothing_is_pending(self):
        """Direct guard: even if the activation reports RATE_LIMITED, no marker without a pending start."""
        engine = _paused_restricted_engine()
        engine.config[CONF_HVAC_FAN_RESTRICT_MODE] = "both"
        engine._fan_toggle_command_time = _DAY - timedelta(seconds=10)

        async def _limited(**_kw):
            return FanCommandResult.RATE_LIMITED_DUP  # reported, but no deferred activation recorded

        engine._activate_fan = _limited
        engine._fan_rate_limited_until = None
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine._re_pause_for_open_sensor())
        assert engine._fan_manual_origin_unclosed is False


# ---------------------------------------------------------------------------
# Review follow-ups: the retry is tied to its session and its exit's own reason/event
# ---------------------------------------------------------------------------


def _scheduled_retry(engine: AutomationEngine):
    """Close the windows inside the floor; return (timers, cancel_calls) with the retry armed."""
    timers: list = []
    cancels: list = []

    def _capture(_hass, delay, fn):
        timers.append((delay, fn))
        return lambda: cancels.append(delay)

    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.async_call_later", side_effect=_capture),
        patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
    ):
        _run(engine.handle_all_doors_windows_closed())
    assert engine._nat_vent_closed_exit_retry_cancel is not None
    return timers, cancels


class TestRetryIsTiedToItsSession:
    def test_session_end_by_another_route_cancels_the_retry(self):
        engine = _ventilating_blower()
        _timers, cancels = _scheduled_retry(engine)

        async def _other_route_ends_session():
            async with engine._decision_pass("other_exit"):
                engine._natural_vent_active = False

        _run(_other_route_ends_session())
        assert cancels, "a session ended elsewhere must cancel the pending closed-window retry"
        assert engine._nat_vent_closed_exit_retry_cancel is None

    def test_a_stale_retry_never_ends_a_newer_session(self):
        """Occupant story: a NEW blower session (sensors closed, #134/#984) must not be stopped by the old
        session's retry even if its timer somehow still fires."""
        engine = _ventilating_blower()
        timers, _cancels = _scheduled_retry(engine)
        floor_timer = [t for t in timers if 200 < t[0] < 299]

        async def _end_then_new_session():
            async with engine._decision_pass("end"):
                engine._natural_vent_active = False
            async with engine._decision_pass("start"):
                engine._natural_vent_active = True

        _run(_end_then_new_session())
        engine._fan_toggle_command_time = _DAY - timedelta(minutes=20)  # floor over: a stale exit WOULD run
        engine._nat_vent_closed_exit_retry_cancel = None
        _fire_exit_retry(engine, floor_timer, at=_DAY + timedelta(seconds=floor_timer[0][0]))
        assert engine._natural_vent_active is True
        assert engine._fan_active is True
        assert _fan_modes_sent(engine) == []

    def test_latched_engine_retry_does_nothing(self):
        engine = _ventilating_blower()
        timers, _ = _scheduled_retry(engine)
        floor_timer = [t for t in timers if 200 < t[0] < 299]
        engine._ha_stopping = True
        engine._fan_toggle_command_time = _DAY - timedelta(minutes=20)
        _fire_exit_retry(engine, floor_timer, at=_DAY + timedelta(seconds=floor_timer[0][0]))
        assert engine._natural_vent_active is True
        assert _fan_modes_sent(engine) == []


class TestRetryCarriesTheOriginalExit:
    def test_non_door_exit_retry_reports_its_own_reason_and_event(self, caplog):
        engine = _ventilating_blower()
        events: list = []
        engine._emit_event_callback = lambda etype, payload: events.append((etype, dict(payload)))
        timers: list = []

        def _capture(_hass, delay, fn):
            timers.append((delay, fn))
            return lambda: None

        with (
            patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
            patch(f"{_AUTOMATION}.async_call_later", side_effect=_capture),
            patch(f"{_AUTOMATION}.callback", side_effect=lambda fn: fn),
        ):
            result = _run(
                engine._exit_nat_vent(
                    reason="outdoor temperature rose above indoor",
                    event_type="nat_vent_outdoor_rise_exit",
                    event_payload={"reason": "outdoor rose"},
                )
            )
        assert result is FanCommandResult.RATE_LIMITED_NEW
        assert [e[0] for e in events] == ["nat_vent_outdoor_rise_exit"]
        assert "deferred" in events[0][1]["fan_mode_change"]
        floor_timer = [t for t in timers if 200 < t[0] < 299]
        assert floor_timer
        caplog.clear()
        with caplog.at_level("INFO"):
            _fire_exit_retry(engine, floor_timer, at=_DAY + timedelta(seconds=floor_timer[0][0]))
        assert engine._natural_vent_active is False
        exits = [e for e in events if e[0] == "nat_vent_outdoor_rise_exit"]
        assert len(exits) == 2, "the completion event is emitted"
        assert "deferred" not in exits[1][1].get("fan_mode_change", "")
        text = caplog.text
        assert "outdoor temperature rose above indoor" in text
        assert "door/window closed" not in text, "a non-door exit must not be logged as a door/window close"


class TestWholeHouseFanStampOnlyForRealCommand:
    def test_whf_start_with_no_control_entity_stamps_no_toggle(self):
        engine = _blower_engine()
        engine.config[CONF_FAN_MODE] = FAN_MODE_WHOLE_HOUSE
        engine._sensor_check_callback = lambda: True  # open house: the sealed-house gate allows the start
        assert not engine.config.get(CONF_FAN_ENTITY)
        assert _activate(engine) is FanCommandResult.EXECUTED
        assert engine._fan_active is True, "the session bookkeeping is unchanged"
        assert engine._fan_toggle_command_time is None, "no command went out, so no toggle happened"
        assert engine._fan_command_time is None, "and no echo window opens for a real fan switch (#1144)"

    def test_failed_activation_rolls_back_both_stamps(self):
        engine = _blower_engine()
        engine.hass.services.async_call.side_effect = RuntimeError("thermostat unreachable")
        with pytest.raises(RuntimeError):
            _activate(engine)
        assert engine._fan_toggle_command_time is None
        assert engine._fan_command_time is None


class TestEarlyGateClearsStaleDeferral:
    def test_restricted_start_clears_an_earlier_activate_deferral(self):
        engine = _restricted_engine()
        engine._fan_rate_limited_until = _DAY + timedelta(seconds=100)
        engine._fan_rate_limited_direction = "activate"
        assert _activate(engine) is FanCommandResult.SUPPRESSED
        assert engine._fan_rate_limited_until is None
        assert engine._fan_rate_limited_direction is None


class TestRetryLatchAndErrors:
    def test_latched_while_waiting_for_the_lock_does_nothing(self):
        engine = _ventilating_blower()
        engine._fan_toggle_command_time = _DAY - timedelta(minutes=20)  # floor over: an un-latched retry WOULD stop it

        async def _scenario():
            await engine._decision_lock.acquire()
            task = asyncio.ensure_future(engine._retry_deferred_closed_exit("test exit", None, None, 0))
            await asyncio.sleep(0)  # the retry passed its outer check and now waits on the lock
            engine._ha_stopping = True
            engine._decision_lock.release()
            await task

        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            asyncio.run(_scenario())
        assert engine._natural_vent_active is True
        assert _fan_modes_sent(engine) == []

    def test_exception_in_the_retry_is_caught_and_logged(self, caplog):
        engine = _ventilating_blower()
        engine._exit_nat_vent = AsyncMock(side_effect=RuntimeError("boom"))
        with caplog.at_level("WARNING"):
            _run(engine._retry_deferred_closed_exit("test exit", None, None, engine._nat_vent_session_seq))
        assert "retry failed" in caplog.text

    def test_reschedule_without_an_event_keeps_the_first_exits_event_and_adds_the_suffix_once(self):
        engine = _ventilating_blower()
        engine._fan_rate_limited_until = _DAY + timedelta(seconds=100)
        with (
            patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
            patch(f"{_AUTOMATION}.async_call_later", return_value=lambda: None),
        ):
            engine._schedule_deferred_closed_exit_retry("first", "nat_vent_outdoor_rise_exit", {"a": 1})
            engine._schedule_deferred_closed_exit_retry("second", None, None)
        assert engine._nat_vent_closed_exit_pending == ("nat_vent_outdoor_rise_exit", {"a": 1})
        engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.EXECUTED)
        _run(engine._retry_deferred_closed_exit("x (deferred retry)", None, None, engine._nat_vent_session_seq))
        assert engine._exit_nat_vent.call_args.kwargs["reason"] == "x (deferred retry)"
