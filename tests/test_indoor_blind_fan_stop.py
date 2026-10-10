"""Issue #1037: a running nat-vent / fan session must not stay blind forever when the
indoor temperature becomes unreadable.

Occupant experience being protected: the indoor sensor drops out overnight while the
whole-house fan is pulling cold air in. Every indoor-dependent exit (comfort floor, away
ceiling, outdoor-rise) needs a reading, so before this fix the fan ran until the sensor
came back and the house overcooled, with the owner never told.

These tests drive the REAL ``AutomationEngine.indoor_blind_check`` /
``note_indoor_available`` / ``_indoor_blind_stop`` / ``indoor_blind_info`` and the real
coordinator ``_append_indoor_blind_suffix`` / ``_async_run_temp_reactive_checks`` — nothing
here re-implements the logic under test. Time is driven by patching ``dt_util.now``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_MODE,
    FAN_MODE_BOTH,
    FAN_MODE_WHOLE_HOUSE,
    INDOOR_BLIND_EPISODE_GAP_S,
    INDOOR_BLIND_FAN_STOP_S,
    INDOOR_BLIND_PUSH_DEBOUNCE_S,
)

_T0 = datetime(2026, 10, 9, 2, 0, 0, tzinfo=UTC)
_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"


_RIGS: list[_Rig] = []


@pytest.fixture(autouse=True)
def _close_leftover_tasks():
    """Close any queued fire-and-forget coroutine a test did not drain (no 'never awaited' noise)."""
    yield
    for rig in _RIGS:
        for coro in rig.tasks:
            coro.close()
    _RIGS.clear()


@pytest.fixture
def clock():
    """Mutable fake clock patched into the engine's ``dt_util.now``."""
    state = {"now": _T0}
    with patch(_NOW_PATH, side_effect=lambda: state["now"]):
        yield state


def _advance(clock: dict, **kwargs: float) -> None:
    clock["now"] = clock["now"] + timedelta(**kwargs)


class _Rig:
    """A real engine plus recorders for events, queued tasks and refresh requests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.tasks: list = []
        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock(side_effect=self.tasks.append)
        hass.states = MagicMock()
        config = {
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
        }
        self.engine = AutomationEngine(
            hass=hass,
            climate_entity="climate.thermostat",
            weather_entity="weather.forecast_home",
            door_window_sensors=["binary_sensor.front_door"],
            notify_service="notify.notify",
            config=config,
        )
        e = self.engine
        e._emit_event_callback = lambda event_type, payload: self.events.append((event_type, payload))
        e._request_refresh_callback = MagicMock()
        e._notify = AsyncMock()
        e._exit_nat_vent = AsyncMock(return_value=FanCommandResult.EXECUTED)
        _RIGS.append(self)

    def start_nat_vent(self) -> None:
        self.engine._natural_vent_active = True
        self.engine._fan_active = True

    def blind(self) -> None:
        asyncio.run(self.engine.indoor_blind_check(None))

    def blind_for(self, clock: dict, seconds: float) -> None:
        """Blind backstop ticks every 5 minutes (the real cadence) until ``seconds`` have elapsed."""
        remaining = seconds
        while remaining > 0:
            step = min(300.0, remaining)
            _advance(clock, seconds=step)
            self.blind()
            remaining -= step

    def drain_tasks(self) -> None:
        """Run every queued fire-and-forget coroutine (notification sends)."""
        while self.tasks:
            asyncio.run(self.tasks.pop(0))

    def types(self, name: str) -> list[dict]:
        return [p for t, p in self.events if t == name]

    def pushes(self) -> list[tuple]:
        self.drain_tasks()
        return [c.args for c in self.engine._notify.await_args_list]


# ---------------------------------------------------------------------------
# Failure state: detection, alert, stop
# ---------------------------------------------------------------------------


class TestBlindDetectionAndStop:
    def test_first_blind_tick_alerts_once_and_does_not_stop(self, clock):
        rig = _Rig()
        rig.start_nat_vent()

        rig.blind()

        started = rig.types("fan_blind_started")
        assert len(started) == 1
        assert started[0]["session"] == "nat_vent"
        assert started[0]["will_stop"] is True
        assert started[0]["stop_at"] == (_T0 + timedelta(seconds=INDOOR_BLIND_FAN_STOP_S)).isoformat()
        rig.engine._exit_nat_vent.assert_not_awaited()
        pushes = rig.pushes()
        assert len(pushes) == 1
        assert pushes[0][2] == "indoor_blind_fan"
        assert "will turn the fan off at 02:30" in pushes[0][0]

    def test_alert_is_sent_once_per_episode(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        for _ in range(4):
            rig.blind()
            _advance(clock, minutes=5)
        assert len(rig.types("fan_blind_started")) == 1
        assert len(rig.pushes()) == 1

    def test_no_stop_just_before_deadline(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S - 1)
        rig.engine._exit_nat_vent.assert_not_awaited()

    def test_stop_at_deadline_exits_nat_vent_and_notifies(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)

        rig.engine._exit_nat_vent.assert_awaited_once()
        kwargs = rig.engine._exit_nat_vent.await_args.kwargs
        assert kwargs["event_type"] == "fan_blind_stopped"
        assert kwargs["event_payload"]["minutes_blind"] == 30
        assert kwargs["event_payload"]["session"] == "nat_vent"
        # No lockout: not an outdoor-driven exit (registered as exempt in the coverage test).
        assert not kwargs.get("set_outdoor_exit_time")
        assert rig.engine._indoor_blind_stopped is True
        titles = [p[1] for p in rig.pushes()]
        assert titles == [
            "Climate Advisor: fan running without a temperature reading",
            "Climate Advisor: ventilation fan turned off",
        ]

    def test_cycling_off_phase_still_stops(self, clock):
        """Nat-vent cycling holds the session open with the fan OFF between runs; the blind
        stop must key on the session, not on the fan being physically on."""
        rig = _Rig()
        rig.engine._natural_vent_active = True
        rig.engine._fan_active = False
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)
        rig.engine._exit_nat_vent.assert_awaited_once()

    def test_stop_uses_real_exit_choke_point_and_clears_session(self, clock):
        """End to end through the real ``_exit_nat_vent`` / ``_end_nat_vent_session``."""
        rig = _Rig()
        e = rig.engine
        del e._exit_nat_vent  # drop the AsyncMock so the class method runs
        e._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        e._any_monitored_sensor_open = MagicMock(return_value=True)
        e._start_grace_period = MagicMock()
        rig.start_nat_vent()

        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)

        assert e._natural_vent_active is False
        stopped = rig.types("fan_blind_stopped")
        assert len(stopped) == 1
        assert stopped[0]["minutes_blind"] == 30
        e._deactivate_fan.assert_awaited_once()

    def test_stop_with_sensors_closed_restores_hvac_and_starts_grace_with_indoor_none(self, clock):
        """The other fork of the real ``_exit_nat_vent`` (sensors closed): resume HVAC + grace,
        none of which may read indoor in a way that breaks while it is None."""
        rig = _Rig()
        e = rig.engine
        del e._exit_nat_vent
        e._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        e._any_monitored_sensor_open = MagicMock(return_value=False)
        e._start_grace_period = MagicMock()
        e._set_temperature_for_mode = AsyncMock()
        e._current_classification = MagicMock()
        rig.start_nat_vent()

        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)

        assert e._natural_vent_active is False
        e._set_temperature_for_mode.assert_awaited_once()
        e._start_grace_period.assert_called_once()
        assert len(rig.types("fan_blind_stopped")) == 1

    def test_rate_limited_stop_retries_without_second_alert_or_stop_flag(self, clock):
        rig = _Rig()
        rig.engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)
        _advance(clock, minutes=5)
        rig.blind()

        assert rig.engine._exit_nat_vent.await_count == 2  # retried on the next tick
        assert rig.engine._indoor_blind_stopped is False
        assert len(rig.types("fan_blind_started")) == 1
        assert [p[1] for p in rig.pushes()] == ["Climate Advisor: fan running without a temperature reading"]

    def test_gap_restarts_the_episode_instead_of_inheriting_stale_since(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        # Nothing evaluated the outage for longer than the episode gap (e.g. fan was not
        # backstopped), so a later blind sighting must start a fresh 30-minute clock.
        _advance(clock, seconds=INDOOR_BLIND_EPISODE_GAP_S + 60)
        rig.blind()
        rig.engine._exit_nat_vent.assert_not_awaited()
        assert rig.engine._indoor_blind_since == clock["now"]
        assert len(rig.types("fan_blind_started")) == 2  # a genuinely new episode


class TestEligibleSessions:
    def test_override_fan_alerts_but_never_stops(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.engine._fan_override_active = True
        rig.blind()
        rig.blind_for(clock, 7200)

        started = rig.types("fan_blind_started")
        assert len(started) == 1
        assert started[0]["session"] == "override"
        assert started[0]["will_stop"] is False
        assert started[0]["stop_at"] is None
        rig.engine._exit_nat_vent.assert_not_awaited()
        assert "will NOT turn it off" in rig.pushes()[0][0]

    def test_economizer_fan_is_alert_only(self, clock):
        rig = _Rig()
        rig.engine._fan_active = True
        rig.engine._economizer_active = True
        rig.blind()
        rig.blind_for(clock, 3600)
        assert rig.types("fan_blind_started")[0]["session"] == "economizer"
        rig.engine._exit_nat_vent.assert_not_awaited()

    def test_min_runtime_only_fan_is_ignored(self, clock):
        """Indoor-independent by design — an outage changes nothing about it."""
        rig = _Rig()
        rig.engine._fan_active = True  # no nat-vent, no override, no economizer
        rig.blind()
        rig.blind_for(clock, 3600)
        assert rig.events == []
        rig.engine._exit_nat_vent.assert_not_awaited()

    def test_no_fan_running_is_ignored(self, clock):
        rig = _Rig()
        rig.blind()
        assert rig.events == []


# ---------------------------------------------------------------------------
# Recovery state
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_recovery_before_deadline_emits_event_and_push(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        _advance(clock, minutes=10)
        asyncio.run(rig.engine.indoor_blind_check(68.4))

        recovered = rig.types("fan_blind_recovered")
        assert recovered == [
            {
                "session": "nat_vent",
                "minutes_blind": 10,
                "lower_bound": False,
                "stopped": False,
                "fan_running": True,
                "indoor_f": 68.4,
            }
        ]
        assert rig.engine._indoor_blind_since is None
        assert rig.engine.indoor_blind_info is None
        titles = [p[1] for p in rig.pushes()]
        assert titles[-1] == "Climate Advisor: indoor temperature is back"
        assert "monitoring the fan normally" in rig.pushes()[-1][0]

    def test_recovery_after_stop_reports_stopped_and_uses_last_blind_sighting(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)  # stop fires here; this is also the last blind sighting
        rig.engine._natural_vent_active = False  # what the real exit does
        rig.engine._fan_active = False
        _advance(clock, hours=3)  # sensor stays dead for hours, then returns
        rig.engine.note_indoor_available(67.0)

        recovered = rig.types("fan_blind_recovered")
        assert len(recovered) == 1
        assert recovered[0]["stopped"] is True
        # Lower bound from the last sighting, not "now" (3 h later) — nothing watched in between.
        assert recovered[0]["minutes_blind"] == 30
        assert recovered[0]["lower_bound"] is True
        assert "fan stays off" in rig.pushes()[-1][0]
        assert "after at least 30 minutes" in rig.pushes()[-1][0]  # never "about 30" for a 3 h outage

    def test_healthy_reading_with_no_episode_is_silent(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.engine.note_indoor_available(70.0)
        assert rig.events == []
        assert rig.pushes() == []

    def test_fan_restarted_after_a_stop_gets_its_own_alert_and_card_suffix(self, clock):
        """The owner's last message was "fan turned off"; a fan running blind again inside the same
        outage (the economizer fails open on indoor None, #1042) must not be silent or hidden."""
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)  # stopped
        rig.engine._natural_vent_active = False
        rig.engine._fan_active = False
        _advance(clock, minutes=5)
        rig.engine._fan_active = True
        rig.engine._economizer_active = True
        rig.blind()
        started = rig.types("fan_blind_started")
        assert len(started) == 2
        assert started[1]["session"] == "economizer"
        assert rig.engine.indoor_blind_info is not None  # visible on the Fan card again
        titles = [p[1] for p in rig.pushes()]
        # Forced past the debounce: this message is materially new.
        assert titles.count("Climate Advisor: fan running without a temperature reading") == 2

    def test_flapping_sensor_does_not_spam_pushes(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()  # alert push #1
        _advance(clock, minutes=5)
        asyncio.run(rig.engine.indoor_blind_check(68.0))  # recovery push #1
        _advance(clock, minutes=5)
        rig.blind()  # new episode inside the debounce window: event yes, push no
        _advance(clock, minutes=5)
        asyncio.run(rig.engine.indoor_blind_check(68.0))  # its recovery push is suppressed too
        assert len(rig.types("fan_blind_started")) == 2  # observability is never debounced
        assert len(rig.types("fan_blind_recovered")) == 2
        assert [p[1] for p in rig.pushes()] == [
            "Climate Advisor: fan running without a temperature reading",
            "Climate Advisor: indoor temperature is back",
        ]
        # Once the window has passed a genuinely new outage alerts again.
        _advance(clock, seconds=INDOOR_BLIND_PUSH_DEBOUNCE_S)
        rig.blind()
        assert [p[1] for p in rig.pushes()][-1] == "Climate Advisor: fan running without a temperature reading"

    def test_recovery_of_a_stale_episode_reports_a_lower_bound_not_now(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind_for(clock, 900)
        rig.engine._natural_vent_active = False  # fan ended another way while still blind
        rig.engine._fan_active = False
        _advance(clock, hours=4)  # nothing evaluated the outage in between
        rig.engine.note_indoor_available(66.0)

        recovered = rig.types("fan_blind_recovered")[0]
        assert recovered["lower_bound"] is True
        assert recovered["minutes_blind"] == 10  # first blind tick +5 min .. last +15 min; not 4 h later
        assert recovered["fan_running"] is False
        assert recovered["stopped"] is False

    def test_recovery_message_does_not_claim_the_fan_is_running_when_it_is_not(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        _advance(clock, minutes=5)
        rig.engine._natural_vent_active = False  # user switched the WHF off while still blind
        rig.engine._fan_active = False
        rig.engine.note_indoor_available(70.0)
        message = rig.pushes()[-1][0]
        assert "The fan is no longer running." in message
        assert "monitoring the fan normally" not in message

    def test_override_handed_to_climate_advisor_restarts_the_deadline_and_realerts(self, clock):
        """Told "will NOT turn it off", then CA takes over the session: the owner must get a fresh
        alert with a real deadline, not a surprise stop on the very next tick."""
        rig = _Rig()
        rig.start_nat_vent()
        rig.engine._fan_override_active = True
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S - 300)  # 25 min in, alert-only
        rig.engine._fan_override_active = False  # override clears; nat-vent session remains
        rig.blind()
        rig.engine._exit_nat_vent.assert_not_awaited()
        started = rig.types("fan_blind_started")
        assert [e["session"] for e in started] == ["override", "nat_vent"]
        assert started[1]["will_stop"] is True
        assert rig.engine._indoor_blind_since == clock["now"]  # deadline restarted from the handoff
        pushes = rig.pushes()
        assert "will NOT turn it off" in pushes[0][0]
        assert "will turn the fan off at" in pushes[1][0]


# ---------------------------------------------------------------------------
# Observability surface
# ---------------------------------------------------------------------------


class TestObservability:
    def test_info_property_reports_the_single_authoritative_deadline(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        assert rig.engine.indoor_blind_info is None  # no episode yet
        rig.blind()
        info = rig.engine.indoor_blind_info
        assert info["session"] == "nat_vent"
        assert info["stop_at"] == _T0 + timedelta(seconds=INDOOR_BLIND_FAN_STOP_S)

    def test_info_is_none_once_stopped_and_for_stale_episodes(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        _advance(clock, seconds=INDOOR_BLIND_EPISODE_GAP_S + 1)
        assert rig.engine.indoor_blind_info is None  # nothing is evaluating it any more

    def test_info_is_none_after_the_stop_has_landed(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)
        assert rig.engine._indoor_blind_stopped is True
        rig.engine._natural_vent_active = True  # even if a session flag lingers
        assert rig.engine.indoor_blind_info is None

    def test_deferred_stop_warns_once_then_drops_to_debug(self, clock, caplog):
        import logging

        rig = _Rig()
        rig.engine._exit_nat_vent = AsyncMock(return_value=FanCommandResult.RATE_LIMITED_NEW)
        rig.start_nat_vent()
        rig.blind()
        with caplog.at_level(logging.DEBUG, logger="custom_components.climate_advisor.automation"):
            rig.blind_for(clock, INDOOR_BLIND_FAN_STOP_S)  # first deferral
            rig.blind_for(clock, 300)  # repeat
        deferred = [r for r in caplog.records if "Fan stop deferred" in r.getMessage()]
        assert [r.levelno for r in deferred] == [logging.WARNING, logging.DEBUG]

    def test_info_has_no_stop_time_for_alert_only_sessions(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.engine._fan_override_active = True
        rig.blind()
        info = rig.engine.indoor_blind_info
        assert info is not None
        assert info["stop_at"] is None

    def test_every_transition_requests_a_coordinator_refresh(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        assert rig.engine._request_refresh_callback.call_count == 1  # started
        asyncio.run(rig.engine.indoor_blind_check(70.0))
        assert rig.engine._request_refresh_callback.call_count == 2  # recovered

    def test_notification_uses_dedicated_type_and_reaches_notify(self, clock):
        rig = _Rig()
        rig.start_nat_vent()
        rig.blind()
        rig.drain_tasks()
        args = rig.engine._notify.await_args.args
        assert args[2] == "indoor_blind_fan"

    def test_notify_failure_is_swallowed_and_logged(self, clock, caplog):
        rig = _Rig()
        rig.engine._notify = AsyncMock(side_effect=RuntimeError("notify service down"))
        rig.start_nat_vent()
        rig.blind()
        rig.drain_tasks()  # must not raise
        assert "Blind-fan notification failed" in caplog.text


# ---------------------------------------------------------------------------
# Wiring: backstop + coordinator
# ---------------------------------------------------------------------------


class TestWiring:
    def _backstop_engine(self, clock):
        rig = _Rig()
        e = rig.engine
        e.fan_thermostat_check = AsyncMock()
        e.nat_vent_temperature_check = AsyncMock()
        e._enforce_sealed_house_whf_guard = AsyncMock()
        e._start_fan_thermo_backstop = MagicMock()
        e._get_indoor_temp_f = MagicMock(return_value=None)
        e._last_outdoor_temp = 55.0
        rig.start_nat_vent()
        return rig

    def test_backstop_tick_with_indoor_none_runs_the_blind_check(self, clock):
        rig = self._backstop_engine(clock)
        asyncio.run(rig.engine._thermo_backstop_task())
        assert len(rig.types("fan_blind_started")) == 1

    def test_backstop_tick_with_healthy_indoor_clears_the_episode(self, clock):
        rig = self._backstop_engine(clock)
        asyncio.run(rig.engine._thermo_backstop_task())
        rig.engine._get_indoor_temp_f = MagicMock(return_value=69.0)
        _advance(clock, minutes=5)
        asyncio.run(rig.engine._thermo_backstop_task())
        assert len(rig.types("fan_blind_recovered")) == 1
        assert rig.engine._indoor_blind_since is None

    def test_failure_in_blind_check_never_stops_the_backstop_rearming(self, clock):
        rig = self._backstop_engine(clock)
        rig.engine.indoor_blind_check = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(rig.engine._thermo_backstop_task())  # must not raise
        rig.engine._start_fan_thermo_backstop.assert_called_once()

    def test_coordinator_reports_recovery_on_healthy_reading_and_not_on_none(self):
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        coord = object.__new__(cls)
        ae = MagicMock()
        ae._natural_vent_active = False
        ae._fan_active = False
        ae.nat_vent_temperature_check = AsyncMock()
        ae.fan_thermostat_check = AsyncMock()
        ae.comfort_family_temperature_check = AsyncMock()
        coord.automation_engine = ae
        coord._last_outdoor_temp = 60.0
        coord._last_predicted_indoor = None
        coord._async_run_temp_reactive_checks = types.MethodType(cls._async_run_temp_reactive_checks, coord)

        coord._get_indoor_temp = MagicMock(return_value=None)
        asyncio.run(coord._async_run_temp_reactive_checks())
        ae.note_indoor_available.assert_not_called()

        coord._get_indoor_temp = MagicMock(return_value=69.5)
        asyncio.run(coord._async_run_temp_reactive_checks())
        ae.note_indoor_available.assert_called_once_with(69.5)


class TestThermalTickRecoveryHook:
    """The 5-minute tick is the source-independent place that notices recovery after a stop."""

    @staticmethod
    def _real_tick():
        """``_async_thermal_sample_tick`` is @callback-decorated, which the stub layer swallows into a
        MagicMock — fetch the undecorated function the way tests/test_outdoor_temp_propagation.py does
        (patch the source ``homeassistant.core.callback``, reload, then reload again to restore the
        default state for every other test in the session)."""
        module = importlib.import_module("custom_components.climate_advisor.coordinator")
        with patch("homeassistant.core.callback", side_effect=lambda fn: fn):
            importlib.reload(module)
            tick = module.ClimateAdvisorCoordinator._async_thermal_sample_tick
        importlib.reload(module)
        return module.ClimateAdvisorCoordinator, tick

    def _coord(self, indoor):
        cls, tick = self._real_tick()
        coord = object.__new__(cls)
        coord.automation_engine = MagicMock()
        coord._refresh_outdoor_temp = MagicMock()
        coord._refresh_indoor_sensor_health = MagicMock()
        coord._sample_all_observations = MagicMock()
        coord._get_indoor_temp = MagicMock(return_value=indoor)
        return coord, tick

    def test_healthy_reading_on_tick_reports_recovery(self):
        coord, tick = self._coord(68.0)
        types.MethodType(tick, coord)(datetime(2026, 10, 9, 3, 0))
        coord.automation_engine.note_indoor_available.assert_called_once_with(68.0)
        coord._sample_all_observations.assert_called_once()

    def test_none_reading_on_tick_does_not_report_recovery(self):
        coord, tick = self._coord(None)
        types.MethodType(tick, coord)(datetime(2026, 10, 9, 3, 0))
        coord.automation_engine.note_indoor_available.assert_not_called()

    def test_recovery_hook_failure_never_aborts_thermal_sampling(self):
        coord, tick = self._coord(68.0)
        coord.automation_engine.note_indoor_available.side_effect = RuntimeError("boom")
        types.MethodType(tick, coord)(datetime(2026, 10, 9, 3, 0))
        coord._sample_all_observations.assert_called_once()


class TestFanCardSuffix:
    def _coord(self, info, fan_mode=None):
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        coord = object.__new__(cls)
        ae = MagicMock()
        ae.indoor_blind_info = info
        ae.config = {CONF_FAN_MODE: fan_mode} if fan_mode else {}
        coord.automation_engine = ae
        coord._append_indoor_blind_suffix = types.MethodType(cls._append_indoor_blind_suffix, coord)
        return coord

    def _suffix(self, info, status, *, card="whf", fan_mode=None, now=_T0):
        coord = self._coord(info, fan_mode)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x),
            patch("custom_components.climate_advisor.coordinator.dt_util.now", return_value=now),
        ):
            return coord._append_indoor_blind_suffix(status, card=card)

    def test_suffix_names_the_stop_time(self):
        stop_at = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
        out = self._suffix({"stop_at": stop_at, "session": "nat_vent", "since": _T0}, "active")
        assert out == "active (indoor sensor offline — stops 02:30)"

    def test_alert_only_session_says_no_auto_stop(self):
        out = self._suffix({"stop_at": None, "session": "override", "since": _T0}, "running (manual override)")
        assert out == "running (manual override) (indoor sensor offline — no auto-stop)"

    def test_past_deadline_says_stop_pending_not_a_time_in_the_past(self):
        stop_at = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
        later = stop_at + timedelta(minutes=7)
        out = self._suffix({"stop_at": stop_at, "session": "nat_vent", "since": _T0}, "active", now=later)
        assert out == "active (indoor sensor offline — stop pending)"

    def test_both_mode_puts_the_note_on_the_whf_card_only(self):
        info = {"stop_at": None, "session": "override", "since": _T0}
        assert "indoor sensor offline" in self._suffix(info, "active", card="whf", fan_mode=FAN_MODE_BOTH)
        assert self._suffix(info, "inactive", card="hvac", fan_mode=FAN_MODE_BOTH) == "inactive"

    def test_hvac_only_install_shows_the_note_on_the_hvac_card(self):
        info = {"stop_at": None, "session": "override", "since": _T0}
        out = self._suffix(info, "running (manual override)", card="hvac", fan_mode="hvac")
        assert out.endswith("(indoor sensor offline — no auto-stop)")

    def test_no_episode_leaves_status_unchanged(self):
        assert self._suffix(None, "active") == "active"

    def test_none_status_stays_none(self):
        assert self._suffix({"stop_at": None, "session": "override", "since": _T0}, None) is None

    def test_partial_engine_double_is_ignored(self):
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        cls = mod.ClimateAdvisorCoordinator
        coord = object.__new__(cls)
        coord.automation_engine = MagicMock()  # indoor_blind_info is a MagicMock, not a dict
        assert types.MethodType(cls._append_indoor_blind_suffix, coord)("inactive", card="whf") == "inactive"
