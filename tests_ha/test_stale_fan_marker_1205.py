"""Real-HA tests for Issue #1205: the occupant's fan-off ALWAYS holds the fan off; the #829 marker is label-only.

Occupant story: someone in the home switches off a fan (the whole-house fan in a whole_house_fan house, the thermostat
blower in an hvac_fan house) that Climate Advisor started, took over or restarted after an earlier manual fan-on.
Before #1205 Climate Advisor read that off as "closing the earlier manual session" and held nothing, so with a window
open and cool air outside it switched the fan back on within minutes.

Owner decision (2026-11-01): CA always knows how a run should end (an RF timer by time = the #530 settle window; a
no-timer run by its default timer, then CA's own turn-off or adopt-on), so every fan-off CA did not command, outside
the settle window, is a person and starts the normal fan-off grace. The marker only selects the Activity Record
wording. Every scenario below asserts the fan STAYS OFF for the whole hold: the house advances in 10 s steps (the
real coordinator cycle and nat-vent checks run on their own), with the window open and nat-vent favourable, and the
test fails with ``_OccupantOutcomeViolated`` if CA commands the fan on before the grace ends.

Scenarios (each in both supported fan modes; ``fan_mode: both`` is unsupported, #1175): 1 ordinary start then off;
2 R1; 3 R2; 4 manual no-timer fan-on, default grace expires, reconcile adopt-on (A1/A2), then off; 5 A4 grace-expiry
restart then off; 6 A3 deferred-then-applied restart then off; 7 CA cycle-off/on inside a marked run then off;
8 the #530 settle window and the reconcile turn-off stay NO hold.

Harness notes: ``house.advance`` steps 30 s slices, so timer-sensitive stretches advance in 10 s steps. The occupant's
off must come more than 120 s after Climate Advisor's own last thermostat command, else the coordinator reads it as
the thermostat confirming that command.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.house

_FANS = ["whf", "hvac_fan"]
_GRACE_S = 120
_FLOOR_S = 300  # FAN_MIN_TOGGLE_INTERVAL_S
_MATCHED_LOG = "holding fan off (fan_off grace); closes session from earlier manual fan-on"
_PLAIN_LOG = "Fan turned off by occupant — holding fan off (fan_off grace): fan="
_FAN_OFF_GRACE_LOG = "starting fan_off grace period"


class _OccupantOutcomeViolated(AssertionError):
    """The occupant's off of the running session was ignored (the #1205 symptom)."""


_HOUSE = {
    "contacts": ["window"],
    "startup": "settled",
    "profile": "scripted",  # indoor stays 72F: no comfort-floor exit, the only exits are the ones the test causes
    "start": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
    "initial_indoor_f": 72.0,
    "outdoor_f": 65.0,
    "high_f": 70.0,
    "low_f": 55.0,
    "initial_hvac_mode": "cool",
}


async def _house(make_house, fan: str, caplog, ca_extra: dict | None = None, **overrides):
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    ca_config = {"fan_min_runtime_per_hour": 0, "sensor_debounce_seconds": 30, "manual_grace_seconds": _GRACE_S}
    ca_config.update(ca_extra or {})
    if fan == "hvac_fan":
        # The thermostat blower has no fan entity. The harness stores fan_entity=None, which makes the fan-drift
        # backstop (_reconcile_fan_physical_drift) raise AttributeError on hass.states.get(None) once a session is
        # stranded -- a harness-only defect (real config flows omit the key), Issue #1213; leave the key out so this
        # file isolates #1205.
        ca_config["fan_entity"] = ...
    house = await make_house(
        fan=fan,
        ca_config=ca_config,
        **{**_HOUSE, **overrides},
    )
    await _steps(house, 360)  # past the 300 s start-up window
    _record_events(house)
    return house


def _record_events(house) -> None:
    """Tee the engine's Activity Record events (fan_cancel label, fan_activated, ...) into ``house.ca_events``."""
    engine = house.coordinator.automation_engine
    forward = engine._emit_event_callback
    house.ca_events = []

    def _tee(event_type: str, data: dict) -> None:
        house.ca_events.append((event_type, dict(data)))
        forward(event_type, data)

    engine._emit_event_callback = _tee


def _last_cancel(house) -> dict:
    return [d for n, d in house.ca_events if n == "fan_cancel"][-1]


async def _steps(house, seconds: float, step: float = 10) -> None:
    for _ in range(int(seconds // step)):
        await house.advance(seconds=step)


def _fan_on(house, fan: str) -> bool:
    return house.fan.is_on if fan == "whf" else house.thermostat.fan_mode == "on"


def _ca_starts(house, fan: str) -> list:
    """Climate Advisor's own fan-on commands (ground truth from the service-call bus)."""
    if fan == "whf":
        return [c for c in house.calls if c.origin.startswith("ca:") and c.domain == "fan" and c.service == "turn_on"]
    return [
        c
        for c in house.calls
        if c.origin.startswith("ca:")
        and c.domain == "climate"
        and c.service == "set_fan_mode"
        and c.data.get("fan_mode") == "on"
    ]


async def _occupant_off(house, fan: str) -> None:
    if fan == "whf":
        await house.occupant_presses_fan(False)
    else:
        await house.occupant_sets_fan_mode("auto")


async def _ordinary_ca_start(house, fan: str) -> None:
    """The window opens (past the debounce) and Climate Advisor starts nat-vent: one new CA fan-on command."""
    starts_before = len(_ca_starts(house, fan))
    await house.open("window")
    await _steps(house, 90)
    assert len(_ca_starts(house, fan)) == starts_before + 1, "precondition: an ordinary CA nat-vent start must run"
    assert _fan_on(house, fan)
    engine = house.coordinator.automation_engine
    assert engine._fan_active and engine._natural_vent_active


async def _wait_until_start_age(house, fan: str, seconds: float) -> None:
    """Advance until the last CA start is ``seconds`` old (the occupant's off must not look like an echo)."""
    started = _ca_starts(house, fan)[-1].at
    while (dt_util.utcnow() - started).total_seconds() < seconds:
        await house.advance(seconds=10)


async def _occupant_turns_off_running_fan(house, fan: str, caplog) -> int:
    """The occupant stops the running fan; returns the caplog mark taken just before, for evidence."""
    await _wait_until_start_age(house, fan, 140)
    mark = len(caplog.records)
    await _occupant_off(house, fan)
    await _steps(house, 10)
    return mark


async def _assert_fan_held_off(
    house, fan: str, caplog, mark: int, *, matched_kind: str | None = None, refresh_every_s: int | None = None
) -> None:
    """A fan-off grace is running and CA never commands the fan on until it ends (checked every 10 s step).

    ``matched_kind`` is the manual-origin label kind ("takeover"/"restart") the Activity Record must carry, or None
    for the plain ``fan_off`` wording. ``refresh_every_s``: also run the REAL coordinator cycle (which calls the
    nat-vent and economizer checks) at that period; the harness quiesces the periodic refresh, so a test that needs
    those checks inside the hold must drive them (Issue #134 x #1205).
    """
    engine = house.coordinator.automation_engine
    messages = [r.getMessage() for r in caplog.records[mark:]]
    problems: list[str] = []
    cancel = _last_cancel(house)
    if matched_kind is None:
        if cancel.get("trigger") != "fan_off" or not any(_PLAIN_LOG in m for m in messages):
            problems.append(f"label: expected the plain fan_off wording, got {cancel!r}")
    elif (
        cancel.get("trigger") != "manual_origin_carryforward"
        or cancel.get("manual_origin_kind") != matched_kind
        or not any(_MATCHED_LOG in m for m in messages)
    ):
        problems.append(f"label: expected manual_origin_carryforward/{matched_kind}, got {cancel!r}")
    if not engine._grace_is_fan_off_grace():
        problems.append(
            f"no fan_off grace (grace_active={engine._grace_active}, trigger={engine._last_grace_trigger!r})"
        )
    starts_before = len(_ca_starts(house, fan))
    events_before = len(house.ca_events)
    # The hold is the configured fan-off grace, driven for its whole length even when no grace is running (the
    # pre-#1205 symptom), so the fan being switched back on is what fails, not only the missing grace.
    held_s = 0
    restarted = False
    while held_s < _GRACE_S - 20 and not restarted:
        await house.advance(seconds=10)
        held_s += 10
        if refresh_every_s and held_s % refresh_every_s == 0:
            await house.coordinator.async_refresh()
            await house.advance(seconds=0)
        if len(_ca_starts(house, fan)) != starts_before or _fan_on(house, fan) or engine._fan_active:
            problems.append(f"CA turned the fan back on {held_s} s into the occupant's hold")
            restarted = True
        if any(n in ("fan_activated", "nat_vent_reactivated_while_paused") for n, _ in house.ca_events[events_before:]):
            problems.append(f"a fan start was recorded {held_s} s into the hold")
            restarted = True
    if problems:
        raise _OccupantOutcomeViolated("; ".join(problems))


# ---------------------------------------------------------------------------
# Control: the assertion itself is sound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
async def test_control_ordinary_start_then_occupant_off_starts_fan_off_grace(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    await _ordinary_ca_start(house, fan)

    mark = await _occupant_turns_off_running_fan(house, fan, caplog)

    assert any(_FAN_OFF_GRACE_LOG in r.getMessage() for r in caplog.records[mark:]), "no fan_off grace logged"
    await _assert_fan_held_off(house, fan, caplog, mark)


# ---------------------------------------------------------------------------
# R1 / R2: the first session ends in the occupant's off, whose grace expires with the window open
# ---------------------------------------------------------------------------


async def _first_session_stopped_by_occupant(house, fan: str, caplog, *, off_at_age: float) -> None:
    """Window opens, CA starts nat-vent, the occupant stops the fan at ``off_at_age`` seconds after the start. That
    off is an ordinary one (no marker yet): a fan-off grace starts."""
    engine = house.coordinator.automation_engine
    await _ordinary_ca_start(house, fan)
    await _wait_until_start_age(house, fan, off_at_age)
    await _occupant_off(house, fan)
    await _steps(house, 10)
    assert engine._grace_is_fan_off_grace(), "precondition: the first off is ordinary and starts a fan-off grace"
    assert not _fan_on(house, fan)


async def _grace_expires_with_window_open(house) -> None:
    engine = house.coordinator.automation_engine
    for _ in range(30):
        if not engine._grace_active:
            break
        await house.advance(seconds=10)
    assert not engine._grace_active, "precondition: the fan-off grace expired"
    await _steps(house, 10)


@pytest.mark.parametrize("fan", _FANS)
async def test_r1_abandoned_deferred_reactivation_then_unrelated_start_off_gets_grace(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    # off at +140 s, grace 120 s: expiry at about +260 s, inside the 300 s toggle floor
    await _first_session_stopped_by_occupant(house, fan, caplog, off_at_age=140)
    await _grace_expires_with_window_open(house)

    # the toggle floor deferred the reactivation: no new fan start, still paused for the open window
    assert len(_ca_starts(house, fan)) == 1
    assert not _fan_on(house, fan)
    assert engine._fan_rate_limited_until is not None, "precondition: the reactivation was rate limited"
    assert engine._paused_by_door is True

    # the window closes before any retry: the deferred start is abandoned and the fan never ran again
    await house.close_contact("window")
    await _steps(house, 30)
    assert len(_ca_starts(house, fan)) == 1
    assert not _fan_on(house, fan)

    await _steps(house, 600)  # well past the floor, the sealed-house resume grace and every timer
    await _ordinary_ca_start(house, fan)
    mark = await _occupant_turns_off_running_fan(house, fan, caplog)
    await _assert_fan_held_off(house, fan, caplog, mark)


@pytest.mark.parametrize("fan", _FANS)
async def test_r2_applied_reactivation_ended_by_ca_then_unrelated_start_off_gets_grace(make_house, fan, caplog) -> None:
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    # off at +200 s, grace 120 s: expiry at about +320 s, past the 300 s toggle floor, so the reactivation runs
    await _first_session_stopped_by_occupant(house, fan, caplog, off_at_age=200)
    await _grace_expires_with_window_open(house)

    assert len(_ca_starts(house, fan)) == 2, "precondition: the grace-expiry reactivation was applied"
    assert _fan_on(house, fan)
    assert engine._fan_active and engine._natural_vent_active

    # CA itself ends that session when the window closes (past the toggle floor): no occupant off is ever seen
    await _steps(house, _FLOOR_S + 30)
    await house.close_contact("window")
    await _steps(house, 90)
    assert not _fan_on(house, fan), "precondition: CA stopped the fan itself"
    assert not engine._fan_active and not engine._natural_vent_active

    await _steps(house, 600)
    await _ordinary_ca_start(house, fan)
    mark = await _occupant_turns_off_running_fan(house, fan, caplog)
    await _assert_fan_held_off(house, fan, caplog, mark)


# ---------------------------------------------------------------------------
# Issue #1205 (flipped from #829): the off of THE run CA took over / restarted IS held, and carries the origin label
# ---------------------------------------------------------------------------


async def _restarted_run_after_first_session(house, fan: str, caplog) -> None:
    """Scenario 5 set-up (A4): the occupant stops CA's first run, the grace expires with the window open and the
    floor passed, and CA restarts the fan (a second CA start, marked)."""
    engine = house.coordinator.automation_engine
    # off at +200 s, grace 120 s: expiry at about +320 s, past the 300 s toggle floor, so the reactivation runs
    await _first_session_stopped_by_occupant(house, fan, caplog, off_at_age=200)
    await _grace_expires_with_window_open(house)
    assert len(_ca_starts(house, fan)) == 2, "precondition: the grace-expiry reactivation was applied"
    assert _fan_on(house, fan)
    assert engine._fan_manual_origin_unclosed is True
    assert engine._fan_manual_origin_run == engine._fan_on_since


@pytest.mark.parametrize("fan", _FANS)
async def test_a4_grace_expiry_restart_then_its_own_off_is_held_and_labelled(make_house, fan, caplog) -> None:
    """Scenario 5. The grace expires with the window open and CA restarts the fan (visible in the Activity Record as
    ``nat_vent_reactivated_while_paused``). The person turns THAT run off: the fan stays off for the whole hold, and
    the label says CA restarted it after the grace ended."""
    house = await _house(make_house, fan, caplog)
    await _restarted_run_after_first_session(house, fan, caplog)
    assert "nat_vent_reactivated_while_paused" in [n for n, _ in house.ca_events], "the restart is on the record"

    # more than 120 s after CA's last thermostat command, else the off reads as the thermostat's confirmation
    mark = await _occupant_turns_off_running_fan(house, fan, caplog)

    await _assert_fan_held_off(house, fan, caplog, mark, matched_kind="restart")
    assert house.coordinator.automation_engine._fan_manual_origin_unclosed is False, "one-shot: consumed"


# ---------------------------------------------------------------------------
# Scenario 7: a CA cycle-off/on inside a marked run, then the person's off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
async def test_ca_cycle_off_on_inside_a_marked_run_off_is_held_with_the_plain_label(make_house, fan, caplog) -> None:
    """The marker is bound to ONE run. CA ends the marked run itself (window closed past the floor), then the window
    opens again and CA starts a NEW run. LABEL CHOICE: plain ``fan_off`` for the person's off of that new run; it is
    a nat-vent run CA started, not the run it restarted, so the restart wording would be wrong. Held either way."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    await _restarted_run_after_first_session(house, fan, caplog)
    marked_run = engine._fan_on_since

    await _steps(house, _FLOOR_S + 30)
    await house.close_contact("window")  # CA's own stop of the marked run
    await _steps(house, 90)
    assert not _fan_on(house, fan) and not engine._fan_active
    await _steps(house, 600)
    await _ordinary_ca_start(house, fan)  # CA's new run
    assert engine._fan_on_since != marked_run

    # off at +200 s, grace 120 s: the grace ends past the 300 s toggle floor, so CA's restart is immediate
    await _wait_until_start_age(house, fan, 200)
    mark = len(caplog.records)
    await _occupant_off(house, fan)
    await _steps(house, 10)

    await _assert_fan_held_off(house, fan, caplog, mark)

    # The hold ends with the window still open: CA restarts the fan once (marked, on the record), and a person's off
    # of THAT restarted run is held again, with the restart label.
    starts_before = len(_ca_starts(house, fan))
    await _grace_expires_with_window_open(house)
    assert len(_ca_starts(house, fan)) == starts_before + 1, "precondition: CA restarted the fan after the hold"
    assert engine._fan_manual_origin_unclosed is True
    mark = await _occupant_turns_off_running_fan(house, fan, caplog)
    await _assert_fan_held_off(house, fan, caplog, mark, matched_kind="restart")


# ---------------------------------------------------------------------------
# Scenario 6 (A3): the restart is deferred by the 5-minute floor and then applied (PAUSED and PAUSED_IDLE)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fan", _FANS)
async def test_a3_deferred_then_applied_restart_then_its_own_off_is_held_and_labelled(make_house, fan, caplog) -> None:
    """The grace expires inside the toggle floor, so the restart is deferred (nothing runs yet); with the window still
    open the next cycle after the floor applies it (PAUSED: the helper retry; PAUSED_IDLE: the HVAC-already-off
    branch). The person turns THAT run off: the fan stays off for the whole hold; the label is the restart wording."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    # off at +140 s, grace 120 s: expiry at about +260 s, inside the 300 s toggle floor
    await _first_session_stopped_by_occupant(house, fan, caplog, off_at_age=140)
    await _grace_expires_with_window_open(house)
    assert len(_ca_starts(house, fan)) == 1 and not _fan_on(house, fan)
    assert engine._fan_rate_limited_until is not None, "precondition: the reactivation was deferred"
    assert engine._paused_by_door is True
    assert bool(engine._paused_with_hvac_already_off) == (fan == "whf"), "precondition: the pause shape"

    # the floor ends with the window still open: the retry applies the deferred start
    for _ in range(60):
        if len(_ca_starts(house, fan)) == 2:
            break
        await house.advance(seconds=10)
    assert len(_ca_starts(house, fan)) == 2, "precondition: the deferred restart applied"
    assert _fan_on(house, fan) and engine._fan_active
    assert engine._fan_manual_origin_unclosed is True, "the pending fact was promoted to the run-bound marker"

    mark = await _occupant_turns_off_running_fan(house, fan, caplog)

    await _assert_fan_held_off(house, fan, caplog, mark, matched_kind="restart")


# ---------------------------------------------------------------------------
# Scenario 4 (A1/A2): a manual no-timer fan-on, the default grace expires, CA keeps the running fan, then the off
# ---------------------------------------------------------------------------


async def _manual_no_timer_fan_on_then_default_grace_expires(house, fan: str) -> None:
    """A person switches the fan on with no timer, inside the sensor debounce of an opening window (whole-house fan)
    or with the window still shut (blower); the default 120 s grace expires with the window open and the fan running."""
    engine = house.coordinator.automation_engine
    if fan == "whf":
        await house.open("window")
        await _steps(house, 10)
        await house.occupant_presses_fan(True)
    else:
        await house.occupant_sets_fan_mode("on")
        await _steps(house, 10)
        await house.open("window")
    await _steps(house, 10)
    assert engine._fan_override_active and engine._grace_active, "precondition: the manual no-timer override"
    assert engine._fan_remote_timer_hours is None
    for _ in range(30):
        if not engine._grace_active:
            break
        await house.advance(seconds=10)
    assert not engine._grace_active, "precondition: the default grace expired"
    assert _fan_on(house, fan) and engine._fan_active and engine._natural_vent_active, "CA kept the running fan"
    assert engine._fan_manual_origin_unclosed is True


@pytest.mark.parametrize("fan", _FANS)
async def test_a1_a2_manual_no_timer_fan_on_default_grace_expires_then_off_is_held(make_house, fan, caplog) -> None:
    """The whole-house fan route is the reconcile ``adopt-on`` (label: took over from your earlier manual fan-on). The
    house harness runs the blower in command-only mode (no fan-state feedback), where the post-grace reconcile does
    not run, so CA keeps the running blower through the grace-expiry restart instead (label: restarted after your
    grace ended); the reconcile route for both modes is in tests/test_stale_fan_marker_1205.py."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    await _manual_no_timer_fan_on_then_default_grace_expires(house, fan)
    if fan == "whf":
        assert any("decision=adopt-on" in r.getMessage() for r in caplog.records), "the reconcile adopted the fan"
    await _steps(house, 150)  # more than 120 s after CA's last command, else the off reads as an echo
    assert _fan_on(house, fan)
    mark = len(caplog.records)

    if fan == "whf":
        await house.occupant_presses_fan(False)
    else:
        await _occupant_off(house, fan)
    await _steps(house, 10)

    assert engine._fan_manual_origin_unclosed is False, "one-shot: consumed"
    await _assert_fan_held_off(house, fan, caplog, mark, matched_kind="takeover" if fan == "whf" else "restart")


# ---------------------------------------------------------------------------
# Scenario 8: NO hold -- the #530 settle window, and CA's own reconcile turn-off
# ---------------------------------------------------------------------------


async def test_selected_remote_timer_ending_on_schedule_starts_no_hold(make_house, caplog) -> None:
    """A 1 h remote timer is selected and runs its course. When CA's clock for it expires the hardware timer's own
    off lands moments later, inside the 120 s settle window (#530): the run ended on schedule, so NO hold and the
    label stays ``timer_boundary_settle``. UNCHANGED by #1205. (whole-house fan only: the QuietCool remote has no
    blower equivalent.)"""
    caplog.set_level(logging.INFO, logger="custom_components.climate_advisor")
    house = await make_house(
        fan="whf_dual",
        ca_config={
            "fan_remote_entity": "event.house_quietcool_remote",
            "fan_min_runtime_per_hour": 0,
            "sensor_debounce_seconds": 30,
        },
        **{**_HOUSE, "startup": "settled"},
    )
    await _steps(house, 360)
    _record_events(house)
    engine = house.coordinator.automation_engine
    await house.open("window")
    await _steps(house, 10)
    house.hass.states.async_set(
        "event.house_quietcool_remote",
        dt_util.utcnow().isoformat(timespec="milliseconds"),
        {"event_type": "timer_1h", "event_types": ["low", "high", "off", "timer_1h"]},
    )
    await house._drain()
    await house.occupant_presses_fan(True)
    await _steps(house, 30)
    assert engine._fan_override_active and engine._fan_remote_timer_hours == 1.0, "precondition: a 1 h timer"

    while engine._grace_active:
        await house.advance(seconds=30)
    assert engine._timer_boundary_settle_until is not None, "precondition: the settle window is armed"
    await house.advance(seconds=30)  # the hardware timer's own off lands a few seconds after CA's clock
    await house.occupant_presses_fan(False)
    await _steps(house, 10)

    assert _last_cancel(house)["trigger"] == "timer_boundary_settle"
    assert not engine._grace_is_fan_off_grace(), "a timer ending on schedule is not a person: no hold"


@pytest.mark.parametrize("fan", _FANS)
async def test_default_expiry_reconcile_turn_off_is_cas_own_off_and_starts_no_hold(make_house, fan, caplog) -> None:
    """No timer selected; the default grace expires while outdoor air has turned warmer than indoors. CA's own stop of
    the unwarranted fan (the reconcile ``turn-off`` for the whole-house fan) never reaches ``on_fan_turned_off()``:
    no ``fan_cancel`` and no fan_off grace. UNCHANGED by #1205: the hold is only for an off CA did not command."""
    house = await _house(make_house, fan, caplog)
    engine = house.coordinator.automation_engine
    if fan == "whf":
        await house.open("window")
        await _steps(house, 10)
        await house.occupant_presses_fan(True)
    else:
        await house.occupant_sets_fan_mode("on")
        await _steps(house, 10)
        await house.open("window")
    await _steps(house, 10)
    assert engine._fan_override_active and engine._grace_active
    house.set_outdoor(88.0, high_f=92.0, low_f=70.0)  # nat-vent no longer warranted when the grace expires
    for _ in range(30):
        if not engine._grace_active:
            break
        await house.advance(seconds=10)
    await _steps(house, 150)

    if fan == "whf":
        assert any("decision=turn-off" in r.getMessage() for r in caplog.records), "the reconcile turned the fan off"
    assert not _fan_on(house, fan), "CA stopped the unwarranted fan itself"
    assert "fan_cancel" not in [n for n, _ in house.ca_events]
    assert not engine._grace_is_fan_off_grace()


# ---------------------------------------------------------------------------
# Issue #134 x #1205: a house warmer than comfort_cool does not buy the fan back inside the occupant's hold
# ---------------------------------------------------------------------------
# hvac_fan: the reactivation gate refuses above the ceiling on its own (comfort_cool, +2 F with aggressive_savings),
# so the 78 F row never restarted the blower; the aggressive_savings 75.5 F row is the one #134 would have restarted.


@pytest.mark.parametrize(
    ("fan", "extra", "hot"),
    [
        pytest.param("whf", {}, 78.0, id="whf-78F"),
        pytest.param("hvac_fan", {}, 78.0, id="hvac_fan-78F"),
        pytest.param("hvac_fan", {"aggressive_savings": True}, 75.5, id="hvac_fan-savings-75.5F"),
    ],
)
async def test_134_hot_house_does_not_override_the_occupants_fan_off_hold(make_house, fan, extra, hot, caplog) -> None:
    house = await _house(make_house, fan, caplog, ca_extra=extra)
    engine = house.coordinator.automation_engine
    await _ordinary_ca_start(house, fan)
    # past the 300 s toggle floor, or the floor (not the hold) would defer a restart and hide the defect
    await _wait_until_start_age(house, fan, _FLOOR_S + 20)
    mark = len(caplog.records)
    await _occupant_off(house, fan)
    await _steps(house, 10)
    house.set_indoor(hot)  # the house is now above comfort_cool (74) with the window open and cooler air outside
    assert hot > engine.config["comfort_cool"]

    await _assert_fan_held_off(house, fan, caplog, mark, refresh_every_s=30)

    assert engine._grace_is_fan_off_grace()
    assert _fan_on(house, fan) is False


# ---------------------------------------------------------------------------
# Issue #1205 (owner decision 2026-11-01): the hold lasts until its OWN end, whatever the occupant does to the
# thermostat afterwards
# ---------------------------------------------------------------------------
# Occupant story: on a hot day with the window open the person switches the whole-house fan off, then sets the
# thermostat to Off. About two minutes later (test config; ten by default) the Off is confirmed as an override and its
# grace replaces the fan-off grace. The fan must still stay off until the pause they asked for ends.


async def test_whf_hot_day_thermostat_off_after_the_fan_off_keeps_the_fan_off_for_the_whole_hold(
    make_house, caplog
) -> None:
    fan = "whf"
    hold_s = 900
    house = await _house(
        make_house,
        fan,
        caplog,
        ca_extra={"manual_grace_seconds": hold_s, "override_confirm_seconds": 120},
        start=datetime(2026, 7, 15, 21, 0, tzinfo=UTC),  # evening: inside the economizer's 5 PM - midnight hours
    )
    engine = house.coordinator.automation_engine
    await _ordinary_ca_start(house, fan)
    await _wait_until_start_age(house, fan, _FLOOR_S + 20)
    await _occupant_off(house, fan)
    await _steps(house, 10)
    assert engine._grace_is_fan_off_grace(), "precondition: the off started the fan-off grace"
    hold_end = engine._fan_off_hold_until
    assert hold_end is not None

    # The day turns hot with cooler air outside. The person closes the window: Climate Advisor resumes the cooling
    # (a window that stays open would keep the thermostat paused Off, so an Off would change nothing).
    house.set_outdoor(65.0, high_f=86.0, low_f=58.0)
    house.set_indoor(78.0)
    assert engine.config["comfort_cool"] < 78.0
    await house.close_contact("window")
    await _steps(house, 60)
    await house.coordinator.async_refresh()
    await house.advance(seconds=0)
    assert engine._current_classification.day_type == "hot", engine._current_classification.day_type
    assert house.thermostat.mode == "cool", "precondition: with the window shut the cooling is back on"
    assert engine._fan_off_hold_active() and not _fan_on(house, fan)

    # They set the thermostat Off; two minutes later it is confirmed as an override and its grace replaces the
    # fan-off grace. Then they open the window again: the hot house wants the fan, cooler air is outside.
    await _steps(house, 130)  # more than 120 s after CA's own thermostat command, or the Off reads as its echo
    await house.occupant_sets_thermostat(mode="off")
    assert engine._override_confirm_pending, "precondition: the Off is awaiting confirmation"
    await _steps(house, 130)
    assert engine._manual_override_active and engine._manual_override_mode == "off"
    assert engine._last_grace_trigger == "override_confirmed" and not engine._grace_is_fan_off_grace()
    # a hot afternoon with cooler air outside: the window-cooling economizer wants the fan (it has no override check)
    house.set_outdoor(66.0, high_f=86.0, low_f=58.0)
    house.set_indoor(84.0)
    await house.coordinator.async_refresh()
    await house.open("window")
    await house.open("window")
    starts_before = len(_ca_starts(house, fan))
    events_before = len(house.ca_events)

    problems: list[str] = []
    held_s = 0
    while dt_util.utcnow() < hold_end - timedelta(seconds=10) and not problems:
        await house.advance(seconds=10)
        held_s += 10
        if held_s % 30 == 0:  # the real coordinator cycle runs the nat-vent and economizer checks
            await house.coordinator.async_refresh()
            await house.advance(seconds=0)
        if len(_ca_starts(house, fan)) != starts_before or _fan_on(house, fan) or engine._fan_active:
            problems.append(f"CA turned the fan back on {held_s} s after the window opened, inside the hold")
        if any(n in ("fan_activated", "nat_vent_reactivated_while_paused") for n, _ in house.ca_events[events_before:]):
            problems.append(f"a fan start was recorded {held_s} s into the hold")
    if problems:
        raise _OccupantOutcomeViolated("; ".join(problems))

    assert held_s >= 120, "the window must have been open for a real stretch of the hold"
    assert engine._fan_off_hold_active() and engine._fan_off_hold_until == hold_end
    assert not engine._grace_is_fan_off_grace()
    assert any("Fan-off hold continues past override_confirmed grace" in r.getMessage() for r in caplog.records)

    # the hold's own deadline passes: it ends and is logged, and CA starts the fan within one coordinator cycle.
    # That proves the house wanted the fan all along, so a hold that ended early would have started it inside the hold.
    await _steps(house, 30)
    assert not engine._fan_off_hold_active()
    assert any("Fan-off hold ended: reason=expired" in r.getMessage() for r in caplog.records)
    await house.advance(seconds=_FLOOR_S)
    await house.coordinator.async_refresh()
    await _steps(house, 30)
    assert len(_ca_starts(house, fan)) > starts_before and _fan_on(house, fan), (
        "after the hold CA starts the fan (the hot house, open window and cooler air outside want it)"
    )


# ---------------------------------------------------------------------------
# Issue #1205 final review: a thermostat mode-to-mode change (coordinator Fix D) does not cut the fan run short
# ---------------------------------------------------------------------------


async def test_hvac_fan_run_survives_thermostat_off_then_heat(make_house, caplog) -> None:
    """The occupant turns the thermostat blower on, sets the thermostat Off (confirmed), then Heat while that override's
    grace runs. The coordinator's mode-to-mode path replaces the Off override; the blower run is a separate request
    and must keep running to its own deadline."""
    fan = "hvac_fan"
    house = await _house(
        make_house, fan, caplog, ca_extra={"manual_grace_seconds": 900, "override_confirm_seconds": 120}
    )
    engine = house.coordinator.automation_engine
    await _steps(house, 150)  # more than 120 s after CA's own thermostat commands
    await house.occupant_sets_fan_mode("on")
    await _steps(house, 20)
    assert engine._fan_override_active, "precondition: the blower press is the occupant's fan run"
    run_end = engine._fan_override_until
    assert run_end is not None
    await house.occupant_sets_thermostat(mode="off")
    await _steps(house, 150)
    assert engine._manual_override_active and engine._manual_override_mode == "off", "precondition: Off confirmed"

    await house.occupant_sets_thermostat(mode="heat")
    await _steps(house, 30)

    assert any("New mode change during active override grace" in r.getMessage() for r in caplog.records), (
        "precondition: the coordinator's mode-to-mode path ran"
    )
    assert engine._fan_override_active, "the replaced thermostat override must not end the occupant's blower run"
    assert engine._fan_override_until == run_end
