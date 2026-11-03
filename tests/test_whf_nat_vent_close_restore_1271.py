"""Issue #1271: the AC stays off for hours after whole-house-fan ventilation ends on a window close (#1215 regression).

Occupant framing: the whole-house fan has been ventilating a warm evening. She closes the window and the fan stops.
The AC never comes back: the house sat at 78 F against a 72 F ceiling for 2+ hours with the thermostat Off and every
HVAC write blocked ("whole-house fan still owns the thermostat"). Heat mirror: the same strand leaves a cold house
unheated.

Mechanism (real engine, real ``_exit_nat_vent`` -> ``_deactivate_fan``): #1215 made the HVAC restore of a fan stop wait
for the fan to read physically off (rule 2: the fan and the AC never run together). ``_deactivate_fan`` asks that
question BEFORE it sends its own ``turn_off``, so a nat-vent fan that is still running at that instant makes the
restore "deferred" -- but the deferral only forces the restore off; the one gated release
(``_complete_whf_safety_release``) runs only while ``_whf_restore_pending`` is set, and only the sealed-house safety
stop set it. The stop that came from the nat-vent exit never armed it, so the pre-fan mode stayed snapshotted forever.

Fixed: a deferred fan stop that actually goes out arms the same gated release. These drive the real engine with the
#1215 lagging-fan plant (dual-entity: transmitter entity flips at once, power reading lags ~15 s) and a no-lag plant
(single entity that follows the fan), and assert what the occupant gets: the pre-fan mode back once the fan reads off
and has stayed off, never beside a running fan, never when the fan cannot be stopped, a window, or the occupant's own
change.
"""

# ruff: noqa: F811  (the imported ``clock`` fixture is re-bound as a test argument)

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.const import FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.whf_safety_decision import WHF_RESTORE_QUIET_S
from tests.test_whf_sealed_stop_1215 import _engine, _Plant, clock  # noqa: F401, F811  (clock is a fixture)

# The callers of ``_deactivate_fan(restore_hvac=True / release_suppression=True)`` that can meet a still-running fan
# on a sealed house, each driven through its own public entry point.
CALLERS = ["nat_vent_exit_window_closed", "reconcile_no_warrant", "plain_default_stop", "comfort_floor_release"]
MODES = ["cool", "heat"]


def _session(clock, *, mode: str, lag: bool, drop_off: bool = False):
    plant = _Plant(clock, control="on", physical_on=True, on_for_s=1800.0, lag=lag, drop_off=drop_off)
    engine = _engine(plant, FAN_MODE_WHOLE_HOUSE, clock, hvac=True)
    engine._pre_fan_hvac_mode = mode  # the thermostat the nat-vent / fan session suppressed
    engine._fan_active = True
    engine._natural_vent_active = True
    engine._fan_on_since = clock.now()
    return plant, engine


async def _caller(engine, which: str) -> None:
    if which == "nat_vent_exit_window_closed":
        await engine._exit_nat_vent(reason="all windows closed")
    elif which == "reconcile_no_warrant":
        await engine._deactivate_fan(reason="reconcile — fan running without CA warrant", emit_event=False)
    elif which == "plain_default_stop":
        await engine._deactivate_fan(reason="min_runtime_cycle_complete")
    else:  # comfort_floor_release: the comfort-floor exit releases the suppression and lets the selector write the mode
        await engine._deactivate_fan(
            reason="nat-vent comfort floor exit", restore_hvac=True, release_suppression=True, emit_event=False
        )


async def _drive(clock, plant, engine, seconds: float, *, hook=None) -> None:
    """Drive the 15 s tick, the fan-entity listener and the engine timers as the coordinator does."""
    real_sleep = asyncio.sleep

    async def _virtual_sleep(s, *a, **kw):
        clock.advance(s)
        await real_sleep(0)

    with patch("custom_components.climate_advisor.automation.asyncio.sleep", new=_virtual_sleep):
        seen = (plant.control, plant.physical_on)
        t0 = clock.now()
        next_tick = t0
        while (clock.now() - t0).total_seconds() < seconds:
            clock.advance(1)
            plant.settle()
            now_seen = (plant.control, plant.physical_on)
            if now_seen != seen:
                seen = now_seen
                await engine.enforce_whf_safety("fan_entity")
            if clock.now() >= next_tick:
                next_tick = clock.now() + timedelta(seconds=15)
                await engine.enforce_whf_safety("tick")
            await clock.fire_due()
            if hook is not None:
                await hook()
            seen = (plant.control, plant.physical_on)


async def _close_and_stop(clock, plant, engine, which: str, *, seconds: float = 60.0, hook=None) -> None:
    await _caller(engine, which)
    await engine.enforce_whf_safety("sensor_closed", sealed_confirmed=True)  # the coordinator's call after the close
    await _drive(clock, plant, engine, seconds, hook=hook)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lag", [True, False], ids=["dual_entity_lagging_power", "single_entity_reads_off_at_once"])
@pytest.mark.parametrize("which", CALLERS)
def test_the_pre_fan_mode_comes_back_once_the_fan_reads_off_and_stays_off(clock, which, lag, mode) -> None:
    plant, engine = _session(clock, mode=mode, lag=lag)

    _run(_close_and_stop(clock, plant, engine, which, seconds=60.0))

    assert plant.physical_on is False, "premise: the fan was stopped"
    assert plant.count("turn_on") == 0
    assert len(clock.restores) == 1, f"{which}: the {mode} was not given back exactly once: {clock.restores}"
    assert clock.restore_fan_on == [False], "the thermostat came back under a running fan (rule 2)"
    off_at = next(t for t, on in plant.physical_log if not on)
    assert (clock.restores[0] - off_at).total_seconds() >= WHF_RESTORE_QUIET_S - 1e-6, "no quiet window (#1215)"
    assert engine._whf_owns_hvac() is False, "the fan still owns the thermostat after it stopped"
    assert engine._whf_restore_pending is False, "the gated release never completed"
    assert engine._pre_fan_hvac_mode is None


@pytest.mark.parametrize("which", CALLERS)
def test_a_fan_that_cannot_be_stopped_never_gets_the_thermostat_back(clock, which) -> None:
    """#1215's intent kept: if the fan really keeps running, HVAC must NOT come back (and nothing is dropped)."""
    plant, engine = _session(clock, mode="cool", lag=True, drop_off=True)

    _run(_close_and_stop(clock, plant, engine, which, seconds=120.0))

    assert plant.physical_on is True, "premise: the RF off is lost, the fan keeps running"
    assert clock.restores == [], f"{which}: heat/cool came back beside a running fan: {clock.restore_fan_on}"
    assert engine._pre_fan_hvac_mode == "cool", "the suppression must survive while the fan runs"
    assert engine._whf_owns_hvac() is True
    assert plant.count("turn_on") == 0


def test_a_window_opened_again_before_the_release_gets_no_hvac(clock) -> None:
    """Rule 3: no heat/cool into an open window. The suppression is released, no mode is written."""
    plant, engine = _session(clock, mode="cool", lag=True)
    state = {"open": False}
    engine._sensor_hold_callback = lambda: state["open"]

    async def _go():
        await _caller(engine, "nat_vent_exit_window_closed")
        await engine.enforce_whf_safety("sensor_closed", sealed_confirmed=True)
        state["open"] = True  # the window opens again while the fan is still coming to a stop
        await _drive(clock, plant, engine, 60.0)

    _run(_go())

    assert plant.physical_on is False
    assert clock.restores == [], f"cool was written into an open window: {clock.restores}"
    assert engine._whf_owns_hvac() is False, "the fan's hold on the thermostat was never released"
    assert engine._whf_restore_pending is False


def test_the_occupants_own_thermostat_change_wins_over_the_restore(clock) -> None:
    """Rule 2: the newest occupant action wins. Their Off is not overwritten; the suppression is still released."""
    plant, engine = _session(clock, mode="cool", lag=True)

    async def _go():
        await _caller(engine, "nat_vent_exit_window_closed")
        await engine.enforce_whf_safety("sensor_closed", sealed_confirmed=True)
        engine._manual_override_active = True  # she set the thermostat herself while the fan was stopping
        await _drive(clock, plant, engine, 60.0)

    _run(_go())

    assert clock.restores == [], "the occupant's own thermostat change was overwritten by the pre-fan mode"
    assert engine._whf_owns_hvac() is False
    assert engine._whf_restore_pending is False


def test_the_deferred_stop_ends_the_thermostatic_backstop_and_arms_the_verify_timer(clock) -> None:
    """The restore is the gated release's now: the backstop the stop used to end is ended, and its verify re-asks."""
    plant, engine = _session(clock, mode="cool", lag=True)
    cancelled: list[bool] = []
    timers: list[str] = []
    engine._cancel_fan_thermo_backstop = lambda: cancelled.append(True)
    real = engine._schedule_whf_safety_timer
    engine._schedule_whf_safety_timer = lambda attr, delay, trigger: (
        timers.append(trigger),
        real(attr, delay, trigger),
    )

    _run(_caller(engine, "nat_vent_exit_window_closed"))

    assert engine._whf_restore_pending is True and engine._pre_fan_hvac_mode == "cool"
    assert cancelled, "the nat-vent thermostatic backstop outlived the stopped session"
    assert "stop_verify" in timers


@pytest.mark.parametrize("in_flight", [True, False], ids=["stop_already_in_flight", "fan_flag_lost_fan_still_running"])
def test_a_fan_the_engine_no_longer_tracks_but_still_reads_on_does_not_strand_the_restore(clock, in_flight) -> None:
    """Mirror: ``_fan_active`` already reads off (cycling-off, a stop still converging) yet the power reading says on.

    The deferred restore must not be dropped on the early already-in-state return either.
    """
    plant, engine = _session(clock, mode="cool", lag=True)
    engine._fan_active = False
    if in_flight:
        _run(plant.service("fan", "turn_off", {}))  # the off CA sent a moment ago; the power reading has not caught up
        engine._record_whf_command("off", control_was_on=True)

    _run(_close_and_stop(clock, plant, engine, "nat_vent_exit_window_closed", seconds=90.0))

    assert plant.physical_on is False, "premise: the fan ends up off"
    assert len(clock.restores) == 1 and clock.restore_fan_on == [False], (clock.restores, clock.restore_fan_on)
    assert engine._whf_owns_hvac() is False and engine._whf_restore_pending is False


@pytest.mark.parametrize("who", ["fan_flickers_back_on", "occupant_turns_the_fan_back_on"])
def test_a_fan_that_comes_back_on_inside_the_quiet_window_restarts_the_wait(clock, who) -> None:
    """#1215 intent / rule 2: the release never races a fan that reads off and then on again.

    The fan reads off, then (inside the quiet window) reads on again for a few seconds -- a flicker of the power
    reading, or the occupant's wall switch -- and then off again. Heat/cool must never come back while it runs, and
    comes back only once the fan has read off again for the FULL quiet window (counted from the second off).
    """
    plant, engine = _session(clock, mode="cool", lag=True)
    state = {"on_at": None, "off_again": False}

    async def _hook():
        if state["off_again"]:
            return
        if state["on_at"] is None:
            off_at = next((t for t, on in plant.physical_log if not on), None)
            if off_at is not None and not plant.physical_on and (clock.now() - off_at).total_seconds() >= 3:
                state["on_at"] = clock.now()
                if who == "occupant_turns_the_fan_back_on":
                    plant.control = "on"
                plant.physical_on, plant.physical_since = True, clock.now()
                plant.physical_log.append((clock.now(), True))
                await engine.enforce_whf_safety("fan_entity")
        elif (clock.now() - state["on_at"]).total_seconds() >= 3:
            state["off_again"] = True
            plant.control, plant.pending = "off", None
            plant.physical_on, plant.physical_since = False, clock.now()
            plant.physical_log.append((clock.now(), False))
            await engine.enforce_whf_safety("fan_entity")

    _run(_close_and_stop(clock, plant, engine, "nat_vent_exit_window_closed", seconds=120.0, hook=_hook))

    assert state["off_again"], "premise: the fan came back on inside the quiet window, then went off again"
    assert plant.physical_on is False
    assert clock.restore_fan_on == [False], f"heat/cool came back beside a running fan: {clock.restore_fan_on}"
    last_off = max(t for t, on in plant.physical_log if not on)
    waited = (clock.restores[0] - last_off).total_seconds()
    assert waited >= WHF_RESTORE_QUIET_S - 1e-6, f"restored {waited:.1f} s after the fan went off again (quiet race)"
    assert engine._whf_owns_hvac() is False and engine._whf_restore_pending is False
