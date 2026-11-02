"""Issue #1215 (engine tier): a sealed-house whole-house-fan stop that never converges and restarts the fan.

Occupant framing: the person closes the last window (or starts the fan from the RF remote) with the whole-house fan
running. Rule 1 says it is off within 30 s and stays off. On 2026-11-01 (v0.8.22) the real production fan kept
running for ~3 minutes while Climate Advisor sent 66 ``fan.turn_off`` and 33 ``fan.turn_on`` commands, and when the
window was closed the fan went off, back ON (turned on by Climate Advisor), then off again.

Mechanism, reproduced on a real ``AutomationEngine``: the fan is commanded through a one-way RF transmitter entity
that flips the instant it is commanded, while the physical fan is read through a power-detect helper that lags the
command (~5.5 s on, ~15.5 s off in the production history). A stop flips the transmitter to "off"; the power reading
still says "on"; ``_enforce_whf_safety_locked`` (triggered by that very flip) sees a running fan, and
``_command_whf_control_entity`` reads "control already off, physical disagrees" as drift and forces a real
transition: ``fan.turn_on``, 5 s, ``fan.turn_off``. The ``fan.turn_on`` restarts the fan the lag was about to
confirm off, so the next cycle starts again, forever, until something outside (a window opening) breaks it.

The scenario drives the real engine through its public entry points only (``handle_fan_manual_override``,
``enforce_whf_safety``) with a lagging fan model standing in for the device; nothing internal is hand-set. The
coordinator listener that re-runs the check on every fan-entity change and the 15 s tick are modelled by the
driver calling ``enforce_whf_safety("fan_entity")`` / ``("tick")`` exactly as the coordinator does.

Fixed in v0.8.28 (the strict xfails of the investigation are now plain tests): a stop is ONE plain ``turn_off``
(never an "on" first), a stop whose power reading has not caught up yet is "in flight" and is not decided again,
and the HVAC restore waits for the fan to stay off. The fan model follows the QuietCool firmware option 1 (the
fan entity follows the remote's real power state: an RF press also flips the control entity to "on"); the
old-firmware plant (``firmware_follows_remote=False``) is the loud-failure test. Positive controls show that the lag,
not the model, is what the fix is about, and that ``hvac_fan`` (no sealed-house rule) sends nothing.

Routes: ``rf_override_window_closed`` (15:12), ``rf_press_sealed`` (15:04, after the 10 s settle) and
``ca_started_window_closed`` (a nat-vent fan CA started itself). Fan modes: ``whole_house_fan`` is the only mode the
rule applies to; ``hvac_fan`` is the control; ``both`` is unsupported (#1175).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult
from custom_components.climate_advisor.const import CONF_FAN_ENTITY, CONF_FAN_MODE, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.fan_status import fan_transition_is_echo_of, remote_event_is_echo_of
from custom_components.climate_advisor.whf_safety_decision import (
    WHF_OCCUPANT_PRESS_PENDING_S,
    WHF_RESTORE_QUIET_S,
    WHF_STOP_CONVERGE_WINDOW_S,
    WHF_STOP_GIVEUP_HOLD_S,
    WHF_STOP_MAX_RETRIES,
    SensorReading,
    WhfSafetyAction,
    WhfSafetyInputs,
    WhfSafetySnapshot,
    decide_whf_safety,
)

T0 = datetime(2026, 11, 1, 15, 12, 14, tzinfo=UTC)
ON_LAG_S = 5.5  # production: 15:12:30.135 turn_on -> 15:12:35.622 detect on
OFF_LAG_S = 15.5  # production: 15:06:45.9 -> 15:07:01.26 (15.3 s); 15:12:35.02 -> 15:12:51.07 (16.0 s)
OBSERVE_S = 50


class _OccupantOutcomeViolated(AssertionError):
    """The sealed house did not get its fan stopped and kept stopped (the #1215 symptom)."""


class _Clock:
    """Virtual clock plus the engine's one-shot timers (``async_call_later``) and the tasks it spawns."""

    def __init__(self) -> None:
        self.t = T0
        self.timers: list[list] = []  # [due, fn, cancelled]
        self.tasks: list = []
        self.restores: list[datetime] = []
        self.restore_fan_on: list[bool] = []

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def schedule(self, _hass, delay, fn):
        entry = [self.t + timedelta(seconds=delay), fn, False]
        self.timers.append(entry)

        def _cancel() -> None:
            entry[2] = True

        return _cancel

    async def fire_due(self) -> None:
        """Fire every timer that is due and run the tasks they spawned, as the event loop would."""
        for entry in list(self.timers):
            if not entry[2] and entry[0] <= self.t:
                entry[2] = True
                entry[1](self.t)
        while self.tasks:
            coro = self.tasks.pop(0)
            with contextlib.suppress(Exception):  # a non-WHF task (setpoint verify, notification) is not judged here
                await coro


class _Plant:
    """One-way RF transmitter entity plus a lagging power-detect reading (a QuietCool-style template fan)."""

    def __init__(
        self,
        clock: _Clock,
        *,
        control: str,
        physical_on: bool,
        on_for_s: float,
        lag: bool = True,
        firmware_follows_remote: bool = True,
        drop_off: bool = False,
    ) -> None:
        self.clock = clock
        self.drop_off = drop_off  # every RF off is lost: the entity flips, the fan never hears it
        # Firmware option 1: the fan entity follows the remote's real power state, so a remote start reads "on".
        # Old firmware: it keeps reading "off" (and a plain off to an entity that reads off transmits nothing).
        self.firmware_follows_remote = firmware_follows_remote
        self.control = control
        self.physical_on = physical_on
        self.physical_since = clock.now() - timedelta(seconds=on_for_s)
        self.lag = lag
        self.pending: tuple[datetime, bool] | None = None
        self.commands: list[tuple[datetime, str]] = []
        self.physical_log: list[tuple[datetime, bool]] = []

    def settle(self) -> None:
        if self.pending and self.pending[0] <= self.clock.now():
            due, on = self.pending
            self.pending = None
            if on != self.physical_on:
                self.physical_on, self.physical_since = on, due
                self.physical_log.append((due, on))

    async def service(self, domain, service, data, **_kw):
        if domain != "fan":
            return None
        self.settle()
        self.commands.append((self.clock.now(), service))
        on = service == "turn_on"
        if (self.control == "on") == on:
            return None  # an ESPHome template fan transmits on a state CHANGE only: on-to-on / off-to-off is a no-op
        self.control = "on" if on else "off"
        if self.drop_off and not on:
            return None
        delay = (ON_LAG_S if on else OFF_LAG_S) if self.lag else 0.0
        self.pending = (self.clock.now() + timedelta(seconds=delay), on)
        self.settle()
        return None

    def count(self, service: str) -> int:
        return sum(1 for _, s in self.commands if s == service)


def _engine(plant: _Plant, fan_mode: str, clock: _Clock, *, hvac: bool = False) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=plant.service)
    hass.async_create_task = MagicMock(side_effect=lambda coro: clock.tasks.append(coro))
    climate = MagicMock()
    climate.state = "off"
    climate.attributes = {"fan_modes": ["auto", "on"]}

    def _get(entity_id):
        if entity_id == "fan.whf":
            fan = MagicMock()
            fan.state = plant.control
            fan.attributes = {}
            return fan
        return climate

    hass.states = MagicMock()
    hass.states.get = MagicMock(side_effect=_get)
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
            CONF_FAN_MODE: fan_mode,
            CONF_FAN_ENTITY: "fan.whf",
        },
    )

    def _physical():
        plant.settle()
        return plant.physical_on

    engine._get_fan_physical_state_callback = _physical
    engine._sensor_check_callback = lambda: False  # every door and window closed
    engine._whf_safety_snapshot_callback = lambda: (
        plant.settle(),
        WhfSafetySnapshot(
            fan_on=plant.physical_on,
            fan_on_for_s=(plant.clock.now() - plant.physical_since).total_seconds() if plant.physical_on else None,
            sensors=(SensorReading(is_open=False, stable_for_s=3600.0),),
            stale_names=(),
        ),
    )[1]
    if hvac:
        engine._pre_fan_hvac_mode = "cool"  # the thermostat was suppressed under the fan; the stop gives it back

        async def _record_restore(mode, **_kw):
            clock.restores.append(clock.now())
            clock.restore_fan_on.append(plant.physical_on)

        engine._set_hvac_mode = AsyncMock(side_effect=_record_restore)
        engine._reclassify_callback = MagicMock()
    return engine


@pytest.fixture
def clock():
    c = _Clock()
    with (
        patch("custom_components.climate_advisor.automation.dt_util.now", side_effect=c.now),
        patch("custom_components.climate_advisor.automation.callback", side_effect=lambda fn: fn),
        patch("custom_components.climate_advisor.automation.async_call_later", side_effect=c.schedule),
    ):
        yield c
    for coro in c.tasks:  # tasks the engine spawned that the test never ran: close, never leak
        coro.close()


async def _episode(
    clock: _Clock,
    route: str,
    *,
    lag: bool = True,
    fan_mode: str = FAN_MODE_WHOLE_HOUSE,
    firmware_follows_remote: bool = True,
    hvac: bool = False,
    observe_s: float = OBSERVE_S,
    drop_off: bool = False,
    hook=None,
):
    """Run one sealed-house stop for ``observe_s`` virtual seconds, driving the engine as the coordinator does."""
    follows = firmware_follows_remote
    if route == "ca_started_window_closed":
        plant = _Plant(clock, control="on", physical_on=True, on_for_s=300.0, lag=lag, firmware_follows_remote=follows)
    elif route == "rf_override_window_closed":
        # RF press: firmware option 1 makes the control entity follow the remote's power state; old firmware reads off
        plant = _Plant(
            clock,
            control="on" if follows else "off",
            physical_on=True,
            on_for_s=300.0,
            lag=lag,
            firmware_follows_remote=follows,
            drop_off=drop_off,
        )
    else:  # rf_press_sealed
        plant = _Plant(
            clock,
            control="on" if follows else "off",
            physical_on=True,
            on_for_s=0.0,
            lag=lag,
            firmware_follows_remote=follows,
            drop_off=drop_off,
        )
    engine = _engine(plant, fan_mode, clock, hvac=hvac)
    if route != "ca_started_window_closed":
        engine.handle_fan_manual_override("off", "on")  # the real override entry the fan listener calls

    real_sleep = asyncio.sleep

    async def _virtual_sleep(seconds, *a, **kw):
        clock.advance(seconds)
        await real_sleep(0)

    async def _drive() -> None:
        seen = (plant.control, plant.physical_on)
        t0 = clock.now()
        await engine.enforce_whf_safety(
            "sensor_closed" if route != "rf_press_sealed" else "fan_entity", sealed_confirmed=route != "rf_press_sealed"
        )
        settled = route != "rf_press_sealed"
        next_tick = t0 + timedelta(seconds=15)
        while (clock.now() - t0).total_seconds() < observe_s:
            clock.advance(1)
            plant.settle()
            if not settled and (clock.now() - t0).total_seconds() >= 10.5:
                settled = True
                await engine.enforce_whf_safety("settle")
            now_seen = (plant.control, plant.physical_on)
            if now_seen != seen:
                seen = now_seen
                await engine.enforce_whf_safety("fan_entity")
            if clock.now() >= next_tick:
                next_tick += timedelta(seconds=15)
                await engine.enforce_whf_safety("tick")
            await clock.fire_due()  # the verify / quiet-window / forced-start timers
            if hook is not None:
                await hook(clock, engine, plant)
            seen = (plant.control, plant.physical_on)

    with patch("custom_components.climate_advisor.automation.asyncio.sleep", new=_virtual_sleep):
        await _drive()
    return plant, engine


def _run(coro):
    return asyncio.run(coro)


ROUTES = ["rf_override_window_closed", "rf_press_sealed", "ca_started_window_closed"]


# ---- The occupant outcomes (were strict xfail reproductions of the investigation) -------------------------------
@pytest.mark.parametrize("route", ROUTES)
def test_a_sealed_house_fan_is_physically_off_within_the_rule_1_bound(clock, route) -> None:
    plant, _ = _run(_episode(clock, route))
    bound = 40.0 if route == "rf_press_sealed" else 30.0  # 10 s settle after a press, none after a debounced close
    off_after = next(((t - T0).total_seconds() for t, on in plant.physical_log if not on), None)
    if off_after is None or off_after > bound:
        raise _OccupantOutcomeViolated(
            f"route={route}: the whole-house fan was still physically running {OBSERVE_S} s after the house was "
            f"sealed (bound {bound:.0f} s); CA sent {plant.count('turn_off')} turn_off and "
            f"{plant.count('turn_on')} turn_on in that time"
        )


@pytest.mark.parametrize("route", ROUTES)
def test_a_sealed_house_stop_does_not_turn_the_fan_back_on(clock, route) -> None:
    """ZERO ``turn_on`` for a stop (the investigation tolerated one #449 opposite-first transition)."""
    plant, _ = _run(_episode(clock, route))
    turn_ons = plant.count("turn_on")
    if turn_ons > 0:
        raise _OccupantOutcomeViolated(
            f"route={route}: Climate Advisor sent {turn_ons} fan.turn_on commands while stopping the fan on a "
            f"sealed house; commands={[(f'{(t - T0).total_seconds():.0f}s', s) for t, s in plant.commands]}"
        )


@pytest.mark.parametrize("route", ROUTES)
def test_a_sealed_house_stop_is_a_bounded_number_of_commands(clock, route) -> None:
    plant, _ = _run(_episode(clock, route))
    if len(plant.commands) > 3:
        raise _OccupantOutcomeViolated(
            f"route={route}: {len(plant.commands)} fan commands in {OBSERVE_S} s to stop one fan (expected <= 3)"
        )
    assert [s for _, s in plant.commands] == ["turn_off"], "a working install needs exactly one plain turn_off"


# ---- Step 5: the HVAC restore waits for the fan to stay off ----------------------------------------------------
@pytest.mark.parametrize("route", ROUTES)
def test_the_hvac_restore_waits_for_the_fan_to_read_off_and_stay_off(clock, route) -> None:
    """15:12:35 the old code restored heat the moment the fan first read off, then restarted the fan (11 s of both)."""
    plant, engine = _run(_episode(clock, route, hvac=True, observe_s=60))
    off_at = next(t for t, on in plant.physical_log if not on)
    assert len(clock.restores) == 1, f"the pre-fan mode must be given back exactly once: {clock.restores}"
    waited = (clock.restores[0] - off_at).total_seconds()
    assert waited >= WHF_RESTORE_QUIET_S, (
        f"HVAC restored {waited:.1f} s after the fan read off (quiet {WHF_RESTORE_QUIET_S} s)"
    )
    assert waited <= WHF_RESTORE_QUIET_S + 16.0, f"HVAC restore was never re-asked promptly: waited {waited:.1f} s"
    assert engine._pre_fan_hvac_mode is None


def test_the_hvac_restore_is_not_given_while_the_fan_still_reads_on(clock) -> None:
    plant, _ = _run(_episode(clock, "ca_started_window_closed", hvac=True, observe_s=12))
    assert plant.physical_on is True and clock.restores == [], "heat/cool came back under a fan that was still on"


def test_a_fan_that_restarts_during_the_quiet_window_is_never_met_by_a_restored_thermostat(clock) -> None:
    """The quiet window exists for this: a restart (anything commanding the fan on while the stop is confirming) shows
    up as 'on' again within the 5.5 s on-lag, restarting the wait."""
    plant, engine = _run(_episode(clock, "ca_started_window_closed", hvac=True, observe_s=18))
    off_at = next(t for t, on in plant.physical_log if not on)
    # the occupant presses the remote on 2 s after the fan first read off (new firmware: entity and power follow)
    clock.t = max(clock.t, off_at) + timedelta(seconds=2)
    plant.control, plant.physical_on, plant.physical_since = "on", True, clock.now()
    plant.physical_log.append((clock.now(), True))
    assert clock.restores == [], "restored inside the quiet window"
    assert engine._whf_safety_release_blocked() is True


# ---- Step 1/2: old firmware -> loud, never an "on" first --------------------------------------------------------
@pytest.mark.parametrize("route", ["rf_override_window_closed", "rf_press_sealed"])
def test_old_firmware_a_plain_off_that_transmits_nothing_is_reported_loudly_and_never_turns_the_fan_on(
    clock, route, caplog
) -> None:
    """The entity reads off (a remote start the firmware does not reflect), the fan is on, the plain off is a no-op."""
    alerts: list[tuple[str, str]] = []
    with caplog.at_level(logging.INFO):
        plant, engine = _run(_episode_with_alerts(clock, route, alerts, firmware_follows_remote=False, observe_s=80))
    assert plant.physical_on is True, "premise: old firmware cannot be stopped by a plain off"
    assert plant.count("turn_on") == 0, "CA must never turn the fan on to stop it (on-then-off restarts it)"
    assert len(plant.commands) <= 1 + WHF_STOP_MAX_RETRIES, plant.commands
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "could not be stopped" in r.getMessage()]
    assert warnings, "no WARNING for a stop that cannot take effect"
    assert "control entity already reads off (firmware may not reflect remote power state)" in warnings[0].getMessage()
    assert [a for a in alerts if "could not be stopped" in a[0]], f"no occupant alert: {alerts}"
    assert engine._whf_stop_failure_alerted is True


def test_old_firmware_after_the_retries_the_enforcer_holds_off_and_then_tries_again(clock) -> None:
    alerts: list[tuple[str, str]] = []
    hold = WHF_STOP_GIVEUP_HOLD_S
    plant, _ = _run(
        _episode_with_alerts(
            clock, "rf_override_window_closed", alerts, firmware_follows_remote=False, observe_s=hold + 120
        )
    )
    times = [(t - T0).total_seconds() for t, _ in plant.commands]
    # first episode: attempts at ~0, ~21, ~42 s; then one hold of WHF_STOP_GIVEUP_HOLD_S; then a fresh episode
    assert len([t for t in times if t < hold]) == 1 + WHF_STOP_MAX_RETRIES, times
    assert len(times) <= 2 * (1 + WHF_STOP_MAX_RETRIES), times
    assert plant.count("turn_on") == 0
    failures = [a for a in alerts if "could not be stopped" in a[0]]
    assert len(failures) == 1, f"the occupant is told once about the failure, not every attempt: {failures}"


async def _episode_with_alerts(clock, route, alerts, **kw):
    """``_episode`` with the occupant alert path captured (patched on the class so the engine built inside sees it)."""
    with patch.object(
        AutomationEngine,
        "_notify_whf_safety",
        autospec=True,
        side_effect=lambda self, title, message: alerts.append((title, message)),
    ):
        return await _episode(clock, route, **kw)


# ---- Step 3: the start direction -------------------------------------------------------------------------------
def test_a_ca_start_is_not_undone_by_a_re_entry_inside_the_spin_up_lag(clock) -> None:
    """Mirror A: re-entry within the power-reading lag used to send off, then on, to a fan CA had just started."""
    plant = _Plant(clock, control="off", physical_on=False, on_for_s=600.0)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    engine._sensor_check_callback = lambda: True  # a window is open: CA may start the fan
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=plant.physical_on, fan_on_for_s=None, sensors=(SensorReading(is_open=True, stable_for_s=3600.0),)
    )
    assert _run(engine._activate_fan(reason="nat_vent")) is not FanCommandResult.SUPPRESSED
    clock.advance(2)  # the transmitter entity reads on, the power reading will say so in 3.5 s
    engine._fan_active = False  # a session flag lost and re-derived (a reconcile): the second entry
    _run(engine._activate_fan(reason="re-entry"))
    assert [s for _, s in plant.commands] == ["turn_on"], plant.commands


@pytest.mark.parametrize("sealed_meanwhile", [False, True])
def test_a_start_over_a_stale_on_entity_forces_a_real_transition_but_never_into_a_sealed_house(
    clock, sealed_meanwhile
) -> None:
    """The start-direction evidence: after a wall switch the entity reads on and the fan is off (#449). The off now,
    the on 5 s later outside the lock; and the same sequence with the house sealed in between sends no turn_on."""
    plant = _Plant(clock, control="on", physical_on=False, on_for_s=3600.0)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    sensors = [SensorReading(is_open=True, stable_for_s=3600.0)]
    engine._sensor_check_callback = lambda: True
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=plant.physical_on, fan_on_for_s=None, sensors=tuple(sensors)
    )

    async def _go():
        await engine._activate_fan(reason="nat_vent")
        assert [s for _, s in plant.commands] == ["turn_off"], "first step: the real transition, no sleep"
        if sealed_meanwhile:
            sensors[:] = [SensorReading(is_open=False, stable_for_s=3600.0)]
        clock.advance(5.5)
        await clock.fire_due()

    _run(_go())
    if sealed_meanwhile:
        assert plant.count("turn_on") == 0, "turn_on into a sealed house"
    else:
        assert [s for _, s in plant.commands] == ["turn_off", "turn_on"]


# ---- Positive controls (must pass) -----------------------------------------------------------------------------
@pytest.mark.parametrize("route", ROUTES)
def test_positive_control_a_device_that_reports_off_at_once_is_stopped_cleanly(clock, route) -> None:
    """No detect lag: the stop converges after its one plain stop.

    This is why the existing suites never saw the bug: their fan reports the stop in the same instant.
    """
    plant, _ = _run(_episode(clock, route, lag=False))
    assert plant.physical_on is False
    assert plant.count("turn_on") == 0
    assert len(plant.commands) <= 3


def test_positive_control_hvac_fan_mode_is_not_subject_to_the_sealed_house_rule(clock) -> None:
    """``hvac_fan`` is out of scope for Rule 1 (the blower is not the whole-house fan): nothing is commanded."""
    plant, engine = _run(_episode(clock, "rf_override_window_closed", fan_mode=FAN_MODE_HVAC))
    assert plant.commands == []
    assert engine.config[CONF_FAN_MODE] == FAN_MODE_HVAC


def test_the_convergence_window_covers_the_measured_production_lag() -> None:
    assert OFF_LAG_S < WHF_STOP_CONVERGE_WINDOW_S < 30.0, "inside Rule 1's bound, past the 16.0 s worst case seen"


# ---- The stop memory and the predicates built on it ----------------------------------------------------------
def _inputs(**kw):
    base = {
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "fan_on": True,
        "fan_believed_on": False,
        "fan_on_for_s": 300.0,
        "sensors": (SensorReading(is_open=False, stable_for_s=3600.0),),
        "sealed_confirmed": True,
        "thermostat_mode": "off",
        "occupant_fan_on_at": None,
        "occupant_hvac_on_at": None,
        "whf_owns_hvac": False,
    }
    base.update(kw)
    return WhfSafetyInputs(**base)


def test_the_pure_decision_leaves_a_converging_stop_alone_and_stops_again_after_the_window() -> None:
    assert decide_whf_safety(_inputs()).action is WhfSafetyAction.STOP_SEALED
    converging = decide_whf_safety(_inputs(stop_in_flight_for_s=8.0))
    assert converging.action is WhfSafetyAction.NONE and converging.in_flight is True
    assert decide_whf_safety(_inputs(stop_in_flight_for_s=None)).action is WhfSafetyAction.STOP_SEALED


def test_the_stop_memory_is_one_predicate_and_confirms_on_the_first_physical_off(clock) -> None:
    plant = _Plant(clock, control="on", physical_on=True, on_for_s=300.0)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    assert engine._whf_stop_in_flight() is None
    _run(engine._deactivate_fan(reason="t", restore_hvac=False, safety=True))
    clock.advance(5)
    assert engine._whf_stop_in_flight() == pytest.approx(5.0)
    assert engine._whf_physically_running_for_safety() is False, "inside the window the lag is not a running fan"
    assert engine.last_whf_command_direction() == "off"
    clock.advance(OFF_LAG_S - 5 + 0.1)  # the power reading catches up (inside the window): confirmed
    assert engine._whf_stop_in_flight() is None
    plant.physical_on = True  # and a later 'on' is a real change, not lag
    assert engine._whf_stop_in_flight() is None
    assert engine._whf_physically_running_for_safety() is True


def test_the_stop_window_ends_and_the_command_direction_ages_out(clock) -> None:
    plant = _Plant(clock, control="on", physical_on=True, on_for_s=300.0, lag=False)
    plant.physical_on = True  # keep it reading on: a stop that did not take
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    engine._record_whf_command("off", control_was_on=True)
    clock.advance(WHF_STOP_CONVERGE_WINDOW_S - 1)
    assert engine._whf_stop_in_flight() is not None
    clock.advance(2)
    assert engine._whf_stop_in_flight() is None
    assert engine.last_whf_command_direction() == "off"
    clock.advance(30)
    assert engine.last_whf_command_direction() is None


def test_echo_helpers_truth_table() -> None:
    for token in ("on", "low", "medium", "high", "timer_1h", "timer_none"):
        assert remote_event_is_echo_of("off", token) is False
        assert remote_event_is_echo_of("on", token) is True
        assert remote_event_is_echo_of(None, token) is True
    assert remote_event_is_echo_of("off", "off") is True
    assert remote_event_is_echo_of("on", "off") is False
    assert remote_event_is_echo_of("cycle", "off") is False  # an occupant off during a forced start is never an echo
    assert remote_event_is_echo_of("cycle", "low") is True
    assert fan_transition_is_echo_of("off", True) is False
    assert fan_transition_is_echo_of("off", False) is True
    assert fan_transition_is_echo_of("on", True) is True
    assert fan_transition_is_echo_of("on", False) is False
    assert fan_transition_is_echo_of(None, True) is True


def test_mirror_a_window_opening_during_a_stop_ends_the_episode_and_nothing_is_retried(clock) -> None:
    """Sensor open vs close: the close starts a stop; an open during its convergence window makes the retry moot
    (Rule 1 no longer applies), so no second command, no failure report, and the episode counters reset."""
    plant = _Plant(clock, control="on", physical_on=True, on_for_s=300.0)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    sensors = [SensorReading(is_open=False, stable_for_s=3600.0)]
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=plant.physical_on, fan_on_for_s=300.0, sensors=tuple(sensors)
    )
    _run(engine.enforce_whf_safety("sensor_closed", sealed_confirmed=True))
    assert plant.count("turn_off") == 1
    clock.advance(5)
    sensors[:] = [SensorReading(is_open=True, stable_for_s=1.0)]  # the occupant opens a window again
    plant.pending = None  # the fan ignored the stop and is still running: with a window open that is allowed
    clock.advance(WHF_STOP_CONVERGE_WINDOW_S)
    _run(engine.enforce_whf_safety("stop_verify"))
    assert plant.count("turn_off") == 1 and plant.count("turn_on") == 0
    assert engine._whf_stop_attempts == 0 and engine._whf_stop_failure_alerted is False


# ---- Rule 2: every HVAC restore after a WHF stop goes through the ONE gated release ------------------------------
async def _reconcile_hook_factory(every_s: float = 300.0):
    state = {"next": None, "runs": 0}

    async def _hook(clock, engine, plant):
        if state["next"] is None:
            state["next"] = clock.now() + timedelta(seconds=every_s)
        if clock.now() >= state["next"]:
            state["next"] += timedelta(seconds=every_s)
            state["runs"] += 1
            # what the 5-minute untracked-fan reconcile does when it finds the fan "running without a warrant" (its
            # _exit_nat_vent ends in this restore_hvac=True _deactivate_fan call)
            engine._fan_active = True
            await engine._deactivate_fan(reason="reconcile — fan running without CA warrant", emit_event=False)

    return _hook, state


@pytest.mark.parametrize("variant", ["old_firmware", "lost_rf_off"])
def test_a_fan_that_cannot_be_stopped_never_gets_the_ac_back_and_the_reconcile_sends_no_command(clock, variant) -> None:
    """The blocker: ~+360 s the reconcile's blind pre-fan restore wrote cool beside a fan that was still running."""
    alerts: list[tuple[str, str]] = []

    async def _go():
        hook, state = await _reconcile_hook_factory(360.0)
        with patch.object(
            AutomationEngine,
            "_notify_whf_safety",
            autospec=True,
            side_effect=lambda self, title, message: alerts.append((title, message)),
        ):
            result = await _episode(
                clock,
                "rf_override_window_closed",
                hvac=True,
                observe_s=780,
                firmware_follows_remote=variant != "old_firmware",
                drop_off=variant == "lost_rf_off",
                hook=hook,
            )
        return result, state

    (plant, engine), state = _run(_go())
    assert plant.physical_on is True, "premise: the stop cannot take effect"
    assert state["runs"] >= 2, "the reconcile ran"
    assert clock.restores == [], (
        f"HVAC was restored while the fan still ran: {clock.restores}, fan_on={clock.restore_fan_on}"
    )
    assert engine._pre_fan_hvac_mode == "cool", (
        "the suppression must survive: nothing may release it under a running fan"
    )
    assert engine._whf_restore_pending is True
    assert plant.count("turn_on") == 0
    times = [(t - T0).total_seconds() for t, _ in plant.commands]
    # 3 enforcer attempts in the first ~45 s, then the 300 s hold: no command at all from 60 s to the hold's end,
    # in particular none from the reconcile at +300 s (it used to bypass the hold with a plain off)
    assert [t for t in times if 60 <= t < 300] == [], times
    assert [t for t in times if 300 <= t < 330] == [] or len(times) <= 7, times
    # the occupant finally stops it at the wall: the gated release runs once, after the quiet window
    plant.physical_on, plant.physical_since = False, clock.now()
    plant.physical_log.append((clock.now(), False))

    async def _after():
        for _ in range(40):
            clock.advance(1)
            await engine.enforce_whf_safety("tick")
            await clock.fire_due()

    _run(_after())
    assert len(clock.restores) == 1 and clock.restore_fan_on == [False], (clock.restores, clock.restore_fan_on)
    assert engine._pre_fan_hvac_mode is None and engine._whf_restore_pending is False


def _forced_start_engine(clock):
    plant = _Plant(clock, control="on", physical_on=False, on_for_s=3600.0)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock)
    engine._sensor_check_callback = lambda: True
    engine._whf_safety_snapshot_callback = lambda: WhfSafetySnapshot(
        fan_on=plant.physical_on, fan_on_for_s=None, sensors=(SensorReading(is_open=True, stable_for_s=3600.0),)
    )
    return plant, engine


def test_an_occupant_action_during_the_forced_start_cycle_aborts_the_turn_on(clock) -> None:
    """Mirror of the echo guard: an RF off pressed between step 1 (off) and step 2 (on) must not be undone."""
    plant, engine = _forced_start_engine(clock)

    async def _go():
        await engine._activate_fan(reason="nat_vent")
        assert [s for _, s in plant.commands] == ["turn_off"]
        clock.advance(2)
        engine.note_occupant_fan_action()  # what the RF listener does for any non-echo remote event
        clock.advance(3.5)
        await clock.fire_due()

    _run(_go())
    assert [s for _, s in plant.commands] == ["turn_off"], "CA turned the fan on after the occupant acted"


def test_ca_does_not_start_a_fan_the_occupant_is_in_the_middle_of_starting(clock) -> None:
    """RF on seen (transmitter on, power still reading off for 5-20 s): CA must not send off-then-on to it."""
    plant, engine = _forced_start_engine(clock)
    engine.note_occupant_fan_action()
    clock.advance(8)
    result = _run(engine._activate_fan(reason="nat_vent"))
    assert result is FanCommandResult.SUPPRESSED and plant.commands == []
    clock.advance(WHF_OCCUPANT_PRESS_PENDING_S)  # settled: the ordinary start path is open again
    assert _run(engine._activate_fan(reason="nat_vent")) is not FanCommandResult.SUPPRESSED


# ---- Occupant's remote OFF inside the forced-start gap (change A) -------------------------------------------------
def test_an_occupant_off_during_the_forced_start_gap_starts_the_fan_off_pause_and_ca_does_not_restart_it(clock) -> None:
    """The occupant presses OFF on the remote right as CA starts the fan: it stays off, and CA stays away.

    Without the pause, CA's start step 1 (off) left the fan off, the occupant's off was swallowed, and the next
    nat-vent tick started the fan again right after the person turned it off.
    """
    plant, engine = _forced_start_engine(clock)

    async def _go():
        await engine._activate_fan(reason="nat_vent")
        assert [s for _, s in plant.commands] == ["turn_off"]
        assert engine._fan_active is True
        clock.advance(2)
        engine.note_occupant_fan_action()  # the coordinator's remote "off" handler
        clock.advance(3.5)
        await clock.fire_due()
        return engine._fan_active

    fan_active = _run(_go())
    assert [s for _, s in plant.commands] == ["turn_off"], "CA turned the fan on after the occupant's off"
    assert fan_active is False, "CA's session must close on the occupant's off"
    assert engine._fan_off_hold_active() is True, "the occupant's off must start the fan-off pause"
    clock.advance(WHF_OCCUPANT_PRESS_PENDING_S)  # press-pending is over: only the hold can refuse now
    assert engine._occupant_fan_action_pending() is False
    assert _run(engine._activate_fan(reason="nat_vent")) is FanCommandResult.SUPPRESSED
    assert [s for _, s in plant.commands] == ["turn_off"], "a later nat-vent start must send nothing"


def test_an_occupant_on_press_during_the_forced_start_gap_is_an_override_not_a_fan_off_pause(clock) -> None:
    """Control: the occupant presses ON in the gap. The fan they started keeps running; no off-pause appears."""
    plant, engine = _forced_start_engine(clock)

    async def _go():
        await engine._activate_fan(reason="nat_vent")
        clock.advance(2)
        engine.handle_fan_manual_override("off", "on", is_remote_event=True)
        clock.advance(3.5)
        await clock.fire_due()

    _run(_go())
    assert engine._fan_override_active is True
    assert engine._fan_off_hold_active() is False, "an ON press must not start the fan-off pause"
    assert [s for _, s in plant.commands] == ["turn_off"], "CA must not send the second step over the occupant's press"


# ---- Press-pending refusal reads as "no working fan" (change B) ---------------------------------------------------
def test_a_press_still_settling_refuses_the_start_as_no_working_fan_so_the_window_pauses_hvac(clock) -> None:
    """Occupant opens a window just after pressing the fan remote: the AC/heat still pauses for the open window.

    The refusal used to read as an occupant override, which kept heat/cool as they were beside an open window.
    """
    from custom_components.climate_advisor.automation import _NAT_VENT_NO_WORKING_FAN_RESULTS
    from tests.test_door_pause_recorded_1132 import _DAY
    from tests.test_door_pause_recorded_1132 import _engine as _nv_engine
    from tests.test_door_pause_recorded_1132 import _open as _nv_open

    engine = _nv_engine(mode="cool")
    engine.config[CONF_FAN_ENTITY] = "fan.whf"
    engine._occupant_fan_action_at = _DAY  # the RF press was seen this very instant; the power reading lags
    with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=_DAY):
        result = _run(engine._activate_fan(reason="nat_vent"))
    assert result is FanCommandResult.SUPPRESSED and result in _NAT_VENT_NO_WORKING_FAN_RESULTS
    _nv_open(engine)
    assert engine._paused_by_door is True, "rule 3: heat/cool pauses for the open window"
    assert engine._test_state.state == "off"
    assert engine._natural_vent_active is False


# ---- Dual-entity transmitter flip marks the occupant's start on its way (change C) --------------------------------
class TestDualEntityTransmitterFlipNotesOccupantAction:
    """A HomeKit / HA UI start flips the transmitter now and the power reading follows: CA must not read it as drift."""

    @staticmethod
    def _flip(*, pending: bool = False, ca_context: bool = False):
        from tests.test_fan_command_guard import _NOW, _fixed_now, _make_coord, _make_fan_entity_event

        coord = _make_coord(fan_command_time=None)
        coord.config["fan_entity"] = "fan.whf"
        coord.config["fan_state_entity"] = "binary_sensor.whf_power"
        ae = coord.automation_engine
        ae._fan_command_pending = pending
        ae.fan_command_context_matches = MagicMock(return_value=ca_context)
        event = _make_fan_entity_event("off", "on")
        event.data["entity_id"] = "fan.whf"
        with _fixed_now(_NOW):
            _run(coord._async_fan_entity_changed(event))
        return ae

    def test_a_non_ca_transmitter_flip_is_noted_but_is_not_an_override(self) -> None:
        ae = self._flip()
        ae.note_occupant_fan_action.assert_called_once()
        ae.handle_fan_manual_override.assert_not_called()

    def test_a_flip_carrying_ca_s_own_context_is_not_noted(self) -> None:
        ae = self._flip(ca_context=True)
        ae.note_occupant_fan_action.assert_not_called()
        ae.handle_fan_manual_override.assert_not_called()

    def test_a_flip_while_ca_s_command_is_pending_is_not_noted(self) -> None:
        ae = self._flip(pending=True)
        ae.note_occupant_fan_action.assert_not_called()
        ae.handle_fan_manual_override.assert_not_called()


# ---- Only a real occupant WHF action stamps the press-pending guard (change D) ------------------------------------
def test_a_fan_off_inside_the_rf_timer_settle_window_is_not_an_occupant_action(clock) -> None:
    """The RF timer ending is not a person at the fan: CA's next start is not held back by a phantom press."""
    _, engine = _forced_start_engine(clock)
    engine._timer_boundary_settle_until = clock.now() + timedelta(seconds=30)
    engine.on_fan_turned_off(fan_before="on", fan_after="off")
    assert engine._occupant_fan_action_at is None


def test_a_wrong_fan_off_is_not_an_occupant_action(clock) -> None:
    """The thermostat blower going off while a whole-house-fan override runs is not a press on the whole-house fan."""
    _, engine = _forced_start_engine(clock)
    engine.handle_fan_manual_override("off", "on")
    engine._occupant_fan_action_at = None  # forget the (real) press that created the override
    engine.on_fan_turned_off(fan_before="on", fan_after="off", hvac_fan=True)
    assert engine._occupant_fan_action_at is None


def test_a_restart_rearm_of_a_remote_timer_is_not_an_occupant_action(clock) -> None:
    """After an HA restart the re-armed RF-timer override is not a new press."""
    _, engine = _forced_start_engine(clock)
    engine.handle_fan_manual_override("off", "on", restored_from="rf_timer")
    assert engine._occupant_fan_action_at is None


def test_the_thermostat_blower_turning_on_is_not_an_occupant_whf_action(clock) -> None:
    """The occupant running the thermostat blower says nothing about the whole-house fan."""
    _, engine = _forced_start_engine(clock)
    engine.handle_fan_manual_override("off", "on", hvac_fan=True)
    assert engine._occupant_fan_action_at is None


def test_a_plain_occupant_fan_off_and_a_whf_fan_on_are_occupant_actions(clock) -> None:
    """Control: a person turning the whole-house fan off or on does stamp the guard."""
    _, engine = _forced_start_engine(clock)
    engine.on_fan_turned_off(fan_before="on", fan_after="off")
    assert engine._occupant_fan_action_at == clock.now()
    _, engine = _forced_start_engine(clock)
    clock.advance(5)
    engine.handle_fan_manual_override("off", "on")
    assert engine._occupant_fan_action_at == clock.now()
