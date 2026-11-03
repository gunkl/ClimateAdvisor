"""Real-HA tests for the whole-house-fan safety enforcer (Issue #1144, phase 1).

Occupant story: a person turned the whole-house fan on (HomeKit, wall switch or RF remote) while every monitored
door and window was closed. The fan then ran for ~55 minutes pulling outdoor air through a sealed house because
Climate Advisor only looked for that situation on a few events and never when the fan was turned on while the house
was already sealed. Rule 1 (owner): the fan never runs on a sealed house and is off within 30 s, override or not.

Prove a test is not vacuous with ``scripts/revert_proof.py origin/main <test>``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.house


def _ca(calls):
    return [c for c in calls if c.origin.startswith("ca:")]


def _ca_fan_off(calls):
    return [c for c in _ca(calls) if c.domain == "fan" and c.service == "turn_off"]


def _sealed_house(**extra):
    cfg = {
        "contacts": ["window"],
        "startup": "settled",
        "initial_indoor_f": 72.0,
        "outdoor_f": 72.0,
        "high_f": 74.0,
        "low_f": 62.0,
        "ca_config": {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30},
    }
    cfg.update(extra)
    return cfg


async def _advance_finely(house, seconds: int) -> None:
    """Advance in 1 s slices. The harness steps its clock 30 s at a time, which would fire every Climate Advisor
    timer (10 s settle, 12 s verify, 15 s tick) at the end of the step and make a "within 30 s" claim untestable."""
    for _ in range(int(seconds)):
        await house.advance(seconds=1)


async def _press_after_refresh(house, on: bool = True):
    """Press the fan outside the coordinator's refresh cooldown (the separate #1130 race)."""
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await house.advance(seconds=3)
    await house.occupant_presses_fan(on)


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_fan_pressed_on_a_sealed_house_is_stopped_within_30s(make_house, fan) -> None:
    house = await make_house(fan=fan, **_sealed_house())
    assert not house.fan.is_on
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)

    assert _ca_fan_off(house.calls[calls_before:]), "CA never sent fan.turn_off for a fan running on a sealed house"
    assert not house.fan.is_on, "the whole-house fan is still running on a sealed house 30 s after it was turned on"
    ae = house.coordinator.automation_engine
    assert ae._fan_override_active is False, "a safety stop must end the fan override"
    assert ae._grace_active is False, "a safety stop must end the fan grace period"


async def test_positive_control_fan_pressed_with_window_open_keeps_running(make_house) -> None:
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    await _advance_finely(house, 60)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_sealed_stop_inside_the_300s_window_after_a_restart(make_house) -> None:
    """Start-up coalescing suppresses the fan listeners for 300 s; a sealed fan must still be stopped in 30 s."""
    house = await make_house(fan="whf", **_sealed_house())
    await house.restart(settle=False)
    await house.advance(seconds=20)
    calls_before = len(house.calls)

    await house.occupant_presses_fan(True)
    await _advance_finely(house, 45)

    assert _ca_fan_off(house.calls[calls_before:]), "no stop inside the post-restart coalescing window"
    assert not house.fan.is_on


async def test_occupant_repress_after_a_safety_stop_is_stopped_again(make_house) -> None:
    """HomeKit-style on/off/on right after a stop: the 300 s toggle rate limit must not shield the fan."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert not house.fan.is_on
    calls_before = len(house.calls)

    await house.occupant_presses_fan(True)
    await _advance_finely(house, 30)

    assert _ca_fan_off(house.calls[calls_before:]), "the second press was not stopped"
    assert not house.fan.is_on


async def test_window_closing_under_an_override_stops_the_fan(make_house) -> None:
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 60)
    assert house.fan.is_on
    calls_before = len(house.calls)

    await house.close_contact("window")
    await _advance_finely(house, 60)

    assert _ca_fan_off(house.calls[calls_before:])
    assert not house.fan.is_on


async def test_user_turning_heat_on_stops_an_override_only_fan(make_house) -> None:
    """Rule 2, newest action wins: the person sets heat while an override-only whole-house fan runs.

    Before #1144 the stand-down reported ALREADY_IN_STATE (``_fan_active`` is False for an override-only fan)
    and the fan kept running next to the heat.
    """
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert house.fan.is_on
    calls_before = len(house.calls)

    await house.occupant_sets_thermostat(mode="heat")
    await _advance_finely(house, 45)

    assert _ca_fan_off(house.calls[calls_before:]), "the fan kept running next to the occupant's heat"
    assert not house.fan.is_on


@pytest.mark.parametrize(
    ("contact", "press"),
    [("window", "heat"), ("window", "cool"), ("door", "heat")],
)
async def test_occupants_press_survives_the_next_cycle_while_a_contact_is_open(make_house, contact, press) -> None:
    """Issue #1158, rule 3 (+ rule 2): fan-override grace running, contact open, HVAC paused; the occupant presses
    heat/cool. The next update cycle used to turn it off again because the press never became a pending change
    (the override/grace FSM treated a press on a paused thermostat as a no-op while a protecting grace ran)."""
    house = await make_house(fan="whf", **_sealed_house(contacts=[contact]))
    await house.open(contact)
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert house.fan.is_on
    ae = house.coordinator.automation_engine
    assert ae._grace_active, "precondition: the fan override's grace period is running"

    await house.occupant_sets_thermostat(mode=press)
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()
    await _advance_finely(house, 5)

    assert house.thermostat.mode == press, "the next update cycle switched the occupant's change off"
    assert ae._override_confirm_pending, "the press must be a pending change that automation defers to"
    assert not house.fan.is_on, "rule 2: the newest occupant action (the press) wins, the fan must have stopped"


async def test_occupants_press_under_a_fan_grace_is_confirmed_and_ends_pending(make_house) -> None:
    """Once the confirmation window ends the press is a confirmed override and nothing stays pending (a pending
    flag the FSM could never clear would shield the thermostat from automation for good)."""
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    await house.occupant_sets_thermostat(mode="heat")
    await house.coordinator.async_request_refresh()
    await house.hass.async_block_till_done()

    await house.advance(minutes=12)  # past the 10 minute confirmation

    ae = house.coordinator.automation_engine
    assert not ae._override_confirm_pending
    assert ae._manual_override_active, "the heat press was confirmed as a manual override"
    assert house.thermostat.mode == "heat"


async def test_press_equal_to_the_days_plan_under_a_fan_grace_self_resolves_and_clears_pending(make_house) -> None:
    """A heat press on a cold day (heat IS the day's plan) while a fan-override grace runs is a transient change:
    at the end of the confirmation window it self-resolves. The pending flag must clear, and the running grace
    must stay, or the thermostat would stay shielded from automation for good."""
    from datetime import UTC, datetime

    house = await make_house(
        fan="whf",
        contacts=[],
        startup="settled",
        start=datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
        initial_indoor_f=62.0,
        outdoor_f=38.0,
        high_f=44.0,
        low_f=28.0,
        initial_hvac_mode="heat",
        ca_config={"fan_min_runtime_per_hour": 0, "override_confirm_seconds": 600},
    )
    ae = house.coordinator.automation_engine
    await house.advance(minutes=6)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 30)
    assert ae._grace_active, "precondition: the fan override's grace is running"

    await house.occupant_sets_thermostat(mode="heat")
    await house.advance(minutes=2)
    assert ae._override_confirm_pending, "precondition: the press is awaiting confirmation"
    await house.advance(minutes=10)

    assert not ae._override_confirm_pending, "a self-resolved press left the confirmation pending forever"


async def test_zero_configured_sensors_the_fan_is_never_stopped(make_house) -> None:
    """Owner decision 1: with no door/window sensor configured sealed-ness cannot be judged.

    Issue #1200: a whole-house fan with zero door/window sensors is an unsupported configuration; this pins
    today's unchanged behaviour, not a supported promise.
    """
    house = await make_house(fan="whf", **_sealed_house(contacts=[]))
    await _press_after_refresh(house, True)
    calls_before = len(house.calls)

    await _advance_finely(house, 90)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_window_opening_inside_the_10s_settle_is_not_fought(make_house) -> None:
    """Owner decision 3: someone who opens a window and presses the fan in one motion is not stopped."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    calls_before = len(house.calls)
    await _advance_finely(house, 4)

    await house.open("window")
    await _advance_finely(house, 60)

    assert not _ca_fan_off(house.calls[calls_before:])
    assert house.fan.is_on


async def test_unavailable_sensor_is_trusted_for_60s_then_counted_closed(make_house) -> None:
    """Owner decision 2: an unavailable sensor gets 60 s of benefit of the doubt, then counts as closed."""
    house = await make_house(fan="whf", **_sealed_house())
    await house.open("window")
    await house.advance(minutes=2)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 20)
    assert house.fan.is_on
    calls_before = len(house.calls)

    house.hass.states.async_set("binary_sensor.house_window", "unavailable")
    await _advance_finely(house, 45)
    assert not _ca_fan_off(house.calls[calls_before:]), "stopped before the sensor had been unavailable for 60 s"

    await _advance_finely(house, 45)
    assert _ca_fan_off(house.calls[calls_before:]), "an unavailable sensor never counted as closed"
    assert not house.fan.is_on


@pytest.mark.parametrize("seconds_after_press", [5, 12, 16, 20])
async def test_no_lingering_timers_after_unload(make_house, seconds_after_press) -> None:
    """Unload at every stage of an episode (settling, just stopped with the verify timer armed, later)."""
    house = await make_house(fan="whf", **_sealed_house())
    await _press_after_refresh(house, True)
    await _advance_finely(house, seconds_after_press)
    await house.close()  # the harness' verify_cleanup fails the test on any lingering timer


# --------------------------------------------------------------------------------------------------------------------
# Issue #1288: the rule-2 state backstop. The press stamps above can be missed; device state cannot.
# --------------------------------------------------------------------------------------------------------------------

_COMPRESSOR = ("heating", "cooling")


def _tee_events(house) -> list:
    """Tee the engine's emitted events (``whf_rule2_backstop`` ...) into a list the test reads."""
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    events: list = []

    def _tee(event_type: str, data: dict) -> None:
        events.append((event_type, dict(data)))
        if forward:
            forward(event_type, data)

    engine._emit_event_callback = _tee
    return events


def _overlapping(house) -> bool:
    return (
        bool(house.fan.is_on) and house.thermostat.mode in ("heat", "cool") and house.thermostat.action in _COMPRESSOR
    )


async def _watch_overlap(house, seconds: int) -> tuple[int | None, int | None]:
    """Step 1 s at a time (see ``_advance_finely``) for ``seconds``.

    Returns (second the fan and heating/cooling were first seen running together, second that overlap ended), None
    when it never started / never ended. Keeps stepping after the end so a test can also see the overlap not return.
    """
    start = end = None
    for t in range(int(seconds)):
        both = _overlapping(house)
        if both and start is None:
            start = t
        elif start is not None and end is None and not both:
            end = t
        await house.advance(seconds=1)
    return start, end


def _backstop(events, stopped: str | None = None) -> list:
    return [d for n, d in events if n == "whf_rule2_backstop" and (stopped is None or d.get("stopped") == stopped)]


def _ca_hvac_off(calls):
    return [
        c
        for c in _ca(calls)
        if c.domain == "climate" and c.service == "set_hvac_mode" and c.data.get("hvac_mode") == "off"
    ]


def _open_window_house(**extra):
    """A window open for less than the (long) debounce: no door/window pause yet, the heat keeps running."""
    return _sealed_house(ca_config={"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 3600}, **extra)


async def _heat_running(house) -> None:
    """The occupant asks for heat (setpoint above the 72 F room) and the compressor really runs."""
    await house.occupant_sets_thermostat(mode="heat", setpoint_f=80.0)
    await _advance_finely(house, 35)  # the toy thermostat starts the compressor on its next 30 s tick
    assert house.thermostat.action == "heating", "precondition: the heat is really running"


async def _fan_then_heat_inside_the_echo_guard(house) -> None:
    """Fan on -> Climate Advisor turns the heat off for it -> the occupant presses heat again 1 s later.

    The second press lands inside Climate Advisor's 3 s HVAC echo guard, so it is read as Climate Advisor's own
    write: no occupant HVAC-on stamp exists for rule 2 to compare (the press is missed).
    """
    ae = house.coordinator.automation_engine
    await _press_after_refresh(house, True)
    await _advance_finely(house, 1)
    assert house.thermostat.mode == "off", "precondition: rule 2 suppressed the heat for the newer fan press"
    assert ae._pre_fan_hvac_mode == "heat", "precondition: the fan session owns the HVAC"
    await _advance_finely(house, 1)
    await house.occupant_sets_thermostat(mode="heat")
    assert ae._occupant_hvac_on_at is None, "precondition: the heat press was missed (read as CA's own echo)"


async def test_rule2_backstop_heat_pressed_1s_after_the_fans_suppression_stops_one_device(make_house) -> None:
    """Issue #1288: someone opens a window, the heat is running, they switch the whole-house fan on, Climate Advisor
    turns the heat off for it, and they press heat again a second later. That press is inside the 3 s echo guard and
    is never seen as theirs. Before the backstop the fan and the heat then ran together (the fan session already owned
    the HVAC, so rule 2 had nothing left to do). Now one device is stopped within 30 s of the overlap starting: the
    fan, because the heat press is the newer action.

    Real HA reproduces the missed press here (no stamp is patched); see the precondition asserts.
    """
    house = await make_house(fan="whf", **_open_window_house())
    events = _tee_events(house)
    await house.open("window")
    await _advance_finely(house, 5)
    await _heat_running(house)
    assert not house.coordinator.automation_engine._paused_by_door, "precondition: no door/window pause yet"
    calls_before = len(house.calls)

    await _fan_then_heat_inside_the_echo_guard(house)
    start, end = await _watch_overlap(house, 90)

    assert start is not None, "precondition: the fan and the heat ran together (the missed press reproduced)"
    assert end is not None and end - start <= 30, f"fan and heat still both running {90 - start}s after overlapping"
    assert _backstop(events, "fan"), f"no whf_rule2_backstop event for the stop: {events}"
    assert _ca_fan_off(house.calls[calls_before:]), "the newer heat press must win: the fan is the device stopped"
    assert not house.fan.is_on


async def test_rule2_backstop_door_pause_overridden_then_fan_then_heat_stops_one_device(make_house) -> None:
    """Issue #1288 with a door pause: the door has been open long enough that Climate Advisor paused the HVAC. The
    occupant overrides the pause with heat, then switches the whole-house fan on (Climate Advisor turns the heat off
    for it) and presses heat again 1 s later. That press is missed (inside the echo guard), so without the backstop
    the fan and the heat run together. Now the fan is stopped within 30 s of the overlap starting.
    """
    house = await make_house(fan="whf", **_sealed_house(contacts=["door"]))
    ae = house.coordinator.automation_engine
    events = _tee_events(house)
    await house.open("door")
    await house.advance(minutes=2)
    assert ae._paused_by_door, "precondition: the open door paused the HVAC"
    await _heat_running(house)  # the occupant's heat is a manual override of the pause
    calls_before = len(house.calls)

    await _fan_then_heat_inside_the_echo_guard(house)
    start, end = await _watch_overlap(house, 90)

    assert start is not None, "precondition: the fan and the heat ran together (the missed press reproduced)"
    assert end is not None and end - start <= 30, f"fan and heat still both running {90 - start}s after overlapping"
    assert _backstop(events, "fan"), f"no whf_rule2_backstop event for the stop: {events}"
    assert _ca_fan_off(house.calls[calls_before:])
    assert not house.fan.is_on


async def test_fast_heat_press_during_a_running_door_pause_never_runs_beside_the_fan(make_house) -> None:
    """Control for the test above: the door pause is still running (HVAC off) when the occupant switches the fan on
    and presses heat 1 s later. In real HA the press is missed (echo guard) and the pause's own classification cycle
    turns the heat off again before the compressor starts, so the fan and the heat never run together at all. Pins
    that the backstop adds no second stop here (the fan, legitimately ventilating an open house, keeps running).
    This one passes with or without the backstop.
    """
    house = await make_house(fan="whf", **_sealed_house(contacts=["door"], initial_hvac_mode="heat"))
    ae = house.coordinator.automation_engine
    events = _tee_events(house)
    await house.occupant_sets_thermostat(setpoint_f=80.0)
    await house.advance(minutes=2)
    await house.open("door")
    await house.advance(minutes=2)
    assert ae._paused_by_door and house.thermostat.mode == "off", "precondition: the door pause is running"
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    await _advance_finely(house, 1)
    await house.occupant_sets_thermostat(mode="heat")
    start, _ = await _watch_overlap(house, 60)

    assert start is None, "the fan and the heat ran together during a door pause"
    assert house.fan.is_on and house.thermostat.mode == "off"
    assert not _ca_fan_off(house.calls[calls_before:])
    assert not _backstop(events), events


async def test_mirror_fan_pressed_beside_running_heat_suppresses_hvac_and_keeps_the_fan(make_house) -> None:
    """Symmetry (fan-on AFTER heat-on): the heat is running with a window open and the occupant switches the
    whole-house fan on. The fan is the newer action, so the heat is turned off and the fan keeps running. The
    backstop must not reverse this (it never stops the fan here, and the overlap stays under 30 s)."""
    house = await make_house(fan="whf", **_open_window_house())
    events = _tee_events(house)
    await house.open("window")
    await _advance_finely(house, 5)
    await _heat_running(house)
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    start, end = await _watch_overlap(house, 90)

    assert start is None or (end is not None and end - start <= 30), (start, end)
    assert house.thermostat.mode == "off" and house.thermostat.action not in _COMPRESSOR, "the heat kept running"
    assert house.fan.is_on, "the occupant's newer fan press must win"
    assert not _ca_fan_off(house.calls[calls_before:])
    assert not _backstop(events, "fan"), events


async def test_rule2_backstop_observe_only_notifies_once_and_sends_no_command(make_house) -> None:
    """Observe-only (the occupant switched Climate Advisor's automation off): the heat runs and the occupant switches
    the whole-house fan on beside it. Climate Advisor sends no command, so both keep running, but the occupant is told
    once that the fan and the heating/cooling are on together."""
    house = await make_house(fan="whf", **_open_window_house())
    events = _tee_events(house)
    await house.open("window")
    await _advance_finely(house, 5)
    await _heat_running(house)
    await house.set_automation(False)
    await house.advance(minutes=1)
    notes_before = len(house.notifications)
    calls_before = len(house.calls)

    await _press_after_refresh(house, True)
    start, end = await _watch_overlap(house, 90)

    assert start is not None and end is None, "observe-only: both devices keep running"
    assert not _ca_fan_off(house.calls[calls_before:]), "observe-only sent fan.turn_off"
    assert not _ca_hvac_off(house.calls[calls_before:]), "observe-only sent set_hvac_mode off"
    rule2_notes = [n for n in house.notifications[notes_before:] if "at the same time" in n.data.get("message", "")]
    assert len(rule2_notes) == 1, house.notifications[notes_before:]
    assert [n for n, _ in events].count("whf_rule2_observe_notified") == 1, events


# ── Issue #1293: observe-only records no HVAC ownership / pause it never sent ──────────────────────────────


def _suppressed(events) -> list:
    return [d for n, d in events if n == "whf_hvac_suppressed"]


async def _observe_only_fan_press_beside_heat(house):
    """Heat running, automation switched off, then the occupant switches the fan on."""
    events = _tee_events(house)
    await house.open("window")
    await _advance_finely(house, 5)
    await _heat_running(house)
    await house.set_automation(False)
    await house.advance(minutes=1)
    calls_before = len(house.calls)
    await _press_after_refresh(house, True)
    await _advance_finely(house, 20)
    return events, calls_before


@pytest.mark.parametrize("fan", ["whf", "whf_dual"])
async def test_observe_only_fan_press_takes_no_hvac_ownership_and_reenable_applies_rule2(make_house, fan) -> None:
    """Issue #1293: automation is off and the occupant switches the whole-house fan on beside the running heat.
    Climate Advisor sends nothing (the heat keeps running) and must not record that it suppressed the heat: no
    ``whf_hvac_suppressed``, no fan-session ownership of the thermostat. When automation is switched back on, rule 2
    is applied normally (the fan is the newer action, so the heat is turned off and the fan session owns it from
    then on) within ~30 s, through the press-based rule, not the state backstop that only catches missed presses.
    Before the fix the stale ownership made the press-based rule skip (it read "already owned"), so only the
    backstop stopped the heat."""
    house = await make_house(fan=fan, **_open_window_house())
    ae = house.coordinator.automation_engine
    events, calls_before = await _observe_only_fan_press_beside_heat(house)

    assert house.fan.is_on and house.thermostat.mode == "heat", "observe-only: both keep running"
    assert not _ca_hvac_off(house.calls[calls_before:]), "observe-only sent set_hvac_mode off"
    assert ae._pre_fan_hvac_mode is None, "observe-only recorded a suppression it never sent"
    assert not ae._whf_owns_hvac(), "observe-only: the fan session must not own the thermostat"
    assert not _suppressed(events), f"observe-only emitted whf_hvac_suppressed with nothing sent: {events}"

    calls_before = len(house.calls)
    await house.set_automation(True)
    start, end = await _watch_overlap(house, 60)

    assert start is None or (end is not None and end - start <= 30), (start, end)
    assert house.thermostat.mode == "off", "rule 2 was not applied after the re-enable"
    assert house.fan.is_on, "the occupant's newer fan press must win"
    assert _ca_hvac_off(house.calls[calls_before:]), "the heat was not turned off by Climate Advisor"
    assert ae._pre_fan_hvac_mode == "heat" and ae._whf_owns_hvac(), "ownership must follow the real suppression"
    assert len(_suppressed(events)) == 1, events
    assert not _backstop(events), f"the press was seen: the backstop must not be the one acting: {events}"


async def test_observe_only_blower_press_records_nothing_and_reenable_leaves_heat(make_house) -> None:
    """Issue #1293 control, ``hvac_fan``: the thermostat's own blower coexists with the heat by design, so neither in
    observe-only nor after the re-enable does Climate Advisor own or turn off the heat for it."""
    house = await make_house(fan="hvac_fan", **_open_window_house())
    ae = house.coordinator.automation_engine
    events = _tee_events(house)
    await house.open("window")
    await _advance_finely(house, 5)
    await _heat_running(house)
    await house.set_automation(False)
    await house.advance(minutes=1)
    await house.occupant_sets_fan_mode("on")
    await _advance_finely(house, 20)
    assert ae._pre_fan_hvac_mode is None and not ae._whf_owns_hvac()
    calls_before = len(house.calls)

    await house.set_automation(True)
    await _advance_finely(house, 60)

    assert house.thermostat.mode == "heat", "the blower must not turn the occupant's heat off"
    assert not _ca_hvac_off(house.calls[calls_before:])
    assert ae._pre_fan_hvac_mode is None and not ae._whf_owns_hvac()
    assert not _suppressed(events), events


_DOOR_HOUSE = {
    "fan": "whf",
    "contacts": ["window"],
    "startup": "settled",
    "initial_indoor_f": 82.0,
    # The #1132 house: warm morning, forecast peak inside the nat-vent look-ahead, so an open window means "pause".
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "outdoor_f": 73.5,
    "high_f": 95.0,
    "low_f": 52.0,
    "initial_hvac_mode": "cool",
    "ca_config": {"sensor_debounce_seconds": 30, "override_confirm_seconds": 60, "manual_grace_seconds": 0},
}


async def test_observe_only_window_records_no_pause_and_reenable_pauses_for_real(make_house) -> None:
    """Issue #1293 door-pause analogue: automation is off and a window opens while Climate Advisor's cooling runs.
    Nothing is sent (the cooling keeps running), so no pause may be recorded. Before the fix Climate Advisor
    recorded a "cool→off" pause anyway; after the re-enable it believed the HVAC was already paused and the cooling
    ran into the open window until it closed. Now the re-enable pauses it for real within ~30 s."""
    house = await make_house(**_DOOR_HOUSE)
    ae = house.coordinator.automation_engine
    events = _tee_events(house)
    await house.advance(minutes=6)  # past the 300 s start-up window
    assert house.thermostat.mode == "cool", "precondition: Climate Advisor's cooling runs"
    await house.set_automation(False)
    await house.advance(minutes=1)
    await house.open("window")
    await house.advance(minutes=3)  # past the 30 s debounce

    assert house.thermostat.mode == "cool", "observe-only: the cooling keeps running"
    assert not ae._paused_by_door, "observe-only recorded a pause it never sent"
    assert ae._pre_pause_mode is None
    assert not [d for n, d in events if n == "sensor_opened" and d.get("hvac_mode_change")], events

    calls_before = len(house.calls)
    await house.set_automation(True)
    await _advance_finely(house, 30)

    assert house.thermostat.mode == "off", "the re-enable left the cooling running into the open window"
    assert _ca_hvac_off(house.calls[calls_before:])
    assert ae._paused_by_door and ae._pre_pause_mode == "cool"

    await house.close_contact("window")
    await house.advance(minutes=3)
    assert house.thermostat.mode == "cool", "closing the window must restore the cooling"
