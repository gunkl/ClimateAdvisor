"""Engine-level tests for the WHF safety enforcer (Issue #1144, phase 1).

Occupant story: the whole-house fan is switched on by HomeKit, a wall switch or an RF remote while every door
and window is closed, or while the heat is on. It must be off within 30 s; the occupant's newest action wins.

The pure decision has its own tests (test_whf_safety_decision.py); these drive the real engine shell through
``enforce_whf_safety`` with a ground-truth snapshot, and assert what the occupant gets: the ``fan.turn_off`` that
goes out, the override that ends, the alert that is sent. No test re-implements the decision.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MIN_RUNTIME_PER_HOUR,
    CONF_FAN_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.whf_safety_decision import SensorReading, WhfSafetySnapshot

T0 = datetime(2026, 10, 30, 21, 32, 0, tzinfo=UTC)
CLOSED = SensorReading(is_open=False, stable_for_s=3600.0)
OPEN = SensorReading(is_open=True, stable_for_s=3600.0)


class _Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _consume(coro):
    coro.close()


def _engine(*, thermostat_mode: str = "off") -> tuple[AutomationEngine, dict]:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume)
    state = MagicMock()
    state.state = thermostat_mode
    state.attributes = {"fan_modes": ["auto", "on"]}
    fan_state = MagicMock()
    fan_state.state = "on"  # the command entity agrees with the physical fan: a plain turn_off, no on/off dance
    hass.states = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: fan_state if eid == "fan.whf" else state)
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.home",
        door_window_sensors=["binary_sensor.window"],
        notify_service="notify.notify",
        config={
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.whf",
        },
    )
    world = {
        "fan_on": True,
        "fan_on_for_s": 300.0,
        "sensors": (CLOSED,),
        "stale_names": (),
    }
    engine._get_fan_physical_state_callback = lambda: world["fan_on"]
    engine._sensor_check_callback = lambda: any(s.is_open for s in world["sensors"])
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=world["fan_on"],
        fan_on_for_s=world["fan_on_for_s"],
        sensors=world["sensors"],
        stale_names=world["stale_names"],
    )
    return engine, world


@pytest.fixture
def clock():
    c = _Clock()
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", side_effect=c.now),
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
    ):
        yield c


def _fan_off(engine):
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("fan", "turn_off")]


def _notifications(engine):
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[0] == "notify"]


def _run(engine, trigger="tick", **kw):
    return asyncio.run(engine.enforce_whf_safety(trigger, **kw))


class TestSealedHouseStop:
    def test_fan_on_a_sealed_house_is_stopped_and_its_override_and_grace_end(self, clock):
        engine, _ = _engine()
        engine._fan_override_active = True
        engine._fan_override_time = engine._whf_press_time = clock.now().isoformat()
        engine._fan_remote_timer_hours = 2.0  # RF timer: absolute against routine automation, not against safety

        result = _run(engine)

        assert result is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 1
        assert engine._fan_override_active is False, "a safety stop ends the fan override"
        assert engine._fan_remote_timer_hours is None

    def test_stop_inside_the_300s_toggle_window_still_executes(self, clock):
        engine, _ = _engine()
        engine._fan_override_active = True
        engine._fan_toggle_command_time = clock.now() - timedelta(seconds=20)

        assert _run(engine) is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 1

    def test_homekit_on_off_on_each_press_is_stopped(self, clock):
        """The HomeKit-style on/off/on: CA's own stop stamps the toggle time; the next stop is not deferred."""
        engine, world = _engine()
        engine._fan_override_active = True
        assert _run(engine) is FanCommandResult.EXECUTED
        clock.advance(5)
        world["fan_on"] = False
        assert _run(engine) is None
        clock.advance(5)
        world["fan_on"] = True  # pressed again, 10 s after our own stop
        engine._fan_override_active = True

        assert _run(engine) is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 2

    def test_zero_configured_sensors_never_stop(self, clock):
        engine, world = _engine()
        world["sensors"] = ()

        assert _run(engine) is None
        assert not _fan_off(engine)

    def test_open_window_keeps_the_fan(self, clock):
        engine, world = _engine()
        world["sensors"] = (CLOSED, OPEN)

        assert _run(engine) is None
        assert not _fan_off(engine)

    def test_settle_a_fan_that_just_came_on_is_not_stopped_yet_then_is(self, clock):
        engine, world = _engine()
        world["fan_on_for_s"] = 3.0
        with patch("custom_components.climate_advisor.automation.async_call_later") as later:
            later.return_value = MagicMock()
            assert _run(engine) is None
            assert not _fan_off(engine), "stopped inside the 10 s settle"
            delay = later.call_args.args[1]
        assert 7.0 <= delay <= 8.0, f"the retry must land just after the settle ends, got {delay}"

        world["fan_on_for_s"] = 11.0
        assert _run(engine) is FanCommandResult.EXECUTED

    def test_unavailable_sensor_stops_only_after_60s_and_alerts(self, clock):
        engine, world = _engine()
        world["sensors"] = (SensorReading(is_open=False, unknown_for_s=59.0, stable_for_s=59.0),)
        assert _run(engine) is None, "an unavailable sensor gets 60 s of benefit of the doubt"

        world["sensors"] = (SensorReading(is_open=False, unknown_for_s=61.0, stable_for_s=61.0),)
        world["stale_names"] = ("binary_sensor.window",)
        assert _run(engine) is FanCommandResult.EXECUTED
        asyncio.run(asyncio.sleep(0))
        assert engine._recent_duplicate("whf_stale_sensor", ("binary_sensor.window",), window_seconds=6 * 3600), (
            "the unavailable-sensor alert was never recorded"
        )

    def test_command_only_install_without_flags_does_nothing(self, clock):
        engine, world = _engine()
        world["fan_on"] = None
        engine._get_fan_physical_state_callback = lambda: None

        assert _run(engine) is None
        assert not _fan_off(engine)


class TestAfterTheStop:
    def test_nat_vent_cannot_restart_the_fan_until_a_window_opens(self, clock):
        engine, world = _engine()
        engine._fan_override_active = True
        _run(engine)
        world["fan_on"] = False
        world["sensors"] = (CLOSED,)

        result = asyncio.run(engine._activate_fan(reason="nat-vent test"))
        assert result is FanCommandResult.SUPPRESSED, "CA restarted the fan on a sealed house after a safety stop"

        world["sensors"] = (OPEN,)
        engine._sensor_check_callback = lambda: True
        assert asyncio.run(engine._activate_fan(reason="nat-vent test")) is not FanCommandResult.SUPPRESSED

    def test_a_failed_stop_is_verified_and_retried(self, clock):
        engine, world = _engine()
        engine._fan_override_active = True
        timers: list = []

        def _later(hass, delay, fn):
            timers.append((delay, fn))
            return MagicMock()

        with patch("custom_components.climate_advisor.automation.async_call_later", side_effect=_later):
            assert _run(engine) is FanCommandResult.EXECUTED
            verify = [t for t in timers if t[0] == 12.0]
            assert verify, f"no 12 s verify-and-retry timer armed after the stop: {[t[0] for t in timers]}"
            # the fan ignored the stop and still reads on; the verify timer fires and asks again
            clock.advance(12)
            verify[0][1](clock.now())
        assert engine._whf_safety_queued is False or engine.hass.async_create_task.called

        engine._fan_override_active = True
        assert _run(engine, "stop_verify") is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 2

    def test_notify_once_per_episode_and_alert_on_the_third_distinct_stop(self, clock):
        engine, world = _engine()
        sent: list[str] = []
        engine._notify_whf_safety = lambda title, message: sent.append(title)
        for _ in range(3):
            engine._fan_override_active = True
            world["fan_on"] = True
            assert _run(engine) is FanCommandResult.EXECUTED
            clock.advance(30)
            world["fan_on"] = False
            _run(engine)  # observed off: the next stop is a distinct one
            clock.advance(30)

        assert sent.count("Climate Advisor — whole-house fan stopped") == 1, sent
        assert "Climate Advisor — whole-house fan keeps being turned on" in sent, sent

    def test_retrying_the_same_stop_is_not_counted_as_oscillation(self, clock):
        engine, world = _engine()
        sent: list[str] = []
        engine._notify_whf_safety = lambda title, message: sent.append(title)
        for _ in range(4):  # the fan never goes off between the attempts: one stuck stop, not four presses
            engine._fan_override_active = True
            _run(engine)
            clock.advance(12)

        assert "Climate Advisor — whole-house fan keeps being turned on" not in sent, sent

    def test_cleanup_cancels_the_enforcer_timers(self, clock):
        engine, world = _engine()
        world["fan_on_for_s"] = 3.0
        cancel = MagicMock()
        with patch("custom_components.climate_advisor.automation.async_call_later", return_value=cancel):
            _run(engine)
        engine.cleanup()
        cancel.assert_called()
        assert engine._whf_safety_retry_cancel is None


class TestRule2Mutex:
    def test_occupant_heat_after_an_override_only_fan_stops_the_fan(self, clock):
        engine, world = _engine(thermostat_mode="heat")
        world["sensors"] = (OPEN,)
        engine._fan_override_active = True
        engine._fan_override_time = engine._whf_press_time = clock.now().isoformat()
        engine._grace_active = True  # the fan override's grace, shared with the HVAC change in the field
        engine._grace_protects_override = True
        clock.advance(60)
        engine._occupant_hvac_on_at = clock.now()  # the occupant sets heat a minute later

        with patch.object(AutomationEngine, "_occupant_fan_on_at", return_value=T0):
            result = _run(engine, "thermostat_mode")

        assert result is FanCommandResult.EXECUTED
        assert len(_fan_off(engine)) == 1
        assert engine._fan_override_active is True, (
            "the HVAC change has its own grace; the fan override expires with it"
        )

    def test_occupant_fan_after_armed_heat_suppresses_hvac(self, clock):
        engine, world = _engine(thermostat_mode="heat")
        world["sensors"] = (OPEN,)
        engine._fan_override_active = True
        suppress = AsyncMock()
        engine._suppress_hvac_for_whf = suppress

        with patch.object(AutomationEngine, "_occupant_fan_on_at", return_value=T0):
            result = _run(engine, "fan_entity")

        assert result is None
        suppress.assert_awaited_once()
        assert not _fan_off(engine), "the newer fan press wins; the fan keeps running"

    def test_ca_own_hvac_command_is_not_an_occupant_action(self, clock):
        engine, world = _engine(thermostat_mode="cool")
        world["sensors"] = (OPEN,)
        engine._fan_active = True  # CA's own fan session; no occupant HVAC stamp exists

        assert _run(engine) is None
        assert not _fan_off(engine)


class TestRule2DuringStartupCoalescing:
    """Seed 7000031: the occupant switches the fan on with the AC cooling, minutes after an HA restart.

    Override detection is suppressed while startup coalescing is active, so no override stamp exists; the
    coordinator's snapshot supplies the fan's physical on-time as the occupant fan-on stamp instead.
    """

    def test_a_snapshot_stamp_makes_the_newer_fan_press_suppress_hvac(self, clock):
        engine, world = _engine(thermostat_mode="cool")
        world["sensors"] = (OPEN,)
        stamp = clock.now() - timedelta(seconds=5)
        engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
            fan_on=True, fan_on_for_s=5.0, sensors=(OPEN,), occupant_fan_on_at=stamp
        )
        suppress = AsyncMock()
        engine._suppress_hvac_for_whf = suppress

        assert _run(engine, "fan_entity") is None
        suppress.assert_awaited_once()
        assert not _fan_off(engine)

    def test_without_a_stamp_nothing_is_suppressed(self, clock):
        engine, world = _engine(thermostat_mode="cool")
        world["sensors"] = (OPEN,)
        suppress = AsyncMock()
        engine._suppress_hvac_for_whf = suppress

        assert _run(engine, "fan_entity") is None
        suppress.assert_not_awaited()


class TestRequestCoalescing:
    def test_requests_while_one_is_queued_are_dropped(self, clock):
        engine, _ = _engine()
        engine.request_whf_safety_check("a")
        engine.request_whf_safety_check("b")

        assert engine.hass.async_create_task.call_count == 1

    def test_no_task_while_the_fan_is_off(self, clock):
        engine, world = _engine()
        world["fan_on"] = False
        engine.request_whf_safety_check("tick")

        assert engine.hass.async_create_task.call_count == 0


def _fan_on_calls(engine):
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("fan", "turn_on")]


def _blower_calls(engine):
    return [c for c in engine.hass.services.async_call.call_args_list if c.args[:2] == ("climate", "set_fan_mode")]


class TestEntryGate:
    """Rule 1, entry side (Issue #1144 phase 2): CA never STARTS the whole-house fan on a sealed house.

    Occupant story: nat-vent / economizer / the hourly min-runtime cycle must not spin up the whole-house fan
    while every door and window is shut (golden issue_637 caught CA's own nat-vent doing exactly that).
    """

    def test_whf_only_install_refuses_to_start_on_a_sealed_house(self, clock):
        engine, world = _engine()
        world["fan_on"] = False
        result = asyncio.run(engine._activate_fan(reason="nat-vent"))
        assert result is FanCommandResult.SUPPRESSED
        assert not _fan_on_calls(engine), "CA started the whole-house fan on a sealed house"
        assert engine._fan_active is False

    def test_whf_starts_once_a_window_is_open(self, clock):
        engine, world = _engine()
        world["fan_on"] = False
        world["sensors"] = (OPEN,)
        result = asyncio.run(engine._activate_fan(reason="nat-vent"))
        assert result is FanCommandResult.EXECUTED
        assert engine._fan_active is True

    def test_zero_configured_sensors_means_the_rule_does_not_apply(self, clock):
        engine, world = _engine()
        world["fan_on"] = False
        world["sensors"] = ()
        assert asyncio.run(engine._activate_fan(reason="nat-vent")) is FanCommandResult.EXECUTED

    def test_an_unreadable_sensor_gets_sixty_seconds_of_benefit_of_the_doubt(self, clock):
        engine, world = _engine()
        world["fan_on"] = False
        world["sensors"] = (SensorReading(is_open=False, unknown_for_s=30.0),)
        assert asyncio.run(engine._activate_fan(reason="nat-vent")) is FanCommandResult.EXECUTED

    def test_both_mode_sealed_skips_only_the_whf_leg_and_keeps_the_blower(self, clock):
        engine, world = _engine()
        engine.config[CONF_FAN_MODE] = FAN_MODE_BOTH
        world["fan_on"] = False
        result = asyncio.run(engine._activate_fan(reason="min_runtime_cycle"))
        assert result is FanCommandResult.EXECUTED
        assert not _fan_on_calls(engine), "the whole-house fan leg ran on a sealed house"
        assert _blower_calls(engine), "the HVAC blower leg was locked out (phase-1 side effect)"

    def test_blower_mode_is_unaffected_by_a_sealed_house(self, clock):
        engine, world = _engine()
        engine.config[CONF_FAN_MODE] = FAN_MODE_HVAC
        world["fan_on"] = False
        result = asyncio.run(engine._activate_fan(reason="economizer"))
        assert result is FanCommandResult.EXECUTED
        assert _blower_calls(engine)

    def test_min_runtime_cycle_does_not_run_the_whf_on_a_sealed_house(self, clock):
        engine, world = _engine()
        engine.config[CONF_FAN_MIN_RUNTIME_PER_HOUR] = 10
        world["fan_on"] = False
        timers: list = []

        def _later(hass, delay, fn):
            timers.append(delay)
            return MagicMock()

        with patch("custom_components.climate_advisor.automation.async_call_later", side_effect=_later):
            asyncio.run(engine._fan_cycle_on())
        assert not _fan_on_calls(engine)
        assert engine._fan_min_runtime_active is False
        assert timers, "the cycle must retry later, not stop forever"

    def test_min_runtime_cycle_runs_when_a_window_is_open(self, clock):
        engine, world = _engine()
        engine.config[CONF_FAN_MIN_RUNTIME_PER_HOUR] = 10
        world["fan_on"] = False
        world["sensors"] = (OPEN,)
        with patch("custom_components.climate_advisor.automation.async_call_later", return_value=MagicMock()):
            asyncio.run(engine._fan_cycle_on())
        assert _fan_on_calls(engine)
        assert engine._fan_min_runtime_active is True


class TestObserveOnly:
    """Automation switched off (observe-only): nothing is commanded, the occupant is told once per sealed episode."""

    def _observe_engine(self):
        engine, world = _engine()
        engine.dry_run = True
        engine._fan_override_active = True
        events: list = []
        engine._emit_event_callback = lambda t, p: events.append((t, p))
        return engine, world, events

    def test_observe_only_sends_no_fan_command_and_arms_no_verify_retry(self, clock):
        engine, world, _ = self._observe_engine()
        timers: list = []

        def _later(hass, delay, fn):
            timers.append(delay)
            return MagicMock()

        with patch("custom_components.climate_advisor.automation.async_call_later", side_effect=_later):
            assert _run(engine) is None
        assert not _fan_off(engine)
        assert 12.0 not in timers, "observe-only must not arm a verify-and-retry timer"
        assert engine._fan_override_active is True, "observe-only must not touch the occupant's override"

    def test_a_sealed_running_fan_notifies_exactly_once_per_episode_and_emits_the_marker(self, clock):
        engine, world, events = self._observe_engine()
        for _ in range(3):
            _run(engine)
        pushes = [c for c in _notifications(engine) if c.args[1] != "send_email"]
        assert len(pushes) == 1, f"one notification per episode, got {len(pushes)}"
        assert [t for t, _ in events] == ["whf_sealed_observe_notified"]
        assert not _fan_off(engine)

    def test_the_episode_re_arms_when_the_fan_stops_then_notifies_again(self, clock):
        engine, world, events = self._observe_engine()
        _run(engine)
        world["fan_on"] = False
        _run(engine)  # fan stopped: episode over
        world["fan_on"] = True
        world["fan_on_for_s"] = 300.0
        _run(engine)
        pushes = [c for c in _notifications(engine) if c.args[1] != "send_email"]
        assert len(pushes) == 2

    def test_the_episode_re_arms_when_a_window_opens(self, clock):
        engine, world, events = self._observe_engine()
        _run(engine)
        world["sensors"] = (OPEN,)
        _run(engine)
        world["sensors"] = (CLOSED,)
        _run(engine)
        pushes = [c for c in _notifications(engine) if c.args[1] != "send_email"]
        assert len(pushes) == 2

    def test_no_notice_when_a_window_is_open(self, clock):
        engine, world, events = self._observe_engine()
        world["sensors"] = (OPEN,)
        _run(engine)
        assert _notifications(engine) == [] and events == []

    def test_a_fan_just_switched_on_waits_out_the_settle_before_notifying(self, clock):
        engine, world, events = self._observe_engine()
        world["fan_on_for_s"] = 2.0
        _run(engine)
        assert _notifications(engine) == []

    def test_automation_on_still_stops_the_fan_and_sends_no_observe_marker(self, clock):
        engine, world = _engine()
        engine._fan_override_active = True
        events: list = []
        engine._emit_event_callback = lambda t, p: events.append(t)
        _run(engine)
        assert len(_fan_off(engine)) == 1
        assert "whf_sealed_observe_notified" not in events


class TestOnlyAWholeHouseFanPressIsAnOccupantWhfAction:
    """Owner decision (Issue #1133): a press of the thermostat's own blower is not a WHF action, so rule 2 never
    counts it as the occupant's newest fan-on."""

    def _press(self, clock, *, hvac_fan):
        engine, _ = _engine()
        engine._start_grace_period = MagicMock()
        engine._suppress_hvac_for_whf = AsyncMock()
        engine.hass.async_create_task = MagicMock(side_effect=_consume)
        with patch(
            "custom_components.climate_advisor.automation.dt_util.parse_datetime", side_effect=datetime.fromisoformat
        ):
            engine.handle_fan_manual_override("auto", "on", hvac_fan=hvac_fan)
            engine._stamp = engine._occupant_fan_on_at()
        return engine

    def test_a_whole_house_fan_press_is_stamped(self, clock):
        engine = self._press(clock, hvac_fan=False)
        assert engine._stamp == clock.now()

    def test_a_thermostat_blower_press_is_not(self, clock):
        engine = self._press(clock, hvac_fan=True)
        assert engine._fan_override_active is True
        assert engine._stamp is None


class TestStopNotificationWording:
    """Issue #1178: a sealed-house stop waits for a window; a rule-2 (fan vs heating/cooling) stop does not."""

    @staticmethod
    def _message(clock, *, sealed: bool) -> str:
        engine, _ = _engine()
        sent: list[str] = []
        engine._notify_whf_safety = lambda title, message: sent.append(message)
        engine._after_whf_safety_stop(FanCommandResult.EXECUTED, sealed=sealed, reason="test", trigger="tick")
        assert len(sent) == 1, sent
        return sent[0]

    def test_sealed_house_stop_says_it_waits_for_a_window(self, clock):
        msg = self._message(clock, sealed=True)
        assert "every monitored door and window is closed" in msg
        assert "will not restart the fan until a window opens" in msg

    def test_heating_cooling_stop_does_not_claim_to_wait_for_a_window(self, clock):
        msg = self._message(clock, sealed=False)
        assert "heating/cooling was turned on" in msg
        assert "never run together" in msg
        assert "window" not in msg
