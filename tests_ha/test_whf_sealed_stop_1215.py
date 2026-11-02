"""Real-HA tests for Issue #1215: a sealed-house whole-house-fan stop that ran away and restarted the fan.

Occupant story (2026-11-01, v0.8.22): the person starts the whole-house fan from the RF remote with every window
closed, or closes the last open window with the fan running. Owner Rule 1: the fan is off within 30 s of the
(10 s) settle and stays off. Instead the fan ran for ~3 minutes (15:04:04 to 15:07:01) while Climate Advisor sent 66
``fan.turn_off`` and 33 ``fan.turn_on`` commands, and when the window was closed at 15:12:14 the fan went off, back
ON (restarted by Climate Advisor itself), then off again.

Mechanism the house must model (it did not before this issue: ``tests_ha/house/fans.py`` changed the command and the
power entity in the same instant, so a stop was confirmed immediately and the loop could never start): the fan is
commanded through a one-way RF transmitter whose entity flips at once, while the physical fan is read through a
power-detect helper that lags the command (production history: on ~5.5 s after a command, off 15.3 s and 16.0 s
after the last off). The shared house fan now has that as an opt-in
(``house.fan.enable_realistic_lag()``, default off so every other suite is unchanged), including the firmware
choice: ``remote_updates_command_entity=True`` is the QuietCool firmware option 1 (the fan entity follows the
remote's real power state, so a remote-started fan reads "on" and a plain off is a real transition), ``False`` is the
old firmware (a remote-started fan still reads "off" and a plain off transmits nothing).

Fixed in v0.8.28: a stop is one plain ``turn_off`` (zero ``turn_on``), a converging stop is not decided again, the
HVAC restore waits for the fan to stay off, the RF echo guard is type-aware, and old firmware is reported loudly
(WARNING plus an occupant alert) instead of being "fixed" with an on-then-off that restarts the fan.

Fan modes: the sealed-house rule exists only for ``whole_house_fan`` (``hvac_fan`` and the unsupported ``both`` are
out of scope, #1175); the ``whf_dual`` house is the only shape with a separate physical reading, so the bug is
exercised there. The ``hvac_fan`` leg is covered by the unit twin (``tests/test_whf_sealed_stop_1215.py``).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

REMOTE = "event.house_quietcool_remote"
OFF_LAG_S = 15.5  # production: 15:06:45.9 -> 15:07:01.26 (15.3 s); 15:12:35.02 -> 15:12:51.07 (16.0 s)
QUIET_S = 6.0  # WHF_RESTORE_QUIET_S


class _OccupantOutcomeViolated(AssertionError):
    """The occupant's sealed house did not get the fan stopped and kept stopped (the #1215 symptom)."""


def _ca_fan(calls, service):
    return [c for c in calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == service]


def _sealed_house(**extra):
    ca_config = {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "fan_remote_entity": REMOTE}
    ca_config.update(extra.pop("ca_config", {}))
    cfg = {
        "contacts": ["window"],
        "startup": "settled",
        "initial_indoor_f": 72.0,
        "outdoor_f": 72.0,
        "high_f": 74.0,
        "low_f": 62.0,
        "ca_config": ca_config,
    }
    cfg.update(extra)
    return cfg


async def _lag_house(make_house, *, remote_follows: bool = True, **extra):
    """A whf_dual house whose fan behaves like the production device (lagged reading, firmware choice)."""
    house = await make_house(fan="whf_dual", **_sealed_house(**extra))
    house.fan.enable_realistic_lag(remote_updates_command_entity=remote_follows)
    return house


async def _stale_on_house(make_house):
    """A wall switch / power loss left the transmitter entity reading on while the fan is physically off (#449)."""

    async def _before(h):
        h.hass.states.async_set(h.fan.command_entity, "on", {"friendly_name": "Whole House Fan"})

    house = await make_house(fan="whf_dual", before_ca_setup=_before, **_sealed_house())
    house.fan.enable_realistic_lag()
    return house


async def _advance_finely(house, seconds: int) -> None:
    for _ in range(int(seconds)):
        await house.advance(seconds=1)


async def _press_after_refresh(house, on: bool = True):
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(on)


async def _sealed_press_episode(make_house, *, remote_follows: bool = True):
    """The 15:04 route: windows closed, the occupant presses the RF remote. Returns (house, t0, calls_before)."""
    house = await _lag_house(make_house, remote_follows=remote_follows)
    await house.advance(minutes=3)  # clear every start-up / 120 s expected-confirmation window
    calls_before = len(house.calls)
    await _press_after_refresh(house, True)
    return house, dt_util.utcnow(), calls_before


async def _window_close_episode(make_house, **kw):
    """The 15:12 route: window open, the occupant starts the fan by RF, then closes the window."""
    house = await _lag_house(make_house, **kw)
    await house.open("window")
    await house.advance(minutes=3)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 60)
    assert house.fan.is_on, "precondition: the fan runs with the window open"
    assert not _ca_fan(house.calls, "turn_off"), "precondition: no stop while the window is open"
    calls_before = len(house.calls)
    await house.close_contact("window")
    return house, dt_util.utcnow(), calls_before


async def _wind_down(house) -> None:
    """Break any stop attempt (a window opens) and cancel the device's pending lag so HA's teardown is clean."""
    house.fan.close()
    await house.open("window")
    await _advance_finely(house, 20)


def _first_physical_off_after(house, t0):
    return next((t for t, on in house.fan.physical_log if not on and t >= t0), None)


async def _observe(house, seconds: int) -> None:
    await _advance_finely(house, seconds)


# --------------------------------------------------------------------------------------------------------------
# Outcome 1: the fan is physically off inside Rule 1's bound (10 s settle after a press, 0 s after a window close
# which the debounce already confirmed, plus 30 s) and is not still running because Climate Advisor kept commanding.
# --------------------------------------------------------------------------------------------------------------
async def test_rf_press_on_a_sealed_house_is_off_within_the_rule_1_bound(make_house) -> None:
    house, t0, _ = await _sealed_press_episode(make_house)
    try:
        await _observe(house, 50)
        off_at = _first_physical_off_after(house, t0)
        if off_at is None or off_at - t0 > timedelta(seconds=10 + 30):
            raise _OccupantOutcomeViolated(
                "the whole-house fan was still physically running 50 s after the RF press on a sealed house "
                f"(Rule 1 bound: 10 s settle + 30 s); physical transitions={house.fan.physical_log!r}"
            )
    finally:
        await _wind_down(house)


async def test_closing_the_window_under_a_running_fan_is_off_within_30s(make_house) -> None:
    house, t0, _ = await _window_close_episode(make_house)
    try:
        await _observe(house, 50)
        off_at = _first_physical_off_after(house, t0)
        if off_at is None or off_at - t0 > timedelta(seconds=30):
            raise _OccupantOutcomeViolated(
                "the whole-house fan was not physically off within 30 s of the last window closing"
                f" (off_at={off_at}, close_at={t0}); physical transitions={house.fan.physical_log!r}"
            )
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Outcome 2: Climate Advisor does not restart the fan it is stopping (the "off, back on, off" the occupant saw).
# ZERO fan.turn_on for a stop (the investigation tolerated one #449 opposite-first transition).
# --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["rf_press_sealed", "window_closed"])
async def test_a_sealed_house_stop_does_not_turn_the_fan_back_on(make_house, route) -> None:
    episode = _sealed_press_episode if route == "rf_press_sealed" else _window_close_episode
    house, _t0, calls_before = await episode(make_house)
    try:
        await _observe(house, 50)
        turn_ons = _ca_fan(house.calls[calls_before:], "turn_on")
        if turn_ons:
            raise _OccupantOutcomeViolated(
                f"Climate Advisor sent {len(turn_ons)} fan.turn_on commands to the whole-house fan while stopping it "
                f"on a sealed house (route={route}); each restarts a fan that is spinning down: {turn_ons!r}"
            )
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Outcome 3: a stop is a bounded number of commands, not 66 in two minutes (3 stop commands per 5 s cycle).
# --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("route", ["rf_press_sealed", "window_closed"])
async def test_a_sealed_house_stop_is_a_bounded_number_of_commands(make_house, route) -> None:
    episode = _sealed_press_episode if route == "rf_press_sealed" else _window_close_episode
    house, _t0, calls_before = await episode(make_house)
    try:
        await _observe(house, 50)
        sent = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
        if len(sent) > 3:
            raise _OccupantOutcomeViolated(
                f"Climate Advisor sent {len(sent)} fan commands in 50 s to stop one fan on a sealed house "
                f"(route={route}); expected at most 3: {sent!r}"
            )
        assert [c.service for c in sent] == ["turn_off"], (
            f"a working install needs exactly one plain turn_off: {sent!r}"
        )
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Step 5: heat/cool is not given back until the fan has read physically off for the quiet window.
# --------------------------------------------------------------------------------------------------------------
async def test_the_hvac_restore_waits_for_the_fan_to_read_off_and_stay_off(make_house) -> None:
    """15:12:35 the old code restored heat the moment the fan first read off, then restarted the fan (11 s of both)."""
    house = await _lag_house(make_house, initial_hvac_mode="cool", initial_indoor_f=80.0, outdoor_f=70.0)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        await _press_after_refresh(house, True)  # fan on with cooling armed: the newest action wins, cooling is held
        await _advance_finely(house, 40)
        assert house.fan.is_on
        assert house.coordinator.automation_engine._pre_fan_hvac_mode is not None, "precondition: HVAC held for the fan"
        await house.close_contact("window")
        t0 = dt_util.utcnow()
        await _observe(house, 80)

        off_at = _first_physical_off_after(house, t0)
        assert off_at is not None, "the fan never stopped"
        restores = [
            c
            for c in house.calls
            if c.origin.startswith("ca:")
            and c.domain == "climate"
            and c.service == "set_hvac_mode"
            and c.at >= t0
            and c.data.get("hvac_mode") in ("cool", "heat", "heat_cool", "auto")
        ]
        assert restores, "the held thermostat mode was never given back after the stop"
        waited = (restores[0].at - off_at).total_seconds()
        assert waited >= QUIET_S - 1.0, (
            f"HVAC restored {waited:.1f} s after the fan read off (quiet window {QUIET_S} s)"
        )
        # 6 s quiet + the 5 s post-release settle (a timer since #1215) = about 11 s after the fan read off
        assert waited <= QUIET_S + 5.0 + 4.0, f"HVAC came back {waited:.1f} s after the fan read off (expected ~11 s)"
        assert not _ca_fan(house.calls[len(house.calls) - 1 :], "turn_on")
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Step 6: the 15:07 RF timer press 22 s after CA's stop is the occupant's, not an echo.
# --------------------------------------------------------------------------------------------------------------
def _set_remote(house, event_type: str) -> None:
    stamp = dt_util.utcnow().isoformat(timespec="milliseconds")
    house.hass.states.async_set(REMOTE, stamp, {"event_type": event_type, "event_types": ["low", "high", "off"]})


async def test_a_timer_press_22s_after_a_ca_stop_gets_the_occupants_hour_not_the_default(make_house) -> None:
    house, _t0, calls_before = await _sealed_press_episode(make_house)
    try:
        await _advance_finely(house, 14)  # the 10 s settle passes and CA stops the fan
        stop = _ca_fan(house.calls[calls_before:], "turn_off")
        assert stop, "precondition: CA stopped the sealed fan"
        stop_at = stop[0].at
        await house.open("window")  # the occupant opens a window and wants the fan: his one-hour press follows
        wait = 22 - (dt_util.utcnow() - stop_at).total_seconds()
        await _advance_finely(house, max(int(wait), 0))
        _set_remote(house, "timer_1h")
        await house.occupant_presses_fan(True)
        await _advance_finely(house, 20)

        ae = house.coordinator.automation_engine
        assert ae._fan_override_active, "the occupant's press 22 s after CA's stop was swallowed as an echo"
        assert ae._fan_remote_timer_hours == 1.0, (
            f"the one-hour timer press was dropped as an 'echo' (timer_hours={ae._fan_remote_timer_hours})"
        )
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Step 1/2: old firmware (the fan entity does not follow the remote). A plain off transmits nothing -> loud, and
# never an on-then-off.
# --------------------------------------------------------------------------------------------------------------
async def test_old_firmware_the_stop_that_cannot_take_effect_is_reported_and_never_turns_the_fan_on(
    make_house, caplog
) -> None:
    house, _t0, calls_before = await _sealed_press_episode(make_house, remote_follows=False)
    try:
        with caplog.at_level(logging.INFO):
            await _observe(house, 80)
        assert house.fan.is_on, "premise: with old firmware a plain off to an entity that reads off does nothing"
        assert not _ca_fan(house.calls[calls_before:], "turn_on"), "CA turned the fan on to stop it"
        sent = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
        assert len(sent) <= 3, sent
        assert house.fan.ignored_commands, "the device should have swallowed the plain off"
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "could not be stopped: control entity already reads off (firmware may not reflect remote power state)" in m
            for m in messages
        ), messages
        alerts = [n for n in house.notifications if "could not be stopped" in (n.data.get("message", "") + str(n.data))]
        assert alerts, f"no occupant alert: {[n.data for n in house.notifications]}"
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Step 3: the start direction. The #449 correction stays for a stale-on entity with a fan that is physically off
# (wall switch, power loss), gated: off now, on 5 s later outside the lock, never into a sealed house.
# --------------------------------------------------------------------------------------------------------------
async def test_a_stale_on_entity_with_the_fan_off_is_started_by_a_real_transition(make_house) -> None:
    house = await _stale_on_house(make_house)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        calls_before = len(house.calls)
        ae = house.coordinator.automation_engine
        await ae._activate_fan(reason="issue 1215 start-direction test")
        await _advance_finely(house, 20)
        sent = [c.service for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
        assert sent == ["turn_off", "turn_on"], sent
        assert house.fan.is_on, "the forced transition never reached the fan"
    finally:
        await _wind_down(house)


async def test_a_forced_start_never_turns_on_into_a_house_sealed_in_the_meantime(make_house) -> None:
    house = await _stale_on_house(make_house)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        calls_before = len(house.calls)
        ae = house.coordinator.automation_engine
        await ae._activate_fan(reason="issue 1215 start-direction test")
        await house.close_contact("window")  # sealed before the second step
        await _advance_finely(house, 20)
        sent = [c.service for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
        assert "turn_on" not in sent, sent
        assert not house.fan.is_on
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Transmitter entity listener (v0.8.28): on a dual-entity install only the power entity and the RF remote events
# detect the occupant. The firmware flips the transmitter on its own (remote presses at once, corrections after a lag).
# --------------------------------------------------------------------------------------------------------------
def _count_overrides(house):
    ae = house.coordinator.automation_engine
    original = ae.handle_fan_manual_override
    calls: list = []

    def _wrapped(*args, **kwargs):
        calls.append(kwargs.get("remote_timer_hours"))
        return original(*args, **kwargs)

    ae.handle_fan_manual_override = _wrapped
    return calls


async def test_example_a_firmware_flips_the_entity_off_after_a_failed_start_starts_no_hold(make_house) -> None:
    house = await _lag_house(make_house)
    house.fan.enable_realistic_lag(on_lag_s=10_000.0)  # the power never comes on
    try:
        await house.open("window")
        await house.advance(minutes=3)
        ae = house.coordinator.automation_engine
        await ae._activate_fan(reason="issue 1215 example A")
        await _advance_finely(house, 90)
        assert ae._fan_active and not house.fan.is_on, "precondition: CA believes the fan runs, the power never came on"
        house.hass.states.async_set(house.fan.command_entity, "off", {"friendly_name": "Whole House Fan"})  # firmware
        await house.hass.async_block_till_done()
        await _advance_finely(house, 3)
        assert ae._last_grace_trigger != "fan_off" and not ae._fan_override_active, (
            f"firmware correction read as the occupant turning the fan off (grace={ae._last_grace_trigger})"
        )
    finally:
        await _wind_down(house)


async def test_example_b_a_brief_on_flicker_from_a_lagging_off_starts_no_override(make_house) -> None:
    house = await _lag_house(make_house)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        ae = house.coordinator.automation_engine
        calls = _count_overrides(house)
        house.hass.states.async_set(house.fan.command_entity, "on", {"friendly_name": "Whole House Fan"})  # firmware
        await _advance_finely(house, 5)
        house.hass.states.async_set(house.fan.command_entity, "off", {"friendly_name": "Whole House Fan"})
        await _advance_finely(house, 5)
        assert not calls and not ae._fan_override_active and not ae._grace_active, calls
    finally:
        await _wind_down(house)


async def test_a_real_remote_press_is_detected_once(make_house) -> None:
    house = await _lag_house(make_house)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        calls = _count_overrides(house)
        _set_remote(house, "timer_1h")
        # The RF event reaches CA at once; the power reading follows seconds later (5-15 s on the real install), after
        # the burst window has flushed and is waiting for the fan to read on. The press below is that later reading
        # (the shared house sets power and transmitter together; the transmitter flip is ignored).
        await _advance_finely(house, 4)
        await house.occupant_presses_fan(True)
        await _advance_finely(house, 20)
        ae = house.coordinator.automation_engine
        assert ae._fan_override_active and ae._fan_remote_timer_hours == 1.0
        assert len(calls) == 1, f"the press was recorded {len(calls)} times: {calls}"
    finally:
        await _wind_down(house)


async def test_a_wall_switch_off_seen_only_on_the_power_entity_starts_the_hold(make_house) -> None:
    house = await _lag_house(make_house, remote_follows=False)  # a wall switch never touches the transmitter entity
    try:
        await house.open("window")
        await house.advance(minutes=3)
        ae = house.coordinator.automation_engine
        await house.occupant_presses_fan(True)  # power-only press: still an override
        await _advance_finely(house, 10)
        assert ae._fan_override_active, "a power-only turn-on was not detected"
        await house.occupant_presses_fan(False)
        await _advance_finely(house, 5)
        assert ae._grace_active and ae._last_grace_trigger == "fan_off", ae._last_grace_trigger
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Rule 2 (never together): a fan CA cannot stop must never run beside a restored AC, however long it lasts. The
# 5-minute untracked-fan reconcile used to write the pre-fan mode back (blind restore) about 6 minutes in.
# --------------------------------------------------------------------------------------------------------------
def _install_lost_rf_off(house) -> None:
    """Every RF off CA sends is lost: the transmitter entity flips, the fan never hears it."""
    fan = house.fan
    hass = house.hass

    async def _turn_off(call):
        hass.states.async_set(fan.command_entity, "off", {"friendly_name": "Whole House Fan"}, context=call.context)

    hass.services.async_register("fan", "turn_off", _turn_off)


@pytest.mark.parametrize("variant", ["old_firmware", "lost_rf_off"])
async def test_a_fan_that_cannot_be_stopped_never_gets_the_ac_back_for_ten_minutes(make_house, variant) -> None:
    house = await _lag_house(
        make_house,
        remote_follows=variant != "old_firmware",
        initial_hvac_mode="cool",
        initial_indoor_f=80.0,
        outdoor_f=70.0,
    )
    try:
        await house.advance(minutes=3)
        if variant == "lost_rf_off":
            _install_lost_rf_off(house)
        calls_before = len(house.calls)
        t0 = dt_util.utcnow()
        await _press_after_refresh(house, True)
        ae = house.coordinator.automation_engine
        for _ in range(660):  # 11 minutes in 1 s steps
            await house.advance(seconds=1)
            mode = house.hass.states.get("climate.house_thermostat").state
            if house.fan.is_on and mode != "off":
                raise _OccupantOutcomeViolated(
                    f"{(dt_util.utcnow() - t0).total_seconds():.0f} s in: HVAC mode {mode!r} while the fan still runs"
                )
        assert house.fan.is_on, "premise: the stop cannot take effect"
        fan_cmds = [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
        times = [(c.at - t0).total_seconds() for c in fan_cmds]
        assert not _ca_fan(house.calls[calls_before:], "turn_on")
        assert [t for t in times if 60 <= t < 300] == [], f"fan commands during the give-up hold: {times}"
        assert len(fan_cmds) <= 6, times
        assert ae._whf_restore_pending is True and ae._pre_fan_hvac_mode is not None
        # the occupant finally switches it off at the wall: the gated release runs once
        calls_mark = len(house.calls)
        house.fan.press(False)
        await house.hass.async_block_till_done()
        await _advance_finely(house, 40)
        restores = [
            c
            for c in house.calls[calls_mark:]
            if c.origin.startswith("ca:")
            and c.domain == "climate"
            and c.service == "set_hvac_mode"
            and c.data.get("hvac_mode") in ("cool", "heat", "heat_cool", "auto")
        ]
        assert restores, "the held thermostat mode was never given back once the fan really stopped"
        assert ae._whf_restore_pending is False and ae._pre_fan_hvac_mode is None
    finally:
        await _wind_down(house)


# --------------------------------------------------------------------------------------------------------------
# Positive control: with a window open the lag model alone causes no stop and no CA fan command, so the tests above
# are about the sealed-house path and not about the harness lag.
# --------------------------------------------------------------------------------------------------------------
async def test_positive_control_window_open_press_is_left_running_under_the_lag_model(make_house) -> None:
    house = await _lag_house(make_house)
    try:
        await house.open("window")
        await house.advance(minutes=3)
        calls_before = len(house.calls)
        await _press_after_refresh(house, True)
        await _advance_finely(house, 60)
        assert house.fan.is_on
        assert not [c for c in house.calls[calls_before:] if c.origin.startswith("ca:") and c.domain == "fan"]
    finally:
        await _wind_down(house)
